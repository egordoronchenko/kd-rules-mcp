"""Проверки по коду обработчиков на одном разборе BSL (изменение bsl-handler-lexing).

Для каждой проверки — многострочный литерал с `//`, `""` и строкой-комментарием с кавычкой
(текст литерала не код, код после литерала проверяется) и сравнение в `?(…)` или после ключевого
слова (присваивание — только в начале оператора). Код после литерала заканчивается ещё одним
литералом: прежняя маска литералов захватывала текст между кавычкой в конце литерала и этим
литералом и прятала код.
"""

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from kd_rules_mcp.kd2.rules_io import load_exchange_rules, load_registration_rules
from kd_rules_mcp.structures.store import StructureStore
from kd_rules_mcp.validation.algorithms import MISSING_ALGORITHM, check_algorithm_refs
from kd_rules_mcp.validation.call_graph import build_call_graph
from kd_rules_mcp.validation.exchange_plan import (
    AMBIGUOUS_PKO,
    ATTACHED,
    DOCUMENT_POSTING,
    EXPORT_CACHE,
    EXPORT_KEY,
    INCOMING_KEY,
    MODIFIED_FLAG,
    OBJECT_PARAMS_DOT,
    OBJECT_WRITE,
    PREDEFINED_PKZ,
    PVD_REFUSAL,
    PVD_SELECTION,
    TABLE_NO_CLEAR,
    check_exchange_plan,
)
from kd_rules_mcp.validation.registration import QUERY_PARAMETERS, check_registration
from kd_rules_mcp.validation.report import Issue, ValidationReport
from kd_rules_mcp.validation.search import (
    NAME_NOT_SEARCH_PROP,
    PARAM_NOT_PASSED,
    check_search_objects,
    check_search_params,
)

DUMP = Path(__file__).parent / "data" / "xmldump"
DOC = "ДокументСсылка.Приход"
NOMENCLATURE = "СправочникСсылка.Номенклатура"
CHART = "ПланСчетовСсылка.Хозрасчетный"
DONE = 'Сообщить("готово");'


def literal(inner: str) -> str:
    """Литерал через строки: `inner` — на строке продолжения рядом с `""` и `//`."""
    return (
        'Т = "Текст // не комментарий\n'
        '// комментарий с кавычкой "\n'
        f'|{inner} ""кавычки"" // текст\n'
        '|конец";\n'
    )


def after(code: str) -> str:
    """Код после многострочного литерала и литерал за ним."""
    return literal("Пусто") + code + "\n" + DONE


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
    pko: str = "", pvd: str = "", *, processors: str = "", events: str = "", algorithms: str = ""
) -> bytes:
    return f"""<ПравилаОбмена>
<ВерсияФормата>2.01</ВерсияФормата>
<Источник ВерсияКонфигурации="1.0.0.1">Тестовая</Источник>
<Приемник ВерсияКонфигурации="9.9.9.9">Другая</Приемник>
{events}
{processors}
<ПравилаКонвертацииОбъектов>{pko}</ПравилаКонвертацииОбъектов>
<ПравилаВыгрузкиДанных>{pvd}</ПравилаВыгрузкиДанных>
<Алгоритмы>{algorithms}</Алгоритмы>
</ПравилаОбмена>""".encode()


def pko(code: str, source: str, target: str, body: str = "", extra: str = "") -> str:
    return (
        f"<Правило><Код>{code}</Код><Источник>{source}</Источник>"
        f"<Приемник>{target}</Приемник>{extra}<Свойства>{body}</Свойства></Правило>"
    )


def run(xml: bytes, sides: tuple[sqlite3.Connection, ...] | None = None) -> ValidationReport:
    source, target = sides if sides else (None, None)
    return check_exchange_plan(load_exchange_rules(xml), source, target)


def hits(report: ValidationReport, check: str) -> list[Issue]:
    return [issue for issue in report.issues if issue.check == check]


def skips(report: ValidationReport, check: str) -> list[str]:
    return [item.reason for item in report.skipped if item.check == check]


# --- handlers.* -----------------------------------------------------------------------------


def test_object_write(sides: tuple[sqlite3.Connection, ...]) -> None:
    def check(code: str) -> list[Issue]:
        extra = f"<ПослеЗагрузки>{code}</ПослеЗагрузки>"
        return hits(run(exchange(pko("Приход", DOC, DOC, extra=extra)), sides), OBJECT_WRITE)

    assert check(literal("Объект.Записать();")) == []
    assert len(check(after("Объект.Записать();"))) == 1


def test_pvd_selection(sides: tuple[sqlite3.Connection, ...]) -> None:
    def check(code: str) -> list[Issue]:
        pvd = (
            f"<Правило><Код>В</Код><ОбъектВыборки>{DOC}</ОбъектВыборки>"
            f"<ПередОбработкойПравила>{code}</ПередОбработкойПравила></Правило>"
        )
        return hits(run(exchange(pvd=pvd), sides), PVD_SELECTION)

    assert check(literal("ВыборкаДанных = Запрос.Выполнить();")) == []
    assert len(check(after("ВыборкаДанных = Запрос.Выполнить();"))) == 1


def _before_export(code: str, sides: tuple[sqlite3.Connection, ...]) -> ValidationReport:
    extra = f"<ПередВыгрузкой>{code}</ПередВыгрузкой>"
    return run(exchange(pko("Номенклатура", NOMENCLATURE, NOMENCLATURE, extra=extra)), sides)


def test_export_key(sides: tuple[sqlite3.Connection, ...]) -> None:
    def check(code: str) -> list[Issue]:
        return hits(_before_export(code, sides), EXPORT_KEY)

    assert check(literal("КлючВыгружаемыхДанных = Ссылка;")) == []
    assert len(check(after("КлючВыгружаемыхДанных = Ссылка;"))) == 1
    # Сравнение — не ключ; сравнение с флагом — не запоминание.
    assert check("А = ?(КлючВыгружаемыхДанных = Неопределено, 1, 2);") == []
    remembered_in_condition = (
        "КлючВыгружаемыхДанных = Ссылка;\nЕсли ЗапоминатьВыгруженные = Истина Тогда\nКонецЕсли;"
    )
    assert len(check(remembered_in_condition)) == 1
    assert check("КлючВыгружаемыхДанных = Ссылка;\nПопытка ЗапоминатьВыгруженные = Истина;") == []


def test_export_cache_without_key(sides: tuple[sqlite3.Connection, ...]) -> None:
    def check(code: str) -> list[Issue]:
        return hits(_before_export(code, sides), EXPORT_CACHE)

    assert check(literal("ЗапоминатьВыгруженные = Истина;")) == []
    assert len(check(after("ЗапоминатьВыгруженные = Истина;"))) == 1
    compared = "А = ?(КлючВыгружаемыхДанных = Неопределено, 1, 2);\nЗапоминатьВыгруженные = Истина;"
    assert len(check(compared)) == 1
    assert check("ЗапоминатьВыгруженные = Ф(1, КлючВыгружаемыхДанных = 2);") == []


def test_attached_processing(sides: tuple[sqlite3.Connection, ...]) -> None:
    processors = (
        '<Обработки><Обработка Имя="Библиотека" Наименование="Библиотека">'
        "AAAA</Обработка></Обработки>"
    )

    def check(code: str) -> list[Issue]:
        extra = f"<ПередВыгрузкой>{code}</ПередВыгрузкой>"
        xml = exchange(
            pko("Номенклатура", NOMENCLATURE, NOMENCLATURE, extra=extra), processors=processors
        )
        return hits(run(xml, sides), ATTACHED)

    call = "ДопОбработки.Библиотека.Выполнить();"
    assert check(literal(call)) == []
    assert len(check(after(call))) == 1
    # Сравнение в условии — чтение: экземпляр так не создаётся.
    compared = f"Если ДопОбработки.Библиотека = Неопределено Тогда\nКонецЕсли;\n{call}"
    assert len(check(compared)) == 1
    assert len(check(f'А = ?(ДопОбработки["Библиотека"] = Неопределено, 1, 2);\n{call}')) == 1
    assert (
        check(f"Попытка ДопОбработки.Библиотека = Создать(); Исключение КонецПопытки;\n{call}")
        == []
    )


def test_object_params_dot() -> None:
    def check(code: str) -> ValidationReport:
        extra = f"<ПослеЗагрузки>{code}</ПослеЗагрузки>"
        return run(exchange(pko("Спр", NOMENCLATURE, NOMENCLATURE, extra=extra)))

    assert hits(check(literal("А = ПараметрыОбъекта.Имя;")), OBJECT_PARAMS_DOT) == []
    assert len(hits(check(after("А = ПараметрыОбъекта.Имя;")), OBJECT_PARAMS_DOT)) == 1
    compared = "А = ?(ПараметрыОбъекта = Неопределено, 1, ПараметрыОбъекта.Имя);"
    report = check(compared)
    assert len(hits(report, OBJECT_PARAMS_DOT)) == 1
    assert skips(report, OBJECT_PARAMS_DOT) == []


def test_ignored_modified_flag(sides: tuple[sqlite3.Connection, ...]) -> None:
    def check(code: str) -> list[Issue]:
        extra = f"<ПриЗагрузке>{code}</ПриЗагрузке>"
        return hits(run(exchange(pko("Приход", DOC, DOC, extra=extra)), sides), MODIFIED_FLAG)

    assert check(literal("ОбъектМодифицирован = Ложь;")) == []
    assert len(check(after("ОбъектМодифицирован = Ложь;"))) == 1
    assert check("А = ?(ОбъектМодифицирован = Ложь, 1, 2);") == []
    assert check("Ф(1, ОбъектМодифицирован = Ложь);") == []
    assert len(check("Попытка\nОбъектМодифицирован = Ложь;\nИсключение\nКонецПопытки;")) == 1


def test_table_no_clear(sides: tuple[sqlite3.Connection, ...]) -> None:
    def check(code: str) -> list[Issue]:
        group = (
            '<Группа><Источник Имя="Товары" Вид="ТабличнаяЧасть"/>'
            '<Приемник Имя="Товары" Вид="ТабличнаяЧасть"/>'
            f"<ПередОбработкойВыгрузки>{code}</ПередОбработкойВыгрузки></Группа>"
        )
        xml = exchange(pko("Номенклатура", NOMENCLATURE, NOMENCLATURE, group))
        return hits(run(xml, sides), TABLE_NO_CLEAR)

    assert check(literal("НеОчищать = Истина;")) == []
    assert len(check(after("НеОчищать = Истина;"))) == 1
    assert check("А = ?(НеОчищать = Истина, 1, 2);") == []
    assert len(check("Попытка НеОчищать = Истина; Исключение КонецПопытки;")) == 1


def test_pvd_refusal(sides: tuple[sqlite3.Connection, ...]) -> None:
    def check(code: str) -> list[Issue]:
        pvd = (
            f"<Правило><Код>В</Код><ОбъектВыборки>{DOC}</ОбъектВыборки>"
            f"<ПередОбработкойПравила>{code}</ПередОбработкойПравила></Правило>"
        )
        return hits(run(exchange(pvd=pvd), sides), PVD_REFUSAL)

    assert check(literal("Отказ = Истина;")) == []
    assert len(check(after("Отказ = Истина;"))) == 1
    assert check("А = ?(Отказ = Истина, 1, 2);") == []
    assert len(check("Если А Then\nОтказ = Истина;\nКонецЕсли;")) == 1


# --- structure.* по коду обработчиков ------------------------------------------------------


def _incoming(own: str, sides: tuple[sqlite3.Connection, ...]) -> list[Issue]:
    body = (
        '<Свойство><Источник Имя="Артикул" Вид="Реквизит"/>'
        '<Приемник Имя="Артикул" Вид="Реквизит"/>'
        "<ПолучитьИзВходящихДанных>true</ПолучитьИзВходящихДанных>"
        f"<ПередВыгрузкой>{own}</ПередВыгрузкой></Свойство>"
    )
    export = (
        "<Правило><Код>В</Код><КодПравилаКонвертации>Номенклатура</КодПравилаКонвертации></Правило>"
    )
    xml = exchange(pko("Номенклатура", NOMENCLATURE, NOMENCLATURE, body), pvd=export)
    return hits(run(xml, sides), INCOMING_KEY)


def test_incoming_key(sides: tuple[sqlite3.Connection, ...]) -> None:
    # Присваивание `Значение` до чтения входящих гасит проверку — только настоящее.
    assert _incoming("Значение = 1;", sides) == []
    assert len(_incoming(literal("Значение = 1;"), sides)) == 1
    assert _incoming(after("Значение = 1;"), sides) == []
    assert len(_incoming("А = ?(Значение = Неопределено, 1, 2);", sides)) == 1
    assert len(_incoming("Если Отказ = Истина Тогда\nКонецЕсли;", sides)) == 1


def test_document_posting(sides: tuple[sqlite3.Connection, ...]) -> None:
    export = "<Правило><Код>В</Код><КодПравилаКонвертации>Приход</КодПравилаКонвертации></Правило>"

    def check(event: str, code: str) -> list[Issue]:
        extra = f"<{event}>{code}</{event}>"
        return hits(
            run(exchange(pko("Приход", DOC, DOC, extra=extra), export), sides), DOCUMENT_POSTING
        )

    assert len(check("ПослеЗагрузки", literal("Объект.Проведен = Истина;"))) == 1
    assert check("ПослеЗагрузки", after("Объект.Проведен = Истина;")) == []
    compared = "А = ?(РежимЗаписи = РежимЗаписиДокумента.Проведение, 1, 2);"
    assert len(check("ПриЗагрузке", compared)) == 1
    assert check("ПриЗагрузке", "Попытка РежимЗаписи = РежимЗаписиДокумента.Проведение;") == []


def test_predefined_pkz(sides: tuple[sqlite3.Connection, ...]) -> None:
    def check(extra: str) -> list[Issue]:
        return hits(run(exchange(pko("Счета", CHART, CHART, extra=extra)), sides), PREDEFINED_PKZ)

    def search(code: str) -> str:
        return f"<ПоследовательностьПолейПоиска>{code}</ПоследовательностьПолейПоиска>"

    def named(code: str) -> str:
        return f"<ПередВыгрузкой>{code}</ПередВыгрузкой>"

    mark = "Имя = ИмяПредопределенныхДанных;"
    assert len(check(search(literal("ИмяПредопределенныхДанных")))) == 1
    assert check(search(after(mark))) == []
    assert len(check(named("А = ?(ИмяПредопределенногоЭлемента = Неопределено, 1, 2);"))) == 1
    assert check(named('Попытка ИмяПредопределенногоЭлемента = "Касса";')) == []


def test_ambiguous_default_pko_name_choice(sides: tuple[sqlite3.Connection, ...]) -> None:
    catalog = pko("Ном", NOMENCLATURE, NOMENCLATURE) + pko("НомГруппа", NOMENCLATURE, NOMENCLATURE)

    def check(code: str) -> list[Issue]:
        prop = (
            f'<Свойство><Источник Имя="Номенклатура" Вид="Реквизит" Тип="{NOMENCLATURE}"/>'
            '<Приемник Имя="Номенклатура" Вид="Реквизит"/>'
            f"<ПередВыгрузкой>{code}</ПередВыгрузкой></Свойство>"
        )
        return hits(run(exchange(catalog + pko("Приход", DOC, DOC, prop)), sides), AMBIGUOUS_PKO)

    assert check('ИмяПКО = "Ном";') == []
    assert len(check('А = ?(ИмяПКО = "Ном", 1, 2);')) == 1
    assert check('Попытка ИмяПКО = "Ном"; Исключение КонецПопытки;') == []
    assert len(check(literal('ИмяПКО = ""Ном"";'))) == 1


# --- search.* ------------------------------------------------------------------------------


def _search_rules(handler: str, properties: str) -> bytes:
    return (
        "<ПравилаОбмена><ВерсияФормата>2.01</ВерсияФормата>"
        "<ПравилаКонвертацииОбъектов><Правило><Код>Договоры</Код>"
        f"<ПоследовательностьПолейПоиска>{handler}</ПоследовательностьПолейПоиска>"
        "<Источник>СправочникСсылка.Договоры</Источник>"
        "<Приемник>СправочникСсылка.Договоры</Приемник>"
        f"<Свойства>{properties}</Свойства>"
        "</Правило></ПравилаКонвертацииОбъектов></ПравилаОбмена>"
    ).encode()


def _prop(name: str, *, search: bool = True) -> str:
    attrs = ' Поиск="true"' if search else ""
    return (
        f'<Свойство{attrs}><Источник Имя="{name}" Вид="Свойство"/>'
        f'<Приемник Имя="{name}" Вид="Свойство"/></Свойство>'
    )


def test_search_parameters() -> None:
    def check(handler: str) -> list[Issue]:
        rules = load_exchange_rules(_search_rules(handler, ""))
        return [i for i in check_search_params(rules).issues if i.check == PARAM_NOT_PASSED]

    # Нечётная кавычка в конце литерала: построчный разбор считал комментарий текстом.
    commented = 'Т = "Начало\n|конец"; // ПараметрыОбъекта["ВКомментарии"]'
    assert check(commented) == []
    assert check(literal("ПараметрыОбъекта.Получить(Имя)")) == []
    assert len(check(after('Вид = ПараметрыОбъекта["Вид"];'))) == 1


def test_search_string_and_search_map() -> None:
    def check(handler: str, properties: str) -> list[Issue]:
        report = check_search_objects(load_exchange_rules(_search_rules(handler, properties)))
        return [issue for issue in report.issues if issue.check == NAME_NOT_SEARCH_PROP]

    code = _prop("Код")
    # Присваивание после `Попытка` — присваивание, сравнение в `?(…)` — нет.
    assert (
        len(check('Попытка СтрокаИменСвойствПоиска = "НетТакого"; Исключение КонецПопытки;', code))
        == 1
    )
    assert check('А = ?(СтрокаИменСвойствПоиска = "НетТакого", 1, 2);', code) == []
    assert check(literal('СтрокаИменСвойствПоиска = ""НетТакого"";'), code) == []
    assert len(check(after('СтрокаИменСвойствПоиска = "НетТакого";'), code)) == 1
    written = (
        'СтрокаИменСвойствПоиска = "Банк, Код";\n'
        "Попытка\n"
        '    СвойстваПоиска["Банк"] = Банк;\n'
        "Исключение\n"
        "КонецПопытки;"
    )
    assert check(written, code) == []
    read = (
        'СтрокаИменСвойствПоиска = "Банк, Код";\n'
        'А = ?(СвойстваПоиска["Банк"] = Неопределено, 1, 2);'
    )
    assert len(check(read, code)) == 1
    in_literal = 'СтрокаИменСвойствПоиска = "Банк, Код";\n' + literal(
        'СвойстваПоиска[""Банк""] = 1;'
    )
    assert len(check(in_literal, code)) == 1


# --- algorithm.* и граф вызовов -----------------------------------------------------------


def _calls(handler: str) -> bytes:
    return (
        "<ПравилаОбмена><ВерсияФормата>2.01</ВерсияФормата>"
        "<ПравилаКонвертацииОбъектов>"
        "<Правило><Код>Док</Код><Источник>ДокументСсылка.Пример</Источник>"
        f"<ПередВыгрузкой>{handler}</ПередВыгрузкой></Правило>"
        "<Правило><Код>Спр</Код><Источник>СправочникСсылка.Пример</Источник></Правило>"
        "</ПравилаКонвертацииОбъектов><Алгоритмы></Алгоритмы></ПравилаОбмена>"
    ).encode()


def test_algorithm_reference() -> None:
    def check(handler: str) -> list[Issue]:
        report = check_algorithm_refs(load_exchange_rules(_calls(handler)))
        return [issue for issue in report.issues if issue.check == MISSING_ALGORITHM]

    assert check(literal("Алгоритмы.Нет")) == []
    assert check('Т = "А\n// Алгоритмы.Нет "\n|Б";') == []
    assert len(check(after("Выполнить(Алгоритмы.Нет);"))) == 1


def test_call_graph_edges() -> None:
    def targets(handler: str) -> list[tuple[str, str]]:
        graph = build_call_graph(load_exchange_rules(_calls(handler)))
        return sorted(
            (o.kind, o.target) for o in graph.occurrences if o.handler.event == "ПередВыгрузкой"
        )

    assert targets(literal('ВыгрузитьПоПравилу(Объект,,,, ""Спр"");')) == []
    assert targets(after('ВыгрузитьПоПравилу(Объект,,,, "Спр");')) == [("call", "Спр")]
    # Сравнение `ИмяПКО = "Х"` в условии — не присваивание: цели только из ветвей.
    assert targets(after('ИмяПКО = ?(ИмяПКО = "Х", "Спр", "Док");')) == [
        ("pko_assignment", "Док"),
        ("pko_assignment", "Спр"),
    ]


# --- registration.query_parameter_binding -------------------------------------------------


def _registration(handler: str) -> ValidationReport:
    xml = f"""<ПравилаРегистрации>
<ВерсияФормата>2.01</ВерсияФормата>
<ПланОбмена Имя="Обмен">Обмен</ПланОбмена>
<ПравилаРегистрацииОбъектов>
<Правило Валидное="true">
<Код>1</Код>
<Наименование>Приход</Наименование>
<ОбъектНастройки>ДокументСсылка.Приход</ОбъектНастройки>
<ОбъектМетаданныхИмя>Документ.Приход</ОбъектМетаданныхИмя>
<ПриОбработке>{handler}</ПриОбработке>
</Правило>
</ПравилаРегистрацииОбъектов>
</ПравилаРегистрации>""".encode()
    return check_registration(load_registration_rules(xml), None)


def test_registration_query_parameters() -> None:
    def check(handler: str) -> list[Issue]:
        return [i for i in _registration(handler).warnings if i.check == QUERY_PARAMETERS]

    query = 'ТекстЗапроса = "ВЫБРАТЬ Ссылка ИЗ ПланОбмена.Обмен ГДЕ Поле = &amp;ВидыЦен";'
    assert len(check(query)) == 1
    multiline = (
        'ТекстЗапроса = "ВЫБРАТЬ Ссылка // поле\n'
        '// комментарий с кавычкой "\n'
        "|ИЗ ПланОбмена.Обмен ГДЕ Поле = &amp;ВидыЦен // параметр\n"
        '|";'
    )
    assert len(check(multiline)) == 1
    assert len(check('А = ?(ТекстЗапроса = "", 1, 2);\n' + query)) == 1
    assert len(check("Если ПараметрыЗапроса = Неопределено Тогда\nКонецЕсли;\n" + query)) == 1
    assert len(check(f"Попытка\n{query}\nИсключение\nКонецПопытки;")) == 1


# --- находки ревью: склейка текстов, сравнение и присваивание в одной строке, `Выполнить("…")` --


@pytest.mark.parametrize("call", ["Выполнить(Алгоритмы.Вкл)", "Выполнить(Алгоритмы.Вкл) // вкл"])
def test_algorithm_body_after_a_call_without_semicolon(
    call: str, sides: tuple[sqlite3.Connection, ...]
) -> None:
    """Тело алгоритма приклеивается к обработчику: его первый оператор — начало оператора."""
    algorithms = '<Алгоритм Имя="Вкл"><Текст>ЗапоминатьВыгруженные = Истина;</Текст></Алгоритм>'
    extra = f"<ПередВыгрузкой>{call}</ПередВыгрузкой>"
    xml = exchange(
        pko("Номенклатура", NOMENCLATURE, NOMENCLATURE, extra=extra), algorithms=algorithms
    )
    report = run(xml, sides)
    assert len(hits(report, EXPORT_CACHE)) == 1
    assert skips(report, EXPORT_KEY) == []


def test_assignment_after_a_comparison_on_the_same_line(
    sides: tuple[sqlite3.Connection, ...],
) -> None:
    export = "<Правило><Код>В</Код><КодПравилаКонвертации>Приход</КодПравилаКонвертации></Правило>"
    posting = (
        '<ПриЗагрузке>Если РежимЗаписи = "" Тогда РежимЗаписи = "Проведение"; КонецЕсли;'
        "</ПриЗагрузке>"
    )
    report = run(exchange(pko("Приход", DOC, DOC, extra=posting), export), sides)
    assert hits(report, DOCUMENT_POSTING) == []
    assert _incoming("Если Значение = Неопределено Тогда Значение = 0; КонецЕсли;", sides) == []


def test_code_inside_execute_string(sides: tuple[sqlite3.Connection, ...]) -> None:
    pvd = (
        f"<Правило><Код>В</Код><ОбъектВыборки>{DOC}</ОбъектВыборки>"
        '<ПередОбработкойПравила>Выполнить("Отказ = Истина");</ПередОбработкойПравила></Правило>'
    )
    assert len(hits(run(exchange(pvd=pvd), sides), PVD_REFUSAL)) == 1
    extra = (
        '<ПриЗагрузке>Выполнить("Если А Тогда ОбъектМодифицирован = Ложь; КонецЕсли;");'
        "</ПриЗагрузке>"
    )
    assert len(hits(run(exchange(pko("Приход", DOC, DOC, extra=extra)), sides), MODIFIED_FLAG)) == 1
