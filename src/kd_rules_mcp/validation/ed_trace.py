"""Статический путь объекта: звенья общего ядра и факты исполнителя БСП 3.1.12.

Обозначение XDTO:N соответствует строкам ОбменДаннымиXDTOСервер; основания
переходов собраны в design.md изменения kd3-trace-pair и executor.md §3.
"""

from __future__ import annotations

from collections import deque
from dataclasses import replace
from typing import Any, TypedDict

from kd_rules_mcp.ed import model as ed
from kd_rules_mcp.ed.forms import EVENT_INVOCATIONS, accepts_arguments, invocation_keys_match
from kd_rules_mcp.ed.lexer import tokenize
from kd_rules_mcp.validation.ed_chain import (
    SEARCH_BY_FIELDS,
    ChainView,
    format_path,
    properties,
    same_object,
)
from kd_rules_mcp.validation.ed_projection import event_direction

Row = dict[str, Any]


class TraceResult(TypedDict):
    object: str
    direction: str
    found: bool
    status: str
    static: bool
    version: str | None
    schema_id: str | None
    layered: bool
    parse_status: str
    matched: list[Row]
    paths: list[Row]
    properties: list[Row]
    handlers: list[Row]
    search: list[Row]
    cascades: list[Row]
    breaks: list[Row]
    notes: list[str]


_RANK = {"ok": 0, "partial": 1, "unknown": 2, "broken": 3}
_EVENTS = {
    "ПриОбработке": ["XDTO:8428-8472"],
    "ВыборкаДанных": ["XDTO:8483-8530"],
    "ПриОтправкеДанных": ["XDTO:8545-8587"],
    "ПриКонвертацииДанныхXDTO": ["XDTO:8597-8638"],
    "АлгоритмПоиска": ["XDTO:8697-8730"],
    "ПередЗаписьюПолученныхДанных": ["XDTO:8652-8695"],
    "ПослеЗагрузкиВсехДанных": ["XDTO:7141-7239"],
}
_MESSAGES = {
    "no_pod": "ПОД не найден: отправка завершится ошибкой, получение пропустит объект",
    "pko_not_in_pod": "ПКО не используется ПОД: объект может уйти только ссылкой",
    "shadowed_pod": "Исполнитель выбирает первый ПОД; этот ПОД не используется",
    "shadowed_maybe": "Предыдущий ПОД может затенить этот; его применимость не доказана",
    "layer_order_unknown": "Порядок ПОД из разных слоёв неизвестен",
    "pod_pko_missing": "ПКО отсутствует: отправка молча пропускает его, получение выдаёт ошибку",
    "not_in_direction": "Правило не действует в выбранном направлении или версии",
    "type_missing": "Тип отсутствует в схеме: исполнитель удаляет правило",
    "pko_type_not_declared": "Тип ПКО не объявлен ПОД получения: объект пропускается фильтром",
    "property_missing": "Свойства нет в типе формата: ПКС молча теряется",
    "rule_missing": "Правило свойства не найдено: конвертация значения завершится ошибкой",
    "property_not_in_rules": "ПКС свойства нет: перенос свойства не задан",
    "handler_missing": "Нет ветки диспетчера: обработчик молча не вызывается",
    "handler_throws": "Цепочка диспетчера слоёв завершается исключением",
    "duplicate_name": "Имя ПКО повторяется: исполнитель берёт первую строку, эта не используется",
    "pod_handler_may_switch": "Обработчик ПОД может выключить ПКО: результат не доказан",
    "shadowed_unknown": "Раньше в таблице ПОД с непонятым ключом: может затенить этот",
    "executor_fills": "Стандартный реквизит заполняет исполнитель; значение не проверено",
    "ambiguous": "Вызов обработчика неоднозначен",
    "invalid_signature": "Диспетчер не может вызвать обработчик с этой сигнатурой",
    "search_field_unfilled": "Поле поиска не заполняется: вариант поиска пропускается",
    "handler_may_supply": "Свойство может заполнить обработчик; результат не доказан",
    "handler_literal": "В обработчике есть литерал свойства; выполнение не доказано",
    "handler_fills": "Значение заполняет алгоритм; результат статически не доказан",
}


def _mark(row: Row, status: str, reason: str | None = None, *evidence: str) -> Row:
    previous = row.get("status", "ok")
    row["status"] = max((previous, status), key=_RANK.__getitem__)
    if reason:
        old_reasons = row.get("reasons", [])
        row["reasons"] = list(
            dict.fromkeys(
                [reason, *old_reasons]
                if _RANK[status] > _RANK[previous]
                else [*old_reasons, reason]
            )
        )
        if _RANK[status] > _RANK[previous] or "message" not in row:
            row["message"] = _MESSAGES.get(reason, f"Звено не доказано: {reason}")
    if evidence:
        row["evidence"] = list(dict.fromkeys([*row.get("evidence", []), *evidence]))
    return row


def _state(view: ChainView, entity: ed.Entity, parent: ed.Entity | None = None) -> Row:
    state = view.state(entity, parent)
    row: Row = {"status": "ok"}
    if state.status == "not_applicable":
        _mark(row, "broken", "not_in_direction")
    else:
        for reason in state.reasons:
            _mark(row, state.status, reason)
    # Неизвестный фрагмент вне владельца тоже может менять состав таблиц.
    if view.document.parse_status == ed.ParseStatus.PARTIAL:
        _mark(row, "partial", "reader_partial")
    if parent is not None:
        _merge(row, _state(view, parent))
    return row


def _absence(view: ChainView, row: Row, reason: str, *evidence: str) -> Row:
    _mark(
        row,
        "partial" if view.document.parse_status == ed.ParseStatus.PARTIAL else "broken",
        reason,
        *evidence,
    )
    if view.document.parse_status == ed.ParseStatus.PARTIAL:
        _mark(row, "partial", "reader_partial")
    return row


def _link(view: ChainView, step: str, entity: ed.Entity, *evidence: str) -> Row:
    row = {
        "step": step,
        "address": view.address(entity),
        "name": entity.name,
        **_state(view, entity),
    }
    if evidence:
        row["evidence"] = list(evidence)
    origin = view.origin(entity)
    if origin is not None:
        row["origin"] = origin
    return row


def _type_status(view: ChainView, status: str) -> Row:
    if status == "resolved":
        row: Row = {"status": "ok"}
    elif status == "missing":
        row = _mark({}, "broken", "type_missing", "XDTO:4146-4158")
    else:
        row = _mark({}, "unknown" if status == "schema_unavailable" else "partial", status)
    if view.profile.schema and view.profile.schema.status != "complete":
        if status == "missing":
            row["status"] = "partial"
        _mark(row, "partial", "partial_schema")
    return row


def _type_link(view: ChainView, rule: ed.ObjectRule) -> Row:
    row = _link(view, "type", rule, "XDTO:4146-4158")
    row["name"] = rule.format_object.value
    state = _type_status(view, view.type_state(rule)[1])
    _merge(row, state)
    return row


def _merge(row: Row, other: Row) -> None:
    reasons = other.get("reasons", [])
    if not reasons:
        _mark(row, other["status"])
    for reason in reasons:
        _mark(row, other["status"], reason, *other.get("evidence", []))


def _roots(view: ChainView, name: str) -> tuple[list[Row], list[ed.ObjectRule], list[str]]:
    matched: list[Row] = []
    roots: dict[str, ed.ObjectRule] = {}
    keys: list[str] = []

    def add_key(value: str | None) -> None:
        if value and not any(
            same_object(value, key) if view.direction == "send" else value == key for key in keys
        ):
            keys.append(value)

    def add(entity: ed.ObjectRule | ed.ProcessingRule, by: str, inexact: bool = False) -> None:
        row: Row = {"by": by, "address": view.address(entity)}
        if inexact:
            row["inexact"] = True
        matched.append(row)
        if isinstance(entity, ed.ObjectRule):
            roots[entity.entity_id] = entity
            add_key(
                view.pko_metadata(entity)[0]
                if view.direction == "send"
                else entity.format_object.value
            )
        else:
            add_key(
                view.pod_metadata(entity)[0]
                if view.direction == "send"
                else entity.format_selection.value
            )

    pko_name = name[4:] if name.startswith("ПКО/") else name
    addressed = view.index.by_address.get(name)
    if isinstance(addressed, ed.ObjectRule):
        add(addressed, "pko")
    for rule in view.document.pko:
        if rule.name == pko_name and rule is not addressed:
            add(rule, "pko")
    for entity in (*view.pods(), *view.document.pko):
        metadata = (
            view.pod_metadata(entity)
            if isinstance(entity, ed.ProcessingRule)
            else view.pko_metadata(entity)
        )
        if same_object(metadata[0], name):
            add(entity, "configuration")
    for entity in (*view.pods(), *view.document.pko):
        value = (
            entity.format_selection.value
            if isinstance(entity, ed.ProcessingRule)
            else entity.format_object.value
        )
        if value == name:
            add(entity, "format")
    if not matched:
        for entity in (*view.pods(), *view.document.pko):
            value = (
                entity.format_selection.value
                if isinstance(entity, ed.ProcessingRule)
                else entity.format_object.value
            )
            if value and value.casefold() == name.casefold():
                add(entity, "format", True)
    return matched, list(roots.values()), keys


def _unknown_keys(view: ChainView) -> list[tuple[int, int, int]]:
    """Порядок действующих ПОД, ключ которых сервер не понял."""
    orders = []
    for pod in view.pods():
        if view.state(pod).status == "not_applicable":
            continue
        if view.direction == "send":
            unknown = (
                view.pod_metadata(pod)[0] is None
                and pod.configuration_selection.presence != "absent"
            )
        else:
            unknown = view.literal(pod.format_selection)[1] not in ("resolved", "absent", "empty")
        if unknown:
            orders.append(view.table_order(pod))
    return orders


def _pod_links(view: ChainView, pods: list[ed.ProcessingRule]) -> list[Row]:
    active: list[Row] = []
    rows = []
    unknown = _unknown_keys(view)
    for pod in pods:
        row = _link(view, "pod", pod, "XDTO:8344-8357" if view.direction == "send" else "XDTO:4519")
        if any(order < view.table_order(pod) for order in unknown):
            _mark(row, "partial", "shadowed_unknown")
        if view.direction == "send":
            field = pod.configuration_selection
            if view.applicability.field(field, "send") is not True:
                _mark(row, "partial", "opaque_condition")
            if view.pod_metadata(pod)[1] != "resolved":
                _mark(row, "partial", view.pod_metadata(pod)[1])
        if view.state(pod).status != "not_applicable":
            if view.layered and any(
                p.span.file_id != pod.span.file_id
                for p in pods
                if view.state(p).status != "not_applicable"
            ):
                _mark(row, "partial", "layer_order_unknown")
            elif active:
                if any(p["status"] != "ok" for p in active):
                    _mark(row, "partial", "shadowed_maybe")
                else:
                    _mark(row, "broken", "shadowed_pod")
            active.append(row)
        if view.direction == "receive":
            field = pod.format_selection
            truth = view.applicability.field(field, view.direction)
            if truth is not True or field.presence != "literal" or not field.value:
                _mark(
                    row,
                    "partial",
                    "opaque_condition" if truth is not True else "dynamic_format_type",
                )
            else:
                status = view.profile.find_type(field.value)[1]
                schema_state = _type_status(view, status)
                if status == "missing":
                    schema_state["evidence"] = ["XDTO:3939-3950"]
                _merge(row, schema_state)
        rows.append(row)
    return rows


def _declared(view: ChainView, rule: ed.ObjectRule, pod: ed.ProcessingRule, row: Row) -> None:
    value = rule.format_object.value
    if value == pod.format_selection.value and value:
        return
    candidates = [
        p
        for p in view.pods()
        if p.format_selection.value == value and view.state(p).status != "not_applicable"
    ]
    evidence = ("XDTO:7477-7479", "XDTO:9465-9484")
    if not value or rule.format_object.presence != "literal":
        _mark(row, "partial", "dynamic_format_type", *evidence)
    elif not candidates:
        _absence(view, row, "pko_type_not_declared", *evidence)
    elif not any(
        view.state(p).status == "ok"
        and view.applicability.field(p.format_selection, "receive") is True
        and p.format_selection.presence == "literal"
        and view.profile.find_type(value)[1] not in ("missing", "partial_schema")
        for p in candidates
    ):
        _mark(row, "partial", "declaration_unknown", *evidence)


def _paths(
    view: ChainView, roots: list[ed.ObjectRule], keys: list[str], filtered: bool
) -> tuple[list[Row], list[ed.Entity], list[ed.ObjectRule]]:
    paths: list[Row] = []
    entities: dict[str, ed.Entity] = {}
    rules: dict[str, ed.ObjectRule] = {}
    root_ids = {r.entity_id for r in roots}
    used = {
        rule.entity_id
        for pod in view.pods()
        if view.state(pod).status != "not_applicable"
        for use in pod.used_pko
        for rule in view.pko_named(use.name)
    }
    keys = list(keys)
    for pod in view.pods():
        if view.state(pod).status == "not_applicable":
            continue
        if not any(use.name == root.name for use in pod.used_pko for root in roots):
            continue
        key = view.pod_metadata(pod)[0] if view.direction == "send" else pod.format_selection.value
        if key and not any(
            same_object(key, k) if view.direction == "send" else key == k for k in keys
        ):
            keys.append(key)
    for key in keys:
        first: Row = {
            "step": "object" if view.direction == "send" else "format",
            "name": key,
            "status": "ok",
            "evidence": ["XDTO:8344-8357" if view.direction == "send" else "XDTO:7434-7437"],
        }
        pods = (
            [p for p in view.pods() if same_object(view.pod_metadata(p)[0], key)]
            if view.direction == "send"
            else [p for p in view.pods() if p.format_selection.value == key]
        )
        if not pods:
            missing = _absence(
                view,
                {"step": "pod", "name": key},
                "no_pod",
                "XDTO:8344-8357" if view.direction == "send" else "XDTO:7435-7437",
            )
            paths.append({"steps": [first, missing]})
        for pod, pod_link in zip(pods, _pod_links(view, pods), strict=True):
            entities[pod.entity_id] = pod
            if not pod.used_pko:
                empty = _mark(
                    {"step": "use", "address": view.address(pod)},
                    "partial",
                    "used_pko_empty",
                    "XDTO:794-799" if view.direction == "send" else "XDTO:7455-7475",
                )
                paths.append({"steps": [first.copy(), pod_link.copy(), empty]})
            for use in pod.used_pko:
                candidates = view.pko_named(use.name)
                use_link: Row = {
                    "step": "use",
                    "address": view.address(pod),
                    "name": use.name,
                    "status": "ok",
                    "evidence": ["XDTO:794-799" if view.direction == "send" else "XDTO:7455-7475"],
                }
                if (origin := view.origin(pod)) is not None:
                    use_link["origin"] = origin
                if view.has_event(pod, "ПриОбработке"):
                    _mark(
                        use_link,
                        "partial",
                        "pod_handler_may_switch",
                        "XDTO:8402-8410",
                        "XDTO:804-814" if view.direction == "send" else "XDTO:7489-7497",
                    )
                if not candidates:
                    _absence(view, use_link, "pod_pko_missing", *use_link["evidence"])
                    paths.append({"steps": [first.copy(), pod_link.copy(), use_link]})
                for rule in candidates:
                    rules[rule.entity_id] = rule
                    entities[rule.entity_id] = rule
                    if view.state(pod).status != "not_applicable":
                        used.add(rule.entity_id)
                    link = use_link.copy()
                    if len(candidates) > 1:
                        chosen, chosen_state = view.pko_first(use.name)
                        if chosen is not None and rule is not chosen:
                            _mark(link, "broken", "duplicate_name", "XDTO:504", "XDTO:795")
                        elif chosen_state.status != "ok":
                            _mark(link, "partial", "ambiguous_name")
                    if view.state(rule).status == "not_applicable":
                        _mark(link, "broken", "not_in_direction")
                        link["message"] = (
                            "ПКО молча пропускается при отправке"
                            if view.direction == "send"
                            else "ПКО недоступно при получении: ошибка обмена"
                        )
                    pko = _link(
                        view,
                        "pko",
                        rule,
                        "XDTO:794-799" if view.direction == "send" else "XDTO:7455-7479",
                    )
                    if filtered or root_ids:
                        pko["matched"] = rule.entity_id in root_ids
                    if view.direction == "receive":
                        _declared(view, rule, pod, pko)
                    steps = [first.copy(), pod_link.copy(), link, pko, _type_link(view, rule)]
                    if view.direction == "receive":
                        name, status = view.pko_metadata(rule)
                        obj = _link(view, "object", rule, "XDTO:1856-1885")
                        obj["name"] = name
                        if status != "resolved":
                            _mark(obj, "partial", status)
                        if (
                            view.applicability.field(rule.configuration_object, "receive")
                            is not True
                        ):
                            _mark(obj, "partial", "opaque_condition")
                        steps.append(obj)
                    steps.append(
                        {
                            "step": "correspondent",
                            **_mark(
                                {},
                                "unknown",
                                "correspondent_declaration",
                                *(
                                    ("XDTO:797-803", "XDTO:3878-3893")
                                    if view.direction == "send"
                                    else ("XDTO:7477-7479", "XDTO:9443-9463")
                                ),
                            ),
                            "hint": "ed_pair_validate, live_format_support",
                        }
                    )
                    paths.append({"steps": steps})
    # ПКО-корень вне `ИспользуемыеПКО` действующих ПОД: при отправке уходит только ссылкой
    # (XDTO:1541–1567), при получении конвертирует только ссылки из ПКС (XDTO:6820–6826).
    evidence = "XDTO:1541-1567" if view.direction == "send" else "XDTO:6820-6826"
    for rule in roots:
        if rule.entity_id in used:
            continue
        rules[rule.entity_id] = rule
        entities[rule.entity_id] = rule
        if view.direction == "send":
            name, status = view.pko_metadata(rule)
        else:
            name, status = rule.format_object.value, "resolved"
        obj: Row = {
            "step": "object" if view.direction == "send" else "format",
            "name": name,
            "evidence": [evidence],
            **_state(view, rule),
        }
        if status != "resolved":
            _mark(obj, "partial", status)
        paths.append(
            {
                "steps": [
                    obj,
                    _absence(
                        view,
                        {"step": "pod", "address": view.address(rule)},
                        "pko_not_in_pod",
                        evidence,
                    ),
                    _link(view, "pko", rule, evidence),
                    _type_link(view, rule),
                ]
            }
        )
    return paths, list(entities.values()), list(rules.values())


def _property(
    view: ChainView, rule: ed.ObjectRule, group: ed.PropertyGroup | None, prop: ed.PropertyRule
) -> Row:
    row: Row = {
        "pko": view.address(rule),
        "address": view.address(prop),
        "configuration_property": prop.configuration_property,
        "format_property": prop.format_property,
        **_state(view, prop, group or rule),
    }
    if group is not None:
        row["table"] = group.format_property
    side = prop.format_property if view.direction == "send" else prop.configuration_property
    if not side.strip():
        _mark(
            row,
            "broken",
            "not_in_direction",
            "XDTO:1442" if view.direction == "send" else "XDTO:6724",
        )
    if prop.algorithm_flag:
        row["algorithm"] = prop.algorithm_flag
        _mark(row, "partial", "handler_fills", "XDTO:6748-6755")
    schema = view.property_schema(rule, group, prop)
    if schema.status == "resolved":
        row.update(key=schema.key, schema_path=schema.path)
    elif schema.status == "missing":
        _mark(
            row,
            "partial"
            if view.profile.schema and view.profile.schema.status != "complete"
            else "broken",
            "property_missing",
            "XDTO:933-954",
        )
    else:
        _mark(
            row,
            "unknown" if schema.reason == "schema_unavailable" else "partial",
            schema.reason or schema.status,
        )
    if view.profile.schema and view.profile.schema.status != "complete":
        _mark(row, "partial", "partial_schema")
    if prop.conversion_rule:
        row["conversion_rule"] = prop.conversion_rule
        if view.pko_first(prop.conversion_rule)[0] is None and not any(
            p.name == prop.conversion_rule for p in view.document.pkpd
        ):
            _absence(view, row, "rule_missing", "XDTO:506-508", "XDTO:1543-1554")
    return row


def _handler(rule: ed.ObjectRule, direction: str) -> ed.HandlerBinding | None:
    event = "ПриОтправкеДанных" if direction == "send" else "ПриКонвертацииДанныхXDTO"
    return next((h for h in rule.events if h.event == event and h.target_name), None)


def _filter_break(view: ChainView, rule: ed.ObjectRule, name: str) -> Row:
    row: Row = {"section": "properties", "address": view.address(rule)}
    handler = _handler(rule, view.direction)
    if handler is None:
        return _absence(
            view,
            row,
            "property_not_in_rules",
            "XDTO:1442" if view.direction == "send" else "XDTO:6724",
        )
    _mark(row, "partial", "handler_may_supply", *_EVENTS[handler.event])
    if view.references is not None and handler.target_id:
        refs = [
            r
            for r in view.references.entries
            if r.owner_id == handler.target_id
            and r.kind == "format_property"
            and r.name
            and r.name.casefold() == name.casefold()
        ]
        if refs:
            row.pop("reasons")
            _mark(row, "partial", "handler_literal")
            row["line"] = refs[0].span.line_start
    return row


def _handlers(view: ChainView, entities: list[ed.Entity]) -> list[Row]:
    rows = []
    for entity in entities:
        if not isinstance(entity, (ed.ObjectRule, ed.ProcessingRule)):
            continue
        for handler in entity.events:
            if not handler.target_name or handler.event not in _EVENTS:
                continue
            direction = event_direction(handler.event)
            if direction != view.direction and not (
                isinstance(entity, ed.ProcessingRule) and handler.event == "ПриОбработке"
            ):
                continue
            row: Row = {
                "rule": view.address(entity),
                "address": view.address(entity),
                "event": handler.event,
                "target": handler.target_name,
                "evidence": _EVENTS[handler.event],
                **_state(view, handler, entity),
            }
            # Снимок со слоями разрешает привязку цепочкой диспетчера: call — вызывается,
            # no_call — ветки нет, throws — исключение, unknown — путь не понят.
            resolution = {"call": "called", "no_call": "missing"}.get(
                handler.resolution, handler.resolution
            )
            if resolution == "resolved":
                # Читатель разрешает имя процедуры, но наличие ветки проверяет отдельно.
                # Процедура без ветки молча не вызывается (XDTO:8598-8609).
                invocation = EVENT_INVOCATIONS[handler.event]
                dispatcher_name = (
                    "выполнитьфункциюмодуляменеджера"
                    if invocation.kind == "function"
                    else "выполнитьпроцедурумодуляменеджера"
                )
                dispatchers = {
                    r.entity_id
                    for r in view.document.routines
                    if r.name.casefold() == dispatcher_name
                }
                cases = [
                    c
                    for c in view.document.dispatcher_cases
                    if c.dispatcher_id in dispatchers and c.literal_name == handler.target_name
                ]
                if not cases:
                    hidden = any(u.owner_id in dispatchers for u in view.document.unknown)
                    resolution = "unknown" if hidden else "missing"
                elif len(cases) > 1:
                    resolution = "ambiguous"
                else:
                    case = cases[0]
                    selector = next(
                        (
                            r.parameters[0].name
                            for r in view.document.routines
                            if r.entity_id == case.dispatcher_id and r.parameters
                        ),
                        None,
                    )
                    guards = {g.entity_id: g for g in view.document.guards}
                    retained = []
                    for ident in case.guards:
                        guard = guards.get(ident)
                        tokens = tokenize(guard.expression_raw) if guard else ()
                        # Выбор ветки по известному имени уже доказан literal_name.
                        selected = (
                            selector is not None
                            and len(tokens) == 3
                            and tokens[0].folded == selector.casefold()
                            and tokens[1].value == "="
                            and tokens[2].kind == "string"
                            and tokens[2].value == handler.target_name
                        )
                        if not selected:
                            retained.append(ident)
                    _merge(row, _state(view, replace(case, guards=tuple(retained))))
                    targets = [
                        r
                        for r in view.document.routines
                        if r.name.casefold() == case.target.raw.casefold()
                    ]
                    if len(case.target.reference_parts) > 1:
                        resolution = "unknown"
                    elif not targets:
                        resolution = "missing"
                    elif len(targets) > 1:
                        resolution = "ambiguous"
                    elif (
                        not invocation_keys_match(invocation, case.arguments)
                        or not accepts_arguments(targets[0].parameters, len(case.arguments))
                        or targets[0].routine_kind != invocation.kind
                    ):
                        resolution = "invalid_signature"
            if resolution == "missing":
                _mark(row, "broken", "handler_missing", *_EVENTS[handler.event])
            elif resolution == "throws":
                _mark(row, "broken", "handler_throws", *_EVENTS[handler.event])
            elif resolution not in ("resolved", "called"):
                _mark(row, "unknown" if resolution == "unknown" else "broken", resolution)
            rows.append(row)
    return rows


def _search(view: ChainView, rule: ed.ObjectRule) -> Row:
    row: Row = {
        "pko": view.address(rule),
        "identification": rule.identification.value,
        "variants": [],
        **_state(view, rule),
    }
    if rule.identification.presence != "literal" or not rule.identification.value:
        _mark(row, "partial", "identification_unknown", "XDTO:1896-1907")
        return row
    if view.applicability.field(rule.identification, "receive") is not True:
        _mark(row, "partial", "identification_unknown", "XDTO:1896-1907")
    if rule.identification.value not in SEARCH_BY_FIELDS:
        return row
    metadata = view.pko_metadata(rule)[0] or ""
    for search in rule.search_sets:
        variant: Row = {
            "fields": list(search.fields),
            "address": view.address(search),
            "items": [],
            **_state(view, search, rule),
            "evidence": ["XDTO:6863-6887"],
        }
        if not search.fields:
            _mark(variant, "partial", "reader_partial")
        for field in search.fields:
            item: Row = {"field": field, "status": "ok"}
            catalog = metadata.casefold().startswith(("справочник.", "планвидовхарактеристик."))
            # Ключ поля исполнитель сравнивает точно: `ПолеПоиска.Ключ = "Родитель"` (XDTO:6876).
            if field == "Родитель" and catalog:
                item["reason"] = "parent_optional"
            elif field == "ЭтоГруппа" and catalog:
                # Признак группы ставит исполнитель при создании полученных данных, не ПКС.
                _mark(item, "partial", "executor_fills", "XDTO:4399-4402")
                item["reason"] = "executor_fills"
            else:
                candidates = [
                    p
                    for p in rule.properties
                    if p.configuration_property.casefold() == field.casefold()
                    and view.state(p, rule).status != "not_applicable"
                ]
                if candidates:
                    prop = candidates[0]
                    item["format_property"] = prop.format_property
                    prop_row = _property(view, rule, None, prop)
                    _merge(item, prop_row)
                    if len(candidates) > 1:
                        _mark(item, "partial", "ambiguous_name")
                    if "key" in prop_row:
                        item["key"] = prop_row["key"]
                elif _handler(rule, "receive"):
                    _mark(item, "partial", "handler_may_supply")
                else:
                    _absence(view, item, "search_field_unfilled", "XDTO:6863-6887")
                if item.get("reasons"):
                    item["reason"] = item["reasons"][0]
            _merge(variant, item)
            variant["items"].append(item)
        _merge(row, variant)
        row["variants"].append(variant)
    return row


def _cascades(view: ChainView, rules: list[ed.ObjectRule], depth: int) -> list[Row]:
    rows: list[Row] = []
    queue = deque((rule, 0) for rule in rules)
    expanded = {r.entity_id for r in rules}
    seen: set[str] = set()
    while queue:
        owner, level = queue.popleft()
        if level >= depth:
            continue
        for group, prop in properties(owner):
            name = prop.conversion_rule
            if not name or name in seen:
                continue
            seen.add(name)
            row: Row = {
                "from": view.address(prop),
                "rule": name,
                "depth": level + 1,
                **_state(view, prop, group or owner),
            }
            if any(p.name == name for p in view.document.pkpd):
                row["kind"] = "pkpd"
            else:
                candidates = view.pko_named(name)
                if not candidates:
                    _absence(view, row, "rule_missing", "XDTO:506-508", "XDTO:1543-1554")
                for target in candidates:
                    row["address"] = view.address(target)
                    _merge(row, _state(view, target))
                    row["type_status"] = view.type_state(target)[1]
                    _merge(row, _type_status(view, row["type_status"]))
                    if len(candidates) > 1:
                        _mark(row, "partial", "ambiguous_name")
                    if target.entity_id not in expanded:
                        expanded.add(target.entity_id)
                        queue.append((target, level + 1))
            rows.append(row)
    return rows


def _break(row: Row, section: str, address: str = "") -> Row:
    result: Row = {
        "section": section,
        "address": row.get("address", row.get("rule", address)),
        "reason": row["reasons"][0],
        "message": row.get("message", "Звено разорвано"),
    }
    for key in ("step", "evidence"):
        if key in row:
            result[key] = row[key]
    return result


def trace(view: ChainView, object: str, *, property: str | None, depth: int) -> TraceResult:
    """Собрать непостраничную трассу одного объекта без I/O и исполнения BSL."""
    if not object.strip():
        raise ValueError("object: пустая строка")
    if type(depth) is not int or not 0 <= depth <= 3:
        raise ValueError("depth: целое число от 0 до 3")
    matched, roots, keys = _roots(view, object)
    paths, entities, rules = (
        _paths(view, roots, keys, property is not None) if matched else ([], [], [])
    )
    if matched and not paths:
        # Корень найден, но его сторону нельзя превратить в ключ поиска ПОД.
        # Пустой список путей скрыл бы именно эту неопределённость.
        for root in roots:
            first = _type_link(view, root)
            first["step"] = "object" if view.direction == "send" else "format"
            status = (
                view.pko_metadata(root)[1]
                if view.direction == "send"
                else view.literal(root.format_object)[1]
            )
            _mark(first, "partial", status if status != "resolved" else "pod_key_unknown")
            paths.append({"steps": [first, _link(view, "pko", root, "XDTO:7455-7479")]})
            entities.append(root)
            rules.append(root)
        if not roots:
            for match in matched:
                entity = view.index.by_address.get(match["address"])
                if entity is None:
                    continue
                first = _link(
                    view,
                    "object" if view.direction == "send" else "format",
                    entity,
                    "XDTO:8344-8357" if view.direction == "send" else "XDTO:7434-7437",
                )
                _mark(
                    first,
                    "partial",
                    "configuration_object_unavailable"
                    if view.direction == "send"
                    else "dynamic_format_type",
                )
                paths.append({"steps": [first]})
                entities.append(entity)
    prop_rows: list[Row] = []
    breaks: list[Row] = []
    for rule in rules:
        selected = [
            (g, p)
            for g, p in properties(rule)
            if property is None
            or property.casefold()
            in (
                p.configuration_property.casefold(),
                p.format_property.casefold(),
                format_path(g, p).casefold(),
                *((g.configuration_property.casefold(), g.format_property.casefold()) if g else ()),
            )
        ]
        prop_rows.extend(_property(view, rule, group, prop) for group, prop in selected)
        if property is not None and not selected:
            row = _filter_break(view, rule, property)
            row["reason"] = row.pop("reasons")[0]
            breaks.append(row)
    for path in paths:
        steps = [s for s in path["steps"] if s["step"] != "correspondent"]
        status = max((s["status"] for s in steps), key=_RANK.__getitem__)
        path["verdict"] = "complete" if status == "ok" else status
        broken = [s for s in steps if s["status"] == "broken"]
        if broken:
            first = _break(broken[0], "paths")
            first.pop("section")
            path["break"] = first
        breaks.extend(_break(s, "paths") for s in broken)
    handlers = _handlers(view, entities)
    search = [_search(view, rule) for rule in rules] if view.direction == "receive" else []
    cascades = _cascades(view, rules, depth)
    for section, rows in (
        ("properties", prop_rows),
        ("handlers", handlers),
        ("cascades", cascades),
    ):
        breaks.extend(_break(row, section) for row in rows if row["status"] == "broken")
    for row in search:
        breaks.extend(
            _break(v, "search", row["pko"]) for v in row["variants"] if v["status"] == "broken"
        )
    unique: dict[tuple, Row] = {}
    for row in breaks:
        unique.setdefault((row["section"], row.get("step"), row["address"], row["reason"]), row)
    notes = [
        "Статический разбор: выполнение BSL и настройки узла не проверены",
        "Корреспондент проверяется ed_pair_validate и live_format_support",
    ]
    if view.document.parse_status == ed.ParseStatus.PARTIAL:
        notes.append("модуль прочитан не полностью: выводов об отсутствии не делать")
    if view.profile.schema is None:
        notes.append("тип и свойства формата не проверены: передайте schema_id")
    if not matched:
        notes.append(f'Ищите правило через ed_list(kind="pko", text="{object}")')
    return TraceResult(
        object=object,
        direction=view.direction,
        found=bool(matched),
        status="found" if matched else "not_found",
        static=True,
        version=view.profile.format_version,
        schema_id=None,
        layered=view.layered,
        parse_status=view.document.parse_status,
        matched=matched,
        paths=paths,
        properties=prop_rows,
        handlers=handlers,
        search=search,
        cascades=cascades,
        breaks=list(unique.values()),
        notes=notes,
    )
