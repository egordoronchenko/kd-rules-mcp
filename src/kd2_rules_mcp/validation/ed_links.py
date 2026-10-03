"""Проверки связности прочитанного модуля менеджера EnterpriseData.

Ошибка — только то, что в модуле не исполнится или не скомпилируется.
Остальное — предупреждение: данные, условия и тела обработчиков не доказываются.
Схема формата и структура конфигурации в этом срезе не передаются.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

from kd2_rules_mcp.ed.address import AddressIndex, escape_segment
from kd2_rules_mcp.ed.model import (
    DispatcherCase,
    EdDocument,
    Entity,
    ObjectRule,
    PropertyRule,
    Routine,
    SourceSpan,
    UnknownFragment,
)
from kd2_rules_mcp.ed.refs import (
    ReferenceIndex,
    classify_deferred_argument,
    deferred_cases,
    norm_name,
)
from kd2_rules_mcp.validation.report import Level, ValidationReport

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
    document: EdDocument,
    addresses: AddressIndex,
    references: ReferenceIndex,
) -> ValidationReport:
    """Пятнадцать проверок одного снимка. Чтения файлов и модели КД 2 нет."""
    rows: list[_Row] = []
    skips: dict[tuple[str, str], None] = {}

    def skip(check: str, reason: str) -> None:
        skips.setdefault((check, reason), None)

    def emit(check: str, entity: Entity, message: str, address: str | None = None) -> None:
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
    pko_names = {norm_name(rule.name) for rule in document.pko}
    rule_names = pko_names | {norm_name(rule.name) for rule in document.pkpd}
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
    }

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
            if not binding.target_name.strip():
                continue
            if binding.event == _EXTENDED_EVENT:
                extended = True
                continue
            expected = function_ids if binding.event == "ВыборкаДанных" else procedure_ids
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
                        (
                            f"Для события «{binding.event}» правило называет "
                            f"«{binding.target_name}», но ветка диспетчера не найдена: "
                            "обработчик не будет вызван."
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
            if hidden is not None:
                skip(
                    "ed.extension.unused",
                    _hidden_reason(hidden.span, _address(addresses, rule)),
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

    for ref in references.entries:
        if ref.kind not in _CODE_KINDS or ref.name is None or not ref.name.strip():
            continue
        pool = rule_names if ref.kind == "instruction_rule" else pko_names
        if norm_name(ref.name) in pool:
            continue
        owner = by_entity.get(ref.owner_id)
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
