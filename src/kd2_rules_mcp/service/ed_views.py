"""Компактные проекции ED: скаляры, страницы и ограниченный исходный текст."""

import re
from collections import Counter
from dataclasses import fields
from typing import Any

from kd2_rules_mcp import ed
from kd2_rules_mcp.ed.address import AddressIndex, escape_segment
from kd2_rules_mcp.ed.refs import KINDS as REFERENCE_KINDS
from kd2_rules_mcp.ed.refs import EdReference, ReferenceIndex
from kd2_rules_mcp.service.views import report_summary, slice_rows
from kd2_rules_mcp.validation.report import Level, Skipped, ValidationReport

KINDS = frozenset(
    [
        "pko",
        "pks",
        "pktch",
        "pod",
        "pkpd",
        "parameter",
        "algorithm",
        "handler",
        "dispatcher",
        "support",
        "unknown",
        "version",
        "diagnostic",
    ]
)
ROLES = frozenset(["algorithm", "handler", "dispatcher", "support"])
CODE_REFERENCE_ROLES = frozenset({"handler", "algorithm", "event"})
_RAW_LIMIT = 160


def validate_page(offset: int, limit: int, maximum: int = 200) -> None:
    """Ошибочные пределы не зажимаются и получают invalid_argument."""
    if type(offset) is not int or offset < 0:
        raise ValueError("Смещение должно быть целым неотрицательным числом")
    if type(limit) is not int or not 1 <= limit <= maximum:
        raise ValueError(f"Размер страницы должен быть от 1 до {maximum}")


def page(rows: Any, offset: int = 0, limit: int = 50) -> dict[str, Any]:
    validate_page(offset, limit)
    return slice_rows(rows, offset, limit)


def span_view(span: ed.SourceSpan) -> dict[str, Any]:
    return {f.name: getattr(span, f.name) for f in fields(span)}


def short(value: str, span: ed.SourceSpan | None = None) -> Any:
    if len(value) <= 2000:
        return value
    result = {"preview": value[:2000], "truncated": True, "total_chars": len(value)}
    if span is not None:
        result["span"] = span_view(span)
    return result


def scalar(value: Any) -> Any:
    """Вложенные выражения не разворачиваются в AST или списки присваиваний."""
    if isinstance(value, ed.Expr):
        return {
            "raw": short(value.raw, value.span),
            "literal_type": value.literal_type,
            "literal_value": scalar(value.literal_value),
            "span": span_view(value.span),
        }
    if isinstance(value, ed.Field):
        return {"presence": value.presence, "value": scalar(value.value)}
    if isinstance(value, str):
        return short(value)
    return value


def entity_fields(entity: ed.Entity) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for f in fields(entity):
        value = getattr(entity, f.name)
        if f.name in {"raw_text", "span", "body_span", "entity_id", "kind", "status"}:
            continue
        if isinstance(value, (tuple, frozenset)):
            continue
        result[f.name] = (
            short(value, entity.span)
            if isinstance(value, str) and f.name.endswith("_raw")
            else scalar(value)
        )
    from kd2_rules_mcp.ed.layer_model import LayerHandlerBinding

    if isinstance(entity, LayerHandlerBinding) and entity.body_changes:
        result["body_modified"] = entity.body_modified
        result["body_changes"] = [
            {
                "kind": change.kind,
                "target": change.target_name,
                "layer_id": change.origin.layer_id,
                "file_id": change.origin.file_id,
                "span": span_view(change.origin.span),
            }
            for change in entity.body_changes
        ]
    return result


def address_of(entity: ed.Entity, index: AddressIndex) -> str:
    """Индекс пакета первичен; служебные сущности получают адрес по стабильному ID."""
    addresses = index.by_id.get(entity.entity_id)
    if addresses:
        return addresses[0]
    prefix = {"version": "Версия", "diagnostic": "Диагностика"}.get(entity.kind, entity.kind)
    return f"{prefix}/{escape_segment(entity.entity_id)}"


def sides(entity: ed.Entity, entities: dict[str, ed.Entity]) -> tuple[str | None, str | None]:
    owner = entities.get(getattr(entity, "owner_id", ""), entity)
    if isinstance(owner, ed.ObjectRule):
        metadata, format_name = owner.configuration_object, owner.format_object
    elif isinstance(owner, ed.ProcessingRule):
        metadata, format_name = owner.configuration_selection, owner.format_selection
    elif isinstance(owner, ed.PredefinedRule):
        metadata, format_name = owner.configuration_type, owner.format_type
    else:
        return None, None
    expr = metadata.value
    configuration = None
    if metadata.presence != "ambiguous" and expr is not None:
        if expr.literal_type == "string":
            configuration = str(expr.literal_value)
        elif len(expr.reference_parts) == 3 and expr.reference_parts[0].casefold() == "метаданные":
            types = {
                "справочники": "Справочник",
                "документы": "Документ",
                "перечисления": "Перечисление",
                "регистрысведений": "РегистрСведений",
                "регистрынакопления": "РегистрНакопления",
                "регистрыбухгалтерии": "РегистрБухгалтерии",
                "регистрырасчета": "РегистрРасчета",
                "планывидовхарактеристик": "ПланВидовХарактеристик",
                "планысчетов": "ПланСчетов",
                "планывидоврасчета": "ПланВидовРасчета",
                "бизнеспроцессы": "БизнесПроцесс",
                "задачи": "Задача",
                "планыобмена": "ПланОбмена",
            }
            metadata_kind = types.get(expr.reference_parts[1].casefold())
            if metadata_kind is not None:
                configuration = f"{metadata_kind}.{expr.reference_parts[2]}"
    return configuration, format_name.value if format_name.presence != "ambiguous" else None


def row(
    entity: ed.Entity, index: AddressIndex, entities: dict[str, ed.Entity], kind: str | None = None
) -> dict[str, Any]:
    configuration, format_name = sides(entity, entities)
    result: dict[str, Any] = {
        "address": address_of(entity, index),
        "kind": kind or entity.kind,
        "name": short(entity.name),
        "file_id": entity.span.file_id,
        "line_start": entity.span.line_start,
        "line_end": entity.span.line_end,
        "status": entity.status.value,
    }
    if kind in ROLES and isinstance(entity, ed.Routine):
        prefix = {
            "algorithm": "Алгоритм",
            "handler": "Обработчик",
            "dispatcher": "Диспетчер",
            "support": "Служебный",
        }[kind]
        result["address"] = next(
            (a for a in index.by_id.get(entity.entity_id, ()) if a.startswith(prefix + "/")),
            result["address"],
        )
    if isinstance(entity, (ed.PropertyRule, ed.PropertyGroup)):
        # Объект правила уже есть в адресе; в строке свойства нужны сами свойства сторон.
        result["configuration_property"] = short(entity.configuration_property)
        result["format_property"] = short(entity.format_property)
        if entity.namespace:
            result["namespace"] = entity.namespace
    else:
        if configuration is not None:
            result["configuration_object"] = short(configuration)
        if format_name is not None:
            result["format_object"] = short(format_name)
    if not isinstance(entity, ed.PropertyRule):
        # У ПКС «дети» — только разбор аргументов вызова; в строке списка они шум.
        children, _ = direct_children(entity, entities)
        if children:
            result["child_count"] = len(children)
    return result


def direct_children(
    entity: ed.Entity, entities: dict[str, ed.Entity]
) -> tuple[list[Any], set[str]]:
    """Только непосредственные дети; ПКС группы не входят в детей ПКО."""
    attributes: dict[type, tuple[str, ...]] = {
        ed.ObjectRule: ("properties", "groups", "search_sets", "events", "extensions"),
        ed.PropertyGroup: ("properties",),
        ed.ProcessingRule: ("events", "used_pko"),
        ed.PredefinedRule: ("mappings",),
        ed.Conversion: ("events", "entrypoints", "format_version_mentions"),
        ed.Routine: ("parameters",),
        ed.SearchSet: ("fields",),
        ed.PropertyRule: ("raw_arguments", "argument_presence"),
    }
    kinds = {
        "properties": "pks",
        "groups": "pktch",
        "search_sets": "search",
        "events": "binding",
        "extensions": "extension",
        "used_pko": "used_pko",
        "mappings": "value",
        "entrypoints": "entrypoint",
        "format_version_mentions": "version",
        "parameters": "formal_parameter",
        "fields": "field",
        "raw_arguments": "argument",
        "argument_presence": "argument_presence",
    }
    attrs = attributes.get(type(entity), ())
    children = [(kinds[attr], item) for attr in attrs for item in getattr(entity, attr)]
    allowed = {kinds[attr] for attr in attrs}
    if isinstance(entity, (ed.ObjectRule, ed.ProcessingRule, ed.PredefinedRule, ed.Routine)):
        allowed.add("unknown")
        children.extend(
            ("unknown", child)
            for child in entities.values()
            if isinstance(child, ed.UnknownFragment) and child.owner_id == entity.entity_id
        )
    if isinstance(entity, ed.Routine) and "dispatcher" in entity.roles:
        allowed.add("case")
        children.extend(
            ("case", child)
            for child in entities.values()
            if isinstance(child, ed.DispatcherCase) and child.dispatcher_id == entity.entity_id
        )
    children.sort(
        key=lambda pair: getattr(
            getattr(pair[1], "span", None), "char_start", entity.span.char_start
        )
    )
    return children, allowed


def child_view(
    kind: str, item: Any, index: AddressIndex, entities: dict[str, ed.Entity]
) -> dict[str, Any]:
    if isinstance(item, ed.Entity):
        return {"address": address_of(item, index), "kind": item.kind, "name": short(item.name)}
    if isinstance(item, ed.Expr):
        return {"kind": kind, **scalar(item)}
    if isinstance(item, (ed.FormalParameter, ed.RuleRef)):
        return {
            "kind": kind,
            **{
                f.name: span_view(item.span) if f.name == "span" else scalar(getattr(item, f.name))
                for f in fields(item)
            },
        }
    return {"kind": kind, "value": scalar(item)}


def accepts_code_references(entity: ed.Entity) -> bool:
    """Ссылки из кода показываются у обработчика, алгоритма и события."""
    return isinstance(entity, ed.Routine) and bool(entity.roles & CODE_REFERENCE_ROLES)


def reference_row(item: EdReference) -> dict[str, Any]:
    """Строка ссылки: у вычисляемого имени — признак и короткий исходный фрагмент."""
    row: dict[str, Any] = {
        "kind": item.kind,
        "name": item.name,
        "form": item.form,
        "access": item.access,
        "direction": item.direction,
        "line_start": item.span.line_start,
        "line_end": item.span.line_end,
    }
    if item.name is None:
        row["unparsed"] = True
        row["raw"] = item.raw[:_RAW_LIMIT]
    return row


def references_summary(index: ReferenceIndex) -> dict[str, Any]:
    """Счётчики индекса: все семь видов, включая нули."""
    return {
        "known": index.known,
        "unparsed": index.unparsed,
        "unparsed_by_kind": {kind: index.unparsed_by_kind[kind] for kind in REFERENCE_KINDS},
        "deferred_argument_unparsed": index.deferred_argument_unparsed,
    }


def summary(document: ed.EdDocument) -> dict[str, Any]:
    return {
        "code": dict(Counter(d.code for d in document.diagnostics)),
        "severity": dict(Counter(d.severity for d in document.diagnostics)),
    }


# Адрес в тексте пропуска: у модели Skipped отдельного поля нет, адреса лежат в причине.
_SKIPPED_ADDRESS = re.compile(
    r"(?<![\w/])(?:Конвертация\b|"
    r"(?:Слой|Действующее|ПКО|ПОД|ПКПД|Параметр|Алгоритм|Обработчик|Служебный|Диспетчер|Событие|Неизвестное)"
    r"(?:/[^,\s;]+)+)"
)


def address_matches(address: str, prefix: str) -> bool:
    """Адрес равен префиксу или продолжается через «/». Регистр не различается."""
    folded = address.casefold()
    head = prefix.casefold()
    return folded == head or folded.startswith(head + "/")


def skipped_addresses(reason: str) -> tuple[str, ...]:
    """Адреса, которые удалось прочитать из текста причины пропуска."""
    return tuple(_SKIPPED_ADDRESS.findall(reason))


def _skipped_matches(item: Skipped, check_prefix: str | None, address_prefix: str | None) -> bool:
    if check_prefix and not item.check.startswith(check_prefix):
        return False
    if not address_prefix:
        return True
    found = skipped_addresses(item.reason)
    return bool(found) and any(address_matches(address, address_prefix) for address in found)


def validation_view(
    report: ValidationReport,
    level: str | None,
    check_prefix: str | None,
    address_prefix: str | None,
    section: str,
    offset: int,
    limit: int,
    *,
    explain_skipped: bool = False,
) -> dict[str, Any]:
    """Отчёт ed_validate. Итог — по всему отчёту; отборы меняют только запрошенный раздел.

    `issues` отдаёт страницу замечаний и сводку пропусков без текстов причин.
    `skipped` отдаёт страницу пропусков и не отдаёт замечания.
    """
    view: dict[str, Any] = {"summary": report_summary(report)}
    if section == "skipped":
        rows = [
            item.to_dict()
            for item in report.skipped
            if _skipped_matches(item, check_prefix, address_prefix)
        ]
        if explain_skipped:
            for row in rows:
                hint = {
                    "non_atomic_type": "Тип не подтверждён как одиночный примитив; "
                    "задайте ПКО ссылки или алгоритм преобразования",
                    "qualifiers_unavailable": "Квалификаторы типа неизвестны; проверьте структуру "
                    "и ограничения схемы или задайте алгоритм",
                    "handler_may_supply": "Обработчик может менять значение; "
                    "проверьте его код и тип результата",
                    "owner_type_unavailable": "Откройте схему нужной версии "
                    "и проверьте тип формата ПКО",
                    "unresolved_configuration_type": "Тип реквизита не разрешён; "
                    "обновите структуру с нужными расширениями",
                }.get(row["reason"].partition(":")[0])
                if hint:
                    row["hint"] = hint
        view["skipped"] = slice_rows(rows, offset, limit)
        return view
    issues = [issue.to_dict() for issue in report.issues]
    if level:
        issues = [issue for issue in issues if issue["level"] == Level(level).value]
    if check_prefix:
        issues = [issue for issue in issues if issue["check"].startswith(check_prefix)]
    if address_prefix:
        issues = [issue for issue in issues if address_matches(issue["address"], address_prefix)]
    by_check: dict[str, int] = {}
    for item in report.skipped:
        by_check[item.check] = by_check.get(item.check, 0) + 1
    view["skipped"] = {"total": len(report.skipped), "by_check": by_check}
    view["issues"] = slice_rows(issues, offset, limit)
    return view
