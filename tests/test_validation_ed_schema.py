"""Каждая схема-проверка: нарушение, уровень, адрес и чистая граница."""

from collections import Counter
from dataclasses import replace

import pytest

from kd2_rules_mcp.ed.address import build_addresses
from kd2_rules_mcp.ed.schema import load_schema
from kd2_rules_mcp.ed.schema.profile import Applicability, ValidationProfile
from kd2_rules_mcp.validation.ed_schema import validate_schema
from tests.test_ed_profile import BASE, DATA, document
from tests.test_validation_ed_structure import snapshot, table_text


def check(text=BASE, direction="both", schema=None, *, include_value_ranges=False):
    doc = document(text)
    schema = schema or load_schema(DATA / "validation.bin")
    profile = ValidationProfile.build(schema, "1.2", direction)
    return validate_schema(
        doc,
        schema,
        build_addresses(doc),
        profile,
        snapshot(),
        include_value_ranges=include_value_ranges,
        legacy_atomic_only=False,
    )


@pytest.mark.parametrize("direction", ["send", "receive"])
def test_direct_faceted_strings_use_candidate_compatibility(tmp_path, direction):
    from kd2_rules_mcp.authoring.ed.candidates import compatibility

    path = tmp_path / "facets.bin"
    text = (DATA / "validation.bin").read_text(encoding="utf-8")
    path.write_text(
        text.replace('name="Код" type="xs:string"', 'name="Код" type="t:Короткая"').replace(
            "</package>",
            '<valueType name="Короткая" base="xs:string">'
            "<maxLength>2</maxLength></valueType></package>",
        ),
        encoding="utf-8",
    )
    schema = load_schema(path)
    profile = ValidationProfile.build(schema, "1.2", direction)
    owner, _ = profile.find_type("Справочник.Тест")
    assert owner is not None
    prop = profile.properties[profile.resolve(owner, "Код").property_ids[0]]
    attribute = snapshot().objects[("справочник", "тест")].property("Код")[0]
    expected = compatibility(profile, prop, attribute, direction)
    report = check(direction=direction, schema=schema, include_value_ranges=True)
    assert not any(
        s.check == "ed.schema.type_incompatible" and "non_atomic_type" in s.reason
        for s in report.skipped
    )
    ranges = [i for i in report.issues if i.check == "ed.schema.value_range"]
    assert bool(ranges) == bool(expected.value_range)
    if ranges:
        assert expected.value_range is not None
        assert ranges[0].level.value == ("предупреждение" if direction == "send" else "info")
        assert direction in ranges[0].message
        assert ranges[0].address in ranges[0].message
        assert expected.value_range_consequence in ranges[0].message
        assert expected.value_range in ranges[0].message
        assert ranges[0].address == "ПКО/Тест/ПКС/Код"


def test_string_family_mismatch_is_checked():
    report = check(
        BASE.replace("// <properties>", 'ДобавитьПКС(СвойстваШапки, "Флаг", "Номер", 0);')
    )
    assert any(
        i.check == "ed.schema.type_incompatible" and i.address == "ПКО/Тест/ПКС/Номер"
        for i in report.issues
    )


@pytest.mark.parametrize("event", ["ПриОтправкеДанных", "ПриКонвертацииДанныхXDTO"])
@pytest.mark.parametrize("legacy", [False, True])
def test_batch3_direct_types_are_checked_with_owner_handler(event, legacy):
    text = BASE.replace(
        "// <properties>",
        f'ПравилоКонвертации.{event} = "Отменить";\nДобавитьПКС(СвойстваШапки, "Флаг", "Дата", 0);',
    )
    doc = document(text)
    schema = load_schema(DATA / "validation.bin")
    report = validate_schema(
        doc,
        schema,
        build_addresses(doc),
        ValidationProfile.build(schema, "1.2"),
        snapshot(),
        legacy_atomic_only=legacy,
    )
    assert any(
        i.check == "ed.schema.type_incompatible" and i.address.endswith("/Дата")
        for i in report.issues
    )
    assert not any(
        s.check == "ed.schema.type_incompatible" and s.reason.startswith("handler_may_supply:")
        for s in report.skipped
    )


def test_batch3_required_property_with_send_handler_stays_warning():
    text = BASE.replace('ДобавитьПКС(СвойстваШапки, "Код", "Код", 0);', "").replace(
        "// <properties>",
        'ПравилоКонвертации.ПриОтправкеДанных = "Отменить";',
    )
    report = check(text, "send")
    issue = next(i for i in report.issues if i.check == "ed.schema.required_source")
    assert issue.level.value == "предупреждение"
    assert "КлючевыеСвойства.Код" in issue.message
    assert "обработчик отправки может" in issue.message.casefold()
    assert not any(
        s.check == issue.check and "handler_may_supply" in s.reason for s in report.skipped
    )


@pytest.mark.parametrize("algorithm", [False, True])
def test_roundtrip_review_required_table_column_with_handler(algorithm, tmp_path):
    text = table_text().replace(
        "СвойстваТЧ =", 'ПравилоКонвертации.ПриОтправкеДанных = "Fill";\nСвойстваТЧ ='
    )
    # Количество обязательно в схеме; описана только другая колонка.
    text = text.replace(
        '"Количество", "Количество", 0', '"Код", "Строка", 1' if algorithm else '"Код", "Строка", 0'
    )
    path = tmp_path / "required-row.bin"
    path.write_text(
        (DATA / "validation.bin")
        .read_text("utf-8")
        .replace(
            'name="Количество" type="xs:decimal" lowerBound="0"',
            'name="Количество" type="xs:decimal" lowerBound="1"',
        ),
        encoding="utf-8",
    )
    issue = next(
        i
        for i in check(text, "send", load_schema(path)).issues
        if i.check == "ed.schema.required_source" and "/ПКТЧ/" in i.address
    )
    assert issue.level.value == "предупреждение"
    assert ("обработчик отправки может" in issue.message.casefold()) is not algorithm


def test_batch3_empty_format_table_explains_children_skip():
    text = table_text("КонтактнаяИнформация").replace(
        '"КонтактнаяИнформация", "Товары"',
        '"КонтактнаяИнформация", ""',
    )
    report = check(text, "receive")
    child = [
        s
        for s in report.skipped
        if s.check in {"ed.schema.property_missing", "ed.schema.type_incompatible"}
    ]
    assert child and all(s.reason.startswith("empty_format_side:") for s in child)
    from kd2_rules_mcp.service.ed_views import validation_view

    view = validation_view(report, None, None, None, "skipped", 0, 200, explain_skipped=True)
    for row in view["skipped"]["items"]:
        if row["check"].startswith("ed.schema."):
            assert "откройте схему" not in row.get("hint", "").casefold()
            assert "сторона формата не задана" in row["hint"].casefold()


@pytest.mark.parametrize(
    "old,new,check_id,address,message",
    [
        (
            '"Справочник.Тест";',
            '"НетТипа";',
            "type_missing",
            "ПКО/Тест",
            "Тип формата «НетТипа» отсутствует в выбранной схеме; ПКО исключается исполнителем.",
        ),
        (
            '"Код", "Код", 0',
            '"Код", "Кодд", 0',
            "property_missing",
            "ПКО/Тест/ПКС/Кодд",
            "Свойство формата «Кодд» отсутствует в выбранном профиле; "
            "проверьте версию и обработчик.",
        ),
        (
            'ТипXDTO = "Выбор"',
            'ТипXDTO = "НетТипа"',
            "pkpd_type_missing",
            "ПКПД/Выбор",
            "Тип формата ПКПД «НетТипа» отсутствует в выбранной схеме.",
        ),
        (
            'Вставить("A", Перечисления.Выбор.А)',
            'Вставить("Z", Перечисления.Выбор.А)',
            "pkpd_value_missing",
            None,
            "Значение «Z» не входит в перечисление формата «Выбор».",
        ),
        (
            '    ДобавитьПКС(СвойстваШапки, "Код", "Код", 0);',
            "",
            "required_source",
            "ПКО/Тест",
            "Для обязательного свойства «КлючевыеСвойства.Код» не подтверждён источник значения.",
        ),
    ],
)
def test_violation_and_clean(old, new, check_id, address, message):
    issues = [i for i in check(BASE.replace(old, new)).issues if i.check == "ed.schema." + check_id]
    assert len(issues) == 1
    issue = issues[0]
    assert issue.level.value == "предупреждение" and issue.message == message
    assert issue.address == address if address else issue.address.startswith("ПКПД/Выбор/Значение/")
    assert not [i for i in check().issues if i.check == "ed.schema." + check_id]


def test_table_criterion_and_children():
    assert not check(table_text()).issues
    report = check(table_text().replace('"Товары");', '"Плохие");'))
    issue = next(i for i in report.issues if i.check == "ed.schema.table_missing")
    assert issue.level.value == "предупреждение"
    assert issue.address == "ПКО/Тест/ПКТЧ/Плохие"
    assert issue.message == "Группа «Плохие» не разрешается как табличная часть формата."
    assert any(
        s.check == "ed.schema.property_missing" and s.reason.startswith("owner_type_unavailable:")
        for s in report.skipped
    )


def test_types_incompatible_and_atomic_boundary():
    text = BASE.replace("// <properties>", 'ДобавитьПКС(СвойстваШапки, "Флаг", "Дата", 0);')
    issue = next(i for i in check(text).issues if i.check == "ed.schema.type_incompatible")
    assert issue.level.value == "предупреждение"
    assert issue.address == "ПКО/Тест/ПКС/Дата"
    assert issue.message == "Типы свойства «Дата» требуют преобразования, не описанного прямым ПКС."
    assert not check(text.replace('"Флаг", "Дата"', '"Дата", "Дата"')).issues


@pytest.mark.parametrize("name", ["Перечисление", "Объединение", "Произвольный"])
def test_unproven_atomic_families_are_skipped(name):
    report = check(
        BASE.replace("// <properties>", f'ДобавитьПКС(СвойстваШапки, "Флаг", "{name}", 0);')
    )
    assert not [i for i in report.issues if i.check == "ed.schema.type_incompatible"]
    assert any(
        s.check == "ed.schema.type_incompatible" and s.reason.startswith("non_atomic_type:")
        for s in report.skipped
    )


def test_imported_ref_ancestry_is_excluded_from_atomic_comparison():
    schema = load_schema(DATA / "base.bin", locate_import=lambda _: DATA / "message.bin")
    text = BASE.replace('ОбъектФормата = "Справочник.Тест"', 'ОбъектФормата = "Item"').replace(
        "// <properties>", 'ДобавитьПКС(СвойстваШапки, "Флаг", "Link", 0);'
    )
    report = check(text, schema=schema)
    assert not [i for i in report.issues if i.check == "ed.schema.type_incompatible"]
    assert any(
        s.check == "ed.schema.type_incompatible" and s.reason.startswith("non_atomic_type:")
        for s in report.skipped
    )


@pytest.mark.parametrize("constraint", ['lowerBound="0"', 'nillable="true"'])
def test_optional_or_nullable_required_field_is_clean(tmp_path, constraint):
    path = tmp_path / "optional.bin"
    path.write_text(
        (DATA / "validation.bin")
        .read_text(encoding="utf-8")
        .replace('name="Код" type="xs:string"/', f'name="Код" type="xs:string" {constraint}/'),
        encoding="utf-8",
        newline="\n",
    )
    text = BASE.replace('ДобавитьПКС(СвойстваШапки, "Код", "Код", 0);', "")
    assert not [
        i
        for i in check(text, schema=load_schema(path)).issues
        if i.check == "ed.schema.required_source"
    ]


def test_search_source_maps_configuration_to_format():
    text = BASE.replace("// <properties>", 'ПравилоКонвертации.ПоляПоиска.Добавить("Дата");')
    issue = next(i for i in check(text).issues if i.check == "ed.schema.search_source")
    assert issue.level.value == "предупреждение" and issue.address.startswith("ПКО/Тест/Поиск/")
    assert issue.message == "Для поля поиска «Дата» не подтверждён источник в формате."
    supplied = text.replace(
        "ПравилоКонвертации.ПоляПоиска.",
        'ДобавитьПКС(СвойстваШапки, "Дата", "Номер", 0);\n    ПравилоКонвертации.ПоляПоиска.',
        1,
    )
    assert not [i for i in check(supplied).issues if i.check == "ed.schema.search_source"]
    for field in ("ЭтоГруппа", "Родитель"):
        assert not check(text.replace('"Дата"', f'"{field}"')).issues


def test_pko_unavailable_only_existing_target_with_nonempty_type():
    text = BASE.replace(
        "// <properties>", 'ДобавитьПКС(СвойстваШапки, "Код", "Номер", 0, "Другой");'
    )
    doc = document(text)
    target = replace(
        doc.pko[0],
        entity_id="other",
        name="Другой",
        format_object=replace(doc.pko[0].format_object, value="НетТипа"),
    )
    doc = replace(doc, pko=(*doc.pko, target))
    schema = load_schema(DATA / "validation.bin")
    report = validate_schema(doc, schema, build_addresses(doc), ValidationProfile.build(schema))
    issue = next(i for i in report.issues if i.check == "ed.schema.pko_unavailable")
    assert issue.level.value == "предупреждение" and issue.address == "ПКО/Тест/ПКС/Номер"
    assert issue.message == "ПКО «Другой» недоступен в выбранной схеме формата."
    target = replace(target, format_object=replace(target.format_object, value=""))
    doc = replace(doc, pko=(doc.pko[0], target))
    assert not [
        i
        for i in validate_schema(
            doc, schema, build_addresses(doc), ValidationProfile.build(schema)
        ).issues
        if i.check == "ed.schema.pko_unavailable"
    ]


def test_required_handler_and_full_object_proof():
    text = BASE.replace('ДобавитьПКС(СвойстваШапки, "Код", "Код", 0);', "")
    for event in ("ПриОтправкеДанных", "ПриКонвертацииДанныхXDTO", "ПослеЗагрузкиВсехДанных"):
        report = check(
            text.replace("// <properties>", f'ПравилоКонвертации.{event} = "Заполнить";'), "send"
        )
        issues = [i for i in report.issues if i.check == "ed.schema.required_source"]
        assert len(issues) == 1
        assert ("обработчик отправки" in issues[0].message) == (event == "ПриОтправкеДанных")
        assert not any(
            s.check == "ed.schema.required_source" and s.reason.startswith("handler_may_supply:")
            for s in report.skipped
        )
    report = check(text.replace('ПравилоОбработки.ИспользуемыеПКО.Добавить("Тест");', ""))
    assert not [i for i in report.issues if i.check == "ed.schema.required_source"]
    assert any(s.reason.startswith("full_object_not_proven:") for s in report.skipped)


def test_pkpd_enum_inheritance_unrestricted_and_direction():
    bad = BASE.replace('Вставить("A", Перечисления.Выбор.А)', 'Вставить("Z", Перечисления.Выбор.А)')
    assert not [i for i in check(bad, "send").issues if i.check == "ed.schema.pkpd_value_missing"]
    assert any(
        i.check == "ed.schema.pkpd_value_missing"
        for i in check(bad.replace('ТипXDTO = "Выбор"', 'ТипXDTO = "НаследованныйВыбор"')).issues
    )
    assert not check(bad.replace('ТипXDTO = "Выбор"', 'ТипXDTO = "Свободный"')).issues


def test_unknown_conditions_duplicates_and_empty_sides():
    text = BASE.replace(
        'ДобавитьПКС(СвойстваШапки, "Код", "Код", 0);',
        """ДобавитьПКС(СвойстваШапки, "Код", "Нет", 0);
    ДобавитьПКС(СвойстваШапки, "Код", "Нет", 0);""",
    )
    addresses = {i.address for i in check(text).issues if i.check == "ed.schema.property_missing"}
    assert addresses == {"ПКО/Тест/ПКС/Нет#1", "ПКО/Тест/ПКС/Нет#2"}
    doc = document()
    owner = doc.pko[0]
    doc = replace(
        doc,
        pko=(replace(owner, properties=(replace(owner.properties[0], condition_name="Условие"),)),),
    )
    schema = load_schema(DATA / "validation.bin")
    coverage = Counter()
    report = validate_schema(
        doc, schema, build_addresses(doc), ValidationProfile.build(schema), coverage=coverage
    )
    assert not report.errors and coverage["opaque_conditions"] > 0
    assert any(s.reason.startswith("opaque_condition:") for s in report.skipped)
    assert any(
        s.reason.startswith("empty_format_side:")
        for s in check(BASE.replace('"Код", "Код", 0', '"Код", "", 0')).skipped
    )


def test_active_extension_and_ambiguous_alias():
    schema = load_schema(DATA / "validation.bin", extensions=(DATA / "validation-extension.bin",))
    text = BASE.replace(
        "// <properties>", (DATA / "rules-extension.bsl").read_text(encoding="utf-8")
    )
    assert not check(text, schema=schema).issues
    no_extension = check(text)
    assert not [
        i
        for i in no_extension.issues
        if i.check in ("ed.schema.property_missing", "ed.schema.table_missing")
    ]
    text = text.replace('"ДополнительныйКод"', '"Алиас"')
    report = check(text, schema=schema)
    assert not [i for i in report.issues if i.check == "ed.schema.property_missing"]
    assert any(
        s.check == "ed.schema.property_missing" and s.reason.startswith("ambiguous:")
        for s in report.skipped
    )
    # Optional оболочки расширения не требуют свойств детей.
    assert not [i for i in report.issues if i.check == "ed.schema.required_source"]


def test_missing_import_and_ambiguous_format_field():
    schema = load_schema(DATA / "base.bin")
    text = BASE.replace("Справочник.Тест", "Item").replace(
        'ТипXDTO = "Выбор"', 'ТипXDTO = "Choice"'
    )
    report = check(text, schema=schema)
    assert not [i for i in report.issues if i.check.startswith("ed.schema.")]
    assert any(s.reason.startswith("owner_type_unavailable:") for s in report.skipped)
    doc = document()
    owner = replace(
        doc.pko[0], format_object=replace(doc.pko[0].format_object, presence="ambiguous")
    )
    doc = replace(doc, pko=(owner,))
    schema = load_schema(DATA / "validation.bin")
    report = validate_schema(doc, schema, build_addresses(doc), ValidationProfile.build(schema))
    assert not report.issues
    assert any(
        s.check == "ed.schema.type_missing" and s.reason.startswith("opaque_condition:")
        for s in report.skipped
    )


def test_search_source_ambiguous_resolution_and_extension():
    schema = load_schema(DATA / "validation.bin", extensions=(DATA / "validation-extension.bin",))
    text = BASE.replace(
        "// <properties>",
        'ДобавитьПКС(СвойстваШапки, "Дата", "Алиас", 0, "", "urn:test:validation-extension");\n'
        'ПравилоКонвертации.ПоляПоиска.Добавить("Дата");',
    )
    report = check(text, schema=schema)
    assert not [i for i in report.issues if i.check == "ed.schema.search_source"]
    assert any(
        s.check == "ed.schema.search_source" and s.reason.startswith("ambiguous:")
        for s in report.skipped
    )
    report = check(text.replace('"Алиас"', '"ДополнительныйКод"'), schema=schema)
    assert not [s for s in report.skipped if s.check == "ed.schema.search_source"]
    assert not [i for i in report.issues if i.check == "ed.schema.search_source"]


def test_unknown_source_and_false_handler_do_not_hide_required_violation():
    schema = load_schema(DATA / "validation.bin")
    doc = document()
    owner = doc.pko[0]
    owner = replace(owner, properties=(replace(owner.properties[0], condition_name="Условие"),))
    doc = replace(doc, pko=(owner,))
    report = validate_schema(
        doc, schema, build_addresses(doc), ValidationProfile.build(schema, "1.2")
    )
    assert not [i for i in report.issues if i.check == "ed.schema.required_source"]
    assert any(
        s.check == "ed.schema.required_source" and s.reason.startswith("opaque_condition:")
        for s in report.skipped
    )
    text = BASE.replace('ДобавитьПКС(СвойстваШапки, "Код", "Код", 0);', "").replace(
        "// <properties>",
        'Если КомпонентыОбмена.ВерсияФорматаОбмена = "1.0" Тогда\n'
        '    ПравилоКонвертации.ПриОтправкеДанных = "Заполнить";\nКонецЕсли;',
    )
    assert any(i.check == "ed.schema.required_source" for i in check(text, "send").issues)


def test_type_only_in_active_extension():
    schema = load_schema(DATA / "validation.bin", extensions=(DATA / "validation-extension.bin",))
    text = BASE.replace(
        'ОбъектФормата = "Справочник.Тест"', 'ОбъектФормата = "Справочник.Дополнение"'
    )
    assert any(i.check == "ed.schema.type_missing" for i in check(text, schema=schema).issues)
    assert any(i.check == "ed.schema.type_missing" for i in check(text).issues)
    declared = text.replace(
        "// <properties>",
        "ОбменДаннымиXDTOСервер.ИнициализироватьРасширениеПравилаКонвертацииОбъекта("
        'ПравилоКонвертации, "urn:test:validation-extension");',
    )
    assert not [
        i for i in check(declared, schema=schema).issues if i.check == "ed.schema.type_missing"
    ]
    report = check(declared)
    assert not [i for i in report.issues if i.check == "ed.schema.type_missing"]
    assert any(
        s.check == "ed.schema.type_missing" and s.reason.startswith("partial_schema:")
        for s in report.skipped
    )


def test_rule_extensions_are_ordered_and_unloaded_predecessor_is_unknown():
    schema = load_schema(
        DATA / "validation.bin",
        extensions=(DATA / "validation-extension.bin", DATA / "validation-second-extension.bin"),
    )
    doc = document()
    owner = replace(
        doc.pko[0],
        format_object=replace(doc.pko[0].format_object, value="Справочник.Дополнение"),
        extensions=("urn:test:validation-second-extension", "urn:test:validation-extension"),
    )
    profile = ValidationProfile.build(schema)
    app = Applicability.build(doc, profile)
    typ, status = profile.owner_type(owner, "send", app)
    assert status == "resolved" and typ and typ.qname
    assert typ.qname.namespace == "urn:test:validation-second-extension"
    owner = replace(owner, extensions=("urn:test:unloaded", *owner.extensions))
    assert profile.owner_type(owner, "send", app) == (None, "partial_schema")


def test_deleted_owner_suppresses_schema_and_structure_children():
    text = BASE.replace('ОбъектФормата = "Справочник.Тест"', 'ОбъектФормата = "НетТипа"').replace(
        "// <properties>", 'ДобавитьПКС(СвойстваШапки, "Нет", "Номер", 0, "Тест");'
    )
    report = check(text)
    assert {i.check for i in report.issues} == {"ed.schema.type_missing"}
    assert any(
        s.check == "ed.schema.pko_unavailable" and s.reason.startswith("owner_type_unavailable:")
        for s in report.skipped
    )
    from kd2_rules_mcp.validation.ed_structure import validate_structure

    doc = document(text)
    schema = load_schema(DATA / "validation.bin")
    result = validate_structure(
        doc, snapshot(), build_addresses(doc), ValidationProfile.build(schema)
    )
    assert not result.issues
    assert any(
        s.check == "ed.structure.property_missing"
        and s.reason.startswith("owner_type_unavailable:")
        for s in result.skipped
    )
    restored = text.replace('ОбъектФормата = "НетТипа"', 'ОбъектФормата = "Справочник.Тест"')
    assert not [i for i in check(restored).issues if i.check == "ed.schema.pko_unavailable"]
    restored_doc = document(restored)
    assert any(
        i.check == "ed.structure.property_missing"
        for i in validate_structure(
            restored_doc, snapshot(), build_addresses(restored_doc), ValidationProfile.build(schema)
        ).issues
    )


def test_version_ordering_is_skipped_and_exact_equality_is_checked():
    call = "    ДобавитьПКО_Тест(ПравилаКонвертации);"
    text = BASE.replace('"Код", "Код", 0', '"Код", "Нет", 0').replace(
        call,
        'Если КомпонентыОбмена.ВерсияФорматаОбмена < "1.10" Тогда\n' + call + "\nКонецЕсли;",
        1,
    )
    report = check(text)
    assert not report.issues
    assert any(
        s.check == "ed.schema.property_missing" and s.reason.startswith("opaque_condition:")
        for s in report.skipped
    )
    assert any(
        i.check == "ed.schema.property_missing"
        for i in check(text.replace('< "1.10"', '= "1.2"')).issues
    )


def test_imported_pkpd_type_and_nonliteral_format_type():
    schema = load_schema(DATA / "base.bin", locate_import=lambda _: DATA / "message.bin")
    text = BASE.replace('ТипXDTO = "Выбор"', 'ТипXDTO = "{urn:test:message}Ref"')
    assert not [
        i for i in check(text, schema=schema).issues if i.check == "ed.schema.pkpd_type_missing"
    ]
    dynamic = check(BASE.replace('ТипXDTO = "Выбор"', "ТипXDTO = ПолучитьТип()"))
    assert not [i for i in dynamic.issues if i.check == "ed.schema.pkpd_type_missing"]
    assert any(
        s.check == "ed.schema.pkpd_type_missing" and s.reason.startswith("dynamic_format_type:")
        for s in dynamic.skipped
    )
    assert not [s for s in check().skipped if s.check == "ed.schema.pkpd_type_missing"]


def test_nonliteral_owner_type_is_dynamic_and_literal_boundary_is_clean():
    report = check(
        BASE.replace('ОбъектФормата = "Справочник.Тест"', "ОбъектФормата = ПолучитьТип()")
    )
    assert not report.issues
    assert any(
        s.check == "ed.schema.type_missing" and s.reason.startswith("dynamic_format_type:")
        for s in report.skipped
    )
    assert not [s for s in check().skipped if s.check == "ed.schema.type_missing"]


def test_nonempty_common_alias_collision_is_skipped_and_unique_alias_is_clean():
    schema = load_schema(DATA / "validation.bin", extensions=(DATA / "validation-extension.bin",))
    profile = ValidationProfile.build(schema)
    typ, _ = profile.find_type("Справочник.Тест", "urn:test:validation-extension")
    assert typ is not None
    resolved = profile.resolve(typ, "Алиас")
    assert resolved.status == "ambiguous" and len(resolved.property_ids) == 2
    assert all(profile.properties[ident].name.local == "Алиас" for ident in resolved.property_ids)
    text = BASE.replace(
        "// <properties>",
        'ДобавитьПКС(СвойстваШапки, "Код", "Алиас", 0, "", "urn:test:validation-extension");',
    )
    assert any(s.reason.startswith("ambiguous:") for s in check(text, schema=schema).skipped)
    assert not [
        s
        for s in check(text.replace('"Алиас"', '"ДополнительныйКод"'), schema=schema).skipped
        if s.reason.startswith("ambiguous:")
    ]


def test_empty_optional_table_does_not_require_row_properties(tmp_path):
    path = tmp_path / "required-row.bin"
    path.write_text(
        (DATA / "validation.bin")
        .read_text(encoding="utf-8")
        .replace(
            'name="Количество" type="xs:decimal" lowerBound="0"',
            'name="Количество" type="xs:decimal"',
        ),
        encoding="utf-8",
        newline="\n",
    )
    schema = load_schema(path)
    # Табличная часть не создаётся декларацией: обязательность строки не переносится в шапку.
    assert not [
        i for i in check(BASE, schema=schema).issues if i.check == "ed.schema.required_source"
    ]
    assert not [
        i
        for i in check(table_text(), schema=schema).issues
        if i.check == "ed.schema.required_source"
    ]
    empty = table_text().replace('ДобавитьПКС(СвойстваТЧ, "Количество", "Количество", 0);', "")
    report = check(empty, schema=schema)
    assert not [i for i in report.issues if i.check == "ed.schema.required_source"]
    assert any(
        s.check == "ed.schema.required_source"
        and s.reason.startswith("full_object_not_proven:")
        and "ПКТЧ/Товары" in s.reason
        for s in report.skipped
    )
    # При описанной строке отсутствие обязательного поля уже проверяется.
    incomplete_row = table_text().replace('"Количество", "Количество", 0', '"Код", "Строка", 0')
    assert any(
        i.check == "ed.schema.required_source" and i.address == "ПКО/Тест/ПКТЧ/Товары"
        for i in check(incomplete_row, schema=schema).issues
    )
