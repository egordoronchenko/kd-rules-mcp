"""Границы пятнадцати проверок связности: нарушение, чистый пример, пропуски."""

import re
from pathlib import Path

import pytest

from kd2_rules_mcp.ed import forms, read_manager_text
from kd2_rules_mcp.ed.address import build_addresses
from kd2_rules_mcp.ed.refs import build_references
from kd2_rules_mcp.validation.ed_links import validate_links

DATA = Path(__file__).parent / "data" / "ed"
BASE = (DATA / "checks_base.bsl").read_text(encoding="utf-8")
SCHEMA = "Схема формата и структура конфигурации не переданы: проверки по схеме не выполнялись"
_ROUTE = (
    'Если ИмяПроцедуры = "Обработать" Тогда\n'
    "        Обработать(Параметры.ДанныеИБ, Параметры.ДанныеXDTO, "
    "Параметры.КомпонентыОбмена, Параметры.СтекВыгрузки);\n"
    "    КонецЕсли;"
)
_INIT = (
    "    ПравилоКонвертации = ОбменДаннымиXDTOСервер."
    "ИнициализироватьПравилоКонвертацииОбъекта(ПравилаКонвертации);\n"
)
_PKPD = (
    "Процедура ЗаполнитьПравилаКонвертацииПредопределенныхДанных("
    "НаправлениеОбмена, ПравилаКонвертации) Экспорт\n"
    "    ПравилоКонвертации = ПравилаКонвертации.Добавить();\n"
    '    ПравилоКонвертации.ИмяПКПД = "НетПравила";\n'
    '    ПравилоКонвертации.ТипДанных = "СправочникСсылка.Виды";\n'
    '    ПравилоКонвертации.ТипXDTO = "Вид";\n'
    "    ЗначенияДляОтправки = Новый Соответствие;\n"
    "    ЗначенияДляПолучения = Новый Соответствие;\n"
    "КонецПроцедуры\n"
)


def _pko_procedure(name: str, procedure: str | None = None) -> str:
    title = procedure or f"ДобавитьПКО_{name}"
    metadata = "Товары" if name == "Товар" else "Виды"
    format_name = "Товар" if name == "Товар" else "Вид"
    return (
        f"Процедура {title}(ПравилаКонвертации)\n"
        + _INIT
        + f'    ПравилоКонвертации.ИмяПКО = "{name}";\n'
        + f"    ПравилоКонвертации.ОбъектДанных = Метаданные.Справочники.{metadata};\n"
        + f'    ПравилоКонвертации.ОбъектФормата = "Справочник.{format_name}";\n'
        + "КонецПроцедуры\n"
    )


HANDLER_MISSING = (
    "Для события «{event}» правило называет «{name}», "
    "но ветка диспетчера не найдена: обработчик не будет вызван."
)


def module_text(version: int = 2) -> str:
    text = BASE.replace('Возврат "2";', f'Возврат "{version}";', 1)
    if version == 3:
        text = text.replace(
            "Процедура ЗаполнитьПравилаКонвертацииОбъектов("
            "НаправлениеОбмена, ПравилаКонвертации) Экспорт\n"
            "    // <fill-pko>\n"
            "    ДобавитьПКО_Товар(ПравилаКонвертации);",
            "Процедура ЗаполнитьПравилаКонвертацииОбъектов("
            "КомпонентыОбмена, ПравилаКонвертации, ТолькоЗаголовки = Ложь) Экспорт\n"
            "    НаправлениеОбмена = КомпонентыОбмена.НаправлениеОбмена;\n"
            "    ВерсияФорматаОбмена = КомпонентыОбмена.ВерсияФорматаОбмена;\n"
            "    // <fill-pko>\n"
            "    ДобавитьПКО_Товар(ПравилаКонвертации, ВерсияФорматаОбмена, ТолькоЗаголовки);",
            1,
        )
        text = text.replace(
            "Процедура ДобавитьПКО_Товар(ПравилаКонвертации)",
            'Процедура ДобавитьПКО_Товар(ПравилаКонвертации, ВерсияФорматаОбмена = "", '
            "ТолькоЗаголовки = Ложь)",
            1,
        )
    helpers = "\n".join(
        forms.helper_forms(name, version)[0] for name in ("ДобавитьПКС", "ДобавитьПКТЧ")
    )
    return text + "\n" + helpers + "\n"


def put(text: str, marker: str, snippet: str) -> str:
    return text.replace(f"// <{marker}>", snippet, 1)


def drop_routine(text: str, name: str) -> str:
    pattern = rf"(?:Процедура|Функция) {re.escape(name)}\(.*?Конец(?:Процедуры|Функции)\n"
    updated, count = re.subn(pattern, "", text, count=1, flags=re.S)
    assert count == 1
    return updated


def analyze(text: str):
    document = read_manager_text(text)
    references = build_references(document)
    report = validate_links(document, build_addresses(document), references)
    return document, references, report


def checks(report) -> list[str]:
    return [issue.check for issue in report.issues]


def only(report, check: str):
    found = [issue for issue in report.issues if issue.check == check]
    assert checks(report) == [check] * len(found), [
        (issue.check, issue.address, issue.message) for issue in report.issues
    ]
    assert len(found) == 1
    return found[0]


def test_clean_base_has_only_schema_skip():
    _document, _references, report = analyze(module_text())
    assert report.issues == []
    assert [(item.check, item.reason) for item in report.skipped] == [("ed.schema", SCHEMA)]


@pytest.mark.parametrize("version", [1, 2, 3])
def test_conversion_required_across_manager_interfaces(version):
    clean, _references, report = analyze(module_text(version))
    assert "ed.conversion.required" not in checks(report)
    assert clean.parse_status == "complete"
    text = drop_routine(module_text(version), "ПередКонвертацией")
    _document, _references, report = analyze(text)
    issue = only(report, "ed.conversion.required")
    assert issue.level.value == "ошибка"
    assert issue.address == "Конвертация"
    assert issue.message == "Обязательный метод конвертации «ПередКонвертацией» не определён."


def test_empty_conversion_method_is_enough_and_delete_handler_is_optional():
    document, _references, report = analyze(module_text())
    assert "ed.conversion.required" not in checks(report)
    assert not any(
        routine.name == "ПередОбработкойУдаляемогоОбъекта" for routine in document.routines
    )
    text = module_text()
    for name in ("ПередКонвертацией", "ПослеКонвертации", "ПередОтложеннымЗаполнением"):
        text = drop_routine(text, name)
    _document, _references, report = analyze(text)
    assert [issue.message for issue in report.issues] == [
        f"Обязательный метод конвертации «{name}» не определён."
        for name in ("ПередКонвертацией", "ПередОтложеннымЗаполнением", "ПослеКонвертации")
    ]
    assert {issue.level.value for issue in report.issues} == {"ошибка"}


@pytest.mark.parametrize("version", [1, 2, 3])
def test_handler_missing_is_error_until_branch_exists(version):
    text = put(module_text(version), "pko", 'ПравилоКонвертации.ПриОтправкеДанных = "Обработать";')
    text += (
        "\nПроцедура Обработать(ДанныеИБ, ДанныеXDTO, "
        "КомпонентыОбмена, СтекВыгрузки)\nКонецПроцедуры\n"
    )
    _document, _references, report = analyze(text)
    issue = only(report, "ed.handler.missing")
    assert issue.level.value == "ошибка"
    assert issue.address == "ПКО/Товар"
    assert issue.message == HANDLER_MISSING.format(event="ПриОтправкеДанных", name="Обработать")
    linked = put(
        text,
        "proc",
        _ROUTE,
    )
    _document, _references, report = analyze(linked)
    assert "ed.handler.missing" not in checks(report)
    assert report.issues == []


def test_missing_branch_is_not_also_a_missing_target():
    text = put(module_text(), "pko", 'ПравилоКонвертации.ПриОтправкеДанных = "Обработать";')
    _document, _references, report = analyze(text)
    assert checks(report) == ["ed.handler.missing"]


def test_handler_ambiguous_counts_branches_inside_one_dispatcher():
    text = put(module_text(), "pko", 'ПравилоКонвертации.ПриОтправкеДанных = "Обработать";')
    text += (
        "\nПроцедура Обработать(ДанныеИБ, ДанныеXDTO, "
        "КомпонентыОбмена, СтекВыгрузки)\nКонецПроцедуры\n"
    )
    text = put(
        text,
        "proc",
        """Если ИмяПроцедуры = "Обработать" Тогда
        Обработать(Параметры.ДанныеИБ);
    КонецЕсли;
    Если ИмяПроцедуры = "Обработать" Тогда
        Обработать(Параметры.ДанныеИБ);
    КонецЕсли;""",
    )
    _document, _references, report = analyze(text)
    issue = only(report, "ed.handler.ambiguous")
    assert issue.level.value == "предупреждение"
    assert issue.address == "ПКО/Товар"
    assert issue.message == (
        "Связь события «ПриОтправкеДанных» с «Обработать» неоднозначна: 2 кандидатов."
    )
    separated = put(
        module_text(),
        "pko",
        'ПравилоКонвертации.ПриОтправкеДанных = "Обработать";',
    )
    separated += (
        "\nПроцедура Обработать(ДанныеИБ, ДанныеXDTO, КомпонентыОбмена, СтекВыгрузки)\n"
        "КонецПроцедуры\n"
    )
    separated = put(
        separated,
        "proc",
        """Если ИмяПроцедуры = "Обработать" Тогда
        Обработать(Параметры.ДанныеИБ);
    КонецЕсли;""",
    )
    separated = put(
        separated,
        "func",
        """Если ИмяФункции = "Обработать" Тогда
        Возврат Обработать(Параметры.КомпонентыОбмена);
    КонецЕсли;""",
    )
    _document, _references, report = analyze(separated)
    assert "ed.handler.ambiguous" not in checks(report)
    assert report.issues == []


def test_function_dispatcher_is_required_only_for_data_selection():
    text = put(module_text(), "pod", 'ПравилоОбработки.ВыборкаДанных = "Выбрать";')
    text = put(
        text,
        "proc",
        """Если ИмяПроцедуры = "Выбрать" Тогда
        Выбрать(Параметры.КомпонентыОбмена);
    КонецЕсли;""",
    )
    text += "\nФункция Выбрать(КомпонентыОбмена)\n    Возврат Неопределено;\nКонецФункции\n"
    _document, _references, report = analyze(text)
    issue = only(report, "ed.handler.missing")
    assert "ВыборкаДанных" in issue.message
    linked = put(
        module_text(),
        "pod",
        'ПравилоОбработки.ВыборкаДанных = "Выбрать";',
    )
    linked = put(
        linked,
        "func",
        """Если ИмяФункции = "Выбрать" Тогда
        Возврат Выбрать(Параметры.КомпонентыОбмена);
    КонецЕсли;""",
    )
    linked += "\nФункция Выбрать(КомпонентыОбмена)\n    Возврат Неопределено;\nКонецФункции\n"
    _document, _references, report = analyze(linked)
    assert report.issues == []


def test_dispatcher_target_missing_and_qualified_call():
    text = put(module_text(), "pko", 'ПравилоКонвертации.ПриОтправкеДанных = "Обработать";')
    text = put(
        text,
        "proc",
        """Если ИмяПроцедуры = "Обработать" Тогда
        НетМетода(Параметры.ДанныеИБ);
    КонецЕсли;""",
    )
    document, _references, report = analyze(text)
    issue = only(report, "ed.dispatcher.target_missing")
    case = next(item for item in document.dispatcher_cases if item.literal_name == "Обработать")
    assert issue.level.value == "ошибка"
    assert issue.address == "Диспетчер/ВыполнитьПроцедуруМодуляМенеджера"
    assert issue.message == (
        f"Строка {case.span.line_start}: ветка «Обработать» вызывает "
        "отсутствующий локальный метод «НетМетода»."
    )
    present = text + "\nПроцедура НетМетода(ДанныеИБ)\nКонецПроцедуры\n"
    _document, _references, report = analyze(present)
    assert "ed.dispatcher.target_missing" not in checks(report)
    qualified = put(
        module_text(),
        "pko",
        'ПравилоКонвертации.ПриОтправкеДанных = "Обработать";',
    )
    qualified = put(
        qualified,
        "proc",
        """Если ИмяПроцедуры = "Обработать" Тогда
        ДругойМодуль.НетМетода(Параметры.ДанныеИБ);
    КонецЕсли;""",
    )
    _document, _references, report = analyze(qualified)
    assert "ed.dispatcher.target_missing" not in checks(report)
    assert report.issues == []
    external = next(
        item for item in report.skipped if item.check == "ed.dispatcher.external_target"
    )
    assert "ДругойМодуль.НетМетода" in external.reason
    assert "Диспетчер/ВыполнитьПроцедуруМодуляМенеджера" in external.reason


def test_alias_branch_resolves_without_missing_target():
    text = put(module_text(), "pko", 'ПравилоКонвертации.ПослеЗагрузкиВсехДанных = "Псевдоним";')
    text = put(
        text,
        "proc",
        """Если ИмяПроцедуры = "Псевдоним" Тогда
        РеальноеИмя(Параметры.Объект);
    КонецЕсли;""",
    )
    text += "\nПроцедура РеальноеИмя(Объект)\nКонецПроцедуры\n"
    document, _references, report = analyze(text)
    binding = next(item for item in document.pko[0].events if item.target_name == "Псевдоним")
    assert binding.resolution == "resolved"
    assert binding.target_id
    assert report.issues == []


def test_extended_request_event_is_skipped_and_object_events_are_checked():
    text = put(
        module_text(),
        "pko",
        """ПравилоКонвертации.ПриПолученииЗапросаВыгрузкиОбъекта = "Подобрать";
    ПравилоКонвертации.ПослеКонвертацииОбъекта = "ПослеОбъекта";
    ПравилоКонвертации.ПриУдаленииОбъектаИБ = "УдалитьОбъект";""",
    )
    _document, _references, report = analyze(text)
    assert checks(report) == ["ed.handler.missing", "ed.handler.missing"]
    assert [issue.message for issue in report.issues] == [
        HANDLER_MISSING.format(event="ПослеКонвертацииОбъекта", name="ПослеОбъекта"),
        HANDLER_MISSING.format(event="ПриУдаленииОбъектаИБ", name="УдалитьОбъект"),
    ]
    assert "Подобрать" not in " ".join(issue.message for issue in report.issues)
    assert (
        "ed.handler.extended_events",
        "Событие «ПриПолученииЗапросаВыгрузкиОбъекта»: исполнитель читает другое поле правила, "
        "связь с обработчиком не проверялась",
    ) in {(item.check, item.reason) for item in report.skipped}


def test_property_rule_missing_accepts_predefined_rule():
    text = put(
        module_text(),
        "pko",
        'ДобавитьПКС(СвойстваШапки, "Вид", "Вид", 0, "НетПравила");',
    )
    _document, _references, report = analyze(text)
    issue = only(report, "ed.reference.property_rule_missing")
    assert issue.level.value == "предупреждение"
    assert issue.address == "ПКО/Товар/ПКС/Вид"
    assert issue.message == (
        "ПКС ссылается на отсутствующее правило «НетПравила»; проверены ПКО и ПКПД."
    )
    predefined = text + "\n" + _PKPD
    _document, _references, report = analyze(predefined)
    assert "ed.reference.property_rule_missing" not in checks(report)


def test_pod_pko_missing_and_guarded_definition_still_exists():
    text = module_text().replace(
        'ПравилоОбработки.ИспользуемыеПКО.Добавить("Товар");',
        'ПравилоОбработки.ИспользуемыеПКО.Добавить("НетПравила");',
        1,
    )
    _document, _references, report = analyze(text)
    issue = only(report, "ed.reference.pod_pko_missing")
    assert issue.level.value == "предупреждение"
    assert issue.address == "ПОД/Товары"
    assert issue.message == (
        "ПОД использует отсутствующее ПКО «НетПравила»: при отправке оно пропускается, "
        "при получении — ошибка обмена; проверьте условия версии."
    )
    defined = text + "\n" + _pko_procedure("НетПравила")
    defined = defined.replace(
        "// <fill-pko>",
        'Если НаправлениеОбмена = "Отправка" Тогда\n'
        "        ДобавитьПКО_НетПравила(ПравилаКонвертации);\n"
        "    КонецЕсли;",
        1,
    )
    _document, _references, report = analyze(defined)
    assert "ed.reference.pod_pko_missing" not in checks(report)


def test_rule_use_missing_is_compile_error():
    text = module_text().replace(
        "ДобавитьПКО_Товар(ПравилаКонвертации);",
        "ДобавитьПКО_НетПравила(ПравилаКонвертации);",
        1,
    )
    document, _references, report = analyze(text)
    issue = only(report, "ed.reference.rule_use_missing")
    use = next(item for item in document.rule_uses if item.target_name == "ДобавитьПКО_НетПравила")
    assert use.rule_id is None
    assert issue.level.value == "ошибка"
    assert issue.address == "Конвертация"
    assert issue.message == (
        f"Строка {use.span.line_start}: в заполнении правил используется "
        "«ДобавитьПКО_НетПравила», определение не найдено."
    )
    defined = text + "\n" + _pko_procedure("НетПравила")
    _document, _references, report = analyze(defined)
    assert "ed.reference.rule_use_missing" not in checks(report)
    assert report.issues == []


@pytest.mark.parametrize(
    ("snippet", "kind", "label"),
    [
        (
            'ОбменДаннымиXDTOСервер.ПКОПоИмени(КомпонентыОбмена, "НетПравила");',
            "pko_lookup",
            "поиск ПКО",
        ),
        ('Инструкция = Новый Структура("ИмяПКО", "НетПравила");', "instruction_rule", "инструкция"),
        ('Инструкция.Вставить("ИмяПКО", "НетПравила");', "instruction_rule", "инструкция"),
        ('Инструкция.ИмяПКО = "НетПравила";', "instruction_rule", "инструкция"),
        ("ИспользованиеПКО.НетПравила = Ложь;", "pod_use", "использование ПКО"),
    ],
)
def test_code_rule_missing_forms(snippet, kind, label):
    _document, references, report = analyze(put(module_text(), "event", snippet))
    issue = only(report, "ed.reference.code_rule_missing")
    ref = next(item for item in references.entries if item.name == "НетПравила")
    assert ref.kind == kind
    assert issue.level.value == "предупреждение"
    assert issue.address == "Обработчик/ПередКонвертацией"
    assert issue.message == (
        f"Строка {ref.span.line_start}: {label} ссылается на отсутствующее правило «НетПравила»."
    )


def test_known_literal_and_computed_name_are_not_missing_rules():
    text = put(
        module_text(),
        "event",
        """ОбменДаннымиXDTOСервер.ПКОПоИмени(КомпонентыОбмена, "Товар");
    ОбменДаннымиXDTOСервер.ПКОПоИмени(КомпонентыОбмена, Имя);""",
    )
    _document, references, report = analyze(text)
    assert "ed.reference.code_rule_missing" not in checks(report)
    assert report.issues == []
    assert references.unparsed_by_kind["pko_lookup"] == 1
    assert references.unparsed == 1


def test_predefined_rule_satisfies_only_instruction():
    body = "\n" + _PKPD
    lookup = analyze(
        put(
            module_text() + body,
            "event",
            'ОбменДаннымиXDTOСервер.ПКОПоИмени(КомпонентыОбмена, "НетПравила");',
        )
    )[2]
    instruction = analyze(
        put(module_text() + body, "event", 'Инструкция = Новый Структура("ИмяПКО", "НетПравила");')
    )[2]
    assert checks(lookup) == ["ed.reference.code_rule_missing"]
    assert instruction.issues == []


def test_algorithm_handler_missing_boundaries():
    flagged = module_text().replace(
        'ДобавитьПКС(СвойстваШапки, "Код", "Код", 0);',
        'ДобавитьПКС(СвойстваШапки, "Код", "Код", 1);',
        1,
    )
    _document, _references, report = analyze(flagged)
    issue = only(report, "ed.algorithm.handler_missing")
    assert issue.level.value == "предупреждение"
    assert issue.address == "ПКО/Товар"
    assert issue.message == (
        "У ПКО алгоритмические ПКС, но нет привязок обработчиков; заполнение требует проверки."
    )
    handled = put(flagged, "pko", 'ПравилоКонвертации.ПриОтправкеДанных = "Обработать";')
    handled = put(
        handled,
        "proc",
        """Если ИмяПроцедуры = "Обработать" Тогда
        Обработать(Параметры.ДанныеИБ);
    КонецЕсли;""",
    )
    handled += (
        "\nПроцедура Обработать(ДанныеИБ, ДанныеXDTO, КомпонентыОбмена, СтекВыгрузки)\n"
        "КонецПроцедуры\n"
    )
    _document, _references, report = analyze(handled)
    assert "ed.algorithm.handler_missing" not in checks(report)
    undefined = flagged.replace(
        "ПравилоКонвертации.ОбъектДанных = Метаданные.Справочники.Товары;",
        "ПравилоКонвертации.ОбъектДанных = Неопределено;",
        1,
    )
    empty_format = flagged.replace(
        'ПравилоКонвертации.ОбъектФормата = "Справочник.Товар";',
        'ПравилоКонвертации.ОбъектФормата = "";',
        1,
    )
    assert "ed.algorithm.handler_missing" not in checks(analyze(undefined)[2])
    assert "ed.algorithm.handler_missing" not in checks(analyze(empty_format)[2])


def test_deferred_arguments_allow_only_the_executor_set():
    def deferred(call: str) -> str:
        text = put(module_text(), "pko", 'ПравилоКонвертации.ПослеЗагрузкиВсехДанных = "Позже";')
        text = put(
            text,
            "proc",
            f"""Если ИмяПроцедуры = "Позже" Тогда
        {call}
    КонецЕсли;""",
        )
        return text + "\nПроцедура Позже(Объект)\nКонецПроцедуры\n"

    allowed = deferred(
        "Позже(Параметры.Объект, Параметры.КомпонентыОбмена, "
        "Параметры.ОбъектМодифицирован, Параметры.КомпонентыОбмена.ПараметрыКонвертации);"
    )
    _document, references, report = analyze(allowed)
    assert "ed.deferred.argument" not in checks(report)
    assert references.deferred_argument_unparsed == 0
    assert report.issues == []
    invalid = deferred("Позже(Параметры.ДанныеXDTO);")
    document, _references, report = analyze(invalid)
    issue = only(report, "ed.deferred.argument")
    argument = document.dispatcher_cases[0].arguments[0]
    assert issue.level.value == "предупреждение"
    assert issue.address == "Диспетчер/ВыполнитьПроцедуруМодуляМенеджера"
    assert issue.message == (
        f"Строка {argument.span.line_start}: аргумент «{argument.raw}» "
        "отложенной ветки не входит в набор параметров исполнителя."
    )
    computed = deferred("Позже(ПолучитьПараметр());")
    _document, references, report = analyze(computed)
    assert "ed.deferred.argument" not in checks(report)
    assert references.deferred_argument_unparsed == 1
    other = put(module_text(), "pko", 'ПравилоКонвертации.ПриОтправкеДанных = "Обработать";')
    other = put(
        other,
        "proc",
        """Если ИмяПроцедуры = "Обработать" Тогда
        Обработать(Параметры.ЧужоеПоле);
    КонецЕсли;""",
    )
    other += (
        "\nПроцедура Обработать(ДанныеИБ, ДанныеXDTO, КомпонентыОбмена, СтекВыгрузки)\n"
        "КонецПроцедуры\n"
    )
    _document, references, report = analyze(other)
    assert "ed.deferred.argument" not in checks(report)
    assert references.deferred_argument_unparsed == 0


def test_identity_uid_without_pod_respects_direction():
    uid = module_text().replace(
        'ПравилоКонвертации.ОбъектФормата = "Справочник.Товар";',
        'ПравилоКонвертации.ОбъектФормата = "Справочник.Товар";\n'
        '    ПравилоКонвертации.ВариантИдентификации = "ПоУникальномуИдентификатору";',
        1,
    )
    _document, _references, report = analyze(uid)
    assert "ed.identity.uid_without_pod" not in checks(report)
    changed = uid.replace(
        'ПравилоОбработки.ОбъектВыборкиФормат = "Справочник.Товар";',
        'ПравилоОбработки.ОбъектВыборкиФормат = "Справочник.Другой";',
        1,
    )
    _document, _references, report = analyze(changed)
    issue = only(report, "ed.identity.uid_without_pod")
    assert issue.level.value == "предупреждение"
    assert issue.address == "ПКО/Товар"
    assert issue.message == (
        "Получение ПКО «Товар» использует только уникальный идентификатор "
        "без ПОД формата «Справочник.Товар»; новый объект может не создаваться."
    )
    combined = changed.replace(
        '"ПоУникальномуИдентификатору"',
        '"СначалаПоУникальномуИдентификаторуПотомПоПолямПоиска"',
        1,
    )
    assert "ed.identity.uid_without_pod" not in checks(analyze(combined)[2])
    send_only = changed.replace(
        "// <fill-pko>\n    ДобавитьПКО_Товар(ПравилаКонвертации);",
        """Если НаправлениеОбмена = "Отправка" Тогда
        ДобавитьПКО_Товар(ПравилаКонвертации);
    КонецЕсли;""",
        1,
    )
    assert "ed.identity.uid_without_pod" not in checks(analyze(send_only)[2])
    unknown = changed.replace(
        "// <fill-pko>\n    ДобавитьПКО_Товар(ПравилаКонвертации);",
        """Если НепонятныйФлаг Тогда
        ДобавитьПКО_Товар(ПравилаКонвертации);
    КонецЕсли;""",
        1,
    )
    _document, _references, report = analyze(unknown)
    assert "ed.identity.uid_without_pod" not in checks(report)
    assert any(
        item.check == "ed.identity.uid_without_pod" and "Направление не определено" in item.reason
        for item in report.skipped
    )


def test_extension_initialization_pairs():
    pair = put(
        module_text(),
        "pko",
        """ОбменДаннымиXDTOСервер.ИнициализироватьРасширениеПравилаКонвертацииОбъекта(
        ПравилоКонвертации, "urn:example:extra");
    ДобавитьПКС(СвойстваШапки, "ВнешнийКод", "ВнешнийКод", 0, "", "urn:example:extra");
    СвойстваТЧ = ДобавитьПКТЧ(ПравилоКонвертации, "Строки", "Строки", "urn:example:extra");
    ДобавитьПКС(СвойстваТЧ, "Код", "Код", 0, "", "urn:example:extra");""",
    )
    assert analyze(pair)[2].issues == []
    uninitialized = pair.replace(
        """ОбменДаннымиXDTOСервер.ИнициализироватьРасширениеПравилаКонвертацииОбъекта(
        ПравилоКонвертации, "urn:example:extra");
    """,
        "",
        1,
    )
    _document, _references, report = analyze(uninitialized)
    assert checks(report) == ["ed.extension.uninitialized"] * 3
    assert {issue.address for issue in report.issues} == {
        "ПКО/Товар/ПКС/ВнешнийКод",
        "ПКО/Товар/ПКТЧ/Строки",
        "ПКО/Товар/ПКТЧ/Строки/ПКС/Код",
    }
    assert all("urn:example:extra" in issue.message for issue in report.issues)
    only_init = put(
        module_text(),
        "pko",
        """ОбменДаннымиXDTOСервер.ИнициализироватьРасширениеПравилаКонвертацииОбъекта(
        ПравилоКонвертации, "urn:example:extra");""",
    )
    _document, _references, report = analyze(only_init)
    issue = only(report, "ed.extension.unused")
    assert issue.address == "ПКО/Товар"
    assert issue.message == (
        "Расширение «urn:example:extra» инициализировано, но не используется "
        "декларативными свойствами ПКО."
    )
    group_only = put(
        module_text(),
        "pko",
        """ОбменДаннымиXDTOСервер.ИнициализироватьРасширениеПравилаКонвертацииОбъекта(
        ПравилоКонвертации, "urn:example:extra");
    СвойстваТЧ = ДобавитьПКТЧ(ПравилоКонвертации, "Строки", "Строки", "urn:example:extra");""",
    )
    assert "ed.extension.unused" not in checks(analyze(group_only)[2])
    split = put(
        module_text(),
        "pko",
        """ОбменДаннымиXDTOСервер.ИнициализироватьРасширениеПравилаКонвертацииОбъекта(
        ПравилоКонвертации, "urn:example:a");
    ДобавитьПКС(СвойстваШапки, "КодВнешний", "КодВнешний", 0, "", "urn:example:b");""",
    )
    _document, _references, report = analyze(split)
    assert {issue.check for issue in report.issues} == {
        "ed.extension.unused",
        "ed.extension.uninitialized",
    }
    assert any("urn:example:a" in issue.message for issue in report.issues)
    assert any("urn:example:b" in issue.message for issue in report.issues)
    blank = put(
        module_text(),
        "pko",
        'ДобавитьПКС(СвойстваШапки, "КодВнешний", "КодВнешний", 0, "", "");',
    )
    assert analyze(blank)[2].issues == []


def test_group_empty_property_depends_on_direction():
    empty = put(
        module_text(),
        "pko",
        """СвойстваТЧ = ДобавитьПКТЧ(ПравилоКонвертации, "Строки", "Строки");
    ДобавитьПКС(СвойстваТЧ, "", "", 0);""",
    )
    _document, _references, report = analyze(empty)
    issue = only(report, "ed.group.empty_property")
    assert issue.level.value == "предупреждение"
    assert issue.address == "ПКО/Товар/ПКТЧ/Строки/ПКС/~empty"
    assert issue.message == (
        "У свойства группы «Строки» пустое имя для проверяемого направления; проверьте заполнение."
    )
    filled = put(
        module_text(),
        "pko",
        """СвойстваТЧ = ДобавитьПКТЧ(ПравилоКонвертации, "Строки", "Строки");
    ДобавитьПКС(СвойстваТЧ, "Код", "Код", 0);""",
    )
    assert analyze(filled)[2].issues == []
    receive = put(
        module_text(),
        "pko",
        """СвойстваТЧ = ДобавитьПКТЧ(ПравилоКонвертации, "Строки", "Строки");
    ДобавитьПКС(СвойстваТЧ, "Код", "", 0);""",
    )
    receive = receive.replace(
        "// <fill-pko>\n    ДобавитьПКО_Товар(ПравилаКонвертации);",
        """Если НаправлениеОбмена = "Получение" Тогда
        ДобавитьПКО_Товар(ПравилаКонвертации);
    КонецЕсли;""",
        1,
    )
    assert "ed.group.empty_property" not in checks(analyze(receive)[2])
    sending = put(
        module_text(),
        "pko",
        """СвойстваТЧ = ДобавитьПКТЧ(ПравилоКонвертации, "Строки", "Строки");
    ДобавитьПКС(СвойстваТЧ, "Код", "", 0);""",
    )
    _document, _references, report = analyze(sending)
    assert checks(report) == ["ed.group.empty_property"]


def test_duplicate_names_warn_inside_one_rule_space():
    text = module_text() + "\n" + _pko_procedure("Товар", procedure="ДобавитьПКО_Копия")
    _document, _references, report = analyze(text)
    assert checks(report) == ["ed.rule.duplicate", "ed.rule.duplicate"]
    assert [issue.address for issue in report.issues] == ["ПКО/Товар#1", "ПКО/Товар#2"]
    assert {issue.message for issue in report.issues} == {
        "Имя ПКО «Товар» объявлено повторно; условия применимости не вычислялись."
    }
    assert {issue.level.value for issue in report.issues} == {"предупреждение"}
    renamed = module_text().replace(
        'ПравилоОбработки.Имя = "Товары";',
        'ПравилоОбработки.Имя = "Товар";',
        1,
    )
    assert "ed.rule.duplicate" not in checks(analyze(renamed)[2])


def test_several_issues_are_sorted_and_repeated_mentions_stay():
    text = drop_routine(module_text(), "ПередКонвертацией")
    text = put(
        text,
        "event",
        """ОбменДаннымиXDTOСервер.ПКОПоИмени(КомпонентыОбмена, "НетПравила");
    ОбменДаннымиXDTOСервер.ПКОПоИмени(КомпонентыОбмена, "НетПравила");""",
    )
    # Событие удалено вместе с процедурой. Ссылки кладём в ПослеКонвертации через замену тела.
    text = text.replace(
        "Процедура ПослеКонвертации(КомпонентыОбмена) Экспорт\nКонецПроцедуры",
        """Процедура ПослеКонвертации(КомпонентыОбмена) Экспорт
    ОбменДаннымиXDTOСервер.ПКОПоИмени(КомпонентыОбмена, "НетПравила");
    ОбменДаннымиXDTOСервер.ПКОПоИмени(КомпонентыОбмена, "НетПравила");
КонецПроцедуры""",
        1,
    )
    _document, _references, report = analyze(text)
    assert [issue.check for issue in report.issues] == [
        "ed.conversion.required",
        "ed.reference.code_rule_missing",
        "ed.reference.code_rule_missing",
    ]
    lines = [int(issue.message.split()[1].rstrip(":")) for issue in report.issues[1:]]
    assert lines[0] < lines[1]
    again = analyze(text)[2]
    assert [(item.check, item.address, item.message) for item in report.issues] == [
        (item.check, item.address, item.message) for item in again.issues
    ]
    assert [(item.check, item.reason) for item in report.skipped] == [
        (item.check, item.reason) for item in again.skipped
    ]


def test_unknown_near_dispatcher_skips_negative_handler_check():
    text = put(module_text(), "pko", 'ПравилоКонвертации.ПриОтправкеДанных = "Обработать";')
    text = put(text, "proc", "НепонятныйВызов();")
    document, _references, report = analyze(text)
    assert "ed.handler.missing" not in checks(report)
    assert document.unknown
    assert any(item.check == "ed.handler.missing" for item in report.skipped)
    assert any(item.check == "ed.reader.incomplete" for item in report.skipped)
    assert any(item.check == "ed.schema" for item in report.skipped)
    broken_helper = module_text().replace(
        "НоваяСтрока = РодительПКС.Добавить();",
        "НоваяСтрока = ДругаяТаблица.Добавить();",
        1,
    )
    broken_helper = put(
        broken_helper,
        "pko",
        'ПравилоКонвертации.ПриОтправкеДанных = "Обработать";',
    )
    document, _references, report = analyze(broken_helper)
    assert "helper_semantics_unverified" in {item.code for item in document.diagnostics}
    assert checks(report) == ["ed.handler.missing"]
    assert any(
        item.check == "ed.reader.incomplete" and "helper_semantics_unverified" in item.reason
        for item in report.skipped
    )


def test_unknown_inside_rule_skips_algorithm_claim_but_not_present_property():
    text = module_text().replace(
        'ДобавитьПКС(СвойстваШапки, "Код", "Код", 0);',
        'ДобавитьПКС(СвойстваШапки, "Код", "Код", 1);',
        1,
    )
    text = put(text, "pko", "ПравилоКонвертации.РучнаяВставка = ВычислитьЗначение();")
    _document, _references, report = analyze(text)
    assert "ed.algorithm.handler_missing" not in checks(report)
    assert any(item.check == "ed.algorithm.handler_missing" for item in report.skipped)
    grouped = module_text().replace(
        'ДобавитьПКС(СвойстваШапки, "Код", "Код", 0);',
        'ДобавитьПКС(СвойстваШапки, "Код", "Код", 1);',
        1,
    )
    grouped = put(
        grouped,
        "pko",
        """ПравилоКонвертации.РучнаяВставка = ВычислитьЗначение();
    СвойстваТЧ = ДобавитьПКТЧ(ПравилоКонвертации, "Строки", "Строки");
    ДобавитьПКС(СвойстваТЧ, "", "", 0);""",
    )
    _document, _references, report = analyze(grouped)
    assert "ed.group.empty_property" in checks(report)


def test_building_the_index_does_not_change_the_snapshot():
    text = module_text()
    document = read_manager_text(text)
    snapshot = read_manager_text(text)
    references = build_references(document)
    report = validate_links(document, build_addresses(document), references)
    assert document == snapshot
    assert report.issues == []
    assert build_references(document) == references
