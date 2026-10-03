"""Чтение менеджера регистрации: формы §5.2, диапазоны и неизвестные фрагменты."""

from pathlib import Path

import pytest

from kd2_rules_mcp.ed.address import escape_segment
from kd2_rules_mcp.ed.errors import EdFormatError, EdResourceLimitError
from kd2_rules_mcp.ed.model import Classification, ParseStatus
from kd2_rules_mcp.ed.registration import (
    AmbiguousRegistrationAddress,
    find_rule,
    read_registration_manager,
    read_registration_manager_text,
    rule_address,
)
from kd2_rules_mcp.ed.registration_model import HANDLER_EVENTS, XmlNode

DATA = Path(__file__).parent / "data" / "ed" / "registration"
EVENTS = tuple(event for event, _ in HANDLER_EVENTS)


def _read(name: str):
    return read_registration_manager(DATA / name)


def _elements(node: XmlNode) -> int:
    nested = sum(_elements(child) for child in node.children)
    return nested + (node.tag == "ЭлементОтбора")


def _assert_spans(document) -> None:
    text = document.source.text
    cursor = 0
    for segment in document.coverage.segments:
        assert segment.span.file_id == document.source.file_id
        assert segment.span.char_start == cursor
        assert segment.span.char_end > cursor
        cursor = segment.span.char_end
    assert cursor == len(text)
    entities = [
        *document.routines,
        *document.rules,
        *document.parameters,
        *document.dispatch_cases,
        *document.unknown,
    ]
    for entity in entities:
        assert entity.span.file_id == document.source.file_id
        assert text[entity.span.char_start : entity.span.char_end] == entity.raw_text
    for tree in document.filter_literals:
        span = tree.literal_span
        assert span.file_id == document.source.file_id
        assert text[span.char_start : span.char_end] == tree.literal_raw


def _pair(filter_xml: str, neighbor: str = "<ОтборПоСвойствамОбъекта/>") -> str:
    def quoted(value: str) -> str:
        return '"' + value.replace('"', '""') + '"'

    return f"""
Процедура ИнициализацияПравилРегистрации(ПравилаРегистрацииОбъектов, ИмяМенеджераРегистрации)
	ДобавитьПРО_Документ_Заказ(ПравилаРегистрацииОбъектов, ИмяМенеджераРегистрации);
	ДобавитьПРО_Справочник_Товары(ПравилаРегистрацииОбъектов, ИмяМенеджераРегистрации);
КонецПроцедуры

Процедура ДобавитьПРО_Документ_Заказ(ПравилаРегистрацииОбъектов, ИмяМенеджераРегистрации)
	НовоеПравило = ПравилаРегистрацииОбъектов.Добавить();
	НовоеПравило.ОбъектМетаданныхИмя = "Документ.Заказ";
	НовоеПравило.Идентификатор = "Заказ";
	НовоеПравило.ИмяМенеджераРегистрации = ИмяМенеджераРегистрации;
	УстановитьОтборы(НовоеПравило, ОтборЗаказ(), ОтборТовары());
КонецПроцедуры

Процедура ДобавитьПРО_Справочник_Товары(ПравилаРегистрацииОбъектов, ИмяМенеджераРегистрации)
	НовоеПравило = ПравилаРегистрацииОбъектов.Добавить();
	НовоеПравило.ОбъектМетаданныхИмя = "Справочник.Товары";
	НовоеПравило.Идентификатор = "Товары";
	НовоеПравило.ИмяМенеджераРегистрации = ИмяМенеджераРегистрации;
	УстановитьОтборы(НовоеПравило, "<ОтборПоСвойствамПланаОбмена/>", ОтборТовары());
КонецПроцедуры

Функция ОтборЗаказ()
	Возврат {quoted(filter_xml)};
КонецФункции

Функция ОтборТовары()
	Возврат {quoted(neighbor)};
КонецФункции

Процедура УстановитьОтборы(ПравилоРегистрации, ОтборПоСвойствамПланаОбмена, ОтборПоСвойствамОбъекта)
КонецПроцедуры
"""


def test_rules_keep_call_order_and_separate_identifiers() -> None:
    """Два ПРО одного объекта не сливаются; порядок — порядок вызовов, не объявлений."""
    document = _read("basic.bsl")
    assert document.parse_status is ParseStatus.COMPLETE
    assert document.unknown == ()
    assert [rule.procedure_name for rule in document.rules] == [
        "ДобавитьПРО_Документ_ЗаказПовтор",
        "ДобавитьПРО_Справочник_Товары",
        "ДобавитьПРО_Документ_Заказ",
    ]
    assert [rule.metadata_name.value for rule in document.rules] == [
        "Документ.Заказ",
        "Справочник.Товары",
        "Документ.Заказ",
    ]
    assert [rule.code for rule in document.rules] == ["Повтор#1", "Узел#1", "Повтор#2"]
    assert document.rules[0].span != document.rules[2].span
    assert document.rules[0].origins[0].char_start < document.rules[0].origins[1].char_start


def test_shared_literal_and_inline_filter() -> None:
    """Один литерал функции — одно дерево; литерал в аргументе читается отдельно."""
    document = _read("basic.bsl")
    first, goods, second = document.rules
    assert first.plan_filter is second.plan_filter
    assert first.object_filter is second.object_filter
    assert goods.plan_filter is not first.plan_filter
    names = [tree.routine_name for tree in document.filter_literals]
    assert names.count("ОтборПланаЗаказ") == 1
    assert "" in names
    assert goods.object_filter is not None
    assert goods.object_filter.routine_name == ""
    assert goods.plan_filter is not None
    assert [child.tag for child in goods.plan_filter.unknown_children] == ["Пометка"]


def test_empty_flag_is_not_the_tree() -> None:
    """`ПравилоПоСвойствамОбъектаПустое` хранится само по себе и не заменяет XML."""
    document = _read("basic.bsl")
    first, goods, second = document.rules
    assert first.empty_object_filter.value is True
    assert first.object_filter is not None and first.object_filter.tree is not None
    assert _elements(first.object_filter.tree) == 0
    assert goods.empty_object_filter.value is False
    assert goods.object_filter is not None and goods.object_filter.tree is not None
    assert _elements(goods.object_filter.tree) == 1
    assert second.empty_object_filter.value is False
    assert second.object_filter is not None and second.object_filter.tree is not None
    assert _elements(second.object_filter.tree) == 0


def test_quotes_and_continuations_stay_in_decoded_xml() -> None:
    """Кавычки BSL декодируются, продолжения `|` не попадают в XML."""
    document = _read("basic.bsl")
    shared = document.rules[0].plan_filter
    inline = document.rules[1].object_filter
    assert shared is not None and inline is not None
    assert '"да"' in shared.decoded_xml
    assert '""' not in shared.decoded_xml
    assert "|" not in shared.decoded_xml
    assert "|" in inline.literal_raw
    assert "<СвойствоОбъекта>НетПоля</СвойствоОбъекта>" in inline.decoded_xml
    assert "|" not in inline.decoded_xml
    assert len(inline.decoded_line_map) > 1


def test_handler_flags_and_dispatchers() -> None:
    """Пять флагов шаблона и пять диспетчеров; тела обработчиков не разбираются."""
    document = _read("basic.bsl")
    flags = document.rules[2].handler_flags
    assert [event for event, _ in flags] == list(EVENTS)
    assert all(field.value is True for _, field in flags)
    assert document.rules[2].batch.value is True
    assert [case.literal_name for case in document.dispatch_cases] == [
        "Повтор",
        "Повтор",
        "Повтор",
        "Повтор",
        "Повтор",
    ]
    names = {routine.name for routine in document.routines if "handler" in routine.roles}
    assert names == {f"ПРО_{event}" for event in EVENTS}
    handler = next(
        routine for routine in document.routines if routine.name == "ПРО_ПередОбработкой"
    )
    body_lines = range(handler.body_span.line_start, handler.body_span.line_end + 1)
    assert any(
        document.coverage.classify_line(line) is Classification.OPAQUE_CODE for line in body_lines
    )


def test_parameters_keep_date_and_leave_localization_opaque() -> None:
    document = _read("basic.bsl")
    by_name = {item.name: item for item in document.parameters}
    created = by_name["ДатаВремяСоздания"].field
    assert created.presence == "literal"
    assert created.value is not None and created.value.literal_type == "date"
    assert by_name["Наименование"].field.presence == "expression"
    assert by_name["ПланаОбмена"].field.value is not None
    assert by_name["ПланаОбмена"].field.value.literal_value == "УзелЗаказов"
    assert by_name["СинонимКонфигурации"].field.value is not None
    assert by_name["СинонимКонфигурации"].field.value.literal_value == "Узел заказов"
    manager = document.rules[0].manager_name
    assert manager.presence == "literal" and manager.value is not None
    assert manager.value.reference_parts == ("ИмяМенеджераРегистрации",)


def test_spans_coverage_and_determinism() -> None:
    path = DATA / "basic.bsl"
    document = read_registration_manager(path)
    _assert_spans(document)
    assert document.coverage.classified_ratio == 1
    again = read_registration_manager(path)
    assert again == document
    from_text = read_registration_manager_text(path.read_text(encoding="utf-8"), path=path)
    assert from_text == document


def test_addresses_escape_duplicate_suffix_and_ambiguity() -> None:
    document = _read("basic.bsl")
    goods = document.rules[1]
    assert goods.qualified_id == escape_segment("Узел#1") == "Узел%231"
    assert rule_address(goods) == "Регистрация/ПРО/Узел%231"
    assert find_rule(document, rule_address(document.rules[0])).code == "Повтор#1"
    with pytest.raises(AmbiguousRegistrationAddress) as error:
        find_rule(document, "Регистрация/ПРО/Повтор")
    assert error.value.candidates == (
        "Регистрация/ПРО/Повтор#1",
        "Регистрация/ПРО/Повтор#2",
    )
    with pytest.raises(KeyError):
        find_rule(document, "Регистрация/ПРО/нет")


def test_broken_filter_keeps_the_neighbor_rule() -> None:
    document = _read("broken.bsl")
    assert [rule.identifier for rule in document.rules] == ["ЗаказБитый", "ТоварыЦелые"]
    broken = document.rules[0].plan_filter
    assert broken is not None and broken.error == "повреждённый XML отбора"
    assert document.rules[0].object_filter is not None
    assert document.rules[0].object_filter.error is None
    assert document.rules[1].plan_filter is not None
    assert document.rules[1].plan_filter.error is None
    assert any(item.reason == "повреждённый XML отбора" for item in document.unknown)
    assert document.parse_status is ParseStatus.PARTIAL


def test_concatenation_call_loop_and_external_helper_are_unknown() -> None:
    document = _read("unknown.bsl")
    assert [rule.metadata_name.value for rule in document.rules] == ["Документ.Заказ"]
    assert document.rules[0].plan_filter is None
    assert document.rules[0].object_filter is None
    reasons = {item.reason for item in document.unknown}
    assert "функция не возвращает литерал отбора" in reasons
    assert "вычисляемый или внешний вызов отбора" in reasons
    assert "цикл генерации правил" in reasons
    assert "правило внутри цикла или условия" in reasons
    assert "неизвестный оператор инициализации" in reasons
    assert "неизвестный метод" in reasons
    assert not any(
        tree.decoded_xml.startswith("<ОтборПоСвойствам") for tree in document.filter_literals
    )
    unknown = next(item for item in document.unknown if item.reason == "неизвестный метод")
    assert unknown.span.file_id == document.source.file_id
    assert unknown.span.line_start >= 1


def test_unknown_assignment_is_preserved() -> None:
    text = """
Процедура ИнициализацияПравилРегистрации(ПравилаРегистрацииОбъектов, ИмяМенеджераРегистрации)
	ДобавитьПРО_Документ_Заказ(ПравилаРегистрацииОбъектов, ИмяМенеджераРегистрации);
КонецПроцедуры

Процедура ДобавитьПРО_Документ_Заказ(ПравилаРегистрацииОбъектов, ИмяМенеджераРегистрации)
	НовоеПравило = ПравилаРегистрацииОбъектов.Добавить();
	НовоеПравило.ОбъектМетаданныхИмя = "Документ.Заказ";
	НовоеПравило.Идентификатор = "Заказ";
	НовоеПравило.Комментарий = "заметка";
	НовоеПравило.ИмяМенеджераРегистрации = ИмяМенеджераРегистрации;
КонецПроцедуры
"""
    document = read_registration_manager_text(text)
    assert len(document.rules) == 1
    assert document.rules[0].preserved_assignments[0][0] == "Комментарий"
    assert any(item.reason == "неизвестное присваивание" for item in document.unknown)


def test_unclosed_literal_duplicate_and_empty_module_are_format_errors() -> None:
    with pytest.raises(EdFormatError):
        read_registration_manager_text('Функция Отбор()\n\tВозврат "abc\nКонецФункции\n')
    with pytest.raises(EdFormatError):
        read_registration_manager_text("")
    duplicate = """
Процедура ИнициализацияПравилРегистрации(ПравилаРегистрацииОбъектов, ИмяМенеджераРегистрации)
КонецПроцедуры
Процедура ИнициализацияПравилРегистрации(ПравилаРегистрацииОбъектов, ИмяМенеджераРегистрации)
КонецПроцедуры
"""
    with pytest.raises(EdFormatError):
        read_registration_manager_text(duplicate)


def test_dtd_depth_and_size_do_not_drop_the_neighbor(monkeypatch: pytest.MonkeyPatch) -> None:
    dtd = _pair(
        '<ОтборПоСвойствамПланаОбмена><!DOCTYPE foo [<!ENTITY x "y">]>'
        "</ОтборПоСвойствамПланаОбмена>"
    )
    document = read_registration_manager_text(dtd)
    assert [rule.identifier for rule in document.rules] == ["Заказ", "Товары"]
    assert document.rules[0].plan_filter is not None
    assert document.rules[0].plan_filter.error == "DTD и сущности в отборе запрещены"
    assert document.rules[1].plan_filter is not None
    assert document.rules[1].plan_filter.error is None

    deep = _pair(
        "<ОтборПоСвойствамПланаОбмена><Группа><ЭлементОтбора>"
        "<СвойствоПланаОбмена>A</СвойствоПланаОбмена></ЭлементОтбора></Группа>"
        "</ОтборПоСвойствамПланаОбмена>"
    )
    monkeypatch.setattr("kd2_rules_mcp.ed.registration.MAX_XML_DEPTH", 2)
    document = read_registration_manager_text(deep)
    assert document.rules[0].plan_filter is not None
    assert document.rules[0].plan_filter.error == "глубина XML отбора превышает 2"
    assert document.rules[1].identifier == "Товары"

    wide = _pair("<ОтборПоСвойствамПланаОбмена>" + (" " * 40) + "</ОтборПоСвойствамПланаОбмена>")
    monkeypatch.setattr("kd2_rules_mcp.ed.registration.MAX_FILTER_BYTES", 60)
    document = read_registration_manager_text(wide)
    assert document.rules[0].plan_filter is not None
    assert document.rules[0].plan_filter.error == "размер литерала отбора превышает 4 МиБ"
    assert document.rules[1].plan_filter is not None
    assert document.rules[1].plan_filter.tree is not None


def test_input_size_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("kd2_rules_mcp.ed.registration.MAX_BYTES", 20)
    with pytest.raises(EdResourceLimitError):
        read_registration_manager_text("x" * 40)


_HELPER = """
Процедура УстановитьОтборы(ПравилоРегистрации, ОтборПоСвойствамПланаОбмена, ОтборПоСвойствамОбъекта)
КонецПроцедуры
"""
_INIT = (
    "Процедура ИнициализацияПравилРегистрации"
    "(ПравилаРегистрацииОбъектов, ИмяМенеджераРегистрации)\n"
)
_FUNCS = """
Функция ОтборПлана()
	Возврат "<ОтборПоСвойствамПланаОбмена/>";
КонецФункции

Функция ОтборОбъекта()
	Возврат "<ОтборПоСвойствамОбъекта/>";
КонецФункции
"""


def _call(name: str) -> str:
    return f"\tДобавитьПРО_{name}(ПравилаРегистрацииОбъектов, ИмяМенеджераРегистрации);\n"


def _proc(name: str, ident: str, body: str = "", *, filters: str | None = "") -> str:
    if filters == "":
        filters = "\tУстановитьОтборы(НовоеПравило, ОтборПлана(), ОтборОбъекта());\n"
    return (
        f"Процедура ДобавитьПРО_{name}(ПравилаРегистрацииОбъектов, ИмяМенеджераРегистрации)\n"
        "\tНовоеПравило = ПравилаРегистрацииОбъектов.Добавить();\n"
        '\tНовоеПравило.ИмяПланаОбмена = "УзелЗаказов";\n'
        '\tНовоеПравило.ОбъектМетаданныхИмя = "Документ.Заказ";\n'
        f'\tНовоеПравило.Идентификатор = "{ident}";\n'
        "\tНовоеПравило.ИмяМенеджераРегистрации = ИмяМенеджераРегистрации;\n"
        f"{body}{filters or ''}"
        "КонецПроцедуры\n"
    )


def _mod(
    init_body: str,
    rules: str,
    *,
    extra: str = "",
    functions: str = _FUNCS,
    helper: bool = True,
    parameters: str = "",
) -> str:
    return (
        parameters
        + _INIT
        + init_body
        + "КонецПроцедуры\n"
        + rules
        + functions
        + (_HELPER if helper else "")
        + extra
    )


def test_preprocessor_is_unknown_and_region_is_silent() -> None:
    """`#Если` не применяет обе ветки; `#Область` и поле вне директивы читаются."""
    blocked = read_registration_manager_text(
        _mod(
            "#Если Сервер Тогда\n" + _call("А") + "#Иначе\n" + _call("Б") + "#КонецЕсли\n",
            _proc("А", "А") + _proc("Б", "Б"),
        )
    )
    assert blocked.parse_status is ParseStatus.PARTIAL
    assert blocked.rules == ()
    assert any(item.reason == "директива препроцессора" for item in blocked.unknown)

    region = read_registration_manager_text(
        _mod("#Область Правила\n" + _call("А") + "#КонецОбласти\n", _proc("А", "А"))
    )
    assert region.parse_status is ParseStatus.COMPLETE
    assert [rule.identifier for rule in region.rules] == ["А"]

    hidden = read_registration_manager_text(
        _mod(
            _call("А"),
            _proc(
                "А",
                "А",
                '#Если Клиент Тогда\n\tНовоеПравило.ИмяРеквизитаФлага = "ВыгружатьЗаказы";\n'
                "#КонецЕсли\n",
            ),
        )
    )
    assert hidden.rules[0].unload_flag.presence == "absent"
    assert hidden.parse_status is ParseStatus.PARTIAL
    plain = read_registration_manager_text(
        _mod(_call("А"), _proc("А", "А", '\tНовоеПравило.ИмяРеквизитаФлага = "ВыгружатьЗаказы";\n'))
    )
    assert plain.parse_status is ParseStatus.COMPLETE
    assert plain.rules[0].unload_flag.value == "ВыгружатьЗаказы"


def test_swapped_filter_roots_are_unknown() -> None:
    swapped = read_registration_manager_text(
        _mod(
            _call("А"),
            _proc(
                "А",
                "А",
                filters="\tУстановитьОтборы(НовоеПравило, ОтборОбъекта(), ОтборПлана());\n",
            ),
        )
    )
    assert swapped.parse_status is ParseStatus.PARTIAL
    plan, obj = swapped.rules[0].plan_filter, swapped.rules[0].object_filter
    assert plan is not None and obj is not None
    assert plan.error == "корень отбора не соответствует позиции аргумента"
    assert obj.error == "корень отбора не соответствует позиции аргумента"
    assert any(
        item.reason == "корень отбора не соответствует позиции аргумента"
        for item in swapped.unknown
    )

    clean = read_registration_manager_text(_mod(_call("А"), _proc("А", "А")))
    assert clean.parse_status is ParseStatus.COMPLETE
    assert clean.rules[0].plan_filter is not None and clean.rules[0].plan_filter.error is None
    assert clean.rules[0].plan_filter.root_tag == "ОтборПоСвойствамПланаОбмена"
    assert clean.rules[0].object_filter is not None
    assert clean.rules[0].object_filter.root_tag == "ОтборПоСвойствамОбъекта"


def test_conditions_try_and_return_are_not_unconditional() -> None:
    hidden = read_registration_manager_text(
        _mod(
            _call("А"),
            _proc(
                "А",
                "А",
                "\tЕсли Ложь Тогда\n"
                '\t\tНовоеПравило.ИмяРеквизитаФлага = "ВыгружатьЗаказы";\n'
                "\tКонецЕсли;\n",
            ),
        )
    )
    assert hidden.rules[0].unload_flag.presence == "absent"
    assert hidden.parse_status is ParseStatus.PARTIAL
    shown = read_registration_manager_text(
        _mod(_call("А"), _proc("А", "А", '\tНовоеПравило.ИмяРеквизитаФлага = "ВыгружатьЗаказы";\n'))
    )
    assert shown.rules[0].unload_flag.value == "ВыгружатьЗаказы"
    assert shown.parse_status is ParseStatus.COMPLETE

    attempted = read_registration_manager_text(
        _mod(
            "\tПопытка\n" + _call("А") + "\tИсключение\n" + _call("Б") + "\tКонецПопытки;\n",
            _proc("А", "А") + _proc("Б", "Б"),
        )
    )
    assert attempted.rules == ()
    assert any(item.reason == "неизвестная попытка" for item in attempted.unknown)

    returned = read_registration_manager_text(
        _mod(_call("А") + "\tВозврат;\n" + _call("Б"), _proc("А", "А") + _proc("Б", "Б"))
    )
    assert [rule.identifier for rule in returned.rules] == ["А"]
    assert any(item.reason == "оператор после возврата" for item in returned.unknown)


def test_rule_codes_stay_unique_when_identifier_contains_suffix() -> None:
    document = read_registration_manager_text(
        _mod(
            _call("А") + _call("Б") + _call("В"),
            _proc("А", "X") + _proc("Б", "X") + _proc("В", "X#1"),
        )
    )
    codes = [rule.code for rule in document.rules]
    qualified = [rule.qualified_id for rule in document.rules]
    assert len(codes) == len(set(codes)) == 3
    assert len(qualified) == len(set(qualified)) == 3
    assert document.rules[2].qualified_id == "X%231"
    assert document.rules[0].code != document.rules[2].code


def test_unrecognized_parameter_assignment_drops_stale_literal() -> None:
    stale = """
Функция ПараметрыРегистрации() Экспорт
	Результат = Новый Структура();
	План = "УзелЗаказов";
	План = План + "Старый";
	Результат.Вставить("ПланаОбмена", План);
	Возврат Результат;
КонецФункции
"""
    document = read_registration_manager_text(stale + _mod(_call("А"), _proc("А", "А")))
    plan = next(item for item in document.parameters if item.name == "ПланаОбмена")
    assert plan.field.presence != "literal"
    assert plan.field.value is None

    fresh = """
Функция ПараметрыРегистрации() Экспорт
	Результат = Новый Структура();
	План = "УзелЗаказов";
	Результат.Вставить("ПланаОбмена", План);
	Возврат Результат;
КонецФункции
"""
    clean = read_registration_manager_text(fresh + _mod(_call("А"), _proc("А", "А")))
    kept = next(item for item in clean.parameters if item.name == "ПланаОбмена")
    assert kept.field.presence == "literal"
    assert kept.field.value is not None
    assert kept.field.value.literal_value == "УзелЗаказов"


def test_filter_helper_and_manager_parameter() -> None:
    missing = read_registration_manager_text(_mod(_call("А"), _proc("А", "А"), helper=False))
    assert missing.rules[0].plan_filter is None
    assert missing.rules[0].object_filter is None
    assert any(item.reason == "внешний помощник УстановитьОтборы" for item in missing.unknown)
    present = read_registration_manager_text(_mod(_call("А"), _proc("А", "А")))
    assert present.parse_status is ParseStatus.COMPLETE
    assert present.rules[0].plan_filter is not None and present.rules[0].plan_filter.error is None

    foreign = read_registration_manager_text(
        _mod(
            _call("А"),
            _proc("А", "А").replace("= ИмяМенеджераРегистрации;", "= ЧужоеИмя;"),
        )
    )
    assert foreign.rules[0].manager_name.presence != "literal"
    assert any(item.reason == "неизвестное присваивание" for item in foreign.unknown)
    own = read_registration_manager_text(_mod(_call("А"), _proc("А", "А")))
    assert own.rules[0].manager_name.presence == "literal"


def test_xml_comment_is_not_a_dtd_and_prolog_is_uniform() -> None:
    commented = _pair("<ОтборПоСвойствамПланаОбмена><!-- заметка --></ОтборПоСвойствамПланаОбмена>")
    document = read_registration_manager_text(commented)
    tree = document.rules[0].plan_filter
    assert tree is not None and tree.error is None and tree.tree is not None
    assert not any("DTD" in item.reason for item in document.unknown)

    prolog = '<?xml version="1.0"?><ОтборПоСвойствамПланаОбмена/>'

    def quoted(value: str) -> str:
        return '"' + value.replace('"', '""') + '"'

    via_function = read_registration_manager_text(_pair(prolog))
    via_argument = read_registration_manager_text(
        _mod(
            _call("А"),
            _proc(
                "А",
                "А",
                filters=(f"\tУстановитьОтборы(НовоеПравило, {quoted(prolog)}, ОтборОбъекта());\n"),
            ),
            functions=(
                'Функция ОтборОбъекта()\n\tВозврат "<ОтборПоСвойствамОбъекта/>";\nКонецФункции\n'
            ),
        )
    )
    assert via_function.rules[0].plan_filter is not None
    assert via_argument.rules[0].plan_filter is not None
    assert via_function.rules[0].plan_filter.root_tag == via_argument.rules[0].plan_filter.root_tag
    assert via_function.rules[0].plan_filter.error is None
    assert via_argument.rules[0].plan_filter.error is None


def test_field_names_ignore_case() -> None:
    text = _mod(_call("А"), _proc("А", "А")).replace(
        "НовоеПравило.Идентификатор", "НовоеПравило.идентификатор"
    )
    document = read_registration_manager_text(text)
    assert document.parse_status is ParseStatus.COMPLETE
    assert document.rules[0].identifier == "А"
    canonical = read_registration_manager_text(_mod(_call("А"), _proc("А", "А")))
    assert canonical.rules[0].identifier == "А"


def test_line_numbers_split_only_on_lf(tmp_path: Path) -> None:
    source = (DATA / "equal.bsl").read_text(encoding="utf-8")
    shifted = source.replace(
        "Функция ОтборПлана()", "// разделитель \u2028 внутри\nФункция ОтборПлана()"
    )
    document = read_registration_manager_text(shifted)
    tree = document.rules[0].plan_filter
    assert tree is not None
    assert tree.literal_span.line_start == shifted[: tree.literal_span.char_start].count("\n") + 1

    path = tmp_path / "crlf.bsl"
    path.write_bytes(("\ufeff" + source.replace("\n", "\r\n")).encode("utf-8"))
    bom = read_registration_manager(path)
    assert bom.source.bom is True
    assert bom.source.newline == "\r\n"
    assert bom.parse_status is ParseStatus.COMPLETE
    assert bom.rules[0].identifier == "ЗаказВыгружать"


def test_module_without_initialization_is_format_error() -> None:
    with pytest.raises(EdFormatError, match="ИнициализацияПравилРегистрации"):
        read_registration_manager_text(_FUNCS)


def test_filter_function_rejects_extra_statements() -> None:
    two_returns = """
Функция ОтборПлана()
	Если Ложь Тогда
		Возврат "<ОтборПоСвойствамОбъекта/>";
	КонецЕсли;
	Возврат "<ОтборПоСвойствамПланаОбмена/>";
КонецФункции
"""
    before = """
Функция ОтборПлана()
	А = 1;
	Возврат "<ОтборПоСвойствамПланаОбмена/>";
КонецФункции
"""
    for functions in (two_returns, before):
        document = read_registration_manager_text(
            _mod(
                _call("А"),
                _proc("А", "А"),
                functions=functions + _FUNCS.split("Функция ОтборПлана()")[0],
            )
        )
        # ОтборОбъекта остаётся в _FUNCS; ОтборПлана подменён и не является литералом.
        assert document.rules[0].plan_filter is None
        assert any(
            item.reason == "функция не возвращает литерал отбора" for item in document.unknown
        )
    clean = read_registration_manager_text(_mod(_call("А"), _proc("А", "А")))
    assert clean.rules[0].plan_filter is not None and clean.rules[0].plan_filter.error is None


def test_variable_and_parameterized_filter_call_are_unknown() -> None:
    variable = read_registration_manager_text(
        _mod(
            _call("А"),
            _proc(
                "А",
                "А",
                filters=(
                    "\tОтбор = ОтборПлана();\n"
                    "\tУстановитьОтборы(НовоеПравило, Отбор, ОтборОбъекта());\n"
                ),
            ),
        )
    )
    assert variable.rules[0].plan_filter is None
    assert any(item.reason == "вычисляемый или внешний вызов отбора" for item in variable.unknown)
    called = read_registration_manager_text(
        _mod(
            _call("А"),
            _proc(
                "А",
                "А",
                filters="\tУстановитьОтборы(НовоеПравило, ОтборПлана(1), ОтборОбъекта());\n",
            ),
        )
    )
    assert called.rules[0].plan_filter is None
    assert any(item.reason == "вычисляемый или внешний вызов отбора" for item in called.unknown)
    clean = read_registration_manager_text(_mod(_call("А"), _proc("А", "А")))
    assert clean.rules[0].plan_filter is not None


def test_external_entity_in_filter_is_rejected() -> None:
    document = read_registration_manager_text(
        _pair(
            '<ОтборПоСвойствамПланаОбмена><!DOCTYPE x [<!ENTITY e SYSTEM "file:///secret">]>'
            "</ОтборПоСвойствамПланаОбмена>"
        )
    )
    assert document.rules[0].plan_filter is not None
    assert document.rules[0].plan_filter.error == "DTD и сущности в отборе запрещены"
    assert document.rules[1].plan_filter is not None and document.rules[1].plan_filter.error is None
