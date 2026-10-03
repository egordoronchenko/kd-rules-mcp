"""Адреса не теряют дубли и не зависят от направления обмена."""

import pytest

from kd2_rules_mcp.ed import read_manager_text
from kd2_rules_mcp.ed.address import (
    AmbiguousAddressError,
    EntityNotFoundError,
    build_addresses,
    escape_segment,
    locate,
    property_key,
)
from tests.test_ed_reader import fixture_text


def test_escaping_and_empty_property():
    assert escape_segment("А/%#~\n") == "А%2F%25%23%7E%0A"
    assert property_key("", "") == "~empty"
    assert property_key("~empty", "") == "%7Eempty"
    assert property_key("", "Реквизит") == "Реквизит"
    assert property_key("Формат", "Реквизит") == "Формат"


def test_nested_addresses_and_aliases():
    doc = read_manager_text(fixture_text())
    index = build_addresses(doc)
    assert index.find("ПКО/Товар") == doc.pko[0]
    assert index.find("пко/товар/пкс/code") == doc.pko[0].properties[0]
    assert index.find("ПКО/Товар/ПКТЧ/Prices/ПКС/Price") == doc.pko[0].groups[0].properties[0]
    assert index.find("ПКО/Товар/Поиск/1") == doc.pko[0].search_sets[0]
    assert index.find("ПКПД/Статусы/Значение/2") == doc.pkpd[0].mappings[1]
    assert index.find("Алгоритм/ЗавершитьТовар").name == "ЗавершитьТовар"
    assert index.find("Параметр/Лимит") == doc.parameters[0]
    with pytest.raises(EntityNotFoundError):
        index.find("ПКО/Нет")


def test_conflicting_casefold_addresses_all_get_suffix():
    text = fixture_text().replace(
        'ДобавитьПКС(СвойстваШапки, "Код", "Code");',
        """
ДобавитьПКС(СвойстваШапки, "Код", "Code", , , "urn:a");
ДобавитьПКС(СвойстваШапки, "Код", "code", , , "urn:b");""",
    )
    doc = read_manager_text(text)
    index = build_addresses(doc)
    with pytest.raises(AmbiguousAddressError) as exc:
        index.find("ПКО/Товар/ПКС/Code")
    assert exc.value.candidates == ("ПКО/Товар/ПКС/Code#1", "ПКО/Товар/ПКС/code#2")
    assert (
        index.find(exc.value.candidates[0]).span.char_start
        < index.find(exc.value.candidates[1]).span.char_start
    )
    assert dict(index.by_address) == dict(build_addresses(read_manager_text(text)).by_address)


def test_duplicate_parents_qualify_children_and_reserved_literals():
    text = fixture_text().replace(
        'ПравилоКонвертации.ИмяПКО = "Заказ";', 'ПравилоКонвертации.ИмяПКО = "Товар";'
    )
    index = build_addresses(read_manager_text(text))
    with pytest.raises(AmbiguousAddressError):
        index.find("ПКО/Товар")
    assert index.find("ПКО/Товар#1/ПКС/Code").name == "Code"
    assert index.find("ПКО/Товар#2/ПКС/Product").name == "Product"
    text = (
        fixture_text()
        .replace('"Catalog.Product"', '"Catalog.Product"')
        .replace('"Code"', '"a/%#~"')
    )
    index = build_addresses(read_manager_text(text))
    assert index.find("ПКО/Товар/ПКС/a%2F%25%23%7E").name == "a/%#~"


def test_locate_narrowest_and_invalid_position():
    doc = read_manager_text(fixture_text())
    prop = doc.pko[0].properties[0]
    found = locate(doc, prop.span.line_start)
    assert found[0] == prop
    assert doc.pko[0] in found
    with pytest.raises(ValueError):
        locate(doc, 0)
    with pytest.raises(EntityNotFoundError):
        locate(doc, 1, "missing")


def test_empty_literal_name_is_not_an_absent_name():
    text = (
        fixture_text()
        .replace('ИмяПКО = "Товар"', 'ИмяПКО = ""')
        .replace(
            'ДобавитьПКС(СвойстваШапки, "Код", "Code");', 'ДобавитьПКС(СвойстваШапки, "", "");'
        )
    )
    doc = read_manager_text(text)
    assert doc.pko[0].declared_name == doc.pko[0].name == ""
    index = build_addresses(doc)
    assert index.find("ПКО//ПКС/~empty") == doc.pko[0].properties[0]
