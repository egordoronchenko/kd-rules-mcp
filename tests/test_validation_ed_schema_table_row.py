"""Имя строки табличной части при получении и точное имя ПКПД при отправке."""

from dataclasses import replace
from pathlib import Path

import pytest

from kd_rules_mcp.ed.address import build_addresses
from kd_rules_mcp.ed.schema import load_schema
from kd_rules_mcp.ed.schema.profile import ValidationProfile
from kd_rules_mcp.validation.ed_schema import validate_schema
from tests.test_ed_profile import DATA
from tests.test_validation_ed_reference_types import reference_case
from tests.test_validation_ed_schema import check
from tests.test_validation_ed_structure import table_text

ROW_PROPERTY = '<property name="Строка" type="t:Row" upperBound="-1"/>'
ITEM_PROPERTY = '<property name="Item" type="t:Row" upperBound="-1"/>'
ROW_MESSAGE = (
    "Строки таблицы «Товары» в формате называются «Item»: "
    "исполнитель при получении читает только свойство «Строка» "
    "(XDTO:6428–6451), строки не загрузятся."
)


def item_schema(tmp_path: Path):
    """Копия validation.bin: у типа Rows строка называется Item, не Строка."""
    text = (DATA / "validation.bin").read_text(encoding="utf-8")
    rows = text.index('<objectType name="Rows">')
    bad = text.index('<objectType name="BadRows">')
    at = text.index(ROW_PROPERTY)
    assert rows < at < bad
    path = tmp_path / "item.bin"
    path.write_text(text.replace(ROW_PROPERTY, ITEM_PROPERTY, 1), encoding="utf-8")
    return load_schema(path)


def row_name_issues(report):
    return [issue for issue in report.issues if issue.check == "ed.schema.table_row_name"]


@pytest.mark.parametrize(
    ("direction", "expected"),
    [("receive", True), ("send", False), ("both", True)],
)
def test_item_row_warns_only_when_receive_is_checked(tmp_path: Path, direction, expected) -> None:
    report = check(table_text(), direction, item_schema(tmp_path))
    issues = row_name_issues(report)
    if expected:
        assert len(issues) == 1
        assert issues[0].level.value == "предупреждение"
        assert issues[0].address == "ПКО/Тест/ПКТЧ/Товары"
        assert issues[0].message == ROW_MESSAGE
    else:
        assert issues == []
        assert not any(item.check == "ed.schema.table_row_name" for item in report.skipped)


def test_row_named_stroka_is_not_warned() -> None:
    report = check(table_text(), "receive")
    assert row_name_issues(report) == []
    assert not any(item.check == "ed.schema.table_row_name" for item in report.skipped)


def test_not_table_keeps_table_missing_without_row_name() -> None:
    report = check(table_text().replace('"Товары");', '"Плохие");'), "receive")
    missing = [issue for issue in report.issues if issue.check == "ed.schema.table_missing"]
    assert len(missing) == 1
    assert missing[0].address == "ПКО/Тест/ПКТЧ/Плохие"
    assert missing[0].message == "Группа «Плохие» не разрешается как табличная часть формата."
    assert row_name_issues(report) == []


def _send_reference_report(pkpd_name: str):
    """ПКС ссылается на «Выбор», ПКО называется так же; имя ПКПД задаёт тест.

    Без ПКО с тем же именем проверка ссылки молча выходит: следствие строки сравнения
    видно только когда ПКПД больше не закрывает эту ссылку и срабатывает
    ed.schema.reference_type_partial.
    """
    doc, snap = reference_case(target="Выбор")
    target = replace(doc.pko[1], name="Выбор")
    pkpd = replace(doc.pkpd[0], name=pkpd_name)
    doc = replace(doc, pko=(doc.pko[0], target), pkpd=(pkpd,))
    schema = load_schema(DATA / "validation.bin")
    return validate_schema(
        doc,
        schema,
        build_addresses(doc),
        ValidationProfile.build(schema, "1.2", "send"),
        snap,
    )


def test_pkpd_different_case_no_longer_supplies_send_value() -> None:
    mismatched = [
        issue
        for issue in _send_reference_report("выбор").issues
        if issue.check == "ed.schema.reference_type_partial"
    ]
    assert len(mismatched) == 1
    assert mismatched[0].level.value == "предупреждение"
    assert mismatched[0].address == "ПКО/Тест/ПКС/Код"
    exact = _send_reference_report("Выбор")
    assert not any(issue.check == "ed.schema.reference_type_partial" for issue in exact.issues)


def test_pko_lookup_by_property_rule_is_exact() -> None:
    """ПКО «выбор» не цель ссылки «Выбор»: проверка типов ссылки не применяется, а связность
    сообщает об отсутствующем правиле с подсказкой близкого имени (kd3-silent-fixes, Р5)."""
    from kd_rules_mcp.ed.refs import build_references
    from kd_rules_mcp.validation.ed_links import validate_links

    doc, snap = reference_case(target="Выбор")
    target = replace(doc.pko[1], name="выбор")
    pkpd = replace(doc.pkpd[0], name="Другое")
    doc = replace(doc, pko=(doc.pko[0], target), pkpd=(pkpd,))
    schema = load_schema(DATA / "validation.bin")
    report = validate_schema(
        doc, schema, build_addresses(doc), ValidationProfile.build(schema, "1.2", "send"), snap
    )
    assert not any(issue.check == "ed.schema.reference_type_partial" for issue in report.issues)
    links = validate_links(doc, build_addresses(doc), build_references(doc))
    missing = [i for i in links.issues if i.check == "ed.reference.property_rule_missing"]
    assert len(missing) == 1 and "Есть «выбор»" in missing[0].message
