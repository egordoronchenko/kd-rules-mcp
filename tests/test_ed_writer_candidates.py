"""Кандидаты нового менеджера: имя, синоним, вид, примитив, ссылка, страницы, устаревание."""

import sqlite3
from pathlib import Path

import pytest

from kd2_rules_mcp.authoring.ed.manager_candidates import (
    CandidateLookupError,
    StaleCandidateError,
    check_candidate,
    object_candidates,
    property_candidates,
)
from kd2_rules_mcp.ed.schema import load_schema
from kd2_rules_mcp.structures import db

DATA = Path(__file__).parent / "data/ed/writer"
_TYPE_PREFIX = {"Справочник": "СправочникСсылка.", "Документ": "ДокументСсылка."}


def schema():
    return load_schema(DATA / "format.bin", locate_import=lambda _: DATA / "message.bin")


def structure() -> sqlite3.Connection:
    connection = sqlite3.connect(":memory:")
    connection.executescript(db.SCHEMA)
    add_object(connection, "Справочник", "Должности", "Должности")
    add_prop(
        connection,
        "Справочник",
        "Должности",
        "Свойство",
        "Наименование",
        ("Строка",),
        string_length=20,
    )
    add_prop(connection, "Справочник", "Должности", "Реквизит", "НаименованиеКраткое", ("Строка",))
    add_prop(connection, "Справочник", "Должности", "Реквизит", "Комментарий", ("Строка",))
    add_prop(
        connection, "Справочник", "Должности", "Свойство", "Ссылка", ("СправочникСсылка.Должности",)
    )
    add_prop(connection, "Справочник", "Должности", "Реквизит", "Код", ("Строка",), string_length=9)
    add_prop(
        connection,
        "Справочник",
        "Должности",
        "Реквизит",
        "Классификатор",
        ("СправочникСсылка.Классификатор",),
    )
    add_prop(connection, "Справочник", "Должности", "Реквизит", "Заметка", ("Строка",))
    table = add_prop(connection, "Справочник", "Должности", "ТабличнаяЧасть", "Товары", ())
    add_prop(
        connection,
        "Справочник",
        "Должности",
        "Реквизит",
        "Количество",
        ("Число",),
        parent=table,
        number_length=10,
        number_precision=0,
    )
    add_object(connection, "Документ", "Заказ", "Заказ")
    add_prop(connection, "Документ", "Заказ", "Реквизит", "Номер", ("Строка",), string_length=11)
    add_object(connection, "Документ", "Акт", "Акт")
    add_prop(connection, "Документ", "Акт", "Реквизит", "Номер", ("Строка",), string_length=11)
    add_object(connection, "Справочник", "ПодразделенияОрганизаций", "Подразделения")
    add_object(connection, "Справочник", "Склад1", "Склады")
    add_object(connection, "Справочник", "Склад2", "Склады")
    return connection


def add_object(connection: sqlite3.Connection, kind: str, name: str, synonym: str) -> None:
    connection.execute(
        "INSERT INTO objects (kind, name, type_name, synonym) VALUES (?, ?, ?, ?)",
        (kind, name, _TYPE_PREFIX[kind] + name, synonym),
    )


def object_id(connection: sqlite3.Connection, kind: str, name: str) -> int:
    row = connection.execute(
        "SELECT id FROM objects WHERE kind = ? AND name = ?", (kind, name)
    ).fetchone()
    assert row is not None
    return int(row[0])


def add_prop(
    connection: sqlite3.Connection,
    kind: str,
    owner: str,
    prop_kind: str,
    name: str,
    types: tuple[str, ...],
    *,
    parent: int | None = None,
    string_length: int = 0,
    number_length: int = 0,
    number_precision: int = 0,
) -> int:
    type_id = None
    if types:
        text = "\n".join(types)
        found = connection.execute("SELECT id FROM type_sets WHERE types = ?", (text,)).fetchone()
        if found is None:
            type_id = int(
                connection.execute("INSERT INTO type_sets (types) VALUES (?)", (text,)).lastrowid
                or 0
            )
        else:
            type_id = int(found[0])
    path = name if parent is None else f"{_path(connection, parent)}.{name}"
    cursor = connection.execute(
        "INSERT INTO properties (object_id, parent_id, kind, name, path, type_set_id, "
        "string_length, number_length, number_precision) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            object_id(connection, kind, owner),
            parent,
            prop_kind,
            name,
            path,
            type_id,
            string_length,
            number_length,
            number_precision,
        ),
    )
    return int(cursor.lastrowid or 0)


def _path(connection: sqlite3.Connection, prop_id: int) -> str:
    row = connection.execute("SELECT path FROM properties WHERE id = ?", (prop_id,)).fetchone()
    assert row is not None
    return str(row[0])


def test_object_name_synonym_kind_and_pages():
    rows = object_candidates(structure(), schema(), direction="send", limit=50)
    assert rows["total"] == 4 and rows["has_more"] is False and rows["limit"] == 50
    assert [item["configuration"] for item in rows["items"]] == [
        "Документ.Акт",
        "Документ.Заказ",
        "Справочник.Должности",
        "Справочник.ПодразделенияОрганизаций",
    ]
    exact = {item["configuration"]: item for item in rows["items"][:3]}
    assert exact["Справочник.Должности"]["format_type"] == "Справочник.Должности"
    assert exact["Справочник.Должности"]["confidence"] == "точно"
    assert exact["Справочник.Должности"]["reason"] == "совпадает имя"
    assert exact["Справочник.Должности"]["auto"] is False
    synonym = rows["items"][3]
    assert synonym["format_type"] == "Справочник.Подразделения"
    assert synonym["confidence"] == "по синониму"
    assert "Подразделения" in synonym["reason"]
    assert all(item["auto"] is False for item in rows["items"])
    paired = {(item["configuration"], item["format_type"]) for item in rows["items"]}
    assert ("Справочник.Должности", "Документ.Должности") not in paired
    assert "Справочник.Должности.Сотрудники" not in {item["format_type"] for item in rows["items"]}
    assert "Справочник.Склады" not in {item["format_type"] for item in rows["items"]}
    page = object_candidates(structure(), schema(), direction="send", offset=0, limit=1)
    assert page["total"] == 4 and page["has_more"] is True
    assert page["items"][0]["configuration"] == "Документ.Акт"
    tail = object_candidates(structure(), schema(), direction="send", offset=3, limit=1)
    assert tail["items"][0]["confidence"] == "по синониму" and tail["has_more"] is False
    documents = object_candidates(structure(), schema(), direction="send", kinds=["Документ"])
    assert {item["configuration"] for item in documents["items"]} == {
        "Документ.Акт",
        "Документ.Заказ",
    }
    found = object_candidates(structure(), schema(), direction="send", text="подраздел")
    assert [item["configuration"] for item in found["items"]] == [
        "Справочник.ПодразделенияОрганизаций"
    ]
    wide = object_candidates(structure(), schema(), direction="send", limit=500)
    assert wide["limit"] == 200
    with pytest.raises(TypeError):
        object_candidates(structure(), schema(), direction="send", kinds="Справочник")
    with pytest.raises(ValueError):
        object_candidates(structure(), schema(), direction="send", offset=-1)


def test_properties_direct_reference_mismatch_tables_and_direction():
    send = property_candidates(
        structure(), schema(), "Справочник.Должности", "Справочник.Должности", direction="send"
    )
    receive = property_candidates(
        structure(), schema(), "Справочник.Должности", "Справочник.Должности", direction="receive"
    )
    by_name = {item["configuration"]: item for item in send["properties"]["items"]}
    assert set(by_name) == {
        "Классификатор",
        "Код",
        "Комментарий",
        "Наименование",
        "НаименованиеКраткое",
        "Ссылка",
    }
    codes = [item for item in send["properties"]["items"] if item["configuration"] == "Код"]
    assert [item["format_path"] for item in codes] == ["КлючевыеСвойства.Код"]
    names = [
        item["format_path"]
        for item in send["properties"]["items"]
        if item["configuration"] == "Наименование"
    ]
    assert names == ["КлючевыеСвойства.Наименование"]
    name = by_name["Наименование"]
    assert name["class"] == "direct" and name["auto"] is False
    assert name["format_path"] == "КлючевыеСвойства.Наименование"
    assert name["format_name"] == "Наименование"
    assert name["value_range"]
    assert "20" in name["value_range"] and "10" in name["value_range"]
    back = {item["configuration"]: item for item in receive["properties"]["items"]}
    assert back["Наименование"]["class"] == "direct"
    assert back["Наименование"]["value_range"] is None
    short = by_name["НаименованиеКраткое"]
    assert short["class"] == "direct" and short["format_path"] == "НаименованиеКраткое"
    assert short["value_range"] is None
    assert back["НаименованиеКраткое"]["value_range"] is None
    comment = by_name["Комментарий"]
    assert comment["class"] == "direct"
    assert comment["format_path"] == "ОбщиеСвойстваОбъектовФормата.Комментарий"
    link = by_name["Ссылка"]
    assert link["class"] == "reference" and link["needs_rule"] is True
    assert link["configuration_types"] == ["СправочникСсылка.Должности"]
    assert link["format_type"] == "СправочникСсылка.Должности"
    assert link["format_object"] is None
    assert link["format_path"] == "КлючевыеСвойства.Ссылка"
    classifier = by_name["Классификатор"]
    assert classifier["class"] == "reference" and classifier["needs_rule"] is True
    assert classifier["format_type"] == "КлючевыеСвойстваКлассификатор"
    assert classifier["format_object"] == "Справочник.Классификатор"
    assert classifier["format_path"] == "Классификатор"
    assert by_name["Код"]["class"] == "direct"
    assert by_name["Код"]["format_path"] == "КлючевыеСвойства.Код"
    assert [item["configuration"] for item in send["properties"]["items"]] == [
        "Классификатор",
        "Код",
        "Комментарий",
        "Наименование",
        "НаименованиеКраткое",
        "Ссылка",
    ]
    assert send["table_parts"]["items"] == [
        {
            "candidate_id": send["table_parts"]["items"][0]["candidate_id"],
            "configuration": "Товары",
            "format": "Товары",
            "format_path": "Товары",
            "auto": False,
            "direction": "send",
        }
    ]
    assert "Товары" not in by_name
    assert "Количество" not in by_name
    assert {item["name"] for item in send["unmatched_configuration"]["items"]} == {"Заметка"}
    assert {item["path"] for item in send["unmatched_format"]["items"]} == {
        "КодКлассификатора",
        "Лишнее",
    }
    assert "Количество" not in {item["name"] for item in send["unmatched_configuration"]["items"]}
    paged = property_candidates(
        structure(),
        schema(),
        "Справочник.Должности",
        "Справочник.Должности",
        direction="send",
        offset=0,
        limit=1,
    )
    assert paged["properties"]["total"] == 6 and paged["properties"]["has_more"] is True
    assert paged["properties"]["items"][0]["configuration"] == "Классификатор"
    act = property_candidates(
        structure(), schema(), "Документ.Акт", "Документ.Акт", direction="send"
    )
    assert act["properties"]["items"][0]["configuration"] == "Номер"
    assert act["properties"]["items"][0]["format_name"] == "НомерДок"
    assert act["properties"]["items"][0]["confidence"] == "синоним КД"
    assert act["properties"]["items"][0]["class"] == "direct"
    with pytest.raises(CandidateLookupError):
        property_candidates(
            structure(), schema(), "Справочник.НетТакого", "Справочник.Должности", direction="send"
        )
    with pytest.raises(CandidateLookupError):
        property_candidates(
            structure(), schema(), "Справочник.Должности", "Справочник.НетТакого", direction="send"
        )


def test_candidate_id_is_bound_to_structure_and_schema():
    connection = structure()
    opened = schema()
    first = object_candidates(connection, opened, direction="send")
    again = object_candidates(connection, opened, direction="send")
    assert [item["candidate_id"] for item in first["items"]] == [
        item["candidate_id"] for item in again["items"]
    ]
    other = object_candidates(connection, opened, direction="receive")
    assert first["items"][0]["candidate_id"] != other["items"][0]["candidate_id"]
    chosen = first["items"][0]["candidate_id"]
    check_candidate(chosen, connection, opened)
    connection.execute("UPDATE objects SET synonym = synonym || '!'")
    with pytest.raises(StaleCandidateError):
        check_candidate(chosen, connection, opened)
    with pytest.raises(StaleCandidateError):
        check_candidate(chosen[:16], connection, opened)
    with pytest.raises(StaleCandidateError):
        check_candidate(chosen[:16] + "0" * (len(chosen) - 16), connection, opened)
    with pytest.raises(StaleCandidateError):
        check_candidate(chosen[:16] + "мусор", connection, opened)


def test_candidate_id_is_not_reused_for_the_next_connection(tmp_path: Path) -> None:
    """Адрес закрытого соединения переиспользуется, отпечаток — нет."""
    left = tmp_path / "left.sqlite"
    right = tmp_path / "right.sqlite"
    _file_structure(left, "Альфа")
    _file_structure(right, "Бета")
    opened = schema()
    first = db.open_readonly(left)
    left_id = object_candidates(first, opened, direction="send", limit=1)["items"][0][
        "candidate_id"
    ]
    address = id(first)
    first.close()
    del first
    second = None
    for _ in range(20):
        candidate = db.open_readonly(right)
        if id(candidate) == address:
            second = candidate
            break
        candidate.close()
    assert second is not None
    try:
        right_id = object_candidates(second, opened, direction="send", limit=1)["items"][0][
            "candidate_id"
        ]
        assert left_id != right_id
        with pytest.raises(StaleCandidateError):
            check_candidate(left_id, second, opened)
        check_candidate(right_id, second, opened)
    finally:
        second.close()


def _file_structure(path: Path, name: str) -> None:
    connection = db.create(path)
    try:
        add_object(connection, "Справочник", "Должности", "Должности")
        add_object(connection, "Справочник", name, name)
        connection.commit()
    finally:
        connection.close()


def test_extension_properties_join_the_base_type() -> None:
    opened = load_schema(
        DATA / "format.bin",
        extensions=(DATA / "extension.bin",),
        locate_import=lambda namespace: (
            DATA / "message.bin" if namespace == "urn:test:writer-message" else None
        ),
    )
    connection = structure()
    add_prop(connection, "Справочник", "Должности", "Реквизит", "СвояДата", ("Дата",))
    add_prop(connection, "Справочник", "Должности", "Реквизит", "Момент", ("Дата",))
    add_prop(connection, "Справочник", "Должности", "Реквизит", "ВремяСуток", ("Дата",))
    connection.execute(
        "UPDATE properties SET date_parts = 'Дата' WHERE name IN ('СвояДата', 'Момент')"
    )
    connection.execute("UPDATE properties SET date_parts = 'Дата' WHERE name = 'ВремяСуток'")
    bare = property_candidates(
        connection, opened, "Справочник.Должности", "Справочник.Должности", direction="send"
    )
    qualified = property_candidates(
        connection,
        opened,
        "Справочник.Должности",
        "{urn:test:ext}Справочник.Должности",
        direction="send",
    )
    bare_keys = {(item["format_path"], item["namespace"]) for item in bare["properties"]["items"]}
    qualified_keys = {
        (item["format_path"], item["namespace"]) for item in qualified["properties"]["items"]
    }
    assert bare_keys == qualified_keys
    assert ("Момент", "urn:test:ext") in bare_keys
    names = {(item["format_name"], item["namespace"]) for item in bare["properties"]["items"]}
    assert ("Наименование", "urn:test:writer") in names
    moment = next(item for item in bare["properties"]["items"] if item["format_name"] == "Момент")
    assert moment["class"] == "direct"
    assert moment["value_range"] is None
    clock = next(
        item for item in bare["properties"]["items"] if item["format_name"] == "ВремяСуток"
    )
    assert clock["class"] == "mismatch"
    back = property_candidates(
        connection, opened, "Справочник.Должности", "Справочник.Должности", direction="receive"
    )
    received = next(item for item in back["properties"]["items"] if item["format_name"] == "Момент")
    assert received["class"] == "direct"
    assert received["value_range"] == "время из сообщения отбрасывается"
    connection.execute("UPDATE properties SET date_parts = 'Дата и время' WHERE name = 'СвояДата'")
    sent = property_candidates(
        connection, opened, "Справочник.Должности", "Справочник.Должности", direction="send"
    )
    day = next(item for item in sent["properties"]["items"] if item["format_name"] == "СвояДата")
    assert day["class"] == "direct"
    assert day["value_range"] == "время реквизита отбрасывается"
