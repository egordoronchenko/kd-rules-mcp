"""Логика инструментов MCP без транспорта (спецификация `mcp-service`).

Каждый публичный метод `Kd2Service` — один инструмент: принимает простые значения, возвращает
словарь, пригодный для JSON, и бросает `Kd2Error` при отказе.
Ответы компактные: списки — постранично (`offset`, `limit`, `has_more`), XML правил целиком
не возвращается, тексты обработчиков обрезаются.

Пути. Агент передаёт пути так, как видит их на своей машине (`D:\\Repos\\bp\\…`); в контейнере
исходники смонтированы в другое место. `PathMap` переводит путь агента в локальный и обратно
(переменная `KD2_PATH_MAP`: `D:\\Repos\\bp=/projects/bp;…`). Пишется только в рабочую папку.
"""

import os
import sqlite3
import threading
from collections.abc import Callable, Generator, Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

from kd2_rules_mcp.authoring.candidates import (
    Candidate,
    Confidence,
    object_candidates,
    property_candidates,
    value_candidates,
)
from kd2_rules_mcp.authoring.correspondent import mirror_rules
from kd2_rules_mcp.authoring.edits import (
    EditResult,
    create_pko_with_properties,
    create_rule,
    delete_rule,
    find_rule,
    update_rule,
)
from kd2_rules_mcp.authoring.pack import collect, pack_rules
from kd2_rules_mcp.authoring.registration import (
    ObjectFilter,
    PlanFilter,
    RegistrationObject,
    build_registration_rules,
)
from kd2_rules_mcp.authoring.workspace import RulesProject, RulesWorkspace, normalize_relative
from kd2_rules_mcp.errors import (
    Kd2Error,
    ObjectNotFoundError,
    StructureNotFoundError,
    WorkspacePathError,
)
from kd2_rules_mcp.kd2.model import ExchangeRules, Node, RegistrationRules, RulesDocument
from kd2_rules_mcp.projects import (
    Catalog,
    load_catalog,
    load_local,
    parse_project_dirs,
    project_rules_dirs,
    resolve,
)
from kd2_rules_mcp.structures.queries import (
    MAX_LIMIT,
    NotFound,
    Page,
    compare_structures,
    describe_object,
    exchange_plan_content,
    list_objects,
    object_values,
)
from kd2_rules_mcp.structures.store import LoadResult, StructureStore
from kd2_rules_mcp.validation.address import rule_address, side_name, walk_pks
from kd2_rules_mcp.validation.algorithms import check_algorithm_refs
from kd2_rules_mcp.validation.format import check_format
from kd2_rules_mcp.validation.handlers import HandlerExport, export_handlers, locate
from kd2_rules_mcp.validation.registration import check_registration
from kd2_rules_mcp.validation.report import Level, ValidationReport
from kd2_rules_mcp.validation.search import check_search_params
from kd2_rules_mcp.validation.structure import check_structures

# Длина текста обработчика или поля в ответе; полный код — через handlers_export.
TEXT_LIMIT = 2000

# Разделы правил обмена: имя раздела в инструментах → тег.
EXCHANGE_SECTIONS = {
    "pko": "ПравилаКонвертацииОбъектов",
    "pvd": "ПравилаВыгрузкиДанных",
    "pod": "ПравилаОчисткиДанных",
    "algorithms": "Алгоритмы",
    "queries": "Запросы",
    "parameters": "Параметры",
}
REGISTRATION_SECTION = "registration"
# Поля правила в строке списка rules_list.
_ROW_FIELDS = (
    "Наименование",
    "Источник",
    "Приемник",
    "ОбъектВыборки",
    "КодПравилаКонвертации",
    "ОбъектМетаданныхИмя",
)


@dataclass(frozen=True, slots=True)
class PathMap:
    """Соответствие префиксов путей агента и локальных путей сервера."""

    pairs: tuple[tuple[str, str], ...] = ()

    @classmethod
    def parse(cls, text: str) -> "PathMap":
        """`путь_агента=локальный;…`; пустая строка — пути не переводятся."""
        pairs: list[tuple[str, str]] = []
        for chunk in text.split(";"):
            if not chunk.strip():
                continue
            host, sep, local = chunk.partition("=")
            if not sep or not host.strip() or not local.strip():
                raise Kd2Error(f"Неверный элемент KD2_PATH_MAP: «{chunk}»")
            pairs.append((host.strip(), local.strip()))
        # Длинный префикс раньше: рабочая папка внутри смонтированного каталога проектов.
        pairs.sort(key=lambda pair: len(_norm(pair[0])), reverse=True)
        return cls(tuple(pairs))

    def to_local(self, path: str) -> Path:
        """Путь агента → локальный путь сервера."""
        for host, local in self.pairs:
            rest = _strip_prefix(path, host)
            if rest is not None:
                return Path(local, *rest)
        return Path(path)

    def to_host(self, path: Path) -> str:
        """Локальный путь сервера → путь агента (для ответов)."""
        text = str(path)
        for host, local in sorted(self.pairs, key=lambda pair: len(pair[1]), reverse=True):
            rest = _strip_prefix(text, local)
            if rest is not None:
                separator = "\\" if "\\" in host or ":" in host else "/"
                return separator.join([host.rstrip("\\/"), *rest])
        return text


def _norm(path: str) -> str:
    return path.replace("\\", "/").rstrip("/").casefold()


def _strip_prefix(path: str, prefix: str) -> list[str] | None:
    """Части пути после префикса или `None`, если путь не под ним (регистр не важен)."""
    norm_path, norm_prefix = _norm(path), _norm(prefix)
    if norm_path != norm_prefix and not norm_path.startswith(norm_prefix + "/"):
        return None
    tail = path.replace("\\", "/").rstrip("/")[len(norm_prefix) :]
    return [part for part in tail.split("/") if part]


@dataclass(slots=True)
class Settings:
    """Настройки сервера из окружения."""

    cache_dir: Path = Path("cache")
    workspace: Path = Path("workspace")
    path_map: PathMap = field(default_factory=PathMap)
    host: str = "127.0.0.1"
    port: int = 8060
    # Общий файл проектов и папки проектов на этой машине (или в контейнере).
    projects_file: Path = Path("projects.yaml")
    project_dirs: dict[str, Path] = field(default_factory=dict)
    # Папки живых правил проектов (`rules_dir`), доступные на запись; пусто — из projects.yaml.
    rules_dirs: dict[str, Path] = field(default_factory=dict)

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "Settings":
        """`KD2_CACHE_DIR`, `KD2_WORKSPACE`, `KD2_PATH_MAP`, `KD2_HOST`, `KD2_PORT`,
        `KD2_PROJECTS_FILE`, `KD2_PROJECT_DIRS`, `KD2_RULES_DIRS`.

        Без `KD2_PROJECT_DIRS` папки проектов берутся из `projects.local.yaml` рядом с
        `projects.yaml` (локальный запуск без Docker); без `KD2_RULES_DIRS` папки живых правил —
        `rules_dir` проектов от этих папок.
        """
        source = os.environ if env is None else env
        projects_file = Path(source.get("KD2_PROJECTS_FILE", "projects.yaml"))
        dirs_text = source.get("KD2_PROJECT_DIRS", "")
        if dirs_text:
            dirs = parse_project_dirs(dirs_text)
        else:
            local = projects_file.with_name("projects.local.yaml")
            dirs = load_local(local).project_dirs if local.is_file() else {}
        return cls(
            cache_dir=Path(source.get("KD2_CACHE_DIR", "cache")),
            workspace=Path(source.get("KD2_WORKSPACE", "workspace")),
            path_map=PathMap.parse(source.get("KD2_PATH_MAP", "")),
            host=source.get("KD2_HOST", "127.0.0.1"),
            port=int(source.get("KD2_PORT", "8060")),
            projects_file=projects_file,
            project_dirs=dirs,
            rules_dirs=parse_project_dirs(source.get("KD2_RULES_DIRS", ""), "KD2_RULES_DIRS"),
        )


class Kd2Service:
    """Состояние сервера: кэш структур, рабочая папка с открытыми проектами правил."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.store = StructureStore(settings.cache_dir)
        self.workspace = RulesWorkspace(settings.workspace)
        self._exports: dict[str, HandlerExport] = {}
        # Проекты правил меняются на месте — вызовы, которые их трогают, идут по одному.
        self._lock = threading.RLock()

    # --- Структуры ---------------------------------------------------------------------------

    def structure_list(self) -> dict[str, Any]:
        items = []
        for structure_id in self.store.ids():
            meta = self.store.meta(structure_id)
            items.append(
                {
                    "structure_id": structure_id,
                    "configuration": meta.get("config_name", ""),
                    "synonym": meta.get("config_synonym", ""),
                    "version": meta.get("config_version", ""),
                    "source": meta.get("source", ""),
                    "source_path": self._host_text(meta.get("source_path", "")),
                    "extensions": meta.get("extensions", "[]"),
                    "loaded_at": meta.get("loaded_at", ""),
                }
            )
        return {"structures": items}

    def project_list(self) -> dict[str, Any]:
        catalog = self._catalog()
        writable = self.rules_dirs()
        projects = []
        for project in catalog.projects.values():
            folder = self.settings.project_dirs.get(project.id)
            rules_dir = writable.get(project.id)
            row: dict[str, Any] = {
                "project": project.id,
                "name": project.name,
                "available": folder is not None and folder.is_dir(),
                "configurations": {
                    config.id: {
                        "structure_id": _structure_id(project.id, config.id),
                        "dump": config.dump,
                        "extensions": list(config.extensions),
                    }
                    for config in project.configurations.values()
                },
                "bases": {
                    base.id: {
                        "role": base.role,
                        "configuration": base.configuration,
                        **(
                            {"data_mcp": f"{project.id}-{base.data_mcp}"}
                            if base.data_mcp and base.is_sandbox
                            else {}
                        ),
                    }
                    for base in project.bases.values()
                },
                "code_mcp": [f"{project.id}-{name}" for name in project.code_mcp],
            }
            if folder is not None:
                row["folder"] = self._host(folder)
            if project.rules_dir:
                row["rules_dir"] = {
                    "path": self._host(rules_dir) if rules_dir else project.rules_dir,
                    "writable": rules_dir is not None and rules_dir.is_dir(),
                }
            projects.append(row)
        exchanges = [
            {"plan": item.plan, "projects": list(item.projects)} for item in catalog.exchanges
        ]
        return {
            "projects": projects,
            "exchanges": exchanges,
            "workspace": self._host(self.workspace.root),
            "shared_mcp": list(catalog.shared_mcp),
        }

    def structure_load_project(
        self,
        project_id: str,
        configuration_id: str = "full",
        structure_id: str | None = None,
        force: bool = False,
    ) -> dict[str, Any]:
        catalog = self._catalog()
        config = catalog.configuration(project_id, configuration_id)
        folder = self.settings.project_dirs.get(project_id)
        if folder is None:
            known = ", ".join(self.settings.project_dirs) or "нет"
            raise Kd2Error(
                f"Папка проекта «{project_id}» на сервере не задана (projects.local.yaml → "
                f"scripts/setup_local.py); заданы: {known}"
            )
        main = resolve(folder, config.dump)
        extensions = [resolve(folder, item) for item in config.extensions]
        for path in (main, *extensions):
            if not path.is_dir():
                raise Kd2Error(f"Нет каталога выгрузки {self._host(path)}")
        target_id = structure_id or _structure_id(project_id, configuration_id)
        result = self.store.load_xml(target_id, main, extensions, force=force)
        return {**self._load_view(result), "project": project_id, "configuration": config.id}

    def structure_load_md83exp(
        self, structure_id: str, path: str, force: bool = False
    ) -> dict[str, Any]:
        source = self._read_path(path)
        return self._load_view(self.store.load_md83exp(structure_id, source, force=force))

    def structure_load_xml(
        self,
        structure_id: str,
        configuration_path: str,
        extension_paths: Sequence[str] = (),
        force: bool = False,
    ) -> dict[str, Any]:
        main = self._read_path(configuration_path)
        extensions = [self._read_path(item) for item in extension_paths]
        return self._load_view(self.store.load_xml(structure_id, main, extensions, force=force))

    def structure_objects(
        self, structure_id: str, kind: str | None, text: str | None, offset: int, limit: int
    ) -> dict[str, Any]:
        with self._structure(structure_id) as conn:
            return _page(list_objects(conn, kind or None, text or None, offset, limit))

    def structure_object(
        self, structure_id: str, name: str, offset: int, limit: int
    ) -> dict[str, Any]:
        with self._structure(structure_id) as conn:
            found = _found(describe_object(conn, name, offset, limit))
            return {**found, "properties": _page(found["properties"])}

    def structure_values(
        self, structure_id: str, name: str, offset: int, limit: int
    ) -> dict[str, Any]:
        with self._structure(structure_id) as conn:
            return _page(_found(object_values(conn, name, offset, limit)))

    def structure_plan_content(
        self, structure_id: str, exchange_plan: str, offset: int, limit: int
    ) -> dict[str, Any]:
        with self._structure(structure_id) as conn:
            return _page(_found(exchange_plan_content(conn, exchange_plan, offset, limit)))

    def structure_compare(self, old_id: str, new_id: str, limit: int) -> dict[str, Any]:
        with self._structure(old_id) as old, self._structure(new_id) as new:
            diff = compare_structures(old, new)
        limit = _limit(limit)
        lists = {
            "added_objects": diff.added_objects,
            "removed_objects": diff.removed_objects,
            "added_properties": diff.added_properties,
            "removed_properties": diff.removed_properties,
            "changed_properties": diff.changed_properties,
            "added_values": diff.added_values,
            "removed_values": diff.removed_values,
        }
        return {
            "counts": diff.counts(),
            "limit": limit,
            **{key: values[:limit] for key, values in lists.items()},
        }

    # --- Кандидаты сопоставления -------------------------------------------------------------

    def match_objects(
        self,
        source_structure: str,
        target_structure: str,
        kind: str | None,
        confidence: str | None,
        offset: int,
        limit: int,
        text: str | None = None,
    ) -> dict[str, Any]:
        with (
            self._structure(source_structure) as source,
            self._structure(target_structure) as target,
        ):
            found = object_candidates(source, target, kind or None)
        if text:
            needle = text.casefold()
            found = [item for item in found if _mentions(item, needle)]
        rows = [_object_row(item) for item in found]
        return _slice(_filter_confidence(rows, confidence), offset, limit)

    def match_properties(
        self,
        source_structure: str,
        target_structure: str,
        source_object: str,
        target_object: str,
        confidence: str | None,
        offset: int,
        limit: int,
    ) -> dict[str, Any]:
        with (
            self._structure(source_structure) as source,
            self._structure(target_structure) as target,
        ):
            found = _found(property_candidates(source, target, source_object, target_object))
        rows = [_candidate_row(item, path) for path, item in _flatten(found)]
        return _slice(_filter_confidence(rows, confidence), offset, limit)

    def match_values(
        self,
        source_structure: str,
        target_structure: str,
        source_object: str,
        target_object: str,
        offset: int,
        limit: int,
    ) -> dict[str, Any]:
        with (
            self._structure(source_structure) as source,
            self._structure(target_structure) as target,
        ):
            found = _found(value_candidates(source, target, source_object, target_object))
        return _slice([_candidate_row(item, "") for item in found], offset, limit)

    # --- Проекты правил ------------------------------------------------------------------

    def rules_open(self, path: str) -> dict[str, Any]:
        with self._lock:
            project = self.workspace.open_rules(self._read_path(path))
            return self._project_view(project)

    def rules_create(self, source_structure: str, target_structure: str) -> dict[str, Any]:
        with self._lock:
            project = self.workspace.create_exchange(self.store, source_structure, target_structure)
            return self._project_view(project)

    def rules_projects(self) -> dict[str, Any]:
        with self._lock:
            return {"projects": [self._project_view(item) for item in self._projects()]}

    def rules_overview(self, project_id: str) -> dict[str, Any]:
        with self._lock:
            return self._project_view(self.workspace.get(project_id))

    def rules_list(
        self, project_id: str, section: str, text: str | None, offset: int, limit: int
    ) -> dict[str, Any]:
        with self._lock:
            document = self.workspace.get(project_id).document
            rows = [_rule_row(node) for node in _section_rules(document, section)]
        if text:
            needle = text.casefold()
            rows = [row for row in rows if needle in " ".join(map(str, row.values())).casefold()]
        return _slice(rows, offset, limit)

    def rules_get(
        self, project_id: str, kind: str, key: str, owner: str, limit: int
    ) -> dict[str, Any]:
        with self._lock:
            node = find_rule(self._exchange(project_id), kind, key, owner)
            return _node_view(node, _limit(limit))

    def rules_save(self, project_id: str, path: str, overwrite: bool) -> dict[str, Any]:
        with self._lock:
            project = self.workspace.get(project_id)
            saved = self.workspace.save(
                project_id,
                self._writable(path),
                overwrite=overwrite,
                allowed=list(self.rules_dirs().values()),
            )
            report = check_format(project.document)
            return {
                "project_id": project_id,
                "path": self._host(saved),
                "size_bytes": saved.stat().st_size,
                "counts": _counts(project.document),
                "format_check": _report_summary(report),
            }

    def rules_pack(
        self,
        folder: str,
        exchange_rules: str,
        correspondent_rules: str,
        registration_rules: str,
        path: str,
        overwrite: bool,
    ) -> dict[str, Any]:
        with self._lock:
            source_dir = self._read_path(folder) if folder else None
            explicit = {
                role: self._read_path(value) if value else None
                for role, value in (
                    ("exchange", exchange_rules),
                    ("correspondent", correspondent_rules),
                    ("registration", registration_rules),
                )
            }
            files = collect(source_dir, explicit)
            default = source_dir.name if source_dir is not None else files["exchange"].stem
            result = pack_rules(
                files, self._writable(path or f"{default}.zip"), overwrite=overwrite
            )
            return {
                "path": self._host(result.path),
                "size_bytes": result.path.stat().st_size,
                "load_with": result.form,
                "files": [
                    {
                        "file": item.name,
                        "path": self._host(item.source),
                        "size_bytes": item.size_bytes,
                        "rules": item.summary,
                    }
                    for item in result.files
                ],
                "warnings": result.warnings,
            }

    # --- Правки ----------------------------------------------------------------------------

    def rule_create(
        self, project_id: str, kind: str, key: str, group: str = "", **options: Any
    ) -> dict[str, Any]:
        return self._edit(create_rule, project_id, kind, key, group=group, **options)

    def rule_update(self, project_id: str, kind: str, key: str, **options: Any) -> dict[str, Any]:
        return self._edit(update_rule, project_id, kind, key, **options)

    def rule_delete(
        self,
        project_id: str,
        kind: str,
        key: str,
        owner: str,
        source_structure: str | None,
        target_structure: str | None,
    ) -> dict[str, Any]:
        with self._lock, self._sides(source_structure, target_structure) as (source, target):
            rules = self._exchange(project_id)
            result = delete_rule(rules, kind, key, owner=owner, source=source, target=target)
            return _edit_view(result)

    def pko_create_from_candidates(
        self,
        project_id: str,
        code: str,
        source_structure: str,
        target_structure: str,
        source_object: str,
        target_object: str,
        fields: Mapping[str, Any] | None,
        group: str = "",
    ) -> dict[str, Any]:
        with (
            self._lock,
            self._structure(source_structure) as source,
            self._structure(target_structure) as target,
        ):
            rules = self._exchange(project_id)
            result = create_pko_with_properties(
                rules, code, source, target, source_object, target_object, fields, group=group
            )
            return _edit_view(result)

    # --- Проверки -------------------------------------------------------------------------

    def rules_validate(
        self,
        project_id: str,
        source_structure: str | None,
        target_structure: str | None,
        level: str | None,
        check_prefix: str | None,
        offset: int,
        limit: int,
    ) -> dict[str, Any]:
        with self._lock, self._sides(source_structure, target_structure) as (source, target):
            document = self.workspace.get(project_id).document
            report = check_format(document)
            if isinstance(document, ExchangeRules):
                report.extend(check_structures(document, source, target))
                report.extend(check_algorithm_refs(document))
                report.extend(check_search_params(document))
            elif isinstance(document, RegistrationRules):
                report.extend(check_registration(document, source))
        issues = [issue.to_dict() for issue in report.issues]
        if level:
            issues = [issue for issue in issues if issue["level"] == Level(level).value]
        if check_prefix:
            issues = [issue for issue in issues if issue["check"].startswith(check_prefix)]
        return {
            "summary": _report_summary(report),
            "skipped": [item.to_dict() for item in report.skipped],
            "issues": _slice(issues, offset, limit),
        }

    def handlers_export(self, project_id: str, folder: str, limit: int) -> dict[str, Any]:
        with self._lock:
            rules = self._exchange(project_id)
            out_dir = self._writable(folder)
            export = export_handlers(rules, out_dir)
            self._exports[project_id] = export
        limit = _limit(limit)
        files = [
            {"file": item.name, "address": item.address, "event": item.event}
            for item in export.files
        ]
        return {
            "folder": self._host(out_dir),
            "count": len(files),
            "removed": len(export.removed),
            "files": files[:limit],
            "has_more": len(files) > limit,
        }

    def handlers_locate(self, project_id: str, file_name: str, line: int) -> dict[str, Any]:
        with self._lock:
            export = self._exports.get(project_id)
        if export is None:
            raise Kd2Error(
                f"Обработчики проекта «{project_id}» ещё не выгружались: сначала handlers_export"
            )
        found = locate(export, file_name, line)
        if found is None:
            return {"found": False, "file": file_name, "line": line}
        return {
            "found": True,
            "address": found.address,
            "event": found.event,
            "handler_line": found.line,
        }

    # --- Правила регистрации и корреспондент -------------------------------------------------

    def registration_build(
        self,
        structure_id: str,
        exchange_plan: str,
        rules_project_id: str | None,
        objects: Sequence[Mapping[str, Any]] | None,
    ) -> dict[str, Any]:
        with self._lock, self._structure(structure_id) as conn:
            exchange = self._exchange(rules_project_id) if rules_project_id else None
            specs = [_registration_object(item) for item in objects] if objects else None
            build = build_registration_rules(
                conn, exchange_plan, exchange_rules=exchange, objects=specs
            )
            project = self.workspace.add(build.document)
            return {**self._project_view(project), "warnings": build.warnings}

    def correspondent_draft(
        self, project_id: str, codes: Sequence[str], target_structure: str | None, limit: int
    ) -> dict[str, Any]:
        limit = _limit(limit)
        with self._lock, self._sides(target_structure, None) as (target, _):
            result = mirror_rules(self._exchange(project_id), codes, target)
            project = self.workspace.add(result.rules)
            handlers = [
                {
                    "address": item.address,
                    "event": item.event,
                    "note": item.note,
                    "code": _clip(item.code),
                }
                for item in result.handlers
            ]
            disabled = [
                {"address": item.address, "reason": item.reason} for item in result.disabled
            ]
            return {
                **self._project_view(project),
                "draft": result.draft,
                "missing": result.missing,
                "notes": result.notes[:limit],
                "handlers_total": len(handlers),
                "handlers": handlers[:limit],
                "disabled_total": len(disabled),
                "disabled": disabled[:limit],
            }

    # --- Вспомогательное -------------------------------------------------------------------

    def _edit(
        self,
        operation: Callable[..., EditResult],
        project_id: str,
        kind: str,
        key: str,
        *,
        fields: Mapping[str, Any] | None = None,
        owner: str = "",
        source_structure: str | None = None,
        target_structure: str | None = None,
        group: str | None = None,
    ) -> dict[str, Any]:
        with self._lock, self._sides(source_structure, target_structure) as (source, target):
            rules = self._exchange(project_id)
            # `group` передаётся только из `rule_create`: у `update_rule` такого параметра нет.
            extra: dict[str, Any] = {}
            if group is not None:
                extra["group"] = group
            result = operation(
                rules, kind, key, fields, owner=owner, source=source, target=target, **extra
            )
            return _edit_view(result)

    def _catalog(self) -> Catalog:
        return load_catalog(self.settings.projects_file)

    def rules_dirs(self) -> dict[str, Path]:
        """Папки живых правил проектов на сервере (запись разрешена): `KD2_RULES_DIRS` или
        `rules_dir` проектов от их папок; нет файла проектов — пусто."""
        if self.settings.rules_dirs:
            return self.settings.rules_dirs
        if not self.settings.projects_file.is_file():
            return {}
        return project_rules_dirs(self._catalog(), self.settings.project_dirs)

    def writable_dirs(self) -> list[str]:
        """Каталоги, куда сервер пишет, — путями агента (для ответов и ошибок)."""
        folders = [self.workspace.root, *self.rules_dirs().values()]
        return [self._host(folder) for folder in folders]

    def _exchange(self, project_id: str) -> ExchangeRules:
        document = self.workspace.get(project_id).document
        if not isinstance(document, ExchangeRules):
            raise Kd2Error(f"Проект «{project_id}» — правила регистрации, а нужны правила обмена")
        return document

    def _projects(self) -> list[RulesProject]:
        return [self.workspace.get(project_id) for project_id in self.workspace.ids()]

    @contextmanager
    def _structure(self, structure_id: str) -> Generator[sqlite3.Connection]:
        if not self.store.exists(structure_id):
            known = ", ".join(self.store.ids()) or "кэш пуст"
            raise StructureNotFoundError(
                f"Структура «{structure_id}» не загружена (загружены: {known})"
            )
        conn = self.store.open(structure_id)
        try:
            yield conn
        finally:
            conn.close()

    @contextmanager
    def _sides(
        self, source_id: str | None, target_id: str | None
    ) -> Generator[tuple[sqlite3.Connection | None, sqlite3.Connection | None]]:
        opened: list[sqlite3.Connection] = []
        try:
            source = self._open_optional(source_id, opened)
            target = self._open_optional(target_id, opened)
            yield source, target
        finally:
            for conn in opened:
                conn.close()

    def _open_optional(
        self, structure_id: str | None, opened: list[sqlite3.Connection]
    ) -> sqlite3.Connection | None:
        if not structure_id:
            return None
        with self._structure(structure_id):
            pass
        conn = self.store.open(structure_id)
        opened.append(conn)
        return conn

    def _read_path(self, path: str) -> Path:
        local = self._local(path)
        if local is None:
            raise Kd2Error(
                f"Путь «{path}» серверу не виден: папка не подключена (projects.local.yaml → "
                "scripts/setup_local.py)"
            )
        if not local.exists():
            raise Kd2Error(f"Путь «{path}» не найден (на сервере: {local})")
        return local

    def _write_path(self, path: str) -> Path:
        if not _is_absolute(path):
            return Path(path)
        local = self._local(path)
        if local is None:
            raise WorkspacePathError(
                f"Сохранение «{path}» отклонено: путь вне рабочей папки и папок правил проектов. "
                f"Разрешено: {', '.join(self.writable_dirs())}"
            )
        return local

    def _writable(self, path: str) -> Path:
        """Путь записи на сервере — в рабочей папке или `rules_dir` проекта; иначе ошибка с
        разрешёнными каталогами путями агента (пути контейнера агенту ничего не скажут)."""
        try:
            return self.workspace.resolve(self._write_path(path), list(self.rules_dirs().values()))
        except WorkspacePathError as error:
            raise WorkspacePathError(
                f"Сохранение «{path}» отклонено: путь вне рабочей папки и папок правил проектов. "
                f"Разрешено: {', '.join(self.writable_dirs())}"
            ) from error

    def _local(self, path: str) -> Path | None:
        r"""Локальный путь сервера; абсолютный путь агента, который не удалось перевести на этой ОС
        (например, `C:\…` в Linux-контейнере вне подключённых папок), — `None`."""
        if not _is_absolute(path):
            path = normalize_relative(path)
        local = self.settings.path_map.to_local(path)
        if _is_absolute(path) and not local.is_absolute():
            return None
        return local

    def _host(self, path: Path) -> str:
        return self.settings.path_map.to_host(path.resolve())

    def _host_text(self, path: str) -> str:
        return self.settings.path_map.to_host(Path(path)) if path else ""

    def _project_view(self, project: RulesProject) -> dict[str, Any]:
        document = project.document
        view: dict[str, Any] = {
            "project_id": project.id,
            "kind": "registration" if isinstance(document, RegistrationRules) else "exchange",
            "source_path": self._host(project.source_path) if project.source_path else None,
            "saved_path": self._host(project.saved_path) if project.saved_path else None,
            "counts": _counts(document),
        }
        if isinstance(document, ExchangeRules):
            view["name"] = str(document.root.values.get("Наименование", ""))
            view["source"] = document.source_name
            view["target"] = document.target_name
        elif isinstance(document, RegistrationRules):
            view["name"] = str(document.root.values.get("Наименование", ""))
            view["exchange_plan"] = document.exchange_plan
        return view

    def _load_view(self, result: LoadResult) -> dict[str, Any]:
        view: dict[str, Any] = {
            "structure_id": result.structure_id,
            "reused": result.reused,
            "counts": result.counts,
            "elapsed_s": result.elapsed_s,
            "message": result.message,
        }
        if result.unresolved:
            top = sorted(result.unresolved.items(), key=lambda item: -item[1])[:20]
            view["unresolved_total"] = len(result.unresolved)
            view["unresolved_top"] = dict(top)
        return view


# --- Представления -----------------------------------------------------------------------


def _is_absolute(path: str) -> bool:
    r"""Абсолютный путь Windows (`C:\…`, `\\сервер\…`) или POSIX (`/…`)."""
    return PureWindowsPath(path).is_absolute() or PurePosixPath(path).is_absolute()


def _structure_id(project_id: str, configuration_id: str) -> str:
    """Идентификатор структуры конфигурации проекта: `<проект>-<конфигурация>`."""
    return f"{project_id}-{configuration_id}"


def _limit(limit: int) -> int:
    if limit < 1:
        raise Kd2Error(f"Размер страницы должен быть положительным: {limit}")
    return min(limit, MAX_LIMIT)


def _slice(rows: Sequence[Any], offset: int, limit: int) -> dict[str, Any]:
    if offset < 0:
        raise Kd2Error(f"Смещение страницы не может быть отрицательным: {offset}")
    limit = _limit(limit)
    items = list(rows[offset : offset + limit])
    return {
        "items": items,
        "total": len(rows),
        "offset": offset,
        "limit": limit,
        "has_more": offset + len(items) < len(rows),
    }


def _page(page: Page) -> dict[str, Any]:
    return {
        "items": page.items,
        "total": page.total,
        "offset": page.offset,
        "limit": page.limit,
        "has_more": page.has_more,
    }


def _found[T](result: T | NotFound) -> T:
    if isinstance(result, NotFound):
        raise ObjectNotFoundError(result.message, result.suggestions)
    return result


def _clip(text: str) -> str:
    return text if len(text) <= TEXT_LIMIT else text[:TEXT_LIMIT] + " …[обрезано]"


def _candidate_row(candidate: Candidate, path: str) -> dict[str, Any]:
    def side(value: Any) -> dict[str, Any] | None:
        if value is None:
            return None
        return {
            "name": value.name,
            "kind": value.kind,
            "path": value.path,
            "synonym": value.synonym,
            "types": list(value.types[:5]) + (["…"] if len(value.types) > 5 else []),
        }

    row: dict[str, Any] = {
        "confidence": candidate.confidence.value,
        "auto": candidate.auto,
        "source": side(candidate.source),
        "target": side(candidate.target),
    }
    if path:
        row["path"] = path
    if candidate.note:
        row["note"] = candidate.note
    return row


def _object_row(candidate: Candidate) -> dict[str, Any]:
    """Кандидат ПКО компактно: `Вид.Имя` сторон и синоним — без наборов типов."""
    row: dict[str, Any] = {"confidence": candidate.confidence.value, "auto": candidate.auto}
    for key, side in (("source", candidate.source), ("target", candidate.target)):
        row[key] = f"{side.kind}.{side.name}" if side is not None else None
    main = candidate.target or candidate.source
    if main is not None and main.synonym:
        row["synonym"] = main.synonym
    if candidate.note:
        row["note"] = candidate.note
    return row


def _mentions(candidate: Candidate, needle: str) -> bool:
    """Подстрока в имени или синониме любой стороны кандидата."""
    for side in (candidate.source, candidate.target):
        if side is not None and (
            needle in side.name.casefold() or needle in side.synonym.casefold()
        ):
            return True
    return False


def _flatten(candidates: Iterable[Candidate], prefix: str = "") -> Iterator[tuple[str, Candidate]]:
    """Дерево кандидатов свойств → пары (путь ПКС `группа/свойство`, кандидат)."""
    for item in candidates:
        side = item.target or item.source
        name = side.name if side is not None else ""
        path = f"{prefix}{name}"
        yield path, item
        if item.children:
            yield from _flatten(item.children, f"{path}/")


def _filter_confidence(rows: list[dict[str, Any]], confidence: str | None) -> list[dict[str, Any]]:
    if not confidence:
        return rows
    wanted = Confidence(confidence).value
    return [row for row in rows if row["confidence"] == wanted]


def _section_node(document: RulesDocument, tag: str) -> Node | None:
    return document.root.children.get(tag)


def _section_rules(document: RulesDocument, section: str) -> list[Node]:
    if isinstance(document, RegistrationRules):
        if section != REGISTRATION_SECTION:
            raise Kd2Error(f"У правил регистрации один раздел: «{REGISTRATION_SECTION}»")
        node = _section_node(document, "ПравилаРегистрацииОбъектов")
    else:
        tag = EXCHANGE_SECTIONS.get(section)
        if tag is None:
            known = ", ".join(EXCHANGE_SECTIONS)
            raise Kd2Error(f"Неизвестный раздел «{section}»; разделы правил обмена: {known}")
        node = _section_node(document, tag)
    if node is None:
        return []
    if section == "parameters":
        return [item for item in node.items if item.kind.name == "parameter"]
    return list(node.walk())


def _rule_row(node: Node) -> dict[str, Any]:
    values = node.values
    row: dict[str, Any] = {"address": rule_address(node), "code": node.code}
    for tag in _ROW_FIELDS:
        value = values.get(tag)
        if value not in (None, ""):
            row[tag] = value
    for name in ("Отключить", "ИспользуетсяПриЗагрузке"):
        if node.attrs.get(name) is True:
            row[name] = True
    properties = node.child("Свойства")
    if properties is not None:
        row["pks_count"] = sum(1 for _ in walk_pks(properties))
    return row


def _node_view(node: Node, limit: int) -> dict[str, Any]:
    view: dict[str, Any] = {"kind": node.kind.name, "title": node.kind.title}
    if node.attrs:
        view["attrs"] = dict(node.attrs)
    fields: dict[str, Any] = {}
    for tag, value in node.values.items():
        fields[tag] = _clip(value) if isinstance(value, str) else value
    if fields:
        view["fields"] = fields
    sides = {
        tag: {"attrs": dict(child.attrs), **({"text": child.text} if child.text else {})}
        for tag, child in node.children.items()
        if child.kind.name == "pks_side"
    }
    if sides:
        view["sides"] = sides
    properties = node.child("Свойства")
    if properties is not None:
        rows = [
            {
                "path": path,
                "kind": item.kind.name,
                "source": side_name(item, "Источник"),
                "target": side_name(item, "Приемник"),
                **({"disabled": True} if item.attrs.get("Отключить") is True else {}),
                **({"search": True} if item.attrs.get("Поиск") is True else {}),
                **(
                    {"conversion": item.values["КодПравилаКонвертации"]}
                    if item.values.get("КодПравилаКонвертации")
                    else {}
                ),
            }
            for path, item in walk_pks(properties)
        ]
        view["properties"] = {"total": len(rows), "items": rows[:limit]}
        view["address"] = rule_address(node)
    values = node.child("Значения")
    if values is not None:
        rows = [
            {"source": item.values.get("Источник", ""), "target": item.values.get("Приемник", "")}
            for item in values.walk()
        ]
        view["values"] = {"total": len(rows), "items": rows[:limit]}
    other = sorted(
        tag
        for tag, child in node.children.items()
        if child.kind.name != "pks_side" and tag not in ("Свойства", "Значения")
    )
    if other:
        view["nested"] = other
    return view


def _counts(document: RulesDocument) -> dict[str, int]:
    def count(tag: str) -> int:
        node = document.root.children.get(tag)
        return sum(1 for _ in node.walk()) if node is not None else 0

    if isinstance(document, RegistrationRules):
        return {"registration_rules": count("ПравилаРегистрацииОбъектов")}
    counts = {section: count(tag) for section, tag in EXCHANGE_SECTIONS.items()}
    parameters = document.root.children.get("Параметры")
    counts["parameters"] = (
        sum(1 for item in parameters.items if item.kind.name == "parameter") if parameters else 0
    )
    return counts


def _report_summary(report: ValidationReport) -> dict[str, Any]:
    by_check: dict[str, int] = {}
    for issue in report.issues:
        by_check[issue.check] = by_check.get(issue.check, 0) + 1
    return {
        "errors": len(report.errors),
        "warnings": len(report.warnings),
        "skipped": len(report.skipped),
        "by_check": by_check,
        "text": report.summary(),
    }


def _edit_view(result: EditResult) -> dict[str, Any]:
    view: dict[str, Any] = {"address": result.address}
    for name in ("warnings", "skipped", "not_applied", "unresolved", "disabled"):
        values = getattr(result, name)
        if values:
            view[name] = values[:MAX_LIMIT]
            if len(values) > MAX_LIMIT:
                view[f"{name}_total"] = len(values)
    return view


def _registration_object(item: Mapping[str, Any]) -> RegistrationObject:
    """Объект правил регистрации из словаря инструмента (плоские отборы через «И»)."""
    name = str(item.get("metadata_name", "")).strip()
    if not name:
        raise Kd2Error("У объекта правил регистрации нет «metadata_name»")
    plan_filters = tuple(
        PlanFilter(
            plan_property=str(entry.get("plan_property", "")),
            object_property=str(entry.get("object_property", "")),
            property_type=str(entry.get("property_type", "")),
            comparison=str(entry.get("comparison", "")),
            constant=bool(entry.get("constant", False)),
        )
        for entry in item.get("plan_filters", ()) or ()
    )
    object_filters = tuple(
        ObjectFilter(
            object_property=str(entry.get("object_property", "")),
            property_type=str(entry.get("property_type", "")),
            comparison=str(entry.get("comparison", "")),
            constant_value=str(entry.get("constant_value", "")),
        )
        for entry in item.get("object_filters", ()) or ()
    )
    return RegistrationObject(
        metadata_name=name,
        code=str(item.get("code", "")),
        name=str(item.get("name", "")),
        comment=str(item.get("comment", "")),
        unload_mode=str(item.get("unload_mode", "")),
        plan_filters=plan_filters,
        object_filters=object_filters,
    )
