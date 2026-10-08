"""Перенос регистрации: preview без записи, подтверждения и защищённый комплект."""

import json
import os
import shutil
import tempfile
from collections.abc import Callable
from contextlib import suppress
from dataclasses import asdict, replace
from importlib.resources import files as resources
from pathlib import Path
from string import Template
from typing import Any

from kd_rules_mcp.authoring.ed.instruction import table
from kd_rules_mcp.authoring.ed.manifest import json_bytes, sha256
from kd_rules_mcp.authoring.ed.registration_delivery import (
    NodeValueHint,
    OwnNodeAttribute,
    deletion_mark_instruction,
    read_plan_host,
    render_registration_kit,
    retarget_instruction_details,
)
from kd_rules_mcp.authoring.ed.xml_dump import M, parse_xml
from kd_rules_mcp.authoring.registration_retarget import (
    RetargetResult,
    deletion_filter_summary,
    own_attribute_covers,
    own_attribute_remarks,
    registration_plan_notices,
    registration_rule_active,
    retarget_registration,
)
from kd_rules_mcp.authoring.workspace import _source_changed
from kd_rules_mcp.errors import (
    EdAuthoringPathError,
    RegistrationDeliveryError,
    RegistrationMissingAttributeError,
    RegistrationPlanNotFoundError,
    RegistrationRetargetError,
    RegistrationToolError,
)
from kd_rules_mcp.kd2.model import RegistrationRules
from kd_rules_mcp.kd2.rules_io import dump_rules, load_rules
from kd_rules_mcp.projects import resolve
from kd_rules_mcp.service.ed_authoring import MAX_BYTES, MAX_FILES, EdAuthoringMixin
from kd_rules_mcp.service.ed_views import validate_page
from kd_rules_mcp.service.extension_delivery import (
    delivered_structure_matches,
    delivery_options,
    input_receipt,
    prepare_user_delivery,
    registration_delivery_host,
)
from kd_rules_mcp.service.views import slice_rows
from kd_rules_mcp.structures.queries import ObjectCard, read_object_card
from kd_rules_mcp.structures.store import dump_fingerprint

_OWNER = "ownership.json"
_SCHEMA = "registration-retarget/1"


def _fields(raw: object, name: str, required: set[str], optional: set[str]) -> dict:
    """Проверяет поля до чтения файлов; неизвестные решения не игнорируются."""
    if not isinstance(raw, dict) or set(raw) - required - optional or required - set(raw):
        raise ValueError(f"{name}: нужны поля {', '.join(sorted(required))}; лишние поля запрещены")
    if any(not isinstance(value, str) for value in raw.values()):
        raise ValueError(f"{name}: значения полей должны быть строками")
    return raw


def _notices(result: RetargetResult, own: set[str]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    invalid = {(r.address, r.leaf): r for r in own_attribute_remarks(result.document, own)}
    for remark in result.remarks:
        problem = invalid.pop((remark.address, remark.leaf), None)
        added = own_attribute_covers(remark, own) and problem is None
        rows.append(
            {
                "check": "registration.missing_attribute",
                **asdict(remark),
                "message": (problem.message if problem else remark.message)
                + ("; реквизит добавит расширение." if added else "; запись комплекта запрещена."),
                "blocking": not added,
                "requires_acknowledgement": False,
            }
        )
    rows.extend(
        {
            "check": "registration.own_attribute_type",
            **asdict(r),
            "blocking": True,
            "requires_acknowledgement": False,
        }
        for r in invalid.values()
    )
    rows.extend(asdict(notice) for notice in result.notices)
    rows.extend(
        {
            "check": "registration.code_mention",
            **asdict(mention),
            "message": mention.message + ". Перенос неполон: проверьте и исправьте код вручную.",
            "blocking": False,
            "requires_acknowledgement": True,
        }
        for mention in result.mentions
    )
    for row in rows:
        row["id"] = row["check"] + ":" + sha256(json_bytes(row))[:20]
    return rows


def _manual_files(
    result: RetargetResult,
    plan: str,
    hints: list[NodeValueHint],
    notices: list[dict[str, Any]],
    source_file_hash: str | None = None,
) -> dict[str, bytes]:
    """Использует принятую инструкцию доставки, убирая шаги установки расширения."""
    text = (
        resources("kd_rules_mcp.authoring.ed")
        .joinpath("templates/registration_instruction.md")
        .read_text("utf-8")
    )
    # Заголовок и блок расширения неприменимы к комплекту только из правил и инструкции.
    saved = text.split("## 1. Сохраните текущее состояние", 1)[1].split("## 2.", 1)[0]
    settings = text.split("**Дату начала и отбор по организациям", 1)[1].split("## 4.", 1)[0]
    load = text.split("## 4. Загрузите файл правил регистрации", 1)[1].split(
        "Расширение отключайте только", 1
    )[0]
    required = (
        [n for n in notices if n["requires_acknowledgement"] or n["blocking"]]
        if result.deletion_mark_filter
        else notices
    )
    incomplete = (
        (
            "## Перенос неполон\n\n"
            + "\n".join(
                "- " + n["message"]
                for n in required
                if n["check"]
                not in ("registration.deletion_mode", "registration.deletion_missing_rule")
            )
        )
        if any(
            n["check"] not in ("registration.deletion_mode", "registration.deletion_missing_rule")
            for n in required
        )
        else ""
    )
    details = retarget_instruction_details(result)
    if details:
        incomplete += "\n\n" + details
    manual = (
        f"# Перенос правил регистрации\n\nПлан обмена: `{plan}`.\n\n{incomplete}\n\n"
        "Файл заменяет правила регистрации всего плана. Проверьте все обмены этого плана.\n\n"
        + (deletion_mark_instruction(result) + "\n\n" if result.deletion_mark_filter else "")
        + "## 1. Сохраните текущее состояние"
        + saved
        + "## 2. Перенесите настройки узлов\n\n"
        + table(
            ("Старый реквизит", "Новый реквизит или настройка", "Как перенести"),
            ((v.source, v.target, v.instruction) for v in hints),
        )
        + "\n\n**Дату начала и отбор по организациям"
        + settings
        + "## 3. Загрузите файл правил регистрации"
        + Template(load)
        .substitute(plan_name=plan)
        .replace("(шаг 4)", "(загрузка правил)")
        .replace("(шаг 3)", "(шаг 2)")
        .replace("## 5. Проверьте", "## 4. Проверьте")
        + "\nВ комплекте: `registration/RegistrationRules.xml` и `ИНСТРУКЦИЯ.md`.\n"
        "Этот комплект в базу не ставился. Регистрацию и обмен проверьте в тестовой базе.\n"
    )
    output = {
        "registration/RegistrationRules.xml": dump_rules(result.document),
        "ИНСТРУКЦИЯ.md": manual.encode("utf-8"),
    }
    if result.deletion_mark_filter:
        output["manifest.json"] = json_bytes(
            {
                "schema": _SCHEMA,
                "deletion_mark_filter": True,
                "source_rules": result.source_rules_hash,
                "source_file": source_file_hash,
                "deletion_filters": deletion_filter_summary(result),
                "file_hashes": {p: sha256(b) for p, b in sorted(output.items())},
            }
        )
    return output


class RegistrationRetargetMixin(EdAuthoringMixin):
    """Пути агента и опись владения; исходный проект не редактируется."""

    def _registration_source(self, raw: dict) -> tuple[Path, tuple[Path, ...]]:
        if not isinstance(raw, dict) or set(raw) - {
            "project",
            "configuration",
            "configuration_path",
            "extensions",
        }:
            raise ValueError("source: неверные поля источника выгрузки")
        project, path = raw.get("project"), raw.get("configuration_path")
        if (project is None) == (path is None):
            raise ValueError("source: задайте project либо configuration_path")
        extension_paths = raw.get("extensions")
        if project is not None:
            if not isinstance(project, str) or not project.strip():
                raise ValueError("source.project: нужна непустая строка")
            configuration = raw.get("configuration", "full")
            if not isinstance(configuration, str) or not configuration.strip():
                raise ValueError("source.configuration: нужна непустая строка")
            config = self._catalog().configuration(project, configuration)
            folder = self.settings.project_dirs.get(project)
            if folder is None:
                raise ValueError("Папка проекта не подключена")
            root = self._read_path(self._host(resolve(folder, config.dump))).resolve()
            if extension_paths is None:
                extension_paths = [self._host(resolve(folder, p)) for p in config.extensions]
        else:
            if "configuration" in raw or not isinstance(path, str) or not path.strip():
                raise ValueError("source.configuration_path: нужен путь выгрузки без configuration")
            root = self._read_path(path).resolve()
        extension_paths = [] if extension_paths is None else extension_paths
        if (
            not isinstance(extension_paths, list)
            or len(extension_paths) > 16
            or any(not isinstance(p, str) or not p.strip() for p in extension_paths)
        ):
            raise ValueError("source.extensions: упорядоченный список не более 16 путей")
        return root, tuple(self._read_path(p).resolve() for p in extension_paths)

    def _registration_card(
        self, structure_id: str | None, root: Path, extensions: tuple[Path, ...], plan: str
    ) -> tuple[ObjectCard | None, str | None]:
        if not structure_id:
            return None, None
        with self._structure(structure_id) as connection:
            meta = self.store.meta(structure_id)
            names = []
            for extension in extensions:
                xml = parse_xml(
                    "Configuration.xml", (extension / "Configuration.xml").read_text("utf-8-sig")
                )[0]
                names.append(xml.findtext(f"{{{M}}}Properties/{{{M}}}Name", ""))
            if (
                meta.get("source") != "xml"
                or Path(meta.get("source_path", "")).resolve() != root
                or json.loads(meta.get("extensions", "[]")) != names
                or (
                    meta.get("input_hash") != dump_fingerprint((root, *extensions))
                    and not delivered_structure_matches(
                        self,
                        (root, *extensions),
                        meta.get("input_hash", ""),
                        dump_fingerprint((root, *extensions)),
                    )
                )
            ):
                raise RegistrationToolError(
                    "Структура не соответствует выбранной выгрузке и расширениям; "
                    "загрузите её заново.",
                    {"failures": [{"address": f"ПланОбмена.{plan}", "structure_id": structure_id}]},
                    code="registration.snapshot_mismatch",
                )
            card = read_object_card(connection, f"ПланОбмена.{plan}")
            if card is None:
                raise RegistrationPlanNotFoundError("Целевой план не найден в структуре")
            return card, meta.get("input_hash")

    def _registration_previous(self, slot: Path, owner: str) -> dict[str, bytes]:
        """Читает только собственный неизменённый комплект, включая пустые подкаталоги."""
        self._safe_path(slot)
        if not slot.exists():
            return {}
        if not slot.is_dir():
            raise RegistrationToolError("Каталог занят чужим файлом", code="registration.path")
        contents: dict[str, bytes] = {}
        dirs = set()
        for path in slot.rglob("*"):
            self._safe_path(path)
            name = path.relative_to(slot).as_posix()
            if path.is_dir():
                dirs.add(name)
            else:
                contents[name] = path.read_bytes()
                self._limit(len(contents), MAX_FILES, "файлы прежнего комплекта")
                self._limit(sum(map(len, contents.values())), MAX_BYTES, "байты прежнего комплекта")
        if not contents and not dirs:
            return {}
        try:
            receipt = json.loads(contents[_OWNER])
            expected = receipt["file_hashes"]
            if (
                receipt["schema"] != _SCHEMA
                or receipt["owner"] != owner
                or json_bytes(receipt) != contents[_OWNER]
                or receipt["receipt_hash"]
                != sha256(
                    json_bytes(
                        {key: value for key, value in receipt.items() if key != "receipt_hash"}
                    )
                )
                or expected != {p: sha256(b) for p, b in contents.items() if p != _OWNER}
            ):
                raise ValueError
            allowed = {
                parent.as_posix()
                for name in expected
                for parent in Path(name).parents
                if str(parent) != "."
            }
            if dirs != allowed:
                raise ValueError
        except (KeyError, TypeError, ValueError) as error:
            raise RegistrationToolError(
                "Каталог содержит чужие файлы или комплект изменён вручную; "
                "выберите чистую рабочую папку.",
                code="registration.owned_content_changed",
            ) from error
        return contents

    def _registration_write(
        self,
        slot: Path,
        contents: dict[str, bytes],
        previous: dict[str, bytes],
        owner: str,
        verify: Callable[[], None],
    ) -> str:
        """Публикует весь каталог атомарно, сохраняя опись рядом с выдаваемыми файлами."""
        verify()
        self._safe_path(slot)
        slot.parent.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix=".staging-", dir=slot.parent))
        backup = staging.with_name(staging.name + "-previous")
        moved = False
        try:
            for name, content in contents.items():
                path = staging / name
                self._safe_path(path)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(content)
            verify()
            if self._registration_previous(slot, owner) != previous:
                raise RegistrationToolError(
                    "Комплект изменился во время записи", code="registration.stale"
                )
            if contents == previous:
                return "unchanged"
            self._safe_path(slot)
            if slot.exists():
                os.replace(slot, backup)
                moved = True
            try:
                if moved and self._registration_previous(backup, owner) != previous:
                    raise RegistrationToolError(
                        "Комплект изменился при переименовании", code="registration.stale"
                    )
                os.replace(staging, slot)
            except (OSError, RegistrationToolError):
                if moved:
                    os.replace(backup, slot)
                    moved = False
                raise
            if moved:
                with suppress(OSError):
                    shutil.rmtree(backup)
            return "written"
        finally:
            if staging.exists():
                shutil.rmtree(staging)

    def registration_retarget(
        self,
        project_id: str,
        exchange_plan: str,
        node_properties: dict[str, Any],
        source: dict,
        structure_id: str | None = None,
        own_attributes: list[dict] | None = None,
        node_values: list[dict] | None = None,
        extension: dict | None = None,
        mode: str = "preview",
        expected_preview_hash: str | None = None,
        acknowledged_notices: list[str] | None = None,
        offset: int = 0,
        limit: int = 50,
        deletion_mark_filter: bool = False,
        delivery: dict | None = None,
    ) -> dict[str, Any]:
        options = delivery_options(delivery)
        validate_page(offset, limit)
        if not isinstance(deletion_mark_filter, bool):
            raise ValueError("deletion_mark_filter: нужно булево значение")
        if mode not in ("preview", "write"):
            raise ValueError("mode: preview или write")
        if structure_id is not None and (not isinstance(structure_id, str) or not structure_id):
            raise ValueError("structure_id: нужна непустая строка или null")
        if expected_preview_hash is not None and not isinstance(expected_preview_hash, str):
            raise ValueError("expected_preview_hash: нужна строка или null")
        if extension is not None and not isinstance(extension, dict):
            raise ValueError("extension: нужен словарь или null")
        attributes, hints = [], []
        for raw, label in ((own_attributes, "own_attributes"), (node_values, "node_values")):
            if raw is not None and not isinstance(raw, list):
                raise ValueError(f"{label}: нужен список")
        for raw in own_attributes or []:
            value = _fields(raw, "own_attributes", {"name", "type", "synonym"}, set())
            attributes.append(OwnNodeAttribute(value["name"], value["type"], value["synonym"]))
        for raw in node_values or []:
            value = _fields(raw, "node_values", {"source", "target"}, {"instruction"})
            hints.append(NodeValueHint(**value))
        identity = _fields(extension, "extension", {"name", "prefix"}, set()) if extension else None
        if bool(attributes) != bool(identity):
            raise ValueError("own_attributes и extension задаются вместе")
        if acknowledged_notices is not None and (
            not isinstance(acknowledged_notices, list)
            or any(not isinstance(n, str) for n in acknowledged_notices)
        ):
            raise ValueError("acknowledged_notices: нужен список строк")
        with self._lock:
            try:
                document = self._document(project_id)
                opened = self.workspace.get(project_id)
                source_hash = None
                source_bytes = None
                if opened.source_path is None:
                    raise RegistrationToolError(
                        "У проекта нет исходного файла правил; сохраните правила через rules_save "
                        "и откройте файл через rules_open.",
                        code="registration.source_required",
                    )
                if opened.source_path is not None:
                    source_bytes = opened.source_path.read_bytes()
                    source_hash = sha256(source_bytes)
                    # Снимок после перезапуска сериализован заново: origin не равен файлу.
                    if _source_changed(opened, opened.source_path):
                        raise RegistrationToolError(
                            "Файл исходных правил изменился; "
                            "закройте проект и откройте его заново.",
                            code="registration.stale",
                        )
                # Сначала нижний слой проверяет вид документа и имена, до построения путей XML.
                if isinstance(document, RegistrationRules) and (
                    not document.rules()
                    or not any(registration_rule_active(r) for r in document.rules())
                ):
                    raise RegistrationToolError(
                        "Правила пусты или все правила отключены/невалидны: файл заменил бы "
                        "регистрацию всего плана и прекратил регистрировать объекты; "
                        "перенос запрещён.",
                        code="registration.empty_rules",
                    )
                if (
                    isinstance(document, RegistrationRules)
                    and isinstance(exchange_plan, str)
                    and exchange_plan
                    and document.exchange_plan.casefold() == exchange_plan.casefold()
                    and isinstance(node_properties, dict)
                    and not node_properties
                    and not deletion_mark_filter
                ):
                    raise RegistrationToolError(
                        "Правила уже предназначены для целевого плана, "
                        "а отображение реквизитов пусто; повторный перенос не нужен.",
                        code="registration.already_targeted",
                    )
                result = retarget_registration(
                    document, plan_name=exchange_plan, node_properties=node_properties
                )
                root, extensions = self._registration_source(source)
                source_fingerprint = dump_fingerprint((root, *extensions))
                target = None
                if options["mode"] == "user_extension":
                    from kd_rules_mcp.service.extension_delivery import extension_target

                    target = extension_target(self, options, root)
                object_names = (
                    tuple(str(r.get("ОбъектМетаданныхИмя")) for r in result.document.rules())
                    if deletion_mark_filter
                    else None
                )
                host = read_plan_host(
                    root,
                    exchange_plan,
                    extensions=extensions,
                    object_names=object_names,
                    check_plan_content=True,
                )
                host = registration_delivery_host(
                    host, root, exchange_plan, extensions, target, object_names
                )
                card, structure_hash = self._registration_card(
                    structure_id, root, extensions, exchange_plan
                )
                objects = host.objects
                if deletion_mark_filter and structure_id is not None:
                    with self.store.open(structure_id) as connection:
                        # Константы представлены свойствами НаборКонстант, а не объектами
                        # структуры (xmlbuild:build). Для них используем карточку той же выгрузки.
                        objects = tuple(
                            actual if actual is not None else obj
                            for obj in host.objects
                            if (actual := read_object_card(connection, obj.name)) is not None
                            or obj.kind == "Константа"
                        )
                result = retarget_registration(
                    document,
                    plan_name=exchange_plan,
                    node_properties=node_properties,
                    target_plan=card or host.card,
                    deletion_mark_filter=deletion_mark_filter,
                    target_objects=objects,
                )
                result = replace(
                    result,
                    notices=result.notices
                    + registration_plan_notices(
                        result.document, exchange_plan, host.plan_content, host.subscriptions
                    ),
                )
                own = {a.name.casefold() for a in attributes}
                notices = _notices(result, own)
                if (
                    source_bytes is not None
                    and sha256(dump_rules(load_rules(source_bytes))) != result.source_rules_hash
                ):
                    row = {
                        "check": "registration.unsaved_changes",
                        "address": "",
                        "message": "В проекте есть несохранённые правки; "
                        "комплект построен из модели проекта в памяти.",
                        "blocking": False,
                        "requires_acknowledgement": True,
                    }
                    row["id"] = row["check"] + ":" + sha256(json_bytes(row))[:20]
                    notices.append(row)
                blockers = [n for n in notices if n["blocking"]]
                if identity:
                    try:
                        kit = render_registration_kit(
                            result,
                            host,
                            own_attributes=attributes,
                            node_values=hints,
                            extension_name=identity["name"],
                            prefix=identity["prefix"],
                            source_file_hash=source_hash,
                            notices=[
                                n["message"]
                                for n in notices
                                if n["requires_acknowledgement"]
                                and n["check"] != "registration.code_mention"
                            ],
                        )
                        files = dict(kit.files)
                    except RegistrationMissingAttributeError:
                        if not blockers:
                            raise
                        # Нижний слой отказал по полному отчёту; опасный комплект не порождаем.
                        files = {}
                else:
                    files = _manual_files(result, exchange_plan, hints, notices, source_hash)
                inputs = {
                    "document": sha256(dump_rules(document)),
                    "source_file": source_hash,
                    "host": dict(host.input_hashes),
                    "structure": structure_hash,
                    "source": {"root": str(root), "extensions": list(map(str, extensions))},
                    "decisions": {
                        "mapping": node_properties,
                        "attributes": own_attributes,
                        "hints": node_values,
                        "extension": extension,
                    },
                    "notices": notices,
                }
                if deletion_mark_filter:
                    inputs["decisions"]["deletion_mark_filter"] = True
                owner = sha256(
                    json_bytes(
                        {
                            "project_id": project_id,
                            "root": str(root),
                            "source_file": str(opened.source_path.resolve()),
                            "extensions": list(map(str, extensions)),
                            "plan": exchange_plan,
                            "extension": identity,
                        }
                    )
                )
                slot = (
                    self.workspace.root.absolute() / "registration-retarget" / ("reg-" + owner[:16])
                )
                user_delivery = None
                if options["mode"] == "user_extension":
                    if not identity or not files:
                        raise ValueError("user_extension требует own_attributes и extension")
                    slot = self.workspace.root.absolute() / "ed-authoring" / ("reg-" + owner[:16])
                    user_delivery = prepare_user_delivery(self, options, root, files, slot, owner)
                    files = user_delivery.files
                    inputs["delivery"] = user_delivery.fingerprint
                previous = {} if user_delivery else self._registration_previous(slot, owner)
                hashes = {p: sha256(b) for p, b in sorted(files.items())}
                preview_hash = sha256(
                    json_bytes(
                        {"schema": _SCHEMA, "inputs": inputs, "files": hashes, "owner": owner}
                    )
                )
                contents = {"kit/" + p: b for p, b in files.items()}
                receipt = {
                    "schema": _SCHEMA,
                    "owner": owner,
                    "preview_hash": preview_hash,
                    "file_hashes": {p: sha256(b) for p, b in contents.items()},
                }
                contents[_OWNER] = json_bytes(
                    receipt | {"receipt_hash": sha256(json_bytes(receipt))}
                )
                required = [n["id"] for n in notices if n["requires_acknowledgement"]]
                status = "blocked" if blockers else "ready"

                def verify():
                    current_host = read_plan_host(
                        root,
                        exchange_plan,
                        extensions=extensions,
                        object_names=object_names,
                        check_plan_content=True,
                    )
                    _, current_structure = self._registration_card(
                        structure_id, root, extensions, exchange_plan
                    )
                    if (
                        dict(current_host.input_hashes) != inputs["host"]
                        or current_structure != structure_hash
                        or sha256(dump_rules(self._document(project_id))) != inputs["document"]
                        or (
                            opened.source_path is not None
                            and sha256(opened.source_path.read_bytes()) != source_hash
                        )
                    ):
                        raise RegistrationToolError(
                            "Входы изменились; повторите preview", code="registration.stale"
                        )

                verify()
                if mode == "write":
                    if expected_preview_hash != preview_hash:
                        raise RegistrationToolError(
                            "Нужен хеш текущего preview; повторите предварительную сборку.",
                            {"preview_hash": preview_hash},
                            code="registration.stale",
                        )
                    if blockers:
                        raise RegistrationToolError(
                            "В правилах есть недопустимые ссылки или несовместимые типы "
                            "реквизитов узла; запись комплекта запрещена.",
                            {"failures": blockers, "preview_hash": preview_hash},
                            code="registration.missing_attribute",
                        )
                    missing = sorted(set(required) - set(acknowledged_notices or []))
                    if missing:
                        raise RegistrationToolError(
                            "Требуется явное подтверждение замечаний о неполном переносе.",
                            {"required_acknowledgements": missing, "preview_hash": preview_hash},
                            code="registration.ack_required",
                        )
                    status = (
                        user_delivery.write(
                            verify,
                            lambda: input_receipt(
                                (root, *extensions), structure_hash or source_fingerprint
                            ),
                        )
                        if user_delivery
                        else self._registration_write(slot, contents, previous, owner, verify)
                    )
                return {
                    "project_id": project_id,
                    "exchange_plan": exchange_plan,
                    "counts": {
                        "rules": len(result.rules),
                        "renamed_leaves": sum(r.renamed for r in result.rules),
                        "untouched_leaves": sum(r.untouched for r in result.rules),
                        "unused_keys": len(result.unused),
                        "code_mentions": result.code_mentions,
                        "replaced_leaves": sum(r.replaced for r in result.rules),
                        "removed_leaves": sum(r.removed for r in result.rules),
                    },
                    "unused_keys": list(result.unused),
                    "changes": slice_rows([asdict(c) for c in result.changes], offset, limit),
                    "deletion_mark_filter": deletion_filter_summary(result),
                    "notices": slice_rows(notices, offset, limit),
                    "required_acknowledgements": required,
                    "blocking_notices": len(blockers),
                    "files": [
                        {"path": p, "sha256": h, "bytes": len(files[p])} for p, h in hashes.items()
                    ],
                    "preview_hash": preview_hash,
                    "output_path": self._host(slot if user_delivery else slot / "kit"),
                    **(
                        {"delivery": {**options, "files": user_delivery.merge.rows()}}
                        if user_delivery
                        else {}
                    ),
                    "status": status,
                    "written": mode == "write",
                    "runtime_verified": False,
                    "message": {
                        "ready": "Комплект готов к записи.",
                        "blocked": "Исправьте отсутствующие реквизиты.",
                        "written": "Комплект записан.",
                        "unchanged": "Без изменений.",
                    }[status],
                }
            except (RegistrationRetargetError, RegistrationDeliveryError) as error:
                raise RegistrationToolError(
                    str(error),
                    {
                        "failures": [
                            {
                                "address": "ПланОбмена"
                                + (f".{exchange_plan}" if exchange_plan else ""),
                                "message": str(error),
                            }
                        ]
                    },
                    code=error.code,
                ) from error
            except OSError as error:
                raise RegistrationToolError(
                    "Не удалось прочитать входы или записать комплект; проверьте доступ к папкам.",
                    code="registration.io",
                ) from error
            except EdAuthoringPathError as error:
                raise RegistrationToolError(str(error), code="registration.path") from error
