"""Загрузка MD83Exp в SQLite на синтетическом файле (задачи 3.1, 3.3; спецификация
`metadata-structures`, требование «Загрузка выгрузки MD83Exp»)."""

import shutil
import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from kd_rules_mcp.errors import Kd2Error, StructureFormatError, StructureNotFoundError
from kd_rules_mcp.structures.store import StructureStore

SMALL = Path(__file__).parent / "data" / "md83exp_small.xml"


@pytest.fixture
def store(tmp_path: Path) -> StructureStore:
    return StructureStore(tmp_path / "cache")


@pytest.fixture
def loaded(store: StructureStore) -> Iterator[sqlite3.Connection]:
    store.load_md83exp("small", SMALL)
    connection = store.open("small")
    yield connection
    connection.close()


def object_id(connection: sqlite3.Connection, type_name: str) -> int:
    return connection.execute(
        "SELECT id FROM objects WHERE type_name = ?", (type_name,)
    ).fetchone()[0]


def property_types(connection: sqlite3.Connection, type_name: str, path: str) -> list[str]:
    row = connection.execute(
        "SELECT ts.types FROM properties p JOIN objects o ON o.id = p.object_id"
        " LEFT JOIN type_sets ts ON ts.id = p.type_set_id WHERE o.type_name = ? AND p.path = ?",
        (type_name, path),
    ).fetchone()
    assert row is not None, path
    return row[0].split("\n") if row[0] else []


def test_counts_and_meta(store: StructureStore) -> None:
    result = store.load_md83exp("small", SMALL)
    assert not result.reused
    # 4 примитива + 5 групп-разделов + 5 объектов.
    assert result.counts["objects"] == 14
    assert result.counts["properties"] == 17
    assert result.counts["values"] == 2
    meta = store.meta("small")
    assert meta["source"] == "md83exp"
    assert meta["config_name"] == "Тестовая"
    assert meta["config_version"] == "1.0.0.1"
    assert len(meta["input_hash"]) == 64


def test_objects_keep_kind_name_type_and_group(loaded: sqlite3.Connection) -> None:
    row = loaded.execute(
        "SELECT o.kind, o.name, g.name, o.attrs FROM objects o JOIN objects g ON g.id = o.group_id"
        " WHERE o.type_name = 'СправочникСсылка.Валюты'"
    ).fetchone()
    assert tuple(row[:3]) == ("Справочник", "Валюты", "Справочники")
    assert '"Иерархический": "false"' in row[3]


def test_property_tree_paths_and_qualifiers(loaded: sqlite3.Connection) -> None:
    rows = loaded.execute(
        "SELECT kind, path, is_group, number_length, number_precision FROM properties"
        " WHERE object_id = ? ORDER BY id",
        (object_id(loaded, "ДокументСсылка.Заказ"),),
    ).fetchall()
    assert [tuple(r) for r in rows] == [
        ("Свойство", "Дата", 0, 0, 0),
        ("Реквизит", "Валюта", 0, 0, 0),
        ("Реквизит", "Контрагент", 0, 0, 0),
        ("ТабличнаяЧасть", "Товары", 1, 0, 0),
        ("Реквизит", "Товары.Количество", 0, 15, 3),
        ("Реквизит", "Товары.Вид", 0, 0, 0),
        ("НаборДвиженийРегистраНакопления", "Остатки", 1, 0, 0),
        ("Измерение", "Остатки.Валюта", 0, 0, 0),
        ("Ресурс", "Остатки.Сумма", 0, 15, 2),
    ]


def test_types_resolve_forward_references_by_name(loaded: sqlite3.Connection) -> None:
    # Справочник Валюты и перечисление объявлены в файле после документа.
    assert property_types(loaded, "ДокументСсылка.Заказ", "Валюта") == ["СправочникСсылка.Валюты"]
    assert property_types(loaded, "ДокументСсылка.Заказ", "Товары.Вид") == [
        "ПеречислениеСсылка.ВидыОпераций"
    ]


def test_type_sets_are_deduplicated(loaded: sqlite3.Connection) -> None:
    assert property_types(loaded, "ДокументСсылка.Заказ", "Контрагент") == sorted(
        ["Строка", "СправочникСсылка.Валюты"]
    )
    ids = loaded.execute(
        "SELECT COUNT(DISTINCT type_set_id), COUNT(*) FROM properties"
        " WHERE path IN ('Валюта', 'Остатки.Валюта')"
    ).fetchone()
    assert tuple(ids) == (1, 3)  # у документа, его набора движений и у регистра
    total = loaded.execute("SELECT COUNT(*) FROM type_sets").fetchone()[0]
    assert total == 6  # Строка, Число, Дата, Валюты, Строка+Валюты, ВидыОпераций


def test_exchange_plan_content_and_unresolved_type(loaded: sqlite3.Connection) -> None:
    rows = loaded.execute(
        "SELECT p.name, p.autoregistration, p.unresolved FROM properties p"
        " JOIN properties parent ON parent.id = p.parent_id"
        " WHERE parent.kind = 'СоставПланаОбмена' ORDER BY p.id"
    ).fetchall()
    assert [tuple(r) for r in rows] == [
        ("Валюты", 1, None),
        ("ДокументыОрганизаций", 0, "a0000000-0000-0000-0000-000000000999"),
    ]


def test_enum_values(loaded: sqlite3.Connection) -> None:
    rows = loaded.execute(
        "SELECT name, predefined FROM object_values WHERE object_id = ? ORDER BY id",
        (object_id(loaded, "ПеречислениеСсылка.ВидыОпераций"),),
    ).fetchall()
    assert [tuple(r) for r in rows] == [("Приход", 1), ("Расход", 1)]


def test_repeated_load_of_same_content_is_reused(store: StructureStore, tmp_path: Path) -> None:
    store.load_md83exp("small", SMALL)
    copy = tmp_path / "копия.xml"
    shutil.copyfile(SMALL, copy)
    result = store.load_md83exp("small", copy)
    assert result.reused
    assert "сохранённая структура" in result.message
    assert result.counts["objects"] == 14


def test_changed_content_is_loaded_again(store: StructureStore, tmp_path: Path) -> None:
    store.load_md83exp("small", SMALL)
    changed = tmp_path / "изменён.xml"
    changed.write_bytes(SMALL.read_bytes().replace("Расход".encode(), "Возврат".encode()))
    result = store.load_md83exp("small", changed)
    assert not result.reused
    connection = store.open("small")
    names = [r[0] for r in connection.execute("SELECT name FROM object_values ORDER BY id")]
    connection.close()
    assert names == ["Приход", "Возврат"]


@pytest.mark.parametrize(
    ("content", "reason"),
    [
        ('<?xml version="1.0"?><Правила><А/></Правила>', "Корневой элемент «Правила»"),
        (
            '<?xml version="1.0"?><Конфигурация><CatalogObject.Объекты/></Конфигурация>',
            "ожидалась «CatalogObject.Конфигурации»",
        ),
        ('<?xml version="1.0"?><Конфигурация></Конфигурация>', "нет записи"),
        ('<?xml version="1.0"?><Конфигурация><CatalogObject.Конф', "не является корректным XML"),
    ],
)
def test_invalid_input_keeps_existing_structure(
    store: StructureStore, tmp_path: Path, content: str, reason: str
) -> None:
    store.load_md83exp("small", SMALL)
    before = store.path("small").read_bytes()
    bad = tmp_path / "плохой.xml"
    bad.write_text(content, encoding="utf-8")
    with pytest.raises(StructureFormatError, match=reason):
        store.load_md83exp("small", bad)
    assert store.path("small").read_bytes() == before
    assert not list(store.cache_dir.glob("*.part"))


def test_unknown_structure_and_bad_id(store: StructureStore) -> None:
    with pytest.raises(StructureNotFoundError, match="нет в кэше"):
        store.open("missing")
    with pytest.raises(Kd2Error, match="Недопустимый идентификатор"):
        store.path("../вне")
