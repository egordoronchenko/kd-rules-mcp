"""Предупреждения обмена через план: синтетическое нарушение, чистый пример и пропуск."""

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from kd2_rules_mcp.kd2.rules_io import load_exchange_rules
from kd2_rules_mcp.structures.db import SCHEMA
from kd2_rules_mcp.structures.store import StructureStore
from kd2_rules_mcp.validation.exchange_plan import (
    DOCUMENT_POSTING,
    ENUM_PKZ,
    EXPORT_KEY,
    INCOMING_KEY,
    OBJECT_WRITE,
    PREDEFINED_PKZ,
    PVD_ARBITRARY,
    PVD_SELECTION,
    SOURCE_NAME,
    SOURCE_VERSION,
    check_exchange_plan,
)
from kd2_rules_mcp.validation.report import Issue, ValidationReport

DUMP = Path(__file__).parent / "data" / "xmldump"
ENUM = "ПеречислениеСсылка.Виды"
CHART = "ПланСчетовСсылка.Хозрасчетный"
DOC = "ДокументСсылка.Приход"
NOMENCLATURE = "СправочникСсылка.Номенклатура"
PREDEFINED = "Материалы, ОСвОрганизации, ОсновныеСредства"

ENUM_MESSAGE = (
    "У ПКО перечисления нет правил конвертации значений: сопоставление пропускается, "
    "имя значения в сообщение не пишется — в приёмнике значение пустое"
)
PREDEFINED_MESSAGE = (
    f"Одноимённые предопределённые без ПКЗ ({PREDEFINED}): имя предопределённого в узел "
    "«Ссылка» не пишется, приёмник не ищет по нему и создаёт новый элемент"
)
POSTING_MESSAGE = (
    "У ПКО документа нет включённого ПКС «Проведен»: новый документ записывается "
    "непроведённым, найденный сохраняет состояние приёмника"
)
WRITE_MESSAGE = (
    "В «ПослеЗагрузки» есть «Объект.Записать(»: запись идёт до установки режима "
    "загрузки, проведение выполнится дважды или прервёт загрузку"
)
CONVERSION_WRITE = (
    "В «ПослеЗагрузкиДанных» есть «Объект.Записать(»: запись идёт до установки режима "
    "загрузки, проведение выполнится дважды или прервёт загрузку"
)
MANUAL = (
    "Правило рассчитано на ручной обмен универсальной обработкой, "
    "в обмене через план обмена эта часть не исполняется"
)
INCOMING_MESSAGE = (
    "ПКС «ПолучитьИзВходящихДанных» читает «ВходящиеДанные[Артикул]», а этого ключа "
    "нет в обработчиках до чтения свойства: в протокол пишется ошибка, и при выключенном "
    "«продолжать при ошибке» запись сообщения прерывается"
)


@pytest.fixture(scope="module")
def sides(tmp_path_factory: pytest.TempPathFactory) -> Iterator[tuple[sqlite3.Connection, ...]]:
    store = StructureStore(tmp_path_factory.mktemp("cache"))
    store.load_xml("source", DUMP / "main", [DUMP / "ext"])
    store.load_xml("target", DUMP / "main")
    source, target = store.open("source"), store.open("target")
    yield source, target
    source.close()
    target.close()


def exchange(
    pko: str = "",
    pvd: str = "",
    *,
    source: str = "Тестовая",
    source_version: str = "1.0.0.1",
    target: str = "Другая",
    target_version: str = "9.9.9.9",
    events: str = "",
    algorithms: str = "",
) -> bytes:
    return f"""<ПравилаОбмена>
<ВерсияФормата>2.01</ВерсияФормата>
<Источник ВерсияКонфигурации="{source_version}">{source}</Источник>
<Приемник ВерсияКонфигурации="{target_version}">{target}</Приемник>
{events}
<ПравилаКонвертацииОбъектов>{pko}</ПравилаКонвертацииОбъектов>
<ПравилаВыгрузкиДанных>{pvd}</ПравилаВыгрузкиДанных>
<Алгоритмы>{algorithms}</Алгоритмы>
</ПравилаОбмена>""".encode()


def pko(
    code: str, source: str, target: str, body: str = "", extra: str = "", attrs: str = ""
) -> str:
    return (
        f"<Правило{attrs}><Код>{code}</Код><Источник>{source}</Источник>"
        f"<Приемник>{target}</Приемник>{extra}<Свойства>{body}</Свойства></Правило>"
    )


def pks(source: str, target: str, extra: str = "", attrs: str = "") -> str:
    return (
        f'<Свойство{attrs}><Источник Имя="{source}" Вид="Реквизит"/>'
        f'<Приемник Имя="{target}" Вид="Реквизит"/>{extra}</Свойство>'
    )


def pkz(source: str, target: str) -> str:
    return f"<Значение><Источник>{source}</Источник><Приемник>{target}</Приемник></Значение>"


def run(
    xml: bytes,
    source: sqlite3.Connection | None,
    target: sqlite3.Connection | None,
) -> ValidationReport:
    return check_exchange_plan(load_exchange_rules(xml), source, target)


def hits(report: ValidationReport, check: str) -> list[Issue]:
    return [issue for issue in report.issues if issue.check == check]


def skips(report: ValidationReport, check: str) -> list[str]:
    return [item.reason for item in report.skipped if item.check == check]


def test_clean_rules_have_no_exchange_plan_issues(
    sides: tuple[sqlite3.Connection, ...],
) -> None:
    """Заголовок совпал, ПКЗ есть, у документа включён ПКС «Проведен»."""
    values = (
        pkz("Приход", "Приход") + pkz("Расход", "Расход") + pkz("Корректировка", "Корректировка")
    )
    accounts = (
        pkz("ОсновныеСредства", "ОсновныеСредства")
        + pkz("ОСвОрганизации", "ОСвОрганизации")
        + pkz("Материалы", "Материалы")
    )
    xml = exchange(
        pko("Виды", ENUM, ENUM, extra=f"<Значения>{values}</Значения>")
        + pko("Счета", CHART, CHART, extra=f"<Значения>{accounts}</Значения>")
        + pko("Приход", DOC, DOC, pks("Проведен", "Проведен"))
        + pko("Номенклатура", NOMENCLATURE, NOMENCLATURE, pks("Артикул", "Артикул")),
        "<Правило><Код>В</Код><СпособОтбораДанных>СтандартнаяВыборка</СпособОтбораДанных>"
        f"<ОбъектВыборки>{DOC}</ОбъектВыборки></Правило>",
    )
    report = run(xml, sides[0], sides[1])
    assert report.issues == []
    assert report.skipped == []


def test_enum_without_pkz_warns_and_one_pkz_is_enough(
    sides: tuple[sqlite3.Connection, ...],
) -> None:
    source, target = sides
    warned = run(exchange(pko("Виды", ENUM, ENUM)), source, target)
    assert [(issue.address, issue.message) for issue in hits(warned, ENUM_PKZ)] == [
        ("ПКО «Виды»", ENUM_MESSAGE)
    ]
    covered = run(
        exchange(pko("Виды", ENUM, ENUM, extra=f"<Значения>{pkz('Приход', 'Приход')}</Значения>")),
        source,
        target,
    )
    assert hits(covered, ENUM_PKZ) == []


def test_enum_and_predefined_without_structures_are_skipped() -> None:
    xml = exchange(pko("Виды", ENUM, ENUM) + pko("Счета", CHART, CHART))
    report = run(xml, None, None)
    assert skips(report, ENUM_PKZ) == ["нет структуры источника — не видно, перечисление ли ПКО"]
    assert skips(report, PREDEFINED_PKZ) == [
        "нет структуры источника или приёмника — предопределённые обеих сторон не с чем сверить"
    ]
    assert hits(report, ENUM_PKZ) == []
    assert hits(report, PREDEFINED_PKZ) == []


def test_predefined_without_pkz_warns_until_search_by_name(
    sides: tuple[sqlite3.Connection, ...],
) -> None:
    source, target = sides
    bare = run(exchange(pko("Счета", CHART, CHART)), source, target)
    assert [(issue.address, issue.message) for issue in hits(bare, PREDEFINED_PKZ)] == [
        ("ПКО «Счета»", PREDEFINED_MESSAGE)
    ]
    search = pks("ИмяПредопределенныхДанных", "ИмяПредопределенныхДанных", attrs=' Поиск="true"')
    found = run(exchange(pko("Счета", CHART, CHART, search)), source, target)
    assert hits(found, PREDEFINED_PKZ) == []
    synced = "<СинхронизироватьПоИдентификатору>true</СинхронизироватьПоИдентификатору>"
    # Обе структуры — «Тестовая»: GUID предопределённых совпадают, шаг 3 находит элемент.
    same_config = run(exchange(pko("Счета", CHART, CHART, search, extra=synced)), source, target)
    assert hits(same_config, PREDEFINED_PKZ) == []
    renamed = sqlite3.connect(":memory:")
    source.backup(renamed)
    renamed.execute("UPDATE meta SET value = 'Другая' WHERE key = 'config_name'")
    # ПВД выгружает объект целиком: шаг 3 по GUID создаёт элемент и поиск по полям не выполняется.
    whole = "<Правило><Код>В</Код><КодПравилаКонвертации>Счета</КодПравилаКонвертации></Правило>"
    by_guid = run(
        exchange(pko("Счета", CHART, CHART, search, extra=synced), pvd=whole), source, renamed
    )
    assert [(issue.address, issue.message) for issue in hits(by_guid, PREDEFINED_PKZ)] == [
        ("ПКО «Счета»", PREDEFINED_MESSAGE)
    ]
    continued = synced + (
        "<ПродолжитьПоискПоПолямПоискаЕслиПоИдентификаторуНеНашли>true"
        "</ПродолжитьПоискПоПолямПоискаЕслиПоИдентификаторуНеНашли>"
    )
    again = run(exchange(pko("Счета", CHART, CHART, search, extra=continued)), source, target)
    assert hits(again, PREDEFINED_PKZ) == []
    # Ссылка с GUID и «не создавать» не порождает элемент, даже если имени в «Ссылка» нет.
    kept = synced + "<НеСоздаватьЕслиНеНайден>true</НеСоздаватьЕслиНеНайден>"
    kept_rules = exchange(pko("Счета", CHART, CHART, extra=kept), pvd=whole)
    assert hits(run(kept_rules, source, renamed), PREDEFINED_PKZ) == []
    renamed.close()


def test_opaque_search_handler_is_skipped(sides: tuple[sqlite3.Connection, ...]) -> None:
    extra = "<ПоследовательностьПолейПоиска>Выполнить(КодПоиска);</ПоследовательностьПолейПоиска>"
    report = run(exchange(pko("Счета", CHART, CHART, extra=extra)), sides[0], sides[1])
    assert hits(report, PREDEFINED_PKZ) == []
    assert skips(report, PREDEFINED_PKZ) == [
        "ПКО «Счета»: обработчик поиска непрозрачен — не видно, "
        "ищет ли он по имени предопределённого"
    ]


def test_document_without_posted_warns_until_handler_sets_mode(
    sides: tuple[sqlite3.Connection, ...],
) -> None:
    source, target = sides
    warned = run(exchange(pko("Приход", DOC, DOC)), source, target)
    assert [(issue.address, issue.message) for issue in hits(warned, DOCUMENT_POSTING)] == [
        ("ПКО «Приход»", POSTING_MESSAGE)
    ]
    with_pks = run(exchange(pko("Приход", DOC, DOC, pks("Проведен", "Проведен"))), source, target)
    assert hits(with_pks, DOCUMENT_POSTING) == []
    after = "<ПослеЗагрузки>Объект.Проведен = Истина;</ПослеЗагрузки>"
    by_handler = run(exchange(pko("Приход", DOC, DOC, extra=after)), source, target)
    assert hits(by_handler, DOCUMENT_POSTING) == []
    before = '<ПередВыгрузкой>РежимЗаписи = "Проведение";</ПередВыгрузкой>'
    by_export = run(exchange(pko("Приход", DOC, DOC, extra=before)), source, target)
    assert hits(by_export, DOCUMENT_POSTING) == []
    empty_mode = '<ПередЗагрузкой>РежимЗаписи = "";</ПередЗагрузкой>'
    still = run(exchange(pko("Приход", DOC, DOC, extra=empty_mode)), source, target)
    assert hits(still, DOCUMENT_POSTING) == [
        Issue(
            level=hits(still, DOCUMENT_POSTING)[0].level,
            check=DOCUMENT_POSTING,
            address="ПКО «Приход»",
            message=POSTING_MESSAGE,
        )
    ]
    on_load = "<ПриЗагрузке>РежимЗаписи = РежимЗаписиДокумента.Проведение;</ПриЗагрузке>"
    loaded = run(exchange(pko("Приход", DOC, DOC, extra=on_load)), source, target)
    assert hits(loaded, DOCUMENT_POSTING) == []
    # Атрибут «РежимЗаписи» пишется до «ПриВыгрузке», присваивание в нём режим уже не меняет.
    late = '<ПриВыгрузке>РежимЗаписи = "Проведение";</ПриВыгрузке>'
    late_hits = hits(
        run(exchange(pko("Приход", DOC, DOC, extra=late)), source, target), DOCUMENT_POSTING
    )
    assert [(issue.address, issue.message) for issue in late_hits] == [
        ("ПКО «Приход»", POSTING_MESSAGE)
    ]


def test_opaque_posting_handler_is_skipped(sides: tuple[sqlite3.Connection, ...]) -> None:
    extra = "<ПослеЗагрузки>Выполнить(КодЗаписи);</ПослеЗагрузки>"
    report = run(exchange(pko("Приход", DOC, DOC, extra=extra)), sides[0], sides[1])
    assert hits(report, DOCUMENT_POSTING) == []
    assert skips(report, DOCUMENT_POSTING) == [
        "ПКО «Приход»: обработчик непрозрачен — не видно, задаёт ли он режим записи или «Проведен»"
    ]
    no_target = run(exchange(pko("Приход", DOC, DOC)), sides[0], None)
    assert skips(no_target, DOCUMENT_POSTING) == [
        "нет структуры приёмника — не видно, проводится ли документ"
    ]


def test_object_write_warns_only_on_direct_call(sides: tuple[sqlite3.Connection, ...]) -> None:
    source, target = sides
    direct = "<ПослеЗагрузки>Объект.Записать(РежимЗаписиДокумента.Проведение);</ПослеЗагрузки>"
    warned = run(exchange(pko("Приход", DOC, DOC, extra=direct)), source, target)
    assert [(issue.address, issue.message) for issue in hits(warned, OBJECT_WRITE)] == [
        ("ПКО «Приход»", WRITE_MESSAGE)
    ]
    other = "<ПослеЗагрузки>ДругойОбъект.Записать();</ПослеЗагрузки>"
    assert (
        hits(run(exchange(pko("Приход", DOC, DOC, extra=other)), source, target), OBJECT_WRITE)
        == []
    )
    commented = "<ПослеЗагрузки>// Объект.Записать();\nА = 1;</ПослеЗагрузки>"
    assert (
        hits(run(exchange(pko("Приход", DOC, DOC, extra=commented)), source, target), OBJECT_WRITE)
        == []
    )
    disabled = run(
        exchange(pko("Приход", DOC, DOC, extra=direct, attrs=' Отключить="true"')),
        source,
        target,
    )
    assert hits(disabled, OBJECT_WRITE) == []
    conversion = run(
        exchange(events="<ПослеЗагрузкиДанных>Объект.Записать();</ПослеЗагрузкиДанных>"),
        source,
        target,
    )
    assert [(issue.address, issue.message) for issue in hits(conversion, OBJECT_WRITE)] == [
        ("Конвертация", CONVERSION_WRITE)
    ]


def test_manual_exchange_forms_warn_and_standard_selection_does_not(
    sides: tuple[sqlite3.Connection, ...],
) -> None:
    source, target = sides
    pvd = (
        "<Правило><Код>В</Код><СпособОтбораДанных>ПроизвольныйАлгоритм</СпособОтбораДанных>"
        "<ПередОбработкойПравила>ВыборкаДанных = Создать();</ПередОбработкойПравила></Правило>"
    )
    warned = run(exchange(pvd=pvd), source, target)
    assert [(issue.check, issue.address, issue.message) for issue in warned.issues] == [
        (PVD_ARBITRARY, "ПВД «В»", f"{MANUAL}: способ отбора «ПроизвольныйАлгоритм»"),
        (PVD_SELECTION, "ПВД «В»", f"{MANUAL}: «ВыборкаДанных» в «ПередОбработкой»"),
    ]
    standard = (
        "<Правило><Код>В</Код><СпособОтбораДанных>СтандартнаяВыборка</СпособОтбораДанных>"
        "<ПередОбработкойПравила>Отказ = Истина;</ПередОбработкойПравила></Правило>"
    )
    assert run(exchange(pvd=standard), source, target).issues == []
    opaque = (
        "<Правило><Код>В</Код><ПередОбработкойПравила>Выполнить(КодОтбора);</ПередОбработкойПравила>"
        "</Правило>"
    )
    skipped = run(exchange(pvd=opaque), source, target)
    assert hits(skipped, PVD_SELECTION) == []
    assert skips(skipped, PVD_SELECTION) == [
        "ПВД «В»: «ПередОбработкой» непрозрачен — не видно, задаёт ли обработчик «ВыборкаДанных»"
    ]


def test_export_key_without_remembering_warns(sides: tuple[sqlite3.Connection, ...]) -> None:
    source, target = sides
    key = "<ПередВыгрузкой>КлючВыгружаемыхДанных = Ссылка;</ПередВыгрузкой>"
    warned = run(
        exchange(pko("Номенклатура", NOMENCLATURE, NOMENCLATURE, extra=key)), source, target
    )
    assert [(issue.address, issue.message) for issue in hits(warned, EXPORT_KEY)] == [
        (
            "ПКО «Номенклатура»",
            f"{MANUAL}: «КлючВыгружаемыхДанных» без запоминания выгруженных",
        )
    ]
    remembered = (
        "<ПередВыгрузкой>КлючВыгружаемыхДанных = Ссылка;\n"
        "ЗапоминатьВыгруженные = Истина;</ПередВыгрузкой>"
    )
    clean = run(
        exchange(pko("Номенклатура", NOMENCLATURE, NOMENCLATURE, extra=remembered)),
        source,
        target,
    )
    assert hits(clean, EXPORT_KEY) == []
    flag_only = (
        "<Правило><Код>Н</Код><НеЗапоминатьВыгруженные>true</НеЗапоминатьВыгруженные></Правило>"
    )
    assert hits(run(exchange(flag_only), source, target), EXPORT_KEY) == []
    opaque = "<ПередВыгрузкой>КлючВыгружаемыхДанных = Ссылка;\nВыполнить(Код);</ПередВыгрузкой>"
    skipped = run(
        exchange(pko("Номенклатура", NOMENCLATURE, NOMENCLATURE, extra=opaque)),
        source,
        target,
    )
    assert hits(skipped, EXPORT_KEY) == []
    assert skips(skipped, EXPORT_KEY) == [
        "ПКО «Номенклатура»: «ПередВыгрузкой» непрозрачен — не видно, "
        "включает ли обработчик запоминание выгруженных"
    ]


def test_incoming_key_missing_warns_until_handler_fills_it(
    sides: tuple[sqlite3.Connection, ...],
) -> None:
    source, target = sides
    body = pks("Артикул", "Артикул", "<ПолучитьИзВходящихДанных>true</ПолучитьИзВходящихДанных>")
    export = (
        "<Правило><Код>В</Код><КодПравилаКонвертации>Номенклатура</КодПравилаКонвертации></Правило>"
    )
    warned = run(
        exchange(pko("Номенклатура", NOMENCLATURE, NOMENCLATURE, body), pvd=export),
        source,
        target,
    )
    assert [(issue.address, issue.message) for issue in hits(warned, INCOMING_KEY)] == [
        ("ПКО «Номенклатура» / ПКС Артикул", INCOMING_MESSAGE)
    ]
    filled = '<ПередВыгрузкой>ВходящиеДанные.Вставить("Артикул", 1);</ПередВыгрузкой>'
    on_export = (
        '<ПриВыгрузке>ВходящиеДанные = Новый Структура("Артикул");\n'
        "ВходящиеДанные.Артикул = 1;</ПриВыгрузке>"
    )
    assert (
        hits(
            run(
                exchange(pko("Номенклатура", NOMENCLATURE, NOMENCLATURE, body, extra=on_export)),
                source,
                target,
            ),
            INCOMING_KEY,
        )
        == []
    )
    search_body = pks(
        "Артикул",
        "Артикул",
        "<ПолучитьИзВходящихДанных>true</ПолучитьИзВходящихДанных>",
        attrs=' Поиск="true"',
    )
    search_hits = hits(
        run(
            exchange(pko("Номенклатура", NOMENCLATURE, NOMENCLATURE, search_body, extra=on_export)),
            source,
            target,
        ),
        INCOMING_KEY,
    )
    assert [(issue.address, issue.message) for issue in search_hits] == [
        ("ПКО «Номенклатура» / ПКС Артикул", INCOMING_MESSAGE)
    ]
    called = (
        '<ПередВыгрузкой>ВыгрузитьПоПравилу(Объект,, ДанныеУЦ,, "Номенклатура");</ПередВыгрузкой>'
    )
    called_report = run(
        exchange(pko("Номенклатура", NOMENCLATURE, NOMENCLATURE, body, extra=called)),
        source,
        target,
    )
    assert hits(called_report, INCOMING_KEY) == []
    assert skips(called_report, INCOMING_KEY) == [
        "ПКО «Номенклатура» / ПКС Артикул: ПКО вызывается из обработчика с входящими данными, "
        "и не видно, передаётся ли ключ «Артикул»"
    ]
    omitted = '<ПередВыгрузкой>ВыгрузитьПоПравилу(Объект,,,, "Номенклатура");</ПередВыгрузкой>'
    omitted_hits = hits(
        run(
            exchange(pko("Номенклатура", NOMENCLATURE, NOMENCLATURE, body, extra=omitted)),
            source,
            target,
        ),
        INCOMING_KEY,
    )
    assert [(issue.address, issue.message) for issue in omitted_hits] == [
        ("ПКО «Номенклатура» / ПКС Артикул", INCOMING_MESSAGE)
    ]
    clean = run(
        exchange(pko("Номенклатура", NOMENCLATURE, NOMENCLATURE, body, extra=filled)),
        source,
        target,
    )
    assert hits(clean, INCOMING_KEY) == []
    own = body.replace(
        "</Свойство>",
        "<ПередВыгрузкой>Значение = 1;</ПередВыгрузкой></Свойство>",
    )
    assert (
        hits(
            run(exchange(pko("Номенклатура", NOMENCLATURE, NOMENCLATURE, own)), source, target),
            INCOMING_KEY,
        )
        == []
    )
    dynamic = "<ПередВыгрузкой>ВходящиеДанные.Вставить(Имя, 1);</ПередВыгрузкой>"
    skipped = run(
        exchange(pko("Номенклатура", NOMENCLATURE, NOMENCLATURE, body, extra=dynamic), pvd=export),
        source,
        target,
    )
    assert hits(skipped, INCOMING_KEY) == []
    assert skips(skipped, INCOMING_KEY) == [
        "ПКО «Номенклатура» / ПКС Артикул: заполнение входящих данных непрозрачно — "
        "ключ «Артикул» может вычисляться"
    ]
    via_execute = (
        '<ПослеВыгрузки>СтруктураФИО = Новый Структура("Артикул", 1);\n'
        'Выполнить("ВыгрузитьПоПравилу(Неопределено,,СтруктураФИО,, ""Номенклатура"")");'
        "</ПослеВыгрузки>"
    )
    assert (
        hits(
            run(
                exchange(pko("Номенклатура", NOMENCLATURE, NOMENCLATURE, body, extra=via_execute)),
                source,
                target,
            ),
            INCOMING_KEY,
        )
        == []
    )
    referenced = pko(
        "Шапка",
        NOMENCLATURE,
        NOMENCLATURE,
        pks(
            "Номенклатура",
            "Номенклатура",
            "<КодПравилаКонвертации>Номенклатура</КодПравилаКонвертации>",
        ),
    )
    only_link = pko("Номенклатура", NOMENCLATURE, NOMENCLATURE, body)
    assert hits(run(exchange(referenced + only_link), source, target), INCOMING_KEY) == []
    disabled = pks(
        "Артикул",
        "Артикул",
        "<ПолучитьИзВходящихДанных>true</ПолучитьИзВходящихДанных>",
        attrs=' Отключить="true"',
    )
    assert (
        hits(
            run(
                exchange(pko("Номенклатура", NOMENCLATURE, NOMENCLATURE, disabled)), source, target
            ),
            INCOMING_KEY,
        )
        == []
    )


def test_header_name_and_version_against_source_structure(
    sides: tuple[sqlite3.Connection, ...],
) -> None:
    source, target = sides
    matched = run(exchange(source="тестовая", source_version="1.0.0.99"), source, target)
    assert hits(matched, SOURCE_NAME) == []
    assert hits(matched, SOURCE_VERSION) == []
    renamed = run(exchange(source="Чужая"), source, target)
    assert [(issue.address, issue.message) for issue in hits(renamed, SOURCE_NAME)] == [
        (
            "Конвертация",
            "Имя конфигурации в «Источник» («Чужая») не совпадает с именем "
            "структуры источника («Тестовая»): форма загрузки комплекта отклонит правила",
        )
    ]
    assert hits(renamed, SOURCE_VERSION) == []
    shifted = run(exchange(source_version="2.0.0.1"), source, target)
    assert [(issue.address, issue.message) for issue in hits(shifted, SOURCE_VERSION)] == [
        (
            "Конвертация",
            "Версия конфигурации в «Источник» (2.0.0) отличается от версии "
            "структуры источника (1.0.0) первыми тремя числами: загрузка комплекта "
            "предупредит о несоответствии",
        )
    ]
    receiver = run(exchange(target="Тестовая", target_version="1.0.0.1"), source, target)
    assert hits(receiver, SOURCE_NAME) == []
    assert hits(receiver, SOURCE_VERSION) == []
    missing = run(exchange(), None, target)
    assert hits(missing, SOURCE_NAME) == []
    assert skips(missing, SOURCE_NAME) == [
        "нет структуры источника — имя конфигурации в заголовке не с чем сверить"
    ]
    assert skips(missing, SOURCE_VERSION) == [
        "нет структуры источника — версию конфигурации в заголовке не с чем сверить"
    ]


def test_basic_suffix_is_stripped_only_from_structure_name() -> None:
    connection = sqlite3.connect(":memory:")
    connection.executescript(SCHEMA)
    connection.executemany(
        "INSERT INTO meta (key, value) VALUES (?, ?)",
        (("config_name", "ТестоваяБазовая"), ("config_version", "01.0.0.5")),
    )
    matched = run(exchange(source="Тестовая", source_version="1.0.0.1"), connection, connection)
    assert hits(matched, SOURCE_NAME) == []
    assert hits(matched, SOURCE_VERSION) == []
    kept = run(exchange(source="ТестоваяБазовая"), connection, connection)
    assert hits(kept, SOURCE_NAME)[0].address == "Конвертация"
    assert "«ТестоваяБазовая»" in hits(kept, SOURCE_NAME)[0].message
    empty = run(exchange(source_version=""), connection, connection)
    assert hits(empty, SOURCE_VERSION)[0].message.startswith(
        "Версия конфигурации в «Источник» (0.0.0) отличается"
    )
    broken = run(exchange(source_version="1.0"), connection, connection)
    assert hits(broken, SOURCE_VERSION) == []
    assert skips(broken, SOURCE_VERSION) == [
        "версия конфигурации в заголовке или в структуре не разбирается на три числа"
    ]
    connection.close()
