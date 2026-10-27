"""Входы разборов 26.10.2026, на которых чтение и проверка модуля КД 3 отвечали «всё хорошо» молча.

Изменение OpenSpec kd3-silent-fixes; разборы — docs/plans/evals/2026-10-26-kd3-review-*.md.
Ожидания записаны явно, вход — фикстура `manager_v2.bsl`/`manager_v3.bsl` с одной правкой.
"""

from pathlib import Path

import pytest

from kd_rules_mcp.ed import read_manager_text
from kd_rules_mcp.ed.address import build_addresses
from kd_rules_mcp.ed.refs import build_references
from kd_rules_mcp.service import Kd2Service, Settings
from kd_rules_mcp.validation.ed_interface import validate_interface
from kd_rules_mcp.validation.ed_links import validate_links

DATA = Path(__file__).parent / "data/ed"
# В фикстуре v2 есть одна намеренная ручная вставка; без неё модуль читается полностью.
V2 = (
    (DATA / "manager_v2.bsl")
    .read_text(encoding="utf-8")
    .replace("    ПравилоКонвертации.РучнаяВставка = ВычислитьЗначение();\n", "")
)
V3 = (DATA / "manager_v3.bsl").read_text(encoding="utf-8")


def patched(text: str, old: str, new: str) -> str:
    assert text.count(old) == 1, old
    return text.replace(old, new)


def read(text: str):
    return read_manager_text(text)


def links(text: str):
    doc = read(text)
    return validate_links(doc, build_addresses(doc), build_references(doc))


def interface(text: str):
    doc = read(text)
    return validate_interface(doc, build_addresses(doc))


def checks(report) -> list[str]:
    return sorted(issue.check for issue in report.issues)


def unknown(doc) -> list[str]:
    return sorted(item.reason for item in doc.unknown)


# --- 1. Интерфейс менеджера (Fable D2, Astra Д1) ---


def test_interface_clean_fixture():
    assert read(V2).parse_status.value == "complete"
    report = interface(V2)
    assert report.issues == [] and report.skipped == []


def test_interface_minimal_v3_fixture_lacks_executor_methods():
    # Синтетика v3 — только формы чтения; для исполнителя ей не хватает трёх методов.
    report = interface(V3)
    assert checks(report) == ["ed.conversion.required"] * 3
    assert sorted(i.message.split("»")[0] for i in report.issues) == [
        "Нет метода «ВыполнитьПроцедуруМодуляМенеджера",
        "Нет метода «ЗаполнитьПараметрыКонвертации",
        "Нет метода «ЗаполнитьПравилаОбработкиДанных",
    ]


@pytest.mark.parametrize(
    "routine",
    [
        "ЗаполнитьПараметрыКонвертации(ПараметрыКонвертации)",
        "ЗаполнитьПравилаКонвертацииПредопределенныхДанных(НаправлениеОбмена, ПравилаКонвертации)",
        "ЗаполнитьПравилаОбработкиДанных(НаправлениеОбмена, ПравилаОбработкиДанных)",
        "ВыполнитьПроцедуруМодуляМенеджера(ИмяПроцедуры, Параметры)",
    ],
)
def test_interface_missing_required(routine):
    # Переименование метода — то же, что отсутствие: исполнитель зовёт его по имени.
    text = patched(V2, f"Процедура {routine} Экспорт", f"Процедура Другое{routine} Экспорт")
    report = interface(text)
    assert checks(report) == ["ed.conversion.required"]
    name = routine.split("(")[0]
    assert f"Нет метода «{name}»" in report.issues[0].message
    assert "XDTO:" in report.issues[0].message


def test_interface_missing_event_left_to_links():
    text = patched(
        V2, "Процедура ПослеКонвертации(КомпонентыОбмена) Экспорт", "Процедура Иное() Экспорт"
    )
    assert checks(interface(text)) == []
    assert "ed.conversion.required" in checks(links(text))


def test_interface_export_removed():
    text = patched(
        V2,
        "Процедура ПередКонвертацией(КомпонентыОбмена) Экспорт",
        "Процедура ПередКонвертацией(КомпонентыОбмена)",
    )
    report = interface(text)
    assert checks(report) == ["ed.conversion.signature"]
    assert "нет «Экспорт»" in report.issues[0].message


def test_interface_extra_parameter_without_default():
    text = patched(
        V2,
        "Процедура ПередКонвертацией(КомпонентыОбмена) Экспорт",
        "Процедура ПередКонвертацией(КомпонентыОбмена, Обязательный) Экспорт",
    )
    report = interface(text)
    assert checks(report) == ["ed.conversion.signature"]
    assert "у лишних нет значения по умолчанию" in report.issues[0].message


def test_interface_extra_parameter_with_default_is_callable():
    text = patched(
        V2,
        "Процедура ПередКонвертацией(КомпонентыОбмена) Экспорт",
        "Процедура ПередКонвертацией(КомпонентыОбмена, Необязательный = Ложь) Экспорт",
    )
    assert checks(interface(text)) == []


def test_interface_missing_parameter():
    text = patched(
        V2,
        "Процедура ЗаполнитьПараметрыКонвертации(ПараметрыКонвертации) Экспорт",
        "Процедура ЗаполнитьПараметрыКонвертации() Экспорт",
    )
    report = interface(text)
    assert checks(report) == ["ed.conversion.signature"]
    assert "формальных параметров 0, исполнитель передаёт 1" in report.issues[0].message


def test_interface_v3_fill_needs_three_parameters():
    text = patched(
        V3,
        "ЗаполнитьПравилаКонвертацииОбъектов(КомпонентыОбмена, ПравилаКонвертации, "
        "ТолькоЗаголовки = Ложь) Экспорт",
        "ЗаполнитьПравилаКонвертацииОбъектов(КомпонентыОбмена, ПравилаКонвертации) Экспорт",
    )
    assert "ed.conversion.signature" in checks(interface(text))
    assert "ed.conversion.signature" not in checks(interface(V3))


def test_interface_function_contract_declared_as_procedure():
    text = patched(
        V2,
        "Функция ВыполнитьФункциюМодуляМенеджера(ИмяФункции, Параметры) Экспорт",
        "Процедура ВыполнитьФункциюМодуляМенеджера(ИмяФункции, Параметры) Экспорт",
    )
    text = patched(
        text,
        "        Возврат ПОД_Товары_ВыборкаДанных(Параметры.КомпонентыОбмена);\n    КонецЕсли;\n"
        "КонецФункции",
        "        ПОД_Товары_ВыборкаДанных(Параметры.КомпонентыОбмена);\n    КонецЕсли;\n"
        "КонецПроцедуры",
    )
    report = interface(text)
    assert checks(report) == ["ed.conversion.signature"]
    assert "объявлена процедура" in report.issues[0].message


def test_interface_selection_dispatcher_required_only_with_selection_handler():
    start = V2.index("Функция ВыполнитьФункциюМодуляМенеджера")
    end = V2.index("КонецФункции", start) + len("КонецФункции")
    without = V2[:start] + V2[end:]
    assert checks(interface(without)) == ["ed.conversion.required"]
    plain = patched(
        without, '    ПравилоОбработки.ВыборкаДанных = "ПОД_Товары_ВыборкаДанных";\n', ""
    )
    assert checks(interface(plain)) == []


def test_interface_duplicate_declaration():
    text = V2 + "\nПроцедура ЗаполнитьПараметрыКонвертации(Параметры) Экспорт\nКонецПроцедуры\n"
    report = interface(text)
    assert checks(report) == ["ed.conversion.signature"]
    assert "объявлен 2 раз" in report.issues[0].message


def test_interface_layers_skip_missing():
    text = patched(
        V2,
        "Процедура ЗаполнитьПараметрыКонвертации(ПараметрыКонвертации) Экспорт",
        "Процедура Иное(ПараметрыКонвертации) Экспорт",
    )
    doc = read(text)
    report = validate_interface(doc, build_addresses(doc), extensions=True)
    assert report.issues == []
    assert [item.check for item in report.skipped] == ["ed.conversion.required"]


@pytest.fixture
def service(tmp_path):
    return Kd2Service(Settings(cache_dir=tmp_path / "cache", workspace=tmp_path / "workspace"))


def test_service_validate_reports_interface(service, tmp_path):
    path = tmp_path / "manager.bsl"
    path.write_text(
        patched(
            V2,
            "Процедура ПередКонвертацией(КомпонентыОбмена) Экспорт",
            "Процедура ПередКонвертацией(КомпонентыОбмена)",
        ),
        encoding="utf-8",
    )
    project = service.ed_open(str(path))["project_id"]
    result = service.ed_validate(project, check_prefix="ed.conversion")
    assert [item["check"] for item in result["issues"]["items"]] == ["ed.conversion.signature"]


# --- 2. Ранний Возврат в ПКО (Fable D1) ---

EARLY = (
    '    Если НЕ ПолучитьФункциональнуюОпцию("X") Тогда\n        Возврат;\n    КонецЕсли;\n'
    "    СвойстваШапки = ПравилоКонвертации.Свойства;\n"
    '    ДобавитьПКС(СвойстваШапки, "Товар", "Product", , "Товар");'
)


def test_early_return_by_option_is_unknown():
    text = patched(
        V2,
        "    СвойстваШапки = ПравилоКонвертации.Свойства;\n"
        '    ДобавитьПКС(СвойстваШапки, "Товар", "Product", , "Товар");',
        EARLY,
    )
    doc = read(text)
    assert doc.parse_status.value == "partial"
    assert unknown(doc) == ["early_return"]
    order = next(rule for rule in doc.pko if rule.name == "Заказ")
    assert doc.unknown[0].owner_id == order.entity_id
    assert order.status.value == "partial"


def test_generator_headers_only_return_v3_is_complete():
    doc = read(V3)
    assert doc.parse_status.value == "complete", unknown(doc)


@pytest.mark.parametrize(
    "guard",
    [
        "    Если ТолькоЗаголовки Тогда\n        Возврат;\n        Сообщить(1);\n    КонецЕсли;\n",
        "    Если Ложь Тогда\n    ИначеЕсли ТолькоЗаголовки Тогда\n        Возврат;\n"
        "    КонецЕсли;\n",
        "    Если ТолькоЗаголовки И Истина Тогда\n        Возврат;\n    КонецЕсли;\n",
    ],
    ids=["extra-statement", "elseif", "compound"],
)
def test_headers_only_return_other_forms_unknown(guard):
    text = patched(
        V3,
        "    Если ТолькоЗаголовки Тогда\n        Возврат;\n    КонецЕсли;\n",
        guard,
    )
    assert "early_return" in unknown(read(text))


def test_headers_only_return_in_v2_unknown():
    text = patched(
        V2,
        "    СвойстваШапки = ПравилоКонвертации.Свойства;\n"
        '    ДобавитьПКС(СвойстваШапки, "Товар", "Product", , "Товар");',
        "    Если ТолькоЗаголовки Тогда\n        Возврат;\n    КонецЕсли;\n"
        "    СвойстваШапки = ПравилоКонвертации.Свойства;",
    )
    assert unknown(read(text)) == ["early_return"]


# --- 3. Процедура ПКПД (Astra Д2, Fable D7) ---

PKPD_HEAD = (
    "Процедура ЗаполнитьПравилаКонвертацииПредопределенныхДанных"
    "(НаправлениеОбмена, ПравилаКонвертации) Экспорт\n"
)


def pkpd_body(body: str) -> str:
    start = V2.index(PKPD_HEAD) + len(PKPD_HEAD)
    end = V2.index("КонецПроцедуры", start)
    return V2[:start] + body + V2[end:]


def test_pkpd_other_variable_is_not_empty():
    doc = read(
        pkpd_body(
            "    П = ПравилаКонвертации.Добавить();\n"
            '    П.ИмяПКПД = "Статусы";\n'
            '    П.ТипДанных = "ПеречислениеСсылка.Статусы";\n'
            '    П.ТипXDTO = "Status";\n'
        )
    )
    assert doc.pkpd == ()
    assert doc.parse_status.value == "partial"
    assert unknown(doc) == ["unsupported_pkpd_statement"] * 4


def test_pkpd_external_filler_is_not_empty():
    doc = read(pkpd_body("    ОбщийМодуль.ЗаполнитьПКПД(НаправлениеОбмена, ПравилаКонвертации);\n"))
    assert unknown(doc) == ["unsupported_pkpd_statement"]


def test_pkpd_empty_is_complete():
    doc = read(pkpd_body(""))
    assert doc.pkpd == () and doc.parse_status.value == "complete"


def test_pkpd_v3_preamble_is_generator_form():
    doc = read(V3)
    assert [rule.name for rule in doc.pkpd] == ["Статусы"]
    assert "pkpd_preamble" not in unknown(doc)


def test_pkpd_v3_preamble_alone_is_complete():
    start = V3.index("    Если УсловиеПримененияВыполняется")
    end = V3.index("КонецПроцедуры", start)
    doc = read(V3[:start] + V3[end:])
    assert doc.pkpd == () and doc.parse_status.value == "complete"


def test_pkpd_v3_preamble_partial_is_unknown():
    text = patched(V3, "    ПараметрСеанса_ВспомогательныеДанныеED = Неопределено;\n", "")
    assert "pkpd_preamble" in unknown(read(text))


def test_pkpd_v3_preamble_in_v2_is_unknown():
    preamble = V3[V3.index("Процедура ЗаполнитьПравилаКонвертацииПредопределенныхДанных") :]
    preamble = preamble[
        preamble.index("\n") + 1 : preamble.index("    Если УсловиеПримененияВыполняется")
    ]
    text = pkpd_body(preamble)
    assert unknown(read(text)) == ["unsupported_pkpd_statement"] * 4


# --- 3. ПКС табличной части под условием (Fable D7) ---


def test_table_property_under_version_guard():
    text = patched(
        V2,
        '    ДобавитьПКС(СвойстваТЧ, "Цена", "Price", 0);',
        '    Если ВерсияФорматаЧислом("1.8") >= 1 Тогда\n'
        '        ДобавитьПКС(СвойстваТЧ, "Цена", "Price", 0);\n    КонецЕсли;',
    )
    doc = read(text)
    assert doc.parse_status.value == "complete", unknown(doc)
    product = next(rule for rule in doc.pko if rule.name == "Товар")
    (prop,) = product.groups[0].properties
    assert prop.format_property == "Price" and len(prop.guards) == 1


def test_table_property_after_guarded_group_stays_ambiguous():
    text = patched(
        V2,
        '    СвойстваТЧ = ДобавитьПКТЧ(ПравилоКонвертации, "Цены", "Prices", "urn:example:extra");',
        "    Если Истина Тогда\n"
        '        СвойстваТЧ = ДобавитьПКТЧ(ПравилоКонвертации, "Цены", "Prices", '
        '"urn:example:extra");\n    КонецЕсли;',
    )
    assert unknown(read(text)) == ["ambiguous_property_parent"]


# --- 6. Составные условия направления (Fable D4) ---

ENTRY = (
    "    ДобавитьПКО_Товар(ПравилаКонвертации);\n"
    '    Если НаправлениеОбмена = "Отправка" Тогда\n'
    "        ДобавитьПКО_Заказ(ПравилаКонвертации);\n    КонецЕсли;\n"
)


def uses(text: str) -> dict[str, str | None]:
    return {use.target_name: use.direction for use in read(text).rule_uses}


@pytest.mark.parametrize(
    ("block", "expected"),
    [
        (
            '    Если НаправлениеОбмена = "Отправка" ИЛИ НаправлениеОбмена = "Получение" Тогда\n'
            "        ДобавитьПКО_Заказ(ПравилаКонвертации);\n    КонецЕсли;\n",
            "both",
        ),
        (
            '    Если НаправлениеОбмена <> "Отправка" Тогда\n'
            "        ДобавитьПКО_Заказ(ПравилаКонвертации);\n    КонецЕсли;\n",
            "receive",
        ),
        (
            '    Если НЕ (НаправлениеОбмена = "Получение") Тогда\n'
            "        ДобавитьПКО_Заказ(ПравилаКонвертации);\n    КонецЕсли;\n",
            "send",
        ),
        (
            '    Если НаправлениеОбмена = "Отправка" Тогда\n    Иначе\n'
            "        ДобавитьПКО_Заказ(ПравилаКонвертации);\n    КонецЕсли;\n",
            "receive",
        ),
        (
            '    Если НаправлениеОбмена = "Отправка" Тогда\n'
            '    ИначеЕсли НаправлениеОбмена = "Отправка"\n'
            '        ИЛИ НаправлениеОбмена = "Получение" Тогда\n'
            "        ДобавитьПКО_Заказ(ПравилаКонвертации);\n    КонецЕсли;\n",
            "receive",
        ),
        (
            '    Если НаправлениеОбмена = "Отправка" И Флаг Тогда\n'
            "        ДобавитьПКО_Заказ(ПравилаКонвертации);\n    КонецЕсли;\n",
            None,
        ),
        (
            "    Если Флаг Тогда\n    Иначе\n"
            "        ДобавитьПКО_Заказ(ПравилаКонвертации);\n    КонецЕсли;\n",
            None,
        ),
        (
            '    Если НаправлениеОбмена = "Отправка" Тогда\n'
            '        Если НаправлениеОбмена = "Получение" Тогда\n'
            "            ДобавитьПКО_Заказ(ПравилаКонвертации);\n        КонецЕсли;\n"
            "    КонецЕсли;\n",
            None,
        ),
        (
            '    Если НаправлениеОбмена = "отправка" Тогда\n'
            "        ДобавитьПКО_Заказ(ПравилаКонвертации);\n    КонецЕсли;\n",
            None,
        ),
    ],
    ids=["or", "not-equal", "not", "else", "elseif", "and-opaque", "else-opaque", "nested", "case"],
)
def test_direction_sets(block, expected):
    text = patched(V2, ENTRY, "    ДобавитьПКО_Товар(ПравилаКонвертации);\n" + block)
    assert uses(text)["ДобавитьПКО_Заказ"] == expected


def test_direction_or_includes_receive_for_uid_check():
    text = patched(
        V2,
        ENTRY,
        "    ДобавитьПКО_Товар(ПравилаКонвертации);\n"
        + (
            '    Если НаправлениеОбмена = "Отправка" ИЛИ НаправлениеОбмена = "Получение" Тогда\n'
            "        ДобавитьПКО_Заказ(ПравилаКонвертации);\n    КонецЕсли;\n"
        ),
    )
    text = patched(
        text,
        '    ПравилоКонвертации.ОбъектФормата = "Document.Order";\n'
        '    ПравилоКонвертации.ПриОтправкеДанных = "ПКО_Товар_ПриОтправкеДанных";',
        '    ПравилоКонвертации.ОбъектФормата = "Document.Invoice";\n'
        '    ПравилоКонвертации.ПриОтправкеДанных = "ПКО_Товар_ПриОтправкеДанных";\n'
        '    ПравилоКонвертации.ВариантИдентификации = "ПоУникальномуИдентификатору";',
    )
    assert "ed.identity.uid_without_pod" in checks(links(text))


def test_simple_atom_keeps_known_direction():
    doc = read(V2)
    simple = [g for g in doc.guards if g.expression_raw == 'НаправлениеОбмена = "Отправка"']
    assert simple and all(g.known_direction == "send" for g in simple)
    assert all(g.directions == ("send",) for g in simple)


def test_compound_guard_has_no_known_direction():
    text = patched(
        V2,
        ENTRY,
        "    ДобавитьПКО_Товар(ПравилаКонвертации);\n"
        '    Если НаправлениеОбмена = "Отправка" ИЛИ НаправлениеОбмена = "Получение" Тогда\n'
        "        ДобавитьПКО_Заказ(ПравилаКонвертации);\n    КонецЕсли;\n",
    )
    guard = next(g for g in read(text).guards if "ИЛИ" in g.expression_raw)
    assert guard.known_direction is None
    assert guard.directions == ("receive", "send")


# --- 6. Составные условия диспетчера (Fable D5) ---

BRANCH = (
    '    ИначеЕсли ИмяПроцедуры = "ПКО_Товар_ПриОтправкеДанных" Тогда\n'
    "        ПКО_Товар_ПриОтправкеДанных(Параметры.ДанныеИБ, Параметры.ДанныеXDTO, "
    "Параметры.КомпонентыОбмена, Параметры.СтекВыгрузки);\n"
)


def test_dispatcher_or_gives_case_per_literal():
    text = patched(
        V2,
        '    ИначеЕсли ИмяПроцедуры = "ПКО_Товар_ПриОтправкеДанных" Тогда\n',
        '    ИначеЕсли ИмяПроцедуры = "ПКО_Товар_ПриОтправкеДанных" '
        'ИЛИ ИмяПроцедуры = "ПКО_Товар_ПриКонвертацииДанныхXDTO" Тогда\n',
    )
    doc = read(text)
    literals = sorted(case.literal_name for case in doc.dispatcher_cases)
    assert "ПКО_Товар_ПриОтправкеДанных" in literals
    assert "ПКО_Товар_ПриКонвертацииДанныхXDTO" in literals
    assert len({case.entity_id for case in doc.dispatcher_cases}) == len(doc.dispatcher_cases)
    found = checks(links(text))
    assert "ed.handler.ambiguous" not in found
    # Обе ветки — одна процедура: вторая получает «лишние» аргументы, но это не отсутствие ветки.
    report = links(text)
    assert not any(
        "ПКО_Товар_ПриКонвертацииДанныхXDTO" in issue.message
        for issue in report.issues
        if issue.check == "ed.handler.missing"
    )


def test_dispatcher_other_compound_is_unknown():
    text = patched(
        V2,
        '    ИначеЕсли ИмяПроцедуры = "ПКО_Товар_ПриОтправкеДанных" Тогда\n',
        '    ИначеЕсли ИмяПроцедуры = "ПКО_Товар_ПриОтправкеДанных" И Параметры <> 0 Тогда\n',
    )
    doc = read(text)
    assert "unsupported_dispatcher_condition" in unknown(doc)
    report = links(text)
    assert "ed.handler.missing" not in checks(report)
    assert any(item.check == "ed.handler.missing" for item in report.skipped)


def test_dispatcher_platform_function_is_not_target():
    text = patched(V2, BRANCH, BRANCH + '        Сообщить("Отправка");\n')
    found = checks(links(text))
    assert "ed.dispatcher.target_missing" not in found
    assert "ed.handler.ambiguous" not in found
    assert read(text).parse_status == read(V2).parse_status


def test_dispatcher_unknown_local_call_still_missing():
    text = patched(V2, BRANCH, BRANCH + "        НетТакогоМетода(Параметры);\n")
    assert "ed.dispatcher.target_missing" in checks(links(text))


# --- 5. Точное сравнение имён (Fable D3, D6) ---


def test_handler_name_case_is_missing_with_hint():
    text = patched(
        V2,
        '    ПравилоКонвертации.ПослеЗагрузкиВсехДанных = "ЗавершитьТовар";',
        '    ПравилоКонвертации.ПослеЗагрузкиВсехДанных = "завершитьТовар";',
    )
    report = links(text)
    missing = [
        i
        for i in report.issues
        if i.check == "ed.handler.missing" and "завершитьТовар" in i.message
    ]
    assert len(missing) == 1
    assert "«ЗавершитьТовар» — отличается регистром или пробелами" in missing[0].message
    assert not any("ЗавершитьТовар" in i.message for i in links(V2).issues)


def test_handler_name_trailing_space_is_missing():
    text = patched(
        V2,
        '    Если ИмяПроцедуры = "ЗавершитьТовар" Тогда',
        '    Если ИмяПроцедуры = "ЗавершитьТовар " Тогда',
    )
    missing = [
        i
        for i in links(text).issues
        if i.check == "ed.handler.missing" and "называет «ЗавершитьТовар»" in i.message
    ]
    assert len(missing) == 1 and "«ЗавершитьТовар »" in missing[0].message


def test_property_rule_trailing_space_is_missing_with_hint():
    text = patched(
        V2,
        '    ДобавитьПКС(СвойстваШапки, "Товар", "Product", , "Товар");',
        '    ДобавитьПКС(СвойстваШапки, "Товар", "Product", , "Товар ");',
    )
    issues = [i for i in links(text).issues if i.check == "ed.reference.property_rule_missing"]
    assert len(issues) == 1 and "Есть «Товар»" in issues[0].message


def test_pod_used_pko_case_is_missing_with_hint():
    text = patched(
        V2,
        '    ПравилоОбработки.ИспользуемыеПКО.Добавить("Товар");',
        '    ПравилоОбработки.ИспользуемыеПКО.Добавить("товар");',
    )
    doc = read(text)
    pod = next(rule for rule in doc.pod if rule.name == "Товары")
    assert [ref.resolution for ref in pod.used_pko] == ["missing"]
    issues = [i for i in links(text).issues if i.check == "ed.reference.pod_pko_missing"]
    assert len(issues) == 1 and "Есть «Товар»" in issues[0].message


def test_pod_usage_insert_other_case_renames_key():
    # Живая проба: Вставить("товар") поверх «Товар» оставляет ключ «товар»; ПКО по ключу
    # исполнитель ищет точно (XDTO:795, 7457) — не найдёт.
    text = patched(
        V2,
        '    ИспользованиеПКО.Вставить("Товар", Истина);',
        '    ИспользованиеПКО.Вставить("товар", Истина);',
    )
    report = links(text)
    assert "ed.reference.pod_usage_key_missing" not in checks(report)
    found = [i for i in report.issues if i.check == "ed.reference.code_rule_missing"]
    assert len(found) == 1 and "Есть «Товар»" in found[0].message


@pytest.mark.parametrize(
    "statement",
    ["    ИспользованиеПКО.товар = Истина;", '    ИспользованиеПКО["товар"] = Истина;'],
    ids=["member", "index"],
)
def test_pod_usage_member_other_case_is_same_key(statement):
    # Через точку и индекс ключ структуры находится без учёта регистра и не меняется.
    text = patched(V2, '    ИспользованиеПКО.Вставить("Товар", Истина);', statement)
    found = checks(links(text))
    assert "ed.reference.pod_usage_key_missing" not in found
    assert "ed.reference.code_rule_missing" not in found


# --- Находки ревью Opus ---


def test_version_with_leading_zero_is_not_three():
    # Исполнитель сравнивает строку с "3" (XDTO:4718): "03" — прежний интерфейс.
    doc = read(patched(V3, 'Возврат "3";', 'Возврат "03";'))
    assert doc.manager_version is None
    assert "unsupported_manager_version" in [d.code for d in doc.diagnostics]


def test_preprocessor_interleaved_with_if_chain_is_unknown():
    block = (
        '    Если НаправлениеОбмена = "Отправка" Тогда\n'
        "    #Если Сервер Тогда\n"
        "    ИначеЕсли Флаг Тогда\n"
        "    #КонецЕсли\n"
        "    Иначе\n"
        "        ДобавитьПКО_Заказ(ПравилаКонвертации);\n"
        "    КонецЕсли;\n"
    )
    text = patched(V2, ENTRY, "    ДобавитьПКО_Товар(ПравилаКонвертации);\n" + block)
    doc = read(text)
    assert uses(text)["ДобавитьПКО_Заказ"] is None
    assert doc.parse_status.value == "partial"
    assert "interleaved_preprocessor" in [d.code for d in doc.diagnostics]


def test_function_dispatcher_returning_global_function_is_not_target():
    text = patched(
        V2,
        "        Возврат ПОД_Товары_ВыборкаДанных(Параметры.КомпонентыОбмена);",
        "        Возврат ЗначениеЗаполнено(Параметры);",
    )
    found = checks(links(text))
    assert "ed.dispatcher.target_missing" not in found
    # Ветки на обработчик больше нет — это честная находка «нет ветки».
    assert "ed.handler.missing" in found


@pytest.mark.parametrize(
    ("old", "new"),
    [
        (
            "    Если ТолькоЗаголовки Тогда\n        Возврат;\n    КонецЕсли;\n"
            "    СвойстваШапки = ПравилоКонвертации.Свойства;\n"
            '    ДобавитьПКС(СвойстваШапки, "Номер", "Number", 0, , , "Версия120");\n',
            "    СвойстваШапки = ПравилоКонвертации.Свойства;\n"
            '    ДобавитьПКС(СвойстваШапки, "Номер", "Number", 0, , , "Версия120");\n'
            "    Если ТолькоЗаголовки Тогда\n        Возврат;\n    КонецЕсли;\n",
        ),
    ],
    ids=["after-properties"],
)
def test_headers_only_return_out_of_place_is_unknown(old, new):
    assert "early_return" in unknown(read(patched(V3, old, new)))


def test_duplicate_differs_only_by_case_is_not_duplicate():
    text = patched(
        V2,
        '    ПравилоКонвертации.ИмяПКО = "Заказ";',
        '    ПравилоКонвертации.ИмяПКО = "товар";',
    )
    assert "ed.rule.duplicate" not in checks(links(text))


def test_send_event_of_receive_only_rule_needs_no_branch():
    # ПКО Заказ вызывается только при получении: его ПриОтправкеДанных исполнитель не вызывает
    # (event_direction, как в слоях), ветка диспетчера не нужна.
    receive = patched(
        V2,
        ENTRY,
        "    ДобавитьПКО_Товар(ПравилаКонвертации);\n"
        '    Если НаправлениеОбмена = "Получение" Тогда\n'
        "        ДобавитьПКО_Заказ(ПравилаКонвертации);\n    КонецЕсли;\n",
    )
    tail = "\n    СвойстваШапки"
    renamed = '    ПравилоКонвертации.ПриОтправкеДанных = "НетТакогоОбработчика";' + tail
    old = '    ПравилоКонвертации.ПриОтправкеДанных = "ПКО_Товар_ПриОтправкеДанных";' + tail
    missing = [
        i
        for i in links(patched(receive, old, renamed)).issues
        if i.check == "ed.handler.missing" and "НетТакогоОбработчика" in i.message
    ]
    assert missing == []
    sent = patched(V2, old, renamed)
    assert any(
        i.check == "ed.handler.missing" and "НетТакогоОбработчика" in i.message
        for i in links(sent).issues
    )


def test_optional_delete_handler_signature_is_warning():
    # ПередОбработкойУдаляемогоОбъекта исполнитель зовёт в Попытка с пустым Исключение
    # (XDTO:8744–8748): лишний обязательный параметр — обработчик молча не выполняется.
    text = patched(
        V2,
        "Процедура ПередОбработкойУдаляемогоОбъекта(КомпонентыОбмена, УдаляемыйОбъект) Экспорт",
        "Процедура ПередОбработкойУдаляемогоОбъекта(КомпонентыОбмена, УдаляемыйОбъект, Отказ) "
        "Экспорт",
    )
    report = interface(text)
    assert [(i.check, i.level.value) for i in report.issues] == [
        ("ed.conversion.signature", "предупреждение")
    ]
    assert "молча не выполняется" in report.issues[0].message
