"""Чистая доставка W1: собственный менеджер интерфейса 2 и один маршрут с узлом."""

from __future__ import annotations

import json
from bisect import bisect_right
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import asdict, dataclass, fields, replace
from importlib.resources import files
from types import MappingProxyType
from typing import Any, Literal
from uuid import UUID

from kd_rules_mcp.ed.canonical import canonical_value, canonicalize, model_addresses
from kd_rules_mcp.ed.errors import EdFormatError, EdReadError, EdResourceLimitError
from kd_rules_mcp.ed.forms import ENTRYPOINTS, VERSION_ROUTINE
from kd_rules_mcp.ed.lexer import tokenize
from kd_rules_mcp.ed.model import EdDocument
from kd_rules_mcp.ed.reader import read_manager_text
from kd_rules_mcp.ed.writer import RenderResult, render
from kd_rules_mcp.ed.writer_model import (
    CodeUnit,
    ManagerModel,
    RetainedBlock,
    digest,
    dump_model,
    validate_model,
)
from kd_rules_mcp.ed.writer_readback import check_readback

from .artifacts import validate_manager_files
from .hook import bsl_string, valid_identifier
from .identity import IdentityMap, make_identity_map, refuse
from .instruction import render_manager_instruction
from .manifest import json_bytes, manager_changes, manager_decision_hash, sha256
from .model import AuthoringPreconditionError, ExtensionIdentity
from .xml_dump import (
    Description,
    ManagerHost,
    SubscriptionAddition,
    check_subscription_sources,
    dump_manager_extension,
    manager_identity_roles,
    profile_template,
)

MANAGER_GENERATOR_VERSION = "ed-manager/1"
MANAGER_TEMPLATE_VERSION = "ed-manager-delivery/2"
REQUIRED_ROUTINES = (
    VERSION_ROUTINE,
    *sorted(ENTRYPOINTS),
    "ПередКонвертацией",
    "ПослеКонвертации",
    "ПередОтложеннымЗаполнением",
    "ВыполнитьПроцедуруМодуляМенеджера",
    "ВыполнитьФункциюМодуляМенеджера",
)


@dataclass(frozen=True, slots=True)
class ManagerRoute:
    """Один точный ключ карты плана; кортеж нужен для диагностируемого отказа списка."""

    plan_name: str
    version_key: str | tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ManagerManifest:
    """Отдельный формат доставки; свидетельство формы не повышает runtime_verified."""

    identity: ExtensionIdentity
    identity_map: IdentityMap
    inputs: Mapping[str, Any]
    input_hashes: Mapping[str, str]
    file_hashes: Mapping[str, str]
    decision_hash: str
    entity_hashes: Mapping[str, str]
    entity_addresses: Mapping[str, str]
    changes: Mapping[str, tuple[str, ...]]
    form_evidence: Mapping[str, bool]
    project_id: str
    creation_fingerprint: str

    @property
    def extension_version(self) -> str:
        return self.decision_hash[:12]

    @property
    def runtime_verified(self) -> Literal[False]:
        return False

    def to_dict(self) -> dict[str, Any]:
        result = {
            "schema_version": 1,
            "project_id": self.project_id,
            "creation_fingerprint": self.creation_fingerprint,
            "generator_version": MANAGER_GENERATOR_VERSION,
            "identity": asdict(self.identity),
            "identity_map": {
                "artifact_uuid": self.identity_map.artifact_uuid,
                "objects": dict(self.identity_map.objects),
                "borrowed": dict(self.identity_map.borrowed),
            },
            "inputs": dict(self.inputs),
            "input_hashes": dict(self.input_hashes),
            "file_hashes": dict(self.file_hashes),
            "decision_hash": self.decision_hash,
            "extension_version": self.extension_version,
            "entity_hashes": dict(self.entity_hashes),
            "entity_addresses": dict(self.entity_addresses),
            "changes": {k: list(v) for k, v in self.changes.items()},
            "form_evidence": dict(self.form_evidence),
            "runtime_verified": False,
        }
        for key in ("plan_content_additions", "subscription_adopted_objects"):
            if self.inputs.get(key):
                result[key] = [row["metadata"] for row in self.inputs[key]]
        for key in ("registration_subscription_additions", "registration_objects"):
            if self.inputs.get(key):
                result[key] = self.inputs[key]
        if "pod_stubs" in self.inputs:
            result["pod_stubs"] = self.inputs["pod_stubs"]
        return result

    def to_bytes(self) -> bytes:
        return json_bytes(self.to_dict())

    @classmethod
    def from_bytes(cls, content: bytes) -> ManagerManifest:
        """Чтение манифеста не является проверкой файлов; её выполняет сборщик."""
        try:
            value = json.loads(content)
            if (
                value["generator_version"] != MANAGER_GENERATOR_VERSION
                or value["schema_version"] != 1
                or value["runtime_verified"] is not False
                or not isinstance(value["project_id"], str)
                or not value["project_id"]
                or not isinstance(value["creation_fingerprint"], str)
                or len(value["creation_fingerprint"]) != 64
                or any(c not in "0123456789abcdef" for c in value["creation_fingerprint"])
            ):
                raise ValueError("Неподдерживаемый формат")
            result = cls(
                ExtensionIdentity(**value["identity"]),
                IdentityMap(**value["identity_map"]),
                value["inputs"],
                value["input_hashes"],
                value["file_hashes"],
                value["decision_hash"],
                value["entity_hashes"],
                value["entity_addresses"],
                {k: tuple(v) for k, v in value["changes"].items()},
                value["form_evidence"],
                value["project_id"],
                value["creation_fingerprint"],
            )
            if result.to_bytes() != content:
                raise ValueError("Неканонический манифест")
            return result
        except (ValueError, KeyError, TypeError, AttributeError):
            refuse("owned_content_changed", "Повреждён манифест собственного менеджера")


@dataclass(frozen=True, slots=True)
class ManagerKit:
    files: Mapping[str, bytes]
    manifest: ManagerManifest
    instruction: str
    module_name: str
    status: Literal["ready", "unchanged"]

    def __post_init__(self) -> None:
        object.__setattr__(self, "files", MappingProxyType(dict(sorted(self.files.items()))))


def render_manager_route(
    route: ManagerRoute,
    *,
    module_name: str | None = None,
    prefix: str,
    format_namespace: str | None = None,
    settings_parameter: str = "Настройки",
) -> str:
    """Точная форма (б) пилота; путь без узла этим перехватом не обслуживается."""
    key = _route_key(route)
    if format_namespace is not None:
        if not valid_identifier(settings_parameter):
            refuse("identifier_conflict", "Недопустимое имя параметра настроек")
        body = ""
        if module_name is not None:
            body += (
                f"\tЕсли ТипЗнч({settings_parameter}.ВерсииФорматаОбмена) "
                '= Тип("Соответствие") Тогда\n'
                f"\t\t{settings_parameter}.ВерсииФорматаОбмена.Вставить({bsl_string(key)}, "
                f"{module_name});\n\tКонецЕсли;\n"
            )
        body += (
            f"\tЕсли ТипЗнч({settings_parameter}.РасширенияФорматаОбмена) "
            '= Тип("Соответствие") Тогда\n'
            f"\t\t{settings_parameter}.РасширенияФорматаОбмена.Вставить("
            f"{bsl_string(format_namespace)}, {bsl_string(key)});\n\tКонецЕсли;\n"
        )
        return (
            "#Если Сервер Или ТолстыйКлиентОбычноеПриложение Или ВнешнееСоединение Тогда\n\n"
            '&После("ПриПолученииНастроек")\n'
            f"Процедура {prefix}ПриПолученииНастроек({settings_parameter})\n"
            + body
            + "КонецПроцедуры\n\n#КонецЕсли\n"
        )
    if module_name is None:
        refuse("route_scope_conflict", "Не задано дополнение настроек плана")
    return (
        "#Если Сервер Или ТолстыйКлиентОбычноеПриложение Или ВнешнееСоединение Тогда\n\n"
        "// Подмена маршрута: ключ версии формата этого плана "
        "ведёт на собственный менеджер обмена.\n"
        '&После("ПриПолученииНастроек")\n'
        f"Процедура {prefix}ПриПолученииНастроек(Настройки)\n"
        '\tЕсли ТипЗнч(Настройки.ВерсииФорматаОбмена) = Тип("Соответствие") Тогда\n'
        f"\t\tНастройки.ВерсииФорматаОбмена.Вставить({bsl_string(key)}, {module_name});\n"
        "\tКонецЕсли;\nКонецПроцедуры\n\n#КонецЕсли\n"
    )


def _route_key(route: ManagerRoute) -> str:
    keys = (route.version_key,) if isinstance(route.version_key, str) else route.version_key
    if len(keys) != 1 or not isinstance(keys[0], str):
        refuse(
            "route_scope_conflict",
            "Доставка требует ровно один строковый ключ версии: " + ascii(keys),
            address="Маршрут/" + route.plan_name,
        )
    key = keys[0]
    # Буквенные заполнители Hangul невидимы, хотя isalpha() считает их буквами.
    invisible_letters = "\u115f\u1160\u3164\uffa0"
    if not key or any(
        not (c.isalpha() or c.isdecimal() or c in ".-_") or c in invisible_letters for c in key
    ):
        refuse(
            "route_scope_conflict",
            "Недопустимый ключ версии "
            + ascii(key)
            + ": нужны буквы, цифры, точка, дефис или подчёркивание",
            address="Маршрут/" + route.plan_name,
        )
    return key


def _host_dict(host: ManagerHost) -> dict[str, Any]:
    def description(value: Description) -> dict[str, Any]:
        return {
            "path": value.path,
            "kind": value.kind,
            "name": value.name,
            "uuid": value.uuid,
            "props": dict(value.props),
            "generated_types": [list(t) for t in value.generated_types],
        }

    return {
        "configuration_uuid": host.configuration_uuid,
        "language": description(host.language),
        "exchange_plan": description(host.exchange_plan),
        "compatibility_mode": host.compatibility_mode,
        "interface_compatibility_mode": host.interface_compatibility_mode,
        "identity": asdict(host.identity),
    }


def _check_server_methods(document: EdDocument) -> None:
    """Принимает безусловные методы и типовую охрану всего модуля.

    Читатель уже даёт контекст и диапазоны условий. Лексер отделяет комментарии
    и строки при сверке формы директивы и границ охраняемого модуля.
    """
    guards = {}
    for guard in document.guards:
        tokens = tokenize(guard.expression_raw)
        # Читатель может назначить preprocessor подвид headers_only/condition_name.
        if tokens and tokens[0].kind == "directive":
            guards[guard.entity_id] = guard
    source = document.files[0]
    code_tokens = [t for t in tokenize(source.text) if t.kind not in ("comment", "directive")]
    standard = (
        "если",
        "сервер",
        "или",
        "толстыйклиентобычноеприложение",
        "или",
        "внешнеесоединение",
        "тогда",
    )
    allowed = set()
    for guard in guards.values():
        condition = tuple(
            t.folded
            for t in tokenize(guard.expression_raw.removeprefix("#"))
            if t.kind != "comment"
        )
        if (
            guard.branch == "if"
            and guard.parent_id is None
            and condition == standard
            and all(
                guard.span.char_start <= t.start and t.end <= guard.span.char_end
                for t in code_tokens
            )
        ):
            allowed.add(guard.entity_id)
    required = {name.casefold() for name in REQUIRED_ROUTINES}
    for routine in document.routines:
        if routine.name.casefold() not in required:
            continue
        contexts = [key for key in routine.guards if key in guards]
        if contexts and (len(contexts) != 1 or contexts[0] not in allowed):
            refuse(
                "model_invalid",
                "Обязательный метод "
                + routine.name
                + " находится под недопустимым условием препроцессора; "
                "нужна доступность на сервере",
                address="Код/" + routine.name,
            )


def _reread(model: ManagerModel, rendered: RenderResult) -> EdDocument:
    try:
        validate_model(model)
        if rendered.data.decode("utf-8-sig") != rendered.text:
            raise ValueError("Текст и байты порождённого модуля различаются")
        document = read_manager_text(rendered.data.decode("utf-8"))
        routines = {r.name.casefold(): r for r in document.routines}
        for name in REQUIRED_ROUTINES:
            routine = routines.get(name.casefold())
            kind = (
                "function"
                if name in (VERSION_ROUTINE, "ВыполнитьФункциюМодуляМенеджера")
                else "procedure"
            )
            if routine is None or not routine.exported or routine.routine_kind != kind:
                refuse(
                    "model_invalid",
                    "Отсутствует обязательный экспортный метод менеджера",
                    address="Код/" + name,
                )
        _check_server_methods(document)
        if document.parse_status != "complete":
            address = document.unknown[0].name if document.unknown else "Конвертация"
            refuse("model_invalid", "Порождённый модуль прочитан не полностью", address=address)
        mismatch = check_readback(model, document)
        if mismatch is not None:
            refuse("model_invalid", mismatch.message, address=mismatch.address)
        expected = render(
            model, rendered.report.mode, use_source_style=rendered.report.use_source_style
        )
        if expected != rendered:
            refuse(
                "model_invalid",
                "Текст или отчёт листьев не соответствует модели",
                address="Конвертация/Раскладка",
            )
        return document
    except AuthoringPreconditionError:
        raise
    except (ValueError, UnicodeError, EdFormatError, EdReadError, EdResourceLimitError) as error:
        refuse("model_invalid", str(error), address="Конвертация")


def manager_source_map(
    model: ManagerModel, rendered: RenderResult, module_path: str
) -> dict[str, Any]:
    """Логические ID → строки результата; смещения отчёта включают BOM и UTF-8."""
    addresses = model_addresses(model)
    elements = {e.logical_id: e for c in model.layouts for e in c.elements}
    owners = {e.logical_id: c.logical_id for c in model.layouts for e in c.elements}
    containers = {c.logical_id: c for c in model.layouts}
    spans: dict[str, list[tuple[int, int]]] = defaultdict(list)
    own_text: dict[str, list[bytes]] = defaultdict(list)
    for entry in rendered.report.entries:
        element = elements.get(entry.leaf_id)
        owner = (
            (element.entity_id or element.block_id or element.logical_id)
            if element
            else entry.leaf_id.rsplit("/", 1)[0]
        )
        span = (entry.byte_start, entry.byte_end)
        spans[owner].append(span)
        own_text[owner].append(rendered.data[entry.byte_start : entry.byte_end])
        if element:
            spans[entry.leaf_id].append(span)
        container_id = owners.get(entry.leaf_id, owner if owner in containers else "")
        while container_id:
            spans[container_id].append(span)
            container_id = containers[container_id].owner_id or ""
    line_starts = [0, *(n + 1 for n, char in enumerate(rendered.data) if char == 10)]
    result = {}
    for key, ranges in sorted(spans.items()):
        start, end = min(a for a, _ in ranges), max(b for _, b in ranges)
        result[key] = {
            "address": addresses.get(key, "Конвертация"),
            "file": module_path,
            "line_start": bisect_right(line_starts, start),
            "line_end": bisect_right(line_starts, max(start, end - 1)),
            "byte_start": start,
            "byte_end": end,
            "text_sha256": sha256(b"".join(own_text.get(key, ()))),
        }
    return result


def _entity_hashes(model: ManagerModel, source_map: Mapping[str, Any]) -> dict[str, str]:
    addresses = model_addresses(model)
    layouts = {c.logical_id: c for c in model.layouts}
    result = {}
    for member in model.members():
        excluded = {"properties", "groups", "events", "search_sets", "mappings"}
        if isinstance(member, RetainedBlock):
            excluded |= {"file_id", "source_hash", "char_start", "char_end"}
        if isinstance(member, CodeUnit):
            excluded |= {"file_id", "body_start", "body_end", "origin"}
        own = {
            f.name: getattr(member, f.name).logical_id
            if f.name == "identification"
            else canonical_value(getattr(member, f.name), addresses, f.name)
            for f in fields(member)
            if f.name not in excluded and not f.name.startswith("_")
        }
        text_hash = source_map.get(member.logical_id, {}).get("text_sha256", "")
        container = layouts.get(member.logical_id)
        order = [e.logical_id for e in container.elements] if container else []
        result[member.logical_id] = sha256(
            json_bytes({"model": own, "order": order, "text_sha256": text_hash})
        )
    for key, row in source_map.items():
        if key not in result and row["text_sha256"] != sha256(b""):
            result[key] = sha256(
                json_bytes({"address": row["address"], "text_sha256": row["text_sha256"]})
            )
    return result


def _check_previous(previous: ManagerManifest, previous_files: Mapping[str, bytes]) -> None:
    validate_manager_files(previous.to_dict(), previous_files)


def _delivery_changed(
    previous: ManagerManifest,
    inputs: Mapping[str, Any],
    output: Mapping[str, bytes],
    form_evidence: Mapping[str, bool],
) -> bool:
    if any(
        previous.inputs.get(key) != inputs[key] for key in ("template_version", "templates_sha256")
    ):
        return True
    if (
        previous.inputs.get("canonical_model_sha256") == inputs["canonical_model_sha256"]
        and previous.inputs.get("rendering") == inputs["rendering"]
        and previous.inputs.get("module_sha256") != inputs["module_sha256"]
    ):
        return True
    # Старый генератор мог не менять метку версии. Его файлы всё равно принадлежат
    # прежнему манифесту; отличие вывода при тех же входах видно в причине дельты.
    return previous.inputs == inputs and any(
        previous.file_hashes.get(path) != sha256(payload)
        for path, payload in output.items()
        if path.startswith("extension/")
        or path == "source-map.json"
        or (path == "instruction.md" and previous.form_evidence == form_evidence)
    )


def _adopted_input(obj: Description) -> dict[str, str]:
    """Заимствуемый объект во входах решения: смена описания меняет decision_hash."""
    return {
        "metadata": obj.kind + "." + obj.name,
        "uuid": obj.uuid,
        "description_sha256": sha256(
            json_bytes(
                (obj.path, obj.kind, obj.name, obj.uuid, dict(obj.props), obj.generated_types)
            )
        ),
    }


def render_manager_kit(
    model: ManagerModel,
    rendered: RenderResult,
    host: ManagerHost,
    route: ManagerRoute,
    *,
    executor_profile_id: str,
    form_evidence: Mapping[str, bool] | None = None,
    content_objects: tuple[Description, ...] = (),
    subscription_additions: tuple[SubscriptionAddition, ...] = (),
    subscription_objects: tuple[Description, ...] = (),
    registration_objects: tuple[str, ...] = (),
    registered_objects: tuple[str, ...] = (),
    pod_stubs: tuple[dict[str, str], ...] = (),
    data_preflight: str = "",
    previous_manifest: ManagerManifest | None = None,
    previous_files: Mapping[str, bytes] | None = None,
    keep_version: bool = False,
    creation_fingerprint: str = "",
) -> ManagerKit:
    """Порождает байты, ничего не записывает. Профильные ed.writer.* проверяет сервис.

    subscription_objects — объекты добавляемых источников подписок вне content_objects:
    заимствуются без изменения состава плана.
    keep_version сохраняет печать только при тех же решениях; изменённая сборка всегда
    получает decision_hash[:12]. Дельта прежней сборки сохраняется при повторе без правок.
    """
    if model.header.interface_version != 2:
        refuse(
            "unsupported_form",
            "Доставка W1 поддерживает интерфейс менеджера 2",
            address="Конвертация/header.interface_version",
        )
    key = _route_key(route)
    bindings = tuple(binding.key for binding in model.format_bindings)
    if bindings and key not in bindings:
        refuse(
            "route_scope_conflict",
            "Ключ маршрута "
            + ascii(key)
            + " не входит в привязки формата модели: "
            + ascii(bindings),
            address="Конвертация/format_bindings",
        )
    if route.plan_name != host.exchange_plan.name:
        refuse(
            "route_scope_conflict",
            "План маршрута не совпадает с заимствованным планом",
            address="Маршрут/" + route.plan_name,
        )
    prefix = host.identity.prefix
    name = model.header.manager_name
    module_name = name if name.startswith(prefix) else prefix + name
    for address, identifier in (
        ("Конвертация/header.manager_name", name),
        ("Расширение/prefix", prefix),
        ("Расширение/name", host.identity.name),
        ("Расширение/Модуль", module_name),
        ("Маршрут/Перехват", prefix + "ПриПолученииНастроек"),
        ("Маршрут/План", route.plan_name),
        ("Расширение/Язык", host.language.name),
    ):
        if not valid_identifier(identifier):
            refuse("identifier_conflict", "Недопустимое имя 1С: " + identifier, address=address)
    for address, identifier in (
        ("Расширение/Модуль", module_name),
        ("Расширение/name", host.identity.name),
    ):
        if len(identifier) > 80:
            refuse(
                "identifier_conflict",
                "Имя должно быть не длиннее 80 знаков: " + identifier,
                address=address,
            )
    try:
        for value in (host.configuration_uuid, host.language.uuid, host.exchange_plan.uuid):
            UUID(value)
    except ValueError:
        refuse(
            "metadata_profile_unsupported",
            "Неверный UUID конфигурации, языка или плана",
            address="Конфигурация",
        )
    if (
        host.language.kind != "Language"
        or host.exchange_plan.kind != "ExchangePlan"
        or not host.language.props.get("LanguageCode")
    ):
        refuse(
            "metadata_profile_unsupported",
            "Неподдерживаемые сведения принимающей конфигурации",
            address="Конфигурация",
        )
    for field_name, mode in (
        ("CompatibilityMode", host.compatibility_mode),
        ("InterfaceCompatibilityMode", host.interface_compatibility_mode),
    ):
        if not mode.strip():
            refuse(
                "metadata_profile_unsupported",
                "Не задан режим основной конфигурации: " + field_name,
                address="Конфигурация/" + field_name,
            )
    if bool(previous_manifest) != (previous_files is not None):
        refuse("owned_content_changed", "Прежний манифест и все файлы нужны вместе")
    if previous_manifest is not None:
        assert previous_files is not None
        if previous_manifest.project_id != model.project_id:
            refuse(
                "kit_owned_by_other_project",
                f"Комплект проекта «{previous_manifest.project_id}» нельзя заменить проектом "
                f"«{model.project_id}». Выберите другое identity.name или закройте прежний проект.",
            )
        _check_previous(previous_manifest, previous_files)
    _reread(model, rendered)
    check_subscription_sources(content_objects, subscription_additions, subscription_objects)
    paths, borrowed = manager_identity_roles(
        host, module_name, content_objects, subscription_additions, subscription_objects
    )
    ids = make_identity_map(host.configuration_uuid, host.identity.name, paths, borrowed)
    model_bytes = dump_model(model)
    template = (
        files("kd_rules_mcp.authoring.ed").joinpath("templates/manager_instruction.md").read_bytes()
    )
    inputs = {
        "canonical_model_sha256": sha256(json_bytes(canonicalize(model))),
        "module_sha256": sha256(rendered.data),
        "route": {"plan_name": route.plan_name, "version_key": key},
        "executor_profile_id": executor_profile_id,
        "host": _host_dict(host),
        "template_version": MANAGER_TEMPLATE_VERSION,
        "templates_sha256": sha256(json_bytes(profile_template()) + template),
        "rendering": {
            "mode": rendered.report.mode,
            "use_source_style": rendered.report.use_source_style,
        },
    }
    for input_key, objects in (
        ("plan_content_additions", content_objects),
        ("subscription_adopted_objects", subscription_objects),
    ):
        if objects:
            inputs[input_key] = [_adopted_input(obj) for obj in objects]
    if data_preflight:
        inputs["data_preflight_sha256"] = sha256(data_preflight.encode("utf-8"))
    if subscription_additions:
        inputs["registration_subscription_additions"] = [
            {
                "name": s.description.name,
                "uuid": s.description.uuid,
                "event": s.description.props.get("Event", ""),
                "sources": list(s.sources),
            }
            for s in subscription_additions
        ]
    if registration_objects:
        inputs["registration_objects"] = list(registration_objects)
    if registered_objects:
        inputs["registered_objects"] = list(registered_objects)
    inputs["pod_stubs"] = list(pod_stubs)
    decision_hash = manager_decision_hash(inputs)
    if keep_version and previous_manifest and previous_manifest.decision_hash != decision_hash:
        refuse(
            "model_invalid",
            "Нельзя сохранить версию расширения при изменённых решениях",
            address="Расширение/Версия",
        )
    module_path = "extension/CommonModules/" + module_name + "/Ext/Module.bsl"
    source_map = manager_source_map(model, rendered, module_path)
    entity_hashes = _entity_hashes(model, source_map)
    try:
        xml = dump_manager_extension(
            host,
            module_name,
            ids,
            version=decision_hash[:12],
            content_objects=content_objects,
            subscription_additions=subscription_additions,
            subscription_objects=subscription_objects,
        )
    except ValueError:
        refuse(
            "metadata_profile_unsupported",
            "Сведения содержат недопустимые для XML символы",
            address="Конфигурация",
        )
    result = {"extension/" + path: payload for path, payload in xml.items()}
    result[module_path] = rendered.data
    result["extension/ExchangePlans/" + route.plan_name + "/Ext/ManagerModule.bsl"] = (
        render_manager_route(route, module_name=module_name, prefix=prefix).encode("utf-8")
    )
    result["manager.ed.json"] = model_bytes
    result["source-map.json"] = json_bytes(source_map)
    instruction = render_manager_instruction(
        model,
        extension_name=host.identity.name,
        module_name=module_name,
        plan_name=route.plan_name,
        version_key=key,
        build_hash=decision_hash,
        form_evidence=form_evidence,
        paths=tuple(sorted((*result, "manifest.json", "instruction.md"))),
        compatibility_mode=host.compatibility_mode,
        interface_compatibility_mode=host.interface_compatibility_mode,
        plan_content_additions=tuple(obj.kind + "." + obj.name for obj in content_objects),
        registration_subscriptions=tuple(
            s.description.name + ": " + ", ".join(s.sources) for s in subscription_additions
        ),
        registration_objects=registration_objects,
        subscription_objects=tuple(obj.kind + "." + obj.name for obj in subscription_objects),
    )
    # Заглушки — до раздела проверки данных: сервис дополняет «Проверьте данные перед первым
    # обменом» по последнему заголовку и считает всё после него частью раздела (Д-П3).
    if pod_stubs:
        instruction += (
            "\n## Заглушки ПОД\n\n"
            "Объекты состава плана без правил получают ПОД отправки с пустым списком ПКО: "
            "они могут регистрироваться, но не отправляются; "
            "сообщение продолжает формироваться (#73). "
            "Регистрация снимается штатным механизмом БСП, при квитировании — после подтверждения. "
            "Перечень объектов и ПОД: "
            + "; ".join(row["object"] + " → " + row["name"] for row in pod_stubs)
            + ".\n"
        )
    instruction += data_preflight
    result["instruction.md"] = instruction.encode("utf-8")
    changes = manager_changes(
        entity_hashes,
        previous_manifest.entity_hashes if previous_manifest else None,
        reasons=("изменилась версия шаблонов доставки",)
        if previous_manifest
        and _delivery_changed(previous_manifest, inputs, result, dict(form_evidence or {}))
        else (),
    )
    addresses = model_addresses(model)
    manifest = ManagerManifest(
        host.identity,
        ids,
        inputs,
        {
            **{k: sha256(json_bytes(v)) for k, v in inputs.items()},
            "manager.ed.json": sha256(model_bytes),
        },
        {p: sha256(b) for p, b in sorted(result.items())},
        decision_hash,
        entity_hashes,
        {
            k: addresses.get(k) or str(source_map.get(k, {}).get("address", "Конвертация"))
            for k in entity_hashes
        },
        changes,
        dict(form_evidence or {}),
        model.project_id,
        creation_fingerprint or digest((model.host, model.format_bindings)),
    )
    status: Literal["ready", "unchanged"] = "ready"
    if previous_manifest is not None and previous_files is not None:
        repeated = replace(manifest, changes=previous_manifest.changes)
        if {**result, "manifest.json": repeated.to_bytes()} == dict(previous_files):
            manifest = repeated
            status = "unchanged"
    result["manifest.json"] = manifest.to_bytes()
    return ManagerKit(
        result,
        manifest,
        instruction,
        module_name,
        status,
    )
