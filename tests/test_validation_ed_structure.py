"""Нарушение и чистая граница каждой конфигурационной проверки."""

import os
import sqlite3
from collections import Counter
from dataclasses import replace
from pathlib import Path
from types import MappingProxyType
from typing import Any, cast

import pytest

from kd_rules_mcp.ed.address import build_addresses
from kd_rules_mcp.ed.schema.profile import ValidationProfile
from kd_rules_mcp.structures import db, md83exp
from kd_rules_mcp.validation.ed_structure import validate_structure
from kd_rules_mcp.validation.ed_structure_snapshot import (
    COLLECTIONS,
    CheckContext,
    StructureSnapshot,
)
from tests.test_ed_profile import BASE, DATA, document


def snapshot():
    with sqlite3.connect(":memory:") as connection:
        connection.executescript(db.SCHEMA)
        md83exp.load(DATA / "structure.xml", connection)
        return StructureSnapshot.load(connection)


def check(text=BASE, direction="both"):
    doc = document(text)
    return validate_structure(
        doc, snapshot(), build_addresses(doc), ValidationProfile.build(None, "1.2", direction)
    )


def test_session_reuses_unchanged_structure_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Повтор того же файла не собирает снимок заново; правка файла собирает."""
    from tests import session_inputs

    monkeypatch.setattr(session_inputs, "_VOLATILE_ROOT", tmp_path.parent / "не-временный")
    monkeypatch.setattr(session_inputs, "_snapshots", {})
    path = tmp_path / "snap.sqlite"
    created = db.create(path)
    created.execute(
        "INSERT INTO objects (kind, name, type_name) "
        "VALUES ('Справочник', 'А', 'СправочникСсылка.А')"
    )
    created.commit()
    created.close()
    connection = sqlite3.connect(path)
    try:
        first = StructureSnapshot.load(connection)
        assert StructureSnapshot.load(connection) is first
        connection.execute(
            "INSERT INTO objects (kind, name, type_name) VALUES "
            "('Справочник', 'Б', 'СправочникСсылка.Б')"
        )
        connection.commit()
        # Размер файла прежний (та же страница), а время записи под нагрузкой может совпасть
        # с предыдущим: отметку времени сдвигаем явно, иначе тест плавает.
        stat = path.stat()
        os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))
        changed = StructureSnapshot.load(connection)
    finally:
        connection.close()
    assert changed is not first
    assert ("справочник", "б") in changed.objects


def test_clean_and_snapshot_immutable():
    assert check().issues == []
    snap = snapshot()
    item = snap.objects[("справочник", "тест")]
    assert item.property("код")[0].qualifiers["string_length"] == 12
    assert item.property("неполный")[0].unresolved
    with pytest.raises(TypeError):
        cast(Any, item.properties)[("нет", "")] = ()
    assert snap.search(item, "Ссылка.Код")[1] == "resolved"
    assert snap.search(item, "Неполный.Код")[1] == "unresolved_configuration_type"


@pytest.mark.parametrize(
    "old,new,check_id,address,message,level",
    [
        (
            "Метаданные.Справочники.Тест;",
            "Метаданные.Справочники.Нет;",
            "object_missing",
            "ПКО/Тест",
            "Объект конфигурации «Метаданные.Справочники.Нет» отсутствует в структуре.",
            "ошибка",
        ),
        (
            '"Код", "Код", 0',
            '"Нет", "Код", 0',
            "property_missing",
            "ПКО/Тест/ПКС/Код",
            "Свойство конфигурации «Нет» отсутствует в структуре.",
            "предупреждение",
        ),
        (
            "Метаданные.Перечисления.Выбор;",
            "Метаданные.Перечисления.Нет;",
            "pkpd_type_missing",
            "ПКПД/Выбор",
            "Тип конфигурации ПКПД «Метаданные.Перечисления.Нет» отсутствует в структуре.",
            "ошибка",
        ),
        (
            "Перечисления.Выбор.А",
            "Перечисления.Выбор.Нет",
            "pkpd_value_missing",
            None,
            "Значение конфигурации «Перечисления.Выбор.Нет» отсутствует у указанного типа ПКПД.",
            "предупреждение",
        ),
    ],
)
def test_violation_and_clean(old, new, check_id, address, message, level):
    report = check(BASE.replace(old, new))
    issues = [i for i in report.issues if i.check == "ed.structure." + check_id]
    assert issues and all(i.level.value == level and i.message == message for i in issues)
    if address:
        assert {i.address for i in issues} == {address}
    else:
        assert all(i.address.startswith("ПКПД/Выбор/Значение/") for i in issues)
    assert not [i for i in check().issues if i.check == "ed.structure." + check_id]


def test_search_missing_and_reference_path():
    text = BASE.replace("// <properties>", 'ПравилоКонвертации.ПоляПоиска.Добавить("Нет");')
    issue = check(text).issues[0]
    assert issue.check == "ed.structure.search_missing"
    assert issue.address.startswith("ПКО/Тест/Поиск/")
    assert issue.message == "Поле поиска «Нет» отсутствует в структуре объекта."
    assert issue.level.value == "предупреждение"
    assert not check(text.replace('"Нет"', '"Ссылка.Код"')).issues


def table_text(configuration="Товары", field="Количество"):
    return BASE.replace(
        "// <properties>",
        f'''СвойстваТЧ = ДобавитьПКТЧ(ПравилоКонвертации, "{configuration}", "Товары");
    ДобавитьПКС(СвойстваТЧ, "{field}", "Количество", 0);''',
    )


def test_table_parent_suppresses_cascade_and_exact_kind():
    report = check(table_text("Нет", "Нет"))
    assert [i.message for i in report.issues] == [
        "Свойство конфигурации «Нет» отсутствует в структуре."
    ]
    assert any(s.reason.startswith("owner_object_unavailable:") for s in report.skipped)
    assert check(table_text()).issues == []
    issue = check(table_text(field="Нет")).issues[0]
    assert issue.message == "Свойство конфигурации «Товары.Нет» отсутствует в структуре."


@pytest.mark.parametrize(
    "replacement,reason",
    [
        ("Неопределено", "structure_source"),
        ("ПолучитьОбъект()", "dynamic_configuration_object"),
        ("Метаданные.Новые.Тест", "unknown_collection"),
    ],
)
def test_opaque_configuration(replacement, reason):
    report = check(BASE.replace("Метаданные.Справочники.Тест", replacement))
    assert not report.errors
    assert any(
        s.check == "ed.structure.object_missing" and s.reason.startswith(reason + ":")
        for s in report.skipped
    )


def test_ambiguous_field_and_unknown_parent():
    doc = document()
    owner = replace(
        doc.pko[0],
        configuration_object=replace(doc.pko[0].configuration_object, presence="ambiguous"),
    )
    doc = replace(doc, pko=(owner,))
    coverage = Counter()
    report = validate_structure(
        doc, snapshot(), build_addresses(doc), ValidationProfile.build(None), coverage
    )
    assert not report.errors
    assert coverage["opaque_conditions"] > 0


def test_pkpd_wrong_owner_and_dynamic_value():
    assert any(
        i.check == "ed.structure.pkpd_value_missing"
        for i in check(BASE.replace("Перечисления.Выбор.А", "Справочники.Тест.А")).issues
    )
    report = check(BASE.replace("Перечисления.Выбор.А", "ПолучитьЗначение()"))
    assert not report.issues
    assert any(s.reason.startswith("dynamic_configuration_value:") for s in report.skipped)


def test_coverage_counts_unique_check_entity_for_search_fields():
    doc = document(
        BASE.replace("// <properties>", 'ПравилоКонвертации.ПоляПоиска.Добавить("Нет");')
    )
    owner = doc.pko[0]
    search = owner.search_sets[0]
    coverage = []
    for fields in [("Нет",), ("Нет", "Другой")]:
        updated = replace(doc, pko=(replace(owner, search_sets=(replace(search, fields=fields),)),))
        counter = Counter()
        report = validate_structure(
            updated, snapshot(), build_addresses(updated), ValidationProfile.build(None), counter
        )
        assert len(report.issues) == len(fields)
        coverage.append(counter)
    assert coverage[0] == coverage[1]


@pytest.mark.parametrize(
    "attribute",
    ["Ссылка", "Предопределенный", "ИмяПредопределенныхДанных", "ВерсияДанных", "ПометкаУдаления"],
)
def test_absent_standard_attributes_are_skipped_for_properties_and_search(attribute):
    snap = snapshot()
    key = ("справочник", "тест")
    owner = snap.objects[key]
    # Вариант выгрузки без стандартного реквизита Ссылка.
    owner = replace(
        owner,
        properties=MappingProxyType(
            {k: v for k, v in owner.properties.items() if k[0] != "ссылка"}
        ),
    )
    snap = replace(snap, objects=MappingProxyType({**snap.objects, key: owner}))
    text = BASE.replace(
        "// <properties>",
        f'ДобавитьПКС(СвойстваШапки, "{attribute}", "Номер", 0);\n'
        f'ПравилоКонвертации.ПоляПоиска.Добавить("{attribute}");',
    )
    doc = document(text)
    coverage = Counter()
    report = validate_structure(
        doc, snap, build_addresses(doc), ValidationProfile.build(None), coverage
    )
    assert not report.issues
    assert {(s.check, s.reason.split(":")[0]) for s in report.skipped} >= {
        ("ed.structure.property_missing", "standard_attribute"),
        ("ed.structure.search_missing", "standard_attribute"),
    }
    assert any(
        i.check == "ed.structure.property_missing"
        for i in check(text.replace(attribute, "Нестандартный")).issues
    )


def test_standard_attributes_depend_on_object_kind_and_table_context():
    from kd_rules_mcp.structures.xmldump import KINDS

    assert {row[2].casefold(): row[1] for row in KINDS.values()} == COLLECTIONS
    doc = document(
        BASE.replace("// <properties>", 'ДобавитьПКС(СвойстваШапки, "Ссылка", "Номер", 0);')
    )
    owner = snapshot().objects[("справочник", "тест")]
    obj = replace(
        owner,
        kind="РегистрСведений",
        properties=MappingProxyType(
            {k: v for k, v in owner.properties.items() if k[0] != "ссылка"}
        ),
    )
    snap = replace(
        snapshot(), objects=MappingProxyType({**snapshot().objects, ("справочник", "тест"): obj})
    )
    report = validate_structure(doc, snap, build_addresses(doc), ValidationProfile.build(None))
    assert any(
        i.message == "Свойство конфигурации «Ссылка» отсутствует в структуре."
        for i in report.issues
    )
    report = check(table_text(field="НомерСтроки"))
    assert not report.issues
    assert any(s.reason.startswith("standard_attribute: 1;") for s in report.skipped)
    assert any(
        i.check == "ed.structure.property_missing"
        for i in check(table_text(field="НомерСтрокии")).issues
    )
    assert any(
        s.reason.startswith("standard_attribute:")
        for s in check(
            table_text().replace(
                "ДобавитьПКС(СвойстваТЧ,",
                'ПравилоКонвертации.ПоляПоиска.Добавить("Товары.НомерСтроки");\nДобавитьПКС(СвойстваТЧ,',
            )
        ).skipped
    )


def test_opaque_owner_keeps_children_in_skipped_and_coverage():
    call = "    ДобавитьПКО_Тест(ПравилаКонвертации);"
    text = BASE.replace(call, "Если Опак() Тогда\n" + call + "\nКонецЕсли;", 1).replace(
        "// <properties>", 'ПравилоКонвертации.ПоляПоиска.Добавить("Нет");'
    )
    doc = document(text)
    counter = Counter()
    report = validate_structure(
        doc, snapshot(), build_addresses(doc), ValidationProfile.build(None, "1.2"), counter
    )
    assert not report.issues
    assert {s.check for s in report.skipped} >= {
        "ed.structure.object_missing",
        "ed.structure.property_missing",
        "ed.structure.search_missing",
    }
    assert all(s.reason.startswith("opaque_condition: 1;") for s in report.skipped)
    assert counter["opaque_conditions"] == 3
    assert any(
        i.check == "ed.structure.search_missing"
        for i in check(
            text.replace("Опак()", 'КомпонентыОбмена.ВерсияФорматаОбмена = "1.2"')
        ).issues
    )


def test_empty_table_configuration_side_is_distinct_from_missing_owner():
    report = check(table_text(configuration=""))
    assert not report.issues
    assert any(
        s.check == "ed.structure.property_missing"
        and s.reason.startswith("empty_configuration_side: 2;")
        for s in report.skipped
    )
    assert not any(s.reason.startswith("owner_object_unavailable:") for s in report.skipped)
    assert not check(table_text()).skipped


def test_skip_counts_and_address_cap_deduplicate_instances_and_directions():
    text = BASE.replace(
        "// <properties>",
        "\n".join(f'ДобавитьПКС(СвойстваШапки, "Код", "Поле{i}", 0);' for i in range(7)),
    )
    doc = document(text)
    context = CheckContext(doc, build_addresses(doc), ValidationProfile.build(None))
    for direction in ("send", "receive"):
        for prop in doc.pko[0].properties:
            for _ in range(3):
                assert context.start("ed.schema.property_missing", prop, direction, doc.pko[0])
                context.skip("ed.schema.property_missing", "opaque_condition", prop)
    skipped = context.finish().skipped[0]
    count, addresses = skipped.reason.split("; ")
    assert count == "opaque_condition: 8"
    assert len(addresses.split(", ")) == 5
    assert context.coverage["opaque_conditions"] == 8
