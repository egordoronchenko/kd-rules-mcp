"""Сборка правил регистрации из состава плана обмена (задача 6.4)."""

import sqlite3
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path

import pytest
from lxml import etree

from kd2_rules_mcp.authoring.registration import (
    FilterProperty,
    ObjectFilter,
    ObjectFilterGroup,
    PlanFilter,
    PlanFilterGroup,
    RegistrationObject,
    build_registration_rules,
)
from kd2_rules_mcp.kd2.canonical import canonical_form, parse_xml
from kd2_rules_mcp.kd2.model import ExchangeRules, Node, RegistrationRules
from kd2_rules_mcp.kd2.rules_io import dump_rules, load_registration_rules
from kd2_rules_mcp.kd2.xmlstyle import KD_STYLE
from kd2_rules_mcp.structures.store import StructureStore
from kd2_rules_mcp.validation.format import check_format
from kd2_rules_mcp.validation.registration import check_registration

DUMP = Path(__file__).parent / "data" / "registration" / "dump"
WHEN = datetime(2026, 9, 27, 8, 0, 0)
RULE_ID = "00000000-0000-0000-0000-000000000064"


@pytest.fixture(scope="module")
def structure(tmp_path_factory: pytest.TempPathFactory) -> Iterator[sqlite3.Connection]:
    """Структура из выгрузки `tests/data/registration/dump` (план «Обмен»)."""
    store = StructureStore(tmp_path_factory.mktemp("authoring-registration"))
    store.load_xml("registration", DUMP)
    connection = store.open("registration")
    yield connection
    connection.close()


def _pko(code: str, source: str) -> Node:
    node = Node.new("pko", "Правило")
    node.values["Код"] = code
    node.values["Источник"] = source
    return node


def _pvd(code: str, selection: str = "", conversion: str = "") -> Node:
    node = Node.new("pvd", "Правило")
    node.values["Код"] = code
    if selection:
        node.values["ОбъектВыборки"] = selection
    if conversion:
        node.values["КодПравилаКонвертации"] = conversion
    return node


def _exchange(*rules: Node, pko: list[Node] | None = None) -> ExchangeRules:
    root = Node.new("exchange_rules", "ПравилаОбмена")
    version = Node.new("format_version", "ВерсияФормата")
    version.text = "2.01"
    root.children["ВерсияФормата"] = version
    conversions = Node.new("pko_list", "ПравилаКонвертацииОбъектов")
    conversions.items.extend(pko or [])
    root.children["ПравилаКонвертацииОбъектов"] = conversions
    unloading = Node.new("pvd_list", "ПравилаВыгрузкиДанных")
    unloading.items.extend(rules)
    root.children["ПравилаВыгрузкиДанных"] = unloading
    return ExchangeRules(root, KD_STYLE)


def _sample_rules() -> ExchangeRules:
    """ПВД: Приход по ОбъектВыборки, Контрагенты — по источнику ПКО, Расход вне состава,
    нет такого объекта, и правило без объекта."""
    return _exchange(
        _pvd("Приход", "ДокументСсылка.Приход"),
        _pvd("Контрагенты", conversion="Контрагенты                                       "),
        _pvd("Расход", "ДокументСсылка.Расход"),
        _pvd("НетТакого", "ДокументСсылка.НетТакого"),
        _pvd("БезОбъекта", conversion="НетПКО"),
        pko=[_pko("Контрагенты", "СправочникСсылка.Контрагенты")],
    )


def _names(document: RegistrationRules) -> list[str]:
    return [str(rule.get("ОбъектМетаданныхИмя")) for rule in document.rules()]


def test_default_selection_header_and_plan_content(structure: sqlite3.Connection) -> None:
    """Состав с авторегистрацией, заголовок писателя и выбор объектов по ПВД.

    Приход задан `ОбъектВыборки`. Контрагенты — пустой объект выборки, тип берётся
    из `Источник` ПКО по коду (пробелы кода, как в макете, отбрасываются).
    Расход есть в структуре, но не в составе — ПРО нет. НетТакого нет в структуре — ПРО нет.
    """
    result = build_registration_rules(
        structure,
        "Обмен",
        exchange_rules=_sample_rules(),
        created_at=WHEN,
        identifier=RULE_ID,
    )
    document = result.document
    root = document.root
    assert root.get("ВерсияФормата") == "2.01"
    assert root.get("Ид") == RULE_ID
    assert root.get("Наименование") == "Регистрация: Обмен"
    assert root.get("ДатаВремяСоздания") == "2026-09-27T08:00:00"
    plan = root.child("ПланОбмена")
    assert plan is not None
    assert plan.attrs["Имя"] == "Обмен"
    assert plan.text == "ПланОбменаСсылка.Обмен"
    config = root.child("Конфигурация")
    assert config is not None
    assert config.text == "Регистрация"
    assert config.attrs["ВерсияКонфигурации"] == "1.0.0.1"
    assert config.attrs["СинонимКонфигурации"] == "Регистрация"
    # Приложение в выгрузке XML нет: писатель для неизвестного значения пишет пустую строку.
    assert config.attrs["ВерсияПлатформы"] == ""
    assert [
        (str(item.get("Тип")), item.get("Авторегистрация")) for item in document.plan_content()
    ] == [
        ("СправочникСсылка.Контрагенты", True),
        ("ДокументСсылка.Приход", False),
        ("РегистрСведенийЗапись.Курсы", False),
    ]
    assert _names(document) == ["Документ.Приход", "Справочник.Контрагенты"]
    income = document.rules()[0]
    assert income.get("ОбъектНастройки") == "ДокументСсылка.Приход"
    assert income.get("ОбъектМетаданныхТип") == "Документ"
    assert income.get("Наименование") == "Приход"
    assert income.attrs["Отключить"] is False
    assert income.attrs["Валидное"] is True
    assert result.warnings == [
        "Не удалось определить объект правила выгрузки, правило регистрации не создано: "
        "ПВД «БезОбъекта» (нет ПКО «НетПКО»)",
        "Нет в структуре, правила регистрации не созданы: ДокументСсылка.НетТакого",
        "Не входят в состав плана обмена «Обмен», правила регистрации не созданы: Документ.Расход",
    ]
    report = check_registration(document, structure)
    assert report.errors == []
    assert report.warnings == []


def test_explicit_object_outside_plan_is_created(structure: sqlite3.Connection) -> None:
    """Сценарий «Объект вне состава плана обмена»: ПРО есть, в ответе — предупреждение."""
    result = build_registration_rules(
        structure,
        "ПланОбмена.Обмен",
        objects=[RegistrationObject("Документ.Расход", code="000000009", name="Расход вручную")],
        created_at=WHEN,
        identifier=RULE_ID,
    )
    assert _names(result.document) == ["Документ.Расход"]
    rule = result.document.rules()[0]
    assert rule.code == "000000009"
    assert rule.get("Наименование") == "Расход вручную"
    assert rule.get("ОбъектНастройки") == "ДокументСсылка.Расход"
    assert result.warnings == [
        "Объект «Документ.Расход» нужно добавить в состав плана обмена «Обмен» в конфигурации"
    ]


def test_explicit_object_missing_from_structure(structure: sqlite3.Connection) -> None:
    """Явно указанного объекта нет в структуре — ПРО не создаётся."""
    result = build_registration_rules(
        structure,
        "Обмен",
        objects=[RegistrationObject("Документ.НетТакого")],
        created_at=WHEN,
        identifier=RULE_ID,
    )
    assert result.document.rules() == []
    assert result.warnings == [
        "Нет в структуре, правила регистрации не созданы: Документ.НетТакого"
    ]


def test_filters_roundtrip_and_registration_check(structure: sqlite3.Connection) -> None:
    """Отборы по плану и по объекту выводятся тегами писателя; проверка 5.3 без ошибок.

    Пути свойств — те же, что в `tests/data/registration/ok.xml`: они есть в этой структуре.
    """
    income = RegistrationObject(
        "Документ.Приход",
        unload_mode="РежимВыгрузки",
        before_processing="Отказ = Ложь;",
        plan_filters=(
            PlanFilter(
                plan_property="ИспользоватьОтбор",
                object_property="false",
                property_type="Булево",
                comparison="Равно",
                constant=True,
                plan_properties=(FilterProperty("ИспользоватьОтбор", "Булево", "Реквизит"),),
            ),
            PlanFilter(
                plan_property="ДатаНачала",
                object_property="Дата",
                property_type="Дата",
                comparison="БольшеИлиРавно",
                object_properties=(FilterProperty("Дата", "Дата", "Свойство"),),
                plan_properties=(FilterProperty("ДатаНачала", "Дата", "Реквизит"),),
            ),
            PlanFilterGroup(
                "ИЛИ",
                (
                    PlanFilter(
                        plan_property="[Организации].Организация.ИНН",
                        object_property="Контрагент.ИНН",
                        property_type="Строка",
                        comparison="Равно",
                        object_properties=(
                            FilterProperty(
                                "Контрагент", "СправочникСсылка.Контрагенты", "Реквизит"
                            ),
                            FilterProperty("ИНН", "Строка", "Реквизит"),
                        ),
                        plan_properties=(
                            FilterProperty("[Организации]", kind="ТабличнаяЧасть"),
                            FilterProperty(
                                "Организация", "СправочникСсылка.Контрагенты", "Реквизит"
                            ),
                            FilterProperty("ИНН", "Строка", "Реквизит"),
                        ),
                    ),
                ),
            ),
        ),
        object_filters=(
            ObjectFilterGroup(
                "И",
                (
                    ObjectFilter(
                        object_property="Товары.Контрагент.Код",
                        property_type="Строка",
                        comparison="Равно",
                        element_kind="АлгоритмЗначения",
                        constant_value='Значение = "";',
                        object_properties=(
                            FilterProperty("Товары", kind="ТабличнаяЧасть"),
                            FilterProperty(
                                "Контрагент", "СправочникСсылка.Контрагенты", "Реквизит"
                            ),
                            FilterProperty("Код", "Строка", "Свойство"),
                        ),
                    ),
                ),
            ),
        ),
    )
    result = build_registration_rules(
        structure,
        "Обмен",
        objects=[income, RegistrationObject("Справочник.Контрагенты")],
        created_at=WHEN,
        identifier=RULE_ID,
    )
    assert result.warnings == []
    raw = dump_rules(result.document)
    root = parse_xml(raw)
    rules = root.find("ПравилаРегистрацииОбъектов")
    assert rules is not None
    first, second = list(rules)
    plan_items = _children(first.find("ОтборПоСвойствамПланаОбмена"))
    assert [item.tag for item in plan_items] == ["ЭлементОтбора", "ЭлементОтбора", "Группа"]
    constant, by_date, group = plan_items
    assert _child_tags(constant) == [
        "ЭтоСтрокаКонстанты",
        "ТипСвойстваОбъекта",
        "СвойствоПланаОбмена",
        "ВидСравнения",
        "СвойствоОбъекта",
        "ТаблицаСвойствПланаОбмена",
    ]
    assert constant.findtext("ЭтоСтрокаКонстанты") == "true"
    assert constant.findtext("СвойствоОбъекта") == "false"
    assert _child_tags(_children(constant.find("ТаблицаСвойствПланаОбмена"))[0]) == [
        "Наименование",
        "Тип",
        "Вид",
    ]
    assert _child_tags(by_date) == [
        "ЭтоСтрокаКонстанты",
        "ТипСвойстваОбъекта",
        "СвойствоПланаОбмена",
        "ВидСравнения",
        "СвойствоОбъекта",
        "ТаблицаСвойствОбъекта",
        "ТаблицаСвойствПланаОбмена",
    ]
    assert by_date.findtext("ЭтоСтрокаКонстанты") == "false"
    assert _child_tags(group) == ["БулевоЗначениеГруппы", "ЭлементОтбора"]
    assert group.findtext("БулевоЗначениеГруппы") == "ИЛИ"
    nested = _must(group.find("ЭлементОтбора"))
    tabular = _children(nested.find("ТаблицаСвойствПланаОбмена"))[0]
    assert tabular.findtext("Наименование") == "[Организации]"
    assert tabular.find("Тип") is None
    assert tabular.findtext("Вид") == "ТабличнаяЧасть"
    obj_group = _must(_must(first.find("ОтборПоСвойствамОбъекта")).find("Группа"))
    assert _child_tags(obj_group) == ["БулевоЗначениеГруппы", "ЭлементОтбора"]
    selection = _must(obj_group.find("ЭлементОтбора"))
    assert _child_tags(selection) == [
        "ТипСвойстваОбъекта",
        "ВидСравнения",
        "СвойствоОбъекта",
        "Вид",
        "ЗначениеКонстанты",
        "ТаблицаСвойствОбъекта",
    ]
    assert selection.findtext("Вид") == "АлгоритмЗначения"
    assert selection.findtext("ЗначениеКонстанты") == 'Значение = "";'
    assert first.findtext("ПередОбработкой") == "Отказ = Ложь;"
    assert first.find("ПриОбработке") is None
    assert first.findtext("РеквизитРежимаВыгрузки") == "РежимВыгрузки"
    # Пустые деревья отбора писатель всё равно открывает.
    assert _child_tags(second.find("ОтборПоСвойствамПланаОбмена")) == []
    assert _child_tags(second.find("ОтборПоСвойствамОбъекта")) == []
    loaded = load_registration_rules(raw)
    assert canonical_form(raw) == canonical_form(dump_rules(loaded))
    assert check_format(result.document).errors == []
    assert check_format(loaded).errors == []
    assert check_registration(loaded, structure).errors == []
    assert check_registration(loaded, structure).warnings == []


def _must(element: etree._Element | None) -> etree._Element:
    assert element is not None
    return element


def _children(element: etree._Element | None) -> list[etree._Element]:
    return list(_must(element))


def _child_tags(element: etree._Element | None) -> list[str]:
    return [str(child.tag) for child in _children(element)]
