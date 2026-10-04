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
    parse_registration_object,
    replace_registration_filters,
)
from kd2_rules_mcp.errors import Kd2Error
from kd2_rules_mcp.kd2.canonical import canonical_form, parse_xml
from kd2_rules_mcp.kd2.model import ExchangeRules, Node, RegistrationRules
from kd2_rules_mcp.kd2.rules_io import dump_rules, load_registration_rules
from kd2_rules_mcp.kd2.xmlstyle import KD_STYLE
from kd2_rules_mcp.service import Kd2Service, Settings
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
    assert [issue.check for issue in report.warnings] == ["registration.autoregistration"]


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
    checked = check_registration(loaded, structure)
    assert checked.errors == []
    # Контрагенты в составе с авторегистрацией «Разрешить»: ПРО при записи не исполняется.
    assert [issue.check for issue in checked.warnings] == ["registration.autoregistration"]
    assert "Справочник.Контрагенты" in checked.warnings[0].message


def _or_group() -> dict[str, object]:
    """Группа «ИЛИ»: признак узла «выгружать всё» или организация в табличной части узла."""
    return {
        "operator": "ИЛИ",
        "items": [
            {
                "plan_property": "ИспользоватьОтбор",
                "object_property": "false",
                "property_type": "Булево",
                "comparison": "Равно",
                "constant": True,
            },
            {
                "plan_property": "[Организации].Организация",
                "object_property": "Контрагент",
                "property_type": "СправочникСсылка.Контрагенты",
                "comparison": "Равно",
            },
        ],
    }


def test_or_group_from_node_flag_and_tabular_section(structure: sqlite3.Connection) -> None:
    """Группа «ИЛИ» из константы реквизита узла и значения табличной части.

    Таблицы свойств дописываются из структуры: читатель их пропускает, но писатель
    и типовой макет их выводят (ВыгрузкаРегистрации:339-343).
    """
    spec = parse_registration_object(
        {"metadata_name": "Документ.Приход", "code": "000000001", "plan_filters": [_or_group()]}
    )
    result = build_registration_rules(
        structure, "Обмен", objects=[spec], created_at=WHEN, identifier=RULE_ID
    )
    raw = dump_rules(result.document)
    loaded = load_registration_rules(raw)
    assert canonical_form(raw) == canonical_form(dump_rules(loaded))
    group = _must(_must(parse_xml(raw).find(".//ОтборПоСвойствамПланаОбмена")).find("Группа"))
    assert group.findtext("БулевоЗначениеГруппы") == "ИЛИ"
    flag, tabular = [item for item in group if item.tag == "ЭлементОтбора"]
    assert flag.findtext("ЭтоСтрокаКонстанты") == "true"
    assert flag.findtext("СвойствоОбъекта") == "false"
    assert flag.findtext("СвойствоПланаОбмена") == "ИспользоватьОтбор"
    assert flag.find("ТаблицаСвойствОбъекта") is None
    plan_row = _children(flag.find("ТаблицаСвойствПланаОбмена"))[0]
    assert plan_row.findtext("Наименование") == "ИспользоватьОтбор"
    assert plan_row.findtext("Тип") == "Булево"
    assert plan_row.findtext("Вид") == "Реквизит"
    assert tabular.findtext("СвойствоПланаОбмена") == "[Организации].Организация"
    assert tabular.findtext("ЭтоСтрокаКонстанты") == "false"
    names = [
        row.findtext("Наименование") for row in _children(tabular.find("ТаблицаСвойствПланаОбмена"))
    ]
    assert names == ["[Организации]", "Организация"]
    assert _children(tabular.find("ТаблицаСвойствПланаОбмена"))[0].find("Тип") is None
    object_row = _children(tabular.find("ТаблицаСвойствОбъекта"))[0]
    assert object_row.findtext("Наименование") == "Контрагент"
    assert object_row.findtext("Тип") == "СправочникСсылка.Контрагенты"


def test_nested_filter_group_roundtrip(structure: sqlite3.Connection) -> None:
    """Вложенная группа принимается: читатель групп рекурсивен и глубину не ограничивает."""
    spec = parse_registration_object(
        {
            "metadata_name": "Документ.Приход",
            "plan_filters": [
                {
                    "operator": "И",
                    "items": [
                        _or_group(),
                        {
                            "plan_property": "ДатаНачала",
                            "object_property": "Дата",
                            "property_type": "Дата",
                            "comparison": "БольшеИлиРавно",
                        },
                    ],
                }
            ],
        }
    )
    raw = dump_rules(
        build_registration_rules(
            structure, "Обмен", objects=[spec], created_at=WHEN, identifier=RULE_ID
        ).document
    )
    loaded = load_registration_rules(raw)
    assert canonical_form(raw) == canonical_form(dump_rules(loaded))
    outer = _must(parse_xml(raw).find(".//ОтборПоСвойствамПланаОбмена/Группа"))
    assert outer.findtext("БулевоЗначениеГруппы") == "И"
    assert [item.tag for item in outer] == ["БулевоЗначениеГруппы", "Группа", "ЭлементОтбора"]
    assert _must(outer.find("Группа")).findtext("БулевоЗначениеГруппы") == "ИЛИ"


def test_flat_filter_list_stays_implicit_and(structure: sqlite3.Connection) -> None:
    """Плоский список по-прежнему два элемента корня: читатель соединяет корень через «И»."""
    spec = parse_registration_object(
        {
            "metadata_name": "Документ.Приход",
            "plan_filters": [
                {
                    "plan_property": "ДатаНачала",
                    "object_property": "Дата",
                    "property_type": "Дата",
                    "comparison": "БольшеИлиРавно",
                },
                {
                    "plan_property": "ИспользоватьОтбор",
                    "object_property": "false",
                    "property_type": "Булево",
                    "comparison": "Равно",
                    "constant": True,
                },
            ],
        }
    )
    root = parse_xml(
        dump_rules(
            build_registration_rules(
                structure, "Обмен", objects=[spec], created_at=WHEN, identifier=RULE_ID
            ).document
        )
    )
    items = _children(root.find(".//ОтборПоСвойствамПланаОбмена"))
    assert [item.tag for item in items] == ["ЭлементОтбора", "ЭлементОтбора"]


def test_unsupported_filter_form_is_rejected() -> None:
    """Неизвестный вид сравнения, вид группы и пустая группа — ошибка, а не молчаливая подмена."""
    with pytest.raises(ValueError, match="Вид сравнения «ВСписке»"):
        parse_registration_object(
            {
                "metadata_name": "Документ.Приход",
                "plan_filters": [{"plan_property": "ДатаНачала", "comparison": "ВСписке"}],
            }
        )
    with pytest.raises(ValueError, match="Вид группы отбора «НЕ»"):
        parse_registration_object(
            {
                "metadata_name": "Документ.Приход",
                "object_filters": [{"operator": "НЕ", "items": [{"object_property": "Дата"}]}],
            }
        )
    with pytest.raises(ValueError, match="без элементов"):
        parse_registration_object(
            {"metadata_name": "Документ.Приход", "plan_filters": [{"operator": "ИЛИ", "items": []}]}
        )
    with pytest.raises(ValueError, match="логическим"):
        parse_registration_object(
            {
                "metadata_name": "Документ.Приход",
                "plan_filters": [{"plan_property": "ИспользоватьОтбор", "constant": "false"}],
            }
        )


def test_replace_filters_keeps_other_rules(structure: sqlite3.Connection) -> None:
    """Правка заменяет отборы названных объектов и не трогает остальные правила и заголовок."""
    original = build_registration_rules(
        structure,
        "Обмен",
        objects=[
            RegistrationObject(
                "Документ.Приход",
                code="000000001",
                name="Приход",
                before_processing="Отказ = Ложь;",
                plan_filters=(PlanFilter(plan_property="ДатаНачала", comparison="Равно"),),
                object_filters=(ObjectFilter(object_property="Дата"),),
            ),
            RegistrationObject(
                "Справочник.Контрагенты",
                code="000000002",
                name="Контрагенты",
                object_filters=(ObjectFilter(object_property="ИНН"),),
            ),
        ],
        created_at=WHEN,
        identifier=RULE_ID,
    ).document
    original.root.values["Наименование"] = "Своё имя"
    counterpart_before = original.rules()[1]
    kept_filter = counterpart_before.child("ОтборПоСвойствамОбъекта")
    assert kept_filter is not None
    kept_property = kept_filter.items[0].get("СвойствоОбъекта")
    kept_name = counterpart_before.get("Наименование")
    kept_code = counterpart_before.code
    spec = parse_registration_object(
        {"metadata_name": "Документ.Приход", "plan_filters": [_or_group()]}
    )
    built = build_registration_rules(
        structure, "Обмен", objects=[spec], created_at=WHEN, identifier=RULE_ID
    ).document
    replace_registration_filters(original, built, [spec])
    income = original.rules()[0]
    assert original.root.get("Наименование") == "Своё имя"
    assert income.get("Наименование") == "Приход"
    assert income.get("ПередОбработкой") == "Отказ = Ложь;"
    assert income.code == "000000001"
    plan = _node(income.child("ОтборПоСвойствамПланаОбмена"))
    assert [item.tag for item in plan.items] == ["Группа"]
    # object_filters в описании нет — отбор по объекту остаётся.
    assert _node(income.child("ОтборПоСвойствамОбъекта")).items[0].get("СвойствоОбъекта") == "Дата"
    counterpart = original.rules()[1]
    assert counterpart.get("Наименование") == kept_name
    assert counterpart.code == kept_code
    assert _node(counterpart.child("ОтборПоСвойствамОбъекта")).items[0].get("СвойствоОбъекта") == (
        kept_property
    )


def test_replace_appends_new_object_and_clears_named_filter(structure: sqlite3.Connection) -> None:
    """Нового объекта в проекте нет — правило добавляется. Пустой список снимает отбор."""
    original = build_registration_rules(
        structure,
        "Обмен",
        objects=[RegistrationObject("Документ.Приход", code="000000001", name="Приход")],
        created_at=WHEN,
        identifier=RULE_ID,
    ).document
    spec = parse_registration_object(
        {
            "metadata_name": "Справочник.Контрагенты",
            "plan_filters": [_or_group()],
            "object_filters": [],
        }
    )
    clear = parse_registration_object({"metadata_name": "Документ.Приход", "plan_filters": []})
    built = build_registration_rules(
        structure,
        "Обмен",
        objects=[clear, spec],
        created_at=WHEN,
        identifier=RULE_ID,
    ).document
    replace_registration_filters(original, built, [clear, spec])
    assert _names(original) == ["Документ.Приход", "Справочник.Контрагенты"]
    assert original.rules()[0].child("ОтборПоСвойствамПланаОбмена") is None
    assert original.rules()[0].get("Наименование") == "Приход"
    added = original.rules()[1]
    assert added.code == "000000002"
    assert _node(added.child("ОтборПоСвойствамПланаОбмена")).items[0].tag == "Группа"


def test_registration_build_updates_open_project(tmp_path: Path) -> None:
    """`registration_build` с идентификатором открытого проекта регистрации заменяет отборы."""
    service = Kd2Service(Settings(cache_dir=tmp_path / "cache", workspace=tmp_path / "workspace"))
    service.structure_load_xml("registration", str(DUMP))
    created = service.registration_build(
        "registration",
        "Обмен",
        None,
        [
            {
                "metadata_name": "Документ.Приход",
                "name": "Приход",
                "plan_filters": [
                    {
                        "plan_property": "ДатаНачала",
                        "object_property": "Дата",
                        "comparison": "Равно",
                    }
                ],
            },
            {
                "metadata_name": "Справочник.Контрагенты",
                "object_filters": [{"object_property": "ИНН"}],
            },
        ],
        project_id="reg",
    )
    document = service.workspace.get("reg").document
    assert isinstance(document, RegistrationRules)
    document.rules()[0].values["ПередОбработкой"] = "Отказ = Ложь;"
    updated = service.registration_build(
        "registration",
        "Обмен",
        None,
        [{"metadata_name": "Документ.Приход", "plan_filters": [_or_group()]}],
        project_id="reg",
    )
    assert created["project_id"] == updated["project_id"] == "reg"
    assert updated["counts"]["registration_rules"] == 2
    income, counterpart = document.rules()
    assert income.get("ПередОбработкой") == "Отказ = Ложь;"
    assert income.get("Наименование") == "Приход"
    assert _node(income.child("ОтборПоСвойствамПланаОбмена")).items[0].tag == "Группа"
    assert (
        _node(counterpart.child("ОтборПоСвойствамОбъекта")).items[0].get("СвойствоОбъекта") == "ИНН"
    )
    taken = service.workspace.add(
        _exchange(_pvd("Приход", "ДокументСсылка.Приход")), "exchange", label="exchange"
    )
    with pytest.raises(Kd2Error, match="занят проектом правил обмена"):
        service.registration_build(
            "registration",
            "Обмен",
            None,
            [{"metadata_name": "Документ.Приход"}],
            project_id=taken.id,
        )


def _node(node: Node | None) -> Node:
    assert node is not None
    return node


def _must(element: etree._Element | None) -> etree._Element:
    assert element is not None
    return element


def _children(element: etree._Element | None) -> list[etree._Element]:
    return list(_must(element))


def _child_tags(element: etree._Element | None) -> list[str]:
    return [str(child.tag) for child in _children(element)]
