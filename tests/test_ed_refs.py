"""Индекс литеральных ссылок: формы §2, не-кандидаты, диапазоны, направление."""

from pathlib import Path

from kd_rules_mcp.ed import forms, read_manager_text
from kd_rules_mcp.ed.refs import build_references

DATA = Path(__file__).parent / "data" / "ed"
BASE = (DATA / "checks_base.bsl").read_text(encoding="utf-8")
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
SAMPLES = r"""
    Текст = "ПКОПоИмени";
    // комментарий ПКОПоИмени не индексируется
    ОбменДаннымиXDTOСервер.ПКОПоИмени( // не код
        КомпонентыОбмена, "Товар");
    ОбменДаннымиXDTOСервер.ПКОПоИмени(КомпонентыОбмена, "  Товар  ");
    ОбменДаннымиXDTOСервер.ПКОПоИмени(КомпонентыОбмена, "");
    ОбменДаннымиXDTOСервер.пкопоимени(КомпонентыОбмена, "Другой");
    ОбменДаннымиXDTOСервер.ПКОПоИмени(КомпонентыОбмена, ИмяПравила);
    ОбменДаннымиXDTOСервер.ПКОПоИмени(КомпонентыОбмена, СтрШаблон("А", 1, 2));
    ОбменДаннымиXDTOСервер.ПКОПоИмени(КомпонентыОбмена, "Товар""X");
    ОбменДаннымиXDTOСервер.ПКОПоИмени(КомпонентыОбмена, "Строка
    |продолжения");
    КомпонентыОбмена.ПравилаКонвертацииОбъектов.Найти("Товар", "ИмяПКО");
    Инструкция = Новый Структура("ИмяПКО, Код", "Товар", "1");
    Инструкция = Новый Структура("Код, ИмяПКО", "1", "Заказ");
    Инструкция = Новый Структура("ИмяПКО", "А, Б");
    Инструкция = Новый Структура(СписокКлючей, "Товар");
    Инструкция.Вставить("ИмяПКО", "Товар");
    Инструкция.ИмяПКО = "Товар";
    Если Инструкция.ИмяПКО = "Товар" Тогда
    КонецЕсли;
    МойПомощник.ПКОПоИмени(КомпонентыОбмена, "Чужое");
    ИспользованиеПКО.Товар = Ложь;
    ИспользованиеПКО.Вставить("Товар", Истина);
    ИспользованиеПКО.Свойство("Товар", Значение);
    ИспользованиеПКО.Получить("Товар");
    ИспользованиеПКО.Удалить("Товар");
    ИспользованиеПКО["Товар"] = Ложь;
    ИспользованиеПКО[ИмяПравила] = Ложь;
    ЗначениеИндекса = ИспользованиеПКО["Товар"];
    ПолученныеДанные.ДополнительныеСвойства.Ключ = 1;
    ПолученныеДанные.ДополнительныеСвойства.Вставить("КлючВызова", 1);
    ПолученныеДанные.ДополнительныеСвойства["КлючИндекса"] = 1;
    КомпонентыОбмена.ПараметрыКонвертации.Лимит = 1;
    КомпонентыОбмена.ПараметрыКонвертации.Вставить("ЛимитВызова", 1);
    КомпонентыОбмена.ПараметрыКонвертации["ЛимитИндекса"] = 1;
    ДанныеXDTO.Код = 1;
    Значение = ДанныеXDTO.Код;
    ДанныеXDTO.Вставить("КодВызова", 1);
    ДанныеXDTO["КодИндекса"] = 1;
    ПолученныеДанные.Наименование = "А";
    ПолученныеДанные.Вставить("ИмяВызова", "А");
    ПолученныеДанные["ИмяИндекса"] = "А";
"""


def _module(event: str = "", *, extra: str = "") -> str:
    helpers = "\n".join(forms.helper_forms(name, 2)[0] for name in ("ДобавитьПКС", "ДобавитьПКТЧ"))
    text = BASE.replace("// <event>", event, 1) + extra + "\n" + helpers + "\n"
    return text


def _indexed(event: str = SAMPLES, *, extra: str = ""):
    text = _module(event, extra=extra)
    document = read_manager_text(text)
    return text, document, build_references(document)


def test_each_form_preserves_name_case_and_skips_non_candidates():
    _text, document, index = _indexed()
    found = [(item.kind, item.form, item.name, item.access) for item in index.entries]
    assert found == [
        ("pko_lookup", "lookup", "Товар", "read"),
        ("pko_lookup", "lookup", "  Товар  ", "read"),
        ("pko_lookup", "lookup", "", "read"),
        ("pko_lookup", "lookup", "Другой", "read"),
        ("pko_lookup", "lookup", None, "read"),
        ("pko_lookup", "lookup", None, "read"),
        ("pko_lookup", "lookup", 'Товар"X', "read"),
        ("pko_lookup", "lookup", "Строка\nпродолжения", "read"),
        ("pko_lookup", "find", "Товар", "read"),
        ("instruction_rule", "constructor", "Товар", "write"),
        ("instruction_rule", "constructor", "Заказ", "write"),
        ("instruction_rule", "constructor", "А, Б", "write"),
        ("instruction_rule", "insert", "Товар", "write"),
        ("instruction_rule", "assignment", "Товар", "write"),
        ("pod_use", "member", "Товар", "write"),
        ("pod_use", "insert", "Товар", "write"),
        ("pod_use", "call", "Товар", "read"),
        ("pod_use", "call", "Товар", "read"),
        ("pod_use", "call", "Товар", "write"),
        ("pod_use", "index", "Товар", "write"),
        ("pod_use", "index", None, "write"),
        ("pod_use", "index", "Товар", "read"),
        ("additional_key", "member", "Ключ", "write"),
        ("additional_key", "call", "КлючВызова", "write"),
        ("additional_key", "index", "КлючИндекса", "write"),
        ("parameter", "member", "Лимит", "write"),
        ("parameter", "call", "ЛимитВызова", "write"),
        ("parameter", "index", "ЛимитИндекса", "write"),
        ("format_property", "member", "Код", "write"),
        ("format_property", "member", "Код", "read"),
        ("format_property", "call", "КодВызова", "write"),
        ("format_property", "index", "КодИндекса", "write"),
        ("received_property", "member", "Наименование", "write"),
        ("received_property", "call", "ИмяВызова", "write"),
        ("received_property", "index", "ИмяИндекса", "write"),
    ]
    names = {item.name for item in index.entries}
    assert "ПКОПоИмени" not in names
    assert "Чужое" not in names
    assert "СписокКлючей" not in names
    # Сравнение в Если и вычисляемый список ключей ссылкой не становятся.
    assert sum(item.form == "assignment" for item in index.entries) == 1
    assert sum(item.form == "constructor" for item in index.entries) == 3
    event = next(routine for routine in document.routines if routine.name == "ПередКонвертацией")
    assert {item.owner_id for item in index.entries} == {event.entity_id}
    assert [item.ordinal for item in index.entries] == list(range(1, len(index.entries) + 1))
    assert index.unparsed_by_kind["pko_lookup"] == 2
    assert index.unparsed_by_kind["pod_use"] == 1
    assert index.known_by_kind["received_property"] == 3
    # ДополнительныеСвойства специальнее ПолученныеДанные: второе упоминание не заводится.
    assert all(item.name != "ДополнительныеСвойства" for item in index.entries)
    assert not any(
        item.kind == "received_property" and item.name == "Ключ" for item in index.entries
    )


def test_span_is_exact_source_slice_including_multiline_literal():
    _text, document, index = _indexed()
    source = document.files[0]
    multiline = next(item for item in index.entries if item.name == "Строка\nпродолжения")
    quoted = next(item for item in index.entries if item.name == 'Товар"X')
    assert source.text[multiline.span.char_start : multiline.span.char_end] == multiline.raw
    assert "|" in multiline.raw and multiline.raw.startswith('"')
    assert quoted.raw == '"Товар""X"'
    assert multiline.span.line_start < multiline.span.line_end
    for item in index.entries:
        assert source.text[item.span.char_start : item.span.char_end] == item.raw


def test_support_and_dispatcher_are_not_indexed():
    extra_support = BASE.replace(
        "// <fill-pko>",
        'ОбменДаннымиXDTOСервер.ПКОПоИмени(КомпонентыОбмена, "ИзСлужебного");',
        1,
    )
    text = extra_support.replace(
        "// <proc>",
        'ОбменДаннымиXDTOСервер.ПКОПоИмени(КомпонентыОбмена, "ИзДиспетчера");',
        1,
    )
    helpers = "\n".join(forms.helper_forms(name, 2)[0] for name in ("ДобавитьПКС", "ДобавитьПКТЧ"))
    document = read_manager_text(text + "\n" + helpers + "\n")
    names = {item.name for item in build_references(document).entries}
    assert "ИзСлужебного" not in names
    assert "ИзДиспетчера" not in names


def test_direction_comes_from_rule_use_and_does_not_cross_calls():
    send = _module(
        "",
        extra="""
Процедура Обработать(ДанныеИБ, ДанныеXDTO, КомпонентыОбмена, СтекВыгрузки)
    ДанныеXDTO.Код = 1;
КонецПроцедуры
#Область Алгоритмы
Процедура Свободный()
    ДанныеXDTO.Код = 1;
КонецПроцедуры
#КонецОбласти
""",
    )
    send = send.replace(
        "// <fill-pko>\n    ДобавитьПКО_Товар(ПравилаКонвертации);",
        """Если НаправлениеОбмена = "Отправка" Тогда
        ДобавитьПКО_Товар(ПравилаКонвертации);
    КонецЕсли;""",
        1,
    )
    send = send.replace("// <pko>", 'ПравилоКонвертации.ПриОтправкеДанных = "Обработать";', 1)
    send = send.replace(
        "// <proc>",
        _ROUTE,
        1,
    )
    document = read_manager_text(send)
    index = build_references(document)
    handler = next(item for item in index.entries if item.direction == "send")
    free = next(
        item
        for item in index.entries
        if item.owner_id.endswith(str(document.routines[-1].span.char_start))
        or item.direction is None
    )
    assert handler.direction == "send"
    assert any(item.direction is None for item in index.entries)
    receive = send.replace('"Отправка"', '"Получение"', 1)
    received = build_references(read_manager_text(receive))
    assert {item.direction for item in received.entries} == {"receive", None}
    both = _module(
        "",
        extra="""
Процедура Обработать(ДанныеИБ, ДанныеXDTO, КомпонентыОбмена, СтекВыгрузки)
    ДанныеXDTO.Код = 1;
КонецПроцедуры
""",
    ).replace("// <pko>", 'ПравилоКонвертации.ПриОтправкеДанных = "Обработать";', 1)
    both = both.replace(
        "// <proc>",
        _ROUTE,
        1,
    )
    assert any(
        item.direction == "both" for item in build_references(read_manager_text(both)).entries
    )
    assert free.direction is None or handler.direction == "send"


def test_union_of_send_and_receive_is_both():
    text = _module(
        "",
        extra="""
Процедура ДобавитьПКО_Запас(ПравилаКонвертации)
"""
        + _INIT
        + """    ПравилоКонвертации.ИмяПКО = "Запас";
    ПравилоКонвертации.ОбъектДанных = Метаданные.Справочники.Товары;
    ПравилоКонвертации.ОбъектФормата = "Справочник.Запас";
    ПравилоКонвертации.ПриОтправкеДанных = "Обработать";
КонецПроцедуры
Процедура Обработать(ДанныеИБ, ДанныеXDTO, КомпонентыОбмена, СтекВыгрузки)
    ДанныеXDTO.Код = 1;
КонецПроцедуры
""",
    )
    text = text.replace(
        "// <fill-pko>\n    ДобавитьПКО_Товар(ПравилаКонвертации);",
        """Если НаправлениеОбмена = "Отправка" Тогда
        ДобавитьПКО_Товар(ПравилаКонвертации);
    ИначеЕсли НаправлениеОбмена = "Получение" Тогда
        ДобавитьПКО_Запас(ПравилаКонвертации);
    КонецЕсли;""",
        1,
    )
    text = text.replace("// <pko>", 'ПравилоКонвертации.ПриОтправкеДанных = "Обработать";', 1)
    text = text.replace(
        "// <proc>",
        _ROUTE,
        1,
    )
    index = build_references(read_manager_text(text))
    assert {item.direction for item in index.entries} == {"both"}


def test_bom_crlf_raw_matches_stored_text_and_repeat_is_stable():
    text = "\ufeff" + _module(SAMPLES).replace("\n", "\r\n")
    first = read_manager_text(text, file_id="source")
    index = build_references(first)
    assert first.files[0].bom and first.files[0].newline == "\r\n"
    source = first.files[0]
    for item in index.entries:
        assert source.text[item.span.char_start : item.span.char_end] == item.raw
    again = build_references(first)
    assert index == again
    assert first == read_manager_text(text, file_id="source")


def test_keys_without_value_are_not_a_reference():
    """Конструктор и Вставить без значения имени правила не задают: ссылка — присваивание."""
    event = """
    Инструкция = Новый Структура("Значение,ИмяПКО");
    Инструкция.ИмяПКО = "Товар";
    Вторая = Новый Структура("Значение, ИмяПКО", Ссылка);
    Вторая.Вставить("ИмяПКО");
"""
    _text, _document, index = _indexed(event)
    rules = [item for item in index.entries if item.kind == "instruction_rule"]
    assert [(item.form, item.name) for item in rules] == [("assignment", "Товар")]
    assert index.unparsed_by_kind["instruction_rule"] == 0
