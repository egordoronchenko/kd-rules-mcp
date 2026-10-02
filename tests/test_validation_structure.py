"""Проверка правил обмена по структурам (спецификация `rules-validation`, «Проверка по структурам
метаданных»). Источник — синтетическая выгрузка `main` + `ext`, приёмник — `main` без расширения:
в приёмнике нет реквизита `КомментарийРасш`, ТЧ `Спецификация` и значения `Корректировка`."""

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from kd2_rules_mcp.kd2.rules_io import load_exchange_rules
from kd2_rules_mcp.structures.store import StructureStore
from kd2_rules_mcp.validation.report import Issue, Level, ValidationReport
from kd2_rules_mcp.validation.structure import check_rule, check_structures

DUMP = Path(__file__).parent / "data" / "xmldump"


@pytest.fixture(scope="module")
def sides(tmp_path_factory: pytest.TempPathFactory) -> Iterator[tuple[sqlite3.Connection, ...]]:
    store = StructureStore(tmp_path_factory.mktemp("cache"))
    store.load_xml("source", DUMP / "main", [DUMP / "ext"])
    store.load_xml("target", DUMP / "main")
    source, target = store.open("source"), store.open("target")
    yield source, target
    source.close()
    target.close()


def rules_xml(pko: str, pvd: str = "", pod: str = "") -> bytes:
    """Минимальные правила обмена с заданными ПКО, ПВД и ПОД."""
    return f"""<ПравилаОбмена>
<ВерсияФормата>2.01</ВерсияФормата>
<Источник>Источник</Источник>
<Приемник>Приемник</Приемник>
<ПравилаКонвертацииОбъектов>{pko}</ПравилаКонвертацииОбъектов>
<ПравилаВыгрузкиДанных>{pvd}</ПравилаВыгрузкиДанных>
<ПравилаОчисткиДанных>{pod}</ПравилаОчисткиДанных>
</ПравилаОбмена>""".encode()


def pko_xml(code: str, source: str, target: str, body: str = "", values: str = "") -> str:
    return (
        f"<Правило><Код>{code}</Код><Источник>{source}</Источник><Приемник>{target}</Приемник>"
        f"<Свойства>{body}</Свойства><Значения>{values}</Значения></Правило>"
    )


def pks_xml(
    source: str, target: str, kind: str = "Реквизит", extra: str = "", attrs: str = ""
) -> str:
    return (
        f'<Свойство{attrs}><Источник Имя="{source}" Вид="{kind}"/>'
        f'<Приемник Имя="{target}" Вид="{kind}"/>{extra}</Свойство>'
    )


def check(sides: tuple[sqlite3.Connection, ...], xml: bytes) -> ValidationReport:
    source, target = sides
    return check_structures(load_exchange_rules(xml), source, target)


def only(report: ValidationReport, check_id: str) -> list[Issue]:
    return [issue for issue in report.issues if issue.check == check_id]


NOMENCLATURE = "СправочникСсылка.Номенклатура"
CATALOG_PKO = pko_xml("Номенклатура", NOMENCLATURE, NOMENCLATURE)


def test_valid_rules_have_no_issues(sides: tuple[sqlite3.Connection, ...]) -> None:
    body = (
        pks_xml("Артикул", "Артикул")
        + pks_xml(
            "Владелец",
            "Владелец",
            "Свойство",
            "<КодПравилаКонвертации>Контр</КодПравилаКонвертации>",
        )
        + '<Группа><Источник Имя="Товары" Вид="ТабличнаяЧасть"/>'
        '<Приемник Имя="Товары" Вид="ТабличнаяЧасть"/><Свойства/>'
        + pks_xml("Номенклатура", "Номенклатура")
        + "</Группа>"
    )
    xml = rules_xml(
        pko_xml("Номенклатура", NOMENCLATURE, NOMENCLATURE, body)
        + pko_xml("Контр", "СправочникСсылка.Контрагенты", "СправочникСсылка.Контрагенты"),
        pvd="<Правило><Код>В</Код><КодПравилаКонвертации>Номенклатура</КодПравилаКонвертации>"
        f"<ОбъектВыборки>{NOMENCLATURE}</ОбъектВыборки></Правило>",
    )
    report = check(sides, xml)
    assert report.issues == []
    assert report.skipped == []
    assert report.summary() == "Ошибок и предупреждений нет"


def test_target_attribute_removed(sides: tuple[sqlite3.Connection, ...]) -> None:
    """Сценарий «Реквизит удалён в приёмнике»: ошибка с кодом ПКО и путём свойства."""
    xml = rules_xml(
        pko_xml(
            "Номенклатура",
            NOMENCLATURE,
            NOMENCLATURE,
            pks_xml("КомментарийРасш", "КомментарийРасш"),
        )
    )
    issues = check(sides, xml).issues
    assert [(i.level, i.check, i.address) for i in issues] == [
        (Level.ERROR, "structure.pks_target", "ПКО «Номенклатура» / ПКС КомментарийРасш"),
        (Level.WARNING, "structure.pko_unreachable", "ПКО «Номенклатура»"),
    ]
    assert "КомментарийРасш" in issues[0].message


def test_tabular_section_paths_and_group_kind(sides: tuple[sqlite3.Connection, ...]) -> None:
    """Свойства группы ищутся по пути `ТЧ.Реквизит`; вид группы выбирает коллекцию."""
    group = (
        '<Группа><Источник Имя="Спецификация" Вид="ТабличнаяЧасть"/>'
        '<Приемник Имя="Товары" Вид="ТабличнаяЧасть"/><Свойства/>'
        + pks_xml("Содержание", "Аналитика")
        + "</Группа>"
        '<Группа><Источник Имя="Товары" Вид="НаборДвиженийРегистраНакопления"/>'
        '<Приемник Имя="Товары" Вид="ТабличнаяЧасть"/><Свойства/>'
        + pks_xml(
            "Номенклатура",
            "Номенклатура",
            extra="<КодПравилаКонвертации>Номенклатура</КодПравилаКонвертации>",
        )
        + "</Группа>"
    )
    report = check(sides, rules_xml(pko_xml("Номенклатура", NOMENCLATURE, NOMENCLATURE, group)))
    assert [(i.check, i.address) for i in report.issues] == [
        # В приёмнике (без расширения) у ТЧ Товары нет колонки Аналитика.
        ("structure.pks_target", "ПКО «Номенклатура» / ПКС Товары/Аналитика"),
        # Набора движений Товары у справочника нет — его свойства в источнике не проверяются.
        ("structure.pks_source", "ПКО «Номенклатура» / ПКС Товары"),
    ]
    assert "«НаборДвиженийРегистраНакопления»" in report.issues[1].message


def test_check_rule_reports_only_the_touched_node(sides: tuple[sqlite3.Connection, ...]) -> None:
    """Группа без коллекции даёт то же замечание, что документ; реквизит внутри и ПКО — нет."""
    group = (
        '<Группа><Источник Имя="Товары" Вид="НаборДвиженийРегистраНакопления"/>'
        '<Приемник Имя="Товары" Вид="ТабличнаяЧасть"/><Свойства/>'
        + pks_xml("Номенклатура", "Номенклатура")
        + "</Группа>"
    )
    rules = load_exchange_rules(
        rules_xml(pko_xml("Номенклатура", NOMENCLATURE, NOMENCLATURE, group))
    )
    source, target = sides
    pko = rules.pko()[0]
    properties = pko.child("Свойства")
    assert properties is not None
    group_node = next(item for item in properties.items if item.is_group)
    child = next(item for item in group_node.items if not item.is_group)
    full = check_structures(rules, source, target)
    group_report = check_rule(rules, group_node, source, target)
    group_address = "ПКО «Номенклатура» / ПКС Товары"
    assert [(i.check, i.message) for i in group_report.issues] == [
        (i.check, i.message) for i in full.issues if i.address == group_address
    ]
    assert group_report.issues[0].check == "structure.pks_source"
    child_report = check_rule(rules, child, source, target)
    assert [i.check for i in child_report.issues if i.check == "structure.pks_source"] == []
    pko_report = check_rule(rules, pko, source, target)
    assert [i.check for i in pko_report.issues] == []

    missing = load_exchange_rules(
        rules_xml(
            pko_xml(
                "Номенклатура",
                NOMENCLATURE,
                NOMENCLATURE,
                pks_xml("Владелец", "Владелец", "Свойство"),
            )
        )
    )
    owner = missing.pko()[0]
    body = owner.child("Свойства")
    assert body is not None
    alone = check_rule(missing, body.items[0], source, target)
    assert [(i.level, i.check) for i in alone.issues] == [(Level.ERROR, "structure.pko_missing")]


def test_reference_type_without_pko(sides: tuple[sqlite3.Connection, ...]) -> None:
    """Сценарий «Ссылочный тип без ПКО»: ошибка с предложением создать ПКО."""
    xml = rules_xml(
        pko_xml(
            "Номенклатура", NOMENCLATURE, NOMENCLATURE, pks_xml("Владелец", "Владелец", "Свойство")
        )
    )
    issues = only(check(sides, xml), "structure.pko_missing")
    assert [(i.level, i.address) for i in issues] == [
        (Level.ERROR, "ПКО «Номенклатура» / ПКС Владелец")
    ]
    assert "СправочникСсылка.Контрагенты" in issues[0].message
    assert "создайте ПКО" in issues[0].message


def test_reference_set_by_pko_handler_is_warning(sides: tuple[sqlite3.Connection, ...]) -> None:
    """Свойство без ПКО, которое упоминает обработчик ПКО, — предупреждение, а не ошибка."""
    body = pks_xml("Владелец", "Владелец", "Свойство")
    handler = "<ПослеЗагрузки>Объект.Владелец = Справочники.Контрагенты.Основной;</ПослеЗагрузки>"
    xml = rules_xml(pko_xml("Номенклатура", NOMENCLATURE, NOMENCLATURE, body)).replace(
        "<Свойства>".encode(), (handler + "<Свойства>").encode(), 1
    )
    issues = only(check(sides, xml), "structure.pko_missing")
    assert [i.level for i in issues] == [Level.WARNING]
    assert "обработчике ПКО" in issues[0].message


def test_composite_type_with_partial_pko_is_warning(sides: tuple[sqlite3.Connection, ...]) -> None:
    """Составной тип: ПКО есть не для всех ссылочных типов — предупреждение со списком."""
    document = "ДокументСсылка.Приход"
    body = (
        '<Группа><Источник Имя="Остатки" Вид="НаборДвиженийРегистраНакопления"/>'
        '<Приемник Имя="Остатки" Вид="НаборДвиженийРегистраНакопления"/><Свойства/>'
        + pks_xml("Регистратор", "Регистратор", "Свойство")
        + "</Группа>"
    )
    xml = rules_xml(pko_xml("Приход", document, document, body))
    issues = only(check(sides, xml), "structure.pko_missing")
    assert [(i.level, i.address) for i in issues] == [
        (Level.WARNING, "ПКО «Приход» / ПКС Остатки/Регистратор")
    ]
    assert "ДокументСсылка.Расход" in issues[0].message
    assert "ДокументСсылка.Приход," not in issues[0].message


def test_handlers_and_disabled_pks_are_not_type_checked(
    sides: tuple[sqlite3.Connection, ...],
) -> None:
    body = pks_xml(
        "Владелец", "Владелец", "Свойство", "<ПриВыгрузке>Значение = Неопределено;</ПриВыгрузке>"
    ) + pks_xml("Нет", "Нет", attrs=' Отключить="true"')
    report = check(sides, rules_xml(pko_xml("Номенклатура", NOMENCLATURE, NOMENCLATURE, body)))
    assert [(i.level, i.check, i.address) for i in report.issues] == [
        (Level.WARNING, "structure.pko_unreachable", "ПКО «Номенклатура»"),
    ]


def test_primitive_to_reference(sides: tuple[sqlite3.Connection, ...]) -> None:
    """Запрет «примитив → объектная ссылка» (дизайн, Д7)."""
    xml = rules_xml(
        pko_xml("Номенклатура", NOMENCLATURE, NOMENCLATURE, pks_xml("Артикул", "Владелец"))
    )
    issues = only(check(sides, xml), "structure.pks_type")
    assert [(i.level, i.address) for i in issues] == [
        (Level.ERROR, "ПКО «Номенклатура» / ПКС Владелец")
    ]


def test_pko_rule_type_mismatch(sides: tuple[sqlite3.Connection, ...]) -> None:
    """ПКС с правилом конвертации другого типа — предупреждение."""
    body = pks_xml(
        "Владелец",
        "Владелец",
        "Свойство",
        "<КодПравилаКонвертации>Номенклатура</КодПравилаКонвертации>",
    )
    report = check(sides, rules_xml(pko_xml("Номенклатура", NOMENCLATURE, NOMENCLATURE, body)))
    issues = only(report, "structure.pks_type")
    assert [i.level for i in issues] == [Level.WARNING, Level.WARNING]
    assert all("ПКО «Номенклатура»" in i.message for i in issues)


def test_unknown_objects(sides: tuple[sqlite3.Connection, ...]) -> None:
    xml = rules_xml(
        pko_xml("Нет", "СправочникСсылка.Нет", "СправочникСсылка.Бригады"),
        pvd="<Правило><Код>В</Код><ОбъектВыборки>СправочникСсылка.Нет</ОбъектВыборки></Правило>"
        '<Правило Отключить="true"><Код>Выкл</Код>'
        "<ОбъектВыборки>СправочникСсылка.Нет</ОбъектВыборки></Правило>",
        pod="<Правило><Код>О</Код><ОбъектВыборки>СправочникСсылка.Бригады</ОбъектВыборки></Правило>",
    )
    assert [(i.check, i.address) for i in check(sides, xml).issues] == [
        ("structure.pko_source", "ПКО «Нет»"),
        ("structure.pko_target", "ПКО «Нет»"),
        ("structure.pvd_object", "ПВД «В»"),
        ("structure.pod_object", "ПОД «О»"),
        ("structure.pko_unreachable", "ПКО «Нет»"),
    ]


def test_pvd_selection_differs_from_pko(sides: tuple[sqlite3.Connection, ...]) -> None:
    xml = rules_xml(
        CATALOG_PKO,
        pvd="<Правило><Код>В</Код><КодПравилаКонвертации>Номенклатура</КодПравилаКонвертации>"
        "<ОбъектВыборки>СправочникСсылка.Контрагенты</ОбъектВыборки></Правило>",
    )
    issues = check(sides, xml).issues
    assert [(i.level, i.check) for i in issues] == [(Level.WARNING, "structure.pvd_pko")]


ENUM = "ПеречислениеСсылка.Виды"


def pkz_xml(source: str, target: str) -> str:
    return f"<Значение><Источник>{source}</Источник><Приемник>{target}</Приемник></Значение>"


def test_uncovered_enum_values(sides: tuple[sqlite3.Connection, ...]) -> None:
    """Сценарий «Непокрытое значение перечисления»: предупреждение со списком значений."""
    xml = rules_xml(pko_xml("Виды", ENUM, ENUM, values=pkz_xml("Приход", "Приход")))
    issues = check(sides, xml).issues
    assert [(i.level, i.check, i.address) for i in issues] == [
        (Level.WARNING, "structure.pkz_coverage", "ПКО «Виды»"),
        (Level.WARNING, "structure.pko_unreachable", "ПКО «Виды»"),
    ]
    assert issues[0].message.endswith("Корректировка, Расход")


def test_enum_without_pkz_is_not_coverage_issue(sides: tuple[sqlite3.Connection, ...]) -> None:
    """Без ПКЗ соответствие значений не строится (Исп:738) — покрытие не проверяется."""
    issues = check(sides, rules_xml(pko_xml("Виды", ENUM, ENUM))).issues
    assert [(i.level, i.check, i.address) for i in issues] == [
        (Level.WARNING, "structure.pko_unreachable", "ПКО «Виды»"),
    ]


def test_unknown_enum_values(sides: tuple[sqlite3.Connection, ...]) -> None:
    values = (
        pkz_xml("Приход", "Приход")
        + pkz_xml("Расход", "Расход")
        + pkz_xml("Корректировка", "Корректировка")
        + pkz_xml("Нет", "Расход")
    )
    report = check(sides, rules_xml(pko_xml("Виды", ENUM, ENUM, values=values)))
    assert [(i.check, i.address) for i in report.issues] == [
        ("structure.pkz_source", "ПКО «Виды» / ПКЗ Нет"),
        ("structure.pkz_target", "ПКО «Виды» / ПКЗ Корректировка"),
        ("structure.pko_unreachable", "ПКО «Виды»"),
    ]


def test_missing_structure_is_skipped(sides: tuple[sqlite3.Connection, ...]) -> None:
    """Сценарий «Структура приёмника не загружена»: проверки по приёмнику не выполнены."""
    source, _ = sides
    xml = rules_xml(
        pko_xml("Номенклатура", NOMENCLATURE, NOMENCLATURE, pks_xml("КомментарийРасш", "Нет"))
    )
    report = check_structures(load_exchange_rules(xml), source, None)
    assert [(i.level, i.check, i.address) for i in report.issues] == [
        (Level.WARNING, "structure.pko_unreachable", "ПКО «Номенклатура»"),
    ]
    assert [item.check for item in report.skipped] == ["structure.target"]
    assert "результат неполный" in report.summary()


def _pvd(code: str, pko: str, *, disabled: bool = False) -> str:
    flag = ' Отключить="true"' if disabled else ""
    return (
        f"<Правило{flag}><Код>{code}</Код><ОбъектВыборки>{NOMENCLATURE}</ОбъектВыборки>"
        f"<КодПравилаКонвертации>{pko}</КодПравилаКонвертации></Правило>"
    )


def test_unreachable_pko_warns(sides: tuple[sqlite3.Connection, ...]) -> None:
    """ПКО без ПВД и без ссылок из ПКС — одно предупреждение на его адрес."""
    report = check(sides, rules_xml(pko_xml("Лишний", NOMENCLATURE, NOMENCLATURE)))
    assert [(i.level, i.check, i.address, i.message) for i in report.issues] == [
        (
            Level.WARNING,
            "structure.pko_unreachable",
            "ПКО «Лишний»",
            "ПКО «Лишний» не вызывается ни из ПВД, ни из ПКС: проверьте состав плана обмена"
            " (structure_plan_content) и добавьте ПВД или ссылку из ПКС",
        )
    ]


def test_pvd_makes_pko_reachable(sides: tuple[sqlite3.Connection, ...]) -> None:
    xml = rules_xml(pko_xml("Лишний", NOMENCLATURE, NOMENCLATURE), pvd=_pvd("В", "Лишний"))
    assert only(check(sides, xml), "structure.pko_unreachable") == []


def test_pks_of_another_pko_makes_pko_reachable(sides: tuple[sqlite3.Connection, ...]) -> None:
    body = pks_xml(
        "Владелец",
        "Владелец",
        "Свойство",
        "<КодПравилаКонвертации>Лишний</КодПравилаКонвертации>",
    )
    xml = rules_xml(
        pko_xml("Номенклатура", NOMENCLATURE, NOMENCLATURE, body)
        + pko_xml("Лишний", "СправочникСсылка.Контрагенты", "СправочникСсылка.Контрагенты"),
        pvd=_pvd("В", "Номенклатура"),
    )
    assert only(check(sides, xml), "structure.pko_unreachable") == []


def test_pks_group_code_makes_pko_reachable(sides: tuple[sqlite3.Connection, ...]) -> None:
    body = (
        "<Группа><КодПравилаКонвертации>Лишний</КодПравилаКонвертации>"
        '<Источник Имя="Товары" Вид="ТабличнаяЧасть"/>'
        '<Приемник Имя="Товары" Вид="ТабличнаяЧасть"/><Свойства/></Группа>'
    )
    xml = rules_xml(
        pko_xml("Номенклатура", NOMENCLATURE, NOMENCLATURE, body)
        + pko_xml("Лишний", NOMENCLATURE, NOMENCLATURE),
        pvd=_pvd("В", "Номенклатура"),
    )
    assert only(check(sides, xml), "structure.pko_unreachable") == []


def test_empty_source_pko_is_not_unreachable(sides: tuple[sqlite3.Connection, ...]) -> None:
    report = check(sides, rules_xml(pko_xml("ИзОбработчика", "", NOMENCLATURE)))
    assert only(report, "structure.pko_unreachable") == []


def test_disabled_pko_is_not_unreachable(sides: tuple[sqlite3.Connection, ...]) -> None:
    xml = rules_xml(
        f'<Правило Отключить="true"><Код>Лишний</Код><Источник>{NOMENCLATURE}</Источник>'
        f"<Приемник>{NOMENCLATURE}</Приемник></Правило>"
    )
    assert only(check(sides, xml), "structure.pko_unreachable") == []


def test_disabled_pvd_does_not_make_pko_reachable(sides: tuple[sqlite3.Connection, ...]) -> None:
    xml = rules_xml(
        pko_xml("Лишний", NOMENCLATURE, NOMENCLATURE),
        pvd=_pvd("В", "Лишний", disabled=True),
    )
    issues = only(check(sides, xml), "structure.pko_unreachable")
    assert [i.address for i in issues] == ["ПКО «Лишний»"]


def test_handler_mention_makes_pko_reachable(sides: tuple[sqlite3.Connection, ...]) -> None:
    carrier = pko_xml("Номенклатура", NOMENCLATURE, NOMENCLATURE).replace(
        "<Свойства>",
        '<ПередВыгрузкой>ИмяПКО = "Лишний";</ПередВыгрузкой><Свойства>',
        1,
    )
    xml = rules_xml(
        carrier + pko_xml("Лишний", "СправочникСсылка.Контрагенты", "СправочникСсылка.Контрагенты"),
        pvd=_pvd("В", "Номенклатура"),
    )
    assert only(check(sides, xml), "structure.pko_unreachable") == []


def test_algorithm_mention_makes_pko_reachable(sides: tuple[sqlite3.Connection, ...]) -> None:
    pko = pko_xml("Лишний", NOMENCLATURE, NOMENCLATURE)
    xml = (
        "<ПравилаОбмена><ВерсияФормата>2.01</ВерсияФормата>"
        f"<ПравилаКонвертацииОбъектов>{pko}</ПравилаКонвертацииОбъектов>"
        '<Алгоритмы><Алгоритм Имя="Вызов">'
        "<Текст>ВыгрузитьПоПравилу(Лишний);</Текст></Алгоритм></Алгоритмы>"
        "</ПравилаОбмена>"
    ).encode()
    assert only(check(sides, xml), "structure.pko_unreachable") == []


def test_unreachable_pko_does_not_need_structures() -> None:
    """Проверка по самим правилам: без структур не пропускается."""
    report = check_structures(
        load_exchange_rules(rules_xml(pko_xml("Лишний", NOMENCLATURE, NOMENCLATURE))),
        None,
        None,
    )
    assert [i.check for i in report.issues] == ["structure.pko_unreachable"]
    assert "structure.pko_unreachable" not in {item.check for item in report.skipped}
