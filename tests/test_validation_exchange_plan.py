"""Предупреждения обмена через план: синтетическое нарушение, чистый пример и пропуск."""

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from kd2_rules_mcp.kd2.rules_io import load_exchange_rules
from kd2_rules_mcp.structures.db import SCHEMA
from kd2_rules_mcp.structures.store import StructureStore
from kd2_rules_mcp.validation.exchange_plan import (
    AMBIGUOUS_PKO,
    ATTACHED,
    DOCUMENT_POSTING,
    ENUM_PKZ,
    EXPORT_CACHE,
    EXPORT_KEY,
    INCOMING_KEY,
    MODIFIED_FLAG,
    MULTIPLE_PVD,
    OBJECT_WRITE,
    PREDEFINED_PKZ,
    PVD_ARBITRARY,
    PVD_REFUSAL,
    PVD_SELECTION,
    REPEATED_TABLE,
    SOURCE_NAME,
    SOURCE_VERSION,
    TABLE_NO_CLEAR,
    check_exchange_plan,
    plan_name_from_path,
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
    "загрузки, запись выполнится дважды или прервёт загрузку"
)
CATALOG_WRITE = (
    "В «ПослеЗагрузки» есть «Объект.Записать(»: запись идёт до установки режима "
    "загрузки, запись выполнится дважды или прервёт загрузку"
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
    processors: str = "",
) -> bytes:
    return f"""<ПравилаОбмена>
<ВерсияФормата>2.01</ВерсияФормата>
<Источник ВерсияКонфигурации="{source_version}">{source}</Источник>
<Приемник ВерсияКонфигурации="{target_version}">{target}</Приемник>
{events}
{processors}
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


def pks_group(
    source: str, target: str, body: str = "", extra: str = "", kind: str = "ТабличнаяЧасть"
) -> str:
    return (
        f'<Группа><Источник Имя="{source}" Вид="{kind}"/>'
        f'<Приемник Имя="{target}" Вид="{kind}"/>{extra}{body}</Группа>'
    )


def pkz(source: str, target: str) -> str:
    return f"<Значение><Источник>{source}</Источник><Приемник>{target}</Приемник></Значение>"


# ПВД делает ПКО достижимым: без него проверка проведения не выполняется.
_PVD_INCOME = "<Правило><Код>В</Код><КодПравилаКонвертации>Приход</КодПравилаКонвертации></Правило>"


def income(body: str = "", extra: str = "", attrs: str = "") -> bytes:
    return exchange(pko("Приход", DOC, DOC, body, extra, attrs), _PVD_INCOME)


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
    warned = run(income(), source, target)
    assert [(issue.address, issue.message) for issue in hits(warned, DOCUMENT_POSTING)] == [
        ("ПКО «Приход»", POSTING_MESSAGE)
    ]
    with_pks = run(income(pks("Проведен", "Проведен")), source, target)
    assert hits(with_pks, DOCUMENT_POSTING) == []
    after = "<ПослеЗагрузки>Объект.Проведен = Истина;</ПослеЗагрузки>"
    by_handler = run(income(extra=after), source, target)
    assert hits(by_handler, DOCUMENT_POSTING) == []
    before = '<ПередВыгрузкой>РежимЗаписи = "Проведение";</ПередВыгрузкой>'
    by_export = run(income(extra=before), source, target)
    assert hits(by_export, DOCUMENT_POSTING) == []
    empty_mode = '<ПередЗагрузкой>РежимЗаписи = "";</ПередЗагрузкой>'
    still = run(income(extra=empty_mode), source, target)
    assert hits(still, DOCUMENT_POSTING) == [
        Issue(
            level=hits(still, DOCUMENT_POSTING)[0].level,
            check=DOCUMENT_POSTING,
            address="ПКО «Приход»",
            message=POSTING_MESSAGE,
        )
    ]
    on_load = "<ПриЗагрузке>РежимЗаписи = РежимЗаписиДокумента.Проведение;</ПриЗагрузке>"
    loaded = run(income(extra=on_load), source, target)
    assert hits(loaded, DOCUMENT_POSTING) == []
    # Атрибут «РежимЗаписи» пишется до «ПриВыгрузке», присваивание в нём режим уже не меняет.
    late = '<ПриВыгрузке>РежимЗаписи = "Проведение";</ПриВыгрузке>'
    late_hits = hits(run(income(extra=late), source, target), DOCUMENT_POSTING)
    assert [(issue.address, issue.message) for issue in late_hits] == [
        ("ПКО «Приход»", POSTING_MESSAGE)
    ]


def test_opaque_posting_handler_is_skipped(sides: tuple[sqlite3.Connection, ...]) -> None:
    extra = "<ПослеЗагрузки>Выполнить(КодЗаписи);</ПослеЗагрузки>"
    report = run(income(extra=extra), sides[0], sides[1])
    assert hits(report, DOCUMENT_POSTING) == []
    assert skips(report, DOCUMENT_POSTING) == [
        "ПКО «Приход»: обработчик «ПослеЗагрузки» непрозрачен — не видно, "
        "задаёт ли он режим записи или «Проведен»"
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
    catalog = "<ПослеЗагрузки>Объект.Записать();</ПослеЗагрузки>"
    catalog_rules = exchange(pko("Номенклатура", NOMENCLATURE, NOMENCLATURE, extra=catalog))
    catalog_hits = hits(run(catalog_rules, source, target), OBJECT_WRITE)
    assert [(issue.address, issue.message) for issue in catalog_hits] == [
        ("ПКО «Номенклатура»", CATALOG_WRITE)
    ]


def test_unreachable_document_skips_posting_check(
    sides: tuple[sqlite3.Connection, ...],
) -> None:
    """ПКО без ПВД и без ссылок — structure.pko_unreachable, проведение не проверяется."""
    report = run(exchange(pko("Приход", DOC, DOC)), sides[0], sides[1])
    assert hits(report, DOCUMENT_POSTING) == []
    assert skips(report, DOCUMENT_POSTING) == []


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
        f"<ОбъектВыборки>{DOC}</ОбъектВыборки>"
        "<ПередОбработкойПравила>Отказ = Истина;</ПередОбработкойПравила></Правило>"
    )
    refusal = run(exchange(pvd=standard), source, target)
    assert [(issue.check, issue.address) for issue in refusal.issues] == [(PVD_REFUSAL, "ПВД «В»")]
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


def test_export_cache_without_key_warns_and_does_not_overlap_export_key(
    sides: tuple[sqlite3.Connection, ...],
) -> None:
    """Включение кэша без ключа — отдельное замечание; оба присваивания не дают ни одного."""
    source, target = sides
    remember = "<ПередВыгрузкой>ЗапоминатьВыгруженные = Истина;</ПередВыгрузкой>"
    warned = run(
        exchange(pko("Номенклатура", NOMENCLATURE, NOMENCLATURE, extra=remember)),
        source,
        target,
    )
    assert [(issue.address, issue.message) for issue in hits(warned, EXPORT_CACHE)] == [
        (
            "ПКО «Номенклатура»",
            "«ПередВыгрузкой» включает запоминание выгруженных и не задаёт "
            "«КлючВыгружаемыхДанных». В обычном входе ключ остаётся именем ПКО, "
            "поэтому разные объекты принимаются за один",
        )
    ]
    assert hits(warned, EXPORT_KEY) == []
    both = (
        "<ПередВыгрузкой>ЗапоминатьВыгруженные = Истина;\n"
        "КлючВыгружаемыхДанных = Ссылка;</ПередВыгрузкой>"
    )
    clean = run(
        exchange(pko("Номенклатура", NOMENCLATURE, NOMENCLATURE, extra=both)), source, target
    )
    assert hits(clean, EXPORT_CACHE) == []
    assert hits(clean, EXPORT_KEY) == []
    comment = "<ПередВыгрузкой>// ЗапоминатьВыгруженные = Истина;</ПередВыгрузкой>"
    assert (
        hits(
            run(
                exchange(pko("Номенклатура", NOMENCLATURE, NOMENCLATURE, extra=comment)),
                source,
                target,
            ),
            EXPORT_CACHE,
        )
        == []
    )
    literal = '<ПередВыгрузкой>Сообщить("ЗапоминатьВыгруженные = Истина");</ПередВыгрузкой>'
    assert (
        hits(
            run(
                exchange(pko("Номенклатура", NOMENCLATURE, NOMENCLATURE, extra=literal)),
                source,
                target,
            ),
            EXPORT_CACHE,
        )
        == []
    )
    disabled = remember
    assert (
        hits(
            run(
                exchange(
                    pko(
                        "Номенклатура",
                        NOMENCLATURE,
                        NOMENCLATURE,
                        extra=disabled,
                        attrs=' Отключить="true"',
                    )
                ),
                source,
                target,
            ),
            EXPORT_CACHE,
        )
        == []
    )
    compared = (
        "<ПередВыгрузкой>Если ЗапоминатьВыгруженные = Истина Тогда\nКонецЕсли;</ПередВыгрузкой>"
    )
    assert (
        hits(
            run(
                exchange(pko("Номенклатура", NOMENCLATURE, NOMENCLATURE, extra=compared)),
                source,
                target,
            ),
            EXPORT_CACHE,
        )
        == []
    )
    # Флаг ПКО уже включён по умолчанию, но обычную выгрузку он не запоминает (БСП:398).
    flag_on = (
        "<Правило><Код>Н</Код><ПередВыгрузкой>ЗапоминатьВыгруженные = Истина;"
        "</ПередВыгрузкой></Правило>"
    )
    assert hits(run(exchange(flag_on), source, target), EXPORT_CACHE)
    opaque = "<ПередВыгрузкой>ЗапоминатьВыгруженные = Истина;\nВыполнить(Код);</ПередВыгрузкой>"
    skipped = run(
        exchange(pko("Номенклатура", NOMENCLATURE, NOMENCLATURE, extra=opaque)), source, target
    )
    assert hits(skipped, EXPORT_CACHE) == []
    assert skips(skipped, EXPORT_CACHE) == [
        "ПКО «Номенклатура»: «ПередВыгрузкой» непрозрачен — не видно, задаёт ли обработчик "
        "«КлючВыгружаемыхДанных» при включённом запоминании"
    ]
    unknown = "<ПередВыгрузкой>ЗапоминатьВыгруженные = ФлагКэша;</ПередВыгрузкой>"
    unknown_report = run(
        exchange(pko("Номенклатура", NOMENCLATURE, NOMENCLATURE, extra=unknown)), source, target
    )
    assert hits(unknown_report, EXPORT_CACHE) == []
    assert skips(unknown_report, EXPORT_CACHE)
    via_algorithm = "<ПередВыгрузкой>Выполнить(Алгоритмы.Запомнить);</ПередВыгрузкой>"
    algorithms = (
        '<Алгоритм Имя="Запомнить"><Текст>ЗапоминатьВыгруженные = Истина;</Текст></Алгоритм>'
    )
    from_algorithm = run(
        exchange(
            pko("Номенклатура", NOMENCLATURE, NOMENCLATURE, extra=via_algorithm),
            algorithms=algorithms,
        ),
        source,
        target,
    )
    assert [issue.address for issue in hits(from_algorithm, EXPORT_CACHE)] == ["ПКО «Номенклатура»"]


def test_attached_processing_requires_an_instance_the_loader_does_not_create(
    sides: tuple[sqlite3.Connection, ...],
) -> None:
    """`ДопОбработки` остаётся пустой структурой, даже если обработка описана в правилах."""
    source, target = sides
    processors = (
        '<Обработки><Обработка Имя="Библиотека" Наименование="Библиотека">'
        "AAAA</Обработка></Обработки>"
    )
    call = "<ПередВыгрузкой>ДопОбработки.Библиотека.Выполнить();</ПередВыгрузкой>"
    warned = run(
        exchange(
            pko("Номенклатура", NOMENCLATURE, NOMENCLATURE, extra=call),
            processors=processors,
        ),
        source,
        target,
    )
    found = hits(warned, ATTACHED)
    assert len(found) == 1
    assert found[0].address == "ПКО «Номенклатура»"
    assert "Библиотека" in found[0].message
    assert "не создаёт экземпляр" in found[0].message
    bracket = '<ПередВыгрузкой>ДопОбработки["Библиотека"].Выполнить();</ПередВыгрузкой>'
    bracketed = run(
        exchange(
            pko("Номенклатура", NOMENCLATURE, NOMENCLATURE, extra=bracket),
            processors=processors,
        ),
        source,
        target,
    )
    assert len(hits(bracketed, ATTACHED)) == 1
    own = (
        "<ПередВыгрузкой>ДопОбработки.Библиотека = Создать();\n"
        "ДопОбработки.Библиотека.Выполнить();</ПередВыгрузкой>"
    )
    assert (
        hits(
            run(
                exchange(
                    pko("Номенклатура", NOMENCLATURE, NOMENCLATURE, extra=own),
                    processors=processors,
                ),
                source,
                target,
            ),
            ATTACHED,
        )
        == []
    )
    inserted = (
        '<ПередВыгрузкой>ДопОбработки.Вставить("Библиотека", Создать());\n'
        "ДопОбработки.Библиотека.Выполнить();</ПередВыгрузкой>"
    )
    assert (
        hits(
            run(
                exchange(
                    pko("Номенклатура", NOMENCLATURE, NOMENCLATURE, extra=inserted),
                    processors=processors,
                ),
                source,
                target,
            ),
            ATTACHED,
        )
        == []
    )
    comment = "<ПередВыгрузкой>// ДопОбработки.Библиотека.Выполнить();</ПередВыгрузкой>"
    assert (
        hits(
            run(
                exchange(
                    pko("Номенклатура", NOMENCLATURE, NOMENCLATURE, extra=comment),
                    processors=processors,
                ),
                source,
                target,
            ),
            ATTACHED,
        )
        == []
    )
    literal = '<ПередВыгрузкой>Сообщить("ДопОбработки.Библиотека");</ПередВыгрузкой>'
    assert (
        hits(
            run(
                exchange(
                    pko("Номенклатура", NOMENCLATURE, NOMENCLATURE, extra=literal),
                    processors=processors,
                ),
                source,
                target,
            ),
            ATTACHED,
        )
        == []
    )
    disabled = call
    assert (
        hits(
            run(
                exchange(
                    pko(
                        "Номенклатура",
                        NOMENCLATURE,
                        NOMENCLATURE,
                        extra=disabled,
                        attrs=' Отключить="true"',
                    ),
                    processors=processors,
                ),
                source,
                target,
            ),
            ATTACHED,
        )
        == []
    )
    missing = "<ПередВыгрузкой>ДопОбработки.Чужая.Выполнить();</ПередВыгрузкой>"
    missed = run(
        exchange(pko("Номенклатура", NOMENCLATURE, NOMENCLATURE, extra=missing)),
        source,
        target,
    )
    missed_hits = hits(missed, ATTACHED)
    assert len(missed_hits) == 1
    assert "не описана" in missed_hits[0].message
    dynamic = "<ПередВыгрузкой>ДопОбработки[Имя].Выполнить();</ПередВыгрузкой>"
    dynamic_report = run(
        exchange(pko("Номенклатура", NOMENCLATURE, NOMENCLATURE, extra=dynamic)),
        source,
        target,
    )
    assert hits(dynamic_report, ATTACHED) == []
    assert skips(dynamic_report, ATTACHED) == [
        "ПКО «Номенклатура»: «ПередВыгрузкой» задаёт имя вложенной обработки кодом — "
        "это имя не сверяется"
    ]
    algorithms = (
        '<Алгоритм Имя="Вызов"><Текст>ДопОбработки.Библиотека.Выполнить();</Текст></Алгоритм>'
    )
    from_algorithm = run(
        exchange(
            pko(
                "Номенклатура",
                NOMENCLATURE,
                NOMENCLATURE,
                extra="<ПередВыгрузкой>Алгоритмы.Вызов();</ПередВыгрузкой>",
            ),
            algorithms=algorithms,
            processors=processors,
        ),
        source,
        target,
    )
    algorithm_hits = hits(from_algorithm, ATTACHED)
    assert [issue.address for issue in algorithm_hits] == ["алгоритм «Вызов»"]
    assert "Библиотека" in algorithm_hits[0].message


def test_attached_processing_instance_created_elsewhere_silences_every_reader(
    sides: tuple[sqlite3.Connection, ...],
) -> None:
    """Экземпляр создают один раз (алгоритм, событие конвертации), читают в других обработчиках."""
    source, target = sides
    call = "<ПередВыгрузкой>ДопОбработки.Библиотека.Выполнить();</ПередВыгрузкой>"
    created = (
        '<Алгоритм Имя="Создать"><Текст>ДопОбработки.Вставить("Библиотека", '
        "Обработки.Библиотека.Создать());</Текст></Алгоритм>"
    )
    report = run(
        exchange(pko("Номенклатура", NOMENCLATURE, NOMENCLATURE, extra=call), algorithms=created),
        source,
        target,
    )
    assert hits(report, ATTACHED) == []
    other = (
        '<Алгоритм Имя="Создать"><Текст>ДопОбработки.Вставить("Другая", '
        "Обработки.Другая.Создать());</Текст></Алгоритм>"
    )
    still = run(
        exchange(pko("Номенклатура", NOMENCLATURE, NOMENCLATURE, extra=call), algorithms=other),
        source,
        target,
    )
    assert len(hits(still, ATTACHED)) == 1
    whole = '<Алгоритм Имя="Создать"><Текст>ДопОбработки = ПолучитьОбработки();</Текст></Алгоритм>'
    replaced = run(
        exchange(pko("Номенклатура", NOMENCLATURE, NOMENCLATURE, extra=call), algorithms=whole),
        source,
        target,
    )
    assert hits(replaced, ATTACHED) == []
    assert skips(replaced, ATTACHED) == [
        "алгоритм «Создать»: «Текст» присваивает «ДопОбработки» целиком — не видно, "
        "создан ли экземпляр"
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


def test_modified_flag_warns_on_assignment_not_on_lookalikes(
    sides: tuple[sqlite3.Connection, ...],
) -> None:
    source, target = sides
    direct = "<ПриЗагрузке>ОбъектМодифицирован = Ложь;</ПриЗагрузке>"
    warned = run(exchange(pko("Приход", DOC, DOC, extra=direct)), source, target)
    assert [issue.address for issue in hits(warned, MODIFIED_FLAG)] == ["ПКО «Приход»"]
    assert "не отменяет запись" in hits(warned, MODIFIED_FLAG)[0].message
    assert "универсальной обработке" in hits(warned, MODIFIED_FLAG)[0].message
    lookalikes = (
        "<ПослеЗагрузки>// ОбъектМодифицирован = Ложь;\n"
        'Сообщить("ОбъектМодифицирован = Ложь");\n'
        "Данные.ОбъектМодифицирован = Ложь;\n"
        "Если ОбъектМодифицирован = Ложь Тогда\n"
        "КонецЕсли;</ПослеЗагрузки>"
    )
    assert (
        hits(
            run(exchange(pko("Приход", DOC, DOC, extra=lookalikes)), source, target), MODIFIED_FLAG
        )
        == []
    )
    disabled = run(
        exchange(pko("Приход", DOC, DOC, extra=direct, attrs=' Отключить="true"')),
        source,
        target,
    )
    assert hits(disabled, MODIFIED_FLAG) == []
    via_algorithm = exchange(
        pko("Приход", DOC, DOC, extra="<ПриЗагрузке>Выполнить(Алгоритмы.Сброс);</ПриЗагрузке>"),
        algorithms='<Алгоритм Имя="Сброс"><Текст>ОбъектМодифицирован = Ложь;</Текст></Алгоритм>',
    )
    assert hits(run(via_algorithm, source, target), MODIFIED_FLAG)
    quiet_algorithm = exchange(
        pko("Приход", DOC, DOC, extra="<ПриЗагрузке>Выполнить(Алгоритмы.Пустой);</ПриЗагрузке>"),
        algorithms='<Алгоритм Имя="Пустой"><Текст>Сообщить("нет");</Текст></Алгоритм>',
    )
    quiet = run(quiet_algorithm, source, target)
    assert hits(quiet, MODIFIED_FLAG) == []
    assert skips(quiet, MODIFIED_FLAG) == []
    opaque = "<ПриЗагрузке>Выполнить(Текст);</ПриЗагрузке>"
    assert skips(
        run(exchange(pko("Приход", DOC, DOC, extra=opaque)), source, target), MODIFIED_FLAG
    )


def test_table_no_clear_is_only_a_tabular_section(
    sides: tuple[sqlite3.Connection, ...],
) -> None:
    source, target = sides
    flag = "<ПередОбработкойВыгрузки>НеОчищать = Истина;</ПередОбработкойВыгрузки>"
    table = pks_group("Товары", "Товары", extra=flag)
    warned = run(exchange(pko("Номенклатура", NOMENCLATURE, NOMENCLATURE, table)), source, target)
    found = hits(warned, TABLE_NO_CLEAR)
    assert len(found) == 1
    assert "Товары" in found[0].message
    assert "не получает" in found[0].message
    number = pks_group(
        "Товары",
        "Товары",
        extra="<ПередОбработкойВыгрузки>НеОчищать = 1;</ПередОбработкойВыгрузки>",
    )
    assert hits(
        run(exchange(pko("Номенклатура", NOMENCLATURE, NOMENCLATURE, number)), source, target),
        TABLE_NO_CLEAR,
    )
    movement = pks_group("Остатки", "Остатки", extra=flag, kind="НаборДвиженийРегистраНакопления")
    assert (
        hits(run(exchange(pko("Приход", DOC, DOC, movement)), source, target), TABLE_NO_CLEAR) == []
    )
    comment = pks_group(
        "Товары",
        "Товары",
        extra=(
            "<ПередОбработкойВыгрузки>// НеОчищать = Истина;\n"
            'Сообщить("НеОчищать = Истина");\n'
            "Строка.НеОчищать = Истина;</ПередОбработкойВыгрузки>"
        ),
    )
    assert (
        hits(
            run(exchange(pko("Номенклатура", NOMENCLATURE, NOMENCLATURE, comment)), source, target),
            TABLE_NO_CLEAR,
        )
        == []
    )
    disabled = pks_group("Товары", "Товары", extra=flag)
    assert (
        hits(
            run(
                exchange(
                    pko(
                        "Номенклатура",
                        NOMENCLATURE,
                        NOMENCLATURE,
                        disabled,
                        attrs=' Отключить="true"',
                    )
                ),
                source,
                target,
            ),
            TABLE_NO_CLEAR,
        )
        == []
    )
    unknown = pks_group("Товары", "Товары", extra=flag, kind="")
    skipped = run(exchange(pko("Номенклатура", NOMENCLATURE, NOMENCLATURE, unknown)), None, None)
    assert hits(skipped, TABLE_NO_CLEAR) == []
    assert skips(skipped, TABLE_NO_CLEAR)
    # Структура называет «Товары» табличной частью, даже если вид в правилах пуст.
    confirmed = run(
        exchange(pko("Номенклатура", NOMENCLATURE, NOMENCLATURE, unknown)), source, target
    )
    assert hits(confirmed, TABLE_NO_CLEAR)


def test_repeated_table_target_names_both_groups(
    sides: tuple[sqlite3.Connection, ...],
) -> None:
    source, target = sides
    body = pks_group("Товары", "Товары") + pks_group("Прочее", "Товары")
    warned = run(exchange(pko("Номенклатура", NOMENCLATURE, NOMENCLATURE, body)), source, target)
    found = hits(warned, REPEATED_TABLE)
    assert len(found) == 1
    assert "взаимоисключающие" in found[0].message
    assert found[0].message.count("ПКС") == 2
    movement = pks_group("Остатки", "Остатки", kind="НаборДвиженийРегистраНакопления") + pks_group(
        "Ещё", "Остатки", kind="НаборДвиженийРегистраНакопления"
    )
    assert (
        hits(run(exchange(pko("Приход", DOC, DOC, movement)), source, target), REPEATED_TABLE) == []
    )
    one = pks_group("Товары", "Товары")
    assert (
        hits(
            run(exchange(pko("Номенклатура", NOMENCLATURE, NOMENCLATURE, one)), source, target),
            REPEATED_TABLE,
        )
        == []
    )
    unknown = pks_group("А", "Строки", kind="") + pks_group("Б", "Строки", kind="")
    skipped = run(exchange(pko("Номенклатура", NOMENCLATURE, NOMENCLATURE, unknown)), None, None)
    assert hits(skipped, REPEATED_TABLE) == []
    assert "вид не определён" in skips(skipped, REPEATED_TABLE)[0]


def test_ambiguous_default_pko_needs_an_implicit_reference(
    sides: tuple[sqlite3.Connection, ...],
) -> None:
    source, target = sides
    catalog = pko("Ном", NOMENCLATURE, NOMENCLATURE) + pko("НомГруппа", NOMENCLATURE, NOMENCLATURE)
    assert hits(run(exchange(catalog), source, target), AMBIGUOUS_PKO) == []
    property_xml = (
        '<Свойство><Источник Имя="Номенклатура" Вид="Реквизит" '
        f'Тип="{NOMENCLATURE}"/><Приемник Имя="Номенклатура" Вид="Реквизит"/></Свойство>'
    )
    linked = catalog + pko("Приход", DOC, DOC, property_xml)
    warned = run(exchange(linked), source, target)
    found = hits(warned, AMBIGUOUS_PKO)
    assert len(found) == 1
    assert "Ном" in found[0].message and "НомГруппа" in found[0].message
    assert "последнее загруженное" in found[0].message
    assert "ПКС" in found[0].message
    named = property_xml.replace(
        "</Свойство>", "<КодПравилаКонвертации>Ном</КодПравилаКонвертации></Свойство>"
    )
    assert (
        hits(run(exchange(catalog + pko("Приход", DOC, DOC, named)), source, target), AMBIGUOUS_PKO)
        == []
    )
    commented = (
        '<Свойство><Источник Имя="Номенклатура" Вид="Реквизит" '
        f'Тип="{NOMENCLATURE}"/>'
        '<Приемник Имя="Номенклатура" Вид="Реквизит"/>'
        '<ПередВыгрузкой>// ИмяПКО = "";\nИмяПКО = "Ном";</ПередВыгрузкой></Свойство>'
    )
    assert (
        hits(
            run(exchange(catalog + pko("Приход", DOC, DOC, commented)), source, target),
            AMBIGUOUS_PKO,
        )
        == []
    )
    disabled = pko("Ном", NOMENCLATURE, NOMENCLATURE, attrs=' Отключить="true"') + pko(
        "НомГруппа", NOMENCLATURE, NOMENCLATURE
    )
    assert (
        hits(
            run(exchange(disabled + pko("Приход", DOC, DOC, property_xml)), source, target),
            AMBIGUOUS_PKO,
        )
        == []
    )
    untyped = pks("Номенклатура", "Номенклатура")
    skipped = run(exchange(catalog + pko("Приход", DOC, DOC, untyped)), None, None)
    assert hits(skipped, AMBIGUOUS_PKO) == []
    assert skips(skipped, AMBIGUOUS_PKO)


def test_multiple_pvd_of_one_type_keeps_only_the_first(
    sides: tuple[sqlite3.Connection, ...],
) -> None:
    source, target = sides
    rules = (
        f"<Правило><Код>Первое</Код><ОбъектВыборки>{DOC}</ОбъектВыборки></Правило>"
        f"<Правило><Код>Второе</Код><ОбъектВыборки>{DOC}</ОбъектВыборки></Правило>"
    )
    warned = run(exchange(pvd=rules), source, target)
    found = hits(warned, MULTIPLE_PVD)
    assert len(found) == 1
    assert found[0].address == "ПВД «Первое»"
    assert "не выполняются" in found[0].message
    assert "Второе" in found[0].message
    disabled = (
        f"<Правило><Код>Первое</Код><ОбъектВыборки>{DOC}</ОбъектВыборки></Правило>"
        f'<Правило Отключить="true"><Код>Второе</Код><ОбъектВыборки>{DOC}</ОбъектВыборки></Правило>'
    )
    assert hits(run(exchange(pvd=disabled), source, target), MULTIPLE_PVD) == []
    different = (
        f"<Правило><Код>Первое</Код><ОбъектВыборки>{DOC}</ОбъектВыборки></Правило>"
        f"<Правило><Код>Второе</Код><ОбъектВыборки>{NOMENCLATURE}</ОбъектВыборки></Правило>"
    )
    assert hits(run(exchange(pvd=different), source, target), MULTIPLE_PVD) == []
    # Контрагентов в составе плана «Обмен» нет: до выборки изменений объект не доходит.
    outside = (
        "<Правило><Код>Первое</Код>"
        "<ОбъектВыборки>СправочникСсылка.Контрагенты</ОбъектВыборки></Правило>"
        "<Правило><Код>Второе</Код>"
        "<ОбъектВыборки>СправочникСсылка.Контрагенты</ОбъектВыборки></Правило>"
    )
    quiet = run(exchange(pvd=outside), source, target)
    assert hits(quiet, MULTIPLE_PVD) == []
    assert skips(quiet, MULTIPLE_PVD) == []
    unknown = run(exchange(pvd=rules), None, None)
    assert hits(unknown, MULTIPLE_PVD) == []
    assert skips(unknown, MULTIPLE_PVD) == [
        "ПВД «Первое»: состав плана обмена неизвестен: не видно, регистрируется ли объект"
    ]


_PLAN_UNKNOWN = "состав плана обмена неизвестен: не видно, регистрируется ли объект"
_REFUSAL = (
    f"«Отказ» в «ПередОбработкой» ПВД «В» при обмене через план не проверяется: "
    f"зарегистрированные объекты «{DOC}» будут выгружены. "
    "Чтобы объект не уходил — правило регистрации или отказ в «ПередВыгрузкой» ПВД."
)


def _refusal(selection: str = DOC, handler: str = "Отказ = Истина;") -> str:
    return (
        f"<Правило><Код>В</Код><ОбъектВыборки>{selection}</ОбъектВыборки>"
        f"<ПередОбработкойПравила>{handler}</ПередОбработкойПравила></Правило>"
    )


def test_pvd_refusal_is_separate_from_selection(
    sides: tuple[sqlite3.Connection, ...],
) -> None:
    source, target = sides
    both = _refusal(handler="Отказ = Истина;\nВыборкаДанных = Запрос.Выполнить();")
    warned = run(exchange(pvd=both), source, target)
    assert [issue.message for issue in hits(warned, PVD_REFUSAL)] == [_REFUSAL]
    assert hits(warned, PVD_SELECTION)
    lookalikes = (
        "<Правило><Код>В</Код><ПередОбработкойПравила>"
        "// Отказ = Истина;\n"
        'Сообщить("Отказ = Истина");\n'
        "Если Отказ = Истина Тогда\nКонецЕсли;\n"
        "Параметры.Отказ = Истина;"
        "</ПередОбработкойПравила></Правило>"
    )
    assert hits(run(exchange(pvd=lookalikes), source, target), PVD_REFUSAL) == []
    correct_phase = (
        "<Правило><Код>В</Код><ПередВыгрузкойОбъекта>Отказ = Истина;</ПередВыгрузкойОбъекта>"
        "</Правило>"
    )
    assert hits(run(exchange(pvd=correct_phase), source, target), PVD_REFUSAL) == []
    disabled = (
        '<Правило Отключить="true"><Код>В</Код>'
        "<ПередОбработкойПравила>Отказ = Истина;</ПередОбработкойПравила></Правило>"
    )
    assert hits(run(exchange(pvd=disabled), source, target), PVD_REFUSAL) == []
    opaque = (
        "<Правило><Код>В</Код><ПередОбработкойПравила>Выполнить(Текст);</ПередОбработкойПравила>"
        "</Правило>"
    )
    assert skips(run(exchange(pvd=opaque), source, target), PVD_REFUSAL) == [
        "ПВД «В»: «ПередОбработкой» непрозрачен — не видно, присваивается ли «Отказ = Истина»"
    ]
    # Контрагентов в составе плана «Обмен» нет — отказ для выгрузки без плана, замечания нет.
    outside = run(exchange(pvd=_refusal("СправочникСсылка.Контрагенты")), source, target)
    assert hits(outside, PVD_REFUSAL) == []
    assert skips(outside, PVD_REFUSAL) == []
    unknown = run(exchange(pvd=_refusal()), None, None)
    assert hits(unknown, PVD_REFUSAL) == []
    assert skips(unknown, PVD_REFUSAL) == [f"ПВД «В»: {_PLAN_UNKNOWN}"]
    missing_plan = check_exchange_plan(
        load_exchange_rules(exchange(pvd=_refusal())), source, target, "НетТакого"
    )
    assert hits(missing_plan, PVD_REFUSAL) == []
    assert skips(missing_plan, PVD_REFUSAL) == [f"ПВД «В»: {_PLAN_UNKNOWN}"]


def test_several_plans_without_a_name_leave_refusal_unknown() -> None:
    """Несколько планов и нет имени — состав не выбрать, даже если объект есть в одном из них."""
    connection = sqlite3.connect(":memory:")
    connection.executescript(SCHEMA)
    for index, name in enumerate(("Первый", "Второй"), start=1):
        connection.execute(
            "INSERT INTO objects (id, kind, name, type_name) VALUES (?, 'ПланОбмена', ?, ?)",
            (index, name, f"ПланОбменаСсылка.{name}"),
        )
    connection.execute(
        "INSERT INTO type_sets (id, types) VALUES (1, ?)",
        (DOC,),
    )
    connection.execute(
        "INSERT INTO properties (object_id, kind, name, path, type_set_id, autoregistration) "
        "VALUES (1, 'ЭлементСоставаПланаОбмена', 'Приход', 'Приход', 1, 0)"
    )
    report = check_exchange_plan(load_exchange_rules(exchange(pvd=_refusal())), connection, None)
    assert hits(report, PVD_REFUSAL) == []
    assert skips(report, PVD_REFUSAL) == [f"ПВД «В»: {_PLAN_UNKNOWN}"]
    named = check_exchange_plan(
        load_exchange_rules(exchange(pvd=_refusal())), connection, None, "Первый"
    )
    assert [issue.message for issue in hits(named, PVD_REFUSAL)] == [_REFUSAL]
    # Папка живых правил названа именем плана: каталог пути называет план без явного имени.
    hinted = check_exchange_plan(
        load_exchange_rules(exchange(pvd=_refusal())),
        connection,
        None,
        None,
        ("C:", "проект", "ПравилаОбмена", "Первый"),
    )
    assert [issue.message for issue in hits(hinted, PVD_REFUSAL)] == [_REFUSAL]
    # Два каталога пути равны именам двух планов — выбора нет.
    both = check_exchange_plan(
        load_exchange_rules(exchange(pvd=_refusal())), connection, None, None, ("Первый", "Второй")
    )
    assert skips(both, PVD_REFUSAL) == [f"ПВД «В»: {_PLAN_UNKNOWN}"]
    connection.close()


def test_plan_name_comes_from_the_exchange_plan_directory() -> None:
    path = Path("cfg/ExchangePlans/ОбменЗарплата/Templates/ПравилаОбмена/Ext/Template.txt")
    assert plan_name_from_path(path) == "ОбменЗарплата"
    assert plan_name_from_path(Path("workspace/bit-zup/ExchangeRules.xml")) is None
    assert plan_name_from_path(None) is None
