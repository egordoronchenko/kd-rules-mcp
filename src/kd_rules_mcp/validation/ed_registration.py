"""Проекция менеджера регистрации на проверки `RegistrationRules`.

Проекция временная: она не возвращается и не пригодна для `rules_save` / `dump_rules`.
В менеджере нет состава плана и, пока тип объекта не однозначен, нет объекта настройки.
"""

from __future__ import annotations

import sqlite3

from lxml import etree as ET

from kd_rules_mcp.ed.address import escape_segment
from kd_rules_mcp.ed.model import Expr, Field
from kd_rules_mcp.ed.registration_model import (
    RegistrationModuleDocument,
    RegistrationRule,
    XmlNode,
)
from kd_rules_mcp.kd2.model import Node, RegistrationRules
from kd_rules_mcp.kd2.schema import Kind, ValueType, kind
from kd_rules_mcp.kd2.xmlstyle import KD_STYLE
from kd_rules_mcp.validation.address import rule_address as kd_rule_address
from kd_rules_mcp.validation.registration import check_registration
from kd_rules_mcp.validation.report import Issue, Skipped, ValidationReport

# Проверки, которые `_load_plan` снимает, когда имя плана пустое.
_PLAN_DEPENDENT = (
    "registration.plan_membership",
    "registration.plan_property",
    "registration.unload_mode",
    "registration.plan_content",
)
_PLAN_UNKNOWN = "план обмена не определён"


def check_registration_module(
    document: RegistrationModuleDocument, structure: sqlite3.Connection | None
) -> ValidationReport:
    """Проверяет известную часть менеджера существующими проверками регистрации.

    Адреса замечаний переносятся явной картой в `Регистрация/ПРО/<идентификатор>`.
    Нет структуры — обычные пропуски. Нет состава плана — `registration.plan_content`
    пропускается, пустой состав не подставляется.
    """
    projected, mapping, plan_name, parameter_plan = _project(document)
    report = check_registration(
        projected,
        structure,
        has_plan_content=False,
        has_object_settings=False,
    )
    if structure is not None and plan_name is None:
        _unresolved_plan(report)
    remapped = _remap(report, mapping)
    _warn_plan_mismatch(remapped, document, parameter_plan)
    _warn_unknown_tags(remapped, document)
    _skip_unreadable(remapped, document)
    return remapped


def _project(
    document: RegistrationModuleDocument,
) -> tuple[RegistrationRules, dict[str, str], str | None, str | None]:
    root = Node.new("registration_rules", "ПравилаРегистрации")
    parameter_plan = _literal_parameter_plan(document)
    # Исполнитель берёт план отбора из правила, не из параметра регистрации.
    plan_name = _shared_rule_plan(document)
    mapping: dict[str, str] = {}
    if plan_name:
        plan = Node.new("exchange_plan", "ПланОбмена")
        plan.attrs["Имя"] = plan_name
        plan.text = plan_name
        root.children["ПланОбмена"] = plan
        mapping[f"ПланОбмена «{plan_name}»"] = f"Регистрация/ПланОбмена/{escape_segment(plan_name)}"
    else:
        mapping["ПланОбмена «не указан»"] = "Регистрация/ПланОбмена/~empty"
    section = Node(kind("pro_list"), "ПравилаРегистрацииОбъектов")
    for rule in document.rules:
        node = _rule_node(rule)
        section.items.append(node)
        mapping[kd_rule_address(node)] = f"Регистрация/ПРО/{rule.qualified_id}"
    root.children["ПравилаРегистрацииОбъектов"] = section
    return RegistrationRules(root, KD_STYLE), mapping, plan_name, parameter_plan


def _literal_parameter_plan(document: RegistrationModuleDocument) -> str | None:
    for item in document.parameters:
        if item.name != "ПланаОбмена":
            continue
        return _literal_text(item.field)
    return None


def _literal_text(field: Field[Expr]) -> str | None:
    value = field.value
    if (
        field.presence == "literal"
        and value is not None
        and value.literal_type == "string"
        and isinstance(value.literal_value, str)
        and value.literal_value
    ):
        return value.literal_value
    return None


def _shared_rule_plan(document: RegistrationModuleDocument) -> str | None:
    """Одно литеральное имя плана на все правила. Иначе план для проверки не определён."""
    if not document.rules:
        return None
    names: list[str] = []
    for rule in document.rules:
        if rule.plan_name.presence != "literal" or not isinstance(rule.plan_name.value, str):
            return None
        if not rule.plan_name.value:
            return None
        names.append(rule.plan_name.value)
    unique = set(names)
    if len(unique) != 1:
        return None
    return names[0]


def _rule_node(rule: RegistrationRule) -> Node:
    node = Node.new("pro", "Правило")
    # Экранированный идентификатор уникален даже когда сырой код «X#1» столкнулся бы с суффиксом.
    node.values["Код"] = rule.qualified_id
    _put_str(node, "ОбъектМетаданныхИмя", rule.metadata_name.value, rule.metadata_name.presence)
    _put_str(node, "РеквизитРежимаВыгрузки", rule.unload_flag.value, rule.unload_flag.presence)
    for tree, tag, kind_name in (
        (rule.plan_filter, "ОтборПоСвойствамПланаОбмена", "plan_filter"),
        (rule.object_filter, "ОтборПоСвойствамОбъекта", "object_filter"),
    ):
        if tree is None or tree.error or tree.tree is None:
            continue
        node.children[tag] = _convert(tree.tree, kind(kind_name))
    return node


def _put_str(node: Node, tag: str, value: object, presence: str) -> None:
    if presence == "literal" and isinstance(value, str):
        node.values[tag] = value


def _convert(node: XmlNode, node_kind: Kind) -> Node:
    result = Node(node_kind, node.tag)
    for child in node.children:
        leaf = node_kind.leaves.get(child.tag)
        if leaf is not None and not child.children:
            if leaf.type is ValueType.BOOL:
                text = child.text.strip()
                if text in ("true", "false"):
                    result.values[child.tag] = text == "true"
                else:
                    result.unknown.append(_to_element(child))
            else:
                result.values[child.tag] = child.text
            continue
        if child.tag in node_kind.children:
            result.children[child.tag] = _convert(child, kind(node_kind.children[child.tag].kind))
            continue
        if child.tag in node_kind.items:
            result.items.append(_convert(child, kind(node_kind.items[child.tag])))
            continue
        result.unknown.append(_to_element(child))
    return result


def _to_element(node: XmlNode) -> ET._Element:
    element = ET.Element(node.tag, dict(node.attrib))
    if node.text and not node.children:
        element.text = node.text
    for child in node.children:
        element.append(_to_element(child))
    return element


def _remap(report: ValidationReport, mapping: dict[str, str]) -> ValidationReport:
    result = ValidationReport()
    for issue in report.issues:
        result.issues.append(
            Issue(
                issue.level,
                issue.check,
                mapping.get(issue.address, issue.address),
                issue.message,
            )
        )
    result.skipped.extend(report.skipped)
    return result


def _unresolved_plan(report: ValidationReport) -> None:
    """Пустое имя плана — не «план не найден», а пропуск: имя не удалось прочитать."""
    report.issues = [
        issue for issue in report.issues if issue.check != "registration.exchange_plan"
    ]
    rewritten: list[Skipped] = []
    seen = False
    for item in report.skipped:
        if item.check in _PLAN_DEPENDENT:
            rewritten.append(Skipped(item.check, _PLAN_UNKNOWN))
        else:
            rewritten.append(item)
        if item.check == "registration.exchange_plan":
            seen = True
    if not seen:
        rewritten.append(Skipped("registration.exchange_plan", _PLAN_UNKNOWN))
    report.skipped = rewritten


def _warn_plan_mismatch(
    report: ValidationReport, document: RegistrationModuleDocument, parameter: str | None
) -> None:
    if not parameter:
        return
    for rule in document.rules:
        plan = rule.plan_name.value
        if (
            rule.plan_name.presence == "literal"
            and isinstance(plan, str)
            and plan
            and plan != parameter
        ):
            report.warning(
                "registration.exchange_plan",
                f"Регистрация/ПРО/{rule.qualified_id}",
                f"Правило «{rule.qualified_id}»: имя плана обмена «{plan}»"
                f" отличается от параметра «{parameter}»",
            )


def _warn_unknown_tags(report: ValidationReport, document: RegistrationModuleDocument) -> None:
    """Исполнитель неизвестный тег пропускает, поэтому чтение верно, но опечатка видна."""
    for rule in document.rules:
        for tree in (rule.plan_filter, rule.object_filter):
            if tree is None or tree.error:
                continue
            for child in tree.unknown_children:
                report.warning(
                    "registration.unknown_tag",
                    f"Регистрация/ПРО/{rule.qualified_id}",
                    f"Правило «{rule.qualified_id}»: неизвестный тег «{child.tag}»",
                )


def _skip_unreadable(report: ValidationReport, document: RegistrationModuleDocument) -> None:
    for rule in document.rules:
        code = rule.qualified_id
        for tree, check in (
            (rule.plan_filter, "registration.plan_property"),
            (rule.object_filter, "registration.object_property"),
        ):
            if tree is None:
                report.skip(check, f"{code}: отбор не прочитан")
            elif tree.error:
                report.skip(
                    check,
                    f"{code}, строка {tree.literal_span.line_start}: {tree.error}",
                )
    if not document.unknown:
        return
    shown = ", ".join(str(item.span.line_start) for item in document.unknown[:5])
    if len(document.unknown) > 5:
        shown += f" и ещё {len(document.unknown) - 5}"
    report.skip(
        "registration.unknown",
        f"неизвестных фрагментов: {len(document.unknown)}; первые строки: {shown}",
    )
