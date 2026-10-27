"""Общее ядро звеньев: состояние, порядок таблицы, свойство в схеме."""

from kd_rules_mcp.ed.address import build_addresses
from kd_rules_mcp.ed.schema import load_schema
from kd_rules_mcp.ed.schema.profile import ValidationProfile
from kd_rules_mcp.validation.ed_chain import (
    NOT_APPLICABLE,
    OK,
    PARTIAL,
    ChainView,
    LinkState,
    format_path,
    metadata_name,
    properties,
)
from tests.test_ed_profile import BASE, DATA, document


def view(text=BASE, direction="send", schema=True, version="1.2"):
    doc = document(text)
    loaded = load_schema(DATA / "validation.bin") if schema else None
    profile = ValidationProfile.build(loaded, version, direction)
    return ChainView.build("p", doc, build_addresses(doc), direction, profile)


def test_state_ok_and_type_resolved():
    chain = view()
    rule = chain.document.pko[0]
    assert chain.state(rule) == LinkState(OK)
    typ, status = chain.type_state(rule)
    assert status == "resolved" and typ is not None
    assert chain.address(rule) == "ПКО/Тест"
    assert chain.origin(rule) is None


def test_state_direction_false_and_opaque():
    text = BASE.replace(
        "    ДобавитьПОД_Тест(ПравилаОбработкиДанных);",
        '    Если НаправлениеОбмена = "Получение" Тогда\n'
        "        ДобавитьПОД_Тест(ПравилаОбработкиДанных);\n    КонецЕсли;",
    )
    chain = view(text)
    assert chain.state(chain.document.pod[0]).status == NOT_APPLICABLE
    opaque = BASE.replace(
        "    ДобавитьПОД_Тест(ПравилаОбработкиДанных);",
        "    Если ПараметрНеизвестен() Тогда\n"
        "        ДобавитьПОД_Тест(ПравилаОбработкиДанных);\n    КонецЕсли;",
    )
    state = view(opaque).state(view(opaque).document.pod[0])
    assert state.status == PARTIAL and "opaque_condition" in state.reasons


def test_property_schema_key_and_missing():
    text = BASE.replace(
        "    // <properties>",
        '    ДобавитьПКС(СвойстваШапки, "Номер", "Номер", 0);\n'
        '    ДобавитьПКС(СвойстваШапки, "Нет", "НетТакого", 0);',
    )
    chain = view(text)
    rule = chain.document.pko[0]
    found = {
        prop.format_property: chain.property_schema(rule, g, prop) for g, prop in properties(rule)
    }
    assert found["Код"].status == "resolved" and found["Код"].key is True
    assert found["Номер"].key is False
    assert found["НетТакого"].status == "missing"


def test_property_schema_without_schema():
    chain = view(schema=False)
    rule = chain.document.pko[0]
    group, prop = next(properties(rule))
    assert chain.property_schema(rule, group, prop).status == "owner_type_unavailable"


def test_metadata_name_and_format_path():
    chain = view()
    assert chain.pko_metadata(chain.document.pko[0]) == ("Справочник.Тест", "resolved")
    assert metadata_name(None) == (None, "configuration_object_unavailable")
    rule = chain.document.pko[0]
    group, prop = next(properties(rule))
    assert format_path(group, prop) == "Код"


def test_table_order_follows_fill_calls():
    text = BASE.replace(
        "    ДобавитьПОД_Тест(ПравилаОбработкиДанных);",
        "    ДобавитьПОД_Второй(ПравилаОбработкиДанных);\n"
        "    ДобавитьПОД_Тест(ПравилаОбработкиДанных);",
    ).replace(
        "Процедура ДобавитьПКО_Тест(",
        "Процедура ДобавитьПОД_Второй(ПравилаОбработкиДанных)\n"
        "    ПравилоОбработки = ПравилаОбработкиДанных.Добавить();\n"
        '    ПравилоОбработки.Имя = "Второй";\n'
        '    ПравилоОбработки.ОбъектВыборкиФормат = "Справочник.Тест";\n'
        '    ПравилоОбработки.ИспользуемыеПКО.Добавить("Тест");\n'
        "КонецПроцедуры\nПроцедура ДобавитьПКО_Тест(",
    )
    chain = view(text, direction="receive")
    assert [pod.name for pod in chain.pods()] == ["Второй", "Тест"]
