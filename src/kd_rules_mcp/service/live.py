"""Живые проверки КД 2: чтение, предпросмотр и применение в песочницах."""

import base64
import hashlib
import io
import json
import re
import zipfile
from collections.abc import Callable
from typing import Any

from kd_rules_mcp.errors import (
    LiveBaseRefusedError,
    LiveCodeError,
    LiveError,
    LiveParseError,
    LiveStageError,
    PreviewMismatchError,
)
from kd_rules_mcp.live.access import DataMcpAccess, LiveAccess, resolve_base
from kd_rules_mcp.live.codegen import exchange, plans, rules
from kd_rules_mcp.live.codegen.base import base_check_code
from kd_rules_mcp.live.codegen.registration import (
    lookup_code,
    parse_lookup,
    registration_code,
    validate_registration_args,
)
from kd_rules_mcp.live.registration import (
    parse_registration,
    same_registration,
    summarize_registration,
)
from kd_rules_mcp.live.response import compatibility_error, short_error
from kd_rules_mcp.live.runs import (
    LiveRun,
    download_bytes,
    download_rule,
    execute,
    preview_hash,
    uploaded,
)
from kd_rules_mcp.projects import LocalSettings, ProjectConfigError
from kd_rules_mcp.service.base import ServiceBase
from kd_rules_mcp.service.structures import _extension_list
from kd_rules_mcp.service.views import project_structure_id


def _required(data: dict, name: str, kind: type) -> Any:
    value = data.get(name)
    if type(value) is not kind:
        raise LiveParseError(f"неверное поле ответа: {name}")
    return value


def _registration_lookup(
    by: str, value: str, day: str | None, ref: str | None = None
) -> dict[str, Any]:
    """Как искали объект. Дата — только если её передали, ссылка — когда она одна."""
    item: dict[str, Any] = {"by": by, "value": value}
    if day is not None:
        item["date"] = day
    if ref is not None:
        item["ref"] = ref
    return item


def _connection_parts(text: str) -> dict[str, str]:
    """Только адрес базы; произвольные параметры и учётные данные не выходят наружу."""
    return {
        key.casefold(): quoted.replace('""', '"') if quoted is not None else plain.strip()
        for key, quoted, plain in (
            (m[1], m[2], m[3])
            for m in re.finditer(
                r'(?:^|;)\s*([\w]+)\s*=\s*(?:"((?:[^"]|"")*)"|([^;]*))\s*(?=;|$)', text
            )
        )
        if key.casefold() in {"srvr", "ref", "file"}
    }


def _base_identity(expected: str, actual: dict[str, str]) -> dict:
    configured = _connection_parts(expected)

    def normalized(key: str, value: str) -> str:
        return (value.replace("/", "\\").rstrip("\\") if key == "file" else value).casefold()

    common = configured.keys() & actual.keys()
    different = [
        key
        for key in sorted(common)
        if normalized(key, configured[key]) != normalized(key, actual[key])
    ]
    # Нет общих ключей — база другого вида (файловая против серверной): это другая база.
    # Ключ есть только с одной стороны — сверена часть адреса: `ok` обещал бы больше сделанного.
    if different or not common:
        status = "error"
    elif configured.keys() != actual.keys():
        status = "warning"
    else:
        status = "ok"
    if different:
        verdict = f"Различаются: {', '.join(different)}"
    elif not common:
        verdict = "Нет общих ключей адреса: база другого вида или адрес не прочитан"
    elif status == "warning":
        verdict = f"Сверены только {', '.join(sorted(common))}"
    else:
        verdict = "Адрес совпадает"
    return {
        "check": "base.identity",
        "status": status,
        "message": f"Настройка: {configured}; база: {actual}. {verdict}",
    }


class LiveMixin(ServiceBase):
    """Фабрика доступа подменяется тестами; всё разрешение базы остаётся настоящим."""

    live_access_factory: Callable[[DataMcpAccess], LiveAccess] = staticmethod(lambda base: base)
    live_chunk_size: int = 48000

    def _live_access(self, ref: str) -> LiveAccess:
        # Личный файл машины сюда намеренно не попадает, как в контейнерном project_list.
        local = LocalSettings(project_dirs=self.settings.project_dirs)
        _, access = resolve_base(self._catalog(), local, ref)
        return self.live_access_factory(access)

    def _live_run(self, tool: str) -> LiveRun:
        return LiveRun(self.workspace.root, tool, self._host)

    def _live_rules_state(self, access: LiveAccess, plan: str, *, tolerant: bool = False) -> dict:
        result = {}
        for kind in rules.FILES:
            try:
                info = execute(access, rules.metadata_code(plan, kind))
                _required(info, "present", bool)
                if info["present"]:
                    _required(info, "binary", bool)
                    for field in ("source", "file", "info", "record_hash"):
                        _required(info, field, str)
                    if info["binary"]:
                        for field, typ in (("hash", str), ("size", int), ("length", int)):
                            _required(info, field, typ)
            except LiveError as error:
                if not tolerant:
                    raise
                info = {"present": None, "error": str(error), "code": error.code}
            result[kind] = info
        return result

    def _live_snapshot(
        self, access: LiveAccess, plan: str, state: dict
    ) -> tuple[dict, dict[str, bytes]]:
        """Снимок двух записей целиком; отсутствие записи — полное, известное состояние."""
        manifest: dict[str, Any] = {
            "schema": "kd2-live-register/2",
            "plan": plan,
            "records": {},
            "complete": True,
            "reasons": [],
        }
        xml: dict[str, bytes] = {}
        for kind in rules.RECORD_KINDS:
            info = state[kind]
            record: dict[str, Any] = {"present": info["present"]}
            manifest["records"][kind] = record
            if info["present"] is False:
                continue
            if info["present"] is None:
                manifest["reasons"].append(f"{kind}: запись не прочитана")
                continue
            try:
                fingerprint = info["record_hash"]
                meta = execute(access, rules.record_snapshot_code(plan, kind, fingerprint))
                data = download_bytes(
                    access,
                    meta,
                    lambda offset, count, digest, k=kind, f=fingerprint: rules.record_snapshot_code(
                        plan, k, f, offset=offset, size=count
                    ),
                    self.live_chunk_size,
                )
                fields = json.loads(data.decode("utf-8-sig"))
                if (
                    not isinstance(fields, dict)
                    or set(fields) != {"resources", "attributes"}
                    or any(
                        not isinstance(fields[group], dict)
                        or any(
                            not isinstance(k, str) or not isinstance(v, str)
                            for k, v in fields[group].items()
                        )
                        for group in ("resources", "attributes")
                    )
                    or not fields["resources"]
                ):
                    raise LiveParseError("неполный снимок: нужны ресурсы и реквизиты регистра")
                record.update(**fields, record_hash=fingerprint)
            except (LiveError, ValueError) as error:
                manifest["reasons"].append(f"{kind}: ресурсы не прочитаны: {error}")
            for xml_kind in (
                ("exchange", "correspondent") if kind == "exchange" else ("registration",)
            ):
                item = state[xml_kind]
                try:
                    if item.get("present") is not True or not item.get("binary"):
                        raise LiveParseError("XML отсутствует или не прочитан")
                    if item["record_hash"] != info["record_hash"]:
                        raise LiveParseError("запись изменилась между чтениями")
                    xml[xml_kind] = download_rule(
                        access, plan, xml_kind, item, self.live_chunk_size
                    )
                except LiveError as error:
                    manifest["reasons"].append(f"{xml_kind}: {error}")
        manifest["complete"] = not manifest["reasons"]
        return manifest, xml

    def _live_save_snapshot(self, snapshot: tuple[dict, dict[str, bytes]], run: LiveRun) -> dict:
        manifest, xml = snapshot
        out = run.path / "backup"
        out.mkdir()
        saved = []
        file_info = {}
        try:
            for kind, content in xml.items():
                path = out / rules.FILES[kind]
                path.write_bytes(content)
                digest = base64.b64encode(hashlib.sha256(content).digest()).decode("ascii")
                info = {"size": len(content), "hash": digest}
                file_info[kind] = info
                saved.append({"kind": kind, "path": self._host(path), **info})
            manifest = {**manifest, "files": file_info}
            (out / "manifest.json").write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
                newline="\n",
            )
        except Exception:
            for path in out.iterdir():
                path.unlink()
            out.rmdir()
            raise
        return {
            "folder": self._host(out),
            "manifest": self._host(out / "manifest.json"),
            "complete": manifest["complete"],
            "reasons": manifest["reasons"],
            "files": saved,
        }

    def _live_restore_input(self, files: str | dict[str, str], plan: str) -> dict | None:
        if not isinstance(files, str):
            return None
        candidate = self._read_path(files)
        folder = candidate if candidate.is_dir() else candidate.parent
        path = (
            folder / "manifest.json"
            if candidate.is_dir() or candidate.name == "manifest.json"
            else None
        )
        if path is None or not path.is_file():
            return None
        manifest = json.loads(path.read_text(encoding="utf-8"))
        if (
            not isinstance(manifest, dict)
            or manifest.get("schema") not in ("kd2-live-register/1", "kd2-live-register/2")
            or manifest.get("plan") != plan
            or manifest.get("complete") is not True
            or not isinstance(manifest.get("records"), dict)
            or set(manifest["records"]) != set(rules.RECORD_KINDS)
        ):
            raise ValueError("неполный снимок или другой план: восстановление запрещено")
        for kind, record in manifest["records"].items():
            if not isinstance(record, dict) or type(record.get("present")) is not bool:
                raise ValueError("неверная запись манифеста")
            if not record["present"]:
                continue
            for group in ("resources", "attributes"):
                values = record.get(group)
                if group == "attributes" and manifest["schema"] == "kd2-live-register/1":
                    continue
                if (
                    not isinstance(values, dict)
                    or (group == "resources" and not values)
                    or any(
                        not isinstance(k, str) or not isinstance(v, str) for k, v in values.items()
                    )
                ):
                    raise ValueError("неполный снимок ресурсов и реквизитов; снимите новый снимок")
            if not isinstance(record.get("record_hash"), str):
                raise ValueError("в снимке нет record_hash; снимите новый снимок")
            for xml_kind in (
                ("exchange", "correspondent") if kind == "exchange" else ("registration",)
            ):
                info = manifest.get("files", {}).get(xml_kind, {})
                content = (folder / rules.FILES[xml_kind]).read_bytes()
                digest = base64.b64encode(hashlib.sha256(content).digest()).decode("ascii")
                if info.get("size") != len(content) or info.get("hash") != digest:
                    raise ValueError("XML снимка изменён: размер или SHA256 не совпал")
        if manifest["schema"] == "kd2-live-register/1":
            manifest["complete"] = False
            manifest["reasons"] = ["снимок /1 не содержит реквизитов; снимите новый снимок /2"]
        return manifest

    def _live_nodes(self, access: LiveAccess, plan: str) -> dict:
        state = execute(access, exchange.nodes_code(plan))
        _required(state, "enabled", bool)
        for node in _required(state, "nodes", list):
            if not isinstance(node, dict):
                raise LiveParseError("неверный узел")
            for field, typ in (
                ("code", str),
                ("this", bool),
                ("ready", bool),
                ("sent", int),
                ("received", int),
                ("registrations", int),
            ):
                _required(node, field, typ)
            tables = _required(node, "registration_tables", dict)
            if (
                any(
                    not isinstance(name, str) or type(count) is not int or count <= 0
                    for name, count in tables.items()
                )
                or sum(tables.values()) != node["registrations"]
            ):
                raise LiveParseError("неверное число регистраций по таблицам")
        if len([n for n in state["nodes"] if n["this"]]) != 1:
            raise LiveParseError("не найден единственный этот узел")
        return state

    def live_base_check(
        self, base: str, plan: str | None = None, exchange: str | None = None
    ) -> dict[str, Any]:
        """Паспорт песочницы и сверки с профилем и кэшем структур без записи в базу."""
        catalog = self._catalog()
        selected = (catalog.exchange(exchange),) if exchange is not None else catalog.exchanges
        # Разрешение роли и доступа — до прогона; неизвестный обмен тоже отвергается до сети.
        access = self._live_access(base)
        project, _, base_id = base.partition(".")
        configured = catalog.base(project, base_id)
        profile_sides = [
            (item, side)
            for item in selected
            for side in item.sides
            if side.project == project and side.base == base_id
        ]
        if exchange is not None and not profile_sides:
            # Иначе проверка молча ушла бы в обзор всех планов без сверки плана и технологии.
            bases = ", ".join(f"{s.project}.{s.base}" for s in selected[0].sides if s.base)
            raise ProjectConfigError(
                f"База {base} — не сторона обмена «{exchange}» (стороны с базой: {bases or 'нет'})"
            )
        names = (
            [plan]
            if plan is not None
            else list(dict.fromkeys(side.plan for _, side in profile_sides)) or None
        )
        run = self._live_run("live_base_check")

        def read() -> dict:
            raw = execute(access, base_check_code(names))
            connection = (
                _connection_parts(_required(raw, "connection", str))
                if "connection_error" not in raw
                else {}
            )
            result = {
                "connection": connection,
                "configuration": raw.get("configuration", {}),
                "platform": raw.get("platform"),
                "user": raw.get("user"),
                "universal_xml": raw.get("universal_xml"),
                "bsp": raw.get("bsp"),
                "extensions": raw.get("extensions", []),
                "plans": [],
            }
            for section in (
                "connection",
                "configuration",
                "platform",
                "user",
                "universal_xml",
                "bsp",
                "extensions",
                "plans",
            ):
                if f"{section}_error" in raw:
                    result[f"{section}_error"] = _required(raw, f"{section}_error", str)
            if "user_error" not in raw:
                user = _required(result, "user", dict)
                _required(user, "name", str)
                _required(user, "full_name", str)
            if "universal_xml_error" not in raw:
                _required(result, "universal_xml", bool)
            configuration = _required(result, "configuration", dict)
            if "configuration_error" not in raw:
                for field in ("name", "synonym", "version"):
                    _required(configuration, field, str)
            extensions = _required(result, "extensions", list)
            for ext in extensions:
                if not isinstance(ext, dict):
                    raise LiveParseError("неверное расширение")
                _required(ext, "name", str)
                _required(ext, "version", str)
                _required(ext, "active", bool)
            for entry in raw.get("plans", []):
                if not isinstance(entry, dict):
                    raise LiveParseError("неверный план")
                for field, kind in (("name", str), ("exists", bool), ("bsp", bool)):
                    _required(entry, field, kind)
                row = {k: entry[k] for k in ("name", "exists", "bsp")}
                # КД 2 — подключённый план не XDTO и не РИБ; РИБ по правилам КД 2 не работает.
                # Признак РИБ не прочитан — технология не утверждается.
                if not entry["bsp"] or "xdto" not in entry:
                    technology = None
                elif entry["xdto"] is True:
                    technology = "ed"
                elif entry.get("rib") is True:
                    technology = "rib"
                elif entry.get("rib") is False:
                    technology = "kd2"
                else:
                    technology = None
                row["technology"] = technology
                row.update(
                    {
                        key: entry[key]
                        for key in ("versions", "format", "error", "rib_error")
                        if key in entry
                    }
                )
                result["plans"].append(row)
            return result

        data = run.stage("БАЗА", read)
        if data is None:
            return run.result(base=base)
        checks = [_base_identity(configured.connection, data["connection"])]
        if names is not None:
            for name in names:
                row = next((p for p in data["plans"] if p["name"] == name), {})
                ok = row.get("exists") and row.get("bsp") and not row.get("error")
                checks.append(
                    {
                        "check": "base.plan",
                        "status": "ok" if ok else "error",
                        "message": (
                            f"{name}: план есть и подключён к БСП"
                            if ok
                            else f"{name}: план отсутствует, не подключён к БСП или не прочитан; "
                            f"{row.get('error', data.get('plans_error', ''))}"
                        ),
                    }
                )
        # Без явных планов и профиля (все планы БСП) сверять технологию не с чем.
        for row in data["plans"] if names is not None else []:
            expected = {
                item.technology
                for item, side in profile_sides
                if side.plan == row["name"] and item.technology is not None
            }
            checks.append(
                {
                    "check": "base.technology",
                    "status": (
                        "skipped"
                        if not expected
                        else "ok"
                        if expected == {row["technology"]}
                        else "error"
                    ),
                    "message": (
                        f"{row['name']}: профиль "
                        f"{', '.join(sorted(expected)) or 'без технологии'}; "
                        f"база {row['technology']}"
                    ),
                }
            )
        structure_id = project_structure_id(project, configured.configuration)
        steps = []
        if structure_id not in self.store.ids():
            checks.append(
                {
                    "check": "base.structure",
                    "status": "warning",
                    "message": f"Структура {structure_id} не загружена",
                }
            )
            steps.append(
                {
                    "tool": "structure_load_project",
                    "arguments": {"project": project, "configuration": configured.configuration},
                }
            )
        else:
            meta = self.store.meta(structure_id)
            cached = {str(name).casefold() for name in _extension_list(meta.get("extensions"))}
            # Структура собирается из активных расширений; отключённое в базе не сравнивается.
            active = {ext["name"].casefold() for ext in data["extensions"] if ext["active"]}
            config = data["configuration"]
            fresh = (
                meta.get("config_name", "").casefold() == config.get("name", "").casefold()
                and meta.get("config_version") == config.get("version")
                and cached == active
                and not data.get("configuration_error")
                and not data.get("extensions_error")
            )
            message = (
                f"Структура {structure_id}: {meta.get('config_name')}, "
                f"версия {meta.get('config_version')}; "
                f"база: {config.get('name')}, версия {config.get('version')}; "
                f"расширения только в структуре: {sorted(cached - active)}, "
                f"только в базе: {sorted(active - cached)}"
            )
            checks.append(
                {
                    "check": "base.structure",
                    "status": "ok" if fresh else "warning",
                    "message": message
                    if fresh
                    else "Структура устарела или не сверена: " + message,
                }
            )
        return run.result(
            base=base,
            **data,
            checks=checks,
            problems=sum(c["status"] == "error" for c in checks),
            warnings=sum(c["status"] == "warning" for c in checks),
            **({"steps": steps} if steps else {}),
        )

    def live_status(self, base: str, plan: str | None = None) -> dict[str, Any]:
        access = self._live_access(base)
        run = self._live_run("live_status")
        result: list[dict] = []
        names = (
            [plan]
            if plan
            else run.stage("ПЛАНЫ", lambda: execute(access, plans.bsp_plans_code())["plans"])
        )
        if names is not None:
            if not isinstance(names, list) or any(not isinstance(n, str) for n in names):
                raise LiveParseError("неверный список планов")
            for name in names:
                state = run.stage(
                    name,
                    lambda n=name: {
                        "plan": n,
                        **self._live_nodes(access, n),
                        "rules": self._live_rules_state(access, n),
                    },
                )
                if state is not None:
                    result.append(state)
        return run.result(base=base, plans=result)

    def _live_dump(
        self,
        access: LiveAccess,
        plan: str,
        run: LiveRun,
        folder: str = "rules",
        state: dict | None = None,
    ) -> dict:
        # Сначала полностью сверяем все части. До этого ни одного XML результата нет.
        state = state if state is not None else self._live_rules_state(access, plan)
        data = {
            kind: download_rule(access, plan, kind, info, self.live_chunk_size)
            for kind, info in state.items()
            if info["present"] and info.get("binary")
        }
        out = run.path / folder
        out.mkdir()
        saved = []
        try:
            for kind, content in data.items():
                path = out / rules.FILES[kind]
                path.write_bytes(content)
                saved.append(
                    {
                        "kind": kind,
                        "path": self._host(path),
                        "size": len(content),
                        "hash": state[kind]["hash"],
                    }
                )
            archive = out / "rules.zip"
            with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as package:
                for kind, content in data.items():
                    package.writestr(rules.FILES[kind], content)
        except Exception:
            for path in out.iterdir():
                path.unlink()
            out.rmdir()
            raise
        return {
            "files": saved,
            "archive": self._host(archive),
            "folder": self._host(out),
            "complete": len(saved) == 3,
            "records": state,
        }

    def live_rules_dump(self, base: str, plan: str, open: bool = False) -> dict[str, Any]:
        access = self._live_access(base)
        run = self._live_run("live_rules_dump")
        result = run.stage("СНЯТИЕ ПРАВИЛ", lambda: self._live_dump(access, plan, run))
        if result is not None and open:

            def open_projects() -> list[dict[str, Any]]:
                # Тот же путь, что у rules_open: проект по пути файла прогона (папка своя у
                # каждого прогона, поэтому проект всегда новый).
                with self._lock:
                    return [
                        {
                            "kind": item["kind"],
                            "project_id": self.workspace.open_rules(
                                run.path / "rules" / rules.FILES[item["kind"]]
                            ).project.id,
                        }
                        for item in result["files"]
                    ]

            result["projects"] = run.stage("ОТКРЫТИЕ ПРАВИЛ", open_projects)
        return run.result(**(result or {}))

    def _live_files(self, files: str | dict[str, str]) -> tuple[bytes, dict, bool]:
        contents: dict[str, bytes] = {}
        if isinstance(files, str):
            path = self._read_path(files)
            if path.is_dir():
                contents = {
                    kind: (path / name).read_bytes()
                    for kind, name in rules.FILES.items()
                    if (path / name).is_file()
                }
            else:
                try:
                    with zipfile.ZipFile(path) as package:
                        names = package.namelist()
                        if len(set(names)) != len(names) or set(names) - set(rules.FILES.values()):
                            raise ValueError("архив содержит лишние или повторные файлы")
                        contents = {
                            kind: package.read(name)
                            for kind, name in rules.FILES.items()
                            if name in names
                        }
                except zipfile.BadZipFile:
                    raise ValueError("нужен ZIP правил или manifest.json снимка") from None
        elif isinstance(files, dict) and not set(files) - set(rules.FILES):
            contents = {kind: self._read_path(path).read_bytes() for kind, path in files.items()}
        if not {"exchange", "correspondent"} <= set(contents):
            raise ValueError("нужны файлы exchange и correspondent; registration необязателен")
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as package:
            for kind in rules.FILES:
                if kind in contents:
                    package.writestr(zipfile.ZipInfo(rules.FILES[kind]), contents[kind])
        receipt = {
            kind: {
                "source": "Файл",
                "size": len(value),
                "hash": base64.b64encode(hashlib.sha256(value).digest()).decode("ascii"),
            }
            for kind, value in contents.items()
        }
        return output.getvalue(), receipt, "registration" in contents

    def _live_load(
        self,
        access: LiveAccess,
        plan: str,
        archive: bytes,
        full_set: bool,
        run: LiveRun,
        *,
        write: bool = False,
        accept_version_mismatch: bool = False,
    ) -> dict:
        with uploaded(access, archive, run, self.live_chunk_size) as path:
            expected = (
                {"exchange", "correspondent", "registration"}
                if full_set
                else {"exchange", "correspondent"}
            )
            try:
                report = execute(
                    access,
                    rules.load_file_code(
                        plan,
                        path,
                        "rules.zip",
                        write=write,
                        full_set=full_set,
                        accept_version_mismatch=accept_version_mismatch,
                        report_errors=True,
                    ),
                )
            except LiveCodeError as error:
                report = {
                    "loaded": False,
                    "messages": [],
                    "compatibility": None,
                    "error": str(error),
                    "rules": [
                        {"kind": kind, "loaded": False, "info": ""}
                        for kind in rules.FILES
                        if kind in expected
                    ],
                }
            loaded = _required(report, "loaded", bool)
            entries = _required(report, "rules", list)
            messages = _required(report, "messages", list)
            if (
                any(not isinstance(m, str) for m in messages)
                or any(
                    not isinstance(r, dict)
                    or not isinstance(r.get("info"), str)
                    or type(r.get("loaded")) is not bool
                    for r in entries
                )
                or {r.get("kind") for r in entries} != expected
                or len(entries) != len(expected)
            ):
                raise LiveParseError("неверный отчёт загрузки правил")
            refusal = compatibility_error(report.get("compatibility"), accept_version_mismatch)
            if report.get("error"):
                report["error"] = short_error(_required(report, "error", str))
                refusal = report["error"]
            if (
                refusal
                or not entries
                or any(not isinstance(r, dict) or r.get("loaded") is not True for r in entries)
                or not loaded
            ):
                raise LiveStageError(refusal or "правила не загружены", {"report": report})
            return report

    def live_rules_check(
        self,
        base: str,
        plan: str,
        files: str | dict[str, str],
        accept_version_mismatch: bool = False,
    ) -> dict[str, Any]:
        access = self._live_access(base)
        archive, _, full_set = self._live_files(files)
        run = self._live_run("live_rules_check")
        report = run.stage(
            "ПРОВЕРКА ПРАВИЛ",
            lambda: self._live_load(
                access,
                plan,
                archive,
                full_set,
                run,
                accept_version_mismatch=accept_version_mismatch,
            ),
        )
        return run.result(
            report=report or next((s["report"] for s in run.stages if "report" in s), None)
        )

    def _live_preview(
        self,
        tool: str,
        inputs: dict,
        state: dict,
        effects: list[dict],
        mode: str,
        expected: str | None,
    ) -> dict | None:
        if mode not in ("preview", "apply"):
            raise ValueError("mode: preview или apply")
        digest = preview_hash(tool, inputs, state)
        if not hasattr(self, "_live_previews"):
            self._live_previews: dict[tuple[str, str], dict] = {}
        key = (tool, preview_hash(tool, inputs, {}))
        if mode == "preview":
            self._live_previews[key] = {"hash": digest, "effects": effects}
            return {"mode": mode, "preview_hash": digest, "effects": effects, "state": state}
        if (
            not expected
            or expected != digest
            or self._live_previews.get(key, {}).get("hash") != expected
        ):
            raise PreviewMismatchError("входы или база изменились: повторите preview")
        del self._live_previews[key]
        return None

    def live_rules_install(
        self,
        base: str,
        plan: str,
        files: str | dict[str, str],
        mode: str = "preview",
        expected_preview_hash: str | None = None,
        accept_version_mismatch: bool = False,
    ) -> dict[str, Any]:
        with self._lock:
            access = self._live_access(base)
            restore = self._live_restore_input(files, plan)
            if (
                restore is not None
                and mode == "apply"
                and restore["schema"] != "kd2-live-register/2"
            ):
                raise ValueError("снимок /1 не содержит реквизитов; снимите новый снимок /2")
            if restore is None:
                archive, receipt, full_set = self._live_files(files)
            else:
                archive = json.dumps(restore, ensure_ascii=False, sort_keys=True).encode("utf-8")
                receipt, full_set = (
                    {
                        "snapshot_hash": hashlib.sha256(archive).hexdigest(),
                        "records": {
                            kind: {
                                "present": record["present"],
                                "resource_names": sorted(record.get("resources", {})),
                                "attribute_names": sorted(record.get("attributes", {})),
                            }
                            for kind, record in restore["records"].items()
                        },
                    },
                    True,
                )
            state = self._live_rules_state(access, plan, tolerant=True)
            inputs = {
                "base": base,
                "plan": plan,
                "files": files,
                "receipt": receipt,
                "accept_version_mismatch": accept_version_mismatch,
            }
            # Хеш привязан к дешёвым метаданным; полные значения и XML ещё не читаем.
            prior_effects = None
            if mode == "apply":
                key = ("live_rules_install", preview_hash("live_rules_install", inputs, {}))
                prior_effects = getattr(self, "_live_previews", {}).get(key, {}).get("effects")
                self._live_preview(
                    "live_rules_install", inputs, {"rules": state}, [], mode, expected_preview_hash
                )
            snapshot = self._live_snapshot(access, plan, state)
            summary = {"complete": snapshot[0]["complete"], "reasons": snapshot[0]["reasons"]}
            if restore is not None and not restore["complete"]:
                summary = {"complete": False, "reasons": summary["reasons"] + restore["reasons"]}
            effects = [
                {
                    "action": "восстановить записи регистра"
                    if restore
                    else "заменить записи регистра",
                    "current": state,
                    "new": receipt,
                    "irreversible": not summary["complete"],
                    "reason": "; ".join(summary["reasons"]),
                }
            ]
            if mode == "apply" and effects != prior_effects:
                raise PreviewMismatchError("полнота снимка изменилась: повторите preview")
            if mode != "apply":
                preview = self._live_preview(
                    "live_rules_install",
                    inputs,
                    {"rules": state},
                    effects,
                    mode,
                    expected_preview_hash,
                )
                assert preview is not None
                return preview
            run = self._live_run("live_rules_install")
            backup = run.stage("СНЯТИЕ ПРАВИЛ", lambda: self._live_save_snapshot(snapshot, run))
            if any(item.get("present") is None for item in state.values()):

                def refuse_unknown_state():
                    raise LiveStageError(
                        "состояние записи регистра не прочитано; применение запрещено"
                    )

                run.stage("СОСТОЯНИЕ РЕГИСТРА", refuse_unknown_state)
            if restore is not None:

                def restore_records():
                    with uploaded(
                        access, archive, run, self.live_chunk_size, extension="json"
                    ) as path:
                        report = execute(access, rules.restore_file_code(plan, path))
                        if report.get("restored") is not True:
                            raise LiveParseError("нет подтверждения восстановления записей")
                        actual = self._live_rules_state(access, plan)
                        for kind in rules.RECORD_KINDS:
                            expected = restore["records"][kind]
                            if actual[kind]["present"] != expected["present"] or (
                                expected["present"]
                                and actual[kind]["record_hash"] != expected["record_hash"]
                            ):
                                raise LiveStageError(
                                    "record_hash после восстановления не совпал",
                                    {
                                        "kind": kind,
                                        "expected": expected.get("record_hash"),
                                        "actual": actual[kind].get("record_hash"),
                                    },
                                )
                        return report

                report = run.stage("ВОССТАНОВЛЕНИЕ ПРАВИЛ", restore_records)
                return run.result(backup=backup, report=report)
            run.stage(
                "ПРОВЕРКА ПРАВИЛ",
                lambda: self._live_load(
                    access,
                    plan,
                    archive,
                    full_set,
                    run,
                    accept_version_mismatch=accept_version_mismatch,
                ),
                include_data=True,
            )
            report = run.stage(
                "ЗАПИСЬ ПРАВИЛ",
                lambda: self._live_load(
                    access,
                    plan,
                    archive,
                    full_set,
                    run,
                    write=True,
                    accept_version_mismatch=accept_version_mismatch,
                ),
            )
            return run.result(
                backup=backup,
                report=report or next((s["report"] for s in run.stages if "report" in s), None),
            )

    def _live_pair(self, source: str, target: str) -> tuple[LiveAccess, LiveAccess]:
        if source == target:
            raise LiveBaseRefusedError("источник и приёмник должны быть разными базами")
        catalog = self._catalog()
        local = LocalSettings(project_dirs=self.settings.project_dirs)
        sb, sa = resolve_base(catalog, local, source)
        tb, ta = resolve_base(catalog, local, target)
        if sb.connection == tb.connection or sa.url.split("?", 1)[0] == ta.url.split("?", 1)[0]:
            raise LiveBaseRefusedError("источник и приёмник указывают на одну базу")
        return self.live_access_factory(sa), self.live_access_factory(ta)

    @staticmethod
    def _live_codes(states: list[dict], source_code: str, target_code: str) -> tuple[str, str]:
        if not source_code or not target_code or source_code == target_code:
            raise ValueError("нужны разные непустые коды узлов")
        actual = [
            next(n["code"] for n in state["nodes"] if n["this"]) or default
            for state, default in zip(states, (source_code, target_code), strict=True)
        ]
        if actual[0] == actual[1]:
            raise ValueError("коды этих узлов совпадают: нужны разные")
        return actual[0], actual[1]

    def live_nodes_setup(
        self,
        source: str,
        target: str,
        plan: str,
        source_code: str = "KD2S",
        target_code: str = "KD2T",
        mode: str = "preview",
        expected_preview_hash: str | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            sa, ta = self._live_pair(source, target)
            states = [self._live_nodes(a, plan) for a in (sa, ta)]
            sc, tc = self._live_codes(states, source_code, target_code)
            actions = []
            for state, other in zip(states, (tc, sc), strict=True):
                own = next(n for n in state["nodes"] if n["this"])
                peer = next(
                    (n for n in state["nodes"] if n["code"] == other and not n["this"]), None
                )
                actions.append(
                    (not state["enabled"] or not own["code"], peer is None or not peer["ready"])
                )
            effects = [
                {
                    "action": "настроить узлы" if any(any(a) for a in actions) else "изменений нет",
                    "irreversible": False,
                    "actions": actions,
                }
            ]
            preview = self._live_preview(
                "live_nodes_setup",
                {
                    "source": source,
                    "target": target,
                    "plan": plan,
                    "source_code": source_code,
                    "target_code": target_code,
                },
                {"source": states[0], "target": states[1]},
                effects,
                mode,
                expected_preview_hash,
            )
            if preview is not None:
                return preview
            run = self._live_run("live_nodes_setup")
            for label, access, code, other, action in zip(
                ("ИСТОЧНИК", "ПРИЕМНИК"), (sa, ta), (sc, tc), (tc, sc), actions, strict=True
            ):

                def setup(a=access, c=code, o=other, steps=action):
                    if steps[0]:
                        execute(a, exchange.this_node_code(plan, c))
                    if steps[1]:
                        identifier = execute(a, exchange.correspondent_node(plan, o)).get("id")
                        if identifier != c:
                            raise LiveStageError(
                                "идентификатор не совпал с кодом; нужны коды длиннее 2 символов"
                            )

                run.stage(label, setup)
            return run.result(changed=any(any(a) for a in actions))

    def live_exchange_run(
        self,
        source: str,
        target: str,
        plan: str,
        object: str,
        ref: str,
        query: str | None = None,
        expect_rows: int | None = None,
        source_rules: str | dict[str, str] | None = None,
        target_rules: str | dict[str, str] | None = None,
        mode: str = "preview",
        expected_preview_hash: str | None = None,
        accept_version_mismatch: bool = False,
    ) -> dict[str, Any]:
        if expect_rows is not None and not query:
            raise ValueError("expect_rows требует query")
        with self._lock:
            sa, ta = self._live_pair(source, target)
            states = [self._live_nodes(a, plan) for a in (sa, ta)]
            included = execute(sa, exchange.plan_content_check(plan, object))
            _required(included, "included", bool)
            archives = [
                self._live_files(f) if f is not None else None for f in (source_rules, target_rules)
            ]
            current_rules = [
                self._live_rules_state(a, plan) if f is not None else None
                for a, f in zip((sa, ta), archives, strict=True)
            ]
            inputs = {
                "source": source,
                "target": target,
                "plan": plan,
                "object": object,
                "ref": ref,
                "query": query,
                "expect_rows": expect_rows,
                "accept_version_mismatch": accept_version_mismatch,
                "source_rules": source_rules,
                "target_rules": target_rules,
                "receipts": [a[1] if a else None for a in archives],
            }
            state = {
                "source": states[0],
                "target": states[1],
                "composition": included,
                "rules": current_rules,
            }
            effects = [
                {"action": action, "irreversible": True}
                for action in (
                    "снять все регистрации узла корреспондента в источнике",
                    "увеличить номера сообщений",
                    "записать данные приёмника; отката обмена нет",
                )
            ]
            target_code = next(n["code"] for n in states[1]["nodes"] if n["this"])
            peer = next(
                (n for n in states[0]["nodes"] if n["code"] == target_code and not n["this"]), None
            )
            registrations = peer["registration_tables"] if peer else {}
            count = sum(registrations.values())
            details = ", ".join(
                f"{table}: {number}" for table, number in sorted(registrations.items())
            )
            effects[0] = {
                "action": f"снять регистрации узла корреспондента в источнике: {count}"
                + (f" ({details})" if details else ""),
                "irreversible": True,
                "registrations": registrations,
            }
            if any(archives):
                effects.append(
                    {"action": "заменить переданные правила в регистре", "irreversible": True}
                )
            preview = self._live_preview(
                "live_exchange_run", inputs, state, effects, mode, expected_preview_hash
            )
            if preview is not None:
                return preview
            run = self._live_run("live_exchange_run")
            codes: list[str] = []
            for label, index in (("УЗЕЛ ИСТОЧНИКА", 0), ("УЗЕЛ ПРИЕМНИКА", 1)):

                def node(i=index):
                    own = next(n for n in states[i]["nodes"] if n["this"])
                    peer = next(n for n in states[1 - i]["nodes"] if n["this"])
                    if (
                        not states[i]["enabled"]
                        or not own["code"]
                        or own["code"] == peer["code"]
                        or not any(
                            n["code"] == peer["code"] and not n["this"] and n["ready"]
                            for n in states[i]["nodes"]
                        )
                    ):
                        raise LiveStageError("нет согласованных узлов: выполните live_nodes_setup")
                    codes.append(own["code"])

                run.stage(label, node)

            def composition():
                if not included["included"]:
                    raise LiveStageError("объект не входит в состав плана: базы не менялись")

            run.stage("СОСТАВ", composition)
            for label, access, archive in zip(
                ("ПРАВИЛА ИСТОЧНИКА", "ПРАВИЛА ПРИЕМНИКА"), (sa, ta), archives, strict=True
            ):
                if archive is not None:

                    def install(a=access, f=archive):
                        self._live_load(
                            a,
                            plan,
                            f[0],
                            f[2],
                            run,
                            accept_version_mismatch=accept_version_mismatch,
                        )
                        return self._live_load(
                            a,
                            plan,
                            f[0],
                            f[2],
                            run,
                            write=True,
                            accept_version_mismatch=accept_version_mismatch,
                        )

                    run.stage(label, install, include_data=True)
            run.stage(
                "РЕГИСТРАЦИЯ",
                lambda: execute(sa, exchange.register_object(plan, codes[1], object, ref)),
            )
            message_file: str | None = None

            def export():
                nonlocal message_file
                encoded = execute(sa, exchange.export_registered(plan, codes[1]))
                data = base64.b64decode(_required(encoded, "message", str), validate=True)
                path = run.path / "message.xml"
                path.write_bytes(data)
                message_file = self._host(path)
                return data

            message = run.stage("ВЫГРУЗКА", export)

            def load():
                with uploaded(ta, message, run, self.live_chunk_size, extension="xml") as path:
                    report = execute(ta, exchange.import_file(plan, codes[0], path))
                    if report.get("error"):
                        report["error"] = short_error(_required(report, "error", str))
                        raise LiveStageError(report["error"], {"data": report})
                    return report

            run.stage("ЗАГРУЗКА", load, include_data=True)
            found = None
            if query is not None:

                def check():
                    report = execute(ta, exchange.query_rows(query, limit=20))
                    count = _required(report, "count", int)
                    _required(report, "rows", list)
                    if expect_rows is not None and count != expect_rows:
                        raise LiveStageError(
                            "число строк не совпало",
                            {"expected": expect_rows, "actual": count, "rows": report["rows"][:20]},
                        )
                    return report

                found = run.stage("ЗАПРОС", check)
            return run.result(message_path=message_file, query=found)

    def live_registration_check(
        self,
        base: str,
        object: str,
        ref: str | None = None,
        number: str | None = None,
        date: str | None = None,
        code: str | None = None,
        plan: str | None = None,
        changes: dict[str, str | int | float | bool | dict[str, str]] | None = None,
        write: bool = True,
    ) -> dict[str, Any]:
        """Регистрация сейчас, получатели по правилам и запись в отменяемой транзакции."""
        with self._lock:
            checked = validate_registration_args(
                object, ref, plan, changes, write, number=number, date=date, code=code
            )
            access = self._live_access(base)
            run = self._live_run("live_registration_check")
            parsed: dict[str, Any] = {}
            lookup: dict[str, Any] | None = None
            resolved = checked.ref
            if checked.number is not None or checked.code is not None:
                by = "number" if checked.number is not None else "code"
                field = "Номер" if checked.number is not None else "Код"
                value = checked.number if checked.number is not None else checked.code
                assert value is not None
                sought = _registration_lookup(by, value, checked.date)

                def find() -> dict[str, Any]:
                    payload = execute(
                        access, lookup_code(checked.object, field, value, checked.date)
                    )
                    (run.path / "lookup.json").write_text(
                        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                        encoding="utf-8",
                        newline="\n",
                    )
                    candidates, more = parse_lookup(payload)
                    if not candidates:
                        raise LiveStageError("объект не найден", {"lookup": sought})
                    if len(candidates) > 1 or more:
                        suffix = "+" if more else ""
                        advice = (
                            "уточните номер датой или передайте ref"
                            if by == "number"
                            else "передайте ref из candidates"
                        )
                        raise LiveStageError(
                            f"найдено объектов: {len(candidates)}{suffix}; {advice}",
                            {"candidates": candidates, "more": more},
                        )
                    return candidates[0]

                found = run.stage("ПОИСК", find)
                if not isinstance(found, dict):
                    return run.result()
                resolved = found["ref"]
                lookup = _registration_lookup(by, value, checked.date, resolved)
                # Найденное значение — как в базе: сравнение строк в запросе без учёта регистра.
                lookup["match"] = {name: found[name] for name in ("value", "date", "presentation")}
            if resolved is None:
                raise ValueError("нужна ссылка, номер или код")
            generated = registration_code(checked.object, resolved, plan, changes, write)

            def read() -> dict[str, Any]:
                payload = execute(access, generated)
                (run.path / "registration.json").write_text(
                    json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                    newline="\n",
                )
                report = parse_registration(payload, wrote=write)
                parsed["value"] = report
                return summarize_registration(report)

            summary = run.stage("РЕГИСТРАЦИЯ", read)
            if summary is None:
                return run.result()
            if write:
                report = parsed["value"]
                after = report["after"]
                assert after is not None

                def verify() -> bool:
                    if not same_registration(report["before"], after):
                        raise LiveStageError(
                            "регистрация после отката не совпала",
                            {"before": report["before"], "after": after},
                        )
                    return True

                if run.stage("ОТКАТ", verify) is None:
                    return run.result()
            if lookup is None:
                return run.result(**summary)
            return run.result(**summary, lookup=lookup)
