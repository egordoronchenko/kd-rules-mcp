"""Атомарные решения над полным менеджером; renderer и сервис не вызываются."""

from __future__ import annotations

import re
from dataclasses import dataclass, fields, replace
from typing import Any, Literal, NoReturn, get_args

from kd2_rules_mcp.ed.canonical import model_addresses
from kd2_rules_mcp.ed.diff import ManagerChange, compare_models
from kd2_rules_mcp.ed.forms import IDENTIFICATIONS, RULE_PARAMETERS
from kd2_rules_mcp.ed.writer_model import (
    Decision,
    Direction,
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
    ProcessingRule,
    Property,
    Reference,
    RetainedBlock,
    RuleUse,
    SearchSet,
    Signature,
    TextStyle,
    Value,
    content_hash,
    decode_dto,
    digest,
    json_bytes,
    json_value,
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


@dataclass(frozen=True, slots=True)
class IdentificationPatch:
    mode: Value | None = None
    search_sets: tuple[tuple[str, ...], ...] | None = None
    not_found_policy: Value | None = None


Patch = ManagerPatch | PodPatch | PkoPatch | PropertyPatch | IdentificationPatch
_PATCHES = {
    "manager": ManagerPatch,
    "pod": PodPatch,
    "pko": PkoPatch,
    "property": PropertyPatch,
    "identification": IdentificationPatch,
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
    after_id: str | None = None
    patch: Patch | None = None
    clear: tuple[str, ...] = ()
    unsupported_payload: str = ""
    position_mode: Literal["explicit", "default"] = "explicit"

    def __post_init__(self) -> None:
        if not isinstance(self.client_id, str) or not self.client_id or len(self.client_id) > 200:
            raise ValueError("Требуется client_id длиной 1–200")
        if self.kind not in get_args(OperationKind) or self.action not in get_args(Action):
            raise ValueError("Неизвестный вид или действие операции")
        if self.position_mode not in ("explicit", "default") or (
            self.position_mode == "default"
            and (self.kind not in ("pko", "pod") or self.action != "create")
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
    return dto


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
    if op.action == "create" and op.kind in ("pko", "pod") and op.container_id is None:
        previous = next((d for d in model.decisions if d.client_id == op.client_id), None)
        if previous is not None:
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
        op = replace(op, container_id=module.logical_id, after_id=anchor, position_mode="default")
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
        return replace(
            after,
            layouts=tuple(containers.values()),
            retained_blocks=tuple(b for b in after.retained_blocks if b.logical_id not in removed),
            guards=tuple(
                g
                for g in after.guards
                if g.logical_id not in removed_containers and g.inside_leaf_id not in removed
            ),
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
    model: ManagerModel, target_id: str, *, ignore_editable_pod: bool = False
) -> tuple[str, ...]:
    addresses = model_addresses(model)
    result = []
    target = next((m for m in model.members() if m.logical_id == target_id), None)
    for member in model.members():
        if member.logical_id == target_id or isinstance(member, RuleUse):
            continue
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
                in ("pko", "pod", "pko_lookup", "instruction_rule", "pod_use", "conversion")
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
        dependencies = references_to(model, key, ignore_editable_pod=op.action != "delete")
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
            item = Property(
                logical_id=key,
                name=updates["format_property"] or updates["configuration_property"],
                **updates,
            )
            values = (
                Value("string", item.configuration_property),
                Value("string", item.format_property),
            )
            presence = updates.get(
                "argument_presence",
                (True, True, True, False, False, True) if item.namespace else (True, True, True),
            )
            if len(presence) > 3:
                presence = (*presence[:3], False, *presence[4:])
                while len(presence) > 3 and not presence[-1]:
                    presence = presence[:-1]
            defaults = (
                Value("number", 0),
                Value("string", ""),
                Value("string", item.namespace),
                Value("string", ""),
            )
            values += tuple(
                defaults[n - 3] if flag else Value() for n, flag in enumerate(presence[3:], 3)
            )
            item = replace(item, argument_presence=presence)
            item = replace(item, argument_values=values)
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
        if op.kind == "pko" and (current.properties or current.groups):
            _fail(
                "dangling_reference",
                address,
                "Удаление не каскадное: остались дочерние свойства",
                tuple(addresses[p.logical_id] for p in (*current.properties, *current.groups)),
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
            values = list(current.argument_values)
            for n, field_name in enumerate(
                (
                    "configuration_property",
                    "format_property",
                    "algorithm_flag",
                    "conversion_rule",
                    "namespace",
                    "condition_name",
                )
            ):
                if field_name in updates and n < len(values):
                    values[n] = Value("string", updates[field_name])
                elif field_name == "namespace" and updates.get(field_name):
                    values.extend(Value() for _ in range(n + 1 - len(values)))
                    values[n] = Value("string", updates[field_name])
                    presence = list(updates.get("argument_presence", current.argument_presence))
                    presence.extend(False for _ in range(n + 2 - len(presence)))
                    presence[n + 1] = True
                    updates["argument_presence"] = tuple(presence)
            updates["argument_values"] = tuple(values)
            presence = updates.get("argument_presence", current.argument_presence)
            if len(presence) != len(values) + 1 or (
                updates.get("namespace", current.namespace)
                and (len(presence) < 6 or not presence[5])
            ):
                _fail("model_invalid", address, "Наличие аргументов несовместимо с полями ПКС")
        if "name" in updates and op.kind in ("pko", "pod"):
            updates["procedure_name"] = (
                "ДобавитьПКО_" if op.kind == "pko" else "ДобавитьПОД_"
            ) + updates["name"]
        item = replace(current, **updates)
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
    return result, key


def _matches_decision(decision: Decision, op: ManagerOperation, fingerprint: str | None = None):
    """Снимки прежнего B1 не включали необязательный способ выбора позиции."""
    if decision.operation_hash == (fingerprint or digest(op)):
        return True
    if decision.position_container is not None:
        return False
    legacy = json_value(op)
    legacy.pop("position_mode")
    return decision.operation_hash == digest(legacy)


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
    for op in operations:
        input_address = op.address
        try:
            op = _resolve_operation(result, op)
        except ManagerOperationError as error:
            failures.extend(error.failures)
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
            else:
                skipped.append(op.client_id)
                canonical.append(CanonicalOperation(op, fingerprint, previous.result_ids[0]))
            continue
        try:
            pending = None
            if op.kind in ("pko", "pod") and op.action in ("delete", "update"):
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
                            r.resolution == "computed"
                            and r.kind in ("pko", "pko_lookup", "instruction_rule", "conversion")
                            for r in u.dependencies
                        )
                    )
                    if refs:
                        address = addresses[current.logical_id]
                        pending = ManagerNotice(
                            "computed_dependencies",
                            address,
                            "Проверьте обработчики с вычисляемым именем правила"
                            + (
                                f": {len(refs)} обработчиков; "
                                "имена могут зависеть от входных данных"
                                if len(refs) > 10
                                else ""
                            ),
                            refs if len(refs) <= 10 else (),
                            digest((op, refs, result.revision)),
                        )
            changed, key = _apply_one(result, op)
            if pending:
                notices.append(pending)
            address = model_addresses(result).get(key, op.address or "Конвертация")
            if op.action == "delete":
                deleted.append((key, address))
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
            failures.extend(error.failures)
        except (TypeError, ValueError) as error:
            failures.append(
                ManagerFailure("model_invalid", op.address or "Конвертация", str(error))
            )
    for key, address in deleted:
        refs = references_to(result, key)
        if refs:
            failures.append(
                ManagerFailure(
                    "dangling_reference", address, "Удаление не каскадное: остались ссылки", refs
                )
            )
    try:
        validate_model(result)
        for kind in ("pko", "pod"):
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
        procedures = [r.procedure_name.casefold() for r in (*result.pko, *result.pod)]
        procedures.extend(u.name.casefold() for u in result.code_units)
        if len(procedures) != len(set(procedures)):
            failures.append(
                ManagerFailure("model_invalid", "Конвертация", "Имя процедуры уже занято")
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
        if failures
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
    resolved = tuple(_resolve_operation(model, op) for op in operations)
    addresses = model_addresses(model)
    conflicts = tuple(
        ManagerFailure(
            "model_invalid",
            op.address
            or addresses.get(
                op.target_id or "",
                decision.address_aliases[-1] if decision.address_aliases else "Конвертация",
            ),
            "Конфликт содержимого client_id",
        )
        for op in resolved
        for decision in model.decisions
        if decision.client_id == op.client_id and not _matches_decision(decision, op)
    )
    if conflicts:
        raise ManagerOperationError(conflicts)
    # Повтор уже применённого решения не требует отката на прежнюю ревизию.
    if operations and all(
        any(d.client_id == op.client_id and _matches_decision(d, op) for d in model.decisions)
        for op in resolved
    ):
        return model
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
