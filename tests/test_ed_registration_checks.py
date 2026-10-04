"""Адаптер менеджера регистрации к проверкам `RegistrationRules`."""

import sqlite3
from collections import Counter
from collections.abc import Iterator
from pathlib import Path

import pytest

import kd2_rules_mcp.validation.ed_registration as registration_adapter
from kd2_rules_mcp.ed.model import ParseStatus
from kd2_rules_mcp.ed.registration import read_registration_manager, read_registration_manager_text
from kd2_rules_mcp.kd2.model import RegistrationRules, RulesDocument
from kd2_rules_mcp.kd2.rules_io import load_registration_rules
from kd2_rules_mcp.structures.store import StructureStore
from kd2_rules_mcp.validation.ed_registration import check_registration_module
from kd2_rules_mcp.validation.registration import check_registration
from kd2_rules_mcp.validation.report import ValidationReport

DATA = Path(__file__).parent / "data" / "ed" / "registration"
XML_RULES = Path(__file__).parent / "data" / "registration"
MISSING = """
Процедура ИнициализацияПравилРегистрации(ПравилаРегистрацииОбъектов, ИмяМенеджераРегистрации)
	ДобавитьПРО_Документ_НетТакого(ПравилаРегистрацииОбъектов, ИмяМенеджераРегистрации);
КонецПроцедуры

Процедура ДобавитьПРО_Документ_НетТакого(ПравилаРегистрацииОбъектов, ИмяМенеджераРегистрации)
	НовоеПравило = ПравилаРегистрацииОбъектов.Добавить();
	НовоеПравило.ИмяПланаОбмена = "УзелЗаказов";
	НовоеПравило.ОбъектМетаданныхИмя = "Документ.НетТакого";
	НовоеПравило.Идентификатор = "НетТакого";
	НовоеПравило.ИмяМенеджераРегистрации = ИмяМенеджераРегистрации;
КонецПроцедуры
"""


@pytest.fixture(scope="module")
def structure(tmp_path_factory: pytest.TempPathFactory) -> Iterator[sqlite3.Connection]:
    store = StructureStore(tmp_path_factory.mktemp("ed-registration"))
    store.load_xml("edreg", DATA / "dump")
    connection = store.open("edreg")
    yield connection
    connection.close()


@pytest.fixture(scope="module")
def registration_structure(
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[sqlite3.Connection]:
    store = StructureStore(tmp_path_factory.mktemp("registration-options"))
    store.load_xml("registration", XML_RULES / "dump")
    connection = store.open("registration")
    yield connection
    connection.close()


def _facts(report) -> Counter[tuple[str, str]]:
    return Counter((issue.check, issue.message) for issue in report.issues)


def test_equal_xml_and_manager_have_equal_issues(structure: sqlite3.Connection) -> None:
    """Одинаковые факты XML и менеджера дают одинаковые замечания; адреса разные."""
    xml = check_registration(load_registration_rules(DATA / "equal.xml"), structure)
    manager = check_registration_module(read_registration_manager(DATA / "equal.bsl"), structure)
    assert _facts(xml) == _facts(manager)
    assert _facts(manager) == Counter(
        {
            (
                "registration.object_property",
                "Правило «ЗаказВыгружать»: свойство «НетПоля» объекта «Документ.Заказ»"
                " не найдено (нет «НетПоля»)",
            ): 1
        }
    )
    assert xml.issues[0].address == "ПРО «ЗаказВыгружать»"
    assert manager.issues[0].address == "Регистрация/ПРО/ЗаказВыгружать"
    assert any(item.check == "registration.settings_type" for item in manager.skipped)
    assert not any(issue.check == "registration.plan_content" for issue in manager.issues)
    assert any(item.check == "registration.plan_content" for item in manager.skipped)
    assert not any(issue.check == "registration.settings_type" for issue in manager.issues)


def test_manager_without_structure_skips_instead_of_inventing_content() -> None:
    document = read_registration_manager(DATA / "equal.bsl")
    report = check_registration_module(document, None)
    assert report.issues == []
    assert {item.check for item in report.skipped} == {
        "registration.exchange_plan",
        "registration.object",
        "registration.plan_membership",
        "registration.plan_property",
        "registration.object_property",
        "registration.unload_mode",
        "registration.plan_content",
        "registration.autoregistration",
        "registration.no_pvd",
        "registration.settings_type",
    }


def test_ambiguous_settings_are_skipped_and_object_error_remains(
    structure: sqlite3.Connection,
) -> None:
    report = check_registration_module(read_registration_manager_text(MISSING), structure)
    assert [issue.check for issue in report.issues] == ["registration.object"]
    assert "Документ.НетТакого" in report.issues[0].message
    assert report.issues[0].address == "Регистрация/ПРО/НетТакого"
    assert not any(issue.check == "registration.settings_type" for issue in report.issues)
    assert not any(issue.check == "registration.plan_content" for issue in report.issues)
    assert any(item.check == "registration.settings_type" for item in report.skipped)
    assert any(item.check == "registration.plan_content" for item in report.skipped)


def test_broken_filter_is_skipped_not_treated_as_empty(structure: sqlite3.Connection) -> None:
    report = check_registration_module(read_registration_manager(DATA / "broken.bsl"), structure)
    assert any(
        item.check == "registration.plan_property" and "повреждённый XML" in item.reason
        for item in report.skipped
    )
    assert not any(
        issue.check == "registration.plan_property" and "ЗаказБитый" in issue.message
        for issue in report.issues
    )


def test_projection_cannot_be_saved_as_rules() -> None:
    """Проекция не возвращается: снаружи есть только отчёт проверки."""
    defined = [
        name
        for name, value in vars(registration_adapter).items()
        if callable(value)
        and getattr(value, "__module__", "") == registration_adapter.__name__
        and not name.startswith("_")
    ]
    assert defined == ["check_registration_module"]
    document = read_registration_manager(DATA / "equal.bsl")
    report = check_registration_module(document, None)
    assert isinstance(report, ValidationReport)
    assert not isinstance(report, RulesDocument | RegistrationRules)


def test_plan_content_option_skips_without_dropping_other_findings(
    registration_structure: sqlite3.Connection,
) -> None:
    rules = load_registration_rules(XML_RULES / "issues.xml")
    full = check_registration(rules, registration_structure)
    limited = check_registration(rules, registration_structure, has_plan_content=False)
    assert any(issue.check == "registration.plan_content" for issue in full.issues)
    assert not any(issue.check == "registration.plan_content" for issue in limited.issues)
    assert any(item.check == "registration.plan_content" for item in limited.skipped)
    assert _facts(limited) == Counter(
        (issue.check, issue.message)
        for issue in full.issues
        if issue.check != "registration.plan_content"
    )


def test_object_settings_option_skips_the_check(
    registration_structure: sqlite3.Connection,
) -> None:
    rules = load_registration_rules(XML_RULES / "issues.xml")
    full = check_registration(rules, registration_structure)
    without = check_registration(rules, registration_structure, has_object_settings=False)
    assert any(issue.check == "registration.settings_type" for issue in full.warnings)
    assert not any(issue.check == "registration.settings_type" for issue in without.issues)
    assert any(item.check == "registration.settings_type" for item in without.skipped)
    assert [(issue.check, issue.message) for issue in without.errors] == [
        (issue.check, issue.message) for issue in full.errors
    ]


_HELPER = """
Процедура УстановитьОтборы(ПравилоРегистрации, ОтборПоСвойствамПланаОбмена, ОтборПоСвойствамОбъекта)
КонецПроцедуры
"""


_ADD = "\tДобавитьПРО_Документ_Заказ(ПравилаРегистрацииОбъектов, ИмяМенеджераРегистрации);\n"
_ADD_GOODS = (
    "\t\tДобавитьПРО_Справочник_Товары(ПравилаРегистрацииОбъектов, ИмяМенеджераРегистрации);\n"
)


def _sample(init: str, rule: str, *, parameters: str = "", functions: str = "") -> str:
    if not functions:
        functions = (
            "Функция ОтборПлана()\n"
            '\tВозврат "<ОтборПоСвойствамПланаОбмена/>";\n'
            "КонецФункции\n"
            "Функция ОтборОбъекта()\n"
            '\tВозврат "<ОтборПоСвойствамОбъекта><ЭлементОтбора>'
            "<СвойствоОбъекта>НетПоля</СвойствоОбъекта>"
            '</ЭлементОтбора></ОтборПоСвойствамОбъекта>";\n'
            "КонецФункции\n"
        )
    return f"""
{parameters}
Процедура ИнициализацияПравилРегистрации(ПравилаРегистрацииОбъектов, ИмяМенеджераРегистрации)
{init}
КонецПроцедуры
{rule}
{functions}
{_HELPER}
"""


def _one_rule(body: str = "", *, filters: str | None = "") -> str:
    if filters == "":
        filters = "\tУстановитьОтборы(НовоеПравило, ОтборПлана(), ОтборОбъекта());\n"
    return f"""
Процедура ДобавитьПРО_Документ_Заказ(ПравилаРегистрацииОбъектов, ИмяМенеджераРегистрации)
	НовоеПравило = ПравилаРегистрацииОбъектов.Добавить();
	НовоеПравило.ИмяПланаОбмена = "УзелЗаказов";
	НовоеПравило.ОбъектМетаданныхИмя = "Документ.Заказ";
	НовоеПравило.Идентификатор = "ЗаказВыгружать";
	НовоеПравило.ИмяМенеджераРегистрации = ИмяМенеджераРегистрации;
{body}{filters or ""}
КонецПроцедуры
"""


def test_unreadable_filter_is_skipped(structure: sqlite3.Connection) -> None:
    """Непрочитанный отбор не оставляет чистый отчёт и не прячет проверку свойства."""
    text = (
        (DATA / "equal.bsl")
        .read_text(encoding="utf-8")
        .replace("ОтборОбъекта());", "Внешний.Отбор());")
    )
    document = read_registration_manager_text(text)
    assert document.parse_status is ParseStatus.PARTIAL
    report = check_registration_module(document, structure)
    assert not any("НетПоля" in issue.message for issue in report.issues)
    assert any(
        item.check == "registration.object_property" and "ЗаказВыгружать" in item.reason
        for item in report.skipped
    )
    assert any(
        item.check == "registration.unknown" and item.reason.startswith("неизвестных фрагментов:")
        for item in report.skipped
    )

    dropped = read_registration_manager_text(
        _sample(
            _ADD + "\tЕсли Ложь Тогда\n" + _ADD_GOODS + "\tКонецЕсли;\n",
            _one_rule()
            + """
Процедура ДобавитьПРО_Справочник_Товары(ПравилаРегистрацииОбъектов, ИмяМенеджераРегистрации)
	НовоеПравило = ПравилаРегистрацииОбъектов.Добавить();
	НовоеПравило.ОбъектМетаданныхИмя = "Документ.НетТакого";
	НовоеПравило.Идентификатор = "Спрятан";
	НовоеПравило.ИмяПланаОбмена = "УзелЗаказов";
	НовоеПравило.ИмяМенеджераРегистрации = ИмяМенеджераРегистрации;
КонецПроцедуры
""",
        )
    )
    hidden = check_registration_module(dropped, structure)
    assert [rule.identifier for rule in dropped.rules] == ["ЗаказВыгружать"]
    assert not any("НетТакого" in issue.message for issue in hidden.issues)
    assert any(item.check == "registration.unknown" for item in hidden.skipped)

    bare = read_registration_manager_text(
        _sample(
            _ADD,
            _one_rule(filters=None),
        )
    )
    assert bare.parse_status is ParseStatus.COMPLETE
    quiet = check_registration_module(bare, structure)
    assert any(
        item.check == "registration.plan_property"
        and "ЗаказВыгружать: отбор не прочитан" in item.reason
        for item in quiet.skipped
    )
    assert any(
        item.check == "registration.object_property" and "отбор не прочитан" in item.reason
        for item in quiet.skipped
    )
    assert not any(item.check == "registration.unknown" for item in quiet.skipped)


def test_swapped_roots_are_skipped(structure: sqlite3.Connection) -> None:
    document = read_registration_manager_text(
        _sample(
            _ADD,
            _one_rule(filters="\tУстановитьОтборы(НовоеПравило, ОтборОбъекта(), ОтборПлана());\n"),
        )
    )
    report = check_registration_module(document, structure)
    assert not any(issue.check == "registration.object_property" for issue in report.issues)
    assert not any(issue.check == "registration.plan_property" for issue in report.issues)
    assert any(
        item.check == "registration.object_property" and "корень отбора" in item.reason
        for item in report.skipped
    )
    clean = check_registration_module(read_registration_manager(DATA / "equal.bsl"), structure)
    assert any(issue.check == "registration.object_property" for issue in clean.issues)


def test_colliding_identifier_issue_stays_on_its_rule(structure: sqlite3.Connection) -> None:
    text = """
Процедура ИнициализацияПравилРегистрации(ПравилаРегистрацииОбъектов, ИмяМенеджераРегистрации)
	ДобавитьПРО_А(ПравилаРегистрацииОбъектов, ИмяМенеджераРегистрации);
	ДобавитьПРО_Б(ПравилаРегистрацииОбъектов, ИмяМенеджераРегистрации);
	ДобавитьПРО_В(ПравилаРегистрацииОбъектов, ИмяМенеджераРегистрации);
КонецПроцедуры
"""
    rules = []
    for name, ident, metadata in (
        ("А", "X", "Документ.НетТакого"),
        ("Б", "X", "Документ.Заказ"),
        ("В", "X#1", "Документ.Заказ"),
    ):
        rules.append(
            f"""
Процедура ДобавитьПРО_{name}(ПравилаРегистрацииОбъектов, ИмяМенеджераРегистрации)
	НовоеПравило = ПравилаРегистрацииОбъектов.Добавить();
	НовоеПравило.ИмяПланаОбмена = "УзелЗаказов";
	НовоеПравило.ОбъектМетаданныхИмя = "{metadata}";
	НовоеПравило.Идентификатор = "{ident}";
	НовоеПравило.ИмяМенеджераРегистрации = ИмяМенеджераРегистрации;
КонецПроцедуры
"""
        )
    document = read_registration_manager_text(text + "".join(rules) + _HELPER)
    report = check_registration_module(document, structure)
    missed = [issue for issue in report.issues if issue.check == "registration.object"]
    assert len(missed) == 1
    assert missed[0].address == f"Регистрация/ПРО/{document.rules[0].qualified_id}"
    assert document.rules[0].qualified_id != document.rules[2].qualified_id


def test_rule_plan_is_not_replaced_by_parameter(structure: sqlite3.Connection) -> None:
    parameters = """
Функция ПараметрыРегистрации() Экспорт
	Результат = Новый Структура();
	Результат.Вставить("ПланаОбмена", "УзелЗаказов");
	Возврат Результат;
КонецФункции
"""
    other = _one_rule().replace('ИмяПланаОбмена = "УзелЗаказов"', 'ИмяПланаОбмена = "ДругойПлан"')
    report = check_registration_module(
        read_registration_manager_text(
            parameters
            + _sample(
                _ADD,
                other,
            )
        ),
        structure,
    )
    assert any(
        issue.level.value == "предупреждение"
        and issue.check == "registration.exchange_plan"
        and "ДругойПлан" in issue.message
        and "ЗаказВыгружать" in issue.message
        for issue in report.issues
    )
    assert any(
        "ДругойПлан" in issue.message for issue in report.issues if issue.level.value == "ошибка"
    )

    unknown_plan = """
Функция ПараметрыРегистрации() Экспорт
	Результат = Новый Структура();
	План = "УзелЗаказов";
	План = План + "Старый";
	Результат.Вставить("ПланаОбмена", План);
	Возврат Результат;
КонецФункции
"""
    without_name = _one_rule().replace('\tНовоеПравило.ИмяПланаОбмена = "УзелЗаказов";\n', "")
    unresolved = check_registration_module(
        read_registration_manager_text(
            unknown_plan
            + _sample(
                _ADD,
                without_name,
            )
        ),
        structure,
    )
    assert not any(issue.check == "registration.exchange_plan" for issue in unresolved.issues)
    assert any(
        item.check == "registration.exchange_plan" and item.reason == "план обмена не определён"
        for item in unresolved.skipped
    )


def test_unknown_xml_tag_warns_with_rule_code(structure: sqlite3.Connection) -> None:
    typo = (
        "Функция ОтборПлана()\n"
        '\tВозврат "<ОтборПоСвойствамПланаОбмена><ЭлементОтбора>'
        "<ВидСравненя>Равно</ВидСравненя>"
        '</ЭлементОтбора></ОтборПоСвойствамПланаОбмена>";\n'
        "КонецФункции\n"
        "Функция ОтборОбъекта()\n"
        '\tВозврат "<ОтборПоСвойствамОбъекта/>";\n'
        "КонецФункции\n"
    )
    document = read_registration_manager_text(
        _sample(
            _ADD,
            _one_rule(),
            functions=typo,
        )
    )
    assert document.parse_status is ParseStatus.COMPLETE
    report = check_registration_module(document, structure)
    assert any(
        issue.level.value == "предупреждение"
        and issue.check == "registration.unknown_tag"
        and "ВидСравненя" in issue.message
        and "ЗаказВыгружать" in issue.address
        for issue in report.issues
    )
    clean = check_registration_module(
        read_registration_manager_text(
            _sample(
                _ADD,
                _one_rule(
                    filters="\tУстановитьОтборы(НовоеПравило, ОтборПлана(), ОтборОбъекта());\n"
                ),
                functions="""
Функция ОтборПлана()
	Возврат "<ОтборПоСвойствамПланаОбмена/>";
КонецФункции
Функция ОтборОбъекта()
	Возврат "<ОтборПоСвойствамОбъекта/>";
КонецФункции
""",
            )
        ),
        structure,
    )
    assert not any(issue.check == "registration.unknown_tag" for issue in clean.issues)
