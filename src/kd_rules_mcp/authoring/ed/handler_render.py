"""Модуль расширения с обработчиками: заполнитель, диспетчер и собственные процедуры."""

from __future__ import annotations

import hashlib
from collections import defaultdict
from collections.abc import Mapping

from kd_rules_mcp.ed.address import build_addresses
from kd_rules_mcp.ed.model import EdDocument, ObjectRule
from kd_rules_mcp.validation.ed_authoring_handlers import preset_procedure_text

from .handlers import DISPATCHER, HandlerBindingPlan
from .hook import event_assignment_line, generate_handler_fill, header_property_lines
from .model import (
    AddAlgorithmicHeaderProperty,
    AddHeaderProperty,
    Operation,
    PreserveMissingHeaderProperty,
    SetObjectHandler,
)

# Порядок событий менеджера, §1.1.
EVENT_ORDER = (
    "ПриОтправкеДанных",
    "ПриКонвертацииДанныхXDTO",
    "ПередЗаписьюПолученныхДанных",
)


def pko_names_from_document(document: EdDocument) -> dict[str, str]:
    """Каноническое имя ПКО для Найти: объявленное имя модели, не адрес с решёткой."""
    index = build_addresses(document)
    names = {}
    for rule in document.pko:
        if not isinstance(rule, ObjectRule):
            continue
        label = rule.declared_name or rule.name
        for address in index.by_id.get(rule.entity_id, ()):
            names[address] = label
    return names


def render_handler_module(
    operations: tuple[Operation, ...],
    bindings: tuple[HandlerBindingPlan, ...],
    *,
    prefix: str,
    interface: int,
    dispatcher_name: str,
    dispatcher_order: tuple[str, ...],
    pko_names: Mapping[str, str] | None = None,
) -> str:
    """Полный модуль §5. Порядок веток и процедур — dispatcher_order, без выбора имён."""
    pko_names = {} if pko_names is None else pko_names
    ordered = _ordered_bindings(bindings, dispatcher_order)
    fill = generate_handler_fill(prefix, interface, _fill_rules(operations, ordered, pko_names))
    parts = [fill, _dispatcher(dispatcher_name, ordered)]
    parts.extend(_procedure(binding, operations) for binding in ordered)
    text = "\n\n".join(part.rstrip("\n") for part in parts) + "\n"
    if "\r" in text or not text.endswith("\n") or text.endswith("\n\n"):
        raise ValueError("Модуль обработчиков должен быть UTF-8/LF с одним завершающим переводом")
    return text


def procedure_block(text: str, name: str) -> str:
    """Текст процедуры от заголовка до строки КонецПроцедуры включительно, без аннотации.

    Конец — строка, целиком равная «КонецПроцедуры» от начала строки до перевода.
    Комментарий и строковый литерал в теле границу не закрывают.
    """
    marker = f"Процедура {name}("
    try:
        start = text.index(marker)
    except ValueError:
        raise ValueError(f"Процедура {name} не найдена") from None
    offset = start
    for line in text[start:].splitlines(keepends=True):
        offset += len(line)
        content = line[:-1] if line.endswith("\n") else line
        if content.endswith("\r"):
            content = content[:-1]
        if content == "КонецПроцедуры":
            return text[start:offset]
    raise ValueError(f"Процедура {name} не найдена")


def procedure_records(
    operations: tuple[Operation, ...],
    bindings: tuple[HandlerBindingPlan, ...],
    *,
    dispatcher_name: str,
    dispatcher_order: tuple[str, ...],
    runtime_verified: bool,
) -> tuple[dict[str, object], ...]:
    """Порождённые процедуры и их отпечатки: правка тела расходится с манифестом."""
    ordered = _ordered_bindings(bindings, dispatcher_order)
    call = _dispatcher(dispatcher_name, ordered)
    records: list[dict[str, object]] = [
        {
            "name": dispatcher_name,
            "role": "dispatcher",
            "runtime_verified": runtime_verified,
            "body_sha256": _sha(procedure_block(call, dispatcher_name)),
        }
    ]
    by_id = {op.operation_id: op for op in operations}
    for binding in ordered:
        body = _procedure(binding, operations)
        records.append(
            {
                "name": binding.handler_name,
                "role": "handler",
                "origin": _origin(binding, by_id),
                "event": binding.event,
                "parameters": list(binding.parameters),
                "runtime_verified": binding.runtime_verified,
                "body_sha256": _sha(procedure_block(body, binding.handler_name)),
            }
        )
    return tuple(records)


def binding_record(binding: HandlerBindingPlan) -> dict[str, object]:
    return {
        "event": binding.event,
        "handler_name": binding.handler_name,
        "manager_interface": binding.manager_interface,
        "operation_ids": list(binding.operation_ids),
        "parameters": list(binding.parameters),
        "previous_arguments": list(binding.previous_arguments),
        "previous_branch_hash": binding.previous_branch_hash,
        "previous_name": binding.previous_name,
        "previous_routine_hash": binding.previous_routine_hash,
        "runtime_verified": binding.runtime_verified,
        "target": {
            "configuration": binding.target.configuration,
            "direction": binding.target.direction,
            "format_version": binding.target.format_version,
            "pko_address": binding.target.pko_address,
            "plan": binding.target.plan,
            "project": binding.target.project,
            "variant": binding.target.variant,
        },
    }


def _ordered_bindings(
    bindings: tuple[HandlerBindingPlan, ...], dispatcher_order: tuple[str, ...]
) -> tuple[HandlerBindingPlan, ...]:
    by_name = {binding.handler_name: binding for binding in bindings}
    if set(dispatcher_order) != set(by_name) or len(dispatcher_order) != len(by_name):
        raise ValueError("Порядок диспетчера не совпадает с привязками")
    return tuple(by_name[name] for name in dispatcher_order)


def _fill_rules(
    operations: tuple[Operation, ...],
    bindings: tuple[HandlerBindingPlan, ...],
    pko_names: Mapping[str, str],
) -> tuple[tuple[str, str, tuple[str, ...]], ...]:
    direct: dict[tuple[str, str], list[AddHeaderProperty]] = defaultdict(list)
    algorithmic: dict[tuple[str, str], list[AddAlgorithmicHeaderProperty]] = defaultdict(list)
    events: dict[tuple[str, str], list[HandlerBindingPlan]] = defaultdict(list)
    for op in operations:
        key = _rule_key(op.target.direction, op.target.pko_address, pko_names)
        if isinstance(op, AddHeaderProperty):
            direct[key].append(op)
        elif isinstance(op, AddAlgorithmicHeaderProperty):
            algorithmic[key].append(op)
    for binding in bindings:
        key = _rule_key(binding.target.direction, binding.target.pko_address, pko_names)
        events[key].append(binding)
    rules = []
    for key in set(direct) | set(algorithmic) | set(events):
        lines: list[str] = []
        for op in sorted(
            direct[key], key=lambda item: (item.format_property, item.configuration_attribute)
        ):
            lines.extend(header_property_lines(op.configuration_attribute, op.format_property))
        for op in sorted(
            algorithmic[key], key=lambda item: (item.format_property, item.configuration_attribute)
        ):
            lines.extend(
                header_property_lines(
                    op.configuration_attribute,
                    op.format_property,
                    algorithmic=True,
                    conversion_rule=op.conversion_rule,
                )
            )
        for binding in sorted(events[key], key=lambda item: EVENT_ORDER.index(item.event)):
            lines.append(event_assignment_line(binding.event, binding.handler_name))
        rules.append((key[0], key[1], tuple(lines)))
    return tuple(rules)


def _rule_key(direction: str, address: str, pko_names: Mapping[str, str]) -> tuple[str, str]:
    return direction, pko_names.get(address, address.removeprefix("ПКО/"))


def _dispatcher(name: str, bindings: tuple[HandlerBindingPlan, ...]) -> str:
    lines = [
        f'&Вместо("{DISPATCHER}")',
        f"Процедура {name}(ИмяПроцедуры, Параметры)",
    ]
    for index, binding in enumerate(bindings):
        keyword = "Если" if index == 0 else "ИначеЕсли"
        lines.append(f'\t{keyword} ИмяПроцедуры = "{binding.handler_name}" Тогда')
        lines.append(_handler_call(binding.handler_name, binding.parameters))
    lines.extend(
        (
            "\tИначе",
            "\t\tПродолжитьВызов(ИмяПроцедуры, Параметры);",
            "\tКонецЕсли;",
            "КонецПроцедуры",
        )
    )
    return "\n".join(lines) + "\n"


def _handler_call(name: str, parameters: tuple[str, ...]) -> str:
    arguments = [f"Параметры.{parameter}" for parameter in parameters]
    if len(arguments) == 4:
        return f"\t\t{name}({arguments[0]}, {arguments[1]},\n\t\t\t{arguments[2]}, {arguments[3]});"
    return f"\t\t{name}({', '.join(arguments)});"


def _procedure(binding: HandlerBindingPlan, operations: tuple[Operation, ...]) -> str:
    by_id = {op.operation_id: op for op in operations}
    selected = [_require(by_id, op_id) for op_id in binding.operation_ids]
    if selected and all(isinstance(op, PreserveMissingHeaderProperty) for op in selected):
        pairs = []
        for op in selected:
            assert isinstance(op, PreserveMissingHeaderProperty)
            prop = _require(by_id, op.property_operation_id)
            if not isinstance(prop, AddHeaderProperty):
                raise ValueError("Preset ссылается не на прямую ПКС")
            pairs.append((prop.format_property, prop.configuration_attribute))
        return preset_procedure_text(binding.handler_name, tuple(pairs))
    if len(selected) != 1 or not isinstance(selected[0], SetObjectHandler):
        raise ValueError("Привязка обработчика не является одним телом агента")
    handler = selected[0]
    text = f"Процедура {binding.handler_name}({', '.join(binding.parameters)})\n"
    if binding.previous_name:
        arguments = ", ".join(binding.previous_arguments)
        text += f"\t{binding.previous_name}({arguments});\n"
    return text + handler.body + "КонецПроцедуры\n"


def _origin(binding: HandlerBindingPlan, by_id: Mapping[str, Operation]) -> str:
    selected = [_require(by_id, op_id) for op_id in binding.operation_ids]
    if selected and all(isinstance(op, PreserveMissingHeaderProperty) for op in selected):
        return "preset"
    return "agent"


def _require(by_id: Mapping[str, Operation], operation_id: str) -> Operation:
    try:
        return by_id[operation_id]
    except KeyError:
        raise ValueError("Привязка ссылается на операцию вне комплекта") from None


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
