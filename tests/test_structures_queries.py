"""Запросы к структуре и сравнение структур (задача 3.4, спецификация `metadata-structures`)."""

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from kd2_rules_mcp.structures.queries import (
    MAX_LIMIT,
    NotFound,
    Page,
    compare_structures,
    describe_object,
    exchange_plan_content,
    list_objects,
    object_values,
)
from kd2_rules_mcp.structures.store import StructureStore

SMALL = Path(__file__).parent / "data" / "md83exp_small.xml"
NUMBER_TYPE = b"a0000000-0000-0000-0000-000000000002"
STRING_TYPE = b"a0000000-0000-0000-0000-000000000001"
QTY_REF = b"a0000000-0000-0000-0000-000000000114"
NAME_REF = b"a0000000-0000-0000-0000-000000000121"
CATALOG_REF = b"a0000000-0000-0000-0000-000000000021"
MISSING_REF = b"a0000000-0000-0000-0000-000000000998"


@pytest.fixture
def connection(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    store = StructureStore(tmp_path / "cache")
    store.load_md83exp("small", SMALL)
    opened = store.open("small")
    yield opened
    opened.close()


def test_list_objects_orders_by_full_name_and_skips_stubs(connection: sqlite3.Connection) -> None:
    page = list_objects(connection, limit=MAX_LIMIT)
    assert page.total == 5
    assert page.offset == 0
    assert not page.has_more
    assert [item["name"] for item in page.items] == [
        "Документ.Заказ",
        "Перечисление.ВидыОпераций",
        "ПланОбмена.Обмен",
        "РегистрНакопления.Остатки",
        "Справочник.Валюты",
    ]
    assert page.items[-1]["type_name"] == "СправочникСсылка.Валюты"
    assert list_objects(connection, text="булево").total == 0
    assert list_objects(connection, text="документы").total == 0


def test_list_objects_filters_kind_and_casefolded_text(connection: sqlite3.Connection) -> None:
    page = list_objects(connection, kind="Справочник", text="ВАЛ")
    assert [item["name"] for item in page.items] == ["Справочник.Валюты"]
    assert list_objects(connection, kind="Документ", text="валют").total == 0


def test_synonym_search_ignores_cyrillic_case(tmp_path: Path) -> None:
    store = StructureStore(tmp_path / "cache")
    store.load_md83exp("small", SMALL)
    writable = sqlite3.connect(store.path("small"))
    writable.execute(
        "UPDATE objects SET synonym = ? WHERE type_name = ?",
        ("Денежные единицы", "СправочникСсылка.Валюты"),
    )
    writable.commit()
    writable.close()
    connection = store.open("small")
    try:
        page = list_objects(connection, text="ДЕНЕЖНЫЕ")
        assert [item["name"] for item in page.items] == ["Справочник.Валюты"]
        assert page.items[0]["synonym"] == "Денежные единицы"
    finally:
        connection.close()


def test_page_window_reports_total_has_more_and_clips_limit(
    connection: sqlite3.Connection,
) -> None:
    first = list_objects(connection, offset=0, limit=2)
    assert first.total == 5
    assert len(first.items) == 2
    assert first.has_more
    assert first.items[0]["name"] == "Документ.Заказ"
    last = list_objects(connection, offset=4, limit=2)
    assert len(last.items) == 1
    assert not last.has_more
    beyond = list_objects(connection, offset=100, limit=10)
    assert beyond.items == []
    assert not beyond.has_more
    clipped = list_objects(connection, limit=10_000)
    assert clipped.limit == MAX_LIMIT
    assert len(clipped.items) == 5


@pytest.mark.parametrize(
    ("offset", "limit", "match"),
    [(-1, 50, "отрицательным"), (0, 0, "не меньше 1"), (0, -5, "не меньше 1")],
)
def test_page_window_rejects_bad_bounds(
    connection: sqlite3.Connection, offset: int, limit: int, match: str
) -> None:
    with pytest.raises(ValueError, match=match):
        list_objects(connection, offset=offset, limit=limit)
    with pytest.raises(ValueError, match=match):
        describe_object(connection, "Документ.Заказ", offset=offset, limit=limit)


def test_describe_document_tree_and_type_name(connection: sqlite3.Connection) -> None:
    described = describe_object(connection, "ДокументСсылка.Заказ")
    assert isinstance(described, dict)
    assert described["name"] == "Документ.Заказ"
    assert described["type_name"] == "ДокументСсылка.Заказ"
    assert described["kind"] == "Документ"
    assert described["attrs"]["Иерархический"] == "false"
    page = described["properties"]
    assert isinstance(page, Page)
    assert page.total == 9
    assert not page.has_more
    by_path = {item["path"]: item for item in page.items}
    assert [item["path"] for item in page.items[:3]] == ["Дата", "Валюта", "Контрагент"]
    goods = by_path["Товары"]
    assert goods["kind"] == "ТабличнаяЧасть"
    assert goods["is_group"] is True
    assert goods["types"] == []
    assert goods["qualifiers"] == {}
    quantity = by_path["Товары.Количество"]
    assert quantity["types"] == ["Число"]
    assert quantity["qualifiers"] == {"number_length": 15, "number_precision": 3}
    assert by_path["Товары.Вид"]["types"] == ["ПеречислениеСсылка.ВидыОпераций"]
    counterparty = by_path["Контрагент"]
    assert counterparty["types"] == sorted(["Строка", "СправочникСсылка.Валюты"])
    assert counterparty["qualifiers"] == {"string_length": 50}
    assert "unresolved" not in counterparty
    movement = by_path["Остатки"]
    assert movement["kind"] == "НаборДвиженийРегистраНакопления"
    assert movement["is_group"] is True
    assert by_path["Остатки.Валюта"]["kind"] == "Измерение"
    assert by_path["Остатки.Валюта"]["types"] == ["СправочникСсылка.Валюты"]
    assert by_path["Остатки.Сумма"]["kind"] == "Ресурс"


def test_describe_properties_are_paged(connection: sqlite3.Connection) -> None:
    described = describe_object(connection, "Документ.Заказ", limit=4)
    assert isinstance(described, dict)
    page = described["properties"]
    assert page.total == 9
    assert len(page.items) == 4
    assert page.has_more
    described = describe_object(connection, "Документ.Заказ", limit=10_000)
    assert isinstance(described, dict)
    assert described["properties"].limit == MAX_LIMIT


def test_missing_object_suggests_same_kind(connection: sqlite3.Connection) -> None:
    missed = describe_object(connection, "Справочник.Валюта")
    assert isinstance(missed, NotFound)
    assert missed.suggestions == ["Справочник.Валюты"]
    assert len(missed.suggestions) <= 5
    assert missed.message == "Объект «Справочник.Валюта» не найден; похожие: Справочник.Валюты"
    by_type = object_values(connection, "СправочникСсылка.Валюта")
    assert isinstance(by_type, NotFound)
    assert by_type.suggestions == ["Справочник.Валюты"]
    other_kind = describe_object(connection, "Справочник.Заказ")
    assert isinstance(other_kind, NotFound)
    assert all(name.startswith("Справочник.") for name in other_kind.suggestions)
    assert "Документ.Заказ" not in other_kind.suggestions
    assert isinstance(describe_object(connection, "Число"), NotFound)
    assert isinstance(describe_object(connection, "Справочники"), NotFound)


def test_object_values_keep_file_order(connection: sqlite3.Connection) -> None:
    page = object_values(connection, "ПеречислениеСсылка.ВидыОпераций", limit=1)
    assert isinstance(page, Page)
    assert page.total == 2
    assert page.has_more
    assert page.items == [{"name": "Приход", "synonym": "Приход", "predefined": True}]
    rest = object_values(connection, "Перечисление.ВидыОпераций", offset=1, limit=1)
    assert isinstance(rest, Page)
    assert rest.items[0]["name"] == "Расход"
    assert not rest.has_more
    empty = object_values(connection, "Справочник.Валюты")
    assert isinstance(empty, Page)
    assert empty.total == 0
    assert empty.items == []


def test_exchange_plan_content_includes_unresolved_type(connection: sqlite3.Connection) -> None:
    page = exchange_plan_content(connection, "ПланОбмена.Обмен")
    assert isinstance(page, Page)
    assert page.total == 2
    assert page.items[0] == {
        "name": "Валюты",
        "types": ["СправочникСсылка.Валюты"],
        "autoregistration": True,
    }
    assert page.items[1]["name"] == "ДокументыОрганизаций"
    assert page.items[1]["autoregistration"] is False
    assert page.items[1]["types"] == []
    assert page.items[1]["unresolved"] == ["a0000000-0000-0000-0000-000000000999"]
    foreign = exchange_plan_content(connection, "Документ.Заказ")
    assert isinstance(foreign, NotFound)
    assert foreign.suggestions == []
    assert foreign.message == "Объект «Документ.Заказ» не является планом обмена"
    missed = exchange_plan_content(connection, "ПланОбмена.НетТакого")
    assert isinstance(missed, NotFound)
    assert "не найден" in missed.message


def test_compare_identical_structure_is_empty(connection: sqlite3.Connection) -> None:
    diff = compare_structures(connection, connection)
    assert diff.counts() == {
        "added_objects": 0,
        "removed_objects": 0,
        "added_properties": 0,
        "removed_properties": 0,
        "changed_properties": 0,
        "added_values": 0,
        "removed_values": 0,
    }


def _replace_first_after(raw: bytes, anchor: bytes, old: bytes, new: bytes) -> bytes:
    start = raw.find(anchor)
    assert start != -1
    at = raw.find(old, start)
    assert at != -1
    return raw[:at] + new + raw[at + len(old) :]


def test_compare_sees_added_value_removed_property_and_changed_type(tmp_path: Path) -> None:
    """Новое значение перечисления, смена типа реквизита и удалённый реквизит."""
    raw = SMALL.read_bytes().replace("Расход".encode(), "Возврат".encode())
    raw = _replace_first_after(raw, QTY_REF, NUMBER_TYPE, STRING_TYPE)
    raw = _replace_first_after(raw, NAME_REF, CATALOG_REF, MISSING_REF)
    changed = tmp_path / "changed.xml"
    changed.write_bytes(raw)
    store = StructureStore(tmp_path / "cache")
    store.load_md83exp("old", SMALL)
    store.load_md83exp("new", changed)
    old = store.open("old")
    new = store.open("new")
    try:
        diff = compare_structures(old, new)
    finally:
        old.close()
        new.close()
    assert diff.added_objects == []
    assert diff.removed_objects == []
    assert diff.added_properties == []
    assert diff.removed_properties == ["Справочник.Валюты|Наименование|Свойство"]
    assert diff.added_values == ["Перечисление.ВидыОпераций|Возврат"]
    assert diff.removed_values == ["Перечисление.ВидыОпераций|Расход"]
    assert len(diff.changed_properties) == 1
    change = diff.changed_properties[0]
    assert change["key"] == "Документ.Заказ|Товары.Количество|Реквизит"
    assert change["old_types"] == ["Число"]
    assert change["new_types"] == ["Строка"]
    assert change["old_qualifiers"] == {"number_length": 15, "number_precision": 3}
    assert change["new_qualifiers"] == change["old_qualifiers"]
    assert diff.counts()["changed_properties"] == 1
    assert diff.counts()["removed_properties"] == 1
