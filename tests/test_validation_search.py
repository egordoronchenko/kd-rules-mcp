"""Проверка параметров объекта и форм поиска в `ПоследовательностьПолейПоиска`."""

import sqlite3
from pathlib import Path

import pytest

from kd2_rules_mcp.kd2.model import ExchangeRules
from kd2_rules_mcp.kd2.rules_io import load_exchange_rules
from kd2_rules_mcp.structures import db
from kd2_rules_mcp.validation.report import Level, Skipped, ValidationReport
from kd2_rules_mcp.validation.search import (
    CONTINUE_WITHOUT_FIELDS,
    GROUP_FLAG,
    NAME_NOT_SEARCH_PROP,
    NO_KEYS,
    ONLY_GROUP_KEY,
    OWNER_KEY,
    PARAM_NOT_IN_SEARCH,
    PARAM_NOT_PASSED,
    UNREACHABLE_HANDLER,
    check_search_objects,
    check_search_params,
)
from tests.corpus import EXCHANGE_KINDS, CorpusFile, corpus_params

# Юрлицо ищется по ИНН и КПП, если параметр «ТехПараметр1» (вид лица) дошёл до поиска.
_SEARCH = """
Если ПараметрыОбъекта = Неопределено Тогда
	СтрокаИменСвойствПоиска = "ИНН, КПП, Наименование, ЭтоГруппа";
ИначеЕсли ПараметрыОбъекта["ТехПараметр1"] = "Юридическое лицо" Тогда
	СтрокаИменСвойствПоиска = "ИНН, КПП, ЭтоГруппа";
КонецЕсли;
// ПараметрыОбъекта["ВКомментарии"]
"""


def _rules(search: str, properties: str) -> ExchangeRules:
    xml = (
        "<ПравилаОбмена><ВерсияФормата>2.01</ВерсияФормата>"
        "<ПравилаКонвертацииОбъектов><Правило><Код>Контрагенты</Код>"
        f"<ПоследовательностьПолейПоиска>{search}</ПоследовательностьПолейПоиска>"
        "<Источник>СправочникСсылка.Контрагенты</Источник>"
        "<Приемник>СправочникСсылка.Контрагенты</Приемник>"
        f"<Свойства>{properties}</Свойства>"
        "</Правило></ПравилаКонвертацииОбъектов></ПравилаОбмена>"
    )
    return load_exchange_rules(xml.encode())


def _parameter(name: str, *, search: bool = False, disabled: bool = False) -> str:
    attrs = (' Поиск="true"' if search else "") + (' Отключить="true"' if disabled else "")
    return (
        f'<Свойство{attrs}><Код>1</Код><Источник Имя="" Вид=""/><Приемник Имя="" Вид=""/>'
        f"<ИмяПараметраДляПередачи>{name}</ИмяПараметраДляПередачи></Свойство>"
    )


def _checks(rules: ExchangeRules) -> list[tuple[str, Level]]:
    return [(issue.check, issue.level) for issue in check_search_params(rules).issues]


def test_parameter_without_search_flag_is_warning() -> None:
    issues = check_search_params(_rules(_SEARCH, _parameter("ТехПараметр1"))).issues
    assert [(issue.check, issue.level) for issue in issues] == [
        (PARAM_NOT_IN_SEARCH, Level.WARNING)
    ]
    assert issues[0].address == "ПКО «Контрагенты»"
    assert "ТехПараметр1" in issues[0].message and "Поиск" in issues[0].message


def test_search_parameter_is_accepted() -> None:
    assert _checks(_rules(_SEARCH, _parameter("ТехПараметр1", search=True))) == []


@pytest.mark.parametrize(
    "properties", ["", _parameter("Другой", search=True), _parameter("ТехПараметр1", disabled=True)]
)
def test_parameter_not_passed(properties: str) -> None:
    assert _checks(_rules(_SEARCH, properties)) == [(PARAM_NOT_PASSED, Level.WARNING)]


def test_get_method_and_no_handler() -> None:
    search = 'Вид = ПараметрыОбъекта.Получить("ТехПараметр1");'
    assert _checks(_rules(search, _parameter("ТехПараметр1"))) == [
        (PARAM_NOT_IN_SEARCH, Level.WARNING)
    ]
    assert _checks(_rules("", _parameter("ТехПараметр1"))) == []


@pytest.mark.parametrize("corpus_file", corpus_params(EXCHANGE_KINDS))
def test_corpus_runs(corpus_file: CorpusFile) -> None:
    """На корпусе проверка отрабатывает без исключений."""
    rules = load_exchange_rules(corpus_file.path)
    check_search_params(rules)
    check_search_objects(rules)


_CATALOG = "СправочникСсылка.Договоры"
_NO_KEYS = (
    "Нет синхронизации по идентификатору, нет ПКС с признаком поиска "
    "(«Поиск» или «Обязательное») и нет обработчика «ПоследовательностьПолейПоиска»: "
    "узел «Ссылка» не формируется, при каждой загрузке создаётся новый объект"
)
_ONLY_GROUP = (
    "Единственное свойство поиска — «ЭтоГруппа»: условие запроса только по признаку "
    "группы, берётся первая строка — разные элементы находят один и тот же объект"
)
_UNREACHABLE = (
    "Обработчик «ПоследовательностьПолейПоиска» не вызывается: включена синхронизация "
    "по идентификатору и нет продолжения поиска по полям"
)
_CONTINUE = (
    "Включено продолжение поиска по полям, но свойств поиска и обработчика "
    "«ПоследовательностьПолейПоиска» нет: по полям объект не находится и создаётся новый"
)
_GROUP_FLAG = (
    "Приёмник «СправочникСсылка.Номенклатура» — справочник с иерархией групп и элементов, "
    "а включённого ПКС «ЭтоГруппа» с признаком поиска нет: новые группы создаются элементами, "
    "в том числе при синхронизации по идентификатору; при поиске по полям группа "
    "находит одноимённый элемент"
)
_OWNER = (
    "Приёмник «СправочникСсылка.Договоры» — подчинённый справочник, поиск идёт по полям, "
    "а «Владелец» не среди свойств поиска: элементы разных владельцев сливаются в один"
)


def _pko(
    *,
    code: str = "Договоры",
    receiver: str = _CATALOG,
    properties: str = "",
    handler: str = "",
    sync: bool = False,
    continue_search: bool = False,
    not_create: bool = False,
    values: str = "",
) -> ExchangeRules:
    flags = ""
    if sync:
        flags += "<СинхронизироватьПоИдентификатору>true</СинхронизироватьПоИдентификатору>"
    if continue_search:
        flags += (
            "<ПродолжитьПоискПоПолямПоискаЕслиПоИдентификаторуНеНашли>true"
            "</ПродолжитьПоискПоПолямПоискаЕслиПоИдентификаторуНеНашли>"
        )
    if not_create:
        flags += "<НеСоздаватьЕслиНеНайден>true</НеСоздаватьЕслиНеНайден>"
    search = (
        f"<ПоследовательностьПолейПоиска>{handler}</ПоследовательностьПолейПоиска>"
        if handler
        else ""
    )
    xml = (
        "<ПравилаОбмена><ВерсияФормата>2.01</ВерсияФормата>"
        "<ПравилаКонвертацииОбъектов><Правило>"
        f"<Код>{code}</Код>{flags}{search}"
        f"<Источник>{receiver}</Источник><Приемник>{receiver}</Приемник>"
        f"<Свойства>{properties}</Свойства><Значения>{values}</Значения>"
        "</Правило></ПравилаКонвертацииОбъектов></ПравилаОбмена>"
    )
    return load_exchange_rules(xml.encode())


def _prop(
    name: str,
    *,
    search: bool = False,
    required: bool = False,
    disabled: bool = False,
    parameter: str = "",
) -> str:
    attrs = ""
    if search:
        attrs += ' Поиск="true"'
    if required:
        attrs += ' Обязательное="true"'
    if disabled:
        attrs += ' Отключить="true"'
    passed = f"<ИмяПараметраДляПередачи>{parameter}</ИмяПараметраДляПередачи>" if parameter else ""
    return (
        f'<Свойство{attrs}><Источник Имя="{name}" Вид="Свойство"/>'
        f'<Приемник Имя="{name}" Вид="Свойство"/>{passed}</Свойство>'
    )


def _group(inner: str) -> str:
    return (
        '<Группа><Источник Имя="КонтактнаяИнформация" Вид="ТабличнаяЧасть"/>'
        '<Приемник Имя="КонтактнаяИнформация" Вид="ТабличнаяЧасть"/>'
        f"<Свойства>{inner}</Свойства></Группа>"
    )


def _objects(rules: ExchangeRules, target: sqlite3.Connection | None = None) -> ValidationReport:
    return check_search_objects(rules, target)


def _one(report: ValidationReport) -> tuple[str, str, str]:
    assert len(report.issues) == 1
    issue = report.issues[0]
    assert issue.level is Level.WARNING
    return issue.check, issue.address, issue.message


def test_no_keys_when_reference_has_nothing_to_search() -> None:
    report = _objects(_pko())
    assert _one(report) == (NO_KEYS, "ПКО «Договоры»", _NO_KEYS)


def test_no_keys_still_fires_when_not_create_flag_cannot_reach_receiver() -> None:
    """`НеСоздаватьЕслиНеНайден` пишется только в узел «Ссылка», а без ключей его нет."""
    report = _objects(_pko(not_create=True))
    assert _one(report) == (NO_KEYS, "ПКО «Договоры»", _NO_KEYS)


def test_no_keys_fires_for_catalog_that_only_has_value_map() -> None:
    """ПКЗ не закрывает путь обычной выгрузки несопоставленного элемента справочника."""
    values = "<Значение><Источник>Основной</Источник><Приемник>Основной</Приемник></Значение>"
    report = _objects(_pko(values=values))
    assert _one(report) == (NO_KEYS, "ПКО «Договоры»", _NO_KEYS)


@pytest.mark.parametrize(
    ("receiver", "properties", "handler", "sync"),
    [
        ("ПеречислениеСсылка.Виды", "", "", False),
        ("РегистрСведенийЗапись.Курсы", "", "", False),
        (_CATALOG, "", "СсылкаНаОбъект = Неопределено;", False),
        (_CATALOG, "", "", True),
        (_CATALOG, _prop("Код", search=True), "", False),
        (_CATALOG, _prop("Код", required=True), "", False),
        (_CATALOG, _prop("Вид", parameter="ВидЛица", search=True), "", False),
    ],
)
def test_no_keys_does_not_fire_when_form_is_not_a_new_object(
    receiver: str, properties: str, handler: str, sync: bool
) -> None:
    report = _objects(_pko(receiver=receiver, properties=properties, handler=handler, sync=sync))
    assert [issue.check for issue in report.issues] == []


def test_no_keys_ignores_disabled_and_nested_search_properties() -> None:
    properties = _prop("Код", search=True, disabled=True) + _group(_prop("Вид", search=True))
    report = _objects(_pko(properties=properties))
    assert _one(report) == (NO_KEYS, "ПКО «Договоры»", _NO_KEYS)


def test_only_group_key_merges_elements() -> None:
    report = _objects(_pko(properties=_prop("ЭтоГруппа", required=True)))
    assert _one(report) == (ONLY_GROUP_KEY, "ПКО «Договоры»", _ONLY_GROUP)


def test_only_group_key_with_sync_fires_only_when_search_continues() -> None:
    properties = _prop("ЭтоГруппа", search=True)
    stopped = _objects(_pko(properties=properties, sync=True))
    assert [issue.check for issue in stopped.issues] == []
    continued = _objects(_pko(properties=properties, sync=True, continue_search=True))
    assert _one(continued) == (ONLY_GROUP_KEY, "ПКО «Договоры»", _ONLY_GROUP)


def test_only_group_key_does_not_fire_when_another_field_exists() -> None:
    properties = _prop("ЭтоГруппа", search=True) + _prop("Наименование", search=True)
    report = _objects(_pko(properties=properties))
    assert report.issues == []


def test_unreachable_handler_when_sync_finishes_search() -> None:
    report = _objects(
        _pko(
            properties=_prop("Код", search=True),
            handler='СтрокаИменСвойствПоиска = "Код";',
            sync=True,
        )
    )
    assert _one(report) == (UNREACHABLE_HANDLER, "ПКО «Договоры»", _UNREACHABLE)


def test_unreachable_handler_does_not_report_names_that_are_not_evaluated() -> None:
    handler = 'СтрокаИменСвойствПоиска = "НетТакого";'
    report = _objects(_pko(properties=_prop("Код", search=True), handler=handler, sync=True))
    assert [issue.check for issue in report.issues] == [UNREACHABLE_HANDLER]


def test_handler_is_reachable_with_continue_or_without_sync() -> None:
    handler = 'СтрокаИменСвойствПоиска = "Код";'
    properties = _prop("Код", search=True)
    continued = _pko(properties=properties, handler=handler, sync=True, continue_search=True)
    assert _objects(continued).issues == []
    assert _objects(_pko(properties=properties, handler=handler)).issues == []


def test_continue_without_fields_creates_new_object() -> None:
    report = _objects(_pko(sync=True, continue_search=True))
    assert _one(report) == (CONTINUE_WITHOUT_FIELDS, "ПКО «Договоры»", _CONTINUE)


def test_continue_without_fields_does_not_create_when_flag_forbids_it() -> None:
    """`НеСоздаватьЕслиНеНайден` не даёт ветке создания нового объекта выполниться."""
    report = _objects(_pko(sync=True, continue_search=True, not_create=True))
    assert report.issues == []


def test_continue_without_fields_does_not_fire_for_enum_or_when_there_is_a_key() -> None:
    enum = _objects(_pko(receiver="ПеречислениеСсылка.Виды", sync=True, continue_search=True))
    assert enum.issues == []
    with_field = _objects(
        _pko(properties=_prop("Код", search=True), sync=True, continue_search=True)
    )
    assert with_field.issues == []
    with_handler = _objects(
        _pko(handler="ПрекратитьПоиск = Истина;", sync=True, continue_search=True)
    )
    assert with_handler.issues == []


def test_name_not_in_search_properties() -> None:
    empty_variant = 'СтрокаИменСвойствПоиска = "Артикул, Код";'
    report = _objects(_pko(properties=_prop("ИНН", search=True), handler=empty_variant))
    assert _one(report) == (
        NAME_NOT_SEARCH_PROP,
        "ПКО «Договоры»",
        "ПоследовательностьПолейПоиска задаёт имена «Артикул», «Код», которых нет "
        "среди ПКС с признаком поиска: эти имена молча выпадают из условия; "
        "не осталось ни одного имени — вариант поиска ничего не находит",
    )
    partial = 'Если Вид Тогда СтрокаИменСвойствПоиска = "ИНН, Артикул"; КонецЕсли;'
    report = _objects(_pko(properties=_prop("ИНН", search=True), handler=partial))
    assert _one(report) == (
        NAME_NOT_SEARCH_PROP,
        "ПКО «Договоры»",
        "ПоследовательностьПолейПоиска задаёт имя «Артикул», которого нет "
        "среди ПКС с признаком поиска: имя молча выпадает из условия",
    )


def test_name_put_into_search_map_by_handler_does_not_drop() -> None:
    """Имя, записанное в `СвойстваПоиска` до запроса, из строки полей не выпадает."""
    bank = """
    СтрокаИменСвойствПоиска = "НомерСчета, Владелец";
    Если Истина Тогда
        СтрокаИменСвойствПоиска = "Банк, НомерСчета, Владелец";
        СвойстваПоиска["Банк"] = Банк;
    КонецЕсли;
    """
    properties = _prop("НомерСчета", search=True) + _prop("Владелец", search=True)
    report = _objects(_pko(properties=properties, handler=bank, sync=True, continue_search=True))
    assert [issue.check for issue in report.issues] == []
    owner = """
    СтрокаИменСвойствПоиска = "Наименование, Владелец";
    СвойстваПоиска.Вставить("Владелец", ОбъектВладелец);
    """
    report = _objects(
        _pko(properties=_prop("Наименование", search=True), handler=owner, continue_search=True)
    )
    assert [issue.check for issue in report.issues] == []
    read_only = """
    Если СвойстваПоиска["Банк"] = Неопределено Тогда
        СтрокаИменСвойствПоиска = "Банк";
    КонецЕсли;
    """
    report = _objects(_pko(properties=_prop("НомерСчета", search=True), handler=read_only))
    assert _one(report)[0] == NAME_NOT_SEARCH_PROP
    assert "Банк" in report.issues[0].message


def test_known_literal_comment_and_comparison_are_clean() -> None:
    handler = """
    // СтрокаИменСвойствПоиска = "НетТакого";
    Если СтрокаИменСвойствПоиска = "НетТакого" Тогда
        СтрокаИменСвойствПоиска = "ИНН, КПП";
    Иначе
        СтрокаИменСвойствПоиска = "";
    КонецЕсли;
    """
    properties = _prop("ИНН", search=True) + _prop("КПП", required=True)
    report = _objects(_pko(properties=properties, handler=handler))
    assert report.issues == []
    assert all(item.check != NAME_NOT_SEARCH_PROP for item in report.skipped)


def test_opaque_search_string_is_skipped() -> None:
    concatenated = 'СтрокаИменСвойствПоиска = "ИНН" + ", КПП";'
    report = _objects(_pko(properties=_prop("ИНН", search=True), handler=concatenated))
    assert report.issues == []
    assert report.skipped == [
        Skipped(GROUP_FLAG, "структура приёмника не загружена"),
        Skipped(OWNER_KEY, "структура приёмника не загружена"),
        Skipped(
            NAME_NOT_SEARCH_PROP,
            "ПКО «Договоры»: строка имён свойств поиска собрана кодом, а не литералом — "
            "нельзя проверить, какие имена попадут в условие",
        ),
    ]
    indexed = "СтрокаИменСвойствПоиска = ПоляПоискаМассив[НомерВариантаПоиска - 1];"
    names = [item.check for item in _objects(_pko(handler=indexed)).skipped]
    assert names == [GROUP_FLAG, OWNER_KEY, NAME_NOT_SEARCH_PROP]


def test_group_flag_and_owner_without_structure_are_skipped() -> None:
    report = _objects(_pko(properties=_prop("Код", search=True)))
    assert report.issues == []
    assert report.skipped == [
        Skipped(GROUP_FLAG, "структура приёмника не загружена"),
        Skipped(OWNER_KEY, "структура приёмника не загружена"),
    ]


def _structure(path: Path) -> sqlite3.Connection:
    connection = db.create(path / "target.sqlite")
    rows = [
        (
            "Справочник",
            "Номенклатура",
            "СправочникСсылка.Номенклатура",
            '{"Иерархический": "true", "ВидИерархии": "ИерархияГруппИЭлементов", '
            '"Подчиненный": "false"}',
            ["ЭтоГруппа", "Родитель"],
        ),
        (
            "Справочник",
            "Статьи",
            "СправочникСсылка.Статьи",
            '{"Иерархический": "true", "ВидИерархии": "ИерархияЭлементов", "Подчиненный": "false"}',
            ["Родитель"],
        ),
        (
            "Справочник",
            "Договоры",
            "СправочникСсылка.Договоры",
            '{"Иерархический": "false", "Подчиненный": "true"}',
            ["Владелец", "Код"],
        ),
        (
            "ПланВидовХарактеристик",
            "Субконто",
            "ПланВидовХарактеристикСсылка.Субконто",
            '{"Иерархический": "true", "ВидИерархии": "", "Подчиненный": "false"}',
            ["ЭтоГруппа", "Родитель"],
        ),
        (
            "Документ",
            "Реализация",
            "ДокументСсылка.Реализация",
            "{}",
            [],
        ),
        (
            "Справочник",
            "Контрагенты",
            "СправочникСсылка.Контрагенты",
            '{"Иерархический": "false", "ВидИерархии": "ИерархияГруппИЭлементов"}',
            [],
        ),
        (
            "Справочник",
            "Папки",
            "СправочникСсылка.Папки",
            '{"Иерархический": "true", "ВидИерархии": "ИерархияГруппИЭлементов"}',
            [],
        ),
    ]
    for kind, name, type_name, attrs, props in rows:
        cursor = connection.execute(
            "INSERT INTO objects (kind, name, type_name, attrs) VALUES (?, ?, ?, ?)",
            (kind, name, type_name, attrs),
        )
        object_id = cursor.lastrowid
        for prop in props:
            connection.execute(
                "INSERT INTO properties (object_id, kind, name, path) VALUES (?, 'Свойство', ?, ?)",
                (object_id, prop, prop),
            )
    connection.commit()
    return connection


def test_group_flag_for_folder_hierarchy(tmp_path: Path) -> None:
    target = _structure(tmp_path)
    rules = _pko(
        code="Номенклатура",
        receiver="СправочникСсылка.Номенклатура",
        properties=_prop("Наименование", search=True),
    )
    report = _objects(rules, target)
    assert _one(report) == (GROUP_FLAG, "ПКО «Номенклатура»", _GROUP_FLAG)
    clean = _pko(
        code="Номенклатура",
        receiver="СправочникСсылка.Номенклатура",
        properties=_prop("Наименование", search=True) + _prop("ЭтоГруппа", required=True),
    )
    assert _objects(clean, target).issues == []
    items = _pko(
        code="Статьи",
        receiver="СправочникСсылка.Статьи",
        properties=_prop("Наименование", search=True),
    )
    assert _objects(items, target).issues == []


def test_group_flag_needs_hierarchical_catalog(tmp_path: Path) -> None:
    """Вид иерархии по умолчанию у не иерархического справочника предупреждения не даёт."""
    target = _structure(tmp_path)
    flat = _pko(
        code="Контрагенты",
        receiver="СправочникСсылка.Контрагенты",
        properties=_prop("Наименование", search=True),
    )
    assert _objects(flat, target).issues == []
    folders = _pko(
        code="Папки",
        receiver="СправочникСсылка.Папки",
        properties=_prop("Наименование", search=True),
    )
    assert _one(_objects(folders, target))[0] == GROUP_FLAG


def test_group_flag_for_characteristic_chart(tmp_path: Path) -> None:
    target = _structure(tmp_path)
    rules = _pko(
        code="Субконто",
        receiver="ПланВидовХарактеристикСсылка.Субконто",
        properties=_prop("Наименование", search=True),
    )
    report = _objects(rules, target)
    assert _one(report) == (
        GROUP_FLAG,
        "ПКО «Субконто»",
        "Приёмник «ПланВидовХарактеристикСсылка.Субконто» — план видов характеристик "
        "с иерархией групп и элементов, а включённого ПКС «ЭтоГруппа» с признаком поиска нет: "
        "новые группы создаются элементами, в том числе при синхронизации по идентификатору; "
        "при поиске по полям группа находит одноимённый элемент",
    )


def test_owner_key_for_subordinate_catalog(tmp_path: Path) -> None:
    target = _structure(tmp_path)
    rules = _pko(properties=_prop("Код", search=True))
    report = _objects(rules, target)
    assert _one(report) == (OWNER_KEY, "ПКО «Договоры»", _OWNER)
    clean = _pko(properties=_prop("Код", search=True) + _prop("Владелец", search=True))
    assert _objects(clean, target).issues == []
    by_id = _pko(properties=_prop("Код", search=True), sync=True)
    assert _objects(by_id, target).issues == []


def test_missing_receiver_in_structure_is_skipped(tmp_path: Path) -> None:
    target = _structure(tmp_path)
    rules = _pko(
        code="Банки",
        receiver="СправочникСсылка.Банки",
        properties=_prop("Код", search=True),
    )
    report = _objects(rules, target)
    assert report.issues == []
    assert report.skipped == [
        Skipped(
            GROUP_FLAG,
            "ПКО «Банки»: объекта «СправочникСсылка.Банки» нет в структуре приёмника — "
            "нельзя установить иерархию групп и элементов",
        ),
        Skipped(
            OWNER_KEY,
            "ПКО «Банки»: объекта «СправочникСсылка.Банки» нет в структуре приёмника — "
            "нельзя установить, подчинённый ли справочник",
        ),
    ]
