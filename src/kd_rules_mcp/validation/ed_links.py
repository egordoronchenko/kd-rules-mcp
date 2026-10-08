"""Проверки связности прочитанного модуля менеджера EnterpriseData.

Ошибка — только то, что в модуле не исполнится или не скомпилируется.
Остальное — предупреждение: данные, условия и тела обработчиков не доказываются.
Схема формата и структура конфигурации в этом срезе не передаются.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass

from kd_rules_mcp.ed.address import AddressIndex, escape_segment
from kd_rules_mcp.ed.layer_model import EffectiveContext, LayeredManager
from kd_rules_mcp.ed.model import (
    DispatcherCase,
    EdDocument,
    Entity,
    HandlerBinding,
    ObjectRule,
    ProcessingRule,
    PropertyRule,
    Routine,
    SourceSpan,
    UnknownFragment,
)
from kd_rules_mcp.ed.refs import (
    ReferenceIndex,
    classify_deferred_argument,
    deferred_cases,
    norm_name,
)
from kd_rules_mcp.validation.report import Level, ValidationReport

_ERRORS = frozenset(
    {
        "ed.conversion.required",
        "ed.handler.missing",
        "ed.dispatcher.target_missing",
        "ed.reference.rule_use_missing",
    }
)
_REQUIRED_EVENTS = ("ПередКонвертацией", "ПослеКонвертации", "ПередОтложеннымЗаполнением")
_EXTENDED_EVENT = "ПриПолученииЗапросаВыгрузкиОбъекта"
_PROCEDURE_DISPATCHER = "выполнитьпроцедурумодуляменеджера"
_FUNCTION_DISPATCHER = "выполнитьфункциюмодуляменеджера"
_EXTENDED = "ed.handler.extended_events"
# ОСКД:253 — исполнитель читает поле ПриПодбореСсылкиДляПовторнойОтправки, а не поле события.
_EXTENDED_REASON = (
    "Событие «ПриПолученииЗапросаВыгрузкиОбъекта»: исполнитель читает другое поле правила, "
    "связь с обработчиком не проверялась"
)
_EXTERNAL = "ed.dispatcher.external_target"
_INCOMPLETE = "ed.reader.incomplete"
_CODE_KINDS = frozenset({"pko_lookup", "instruction_rule", "pod_use"})
_CODE_LABEL = {
    "pko_lookup": "поиск ПКО",
    "instruction_rule": "инструкция",
    "pod_use": "использование ПКО",
}


@dataclass(frozen=True, slots=True)
class _Row:
    file_id: str
    line: int
    level: Level
    check: str
    address: str
    message: str


def validate_links(
    document: EdDocument | LayeredManager,
    addresses: AddressIndex | None = None,
    references: ReferenceIndex | None = None,
    *,
    context: EffectiveContext | None = None,
    explained: frozenset[tuple[str, str, str, str]] = frozenset(),
    uncertain: frozenset[tuple[str, str]] = frozenset(),
    preserved: frozenset[tuple[str, str]] = frozenset(),
    declared_pko: frozenset[str] = frozenset(),
    declared_rules: frozenset[str] = frozenset(),
    unknown_pko: frozenset[str] = frozenset(),
    unknown_rules: frozenset[str] = frozenset(),
    unread_properties: Mapping[str, str] | None = None,
    declared_pod_formats: frozenset[str] = frozenset(),
    baseline_bindings: frozenset[str] = frozenset(),
    baseline_pods: tuple[ProcessingRule, ...] = (),
) -> ValidationReport:
    """Шестнадцать проверок одного снимка. Чтения файлов и модели КД 2 нет."""
    if isinstance(document, LayeredManager):
        from .ed_layers import validate_effective_links

        if context is None:
            raise ValueError("Для действующего представления нужен контекст направления")
        return validate_effective_links(document, context)
    if addresses is None or references is None:
        raise ValueError("Для документа нужны индексы адресов и ссылок")
    rows: list[_Row] = []
    skips: dict[tuple[str, str], None] = {}

    def skip(check: str, reason: str) -> None:
        skips.setdefault((check, reason), None)

    def emit(check: str, entity: Entity, message: str, address: str | None = None) -> None:
        if (check, entity.entity_id) in uncertain:
            skip(check, f"{_address(addresses, entity)}: условие сущности неизвестно")
            return
        key = None
        if isinstance(entity, HandlerBinding):
            key = (check, entity.owner_id, norm_name(entity.target_name), norm_name(entity.event))
        elif isinstance(entity, PropertyRule):
            key = (check, entity.owner_id, entity.namespace, "")
        elif isinstance(entity, (ObjectRule, ProcessingRule)):
            key = (check, entity.entity_id, norm_name(entity.name), "")
        if key in explained and (check, entity.entity_id) not in preserved:
            return
        rows.append(
            _Row(
                entity.span.file_id,
                entity.span.line_start,
                Level.ERROR if check in _ERRORS else Level.WARNING,
                check,
                address if address is not None else _address(addresses, entity),
                message,
            )
        )

    def emit_at(
        check: str,
        file_id: str,
        line: int,
        address: str,
        message: str,
    ) -> None:
        rows.append(
            _Row(
                file_id,
                line,
                Level.ERROR if check in _ERRORS else Level.WARNING,
                check,
                address,
                message,
            )
        )

    incomplete = _incomplete_reason(document)
    if incomplete is not None:
        skip(_INCOMPLETE, incomplete)

    by_entity = {entity.entity_id: entity for entity in document.entities()}
    routines: dict[str, list[Routine]] = defaultdict(list)
    for routine in document.routines:
        routines[routine.name.casefold()].append(routine)
    procedure_ids = {
        routine.entity_id
        for routine in document.routines
        if routine.name.casefold() == _PROCEDURE_DISPATCHER
    }
    function_ids = {
        routine.entity_id
        for routine in document.routines
        if routine.name.casefold() == _FUNCTION_DISPATCHER
    }
    cases: dict[str, list[DispatcherCase]] = defaultdict(list)
    for case in document.dispatcher_cases:
        cases[norm_name(case.literal_name)].append(case)
    pko_names = {norm_name(rule.name) for rule in document.pko} | declared_pko
    rule_names = pko_names | {norm_name(rule.name) for rule in document.pkpd} | declared_rules
    procedures = {norm_name(rule.procedure_name) for rule in (*document.pko, *document.pod)}
    send_ids = {
        use.rule_id
        for use in document.rule_uses
        if use.rule_id and use.direction in ("send", "both")
    }
    receive_ids = {
        use.rule_id
        for use in document.rule_uses
        if use.rule_id and use.direction in ("receive", "both")
    }
    unknown_direction = {
        use.rule_id for use in document.rule_uses if use.rule_id and use.direction is None
    }
    unknown_owners = {item.owner_id for item in document.unknown if item.owner_id}
    pod_formats = {
        rule.format_selection.value
        for rule in document.pod
        if rule.entity_id in receive_ids and rule.format_selection.value
    } | declared_pod_formats

    for name in _REQUIRED_EVENTS:
        if not routines.get(name.casefold()):
            emit(
                "ed.conversion.required",
                document.conversion,
                f"Обязательный метод конвертации «{name}» не определён.",
            )

    extended = False
    for rule in (*document.pko, *document.pod):
        owner_address = _address(addresses, rule)
        for binding in rule.events:
            if context is not None:
                from .ed_projection import event_direction

                if binding.entity_id not in baseline_bindings and event_direction(
                    binding.event
                ) not in (None, context.direction):
                    continue
            if not binding.target_name.strip():
                continue
            if binding.event == _EXTENDED_EVENT:
                extended = True
                continue
            expected = function_ids if binding.event == "ВыборкаДанных" else procedure_ids
            if binding.resolution == "unknown":
                skip(
                    "ed.handler.missing",
                    f"{owner_address}: путь вызова события {binding.event} неизвестен",
                )
                continue
            if binding.resolution == "invalid_signature":
                emit(
                    "ed.handler.missing",
                    binding,
                    f"Событие «{binding.event}» не может вызвать «{binding.target_name}»: "
                    "ветка требует другой способ вызова или ключи структуры параметров.",
                    owner_address,
                )
                continue
            matched = [
                case
                for case in cases.get(norm_name(binding.target_name), ())
                if case.dispatcher_id in expected
            ]
            if not matched:
                if expected & unknown_owners:
                    fragment = min(
                        (item for item in document.unknown if item.owner_id in expected),
                        key=lambda item: item.span.char_start,
                    )
                    skip(
                        "ed.handler.missing",
                        _hidden_reason(fragment.span, owner_address),
                    )
                else:
                    emit(
                        "ed.handler.missing",
                        binding,
                        _handler_missing_message(
                            binding.event,
                            binding.target_name,
                            bool(routines.get(binding.target_name.casefold())),
                        ),
                        owner_address,
                    )
            elif len(matched) > 1 or binding.resolution == "ambiguous":
                count = len(matched)
                if count <= 1:
                    count = max(len(routines.get(binding.target_name.casefold(), ())), 2)
                emit(
                    "ed.handler.ambiguous",
                    binding,
                    (
                        f"Связь события «{binding.event}» с «{binding.target_name}» "
                        f"неоднозначна: {count} кандидатов."
                    ),
                    owner_address,
                )
    if extended:
        skip(_EXTENDED, _EXTENDED_REASON)

    for case in document.dispatcher_cases:
        dispatcher = by_entity.get(case.dispatcher_id)
        dispatcher_address = (
            _address(addresses, dispatcher, "Диспетчер/") if dispatcher is not None else ""
        )
        parts = case.target.reference_parts
        if len(parts) > 1:
            skip(
                _EXTERNAL,
                (
                    f"Квалифицированная цель «{case.target.raw}» не разрешается "
                    f"в одном модуле: {dispatcher_address}, строка {case.span.line_start}"
                ),
            )
        elif len(parts) == 1 and not routines.get(parts[0].casefold()):
            emit(
                "ed.dispatcher.target_missing",
                case,
                (
                    f"Строка {case.span.line_start}: ветка «{case.literal_name}» вызывает "
                    f"отсутствующий локальный метод «{parts[0]}»."
                ),
                dispatcher_address,
            )

    for rule in document.pko:
        hidden = _unknown_of(document, rule.entity_id)
        for prop in _properties(rule):
            if prop.conversion_rule.strip() and norm_name(prop.conversion_rule) not in rule_names:
                if norm_name(prop.conversion_rule) in unknown_rules:
                    skip(
                        "ed.reference.property_rule_missing",
                        f"{_address(addresses, prop)}: определённость цели неизвестна",
                    )
                    continue
                emit(
                    "ed.reference.property_rule_missing",
                    prop,
                    (
                        f"ПКС ссылается на отсутствующее правило «{prop.conversion_rule}»; "
                        "проверены ПКО и ПКПД."
                    ),
                )
        for item in (*_properties(rule), *rule.groups):
            if not item.namespace or item.namespace in rule.extensions:
                continue
            if hidden is not None:
                skip(
                    "ed.extension.uninitialized",
                    _hidden_reason(hidden.span, _address(addresses, rule)),
                )
            else:
                emit(
                    "ed.extension.uninitialized",
                    item,
                    (
                        f"Пространство «{item.namespace}» свойства не инициализировано "
                        "в ПКО; проверьте активацию расширения."
                    ),
                )
        for uri in rule.extensions:
            if any(item.namespace == uri for item in (*_properties(rule), *rule.groups)):
                continue
            unread = (unread_properties or {}).get(rule.entity_id)
            if hidden is not None or unread:
                skip(
                    "ed.extension.unused",
                    _hidden_reason(hidden.span, _address(addresses, rule))
                    if hidden
                    else f"{_address(addresses, rule)}: ПКС прочитаны не полностью; {unread}",
                )
            else:
                emit(
                    "ed.extension.unused",
                    rule,
                    (
                        f"Расширение «{uri}» инициализировано, но не используется "
                        "декларативными свойствами ПКО."
                    ),
                )
        for group in rule.groups:
            for prop in group.properties:
                both_empty = not prop.configuration_property and not prop.format_property
                send_shape = bool(
                    group.configuration_property
                    and group.format_property
                    and not prop.format_property
                )
                if both_empty or (send_shape and rule.entity_id in send_ids):
                    emit(
                        "ed.group.empty_property",
                        prop,
                        (
                            f"У свойства группы «{group.name}» пустое имя для проверяемого "
                            "направления; проверьте заполнение."
                        ),
                    )
                elif (
                    send_shape
                    and rule.entity_id in unknown_direction
                    and rule.entity_id not in send_ids
                ):
                    skip(
                        "ed.group.empty_property",
                        f"Направление не определено: {_address(addresses, rule)}",
                    )
        config = rule.configuration_object.value
        if (
            any(item.algorithm_flag for item in _properties(rule))
            and not rule.events
            and rule.format_object.value
            and config is not None
            and config.literal_type != "undefined"
        ):
            if hidden is not None:
                skip(
                    "ed.algorithm.handler_missing",
                    _hidden_reason(hidden.span, _address(addresses, rule)),
                )
            else:
                emit(
                    "ed.algorithm.handler_missing",
                    rule,
                    (
                        "У ПКО алгоритмические ПКС, но нет привязок обработчиков; "
                        "заполнение требует проверки."
                    ),
                )
        identifying = rule.identification.value == "ПоУникальномуИдентификатору"
        if (
            identifying
            and rule.format_object.value
            and rule.entity_id in unknown_direction
            and rule.entity_id not in receive_ids
        ):
            skip(
                "ed.identity.uid_without_pod",
                f"Направление не определено: {_address(addresses, rule)}",
            )
        elif (
            identifying
            and rule.entity_id in receive_ids
            and rule.format_object.value
            and rule.format_object.value not in pod_formats
        ):
            emit(
                "ed.identity.uid_without_pod",
                rule,
                (
                    f"Получение ПКО «{rule.name}» использует только уникальный идентификатор "
                    f"без ПОД формата «{rule.format_object.value}»; "
                    "новый объект может не создаваться."
                ),
            )

    for rule in document.pod:
        for ref in rule.used_pko:
            if ref.name.strip() and norm_name(ref.name) not in pko_names:
                if norm_name(ref.name) in unknown_pko:
                    skip("ed.reference.pod_pko_missing", f"Цель ПКО неизвестна: {ref.name}")
                    continue
                emit(
                    "ed.reference.pod_pko_missing",
                    rule,
                    # XDTO:794–799 — при отправке пропуск; XDTO:7455–7475 — при получении ошибка.
                    (
                        f"ПОД использует отсутствующее ПКО «{ref.name}»: при отправке оно "
                        "пропускается, при получении — ошибка обмена; проверьте условия версии."
                    ),
                )

    for use in document.rule_uses:
        if use.rule_id is None and norm_name(use.target_name) not in procedures:
            # У вызова заполнения своего адреса нет: адрес — конвертация, место — строка.
            emit(
                "ed.reference.rule_use_missing",
                use,
                (
                    f"Строка {use.span.line_start}: в заполнении правил используется "
                    f"«{use.target_name}», определение не найдено."
                ),
                _address(addresses, document.conversion),
            )

    for case in deferred_cases(document):
        dispatcher = by_entity.get(case.dispatcher_id)
        dispatcher_address = (
            _address(addresses, dispatcher, "Диспетчер/") if dispatcher is not None else ""
        )
        for argument in case.arguments:
            if classify_deferred_argument(argument) != "invalid":
                continue
            emit_at(
                "ed.deferred.argument",
                argument.span.file_id,
                argument.span.line_start,
                dispatcher_address,
                (
                    f"Строка {argument.span.line_start}: аргумент «{argument.raw}» "
                    "отложенной ветки не входит в набор параметров исполнителя."
                ),
            )

    pods_by_handler: dict[str, list[ProcessingRule]] = defaultdict(list)
    used_pko_names = {
        rule.entity_id: {norm_name(item.name) for item in rule.used_pko if item.name.strip()}
        for rule in document.pod
    }
    for rule in (*document.pod, *baseline_pods):
        seen_targets: set[str] = set()
        for binding in rule.events:
            if binding.target_id and binding.target_id not in seen_targets:
                seen_targets.add(binding.target_id)
                if not any(
                    p.entity_id == rule.entity_id for p in pods_by_handler[binding.target_id]
                ):
                    pods_by_handler[binding.target_id].append(rule)

    for ref in references.entries:
        if ref.kind not in _CODE_KINDS or ref.name is None or not ref.name.strip():
            continue
        # XDTO:8402–8414 — исходные ключи берутся из ИспользуемыеПКО ПОД.
        # ПрочитатьСообщениеОбмена, XDTO:7455–7475: Вставить добавляет ключ;
        # исполнитель обходит структуру и ищет ПКО по имени, а не в исходном массиве.
        # XDTO:8450–8466 — ошибка ПриОбработке ставит отказ по объекту, обмен идёт дальше.
        if (
            ref.kind == "pod_use"
            and ref.access == "write"
            and ref.form in {"member", "index", "assignment"}
        ):
            owners = pods_by_handler.get(ref.owner_id, [])
            absent = [
                pod for pod in owners if norm_name(ref.name) not in used_pko_names[pod.entity_id]
            ]
            if absent:
                owner = by_entity.get(ref.owner_id)
                presence = "есть" if norm_name(ref.name) in pko_names else "нет"
                for pod in absent:
                    emit_at(
                        "ed.reference.pod_usage_key_missing",
                        ref.span.file_id,
                        ref.span.line_start,
                        _address(addresses, owner) if owner is not None else "",
                        (
                            f"Строка {ref.span.line_start}: ключ «{ref.name}» отсутствует "
                            f"в ИспользуемыеПКО ПОД «{pod.name}». "
                            f"ПКО с именем «{ref.name}» в модуле {presence}. "
                            "При выполнении этой строки — ошибка в обработчике, отказ по объекту, "
                            "обмен продолжается с ошибками."
                        ),
                    )
                continue
        pool = rule_names if ref.kind == "instruction_rule" else pko_names
        if norm_name(ref.name) in pool:
            continue
        owner = by_entity.get(ref.owner_id)
        unknown_pool = unknown_rules if ref.kind == "instruction_rule" else unknown_pko
        if norm_name(ref.name) in unknown_pool:
            skip(
                "ed.reference.code_rule_missing",
                f"{_address(addresses, owner) if owner else ref.owner_id}: "
                f"определённость цели «{ref.name}» неизвестна",
            )
            continue
        emit_at(
            "ed.reference.code_rule_missing",
            ref.span.file_id,
            ref.span.line_start,
            _address(addresses, owner) if owner is not None else "",
            (
                f"Строка {ref.span.line_start}: {_CODE_LABEL[ref.kind]} ссылается "
                f"на отсутствующее правило «{ref.name}»."
            ),
        )

    for label, rules in (("ПКО", document.pko), ("ПОД", document.pod)):
        grouped: dict[str, list[Entity]] = defaultdict(list)
        for rule in rules:
            grouped[norm_name(rule.name)].append(rule)
        for group in grouped.values():
            if len(group) < 2:
                continue
            for rule in group:
                emit(
                    "ed.rule.duplicate",
                    rule,
                    (
                        f"Имя {label} «{rule.name}» объявлено повторно; "
                        "условия применимости не вычислялись."
                    ),
                )

    report = ValidationReport()
    seen: set[tuple[str, str, int, str]] = set()
    for row in sorted(rows, key=_order):
        key = (row.check, row.address, row.line, row.message)
        if key in seen:
            continue
        seen.add(key)
        if row.level is Level.ERROR:
            report.error(row.check, row.address, row.message)
        else:
            report.warning(row.check, row.address, row.message)
    for check, reason in sorted(skips):
        report.skip(check, reason)
    return report


def _handler_missing_message(event: str, name: str, procedure_exists: bool) -> str:
    """Ветки нет. Процедура в модуле и её отсутствие — разные исправления.

    Для «ПослеЗагрузкиВсехДанных» вызов проходит вхолостую, признак изменения
    остаётся включённым, и объект записывается ещё раз (XDTO:7196–7207).
    """
    if procedure_exists:
        gap = "ветки диспетчера нет, процедура в модуле есть. Добавьте ветку диспетчера"
    else:
        gap = "нет ни ветки диспетчера, ни процедуры. Правило называет обработчик, которого нет"
    if event.casefold() == "послезагрузкивсехданных":
        effect = "Обмен не прерывается, объект записывается повторно."
    else:
        effect = "Обработчик не будет вызван."
    return f"Для события «{event}» правило называет «{name}»: {gap}. {effect}"


def _order(item: _Row) -> tuple[str, int, str, str, str]:
    return (item.file_id, item.line, item.check, item.address, item.message)


def _properties(rule: ObjectRule) -> tuple[PropertyRule, ...]:
    return (*rule.properties, *(item for group in rule.groups for item in group.properties))


def _address(index: AddressIndex, entity: Entity, *prefixes: str) -> str:
    found = index.by_id.get(entity.entity_id, ())
    for prefix in prefixes:
        for item in found:
            if item.startswith(prefix):
                return item
    if found:
        return found[0]
    return f"{entity.kind}/{escape_segment(entity.entity_id)}"


def _incomplete_reason(document: EdDocument) -> str | None:
    codes = sorted({item.code for item in document.diagnostics})
    reasons = sorted({item.reason for item in document.unknown})
    if not codes and not reasons:
        return None
    parts: list[str] = []
    if codes:
        parts.append("коды " + ", ".join(codes))
    if reasons:
        parts.append("неизвестные фрагменты: " + ", ".join(reasons))
    return "Неполное чтение: " + "; ".join(parts)


def _unknown_of(document: EdDocument, owner_id: str) -> UnknownFragment | None:
    found = [item for item in document.unknown if item.owner_id == owner_id]
    if not found:
        return None
    return min(found, key=lambda item: item.span.char_start)


def _hidden_reason(span: SourceSpan, address: str) -> str:
    return (
        f"Неизвестный фрагмент {span.file_id}:{span.line_start}-{span.line_end} "
        f"мог содержать определение; адрес {address}"
    )
