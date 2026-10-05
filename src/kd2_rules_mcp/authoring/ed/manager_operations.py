"""Атомарные решения над полным менеджером; renderer и сервис не вызываются."""

from __future__ import annotations

import re
from dataclasses import dataclass, field, fields, replace
from typing import Any, Literal, NoReturn, get_args

from kd2_rules_mcp.ed.canonical import model_addresses
from kd2_rules_mcp.ed.diff import ManagerChange, compare_models
from kd2_rules_mcp.ed.errors import EdFormatError
from kd2_rules_mcp.ed.forms import (
    CONVERSION_EVENTS,
    DISPATCHERS,
    ENTRYPOINTS,
    EVENT_INVOCATIONS,
    EVENT_SIGNATURES,
    IDENTIFICATIONS,
    RULE_PARAMETERS,
    VERSION_ROUTINE,
    event_name,
)
from kd2_rules_mcp.ed.lexer import split_arguments, tokenize
from kd2_rules_mcp.ed.writer_forms import literal
from kd2_rules_mcp.ed.writer_import import (
    code_occurrences,
    code_rule_references,
    code_texts,
    import_signature,
    parameter_accesses,
    parameter_dependencies,
    property_directions,
)
from kd2_rules_mcp.ed.writer_model import (
    CodeUnit,
    Decision,
    Direction,
    DispatcherCase,
    Event,
    ExecutorProfile,
    Formal,
    FormatBinding,
    Guard,
    Host,
    Identification,
    LayoutContainer,
    LayoutElement,
    ManagerModel,
    ObjectRule,
    Parameter,
    PredefinedRule,
    ProcessingRule,
    Property,
    Reference,
    RetainedBlock,
    RuleUse,
    SearchSet,
    Signature,
    TextStyle,
    Value,
    ValueMapping,
    content_hash,
    decode_dto,
    digest,
    json_bytes,
    json_value,
    leaf_fingerprint,
    logical_id,
    text_hash,
    validate_model,
)
from kd2_rules_mcp.errors import (
    EdAuthoringPreconditionError,
    EdAuthoringResourceLimitError,
    EdAuthoringStaleError,
)

OperationKind = Literal[
    "manager",
    "pod",
    "pko",
    "property",
    "identification",
    "table_part",
    "pkpd",
    "value_mapping",
    "parameter",
    "algorithm",
    "routine",
    "handler",
    "conversion_event",
    "write_policy",
]
Action = Literal["create", "update", "delete", "move"]
MAX_OPERATIONS = 100


def _direction_rank(direction: str) -> int:
    return {"send": 0, "receive": 1, "both": 2}.get(direction, 3)


@dataclass(frozen=True, slots=True)
class ManagerPatch:
    manager_name: str | None = None
    interface_version: int | None = None
    title: Value | None = None
    generated_at: Value | None = None
    text_style: TextStyle | None = None
    host: Host | None = None
    format_bindings: tuple[FormatBinding, ...] | None = None
    executor_profile: ExecutorProfile | None = None


@dataclass(frozen=True, slots=True)
class PodPatch:
    name: str | None = None
    directions: tuple[Direction, ...] | None = None
    configuration_selection: Value | None = None
    format_selection: Value | None = None
    clear_data: Value | None = None
    used_pko: tuple[Reference, ...] | None = None
    events: tuple[Event, ...] | None = None

    def __post_init__(self) -> None:
        if self.directions is not None:
            object.__setattr__(
                self, "directions", tuple(sorted(self.directions, key=_direction_rank))
            )


@dataclass(frozen=True, slots=True)
class PkoPatch:
    name: str | None = None
    directions: tuple[Direction, ...] | None = None
    configuration_object: Value | None = None
    format_object: Value | None = None
    group_flag: Value | None = None
    events: tuple[Event, ...] | None = None
    identification: IdentificationPatch | None = None

    def __post_init__(self) -> None:
        if self.directions is not None:
            object.__setattr__(
                self, "directions", tuple(sorted(self.directions, key=_direction_rank))
            )


@dataclass(frozen=True, slots=True)
class PropertyPatch:
    configuration_property: str | None = None
    format_property: str | None = None
    namespace: str | None = None
    argument_presence: tuple[bool, ...] | None = None
    property_kind: Literal["direct", "reference", "pkpd", "algorithm"] | None = None
    algorithm_flag: int | None = None
    conversion: Reference | None = None


@dataclass(frozen=True, slots=True)
class IdentificationPatch:
    mode: Value | None = None
    search_sets: tuple[tuple[str, ...], ...] | None = None
    not_found_policy: Value | None = None


@dataclass(frozen=True, slots=True)
class PkpdPatch:
    name: str | None = None
    directions: tuple[Direction, ...] | None = None
    data_kind: Literal["enumeration", "predefined"] | None = None
    configuration_type: Value | None = None
    format_type: Value | None = None


@dataclass(frozen=True, slots=True)
class ValueMappingPatch:
    direction: Literal["send", "receive"] | None = None
    configuration_value: Value | None = None
    format_value: Value | None = None


@dataclass(frozen=True, slots=True)
class ParameterPatch:
    name: str | None = None
    default: Value | None = None


@dataclass(frozen=True, slots=True)
class HandlerPatch:
    event: str | None = None
    body: str | None = None
    target: Reference | None = None
    restore_dispatcher: bool | None = None


@dataclass(frozen=True, slots=True)
class ConversionEventPatch:
    body: str | None = None


@dataclass(frozen=True, slots=True)
class AlgorithmPatch:
    name: str | None = None
    routine_kind: Literal["procedure", "function"] | None = None
    parameters: str | None = None
    exported: bool | None = None
    body: str | None = None


Patch = (
    ManagerPatch
    | PodPatch
    | PkoPatch
    | PropertyPatch
    | IdentificationPatch
    | PkpdPatch
    | ValueMappingPatch
    | ParameterPatch
    | HandlerPatch
    | ConversionEventPatch
    | AlgorithmPatch
)
_PATCHES = {
    "manager": ManagerPatch,
    "pod": PodPatch,
    "pko": PkoPatch,
    "property": PropertyPatch,
    "identification": IdentificationPatch,
    "pkpd": PkpdPatch,
    "value_mapping": ValueMappingPatch,
    "parameter": ParameterPatch,
    "handler": HandlerPatch,
    "conversion_event": ConversionEventPatch,
    "algorithm": AlgorithmPatch,
}


@dataclass(frozen=True, slots=True)
class ManagerOperation:
    client_id: str
    kind: OperationKind
    action: Action
    target_id: str | None = None
    owner_id: str | None = None
    address: str | None = None
    container_id: str | None = None
    after_id: str | None = ""
    patch: Patch | None = None
    clear: tuple[str, ...] = ()
    unsupported_payload: str = ""
    position_mode: Literal["explicit", "default"] = "explicit"
    _packet_refs: tuple[tuple[str, str], ...] = field(default=(), repr=False)

    def __post_init__(self) -> None:
        if self.after_id == "" and self.action != "create":
            object.__setattr__(self, "after_id", None)
        if not isinstance(self.client_id, str) or not self.client_id or len(self.client_id) > 200:
            raise ValueError("Требуется client_id длиной 1–200")
        if self.kind not in get_args(OperationKind) or self.action not in get_args(Action):
            raise ValueError("Неизвестный вид или действие операции")
        if self.position_mode not in ("explicit", "default") or (
            self.position_mode == "default"
            and (
                self.kind
                not in (
                    "pko",
                    "pod",
                    "property",
                    "pkpd",
                    "value_mapping",
                    "parameter",
                    "handler",
                    "algorithm",
                )
                or self.action != "create"
            )
        ):
            raise ValueError("Позиция по умолчанию допустима только для создания правил")
        expected = _PATCHES.get(self.kind)
        if expected and self.unsupported_payload:
            raise ValueError("Поля будущих срезов недопустимы для операции W1")
        if self.patch is not None and (expected is None or type(self.patch) is not expected):
            raise ValueError("Поля не соответствуют виду операции")
        if self.patch is not None:
            decode_dto(type(self.patch), json_value(self.patch))
        if self.target_id is not None and self.address is not None:
            raise ValueError("Цель задаётся ID либо адресом")
        if self.action in ("delete", "move") and (self.patch is not None or self.clear):
            raise ValueError("Удаление/перемещение не принимают поля изменения")
        if (self.container_id or self.after_id) and self.action not in ("create", "move"):
            raise ValueError("Позиция задаётся только для create/move")


def parse_operation(data: dict[str, Any]) -> ManagerOperation:
    """Закрытая JSON-схема с заранее известными видами следующих срезов."""
    payload = dict(data)
    patch = payload.pop("patch", None)
    packet_refs = []

    def client_reference(value, path):
        if not isinstance(value, dict):
            return value
        if (
            set(value) != {"client_id"}
            or not isinstance(value["client_id"], str)
            or not value["client_id"]
        ):
            raise ValueError(f"{payload.get('client_id', '')}: некорректная ссылка {path}")
        packet_refs.append((path, value["client_id"]))
        return None

    for name in ("owner_id", "container_id", "after_id", "target_id"):
        if name in payload:
            payload[name] = client_reference(payload[name], name)
    if isinstance(patch, dict):
        patch = dict(patch)
        for name in ("conversion", "used_pko"):
            if name not in patch:
                continue
            values = patch[name] if name == "used_pko" else [patch[name]]
            if name == "used_pko" and not isinstance(values, list | tuple):
                continue
            converted = []
            for n, value in enumerate(values):
                path = f"patch.{name}" + (f".{n}" if name == "used_pko" else "")
                if isinstance(value, dict):
                    value = dict(value)
                    if set(value) == {"client_id"}:
                        value = {
                            "kind": "pko" if name == "used_pko" else "conversion",
                            "target_id": client_reference(value, path),
                        }
                    elif "target_id" in value:
                        value["target_id"] = client_reference(value["target_id"], path)
                    value.setdefault("kind", "pko" if name == "used_pko" else "conversion")
                converted.append(value)
            patch[name] = converted if name == "used_pko" else converted[0]
    dto = decode_dto(ManagerOperation, payload)
    if patch is not None:
        kind = _PATCHES.get(dto.kind)
        if kind is None:
            # Известные W2/W3 принимаются с их непрозрачным payload, но никогда
            # не попадают в план исполнения W1. Никакие поля не исполняются.
            if not isinstance(patch, dict):
                raise ValueError("Ожидался объект полей операции")
            return replace(dto, unsupported_payload=json_bytes(patch).decode("utf-8"))
        dto = replace(dto, patch=decode_dto(kind, patch))
    return replace(dto, _packet_refs=tuple(packet_refs))


@dataclass(frozen=True, slots=True)
class ManagerFailure:
    reason: str
    address: str
    message: str
    references: tuple[str, ...] = ()


class ManagerOperationError(EdAuthoringPreconditionError):
    code = "ed_authoring_precondition"

    def __init__(self, failures: tuple[ManagerFailure, ...]):
        self.failures = failures
        super().__init__("; ".join(f.message for f in failures), {"failures": json_value(failures)})


@dataclass(frozen=True, slots=True)
class CanonicalOperation:
    operation: ManagerOperation
    operation_hash: str
    result_id: str


@dataclass(frozen=True, slots=True)
class ManagerNotice:
    code: str
    address: str
    message: str
    references: tuple[str, ...]
    notice_hash: str


@dataclass(frozen=True, slots=True)
class ManagerPreview:
    base_revision: str
    operations: tuple[CanonicalOperation, ...]
    future_revision: str
    changes: tuple[ManagerChange, ...]
    failures: tuple[ManagerFailure, ...]
    skipped: tuple[str, ...]
    notices: tuple[ManagerNotice, ...]
    preview_hash: str
    model: ManagerModel


def _fail(reason: str, address: str, message: str, references: tuple[str, ...] = ()) -> NoReturn:
    raise ManagerOperationError((ManagerFailure(reason, address, message, references),))


def _updates(patch: Patch | None) -> dict[str, Any]:
    return (
        {
            f.name: getattr(patch, f.name)
            for f in fields(patch)
            if getattr(patch, f.name) is not None
        }
        if patch
        else {}
    )


def _identifier(value: str) -> bool:
    return (
        bool(value)
        and (value[0].isalpha() or value[0] == "_")
        and all(c.isalnum() or c == "_" for c in value)
    )


def _references(value: Any):
    if isinstance(value, Reference):
        yield value
    elif isinstance(value, tuple):
        for item in value:
            yield from _references(item)
    elif hasattr(value, "__dataclass_fields__"):
        for field in fields(value):
            if field.name not in {
                "rule",
                "target",
                "dispatcher",
                "conversion",
                "used_pko",
                "dependencies",
                "properties",
                "groups",
                "events",
                "pko",
                "pod",
                "code_units",
                "retained_blocks",
                "conversion_events",
                "dispatcher_cases",
            }:
                continue
            yield from _references(getattr(value, field.name))


def _order_uses(model: ManagerModel, kind: str, key: str, before: ManagerModel) -> ManagerModel:
    rule_ids = {r.logical_id for r in getattr(model, kind)}
    order = tuple(
        e.container_id
        for c in model.layouts
        if c.kind == "module"
        for e in c.elements
        if e.container_id in rule_ids
    )
    location = order.index(key)
    selected = [u for u in model.rule_uses if u.rule.target_id == key]
    uses = [u for u in model.rule_uses if u.rule.target_id != key]
    last_inserted = {}
    for use in selected:
        preceding = next(
            (
                u
                for candidate in reversed(order[:location])
                for u in reversed(uses)
                if u.rule.target_id == candidate and u.direction == use.direction
            ),
            None,
        )
        following = next(
            (
                u
                for candidate in order[location + 1 :]
                for u in uses
                if u.rule.target_id == candidate and u.direction == use.direction
            ),
            None,
        )
        preceding = last_inserted.get(use.direction, preceding)
        position = (
            uses.index(preceding) + 1
            if preceding
            else uses.index(following)
            if following
            else len(uses)
        )
        uses.insert(position, use)
        last_inserted[use.direction] = use
    old_positions = {u.logical_id: n for n, u in enumerate(before.rule_uses)}
    new_positions = {u.logical_id: n for n, u in enumerate(uses)}
    for use in selected:
        if use.logical_id not in old_positions:
            continue
        for opaque in before.rule_uses:
            if opaque.state == "editable" or opaque.logical_id not in new_positions:
                continue
            if (old_positions[use.logical_id] < old_positions[opaque.logical_id]) != (
                new_positions[use.logical_id] < new_positions[opaque.logical_id]
            ):
                _fail(
                    "opaque_context_changed",
                    "Конвертация/rule_uses",
                    "Перемещение проходит через сохранённое использование",
                )
    return replace(model, rule_uses=tuple(uses))


def _resolve_operation(model: ManagerModel, op: ManagerOperation) -> ManagerOperation:
    if op._packet_refs:
        updates = {}
        patch = op.patch
        for path, client in op._packet_refs:
            decision = next((d for d in model.decisions if d.client_id == client), None)
            if decision is None:
                _fail(
                    "model_invalid",
                    f"Операция/{op.client_id}",
                    f"Операция {op.client_id}: неизвестный или последующий "
                    f"client_id «{client}» ({path})",
                )
            key = decision.result_ids[0]
            member = next((m for m in model.members() if m.logical_id == key), None)
            if member is None:
                _fail(
                    "model_invalid",
                    f"Операция/{op.client_id}",
                    f"Операция {op.client_id}: сущность client_id «{client}» уже удалена",
                )
            if not path.startswith("patch."):
                updates[path] = key
                continue
            assert patch is not None
            parts = path.split(".")
            reference = (
                getattr(patch, parts[1])
                if len(parts) == 2
                else getattr(patch, parts[1])[int(parts[2])]
            )
            if not isinstance(member, ObjectRule | PredefinedRule):
                _fail(
                    "model_invalid",
                    f"Операция/{op.client_id}",
                    f"Операция {op.client_id}: client_id «{client}» не является ПКО или ПКПД",
                )
            reference = replace(
                reference,
                target_id=key,
                name=reference.name,
                kind="pko" if isinstance(member, ObjectRule) else "pkpd",
                resolution="resolved",
            )
            if len(parts) == 2:
                patch = replace(patch, **{parts[1]: reference})
            else:
                values = list(getattr(patch, parts[1]))
                values[int(parts[2])] = reference
                patch = replace(patch, **{parts[1]: tuple(values)})
        op = replace(op, patch=patch, _packet_refs=(), **updates)
    previous = next((d for d in model.decisions if d.client_id == op.client_id), None)
    if op.action == "create" and op.after_id == "" and previous is not None:
        op = replace(
            op,
            container_id=previous.position_container,
            after_id=previous.position_after,
            position_mode="default",
        )
    if op.action == "create" and op.kind == "property" and op.container_id is None:
        previous = next((d for d in model.decisions if d.client_id == op.client_id), None)
        if previous is not None and op.after_id == "":
            op = replace(
                op,
                container_id=previous.position_container,
                after_id=previous.position_after,
                position_mode="default",
            )
        else:
            owner = next((r for r in model.pko if r.logical_id == op.owner_id), None)
            if owner is not None:
                fields = _updates(op.patch)
                sort_key = _property_key(
                    fields.get("configuration_property", ""),
                    fields.get("format_property", ""),
                    fields.get("algorithm_flag", 0),
                )
                preceding = None
                by_id = {p.logical_id: p for p in owner.properties}
                # Без явного контейнера новая ПКС общая: сортируем только общие
                # свойства шапки, не переносим её внутрь ветки направления.
                container = next(
                    (c for c in model.layouts if c.logical_id == owner.logical_id), None
                )
                if container is None:
                    _fail(
                        "opaque_context_changed",
                        model_addresses(model)[owner.logical_id],
                        "ПКО сохранено целиком: позиция новой ПКС непрозрачна",
                    )
                for element in container.elements:
                    prop = by_id.get(element.entity_id or "")
                    if prop is None:
                        continue
                    if model.source_files and (
                        _property_key(
                            prop.configuration_property, prop.format_property, prop.algorithm_flag
                        )
                        > sort_key
                    ):
                        break
                    preceding = prop.logical_id
                op = replace(
                    op,
                    container_id=owner.logical_id,
                    after_id=preceding if op.after_id == "" else op.after_id,
                    position_mode="default" if op.after_id == "" else "explicit",
                )
    if op.action == "create" and op.kind in ("pko", "pod") and op.container_id is None:
        previous = next((d for d in model.decisions if d.client_id == op.client_id), None)
        if previous is not None and op.after_id == "":
            return replace(
                op,
                container_id=previous.position_container,
                after_id=previous.position_after,
                position_mode="default",
            )
        module = next(c for c in model.layouts if c.logical_id == model.root_layouts[0])
        ids = {r.logical_id for r in getattr(model, op.kind)}
        anchor = next(
            (
                e.logical_id
                for e in reversed(module.elements)
                if e.container_id in ids or e.entity_id in ids
            ),
            None,
        )
        if anchor is None:
            blocks = {b.logical_id: b for b in model.retained_blocks}
            area = "#область " + ("пко" if op.kind == "pko" else "под")
            anchor = next(
                (
                    e.logical_id
                    for e in module.elements
                    if e.block_id and blocks[e.block_id].text.strip().casefold() == area
                ),
                None,
            )
        op = replace(
            op,
            container_id=module.logical_id,
            after_id=anchor if op.after_id == "" else op.after_id,
            position_mode="default" if op.after_id == "" else "explicit",
        )
    if op.after_id == "":
        default_position = (
            op.kind
            in (
                "pko",
                "pod",
                "property",
                "pkpd",
                "value_mapping",
                "parameter",
                "handler",
                "algorithm",
            )
            and op.action == "create"
        )
        if op.action in ("create", "move") and op.container_id:
            container = next((c for c in model.layouts if c.logical_id == op.container_id), None)
            if container:
                op = replace(
                    op,
                    after_id=container.elements[-1].logical_id if container.elements else None,
                    position_mode="default" if default_position else "explicit",
                )
        else:
            op = replace(
                op, after_id=None, position_mode="default" if default_position else "explicit"
            )
    if op.container_id is not None and op.after_id is not None:
        container = next((c for c in model.layouts if c.logical_id == op.container_id), None)
        if container is not None:
            matches = [
                e.logical_id
                for e in container.elements
                if op.after_id in (e.logical_id, e.entity_id, e.container_id)
            ]
            if len(matches) == 1:
                op = replace(op, after_id=matches[0])
    if not op.address:
        return op
    if re.search(r"(?:~|#)\d+(?:/|$)", op.address):
        _fail(
            "unstable_address",
            op.address,
            "Позиционный адрес нельзя изменять; возьмите logical_id из списка",
        )
    matches = [
        d.result_ids[0]
        for d in model.decisions
        if d.client_id == op.client_id
        and op.address.casefold() in {a.casefold() for a in d.address_aliases}
    ]
    if not matches:
        addresses = model_addresses(model)
        matches = [
            key for key, address in addresses.items() if address.casefold() == op.address.casefold()
        ]
    if len(matches) != 1:
        _fail("model_invalid", op.address, "Адрес отсутствует или неоднозначен")
    return replace(op, target_id=matches[0], address=None)


def _property_key(configuration: str, format_name: str, algorithm: int) -> tuple[str, int]:
    # reference/kd3-cfg/DataProcessors/ВыгрузкаМодуля/Ext/ObjectModule.bsl:1966–1969.
    return ((configuration or format_name).casefold().replace("ё", "е"), algorithm)


def _property_arguments(item: Property, address: str) -> Property:
    """Обрезка пустого хвоста по генератору; код из флага не выводится."""
    # reference/kd3-cfg/DataProcessors/ВыгрузкаМодуля/Ext/ObjectModule.bsl:2326–2359.
    if item.algorithm_flag not in (0, 1):
        _fail("model_invalid", address, "Флаг алгоритма должен быть 0 или 1")
    if item.property_kind == "direct" and (
        item.algorithm_flag or item.conversion.name or item.conversion.target_id
    ):
        _fail("model_invalid", address, "Прямая ПКС не принимает алгоритм или правило")
    if item.property_kind in ("reference", "pkpd") and (
        item.algorithm_flag or not (item.conversion.name or item.conversion.target_id)
    ):
        _fail("model_invalid", address, "Ссылочная ПКС требует правило без флага алгоритма")
    if item.property_kind == "algorithm" and item.algorithm_flag != 1:
        _fail("model_invalid", address, "Алгоритмическая ПКС требует флаг 1")
    expected = "pko" if item.property_kind == "reference" else "pkpd"
    if (
        item.property_kind in ("reference", "pkpd")
        and item.conversion.kind != expected
        and not (item.conversion.kind == "conversion" and item.conversion.resolution == "ambiguous")
    ):
        _fail("model_invalid", address, "Вид ссылки не соответствует виду ПКС")
    values = [
        Value("string", item.configuration_property),
        Value("string", item.format_property),
        Value("number", 1) if item.algorithm_flag else Value(),
        Value("string", item.conversion.name) if item.conversion.name else Value(),
        Value("string", item.namespace) if item.namespace else Value(),
    ]
    while len(values) > 2 and values[-1].state == "unset":
        values.pop()
    return replace(
        item,
        argument_values=tuple(values),
        argument_presence=(True, *(v.state != "unset" for v in values)),
    )


def _property_patch(
    model: ManagerModel, current: Property | None, updates: dict[str, Any], address: str
) -> dict[str, Any]:
    if (
        "property_kind" in updates
        and current
        and updates["property_kind"] != current.property_kind
        and not {"algorithm_flag", "conversion"} <= updates.keys()
    ):
        _fail("model_invalid", address, "Смена вида требует явные algorithm_flag и conversion")
    # Имя ссылки по ID определяется на итоговой модели пакета: цель может
    # переименовываться последующей операцией.
    if "argument_presence" in updates:
        # Сохранён контракт W1: бессмысленные хвостовые позиции обрезаются.
        requested = updates.pop("argument_presence")
        namespace = updates.get("namespace", current.namespace if current else "")
        if namespace and (len(requested) < 6 or not requested[5]):
            _fail("model_invalid", address, "Пространство имён противоречит наличию аргумента")
    kind = updates.get("property_kind", current.property_kind if current else "direct")
    if kind != "direct" and updates.get("namespace", current.namespace if current else ""):
        _fail("unsupported_form", address, "Пространство имён новой формы ПКС относится к W4")
    return updates


def _layout_context(containers, container):
    """Владелец и вычисляемый стек условий, без каркаса цепочки веток."""
    guards = []
    while container.kind == "conditional":
        if container.branch != "chain":
            guards.append(container.logical_id)
        container = containers[container.owner_id]
    return container, tuple(reversed(guards))


def _field_layout(model, owner_id, entity_id, values):
    """Новый/очищенный оператор поля занимает собственное место в теле."""
    containers = {c.logical_id: c for c in model.layouts}
    container = containers[owner_id]
    rows = list(container.elements)
    for name, value in values.items():
        present = (
            value.state != "unset"
            if isinstance(value, Value)
            else bool(value)
            if isinstance(value, tuple)
            else True
        )
        existing = [e for e in rows if e.entity_id == entity_id and e.field == name]
        if not present:
            rows = [e for e in rows if e not in existing]
        elif not existing:
            # Новое поле идёт после существующих полей владельца, не меняя
            # взаимного положения остальных операторов и человеческого текста.
            position = max(
                (i + 1 for i, e in enumerate(rows) if e.field and e.field != "properties_start"),
                default=0,
            )
            rows.insert(
                position,
                LayoutElement(
                    logical_id(model.project_id, f"field/{entity_id}/{name}"),
                    "entity",
                    entity_id=entity_id,
                    field=name,
                ),
            )
        if present and name in ("used_pko", "extensions"):
            assert isinstance(value, tuple)
            slots = [e for e in rows if e.entity_id == entity_id and e.field == name]
            for extra in slots[len(value) :]:
                rows.remove(extra)
            for n in range(len(slots), len(value)):
                position = max(
                    (
                        i + 1
                        for i, e in enumerate(rows)
                        if e.entity_id == entity_id and e.field == name
                    ),
                    default=0,
                )
                rows.insert(
                    position,
                    LayoutElement(
                        logical_id(model.project_id, f"field/{entity_id}/{name}/{n}"),
                        "entity",
                        entity_id=entity_id,
                        field=name,
                    ),
                )
    rule = next(r for r in (*model.pko, *model.pod) if r.logical_id == owner_id)
    containers[owner_id] = replace(
        container, name=rule.procedure_name, signature=rule.signature, elements=tuple(rows)
    )
    return replace(model, layouts=tuple(containers.values()))


def _edit_layout(before, after, op, key, owner_id, address):
    """Удаляется только оператор; текстовые элементы соседей остаются на месте."""
    if op.action == "update":
        return after
    containers = {c.logical_id: c for c in after.layouts}
    original = next(
        (
            (c, e)
            for c in before.layouts
            for e in c.elements
            if (e.entity_id == key and not e.field) or e.container_id == key
        ),
        None,
    )
    element = original[1] if original else LayoutElement(key, "entity", entity_id=key)
    if original and element.source and not element.source.container_id:
        # Старый снимок ещё не записывал владельца отрезка; до переноса он известен.
        element = replace(
            element, source=replace(element.source, container_id=original[0].logical_id)
        )
    if op.kind in ("pko", "pod") and op.action == "create":
        rule = next(r for r in getattr(after, op.kind) if r.logical_id == key)
        containers[key] = LayoutContainer(
            key,
            "rule",
            rule.procedure_name,
            signature=rule.signature,
            elements=(
                LayoutElement(
                    logical_id(after.project_id, f"field/{key}/properties_start"),
                    "entity",
                    entity_id=key,
                    field="properties_start",
                ),
            )
            if op.kind == "pko"
            else (),
        )
        if op.kind == "pko" and after.header.interface_version == 3:
            from kd2_rules_mcp.ed.writer_forms import HEADERS_GUARD

            newline = after.header.text_style.newline
            newline = "\r\n" if newline == "mixed" else newline
            tab = after.header.text_style.indent
            text = newline.join(tab + line for line in HEADERS_GUARD.splitlines()) + newline
            block_id = logical_id(after.project_id, f"headers/{key}")
            guard_id = logical_id(after.project_id, f"headers/{key}/guard")
            block = RetainedBlock(
                logical_id=block_id,
                name="Текст",
                state="retained",
                kind="scaffold",
                text=text,
                sha256=text_hash(text),
                file_id="",
                source_hash="",
                owner_id=key,
            )
            guard = Guard(
                logical_id=guard_id,
                name="headers_only",
                state="retained",
                inside_leaf_id=block_id,
                expression="ТолькоЗаголовки",
                branch="if",
                guard_kind="headers_only",
            )
            after = replace(
                after,
                retained_blocks=(*after.retained_blocks, block),
                guards=(*after.guards, guard),
            )
            containers[key] = replace(
                containers[key],
                elements=(
                    LayoutElement(block_id, "text", block_id=block_id),
                    *containers[key].elements,
                ),
            )
        element = LayoutElement(key, "container", container_id=key)
    if original:
        source = containers[original[0].logical_id]
        containers[source.logical_id] = replace(
            source, elements=tuple(e for e in source.elements if e.logical_id != element.logical_id)
        )
    if op.action == "delete":
        removed = set()
        removed_containers = set()

        def discard(container_id):
            removed_containers.add(container_id)
            container = containers.pop(container_id)
            for child in container.elements:
                if child.container_id:
                    discard(child.container_id)
                if child.block_id:
                    removed.add(child.block_id)

        if key in containers:
            discard(key)
        return _prune_generated_conditionals(
            replace(
                after,
                layouts=tuple(containers.values()),
                retained_blocks=tuple(
                    b for b in after.retained_blocks if b.logical_id not in removed
                ),
                guards=tuple(
                    g
                    for g in after.guards
                    if g.logical_id not in removed_containers and g.inside_leaf_id not in removed
                ),
            ),
            {original[0].logical_id} if original else set(),
        )
    if op.container_id is None:
        _fail(
            "model_invalid",
            address,
            "Позиция требует container_id и after_id либо начала контейнера",
        )
    destination = containers.get(op.container_id)
    if destination is None:
        # Идентификатор листа не является контейнером, в него нельзя вставлять.
        reason = (
            "opaque_context_changed"
            if any(b.logical_id == op.container_id for b in after.retained_blocks)
            else "model_invalid"
        )
        _fail(reason, address, "Место вставки не является редактируемым контейнером")
    if destination.state != "editable":
        _fail("opaque_context_changed", address, "Условие контейнера непрозрачно")
    if destination.kind == "conditional" and destination.branch == "chain":
        _fail("model_invalid", address, "Выберите тело ветки, а не каркас цепочки условий")
    if owner_id is not None:
        ancestor, guards = _layout_context(containers, destination)
        if ancestor.logical_id != owner_id or ancestor.kind != "rule":
            _fail("model_invalid", address, "Контейнер принадлежит другому правилу")
        rule = next(r for r in after.pko if r.logical_id == owner_id)
        props = tuple(
            replace(p, guards=guards) if p.logical_id == key and p.guards != guards else p
            for p in rule.properties
        )
        after = replace(
            after,
            pko=tuple(
                replace(r, properties=props) if r.logical_id == owner_id else r for r in after.pko
            ),
        )
    elif destination.kind != "module":
        _fail("model_invalid", address, "Декларация правила вставляется в модуль")
    rows = destination.elements
    position = 0
    boundary = 0
    if owner_id is not None and destination.kind == "rule":
        boundary = next((n + 1 for n, e in enumerate(rows) if e.field == "properties_start"), -1)
        if boundary < 0:
            _fail("unsupported_form", address, "Нет границы инициализации свойств шапки")
    elif destination.kind == "module":
        blocks = {b.logical_id: b for b in after.retained_blocks}
        boundary = next(
            (
                n
                for n, e in enumerate(rows)
                if e.container_id
                or (e.entity_id and not e.field.startswith("header."))
                or (e.block_id and blocks[e.block_id].kind == "routine")
            ),
            len(rows),
        )
        boundary = max(
            boundary,
            max((n + 1 for n, e in enumerate(rows) if e.field.startswith("header.")), default=0),
        )
    position = boundary
    if op.after_id is not None:
        positions = [
            n
            for n, e in enumerate(rows)
            if e.logical_id == op.after_id
            or e.entity_id == op.after_id
            or e.container_id == op.after_id
        ]
        if len(positions) != 1:
            _fail("model_invalid", address, "Сосед отсутствует в выбранном контейнере")
        position = positions[0] + 1
        if position < boundary:
            _fail("model_invalid", address, "Позиция находится до инициализации контейнера")
        if destination.kind == "module":
            # Вставка после правила выходит за все области, содержащие только это правило.
            blocks = {b.logical_id: b for b in after.retained_blocks}
            rule_ids = {r.logical_id for r in (*after.pko, *after.pod)}
            anchor_rule = (
                rows[positions[0]].container_id in rule_ids
                or rows[positions[0]].entity_id in rule_ids
            )

            def region(row):
                block = blocks.get(row.block_id)
                if block is None or block.kind != "trivia":
                    return 0
                text = block.text.strip().casefold()
                return (
                    1
                    if text.startswith("#область")
                    else -1
                    if text.startswith("#конецобласти")
                    else 0
                )

            opened = []
            for i, row in enumerate(rows[:position]):
                mark = region(row)
                if mark == 1:
                    opened.append(i)
                elif mark == -1 and opened:
                    opened.pop()
            for preceding in reversed(opened):
                depth = 1
                following = None
                for i in range(preceding + 1, len(rows)):
                    depth += region(rows[i])
                    if depth == 0:
                        following = i
                        break
                if following is None:
                    continue
                entities = sum(
                    bool(e.container_id or e.entity_id)
                    or bool(e.block_id and blocks[e.block_id].kind == "routine")
                    for e in rows[preceding:following]
                )
                category = blocks[
                    rows[preceding].block_id
                ].text.strip().casefold() == "#область " + ("пко" if op.kind == "pko" else "под")
                if (
                    anchor_rule
                    and entities == 1
                    and not (op.position_mode == "default" and category)
                ):
                    position = following + 1
    if op.action == "create" and destination.kind == "module" and op.after_id is not None:
        siblings = {
            d.result_ids[0]
            for d in before.decisions
            if d.position_container == destination.logical_id and d.position_after == op.after_id
        }
        blocks = {b.logical_id: b for b in after.retained_blocks}
        cursor = position
        while cursor < len(rows):
            row = rows[cursor]
            if row.container_id in siblings:
                position = cursor + 1
            elif not (row.block_id and not blocks[row.block_id].text.strip()):
                break
            cursor += 1
    inserted = [element]
    if op.action == "create" and destination.kind == "module":
        blocks = {b.logical_id: b for b in after.retained_blocks}

        def procedure(row):
            return bool(
                row.container_id
                or (row.block_id and blocks[row.block_id].kind == "routine")
                or (row.entity_id and not row.field)
            )

        newline = after.header.text_style.newline
        newline = "\r\n" if newline == "mixed" else newline
        for side, needed in (
            ("before", position > 0 and procedure(rows[position - 1])),
            ("after", position < len(rows) and procedure(rows[position])),
        ):
            if needed:
                block_id = logical_id(after.project_id, f"spacing/{key}/{side}")
                block = RetainedBlock(
                    logical_id=block_id,
                    name="Текст",
                    state="retained",
                    kind="trivia",
                    text=newline,
                    sha256=text_hash(newline),
                    file_id="",
                    source_hash="",
                    owner_id=destination.logical_id,
                )
                after = replace(after, retained_blocks=(*after.retained_blocks, block))
                trivia = LayoutElement(block_id, "text", block_id=block_id)
                inserted.insert(0, trivia) if side == "before" else inserted.append(trivia)
    containers[destination.logical_id] = replace(
        destination, elements=(*rows[:position], *inserted, *rows[position:])
    )
    return replace(after, layouts=tuple(containers.values()))


def _replace_search_layout(before, after, owner_id, old, new):
    old_ids = {s.logical_id for s in old.search_sets}
    if old.search_sets == new.search_sets:
        return after
    replacements = iter(new.search_sets)
    containers = []
    placed = []
    for container in after.layouts:
        rows = []
        for element in container.elements:
            if element.entity_id in old_ids:
                search = next(replacements, None)
                if search is not None:
                    rows.append(
                        replace(element, logical_id=search.logical_id, entity_id=search.logical_id)
                    )
                    placed.append((container.logical_id, len(rows)))
            else:
                rows.append(element)
        containers.append(
            replace(container, elements=tuple(rows))
            if tuple(rows) != container.elements
            else container
        )
    remaining = tuple(replacements)
    if remaining:
        container_id, position = (
            placed[-1]
            if placed
            else (owner_id, len(next(c for c in containers if c.logical_id == owner_id).elements))
        )
        containers = [
            replace(
                c,
                elements=(
                    *c.elements[:position],
                    *(
                        LayoutElement(s.logical_id, "entity", entity_id=s.logical_id)
                        for s in remaining
                    ),
                    *c.elements[position:],
                ),
            )
            if c.logical_id == container_id
            else c
            for c in containers
        ]
    return replace(after, layouts=tuple(containers))


def _sync_use_layout(before, after, kind, key):
    """Порядок вызовов хранится только в телах заполнителей, отдельно от деклараций."""
    selected = {u.logical_id: u for u in after.rule_uses if u.rule.target_id == key}
    old = {u.logical_id for u in before.rule_uses if u.rule.target_id == key}
    templates = {e.entity_id: e for c in before.layouts for e in c.elements if e.entity_id in old}
    containers = {c.logical_id: c for c in after.layouts}
    for c in after.layouts:
        rows = tuple(e for e in c.elements if e.entity_id not in old)
        if rows != c.elements:
            containers[c.logical_id] = replace(c, elements=rows)
    entry_name = (
        "заполнитьправилаконвертацииобъектов"
        if kind == "pko"
        else "заполнитьправилаобработкиданных"
    )
    entries = [
        c for c in containers.values() if c.kind == "entrypoint" and c.name.casefold() == entry_name
    ]
    if selected and not entries:
        # Новый проект имеет новый заполнитель. Непрозрачный импортированный
        # заполнитель нельзя заменить новым безусловным вызовом.
        if any(
            b.kind == "routine" and entry_name in b.text.casefold() for b in after.retained_blocks
        ):
            _fail("opaque_context_changed", "Конвертация", "Точка входа сохранена целиком")
        entry_id = logical_id(after.project_id, "entrypoint/" + kind)
        entry = LayoutContainer(entry_id, "entrypoint", entry_name)
        containers[entry_id] = entry
        module = containers[after.root_layouts[0]]
        containers[module.logical_id] = replace(
            module,
            elements=(
                *module.elements,
                LayoutElement(entry_id, "container", container_id=entry_id),
            ),
        )
        entries = [entry]
    uses = {u.logical_id: u for u in after.rule_uses}
    for use_id, use in selected.items():
        direction = use.direction
        entry = entries[0]
        destination = (
            entry
            if direction == "both"
            else next(
                (
                    c
                    for c in containers.values()
                    if c.kind == "conditional"
                    and _layout_context(containers, c)[0].logical_id == entry.logical_id
                    and c.direction == direction
                ),
                None,
            )
        )
        if destination is None:
            group_id = logical_id(after.project_id, f"entrypoint/{kind}/{direction}")
            expression = (
                'НаправлениеОбмена = "Получение"'
                if direction == "receive"
                else 'НаправлениеОбмена = "Отправка"'
            )
            destination = LayoutContainer(
                group_id,
                "conditional",
                expression,
                owner_id=entry.logical_id,
                direction=direction,
            )
            containers[group_id] = destination
            if kind == "pod" and direction == "send":
                # reference/kd3-cfg/DataProcessors/ВыгрузкаМодуля/Ext/ObjectModule.bsl:2684–2688.
                newline = after.header.text_style.newline
                newline = "\r\n" if newline == "mixed" else newline
                tab = after.header.text_style.indent
                text = newline.join(
                    (
                        tab * 2 + 'Если ПравилаОбработкиДанных.Колонки.Найти("ОчисткаДанных") '
                        "= Неопределено Тогда",
                        tab * 3 + 'ПравилаОбработкиДанных.Колонки.Добавить("ОчисткаДанных");',
                        tab * 2 + "КонецЕсли;",
                        "",
                    )
                )
                block_id = logical_id(after.project_id, group_id + "/clear-column")
                block = RetainedBlock(
                    logical_id=block_id,
                    name="Текст",
                    state="retained",
                    kind="scaffold",
                    text=text,
                    sha256=text_hash(text),
                    file_id="",
                    source_hash="",
                    owner_id=group_id,
                )
                destination = replace(
                    destination, elements=(LayoutElement(block_id, "text", block_id=block_id),)
                )
                containers[group_id] = destination
                after = replace(after, retained_blocks=(*after.retained_blocks, block))
            after = replace(
                after,
                guards=(
                    *after.guards,
                    Guard(
                        logical_id=group_id,
                        name="direction",
                        expression=expression,
                        branch="if",
                        guard_kind="direction",
                        direction=direction,
                    ),
                ),
            )
            entry = containers[entry.logical_id]
            containers[entry.logical_id] = replace(
                entry,
                elements=(
                    *entry.elements,
                    LayoutElement(group_id, "container", container_id=group_id),
                ),
            )
        sequence = [u.logical_id for u in after.rule_uses if u.direction == direction]
        n = sequence.index(use_id)
        existing = {
            e.entity_id: i for i, e in enumerate(destination.elements) if e.entity_id in uses
        }
        preceding = next(
            (candidate for candidate in reversed(sequence[:n]) if candidate in existing), None
        )
        following = next(
            (candidate for candidate in sequence[n + 1 :] if candidate in existing), None
        )
        position = (
            existing[preceding] + 1
            if preceding
            else existing[following]
            if following
            else len(destination.elements)
        )
        element = templates.get(use_id, LayoutElement(use_id, "entity", entity_id=use_id))
        expected_guards = _layout_context(containers, destination)[1]
        if use.guards != expected_guards:
            after = replace(
                after,
                rule_uses=tuple(
                    replace(u, guards=expected_guards) if u.logical_id == use_id else u
                    for u in after.rule_uses
                ),
            )
        containers[destination.logical_id] = replace(
            destination,
            elements=(*destination.elements[:position], element, *destination.elements[position:]),
        )
    for entry in entries:
        current = containers[entry.logical_id]
        branches = [
            containers[e.container_id]
            for e in current.elements
            if e.container_id
            and containers[e.container_id].kind == "conditional"
            and containers[e.container_id].branch != "chain"
        ]
        if len(branches) == 2 and any(c.opening is None for c in branches):
            ordered = sorted(branches, key=lambda c: c.direction != "send")
            chain_id = logical_id(after.project_id, f"entrypoint/{kind}/chain")
            branch_ids = {c.logical_id for c in ordered}
            chain = LayoutContainer(
                chain_id,
                "conditional",
                "Направления обмена",
                owner_id=current.logical_id,
                branch="chain",
                elements=tuple(
                    LayoutElement(c.logical_id, "container", container_id=c.logical_id)
                    for c in ordered
                ),
            )
            containers[chain_id] = chain
            for n, c in enumerate(ordered):
                containers[c.logical_id] = replace(
                    c, owner_id=chain_id, branch="if" if n == 0 else "elseif"
                )
            first = next(i for i, e in enumerate(current.elements) if e.container_id in branch_ids)
            kept = tuple(e for e in current.elements if e.container_id not in branch_ids)
            containers[current.logical_id] = replace(
                current,
                elements=(
                    *kept[:first],
                    LayoutElement(chain_id, "container", container_id=chain_id),
                    *kept[first:],
                ),
            )
            after = replace(
                after,
                guards=tuple(
                    replace(g, branch=containers[g.logical_id].branch)
                    if g.logical_id in branch_ids
                    else g
                    for g in after.guards
                ),
            )
    return replace(after, layouts=tuple(containers.values()))


def references_to(
    model: ManagerModel,
    target_id: str,
    *,
    ignore_editable_pod: bool = False,
    ignore_editable_properties: bool = False,
    ignore_code: bool = False,
    ignore_declarative_properties: bool = False,
) -> tuple[str, ...]:
    addresses = model_addresses(model)
    result = []
    target = next((m for m in model.members() if m.logical_id == target_id), None)
    for member in model.members():
        if member.logical_id == target_id or isinstance(member, RuleUse):
            continue
        if ignore_code and hasattr(member, "dependencies"):
            continue
        if ignore_declarative_properties and isinstance(member, Property):
            continue
        if ignore_declarative_properties and isinstance(member, ObjectRule):
            refs = _references(member.events)
        elif ignore_declarative_properties and hasattr(member, "properties"):
            continue
        elif ignore_editable_properties and isinstance(member, ObjectRule):
            # Свойства перечисляются отдельно; агрегат не должен скрывать их состояние.
            refs = _references((member.events, member.groups))
        elif (
            ignore_editable_properties
            and isinstance(member, Property)
            and member.state == "editable"
        ):
            continue
        else:
            refs = _references(member)
        if (
            ignore_editable_pod
            and isinstance(member, ProcessingRule)
            and member.state == "editable"
            and member.inside_leaf_id is None
        ):
            refs = (
                ref
                for ref in refs
                if not (
                    ref.kind == "pko"
                    and ref.target_id == target_id
                    and ref.resolution == "resolved"
                )
            )
        if any(
            ref.target_id == target_id
            or (
                target is not None
                and ref.target_id is None
                and ref.name.casefold() == target.name.casefold()
                and ref.kind
                in ("pko", "pod", "pkpd", "pko_lookup", "instruction_rule", "pod_use", "conversion")
            )
            for ref in refs
        ):
            result.append(addresses[member.logical_id])
    return tuple(dict.fromkeys(result))


def _validate_patch(kind: str, updates: dict[str, Any], address: str) -> None:
    for key in ("name", "manager_name"):
        if key in updates and not _identifier(updates[key]):
            _fail("model_invalid", address, "Недопустимое имя BSL")
    for key in ("directions",):
        if key in updates and (
            not updates[key]
            or len(set(updates[key])) != len(updates[key])
            or ("both" in updates[key] and len(updates[key]) != 1)
        ):
            _fail("model_invalid", address, "Направления должны быть заданы без повторов")
    for key in ("configuration_object", "configuration_selection"):
        if key in updates and updates[key].state not in ("unset", "reference"):
            _fail("model_invalid", address, "Метаданные задаются канонической ссылкой")
    for key in ("format_object", "format_selection", "title", "generated_at"):
        if key in updates and updates[key].state not in ("unset", "string"):
            _fail("model_invalid", address, "Ожидалась строка либо явное отсутствие")
    for key in ("clear_data", "group_flag"):
        if key in updates and updates[key].state not in ("unset", "boolean"):
            _fail("model_invalid", address, "Ожидался булев литерал либо явное отсутствие")
    if kind == "identification":
        mode = updates.get("mode")
        if mode is not None and (
            mode.state not in ("unset", "string")
            or (mode.state == "string" and mode.value not in IDENTIFICATIONS)
        ):
            _fail("model_invalid", address, "Неизвестный режим идентификации")
        if any(
            not group or any(not field or field.strip() != field for field in group)
            for group in updates.get("search_sets", ())
        ):
            _fail("model_invalid", address, "Пустое или некорректное поле поиска")
        if "not_found_policy" in updates and updates["not_found_policy"].state != "unset":
            _fail(
                "unsupported_form",
                address,
                "Политика ненайденного объекта требует подтверждённого профиля",
            )
    if kind == "property":
        presence = updates.get("argument_presence")
        if presence is not None and (not 3 <= len(presence) <= 7 or not all(presence[:3])):
            _fail("model_invalid", address, "Неверные обязательные позиции аргументов ПКС")
        for key in ("configuration_property", "format_property", "namespace"):
            if key in updates and (
                type(updates[key]) is not str or any(ord(c) < 32 for c in updates[key])
            ):
                _fail("model_invalid", address, "Некорректное имя свойства")


def _apply_one(model: ManagerModel, op: ManagerOperation) -> tuple[ManagerModel, str]:
    if op.kind in ("handler", "conversion_event", "algorithm"):
        return _apply_code(model, op)
    if op.kind in ("pkpd", "value_mapping", "parameter"):
        return _apply_declarative(model, op)
    addresses = model_addresses(model)
    target_id = op.target_id
    if op.address:
        matches = [
            key for key, address in addresses.items() if address.casefold() == op.address.casefold()
        ]
        if len(matches) != 1:
            _fail("model_invalid", op.address, "Адрес отсутствует или неоднозначен")
        target_id = matches[0]
    address = addresses.get(target_id or "", "Конвертация")
    updates = _updates(op.patch)
    identification = updates.pop("identification", None)
    if identification is not None and (op.kind != "pko" or op.action != "create"):
        _fail("model_invalid", address, "Идентификация в patch ПКО допустима только при создании")
    _validate_patch(op.kind, updates, address)
    allowed_clear = {
        "pod": {"configuration_selection", "format_selection", "clear_data"},
        "pko": {"configuration_object", "format_object", "group_flag"},
        "identification": {"mode", "search_sets", "not_found_policy"},
        "property": set(),
    }.get(op.kind, set())
    if set(op.clear) - allowed_clear or set(op.clear) & set(updates):
        _fail("model_invalid", address, "Некорректный список очистки полей")
    updates.update({key: () if key == "search_sets" else Value() for key in op.clear})
    if "used_pko" in updates:
        known_pko = {r.logical_id: r for r in model.pko}
        for reference in updates["used_pko"]:
            if (
                reference.kind != "pko"
                or reference.resolution != "resolved"
                or reference.target_id not in known_pko
            ):
                _fail("dangling_reference", address, "Используемый ПКО отсутствует")
        updates["used_pko"] = tuple(
            replace(ref, name=known_pko[ref.target_id].name) if not ref.name else ref
            for ref in updates["used_pko"]
        )
    if op.kind == "property":
        helper = next((u for u in model.code_units if u.name.casefold() == "добавитьпкс"), None)
        requested = updates.get("argument_presence", ())
        count = max(len(requested), 6 if updates.get("namespace") else 3)
        if requested and updates.get("namespace") and (len(requested) < 6 or not requested[5]):
            _fail("model_invalid", address, "Пространство имён противоречит наличию аргумента")
        maximum = (
            len(helper.signature.parameters)
            if helper
            else 7
            if model.header.interface_version == 3
            else 6
        )
        if count > maximum:
            _fail(
                "unsupported_form",
                address,
                "Аргументы выходят за сигнатуру импортированного помощника",
            )
        if helper and helper.helper_verified is False:
            _fail("unsupported_form", address, "Смысл помощника ДобавитьПКС не подтверждён")
    if op.kind == "manager":
        if op.action != "update" or target_id or op.owner_id or op.clear:
            _fail("unsupported_form", address, "Для manager поддержано только update")
        if (
            "interface_version" in updates
            and model.source_files
            and not any(
                e.field == "header.interface_version" for c in model.layouts for e in c.elements
            )
        ):
            _fail(
                "opaque_context_changed", address, "Функция версии сохранена в непрозрачном листе"
            )
        root = {
            key: updates.pop(key)
            for key in ("host", "format_bindings", "executor_profile")
            if key in updates
        }
        header = replace(model.header, **updates)
        if header.text_style.encoding != "utf-8":
            _fail("unsupported_form", address, "Писатель поддерживает UTF-8")
        if (header.text_style.newline, header.text_style.indent) != (
            model.header.text_style.newline,
            model.header.text_style.indent,
        ) and model.retained_blocks:
            _fail(
                "opaque_context_changed", address, "Смена оформления переписывает сохранённый текст"
            )
        if any(c in str(header.title.value) + str(header.generated_at.value) for c in "\r\n"):
            _fail("model_invalid", address, "Заголовок должен занимать одну строку")
        if header.title.state == "unset" and header.generated_at.state != "unset":
            _fail("model_invalid", address, "Дата заголовка требует названия")
        if " от " in str(header.generated_at.value) or (
            header.generated_at.state == "unset" and " от " in str(header.title.value)
        ):
            _fail("unsupported_form", address, "Заголовок неоднозначен для формы генератора")
        layouts = model.layouts
        if "title" in updates or "generated_at" in updates:
            module = next(c for c in layouts if c.kind == "module")
            elements = tuple(e for e in module.elements if e.field != "header.title")
            existing = next((e for e in module.elements if e.field == "header.title"), None)
            if header.title.state != "unset":
                if existing is None:
                    existing = LayoutElement(
                        logical_id(model.project_id, "header/title"),
                        "entity",
                        entity_id=logical_id(model.project_id, "header/entity"),
                        field="header.title",
                    )
                    elements = (existing, *elements)
                else:
                    elements = module.elements
            layouts = tuple(
                replace(c, elements=elements) if c.logical_id == module.logical_id else c
                for c in layouts
            )
        return replace(model, header=header, layouts=layouts, **root), model.project_id
    if op.kind not in _PATCHES:
        _fail("unsupported_form", address, "Вид операции не поддержан в этом срезе")
    if op.owner_id is not None and op.kind not in ("property", "identification"):
        _fail("model_invalid", address, "Этот вид операции не принимает владельца")
    items: tuple = ()
    owner = None
    if op.kind in ("property", "identification"):
        owner = next(
            (
                r
                for r in model.pko
                if r.logical_id == op.owner_id
                or any(p.logical_id == target_id for p in r.properties)
                or r.identification.logical_id == target_id
            ),
            None,
        )
        if owner is None:
            _fail("model_invalid", address, "Требуется владелец ПКО")
        assert owner is not None
        if owner.state != "editable" or any(
            b.locks_context and b.owner_id in (owner.logical_id, target_id)
            for b in model.retained_blocks
        ):
            _fail("opaque_context_changed", address, "Окружение содержит непрозрачную декларацию")
        items = owner.properties if op.kind == "property" else (owner.identification,)
    else:
        items = getattr(model, op.kind)
    current = next((item for item in items if item.logical_id == target_id), None)
    if op.action != "create" and current is None:
        _fail("model_invalid", address, "Цель операции отсутствует или имеет другой вид")
    if current is not None and current.state != "editable":
        _fail("opaque_context_changed", address, "Сохранённый фрагмент нельзя редактировать в W1")
    if "events" in updates and updates["events"] != (current.events if current else ()):
        _fail("unsupported_form", address, "Правка привязок событий не поддержана в W1")
    if (
        op.kind in ("pko", "pod")
        and current is not None
        and (
            op.action in ("delete", "move")
            or any(
                name in updates and updates[name] != getattr(current, name)
                for name in ("name", "directions")
            )
        )
    ):
        opaque_uses = tuple(
            addresses[u.logical_id]
            for u in model.rule_uses
            if u.rule.target_id == current.logical_id and u.state != "editable"
        )
        if opaque_uses:
            _fail(
                "opaque_context_changed",
                address,
                "Правка затрагивает сохранённые использования правила",
                opaque_uses,
            )
        saved_calls = tuple(
            addresses[b.logical_id]
            for b in model.retained_blocks
            if any(ref.target_id == current.logical_id for ref in b.dependencies)
        )
        if saved_calls and op.action != "delete":
            _fail(
                "opaque_context_changed",
                address,
                "Правка затрагивает вызовы в сохранённом тексте",
                saved_calls,
            )
    if op.kind in ("pko", "pod") and current is not None and "directions" in updates:
        directions = set(updates["directions"])
        containers = {c.logical_id: c for c in model.layouts}
        for container in model.layouts:
            if (
                container.kind == "conditional"
                and container.branch != "chain"
                and _layout_context(containers, container)[0].logical_id == current.logical_id
                and container.direction not in directions
                and "both" not in directions
            ):
                _fail(
                    "unreachable_group",
                    addresses.get(container.logical_id, address),
                    "Смена направлений делает условную группу недостижимой",
                )
    if isinstance(current, ObjectRule) and any(
        field in updates and updates[field] != getattr(current, field)
        for field in ("directions", "configuration_object", "format_object", "group_flag")
    ):
        opaque = tuple(
            addresses[item.logical_id]
            for item in (*current.properties, *current.groups)
            if item.state != "editable"
        )
        if opaque:
            _fail(
                "opaque_context_changed",
                address,
                "Изменение владельца затрагивает сохранённые свойства",
                opaque,
            )
    if op.action == "create" and (target_id or op.address):
        _fail("model_invalid", address, "ID новой сущности определяется client_id")
    key = current.logical_id if current else logical_id(model.project_id, op.client_id)
    if op.kind == "identification":
        assert owner is not None and current is not None
        assert isinstance(current, Identification)
        if op.action != "update":
            _fail("unsupported_form", address, "Идентификация изменяется атомарным update")
        if owner.directions and not any(d in ("receive", "both") for d in owner.directions):
            _fail("model_invalid", address, "Идентификация применяется к получению")
        if "search_sets" in updates:
            updates["search_sets"] = tuple(
                SearchSet(
                    logical_id=logical_id(model.project_id, op.client_id + f"/search/{n}"),
                    name=str(n),
                    fields=group,
                )
                for n, group in enumerate(updates["search_sets"], 1)
            )
        item = replace(current, **updates)
        if (
            item.mode.value
            in ("ПоПолямПоиска", "СначалаПоУникальномуИдентификаторуПотомПоПолямПоиска")
            and not item.search_sets
        ):
            _fail("model_invalid", address, "Режим поиска требует хотя бы одной альтернативы")
        owner = replace(owner, identification=item)
        changed = replace(
            model, pko=tuple(owner if r.logical_id == owner.logical_id else r for r in model.pko)
        )
        changed = _replace_search_layout(model, changed, owner.logical_id, current, item)
        if "mode" in updates:
            changed = _field_layout(changed, owner.logical_id, item.logical_id, {"mode": item.mode})
        return changed, key
    if (
        op.kind in ("pko", "pod")
        and current
        and (op.action == "delete" or ("name" in updates and updates["name"] != current.name))
    ):
        dependencies = references_to(
            model,
            key,
            ignore_editable_pod=op.action != "delete",
            ignore_editable_properties=op.action != "delete",
            ignore_code=op.kind in ("pko", "pod") and op.action != "delete",
            ignore_declarative_properties=op.kind == "pko" and op.action != "delete",
        )
        if op.action != "delete" and dependencies:
            _fail(
                "opaque_context_changed",
                address,
                "Переименование требует явной правки зависимостей",
                dependencies,
            )
    if op.action == "create":
        if op.kind == "property":
            if not {"configuration_property", "format_property"} <= updates.keys():
                _fail("model_invalid", address, "Новая ПКС требует обе стороны")
            updates = _property_patch(model, None, updates, address)
            item = _property_arguments(
                Property(
                    logical_id=key,
                    name=updates["format_property"] or updates["configuration_property"],
                    **updates,
                ),
                address,
            )
        else:
            if not updates.get("name") or not updates.get("directions"):
                _fail("model_invalid", address, "Новое правило требует имя и направления")
            prefix = "ДобавитьПОД_" if op.kind == "pod" else "ДобавитьПКО_"
            signature = Signature(
                parameters=tuple(
                    Formal(
                        name,
                        default=(
                            Value("string", "")
                            if name == "ВерсияФорматаОбмена"
                            else Value("boolean", False)
                            if name == "ТолькоЗаголовки"
                            else Value()
                        ),
                    )
                    for name in RULE_PARAMETERS[
                        "pod"
                        if op.kind == "pod"
                        else "pko_v3"
                        if model.header.interface_version == 3
                        else "pko_old"
                    ]
                )
            )
            if op.kind == "pod":
                item = ProcessingRule(
                    logical_id=key,
                    procedure_name=prefix + updates["name"],
                    signature=signature,
                    **updates,
                )
            else:
                item = ObjectRule(
                    logical_id=key,
                    procedure_name=prefix + updates["name"],
                    signature=signature,
                    identification=Identification(
                        logical_id=logical_id(model.project_id, op.client_id + "/identification"),
                        name="Идентификация",
                    ),
                    **updates,
                )
        items = (*items, item)
    elif op.action == "delete":
        assert current is not None
        if op.kind == "pko":
            event_leaves = {
                e.entity_id: e.block_id for c in model.layouts for e in c.elements if e.block_id
            }
            saved_events = tuple(
                addresses.get(
                    e.inside_leaf_id or event_leaves.get(e.logical_id) or e.logical_id, address
                )
                for e in current.events
                if e.state != "editable" or e.inside_leaf_id
            )
            if saved_events:
                _fail(
                    "opaque_context_changed",
                    address,
                    "Удаление затрагивает сохранённые привязки событий",
                    saved_events,
                )
        if any(b.owner_id == key and b.locks_context for b in model.retained_blocks):
            _fail("opaque_context_changed", address, "Удаление затрагивает сохранённое окружение")
        items = tuple(item for item in items if item.logical_id != key)
    elif op.action == "move":
        pass
    else:
        assert current is not None
        if op.kind == "property":
            assert isinstance(current, Property)
            updates["name"] = updates.get(
                "format_property", current.format_property
            ) or updates.get("configuration_property", current.configuration_property)
            updates = _property_patch(model, current, updates, address)
        if "name" in updates and op.kind in ("pko", "pod"):
            assert isinstance(current, ObjectRule | ProcessingRule)
            prefix = "ДобавитьПКО_" if op.kind == "pko" else "ДобавитьПОД_"
            old_prefix = prefix + current.name
            suffix = (
                current.procedure_name[len(old_prefix) :]
                if current.procedure_name.startswith(old_prefix)
                else ""
            )
            updates["procedure_name"] = prefix + updates["name"] + suffix
        item = replace(current, **updates)
        if op.kind == "property":
            assert isinstance(item, Property)
            item = _property_arguments(item, address)
        items = tuple(item if old.logical_id == key else old for old in items)
    if op.kind == "property":
        assert owner is not None
        owner = replace(owner, properties=items)
        changed = replace(
            model, pko=tuple(owner if r.logical_id == owner.logical_id else r for r in model.pko)
        )
        return _edit_layout(model, changed, op, key, owner.logical_id, address), key
    result = replace(model, **{op.kind: items})
    if "name" in updates and op.action == "update":
        rule = next(item for item in items if item.logical_id == key)
        result = replace(
            result,
            pod=tuple(
                replace(
                    pod,
                    used_pko=tuple(
                        replace(ref, name=rule.name) if ref.target_id == key else ref
                        for ref in pod.used_pko
                    ),
                )
                if op.kind == "pko" and pod.state == "editable"
                else pod
                for pod in result.pod
            ),
            rule_uses=tuple(
                replace(
                    use, name=rule.procedure_name, rule=replace(use.rule, name=rule.procedure_name)
                )
                if use.rule.target_id == key
                else use
                for use in result.rule_uses
            ),
        )
        # Имена ссылок ПКС меняются после всего пакета: два одноимённых ПКО
        # разных направлений должны переименовываться атомарно (ревью Б2).
        assert current is not None
        result = _rename_rule_handlers(result, current, rule)
    if op.action == "create" or "directions" in updates or op.action == "delete":
        uses = tuple(u for u in result.rule_uses if u.rule.target_id != key)
        if op.action != "delete":
            rule = next(r for r in items if r.logical_id == key)
            uses += tuple(
                next(
                    (u for u in model.rule_uses if u.rule.target_id == key and u.direction == d),
                    None,
                )
                or RuleUse(
                    logical_id=logical_id(model.project_id, op.client_id + f"/use/{n}"),
                    name=rule.procedure_name,
                    rule=Reference("rule", key, rule.procedure_name, "resolved"),
                    direction=d,
                )
                for n, d in enumerate(rule.directions, 1)
            )
        result = replace(result, rule_uses=uses)
    result = _edit_layout(model, result, op, key, None, address)
    if op.action in ("create", "update"):
        field_names = (
            ("name", "configuration_object", "format_object", "group_flag", "extensions")
            if op.kind == "pko"
            else ("name", "configuration_selection", "format_selection", "clear_data", "used_pko")
        )
        fields_to_write = {name: updates[name] for name in field_names if name in updates}
        result = _field_layout(result, key, key, fields_to_write)
    if op.action in ("create", "move") or "directions" in updates:
        result = _order_uses(result, op.kind, key, model)
    if op.action in ("create", "move", "delete") or "directions" in updates:
        result = _sync_use_layout(model, result, op.kind, key)
    if op.kind in ("pko", "pod") and current is not None and op.action == "delete":
        assert isinstance(current, ObjectRule | ProcessingRule)
        owned: set[str] = {
            e.target.target_id
            for e in current.events
            if e.event != _DEFERRED and e.target.target_id is not None
        }
        external = _handler_usage_references(model, owned, {e.logical_id for e in current.events})
        if external:
            _fail(
                "dangling_reference", address, "Обработчики правила используются вне него", external
            )
        result = _drop_code(
            result,
            {
                u.logical_id
                for u in result.code_units
                if u.logical_id in owned and "handler" in u.roles
            },
        )
    if op.kind in ("pko", "pod") and (op.action == "delete" or "name" in updates):
        result = _sync_dispatchers(model, result)
        result = _refresh_code_dependencies(model, result)
    if op.kind in ("pko", "pod") and op.action == "delete":
        used_guards = {g for m in result.members() if not isinstance(m, Guard) for g in m.guards}
        used_guards.update(g.logical_id for g in result.guards if g.inside_leaf_id)
        pending = list(used_guards)
        guards = {g.logical_id: g for g in result.guards}
        while pending:
            current_guard = guards.get(pending.pop())
            if (
                current_guard
                and current_guard.parent_id
                and current_guard.parent_id not in used_guards
            ):
                used_guards.add(current_guard.parent_id)
                pending.append(current_guard.parent_id)
        result = replace(
            result, guards=tuple(g for g in result.guards if g.logical_id in used_guards)
        )
    if identification is not None:
        rule = next(r for r in result.pko if r.logical_id == key)
        result, _ = _apply_one(
            result,
            ManagerOperation(
                op.client_id,
                "identification",
                "update",
                target_id=rule.identification.logical_id,
                patch=identification,
            ),
        )
    return result, key


def _renamed_frame_comment(unit: CodeUnit, name: str) -> str:
    """Только удостоверенный комментарий с именем метода принадлежит его рамке."""
    return (
        unit.frame_comment.replace(unit.name, name, 1)
        if unit.frame_comment.strip() == "// " + unit.name
        else unit.frame_comment
    )


def _rename_rule_handlers(model: ManagerModel, old, new) -> ManagerModel:
    units = {u.logical_id: u for u in model.code_units}
    changed_names = {}
    for event in new.events:
        unit = units.get(event.target.target_id or "")
        if unit is None or "handler" not in unit.roles:
            continue
        prefix = "ПКО_" if isinstance(new, ObjectRule) else "ПОД_"
        expected = prefix + old.name + "_" + event.event
        if unit.name != expected:
            _fail(
                "unsupported_form",
                model_addresses(model)[unit.logical_id],
                "Имя обработчика отличается от формы генератора",
            )
        name = prefix + new.name + "_" + event.event
        changed_names[unit.logical_id] = name
        units[unit.logical_id] = replace(
            unit, name=name, frame_comment=_renamed_frame_comment(unit, name)
        )

    def events(rows):
        return tuple(
            replace(e, target=replace(e.target, name=changed_names[e.target.target_id]))
            if e.target.target_id in changed_names
            else e
            for e in rows
        )

    return replace(
        model,
        code_units=tuple(units.values()),
        pko=tuple(replace(r, events=events(r.events)) for r in model.pko),
        pod=tuple(replace(r, events=events(r.events)) for r in model.pod),
    )


_EVENT_ORDER = (
    "ПриОбработке",
    "ВыборкаДанных",
    "ПриОтправкеДанных",
    "ПриКонвертацииДанныхXDTO",
    "ПередЗаписьюПолученныхДанных",
    "ПослеЗагрузкиВсехДанных",
)
_DEFERRED = "ПослеЗагрузкиВсехДанных"


def _handler_signature(owner, event: str) -> Signature:
    if event not in _EVENT_ORDER or event == _DEFERRED:
        _fail("unsupported_form", owner.name, "Событие не входит в подтверждённые формы W2")
    pod = isinstance(owner, ProcessingRule)
    if (event in ("ПриОбработке", "ВыборкаДанных")) != pod:
        _fail("model_invalid", owner.name, "Событие не соответствует виду правила")
    if event == "ВыборкаДанных" and not any(d in ("send", "both") for d in owner.directions):
        _fail("model_invalid", owner.name, "ВыборкаДанных применяется только к отправке")
    if (
        not pod
        and not any(d in ("receive", "both") for d in owner.directions)
        and event != "ПриОтправкеДанных"
    ):
        _fail("model_invalid", owner.name, "Событие применяется к получению")
    if (
        not pod
        and event == "ПриОтправкеДанных"
        and not any(d in ("send", "both") for d in owner.directions)
    ):
        _fail("model_invalid", owner.name, "Событие применяется к отправке")
    alternative = 1 if event == "ПриОбработке" and owner.directions == ("receive",) else 0
    return Signature(
        "function" if event == "ВыборкаДанных" else "procedure",
        False,
        tuple(Formal(n) for n in EVENT_SIGNATURES[event][alternative]),
    )


def _check_body(body: str) -> None:
    """Проверяет физические разделители и баланс областей, не смысл кода."""
    forbidden = {
        "\r": "CR (U+000D)",
        "\x0b": "вертикальная табуляция (U+000B)",
        "\x0c": "перевод страницы (U+000C)",
        "\x85": "U+0085",
        "\u2028": "U+2028",
        "\u2029": "U+2029",
        "\x1c": "U+001C",
        "\x1d": "U+001D",
        "\x1e": "U+001E",
    }
    for position, char in enumerate(body):
        if char in forbidden and not (char == "\r" and body[position : position + 2] == "\r\n"):
            _fail(
                "model_invalid",
                "Код",
                f"Недопустимый знак {forbidden[char]} в теле, позиция {position}",
            )
    depth = 0
    for token in tokenize(body):
        if token.kind != "directive":
            continue
        head = token.folded.split(maxsplit=1)[0]
        if head == "#область":
            depth += 1
        elif head == "#конецобласти":
            depth -= 1
            if depth < 0:
                _fail("model_invalid", "Код", "#КонецОбласти без #Область в теле")
    if depth:
        _fail("model_invalid", "Код", "Незакрытая #Область в теле")


def _authored_body(model: ManagerModel, body: str) -> str:
    # reference/kd3-cfg/DataProcessors/ВыгрузкаМодуля/Ext/ObjectModule.bsl:3497–3504.
    newline = (
        "\r\n" if model.header.text_style.newline == "mixed" else model.header.text_style.newline
    )
    _check_body(body)
    lines = body.replace("\r\n", "\n").split("\n") if body else []
    if lines and lines[-1] == "":
        lines.pop()
    return newline + newline.join("\t" + line for line in lines) + (newline if body else "")


def _replace_body(unit: CodeUnit, body: str) -> CodeUnit:
    # Тело — физический текст между заголовком и закрытием, включая переводы строк.
    _check_body(body)
    if not body or (not body.startswith(("\n", "\r\n")) or body.rsplit("\n", 1)[-1].strip()):
        _fail(
            "model_invalid",
            "Код/" + unit.name,
            "Точное тело должно начинаться переводом строки; "
            "перед закрытием допустим только отступ",
        )
    return replace(unit, body=body, sha256=text_hash(body), origin="authored")


def _module_regions(model: ManagerModel, module: LayoutContainer):
    blocks = {b.logical_id: b for b in model.retained_blocks}
    stack = []
    result = {}
    for element in module.elements:
        block = blocks.get(element.block_id or "")
        text = block.text.strip() if block else ""
        if text.casefold().startswith("#область "):
            stack.append(text.split(maxsplit=1)[1])
        result[element.logical_id] = tuple(stack)
        if text.casefold().startswith("#конецобласти") and stack:
            stack.pop()
    return result


def _rule_groups(model: ManagerModel):
    rules = {r.logical_id: r for r in (*model.pko, *model.pod)}
    result = {}
    for module in model.layouts:
        if module.kind != "module":
            continue
        regions = _module_regions(model, module)
        for element in module.elements:
            rule = rules.get(element.container_id or element.entity_id or "")
            if rule is None:
                continue
            area = "пко" if isinstance(rule, ObjectRule) else "под"
            stack = regions[element.logical_id]
            index = next((n for n, s in enumerate(stack) if s.casefold() == area), None)
            tail = stack[index + 1 :] if index is not None else ()
            result[rule.logical_id] = next(
                (
                    s
                    for s in tail
                    if s.casefold() not in ("отправка", "получение")
                    and s.casefold() != rule.name.casefold()
                ),
                "",
            )
    return result


def _code_key(model: ManagerModel, unit_id: str, groups=None):
    groups = _rule_groups(model) if groups is None else groups
    unit = next(u for u in model.code_units if u.logical_id == unit_id)
    for kind, rules in enumerate((model.pod, model.pko)):
        for rule in rules:
            for event in rule.events:
                if event.target.target_id == unit_id and event.event != _DEFERRED:
                    # reference/kd3-cfg/DataProcessors/ВыгрузкаМодуля/Ext/
                    # ObjectModule.bsl:1453–1455,1914–1916: группа, имя/код.
                    return (
                        kind,
                        groups.get(rule.logical_id, "").casefold().replace("ё", "е"),
                        rule.name.casefold().replace("ё", "е"),
                        _EVENT_ORDER.index(event.event)
                        if event.event in _EVENT_ORDER
                        else len(_EVENT_ORDER),
                    )
    return (2, "", unit.name.casefold().replace("ё", "е"), 0)


def _code_arguments(unit: CodeUnit, event: str) -> tuple[Value, ...]:
    if event != _DEFERRED and event not in EVENT_INVOCATIONS:
        _fail(
            "unsupported_form",
            "Код/" + unit.name,
            f"Для расширенного события «{event}» форма вызова не удостоверена",
        )
    names = (
        EVENT_INVOCATIONS[event].keys
        if event != _DEFERRED
        else tuple(p.name or "" for p in unit.signature.parameters)
    )
    # reference/kd3-cfg/DataProcessors/ВыгрузкаМодуля/Ext/ObjectModule.bsl:3104–3128,3258–3259.
    return tuple(
        Value(
            "unknown",
            raw="Параметры."
            + ("КомпонентыОбмена.ПараметрыКонвертации" if n == "ПараметрыКонвертации" else n),
            reference_parts=tuple(
                (
                    "Параметры."
                    + (
                        "КомпонентыОбмена.ПараметрыКонвертации"
                        if n == "ПараметрыКонвертации"
                        else n
                    )
                ).split(".")
            ),
        )
        for n in names
    )


def _active_handlers(model: ManagerModel):
    units = {u.logical_id: u for u in model.code_units}
    result = {}
    for rule in (*model.pod, *model.pko):
        for event in rule.events:
            unit = units.get(event.target.target_id or "")
            if unit is not None:
                result.setdefault(unit.logical_id, (unit, event.event))
    return result


def _drop_code(model: ManagerModel, keys: set[str]) -> ManagerModel:
    containers = {c.logical_id: c for c in model.layouts}
    removed = set(keys)
    cases = {
        c.logical_id
        for c in model.dispatcher_cases
        if c.target.target_id in keys or c.dispatcher.target_id in keys
    }
    blocks = {
        logical_id(model.project_id, key + suffix)
        for key in keys
        for suffix in ("/before", "/after", "/gap")
    }
    blocks.update(
        e.block_id
        for c in model.layouts
        for e in c.elements
        if e.entity_id in keys | cases and e.block_id is not None
    )
    if not any("algorithm" in u.roles and u.logical_id not in keys for u in model.code_units):
        blocks.update(
            logical_id(model.project_id, key + suffix)
            for key in keys
            for suffix in ("/area", "/area-end")
        )

    def discard(key):
        container = containers.get(key)
        if container:
            for e in container.elements:
                if e.container_id:
                    removed.add(e.container_id)
                    discard(e.container_id)
                if e.block_id:
                    blocks.add(e.block_id)

    for key in keys:
        discard(key)
    return replace(
        model,
        code_units=tuple(u for u in model.code_units if u.logical_id not in keys),
        dispatcher_cases=tuple(c for c in model.dispatcher_cases if c.logical_id not in cases),
        layouts=tuple(
            replace(
                c,
                elements=tuple(
                    e
                    for e in c.elements
                    if e.container_id not in removed
                    and e.entity_id not in keys
                    and e.entity_id not in cases
                    and e.block_id not in blocks
                ),
            )
            for c in model.layouts
            if c.logical_id not in removed
        ),
        retained_blocks=tuple(b for b in model.retained_blocks if b.logical_id not in blocks),
        guards=tuple(g for g in model.guards if g.inside_leaf_id not in blocks),
    )


def _insert_code(
    model: ManagerModel, unit: CodeUnit, owner=None, op: ManagerOperation | None = None
) -> ManagerModel:
    containers = {c.logical_id: c for c in model.layouts}
    module = next(c for c in model.layouts if c.kind == "module")
    if op and op.container_id:
        module = containers.get(op.container_id)
        if module is None or module.kind != "module":
            _fail("model_invalid", unit.name, "Метод вставляется только в модуль")
    rows = list(module.elements)
    blocks = {b.logical_id: b for b in model.retained_blocks}
    units = {u.logical_id: u for u in model.code_units}
    algorithm = "algorithm" in unit.roles
    dispatcher = "dispatcher" in unit.roles
    area = (
        "Алгоритмы"
        if algorithm
        else "ПроцедурыКонвертации"
        if dispatcher
        else "ОбработчикиКонвертации"
    )
    area_index = next(
        (
            n
            for n, e in enumerate(rows)
            if e.block_id
            and blocks[e.block_id].text.strip().casefold() == "#область " + area.casefold()
        ),
        None,
    )
    groups = _rule_groups(model)
    own_group = groups.get(owner.logical_id, "") if owner is not None else ""
    siblings = [
        (n, units[e.container_id])
        for n, e in enumerate(rows)
        if e.container_id in units
        and ("algorithm" if algorithm else "dispatcher" if dispatcher else "handler")
        in units[e.container_id].roles
        and e.container_id != unit.logical_id
        and (
            algorithm
            or dispatcher
            or _code_key(model, e.container_id, groups)[1] == own_group.casefold().replace("ё", "е")
        )
    ]
    if op and (op.container_id or op.after_id is not None):
        if op.after_id:
            positions = [
                n
                for n, e in enumerate(rows)
                if op.after_id in (e.logical_id, e.container_id, e.entity_id)
            ]
            if len(positions) != 1:
                _fail("model_invalid", unit.name, "Якорь метода отсутствует")
            position = positions[0] + 1
        else:
            position = 0
    elif not algorithm and area_index is None and owner is not None:
        position = next(n + 1 for n, e in enumerate(rows) if e.container_id == owner.logical_id)
        while (
            not (op and op.position_mode == "explicit" and op.after_id is None)
            and position < len(rows)
            and rows[position].container_id in units
            and any(e.target.target_id == rows[position].container_id for e in owner.events)
        ):
            position += 1
    elif siblings and op and op.position_mode == "explicit" and op.after_id is None:
        position = siblings[0][0]
    elif siblings:
        greater = next(
            (
                n
                for n, u in siblings
                if _code_key(model, u.logical_id, groups)
                > _code_key(model, unit.logical_id, groups)
            ),
            None,
        )
        if not model.source_files:
            greater = None
        position = greater if greater is not None else siblings[-1][0] + 1
    elif area_index is not None:
        regions = _module_regions(model, module)
        group_position = (
            next(
                (
                    n + 1
                    for n, e in enumerate(rows)
                    if e.block_id
                    and blocks[e.block_id].text.strip().casefold()
                    == "#область " + own_group.casefold()
                    and area in regions[e.logical_id]
                ),
                None,
            )
            if own_group
            else None
        )
        position = group_position if group_position is not None else area_index + 1
    else:
        position = len(rows)
    newline = (
        "\r\n" if model.header.text_style.newline == "mixed" else model.header.text_style.newline
    )

    def trivia(suffix, text):
        key = logical_id(model.project_id, unit.logical_id + suffix)
        blocks[key] = RetainedBlock(
            logical_id=key,
            name="Текст",
            kind="trivia",
            state="retained",
            text=text,
            sha256=text_hash(text),
            file_id="",
            source_hash="",
            owner_id=module.logical_id,
        )
        return LayoutElement(key, "text", block_id=key)

    element = LayoutElement(unit.logical_id, "container", container_id=unit.logical_id)
    inserted = [element]
    if algorithm and area_index is None:
        inserted = [
            trivia("/area", "#Область Алгоритмы" + newline),
            element,
            trivia("/area-end", "#КонецОбласти" + newline),
        ]
    if position and (rows[position - 1].container_id or rows[position - 1].entity_id):
        inserted.insert(0, trivia("/before", newline))
    if position < len(rows) and (rows[position].container_id or rows[position].entity_id):
        inserted.append(trivia("/after", newline))
    containers[module.logical_id] = replace(
        module, elements=tuple(rows[:position] + inserted + rows[position:])
    )
    containers[unit.logical_id] = LayoutContainer(
        unit.logical_id,
        "code",
        unit.name,
        signature=unit.signature,
        elements=(
            LayoutElement(
                logical_id(model.project_id, unit.logical_id + "/body"),
                "entity",
                entity_id=unit.logical_id,
                field="body",
            ),
        ),
    )
    return replace(
        model, layouts=tuple(containers.values()), retained_blocks=tuple(blocks.values())
    )


def _sync_dispatchers(
    before: ManagerModel, model: ManagerModel, restore: set[str] | None = None
) -> ManagerModel:
    units = {u.logical_id: u for u in model.code_units}
    model = replace(
        model,
        layouts=tuple(
            replace(c, name=units[c.logical_id].name, signature=units[c.logical_id].signature)
            if c.kind == "code" and c.logical_id in units
            else c
            for c in model.layouts
        ),
    )
    old_active, active = _active_handlers(before), _active_handlers(model)
    old_units = {u.logical_id: u for u in before.code_units}
    renamed = {
        key for key, unit in units.items() if key in old_units and unit.name != old_units[key].name
    }
    restore = restore or set()
    removed = (old_active.keys() - active.keys()) | (
        {u.logical_id for u in before.code_units} - {u.logical_id for u in model.code_units}
    )
    changed_targets = removed | restore | renamed | {key for key in active if key not in old_active}
    if not changed_targets:
        return model
    added = (active.keys() - old_active.keys()) | restore
    units = {u.logical_id: u for u in model.code_units}
    dispatchers = {u.signature.routine_kind: u for u in model.code_units if "dispatcher" in u.roles}
    required = {active[key][0].signature.routine_kind for key in added if key in active}
    for kind in sorted(required - dispatchers.keys()):
        function = kind == "function"
        argument = "ИмяФункции" if function else "ИмяПроцедуры"
        unit = CodeUnit(
            logical_id=logical_id(model.project_id, "dispatcher/" + kind),
            name="ВыполнитьФункциюМодуляМенеджера"
            if function
            else "ВыполнитьПроцедуруМодуляМенеджера",
            signature=Signature(kind, True, (Formal(argument), Formal("Параметры"))),
            body="",
            sha256=text_hash(""),
            origin="authored",
            roles=("dispatcher",),
            parameters_text=argument + ", Параметры",
        )
        model = replace(model, code_units=(*model.code_units, unit))
        model = _insert_code(model, unit)
        newline = (
            "\r\n"
            if model.header.text_style.newline == "mixed"
            else model.header.text_style.newline
        )
        gap_id = logical_id(model.project_id, unit.logical_id + "/gap")
        gap = RetainedBlock(
            logical_id=gap_id,
            name="Текст",
            state="retained",
            kind="trivia",
            text=newline,
            sha256=text_hash(newline),
            file_id="",
            source_hash="",
            owner_id=unit.logical_id,
        )
        model = replace(
            model,
            retained_blocks=(*model.retained_blocks, gap),
            layouts=tuple(
                replace(
                    c, kind="dispatcher", elements=(LayoutElement(gap_id, "text", block_id=gap_id),)
                )
                if c.logical_id == unit.logical_id
                else c
                for c in model.layouts
            ),
        )
        dispatchers[kind] = unit
        units[unit.logical_id] = unit
    addresses = model_addresses(model)
    sources = {s.logical_id: s for s in model.source_map}
    for key in (added | renamed) & active.keys():
        unit = units[key]
        if unit.signature.routine_kind not in dispatchers:
            # Переименование не восстанавливает отсутствующий диспетчер импорта.
            continue
        dispatcher = dispatchers[unit.signature.routine_kind].logical_id
        for case in model.dispatcher_cases:
            if case.dispatcher.target_id != dispatcher or case.name != unit.name:
                continue
            if case.target.target_id == key or case.target.name.casefold() == unit.name.casefold():
                continue
            source = sources.get(case.logical_id)
            place = addresses[case.logical_id] + (f":{source.line_start}" if source else "")
            _fail(
                "model_invalid",
                place,
                f"Литерал «{unit.name}» занят: ветка вызывает «{case.target.name}», "
                f"строка {source.line_start if source else 'неизвестна'}; "
                "добавление другой ветки скроет существующий вызов",
                (place,),
            )
    cases = []
    groups = _rule_groups(model)
    sort_keys = {key: _code_key(model, key, groups) for key in active}
    for case in model.dispatcher_cases:
        if case.target.target_id in removed:
            continue
        pair = active.get(case.target.target_id or "")
        if (
            case.target.target_id in restore
            and case.target.target_id not in renamed
            and case.name != units[case.target.target_id or ""].name
        ):
            # Восстанавливается литерал привязки, а соседний псевдоним остаётся как есть.
            cases.append(case)
            continue
        if (pair or case.target.target_id in renamed) and case.target.target_id in changed_targets:
            if case.state != "editable":
                _fail(
                    "opaque_context_changed",
                    "Ветка/" + case.name,
                    "Ветка с псевдонимом или комментарием сохранена; измените её явно",
                )
            unit = units[case.target.target_id or ""]
            restored = unit.logical_id in restore
            cases.append(
                replace(
                    case,
                    name=unit.name
                    if unit.logical_id in restore or unit.logical_id in renamed
                    else case.name,
                    target=Reference("code_unit", unit.logical_id, unit.name, "resolved"),
                    arguments=_code_arguments(unit, active[unit.logical_id][1])
                    if restored
                    else case.arguments,
                    returns=unit.signature.routine_kind == "function" if restored else case.returns,
                    dispatcher=Reference(
                        "code_unit",
                        dispatchers[unit.signature.routine_kind].logical_id,
                        "",
                        "resolved",
                    )
                    if restored
                    else case.dispatcher,
                )
            )
        else:
            cases.append(case)
    existing = {(c.dispatcher.target_id, c.name) for c in cases}
    for key in sorted(added & active.keys(), key=sort_keys.__getitem__):
        unit, event = active[key]
        dispatcher = dispatchers[unit.signature.routine_kind].logical_id
        if (dispatcher, unit.name) in existing:
            continue
        cases.append(
            DispatcherCase(
                logical_id=logical_id(model.project_id, key + "/case"),
                name=unit.name,
                dispatcher=Reference(
                    "code_unit", dispatchers[unit.signature.routine_kind].logical_id, "", "resolved"
                ),
                target=Reference("code_unit", key, unit.name, "resolved"),
                arguments=_code_arguments(unit, event),
                returns=unit.signature.routine_kind == "function",
            )
        )
    layouts = []
    old_cases = {c.logical_id: c for c in before.dispatcher_cases}
    new_cases = {c.logical_id: c for c in cases}
    touched = {
        c.dispatcher.target_id
        for key in old_cases.keys() | new_cases.keys()
        if old_cases.get(key) != new_cases.get(key)
        for c in (old_cases.get(key), new_cases.get(key))
        if c is not None
    }
    layout_ids = {c.logical_id for c in model.layouts if c.kind == "dispatcher"}
    old_layouts = {c.logical_id: c for c in before.layouts}
    for key in touched:
        if key in units and key not in layout_ids:
            _fail(
                "opaque_context_changed",
                "Код/" + units[key].name,
                "Диспетчер сохранён целиком: его ветки нельзя изменить",
            )
    case_ids = {c.logical_id for c in model.dispatcher_cases}
    for container in model.layouts:
        if container.kind != "dispatcher":
            unit = units.get(container.logical_id)
            layouts.append(
                replace(container, name=unit.name, signature=unit.signature)
                if unit and container.kind == "code"
                else container
            )
            continue
        if container.logical_id not in touched:
            layouts.append(container)
            continue
        selected = {
            c.logical_id: c for c in cases if c.dispatcher.target_id == container.logical_id
        }
        rows = [
            e
            for e in container.elements
            if (e.field != "dispatch_end" or selected)
            and (e.entity_id not in case_ids or e.entity_id in selected)
        ]
        for key, case in selected.items():
            if any(e.entity_id == key for e in rows):
                continue
            position = next(
                (
                    n
                    for n, e in enumerate(rows)
                    if e.entity_id in selected
                    and sort_keys.get(selected[e.entity_id].target.target_id, (3, "", "", 0))
                    > sort_keys.get(case.target.target_id, (3, "", "", 0))
                ),
                next((n for n, e in enumerate(rows) if e.field == "dispatch_end"), len(rows)),
            )
            rows.insert(position, LayoutElement(key, "entity", entity_id=key))
        if selected and not any(e.field == "dispatch_end" for e in rows):
            old_end = next((e for e in container.elements if e.field == "dispatch_end"), None)
            rows.append(
                old_end
                or LayoutElement(
                    logical_id(model.project_id, container.logical_id + "/end"),
                    "entity",
                    entity_id=container.logical_id,
                    field="dispatch_end",
                )
            )
        old_container = old_layouts.get(container.logical_id)
        old_first = (
            next((e.entity_id for e in old_container.elements if e.entity_id in old_cases), None)
            if old_container
            else None
        )
        new_first = next((e.entity_id for e in rows if e.entity_id in selected), None)
        if any(
            c.state != "editable" and ((c.logical_id == old_first) != (c.logical_id == new_first))
            for c in selected.values()
        ):
            _fail(
                "opaque_context_changed",
                "Код/" + container.name,
                "Изменение первой ветки требует регенерации сохранённого текста; "
                "его комментарии или псевдоним не изменяются автоматически",
            )
        layouts.append(replace(container, elements=tuple(rows)))
    # Callback — производная роль, которую читатель восстанавливает по привязкам/веткам.
    bound = (
        set(active)
        | {e.target.target_id for e in model.conversion_events}
        | {c.target.target_id for c in cases}
    )
    result = replace(
        model,
        dispatcher_cases=tuple(cases),
        layouts=tuple(layouts),
        code_units=tuple(
            replace(
                u,
                roles=tuple(
                    sorted(
                        (set(u.roles) - {"callback"})
                        | ({"callback"} if u.logical_id in bound else set())
                    )
                ),
            )
            if "dispatcher" not in u.roles
            else u
            for u in model.code_units
        ),
    )
    generated_function = logical_id(model.project_id, "dispatcher/function")
    if (
        generated_function in touched
        and not any(c.returns for c in cases)
        and any(u.logical_id == generated_function for u in result.code_units)
    ):
        result = _drop_code(result, {generated_function})
    return result


def _code_signature(name: str, kind: str, parameters: str, exported: bool) -> Signature:
    from kd2_rules_mcp.ed.reader import read_manager_text
    from kd2_rules_mcp.ed.writer_forms import empty_module

    heading = "Функция" if kind == "function" else "Процедура"
    closing = "КонецФункции" if kind == "function" else "КонецПроцедуры"
    text = (
        f"{heading} {name}({parameters})" + (" Экспорт" if exported else "") + "\n" + closing + "\n"
    )
    try:
        document = read_manager_text(
            empty_module() + "#Область Алгоритмы\n" + text + "#КонецОбласти\n"
        )
        routine = document.routines[-1]
        signature = import_signature(routine)
        if (
            routine.name != name
            or routine.parameters_raw != parameters
            or any(p.name is None for p in signature.parameters)
        ):
            raise ValueError("Неподдержанная сигнатура")
        if len(document.routines) != 12:
            raise ValueError("Подмена рамки метода")
        return signature
    except Exception as error:
        # Ошибка лексера/читателя относится к введённой сигнатуре, а не к исполнению тела.
        _fail("model_invalid", "Код/" + name, "Некорректная сигнатура: " + type(error).__name__)


def _apply_code(model: ManagerModel, op: ManagerOperation) -> tuple[ManagerModel, str]:
    before = model
    addresses = model_addresses(model)
    address = addresses.get(op.target_id or op.owner_id or "", "Конвертация")
    updates = _updates(op.patch)
    if op.action == "move" or (op.kind == "conversion_event" and op.action != "update"):
        _fail("unsupported_form", address, "Действие не поддержано для кода W2")
    if op.action == "create" and op.target_id:
        _fail("model_invalid", address, "ID нового метода определяется client_id")
    if set(op.clear) - {"body"} or set(op.clear) & set(updates):
        _fail("model_invalid", address, "Для кода можно очистить только тело")
    if op.owner_id and op.kind != "handler":
        _fail("model_invalid", address, "Владелец задаётся только для обработчика правила")
    unit = next((u for u in model.code_units if u.logical_id == op.target_id), None)
    owner = None
    event = next((e for e in model.conversion_events if e.logical_id == op.target_id), None)
    for rule in (*model.pko, *model.pod):
        binding = next(
            (
                e
                for e in rule.events
                if e.logical_id == op.target_id or e.target.target_id == op.target_id
            ),
            None,
        )
        if binding or rule.logical_id == op.owner_id:
            owner = rule
            event = binding
            break
    if event and unit is None:
        unit = next((u for u in model.code_units if u.logical_id == event.target.target_id), None)
    if "body" in op.clear:
        updates["body"] = (
            "\r\n"
            if model.header.text_style.newline == "mixed"
            else model.header.text_style.newline
        )
    if op.kind == "handler":
        if op.action == "delete" and owner is None and unit and "handler" in unit.roles:
            if unit.state != "editable":
                _fail("opaque_context_changed", address, "Рамка обработчика сохранена целиком")
            refs = _handler_usage_references(model, {unit.logical_id}, set())
            if refs:
                _fail("dangling_reference", address, "Обработчик используется", refs)
            changed = _sync_dispatchers(before, _drop_code(model, {unit.logical_id}))
            return _refresh_code_dependencies(before, changed), unit.logical_id
        if (
            owner is None
            or owner.state != "editable"
            or not any(c.logical_id == owner.logical_id for c in model.layouts)
        ):
            _fail("opaque_context_changed", address, "Требуется редактируемый владелец ПКО или ПОД")
        if op.action == "create":
            name = updates.get("event")
            if name not in _EVENT_ORDER or any(e.event == name for e in owner.events):
                _fail(
                    "model_invalid", address, "Событие отсутствует, не поддержано или уже привязано"
                )
            key = logical_id(model.project_id, op.client_id)
            if name == _DEFERRED or "target" in updates:
                reference = updates.get("target")
                unit = next(
                    (
                        u
                        for u in model.code_units
                        if reference
                        and u.logical_id == reference.target_id
                        and u.state == "editable"
                        and bool(set(u.roles) & {"algorithm", "handler"})
                    ),
                    None,
                )
                if (
                    unit is None
                    or "body" in updates
                    or (
                        name == _DEFERRED
                        and (
                            isinstance(owner, ProcessingRule)
                            or "algorithm" not in unit.roles
                            or unit.signature.routine_kind != "procedure"
                        )
                    )
                    or (name != _DEFERRED and unit.signature != _handler_signature(owner, name))
                ):
                    _fail(
                        "model_invalid",
                        address,
                        "Привязка требует существующий метод с сигнатурой события",
                    )
            else:
                if "target" in updates or "body" not in updates:
                    _fail("model_invalid", address, "Новый обработчик требует тело")
                signature = _handler_signature(owner, name)
                unit = CodeUnit(
                    logical_id=logical_id(model.project_id, op.client_id + "/code"),
                    name=("ПКО_" if isinstance(owner, ObjectRule) else "ПОД_")
                    + owner.name
                    + "_"
                    + name,
                    signature=signature,
                    body=_authored_body(model, updates["body"]),
                    sha256=text_hash(_authored_body(model, updates["body"])),
                    origin="authored",
                    roles=("callback", "handler"),
                    parameters_text=", ".join(p.name or "" for p in signature.parameters),
                )
                model = replace(model, code_units=(*model.code_units, unit))
            event = Event(
                logical_id=key,
                name=name,
                event=name,
                target=Reference("code_unit", unit.logical_id, unit.name, "resolved"),
            )
            owner = replace(owner, events=(*owner.events, event))
            if name != _DEFERRED and "body" in updates:
                # Для ключа размещения новая привязка уже должна быть в модели.
                collection = "pko" if isinstance(owner, ObjectRule) else "pod"
                model = replace(
                    model,
                    **{
                        collection: tuple(
                            owner if r.logical_id == owner.logical_id else r
                            for r in getattr(model, collection)
                        )
                    },
                )
                model = _insert_code(model, unit, owner, op)
            container = next(c for c in model.layouts if c.logical_id == owner.logical_id)
            rows = list(container.elements)
            by_id = {e.logical_id: e for e in owner.events}
            position = next(
                (
                    n
                    for n, e in enumerate(rows)
                    if e.entity_id in by_id
                    and (
                        _EVENT_ORDER.index(by_id[e.entity_id].event)
                        if by_id[e.entity_id].event in _EVENT_ORDER
                        else len(_EVENT_ORDER)
                    )
                    > _EVENT_ORDER.index(name)
                ),
                None,
            )
            if position is None:
                position = max(
                    (
                        n + 1
                        for n, e in enumerate(rows)
                        if e.entity_id in by_id
                        or (e.field and e.field not in ("properties_start", "extensions"))
                    ),
                    default=0,
                )
            rows.insert(position, LayoutElement(key, "entity", entity_id=key))
            model = replace(
                model,
                layouts=tuple(
                    replace(c, elements=tuple(rows)) if c.logical_id == owner.logical_id else c
                    for c in model.layouts
                ),
            )
        else:
            if event is None:
                _fail("model_invalid", address, "Привязка обработчика отсутствует")
            key = event.logical_id
            if op.action == "delete":
                if unit and "handler" in unit.roles:
                    external = _handler_usage_references(model, {unit.logical_id}, {key})
                    if external:
                        _fail(
                            "dangling_reference",
                            address,
                            "Обработчик используется вне привязки",
                            external,
                        )
                owner = replace(owner, events=tuple(e for e in owner.events if e.logical_id != key))
                model = replace(
                    model,
                    layouts=tuple(
                        replace(c, elements=tuple(e for e in c.elements if e.entity_id != key))
                        for c in model.layouts
                    ),
                )
                if unit and "handler" in unit.roles:
                    model = _drop_code(model, {unit.logical_id})
            else:
                if (
                    (unit is None and "target" not in updates)
                    or (unit and unit.state != "editable")
                    or set(updates) - {"body", "target", "restore_dispatcher"}
                ):
                    _fail(
                        "model_invalid",
                        address,
                        "Правка обработчика требует существующий метод и точное тело",
                    )
                if "target" in updates:
                    target = next(
                        (
                            u
                            for u in model.code_units
                            if u.logical_id == updates["target"].target_id
                            and u.state == "editable"
                            and bool(set(u.roles) & {"algorithm", "handler"})
                        ),
                        None,
                    )
                    if (
                        target is None
                        or "body" in updates
                        or (
                            event.event == _DEFERRED
                            and (
                                "algorithm" not in target.roles
                                or target.signature.routine_kind != "procedure"
                            )
                        )
                        or (
                            event.event != _DEFERRED
                            and target.signature != _handler_signature(owner, event.event)
                        )
                    ):
                        _fail(
                            "model_invalid",
                            address,
                            "Привязка требует существующий метод с сигнатурой события",
                        )
                    owner = replace(
                        owner,
                        events=tuple(
                            replace(
                                e,
                                target=Reference(
                                    "code_unit", target.logical_id, target.name, "resolved"
                                ),
                            )
                            if e.logical_id == key
                            else e
                            for e in owner.events
                        ),
                    )
                elif "body" in updates:
                    if unit is None or event.event == _DEFERRED:
                        _fail(
                            "model_invalid",
                            address,
                            "Тело отложенного алгоритма изменяется операцией algorithm",
                        )
                    unit = _replace_body(unit, updates["body"])
                    model = replace(
                        model,
                        code_units=tuple(
                            unit if u.logical_id == unit.logical_id else u for u in model.code_units
                        ),
                    )
        collection = "pko" if isinstance(owner, ObjectRule) else "pod"
        changed = replace(
            model,
            **{
                collection: tuple(
                    owner if r.logical_id == owner.logical_id else r
                    for r in getattr(model, collection)
                )
            },
        )
    elif op.kind == "conversion_event":
        if (
            unit is None
            or "event" not in unit.roles
            or unit.state != "editable"
            or set(updates) != {"body"}
        ):
            _fail(
                "model_invalid",
                address,
                "Событие конвертации требует существующий метод и точное тело",
            )
        unit = _replace_body(unit, updates["body"])
        key = event.logical_id if event else unit.logical_id
        changed = replace(
            model,
            code_units=tuple(
                unit if u.logical_id == unit.logical_id else u for u in model.code_units
            ),
        )
    else:
        if op.action != "create" and (
            unit is None or "algorithm" not in unit.roles or unit.state != "editable"
        ):
            _fail(
                "model_invalid",
                address,
                "Алгоритм отсутствует или сохранён как неподдержанная форма",
            )
        if "name" in updates and not _identifier(updates["name"]):
            _fail("model_invalid", address, "Недопустимое имя BSL")
        if "name" in updates:
            name = updates["name"].casefold()
            if (
                name
                in {
                    n.casefold()
                    for n in (*ENTRYPOINTS, *DISPATCHERS, *CONVERSION_EVENTS, VERSION_ROUTINE)
                }
                or name.startswith(("добавитьпко_", "добавитьпод_"))
                or event_name(updates["name"]) is not None
            ):
                _fail("model_invalid", address, "Имя алгоритма относится к служебной форме")
        key = unit.logical_id if unit else logical_id(model.project_id, op.client_id)
        if op.action == "delete":
            refs = references_to(model, key)
            refs += tuple(
                addresses[o.owner_id] + f":{o.line}"
                for o in code_occurrences(model)
                if o.target_id == key and o.owner_id != key
            )
            if refs:
                _fail("dangling_reference", address, "Алгоритм используется", refs)
            changed = _drop_code(model, {key})
        else:
            if op.action == "create" and (not updates.get("name") or "body" not in updates):
                _fail("model_invalid", address, "Новый алгоритм требует имя и тело")
            name = updates.get("name", unit.name if unit else "")
            parameters = updates.get(
                "parameters",
                unit.parameters_text if unit and unit.parameters_text is not None else "",
            )
            kind = updates.get("routine_kind", unit.signature.routine_kind if unit else "procedure")
            exported = updates.get("exported", unit.signature.exported if unit else False)
            signature = _code_signature(name, kind, parameters, exported)
            if (
                unit
                and kind != "procedure"
                and any(
                    e.event == _DEFERRED and e.target.target_id == key
                    for r in model.pko
                    for e in r.events
                )
            ):
                _fail("model_invalid", address, "Отложенный алгоритм должен быть процедурой")
            if unit:
                new = replace(
                    unit,
                    name=name,
                    signature=signature,
                    parameters_text=parameters,
                    frame_comment=_renamed_frame_comment(unit, name),
                )
                if "body" in updates:
                    new = _replace_body(new, updates["body"])
            else:
                body = _authored_body(model, updates["body"])
                new = CodeUnit(
                    logical_id=key,
                    name=name,
                    signature=signature,
                    parameters_text=parameters,
                    body=body,
                    sha256=text_hash(body),
                    roles=("algorithm",),
                    origin="authored",
                )
            changed = replace(
                model,
                code_units=tuple(new if u.logical_id == key else u for u in model.code_units)
                + (() if unit else (new,)),
            )
            if unit and unit.name != name:
                changed = _rename_algorithm_calls(model, changed, key, name)
            if not unit:
                changed = _insert_code(changed, new, op=op)
    if updates.get("restore_dispatcher") and (
        op.kind != "handler" or op.action != "update" or unit is None
    ):
        _fail("model_invalid", address, "Восстановление ветки требует существующую привязку")
    changed = _sync_dispatchers(
        before, changed, {unit.logical_id} if updates.get("restore_dispatcher") and unit else None
    )
    return _refresh_code_dependencies(before, changed), key


def _rename_algorithm_calls(
    before: ManagerModel, model: ManagerModel, key: str, name: str
) -> ManagerModel:
    old_name = next(u.name for u in before.code_units if u.logical_id == key)
    unit_ids = {u.logical_id for u in before.code_units}
    opaque = tuple(
        model_addresses(before)[o.owner_id] + f":{o.line}"
        for o in code_occurrences(before)
        if o.target_id == key and o.kind == "algorithm_call" and o.owner_id not in unit_ids
    )
    if opaque:
        _fail(
            "opaque_context_changed",
            "Код/" + old_name,
            "Вызов алгоритма находится в непрозрачном операторе декларации; измените его явно",
            opaque,
        )
    units = []
    for unit in model.code_units:
        body = unit.body
        tokens = [t for t in tokenize(body) if t.kind != "comment"]
        replacements = [
            t
            for n, t in enumerate(tokens[:-1])
            if t.kind == "identifier"
            and t.folded == old_name.casefold()
            and tokens[n + 1].value == "("
            and (not n or tokens[n - 1].value != ".")
        ]
        if "dispatcher" not in unit.roles:
            for token in reversed(replacements):
                body = body[: token.start] + name + body[token.end :]
        units.append(_replace_body(unit, body) if body != unit.body else unit)

    def events(rows):
        return tuple(
            replace(e, target=replace(e.target, name=name)) if e.target.target_id == key else e
            for e in rows
        )

    return replace(
        model,
        code_units=tuple(units),
        pko=tuple(replace(r, events=events(r.events)) for r in model.pko),
        pod=tuple(replace(r, events=events(r.events)) for r in model.pod),
    )


def _refresh_code_dependencies(before: ManagerModel, model: ManagerModel) -> ManagerModel:
    from kd2_rules_mcp.ed.reader import read_manager_text
    from kd2_rules_mcp.ed.refs import build_references
    from kd2_rules_mcp.ed.writer_forms import code_open, empty_module, routine_close

    old = {u.logical_id: u for u in before.code_units}
    renamed = {
        r.logical_id
        for r in (*before.pko, *before.pkpd)
        if not any(
            n.logical_id == r.logical_id and n.name == r.name for n in (*model.pko, *model.pkpd)
        )
    }
    selected = [
        u
        for u in model.code_units
        if "dispatcher" not in u.roles
        and (
            u.logical_id not in old
            or u.body != old[u.logical_id].body
            or any(r.target_id in renamed for r in u.dependencies)
        )
    ]
    if not selected:
        return model
    text = (
        empty_module()
        + "#Область Алгоритмы\n"
        + "\n".join(code_open(u) + u.body + routine_close(u.signature) + "\n" for u in selected)
        + "#КонецОбласти\n"
    )
    try:
        document = read_manager_text(text)
    except Exception as error:
        _fail("model_invalid", "Код", "Нарушена лексическая рамка тела: " + type(error).__name__)
    methods = {r.name.casefold(): r for r in document.routines}
    index = build_references(document)
    deps = {}
    for unit in selected:
        routine = methods.get(unit.name.casefold())
        if routine is None or len(document.routines) != 11 + len(selected):
            _fail("model_invalid", "Код/" + unit.name, "Тело изменило границы метода")
        rows = []
        for ref in index.entries:
            if ref.owner_id != routine.entity_id or ref.kind == "parameter":
                continue
            targets = (
                [r for r in model.pko if ref.name and r.name.casefold() == ref.name.casefold()]
                if ref.kind in ("pko_lookup", "instruction_rule", "pod_use")
                else []
            )
            rows.append(
                Reference(
                    "pko" if targets else ref.kind,
                    targets[0].logical_id if len(targets) == 1 else None,
                    ref.name or "",
                    "resolved"
                    if len(targets) == 1
                    else "computed"
                    if ref.name is None
                    else "ambiguous"
                    if targets
                    else "missing",
                )
            )
        for token in tokenize(unit.body):
            if token.kind == "string":
                rule = next(
                    (r for r in model.pkpd if r.name.casefold() == token.value.casefold()), None
                )
                if rule:
                    rows.append(Reference("pkpd", rule.logical_id, rule.name, "resolved"))
        deps[unit.logical_id] = tuple(rows) + parameter_dependencies(unit.body, model.parameters)
    return replace(
        model,
        code_units=tuple(
            replace(u, dependencies=deps[u.logical_id]) if u.logical_id in deps else u
            for u in model.code_units
        ),
    )


def _declarative_entry(model: ManagerModel, kind: str) -> LayoutContainer:
    name = (
        "ЗаполнитьПараметрыКонвертации"
        if kind == "parameter"
        else "ЗаполнитьПравилаКонвертацииПредопределенныхДанных"
    )
    found = next(
        (
            c
            for c in model.layouts
            if c.kind == "entrypoint" and c.name.casefold() == name.casefold()
        ),
        None,
    )
    if found is None or found.state != "editable":
        _fail(
            "opaque_context_changed", "Конвертация", "Заполнитель отсутствует или сохранён целиком"
        )
    return found


def _pkpd_destination(
    model: ManagerModel, rule: PredefinedRule
) -> tuple[ManagerModel, LayoutContainer]:
    entry = _declarative_entry(model, "pkpd")
    direction = rule.directions[0] if len(rule.directions) == 1 else "both"
    if direction == "both":
        return model, entry
    containers = {c.logical_id: c for c in model.layouts}
    found = next(
        (
            c
            for c in model.layouts
            if c.kind == "conditional"
            and c.direction == direction
            and _layout_context(containers, c)[0].logical_id == entry.logical_id
        ),
        None,
    )
    if found:
        return model, found
    key = logical_id(model.project_id, "entrypoint/pkpd/" + direction)
    expression = (
        'НаправлениеОбмена = "' + ("Отправка" if direction == "send" else "Получение") + '"'
    )
    container = LayoutContainer(
        key, "conditional", expression, owner_id=entry.logical_id, direction=direction
    )
    # Блоки направлений предшествуют правилам обоих направлений (W:1200–1229).
    position = next(
        (
            n
            for n, e in enumerate(entry.elements)
            if e.container_id in {r.logical_id for r in model.pkpd}
        ),
        len(entry.elements),
    )
    entry = replace(
        entry,
        elements=(
            *entry.elements[:position],
            LayoutElement(key, "container", container_id=key),
            *entry.elements[position:],
        ),
    )
    model = replace(
        model,
        layouts=(
            *(entry if c.logical_id == entry.logical_id else c for c in model.layouts),
            container,
        ),
        guards=(
            *model.guards,
            Guard(
                logical_id=key,
                name="direction",
                expression=expression,
                branch="if",
                guard_kind="direction",
                direction=direction,
            ),
        ),
    )
    return model, container


def _mapping_containers(model: ManagerModel, rule: PredefinedRule) -> ManagerModel:
    containers = {c.logical_id: c for c in model.layouts}
    owner = containers[rule.logical_id]
    directions = ("send", "receive") if "both" in rule.directions else rule.directions
    elements = list(owner.elements)
    for direction in directions:
        if any(
            containers[e.container_id].kind == "values"
            and containers[e.container_id].direction == direction
            for e in elements
            if e.container_id
        ):
            continue
        key = logical_id(model.project_id, rule.logical_id + "/values/" + direction)
        containers[key] = LayoutContainer(
            key, "values", direction, owner_id=rule.logical_id, direction=direction
        )
        position = (
            next(
                (
                    n
                    for n, e in enumerate(elements)
                    if e.container_id and containers[e.container_id].direction == "receive"
                ),
                len(elements),
            )
            if direction == "send"
            else len(elements)
        )
        elements.insert(position, LayoutElement(key, "container", container_id=key))
    containers[owner.logical_id] = replace(owner, elements=tuple(elements))
    return replace(model, layouts=tuple(containers.values()))


def _declarative_layout(
    before: ManagerModel,
    model: ManagerModel,
    op: ManagerOperation,
    key: str,
    owner: PredefinedRule | None,
) -> ManagerModel:
    current = next(
        (
            (c, e)
            for c in model.layouts
            for e in c.elements
            if e.entity_id == key or e.container_id == key
        ),
        None,
    )
    if op.action == "update" and not (
        (op.kind == "pkpd" and "directions" in _updates(op.patch))
        or (op.kind == "value_mapping" and "direction" in _updates(op.patch))
    ):
        if op.kind == "pkpd":
            rule = next(r for r in model.pkpd if r.logical_id == key)
            model = replace(
                model,
                layouts=tuple(
                    replace(c, name=rule.name) if c.logical_id == key else c for c in model.layouts
                ),
            )
        return model
    containers = {c.logical_id: c for c in model.layouts}
    if current:
        c, element = current
        containers[c.logical_id] = replace(
            c, elements=tuple(e for e in c.elements if e.logical_id != element.logical_id)
        )
    else:
        element = (
            LayoutElement(key, "container", container_id=key)
            if op.kind == "pkpd"
            else LayoutElement(key, "entity", entity_id=key)
        )
    if op.action == "delete":
        removed, removed_blocks = set(), set()

        def discard(container_id):
            removed.add(container_id)
            c = containers.pop(container_id)
            for row in c.elements:
                if row.container_id:
                    discard(row.container_id)
                if row.block_id:
                    removed_blocks.add(row.block_id)

        if key in containers:
            discard(key)
        return _prune_generated_conditionals(
            replace(
                model,
                layouts=tuple(containers.values()),
                retained_blocks=tuple(
                    b for b in model.retained_blocks if b.logical_id not in removed_blocks
                ),
            ),
            {current[0].logical_id} if current else set(),
        )
    model = replace(model, layouts=tuple(containers.values()))
    if op.kind == "pkpd":
        rule = next(r for r in model.pkpd if r.logical_id == key)
        if key not in containers:
            model = replace(
                model, layouts=(*model.layouts, LayoutContainer(key, "predefined", rule.name))
            )
        model, destination = _pkpd_destination(model, rule)
        containers = {c.logical_id: c for c in model.layouts}
        container = containers[key]
        if current and current[0].logical_id != destination.logical_id:

            def moved(container_id):
                child = containers[container_id]
                for row in child.elements:
                    if row.container_id:
                        moved(row.container_id)
                containers[container_id] = replace(
                    child,
                    opening=replace(child.opening, fingerprint="") if child.opening else None,
                    closing=replace(child.closing, fingerprint="") if child.closing else None,
                    elements=tuple(
                        replace(
                            row, source=replace(row.source, fingerprint="", container_id="moved")
                        )
                        if row.source and not row.block_id
                        else row
                        for row in child.elements
                    ),
                )

            moved(key)
            container = containers[key]
        containers[key] = replace(container, name=rule.name, owner_id=destination.logical_id)
        guards = _layout_context(containers, destination)[1]
        model = replace(
            model,
            layouts=tuple(containers.values()),
            pkpd=tuple(
                replace(
                    r, guards=guards, mappings=tuple(replace(v, guards=guards) for v in r.mappings)
                )
                if r.logical_id == key
                else r
                for r in model.pkpd
            ),
        )
        model = _mapping_containers(model, next(r for r in model.pkpd if r.logical_id == key))
        destination = next(c for c in model.layouts if c.logical_id == destination.logical_id)
    elif op.kind == "value_mapping":
        assert owner is not None
        model = _mapping_containers(model, owner)
        item = next(v for v in owner.mappings if v.logical_id == key)
        destination = next(
            c
            for c in model.layouts
            if c.kind == "values"
            and c.owner_id == owner.logical_id
            and c.direction == item.direction
        )
    else:
        destination = _declarative_entry(model, "parameter")
    if op.container_id and op.container_id != destination.logical_id:
        _fail(
            "model_invalid", "Конвертация", "Позиция принадлежит другому направлению или владельцу"
        )
    rows = destination.elements
    if op.after_id:
        position = next(
            (
                n + 1
                for n, e in enumerate(rows)
                if op.after_id in (e.logical_id, e.entity_id, e.container_id)
            ),
            -1,
        )
        if position < 0:
            _fail("model_invalid", "Конвертация", "Сосед отсутствует в выбранном контейнере")
    elif op.after_id is None and op.position_mode == "explicit":
        position = 0
    elif op.kind == "pkpd" and model.source_files:
        names = {r.logical_id: r.name.casefold().replace("ё", "е") for r in model.pkpd}
        sort_key = names[key]
        position = next(
            (
                n
                for n, e in enumerate(rows)
                if e.container_id in names and names[e.container_id] > sort_key
            ),
            len(rows),
        )
    else:
        position = len(rows)
    destination = replace(destination, elements=(*rows[:position], element, *rows[position:]))
    return _prune_generated_conditionals(
        replace(
            model,
            layouts=tuple(
                destination if c.logical_id == destination.logical_id else c for c in model.layouts
            ),
        ),
        {current[0].logical_id} if current else set(),
    )


def _prune_generated_conditionals(
    model: ManagerModel, emptied: set[str] | None = None
) -> ManagerModel:
    """Снимает опустевшую охрану после удаления/перемещения последней декларации."""
    blocks = {b.logical_id: b for b in model.retained_blocks}
    emptied = emptied or set()
    removed = {
        c.logical_id
        for c in model.layouts
        if c.kind == "conditional"
        and c.branch != "chain"
        and (c.opening is None or c.logical_id in emptied)
        and all(e.block_id and not blocks[e.block_id].text.strip() for e in c.elements)
    }
    if not removed:
        return model
    removed_blocks = {
        e.block_id for c in model.layouts if c.logical_id in removed for e in c.elements
    }
    containers = {
        c.logical_id: replace(
            c, elements=tuple(e for e in c.elements if e.container_id not in removed)
        )
        for c in model.layouts
        if c.logical_id not in removed
    }
    changed_branches = set()
    for chain in tuple(containers.values()):
        if chain.kind != "conditional" or chain.branch != "chain":
            continue
        if not chain.elements:
            removed.add(chain.logical_id)
            containers.pop(chain.logical_id)
        else:
            first_id = chain.elements[0].container_id
            assert first_id is not None
            first = containers[first_id]
            if first.branch != "if":
                containers[first.logical_id] = replace(first, branch="if")
                changed_branches.add(first.logical_id)
    return replace(
        model,
        layouts=tuple(
            replace(c, elements=tuple(e for e in c.elements if e.container_id not in removed))
            for c in containers.values()
            if c.logical_id not in removed
        ),
        retained_blocks=tuple(
            b for b in model.retained_blocks if b.logical_id not in removed_blocks
        ),
        guards=tuple(
            replace(g, branch="if") if g.logical_id in changed_branches else g
            for g in model.guards
            if g.logical_id not in removed
        ),
    )


def _mapping_key(item: ValueMapping):
    """Имена ссылок BSL не различают регистр; строковый ключ формата остаётся литералом."""
    return (
        tuple(part.casefold() for part in item.configuration_value.reference_parts)
        if item.direction == "send"
        else item.format_value.value
    )


def _apply_declarative(model: ManagerModel, op: ManagerOperation) -> tuple[ManagerModel, str]:
    addresses = model_addresses(model)
    address = addresses.get(op.target_id or "", "Конвертация")
    updates = _updates(op.patch)
    _validate_patch(op.kind, updates, address)
    if op.kind in ("pkpd", "parameter") and op.action == "move":
        _fail("unsupported_form", address, "Для этого вида поддержаны create/update/delete")
    if set(op.clear) - ({"default"} if op.kind == "parameter" else set()) or set(op.clear) & set(
        updates
    ):
        _fail("model_invalid", address, "Некорректный список очистки полей")
    if op.clear:
        updates["default"] = Value()
    owner = None
    if op.kind == "value_mapping":
        owner = next(
            (
                r
                for r in model.pkpd
                if r.logical_id == op.owner_id
                or any(v.logical_id == op.target_id for v in r.mappings)
            ),
            None,
        )
        if owner is None:
            _fail("model_invalid", address, "Требуется владелец ПКПД")
        if owner.state != "editable" or owner.inside_leaf_id:
            _fail("opaque_context_changed", address, "ПКПД сохранено целиком")
        items = owner.mappings
    else:
        if op.owner_id:
            _fail("model_invalid", address, "Этот вид не принимает владельца")
        items = model.pkpd if op.kind == "pkpd" else model.parameters
    current = next((i for i in items if i.logical_id == op.target_id), None)
    if op.action == "create":
        if op.target_id:
            _fail("model_invalid", address, "ID новой сущности определяется client_id")
        key = logical_id(model.project_id, op.client_id)
        if op.kind == "pkpd":
            if not {"name", "directions", "configuration_type", "format_type"} <= updates.keys():
                _fail("model_invalid", address, "ПКПД требует имя, направления и оба типа")
            item = PredefinedRule(logical_id=key, **updates)
        elif op.kind == "parameter":
            if "name" not in updates:
                _fail("model_invalid", address, "Параметр требует имя")
            item = Parameter(logical_id=key, **updates)
        else:
            if not {"direction", "configuration_value", "format_value"} <= updates.keys():
                _fail("model_invalid", address, "Пара требует направление и обе стороны")
            assert owner is not None
            item = ValueMapping(
                logical_id=key, name=str(len(items) + 1), guards=owner.guards, **updates
            )
        items = (*items, item)
    else:
        if current is None:
            _fail("model_invalid", address, "Цель отсутствует или имеет другой вид")
        if current.state != "editable" or current.inside_leaf_id:
            _fail("opaque_context_changed", address, "Сохранённую сущность нельзя изменять")
        key = current.logical_id
        item = replace(current, **updates)
        items = (
            tuple(i for i in items if i.logical_id != key)
            if op.action == "delete"
            else tuple(item if i.logical_id == key else i for i in items)
        )
    if op.action != "delete":
        if isinstance(item, PredefinedRule):
            if (
                not item.directions
                or item.configuration_type.state != "reference"
                or item.format_type.state != "string"
            ):
                _fail("model_invalid", address, "ПКПД требует направления и типы")
            parts = item.configuration_type.reference_parts
            collection = "Перечисления" if item.data_kind == "enumeration" else "Справочники"
            if (
                len(parts) != 3
                or parts[:2] != ("Метаданные", collection)
                or not all(_identifier(p) for p in parts)
            ):
                _fail("model_invalid", address, "Тип ПКПД не соответствует виду данных")
        if isinstance(item, Parameter):
            if item.default.state == "unknown":
                _fail("model_invalid", address, "Значение параметра должно быть типизированным")
            if item.default.state == "reference" and (
                not item.default.reference_parts
                or item.default.reference_parts[0]
                not in ("Метаданные", "Перечисления", "Справочники")
                or not all(_identifier(part) for part in item.default.reference_parts)
            ):
                _fail("model_invalid", address, "Некорректное имя в значении параметра")
            literal(item.default)
            item = replace(
                item, default_source="implicit" if item.default.state == "unset" else "explicit"
            )
            items = tuple(item if i.logical_id == key else i for i in items)
        if isinstance(item, ValueMapping):
            assert owner is not None
            if item.configuration_value.state != "reference" or item.format_value.state != "string":
                _fail("model_invalid", address, "Пара требует ссылку конфигурации и строку формата")
            literal(item.format_value)
            parts = item.configuration_value.reference_parts
            expected = owner.configuration_type.reference_parts[1:]
            if len(parts) != 3 or parts[:2] != expected or not all(_identifier(p) for p in parts):
                _fail("model_invalid", address, "Значение не принадлежит типу ПКПД")
            if item.direction not in owner.directions and "both" not in owner.directions:
                _fail("model_invalid", address, "Направление пары не разрешено ПКПД")
            key_value = _mapping_key(item)
            previous_key = _mapping_key(current) if isinstance(current, ValueMapping) else None
            if (
                not isinstance(current, ValueMapping)
                or previous_key != key_value
                or current.direction != item.direction
            ) and any(
                isinstance(v, ValueMapping)
                and v.logical_id != key
                and v.direction == item.direction
                and _mapping_key(v) == key_value
                for v in items
            ):
                _fail("model_invalid", address, "Повтор ключа пары в одном направлении")
    if op.kind == "value_mapping":
        assert owner is not None
        owner = replace(owner, mappings=items)
        changed = replace(
            model, pkpd=tuple(owner if r.logical_id == owner.logical_id else r for r in model.pkpd)
        )
    else:
        changed = replace(model, **{"pkpd" if op.kind == "pkpd" else "parameters": items})
    changed = _declarative_layout(model, changed, op, key, owner)
    if op.kind == "value_mapping":
        assert owner is not None
        order = changed.ordered_entity_ids(owner.logical_id)
        mappings = {v.logical_id: v for v in owner.mappings}
        ordered = tuple(
            replace(mappings[k], name=str(n))
            for n, k in enumerate((k for k in order if k in mappings), 1)
        )
        changed = replace(
            changed,
            pkpd=tuple(
                replace(r, mappings=ordered) if r.logical_id == owner.logical_id else r
                for r in changed.pkpd
            ),
        )
    return changed, key


def _rename_property_references(
    model: ManagerModel, key: str, name: str, old_name: str, property_ids: set[str] | None = None
) -> ManagerModel:
    target = next(r for r in (*model.pko, *model.pkpd) if r.logical_id == key)
    kind = "pko" if isinstance(target, ObjectRule) else "pkpd"
    edits = {}
    source_entries = {e.logical_id: e for e in model.source_map}
    blocks = {b.logical_id: b for b in model.retained_blocks}

    def update(prop):
        if property_ids is not None and prop.logical_id not in property_ids:
            return prop
        if property_ids is None and prop.conversion.target_id != key:
            ref = prop.conversion
            if ref.target_id or ref.name != old_name or ref.kind not in (kind, "conversion"):
                return prop
            # Новая ссылка по имени ещё не разрешена до конца пакета. При
            # неоднозначном прежнем имени переименование не выбирает цель за автора.
            if any(
                r.logical_id != key
                and r.name == old_name
                and (ref.kind == "conversion" or isinstance(r, type(target)))
                for r in (*model.pko, *model.pkpd)
            ):
                return prop
        if prop.state != "editable":
            # ПКТЧ остаётся W3; меняется только доказанная позиция имени ПКО в её ПКС.
            entry = source_entries.get(prop.logical_id)
            block_id = prop.inside_leaf_id or next(
                (
                    e.block_id
                    for c in model.layouts
                    for e in c.elements
                    if e.entity_id == prop.logical_id and e.block_id
                ),
                "",
            )
            block = blocks.get(block_id)
            if (
                kind != "pko"
                or entry is None
                or block is None
                or block.kind not in ("pktch", "pks")
            ):
                _fail(
                    "opaque_context_changed",
                    model_addresses(model)[prop.logical_id],
                    "Переименование затрагивает неподдержанную декларацию",
                )
            from kd2_rules_mcp.ed.lexer import split_arguments

            source_text = next(s for s in model.source_files if s.file_id == entry.file_id).text
            prefix = source_text[block.char_start : entry.char_start]
            line = prefix.count("\n")
            column = len(prefix.rsplit("\n", 1)[-1])
            rows = block.text.splitlines(keepends=True)
            offset = sum(len(row) for row in rows[:line]) + column
            tokens = tokenize(block.text[offset:])
            opening = next((n for n, t in enumerate(tokens) if t.value == "("), None)
            closing = None
            depth = 0
            if opening is not None:
                for n, t in enumerate(tokens[opening:], opening):
                    depth += 1 if t.value == "(" else -1 if t.value == ")" else 0
                    if depth == 0:
                        closing = n
                        break
            arguments = (
                split_arguments(tokens[opening + 1 : closing])
                if opening is not None and closing is not None
                else ()
            )
            if (
                len(arguments) < 5
                or len(arguments[4]) != 1
                or arguments[4][0].kind != "string"
                or arguments[4][0].value != old_name
            ):
                _fail(
                    "opaque_context_changed",
                    model_addresses(model)[prop.logical_id],
                    "Позиция ссылки ПКС не доказана",
                )
            token = arguments[4][0]
            edits.setdefault(block.logical_id, []).append(
                (offset + token.start, offset + token.end, literal(Value("string", name)))
            )
        changed = replace(
            prop,
            conversion=replace(
                prop.conversion, kind=kind, target_id=key, name=name, resolution="resolved"
            ),
        )
        if prop.state != "editable":
            # Старый помощник и ПКТЧ остаются сохранёнными: не нормализуем 0/пустые позиции.
            values = list(prop.argument_values)
            if len(values) > 3:
                values[3] = Value("string", name)
            return replace(changed, argument_values=tuple(values))
        return _property_arguments(changed, model_addresses(model)[prop.logical_id])

    changed = replace(
        model,
        pko=tuple(
            replace(
                r,
                properties=tuple(update(p) for p in r.properties),
                groups=tuple(
                    replace(g, properties=tuple(update(p) for p in g.properties)) for g in r.groups
                ),
            )
            for r in model.pko
        ),
    )
    if edits:
        for key, rows in edits.items():
            block = blocks[key]
            text = block.text
            for start, end, replacement in sorted(rows, reverse=True):
                text = text[:start] + replacement + text[end:]
            blocks[key] = replace(block, text=text, sha256=text_hash(text))
        changed = replace(changed, retained_blocks=tuple(blocks.values()))
    return changed


def _resolve_properties(before: ManagerModel, model: ManagerModel) -> ManagerModel:
    """Ссылки по имени разрешаются отдельно в каждом направлении итогового пакета."""

    def properties(rule):
        return (*rule.properties, *(p for g in rule.groups for p in g.properties))

    old = {p.logical_id: p for r in before.pko for p in properties(r)}
    known = {r.logical_id: r for r in (*model.pko, *model.pkpd)}
    old_known = {r.logical_id: r for r in (*before.pko, *before.pkpd)}
    guards = {g.logical_id: g for g in model.guards}
    names = {}
    for rule in known.values():
        names.setdefault(rule.name, []).append(rule)
    old_names = {}
    for rule in old_known.values():
        old_names.setdefault(rule.name, []).append(rule)
    addresses = model_addresses(model)

    def available(target, direction):
        return direction in target.directions or "both" in target.directions

    # Прежняя цель сохраняется по идентичности, а не выбирается заново среди
    # оставшихся одноимённых правил. Только одинаковое новое имя всех целей
    # можно записать одним строковым аргументом ПКС.
    edits = {}
    renamed_failures = []
    for rule in model.pko:
        for group_guards, props in [
            ((), rule.properties),
            *((g.guards, g.properties) for g in rule.groups),
        ]:
            for prop in props:
                previous = old.get(prop.logical_id)
                if previous is not None and prop.conversion != previous.conversion:
                    continue
                old_name = prop.conversion.name
                targets = old_names.get(old_name, [])
                if not any(
                    t.logical_id in known and t.name != known[t.logical_id].name for t in targets
                ):
                    continue
                required = property_directions(rule, prop, guards, group_guards)
                used = {t.logical_id for t in targets if any(available(t, d) for d in required)}
                renamed = {known[k].name for k in used if k in known}
                if len(renamed) != 1 or len(used) != sum(k in known for k in used):
                    renamed_failures.append(addresses[prop.logical_id])
                    continue
                if used:
                    target = known[next(iter(used))]
                    if target.name != old_name:
                        edits.setdefault((target.logical_id, target.name, old_name), set()).add(
                            prop.logical_id
                        )
    if renamed_failures:
        _fail(
            "dangling_reference",
            renamed_failures[0],
            "Переименование меняет цель ссылки ПКС в одном из направлений",
            tuple(renamed_failures),
        )
    for (key, name, old_name), ids in edits.items():
        model = _rename_property_references(model, key, name, old_name, ids)

    old_owners = {r.logical_id: r for r in before.pko}
    reference_failures = []

    def resolve(rule, prop, group_guards=()):
        ref = prop.conversion
        previous = old.get(prop.logical_id)
        if not ref.target_id and not ref.name:
            return prop
        # logical_id задаёт цель черновой операции; сериализуемое имя затем
        # обязательно проверяется против всех правил, включая одноимённые.
        name = ref.name or (known[ref.target_id].name if ref.target_id in known else "")
        matches = names.get(name, [])
        required = property_directions(rule, prop, guards, group_guards)
        if (
            previous is not None
            and ref == previous.conversion
            and any(
                t.logical_id not in known and any(available(t, d) for d in required)
                for t in old_names.get(ref.name, [])
            )
        ):
            _fail(
                "dangling_reference",
                addresses[prop.logical_id],
                "Удалённая цель ПКС не заменяется автоматически одноимённым правилом",
                (addresses[prop.logical_id],),
            )
        old_rule = old_owners.get(rule.logical_id)
        changed = (
            previous is None
            or previous != prop
            or old_rule is None
            or old_rule.directions != rule.directions
            or any(
                t.logical_id not in old_known or t.directions != old_known[t.logical_id].directions
                for t in matches
            )
            or any(
                t.logical_id not in known
                or known[t.logical_id].name != t.name
                or known[t.logical_id].directions != t.directions
                for t in old_names.get(previous.conversion.name if previous else "", [])
            )
        )
        if not matches and previous is not None and not changed:
            return prop
        selected = [t for d in required for t in matches if available(t, d)]
        if any(sum(available(t, d) for t in matches) != 1 for d in required) or not matches:
            if changed:
                _fail(
                    "dangling_reference",
                    addresses[prop.logical_id],
                    "Правило ПКС отсутствует или неоднозначно в нужном направлении",
                    (addresses[prop.logical_id],),
                )
            return prop
        unique = {t.logical_id: t for t in selected or matches}
        kinds = {"pko" if isinstance(t, ObjectRule) else "pkpd" for t in unique.values()}
        if len(kinds) != 1 or ref.kind not in (*kinds, "conversion"):
            if not changed:
                return prop
            _fail(
                "dangling_reference",
                addresses[prop.logical_id],
                "Ссылка ПКС имеет другой вид правила",
                (addresses[prop.logical_id],),
            )
        kind = next(iter(kinds))
        new = Reference(
            kind if len(matches) == 1 else "conversion",
            matches[0].logical_id if len(matches) == 1 else None,
            name,
            "resolved" if len(matches) == 1 else "ambiguous",
        )
        # Нетронутая неоднозначная/неразрешённая связь остаётся точным импортом.
        if previous is not None and not changed:
            return prop
        return (
            prop
            if new == ref
            else replace(prop, conversion=new)
            if prop.state != "editable" or new.name == ref.name
            else _property_arguments(replace(prop, conversion=new), addresses[prop.logical_id])
        )

    def checked(rule, prop, group_guards=()):
        try:
            return resolve(rule, prop, group_guards)
        except ManagerOperationError as error:
            reference_failures.extend(error.failures)
            return prop

    rules = []
    for rule in model.pko:
        props = tuple(checked(rule, p) for p in rule.properties)
        groups = tuple(
            replace(g, properties=tuple(checked(rule, p, g.guards) for p in g.properties))
            for g in rule.groups
        )
        rules.append(
            rule
            if props == rule.properties and groups == rule.groups
            else replace(rule, properties=props, groups=groups)
        )
    if reference_failures:
        refs = tuple(dict.fromkeys(ref for f in reference_failures for ref in f.references))
        _fail(
            "dangling_reference",
            reference_failures[0].address,
            "Правила ПКС отсутствуют или неоднозначны в нужном направлении",
            refs,
        )
    resolved = (
        model
        if all(a is b for a, b in zip(rules, model.pko, strict=True))
        else replace(model, pko=tuple(rules))
    )
    # Изменение однозначности после переименования другого, одноимённого ПКО
    # не меняет оператор ПКС. Сохраняем его исходный отпечаток и точные пробелы.
    metadata_only = {
        p.logical_id
        for r in resolved.pko
        for p in properties(r)
        if p.logical_id in old
        and p != old[p.logical_id]
        and replace(p, conversion=old[p.logical_id].conversion) == old[p.logical_id]
        and p.conversion.name == old[p.logical_id].conversion.name
    }
    if metadata_only:
        members = {m.logical_id: m for m in resolved.members()}
        previous_members = {m.logical_id: m for m in before.members()}
        resolved = replace(
            resolved,
            layouts=tuple(
                replace(
                    c,
                    elements=tuple(
                        replace(
                            e,
                            source=replace(
                                e.source, fingerprint=leaf_fingerprint(resolved, e, members)
                            ),
                        )
                        if e.entity_id in metadata_only
                        and e.source
                        and e.source.fingerprint == leaf_fingerprint(before, e, previous_members)
                        else e
                        for e in c.elements
                    ),
                )
                for c in resolved.layouts
            ),
        )
    return resolved


def _validate_predefined_changes(before: ManagerModel, model: ManagerModel) -> None:
    """Тип и направление сверяются после пакета, включая одновременную замену пар."""
    previous = {r.logical_id: r for r in before.pkpd}
    addresses = model_addresses(model)
    for rule in model.pkpd:
        old = previous.get(rule.logical_id)
        if old is rule:
            continue
        old_values = {v.logical_id: v for v in old.mappings} if old else {}
        for value in rule.mappings:
            if (
                old
                and old.configuration_type == rule.configuration_type
                and old.directions == rule.directions
                and old_values.get(value.logical_id) == value
            ):
                continue
            if value.configuration_value.reference_parts[
                :2
            ] != rule.configuration_type.reference_parts[1:] or not _direction_allowed(
                rule.directions, value.direction
            ):
                _fail(
                    "model_invalid",
                    addresses[value.logical_id],
                    "Существующая пара не соответствует типу или направлению ПКПД",
                )


def _direction_allowed(directions, direction):
    return direction in directions or "both" in directions


def _matches_decision(decision: Decision, op: ManagerOperation, fingerprint: str | None = None):
    """Снимки прежнего B1 не включали необязательный способ выбора позиции."""
    if decision.operation_hash == (fingerprint or digest(op)):
        return True
    if decision.position_container is not None:
        return False
    legacy = json_value(op)
    legacy.pop("position_mode")
    return decision.operation_hash == digest(legacy)


def _code_name_references(model: ManagerModel, name: str, kind: str) -> tuple[str, ...]:
    addresses = model_addresses(model)
    result = []
    for owner_id, body in code_texts(model):
        tokens = [t for t in tokenize(body) if t.kind != "comment"]
        for n, token in enumerate(tokens):
            if token.value.casefold() != name.casefold():
                continue
            if token.kind == "string" or (
                kind == "algorithm"
                and token.kind == "identifier"
                and n + 1 < len(tokens)
                and tokens[n + 1].value == "("
                and (not n or tokens[n - 1].value != ".")
            ):
                result.append(addresses[owner_id] + ":" + str(body.count("\n", 0, token.start) + 1))
    return tuple(dict.fromkeys(result))


def _handler_usage_references(
    model: ManagerModel, unit_ids: set[str], removed_events: set[str]
) -> tuple[str, ...]:
    """Внешние привязки и лексические упоминания удаляемых обработчиков."""
    names = {
        u.name.casefold()
        for u in model.code_units
        if u.logical_id in unit_ids and "handler" in u.roles
    }
    if not names:
        return ()
    addresses = model_addresses(model)
    refs = [
        addresses[e.logical_id]
        for rule in (*model.pko, *model.pod)
        for e in rule.events
        if e.logical_id not in removed_events and e.target.target_id in unit_ids
    ]
    for owner_id, body in code_texts(model):
        if owner_id in unit_ids:
            continue
        folded_body = body.casefold()
        if not any(name in folded_body for name in names):
            continue
        tokens = [t for t in tokenize(body) if t.kind != "comment"]
        for n, token in enumerate(tokens):
            if token.folded in names and (
                token.kind == "string"
                or (
                    token.kind == "identifier"
                    and n + 1 < len(tokens)
                    and tokens[n + 1].value == "("
                    and (not n or tokens[n - 1].value != ".")
                )
            ):
                refs.append(addresses[owner_id] + f":{body.count(chr(10), 0, token.start) + 1}")
    return tuple(dict.fromkeys(refs))


def _call_shape(body: str, start: int) -> tuple[int | None, bool]:
    """Только скобки и границы оператора: смысл выражения не разбирается."""
    tokens = tuple(t for t in tokenize(body) if t.kind != "comment")
    index = next(n for n, t in enumerate(tokens) if t.start == start)
    previous = tokens[index - 1] if index else None
    expression = previous is not None and (
        previous.value in ("=", "(", ",", "+", "-", "*", "/", "<", ">", "[", "<>")
        or previous.folded in ("возврат", "если", "иначеесли", "пока", "не", "и", "или")
    )
    depth = 1
    for closing in range(index + 2, len(tokens)):
        if tokens[closing].kind == "symbol":
            depth += (tokens[closing].value == "(") - (tokens[closing].value == ")")
        if not depth:
            arguments = tokens[index + 2 : closing]
            if closing + 1 < len(tokens) and tokens[closing + 1].value in (
                "+",
                "-",
                "*",
                "/",
                "=",
                "<",
                ">",
                "<>",
                ".",
                "[",
            ):
                expression = True
            try:
                return len(split_arguments(arguments)) if arguments else 0, expression
            except EdFormatError:
                # Несбалансированный сложный вызов не доказывает число аргументов.
                return None, expression
    return None, expression


def _code_change_notices(
    before: ManagerModel, after: ManagerModel, op: ManagerOperation
) -> tuple[ManagerNotice, ...]:
    """Перепривязка и явное восстановление ветки требуют видимого решения."""
    if op.kind in ("pko", "pod") and op.action == "delete":
        owner = next((r for r in (*before.pko, *before.pod) if r.logical_id == op.target_id), None)
        if owner is None:
            return ()
        remaining_names = {
            e.target.name.casefold() for r in (*after.pko, *after.pod) for e in r.events
        }
        names = {
            e.target.name.casefold()
            for e in owner.events
            if e.target.resolution == "ambiguous"
            and e.target.name.casefold() not in remaining_names
        }
        members: list[CodeUnit | DispatcherCase] = [
            u for u in after.code_units if u.name.casefold() in names
        ]
        members += [
            c
            for c in after.dispatcher_cases
            if c.target.name.casefold() in names or c.name.casefold() in names
        ]
        if not members:
            return ()
        addresses = model_addresses(after)
        sources = {s.logical_id: s for s in after.source_map}
        refs = tuple(
            addresses[m.logical_id]
            + (f":{sources[m.logical_id].line_start}" if m.logical_id in sources else "")
            for m in members
        )
        return (
            ManagerNotice(
                "orphan_handler",
                model_addresses(before)[owner.logical_id],
                "После удаления правила останется код, который больше ни к чему не привязан: "
                "неоднозначные методы и ветки сохранены",
                refs,
                digest((op, "orphan_handler", refs, before.revision)),
            ),
        )
    if op.kind != "handler" or op.action != "update":
        return ()
    updates = _updates(op.patch)
    addresses = model_addresses(before)
    result = []
    old_bound = {e.target.target_id for r in (*before.pko, *before.pod) for e in r.events}
    new_bound = {e.target.target_id for r in (*after.pko, *after.pod) for e in r.events}
    for unit in after.code_units:
        if "handler" in unit.roles and unit.logical_id in old_bound - new_bound:
            refs = (addresses[unit.logical_id],)
            result.append(
                ManagerNotice(
                    "orphan_handler",
                    refs[0],
                    "После перепривязки метод остался без привязки",
                    refs,
                    digest((op, "orphan_handler", refs, before.revision)),
                )
            )
    if updates.get("restore_dispatcher"):
        old_cases = {c.logical_id: c for c in before.dispatcher_cases}
        changed = [c for c in after.dispatcher_cases if old_cases.get(c.logical_id) != c]
        if changed:
            address = addresses.get(op.target_id or "", "Конвертация")
            result.append(
                ManagerNotice(
                    "handler_execution_changed",
                    address,
                    "Восстановление ветки: обработчик начнёт выполняться",
                    (address,),
                    digest((op, "handler_execution_changed", before.revision)),
                )
            )
    return tuple(result)


def _code_notices(model: ManagerModel, op: ManagerOperation) -> tuple[ManagerNotice, ...]:
    if op.kind not in ("pko", "pod", "algorithm", "parameter") or op.action not in (
        "update",
        "delete",
    ):
        return ()
    member = next((m for m in model.members() if m.logical_id == op.target_id), None)
    updates = _updates(op.patch)
    if member is None:
        return ()
    addresses = model_addresses(model)
    address = addresses[member.logical_id]
    result = []

    def notice(code, message, refs):
        result.append(
            ManagerNotice(code, address, message, refs, digest((op, code, refs, model.revision)))
        )

    if (
        op.kind == "algorithm"
        and op.action == "update"
        and isinstance(member, CodeUnit)
        and set(updates) & {"parameters", "routine_kind"}
    ):
        parameters = updates.get("parameters", member.parameters_text or "")
        kind = updates.get("routine_kind", member.signature.routine_kind)
        signature = _code_signature(member.name, kind, parameters, member.signature.exported)
        if (
            signature.parameters != member.signature.parameters
            or kind != member.signature.routine_kind
        ):
            calls = [
                o
                for o in code_occurrences(model)
                if o.target_id == member.logical_id and o.kind == "algorithm_call"
            ]
            refs, details = [], []
            bodies = dict(code_texts(model))
            minimum = max(
                (n + 1 for n, p in enumerate(signature.parameters) if p.default.state == "unset"),
                default=0,
            )
            maximum = len(signature.parameters)
            for call in calls:
                ref = addresses[call.owner_id] + f":{call.line}"
                refs.append(ref)
                count, expression = _call_shape(bodies[call.owner_id], call.start)
                if count is not None and not minimum <= count <= maximum:
                    details.append(
                        f"{ref}: {count} аргументов, новая сигнатура принимает {minimum}–{maximum}"
                    )
                if (
                    member.signature.routine_kind == "function"
                    and kind == "procedure"
                    and expression
                ):
                    details.append(f"{ref}: вызов функции в выражении заменяется процедурой")
            for case in model.dispatcher_cases:
                if case.target.target_id == member.logical_id:
                    source = next(
                        (e for e in model.source_map if e.logical_id == case.logical_id), None
                    )
                    refs.append(
                        addresses[case.logical_id] + f":{source.line_start if source else 1}"
                    )
                    ref = refs[-1]
                    if not minimum <= len(case.arguments) <= maximum:
                        details.append(
                            f"{ref}: {len(case.arguments)} аргументов ветки, "
                            f"новая сигнатура принимает {minimum}–{maximum}"
                        )
                    if case.returns and kind == "procedure":
                        details.append(f"{ref}: ветка возвращает вызов функции в выражении")
            if refs:
                notice(
                    "algorithm_signature_calls",
                    "Сигнатура алгоритма изменяется при существующих вызовах; "
                    "проверьте места вызова" + ("; " + "; ".join(details) if details else ""),
                    tuple(dict.fromkeys(refs)),
                )
    if op.action != "delete" and updates.get("name", member.name) == member.name:
        return tuple(result)

    if op.kind == "parameter":
        refs = tuple(
            addresses[key] + f":{line}"
            for key, body in code_texts(model)
            for name, line in parameter_accesses(body)
            if name is None
        )
        if refs:
            notice(
                "computed_dependencies",
                "Проверьте вычисляемые обращения к параметрам конвертации",
                refs,
            )
        return tuple(result)

    if op.action == "update":
        refs = tuple(
            addresses[o.owner_id] + f":{o.line}"
            for o in code_occurrences(model)
            if o.target_id == member.logical_id and o.kind in ("rule_literal", "algorithm_literal")
        )
        if refs:
            notice(
                "code_name_literals",
                "Литералы имени в телах не переписаны; проверьте перечисленные строки тела",
                refs,
            )
    if op.kind == "algorithm":
        if any(u.body.strip() for u in model.code_units if "dispatcher" not in u.roles):
            notice(
                "computed_algorithm_calls",
                "Проверьте вычисляемые вызовы алгоритмов: "
                "лексический индекс не устанавливает их цели",
                (),
            )
    else:
        if op.action == "update" and isinstance(member, ObjectRule | ProcessingRule):
            handler_ids = {
                e.target.target_id
                for e in member.events
                if e.event != _DEFERRED and e.target.target_id is not None
            }
            refs = _handler_usage_references(
                model, handler_ids, {e.logical_id for e in member.events}
            )
            if refs:
                notice(
                    "code_handler_references",
                    "Упоминания прежних имён обработчиков в телах не переписаны; проверьте строки",
                    refs,
                )
        refs = tuple(
            addresses[u.logical_id]
            for u in model.code_units
            if any(
                r.resolution == "computed"
                and r.kind in ("pko_lookup", "instruction_rule", "pko", "conversion")
                for r in u.dependencies
            )
        )
        if refs:
            notice(
                "computed_dependencies",
                "Проверьте обработчики с вычисляемым именем правила"
                + (
                    f": {len(refs)} обработчиков; имена могут зависеть от входных данных"
                    if len(refs) > 10
                    else ""
                ),
                refs if len(refs) <= 10 else (),
            )
    return tuple(result)


def _validate_parameter_changes(
    before: ManagerModel, model: ManagerModel, history: list[Parameter]
) -> ManagerModel:
    current = {p.logical_id: p for p in model.parameters}
    changed = [
        p
        for p in before.parameters
        if p.logical_id not in current or current[p.logical_id].name != p.name
    ]
    changed.extend(
        p for p in history if p.logical_id not in current or current[p.logical_id].name != p.name
    )
    if not changed and tuple((p.logical_id, p.name) for p in before.parameters) == tuple(
        (p.logical_id, p.name) for p in model.parameters
    ):
        return model

    addresses = model_addresses(model)
    accesses = [
        (key, name, line)
        for key, body in code_texts(model)
        for name, line in parameter_accesses(body)
    ]
    for parameter in changed:
        refs = tuple(
            addresses[key] + f":{line}"
            for key, name, line in accesses
            if name is not None and name.casefold() == parameter.name.casefold()
        )
        if refs:
            _fail(
                "dangling_reference",
                model_addresses(before).get(parameter.logical_id, "Параметр/" + parameter.name),
                "Параметр используется в коде; измените обращения явно тем же пакетом",
                refs,
            )

    def refresh(unit):
        deps = tuple(
            r for r in unit.dependencies if r.kind != "parameter"
        ) + parameter_dependencies(unit.body, model.parameters)
        return unit if deps == unit.dependencies else replace(unit, dependencies=deps)

    return replace(model, code_units=tuple(refresh(u) for u in model.code_units))


def _validate_code_directions(
    before: ManagerModel, model: ManagerModel
) -> tuple[ManagerNotice, ...]:
    """Проверяет только изменённые зависимости пакета, сохраняя дефекты импорта."""

    def context(manager):
        return (
            tuple(
                (
                    r.logical_id,
                    r.directions,
                    r.guards,
                    tuple((e.event, e.target.target_id, e.guards) for e in r.events),
                )
                for r in (*manager.pko, *manager.pod)
            ),
            tuple((r.logical_id, r.directions) for r in manager.pkpd),
            tuple((e.event, e.target.target_id, e.guards) for e in manager.conversion_events),
            tuple(
                (u.logical_id, u.sha256, u.guards)
                for u in manager.code_units
                if "dispatcher" not in u.roles
            ),
        )

    if context(before) == context(model):
        return ()
    old_rules = {r.logical_id: r for r in (*before.pko, *before.pkpd)}
    rules = {r.logical_id: r for r in (*model.pko, *model.pkpd)}
    old_refs = {
        (key, name, line): directions
        for key, name, line, directions in code_rule_references(before)
    }
    addresses = model_addresses(model)
    failures, uncertain = [], []
    rules_changed = any(
        key not in rules or rules[key].directions != r.directions for key, r in old_rules.items()
    )
    for key, name, line, directions in code_rule_references(model):
        previous = old_refs.get((key, name, line))
        place = addresses[key] + f":{line}"
        if name is None or not directions:
            if rules_changed or (previous is None or previous != directions):
                uncertain.append(place)
            continue
        old_targets = [r for r in old_rules.values() if r.name == name]
        targets = [r for r in rules.values() if r.name == name]
        if not targets and any(r.name.casefold() == name.casefold() for r in rules.values()):
            # Несовпадение регистра — отдельная диагностика; тело не исправляем.
            continue
        missing = {
            d for d in directions if not any(_direction_allowed(r.directions, d) for r in targets)
        }
        old_missing = {
            d
            for d in previous or ()
            if not any(_direction_allowed(r.directions, d) for r in old_targets)
        }
        deleted_used = any(
            r.logical_id not in rules
            and any(_direction_allowed(r.directions, d) for d in directions)
            for r in old_targets
        )
        affected = (
            previous is None
            or previous != directions
            or any(
                r.logical_id not in rules or rules[r.logical_id].directions != r.directions
                for r in old_targets
            )
        )
        if affected and (missing - old_missing or (previous is None and missing) or deleted_used):
            failures.append(place)
    if failures:
        _fail(
            "dangling_reference",
            failures[0],
            "Правило инструкции в коде отсутствует в направлении события",
            tuple(dict.fromkeys(failures)),
        )
    if uncertain:
        refs = tuple(dict.fromkeys(uncertain))
        return (
            ManagerNotice(
                "computed_dependencies",
                refs[0],
                "Проверьте вычисляемые имена правил и инструкции "
                "с недоказанным направлением исполнения",
                refs,
                digest((refs, tuple(rules.values()), model.revision)),
            ),
        )
    return ()


def preview(
    model: ManagerModel, operations: tuple[ManagerOperation, ...], *, expected_revision: str
) -> ManagerPreview:
    validate_model(model)
    if expected_revision != model.revision or model.revision != content_hash(model):
        raise EdAuthoringStaleError("Базовая ревизия менеджера устарела")
    if len(operations) > MAX_OPERATIONS:
        raise EdAuthoringResourceLimitError("Пакет содержит больше 100 операций")
    result = model
    failures: list[ManagerFailure] = []
    canonical = []
    skipped = []
    deleted: list[tuple[str, str]] = []
    notices: list[ManagerNotice] = []
    deleted_names = {}
    parameter_history = []

    def failed_operation(operation, errors):
        failures.extend(
            replace(
                f,
                address=f"Операция/{operation.client_id}"
                if f.address == "Конвертация"
                else f.address,
                message=f"Операция {operation.client_id}: {f.message}",
            )
            for f in errors
        )
        canonical.append(CanonicalOperation(operation, digest(operation), ""))

    for op in operations:
        input_address = op.address
        try:
            op = _resolve_operation(result, op)
        except ManagerOperationError as error:
            failed_operation(op, error.failures)
            continue
        except (TypeError, ValueError) as error:
            failed_operation(
                op, (ManagerFailure("model_invalid", op.address or "Конвертация", str(error)),)
            )
            continue
        fingerprint = digest(op)
        previous = next((d for d in result.decisions if d.client_id == op.client_id), None)
        if previous:
            if not _matches_decision(previous, op, fingerprint):
                failures.append(
                    ManagerFailure(
                        "model_invalid",
                        op.address
                        or model_addresses(result).get(
                            op.target_id or "",
                            previous.address_aliases[-1]
                            if previous.address_aliases
                            else "Конвертация",
                        ),
                        "Конфликт содержимого client_id",
                    )
                )
                canonical.append(CanonicalOperation(op, fingerprint, previous.result_ids[0]))
            else:
                skipped.append(op.client_id)
                canonical.append(CanonicalOperation(op, fingerprint, previous.result_ids[0]))
            continue
        try:
            pending = None
            if op.kind == "pkpd" and op.action in ("delete", "update"):
                current = next(
                    (r for r in getattr(result, op.kind) if r.logical_id == op.target_id), None
                )
                updates = _updates(op.patch)
                if current and (
                    op.action == "delete" or ("name" in updates and updates["name"] != current.name)
                ):
                    addresses = model_addresses(result)
                    refs = tuple(
                        addresses[u.logical_id]
                        for u in result.code_units
                        if any(
                            (
                                r.resolution == "computed"
                                or (
                                    op.kind in ("pko", "pkpd") and r.target_id == current.logical_id
                                )
                                or (
                                    op.kind in ("pko", "pkpd")
                                    and r.name.casefold() == current.name.casefold()
                                )
                            )
                            and r.kind
                            in ("pko", "pkpd", "pko_lookup", "instruction_rule", "conversion")
                            for r in u.dependencies
                        )
                    )
                    if refs:
                        address = addresses[current.logical_id]
                        pending = ManagerNotice(
                            "computed_dependencies",
                            address,
                            (
                                "Проверьте обработчики с вычисляемым именем правила"
                                if any(
                                    r.resolution == "computed"
                                    for u in result.code_units
                                    for r in u.dependencies
                                    if r.kind
                                    in (
                                        "pko",
                                        "pkpd",
                                        "pko_lookup",
                                        "instruction_rule",
                                        "conversion",
                                    )
                                )
                                else "Проверьте ссылки на правило в сохранённом коде"
                            )
                            + (
                                f": {len(refs)} обработчиков; "
                                "имена могут зависеть от входных данных"
                                if len(refs) > 10
                                else ""
                            ),
                            refs if len(refs) <= 10 else (),
                            digest((op, refs, result.revision)),
                        )
            code_notices = _code_notices(result, op)
            changed, key = _apply_one(result, op)
            notices.extend(_code_change_notices(result, changed, op))
            if op.kind == "parameter" and op.action in ("delete", "update"):
                old_parameter = next((p for p in result.parameters if p.logical_id == key), None)
                if old_parameter and (
                    op.action == "delete"
                    or _updates(op.patch).get("name", old_parameter.name) != old_parameter.name
                ):
                    parameter_history.append(old_parameter)
            notices.extend(code_notices)
            if pending:
                notices.append(pending)
            address = model_addresses(result).get(key, op.address or "Конвертация")
            if op.action == "delete":
                deleted.append((key, address))
                member = next((m for m in result.members() if m.logical_id == key), None)
                if member and op.kind in ("pko", "pod", "algorithm"):
                    deleted_names[key] = (member.name, op.kind)
            resolved = replace(
                op,
                target_id=key if op.action != "create" and op.kind != "manager" else None,
                address=None,
            )
            canonical.append(CanonicalOperation(resolved, fingerprint, key))
            result = replace(
                changed,
                decisions=(
                    *result.decisions,
                    Decision(
                        op.client_id,
                        fingerprint,
                        (key,),
                        tuple(dict.fromkeys(a for a in (input_address, address) if a)),
                        op.container_id if op.action == "create" else None,
                        op.after_id if op.action == "create" else None,
                    ),
                ),
                confirmations=(),
            )
        except ManagerOperationError as error:
            failed_operation(op, error.failures)
        except (TypeError, ValueError) as error:
            failed_operation(
                op, (ManagerFailure("model_invalid", op.address or "Конвертация", str(error)),)
            )
    try:
        result = _resolve_properties(model, result)
        result = _validate_parameter_changes(model, result, parameter_history)
        _validate_predefined_changes(model, result)
        notices.extend(_validate_code_directions(model, result))
    except ManagerOperationError as error:
        failures.extend(error.failures)
    for key, address in deleted:
        same_name = key in deleted_names and any(
            r.name == deleted_names[key][0] for r in (*result.pko, *result.pkpd)
        )
        refs = references_to(result, key, ignore_code=same_name)
        if key in deleted_names:
            name, kind = deleted_names[key]
            if not same_name:
                refs += _code_name_references(result, name, kind)
        if refs:
            failures.append(
                ManagerFailure(
                    "dangling_reference", address, "Удаление не каскадное: остались ссылки", refs
                )
            )
    try:
        validate_model(result)
        old_pko = {r.logical_id: r for r in model.pko}
        old_pkpd = {r.logical_id: r for r in model.pkpd}
        for pko in result.pko:
            for pkpd in result.pkpd:
                if pko.name.casefold() != pkpd.name.casefold():
                    continue
                a, b = old_pko.get(pko.logical_id), old_pkpd.get(pkpd.logical_id)
                if a is None or b is None or a.name != pko.name or b.name != pkpd.name:
                    failures.append(
                        ManagerFailure(
                            "model_invalid",
                            model_addresses(result)[pko.logical_id],
                            "Имена ПКО и ПКПД находятся в одном пространстве",
                            (model_addresses(result)[pkpd.logical_id],),
                        )
                    )
        for kind in ("pko", "pod", "pkpd"):
            rows = getattr(result, kind)
            for n, row in enumerate(rows):
                for other in rows[n + 1 :]:
                    if row.name.casefold() != other.name.casefold():
                        continue
                    left = {"send", "receive"} if "both" in row.directions else set(row.directions)
                    right = (
                        {"send", "receive"} if "both" in other.directions else set(other.directions)
                    )
                    old_rows = {item.logical_id: item for item in getattr(model, kind)}
                    old_a, old_b = old_rows.get(row.logical_id), old_rows.get(other.logical_id)
                    unchanged = (
                        old_a is not None
                        and old_b is not None
                        and old_a.name == row.name
                        and old_b.name == other.name
                        and old_a.directions == row.directions
                        and old_b.directions == other.directions
                    )
                    if left & right and not unchanged:
                        failures.append(
                            ManagerFailure(
                                "model_invalid",
                                model_addresses(result)[row.logical_id],
                                "Имя правила уже занято в этом направлении",
                            )
                        )

        def method_groups(manager):
            groups = {}
            for member in (*manager.pko, *manager.pod, *manager.code_units):
                name = member.name if isinstance(member, CodeUnit) else member.procedure_name
                groups.setdefault(name.casefold(), set()).add(member.logical_id)
            return groups

        old_methods = method_groups(model)
        if any(
            len(ids) > 1 and not ids <= old_methods.get(name, set())
            for name, ids in method_groups(result).items()
        ):
            failures.append(
                ManagerFailure("model_invalid", "Конвертация", "Имя процедуры уже занято")
            )
        for n, parameter in enumerate(result.parameters):
            for other in result.parameters[n + 1 :]:
                if parameter.name.casefold() == other.name.casefold() and (
                    parameter not in model.parameters or other not in model.parameters
                ):
                    failures.append(
                        ManagerFailure(
                            "model_invalid",
                            model_addresses(result)[parameter.logical_id],
                            "Имя параметра уже занято",
                        )
                    )
        # Новые связи обязательно проверены; прежние неразрешённые связи импорта
        # остаются draft, а не объявляются исправленными.
        known = {m.logical_id for m in result.members()}
        old_refs = set(_references(model))
        for ref in _references(result):
            if ref not in old_refs and (
                (ref.target_id and ref.target_id not in known)
                or (ref.resolution == "resolved" and ref.target_id is None)
            ):
                failures.append(
                    ManagerFailure("dangling_reference", ref.name, "Новая ссылка не разрешена")
                )
    except ValueError as error:
        failures.append(ManagerFailure("model_invalid", "Конвертация", str(error)))
    result = (
        model
        if failures or result is model
        else replace(
            result, confirmations=tuple((n.code, n.notice_hash) for n in notices)
        ).with_revision()
    )
    changes = compare_models(model, result).changes
    plan_hash = digest(
        (expected_revision, canonical, result.revision, changes, failures, skipped, notices)
    )
    return ManagerPreview(
        expected_revision,
        tuple(canonical),
        result.revision,
        changes,
        tuple(failures),
        tuple(skipped),
        tuple(notices),
        plan_hash,
        result,
    )


def apply(
    model: ManagerModel,
    operations: tuple[ManagerOperation, ...],
    *,
    expected_revision: str,
    expected_preview_hash: str,
    confirmations: tuple[tuple[str, str], ...] = (),
) -> ManagerModel:
    # Повтор уже применённого решения не требует отката на прежнюю ревизию.
    if operations and all(
        any(d.client_id == op.client_id for d in model.decisions) for op in operations
    ):
        replayed = preview(model, operations, expected_revision=model.revision)
        if replayed.failures:
            raise ManagerOperationError(replayed.failures)
        return replayed.model
    planned = preview(model, operations, expected_revision=expected_revision)
    if planned.preview_hash != expected_preview_hash:
        raise EdAuthoringStaleError("Хеш просмотра менеджера не совпадает")
    if planned.failures:
        raise ManagerOperationError(planned.failures)
    missing = tuple(n for n in planned.notices if (n.code, n.notice_hash) not in confirmations)
    if missing:
        raise ManagerOperationError(
            tuple(
                ManagerFailure("confirmation_required", n.address, n.message, n.references)
                for n in missing
            )
        )
    return planned.model
