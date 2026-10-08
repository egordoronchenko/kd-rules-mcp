"""Статический состав и связи: тела не сертифицируются, обращения дают подсказку."""

import pytest

from kd_rules_mcp.ed.layer_model import LayerDescriptor, LayerHandlerBinding, rule_view
from kd_rules_mcp.ed.layer_reader import read_extension_text
from kd_rules_mcp.ed.layers import compose_manager
from kd_rules_mcp.ed.reader import read_manager_text
from kd_rules_mcp.validation.ed_layers import validate_effective_links, validate_layers
from kd_rules_mcp.validation.ed_projection import effective_document, select_context
from tests.test_ed_layers_handlers import (
    EXTENSION,
    SEND_BODY,
    SEND_CALL,
    base_document,
    overlay,
    unknown_lines,
)


@pytest.mark.parametrize(
    "statement,hint",
    [
        ('Выполнить("КомпонентыОбмена.ПравилаКонвертацииОбъектов.Очистить()");', False),
        ('Х = Вычислить("1");', False),
        ("Запрос.Выполнить(); Модуль.Любой(КомпонентыОбмена);", False),
        (
            "#Область Тело\nМодуль.Любой(КомпонентыОбмена.ПараметрыКонвертации);\n#КонецОбласти",
            False,
        ),
        ('Сообщить("ПравилаКонвертацииОбъектов"); // ПравилаОбработкиДанных\nА = 1;', False),
        ("К.ПравилаКонвертацииОбъектов.Очистить();", True),
        ("К.ПравилаОбработкиДанных.Количество();", True),
        ("К.ПравилаКонвертацииПредопределенныхДанных.Очистить();", True),
        (
            'П = ?(Истина, ОбменДаннымиXDTOСервер.ПКОПоИмени(К, "Товар"), Неопределено); '
            '~М: П.ИмяПКО = "Х";',
            True,
        ),
        ('П = ОбменДаннымиXDTOСервер.ПОДПоТипуОбъектаXDTO(К, "Т");', True),
        ('П = ОбменДаннымиXDTOСервер.ПОДПоТипуСсылкиXDTO(К, "Т");', True),
        ("П = ОбменДаннымиXDTOСервер.ИнициализироватьПравилоКонвертацииОбъекта(К);", True),
        ('П = ОбменДаннымиXDTOСервер.ПОДПоИмени(К, "Т");', False),
        ('П = ОбменДаннымиXDTOСервер.ПОДПоОбъектуМетаданных(К, "Т");', False),
    ],
)
def test_opaque_body_and_lexical_hint(statement, hint):
    layered, reading = overlay(EXTENSION.replace(SEND_BODY, statement))
    assert layered.status == "complete" and not layered.skipped
    assert unknown_lines(reading) == 0
    assert all(rule_view(c).certainty == "known" for c in layered.contexts)
    issues = validate_layers(layered, context=select_context(layered)).issues
    assert bool(issues) is hint
    if hint:
        assert len(issues) == 1
        issue = issues[0]
        assert (
            issue.check == "ed.layer.handler.touches_rules"
            and issue.level.value == "предупреждение"
        )
        assert issue.address == "Слой/L01-Обработчики/Обработчик/Отправить"
        assert "состав правил во время обмена может отличаться от показанного" in issue.message
        assert f"{reading.source.path}:" in issue.message


@pytest.mark.parametrize(
    "body,hint",
    [
        ("Возврат К.ПравилаКонвертацииОбъектов;", True),
        ("Возврат К;", False),
        ('Возврат ОбменДаннымиXDTOСервер.ПКОПоИмени(К, "Товар");', True),
        ("Помощник(К); Возврат К;", False),
    ],
)
def test_own_function_and_recursive_helper_are_opaque(body, hint):
    text = EXTENSION.replace(SEND_BODY, 'П = Помощник(КомпонентыОбмена); П.ИмяПКО = "Х";')
    text += f"\nФункция Помощник(К)\n{body}\nКонецФункции\n"
    layered, reading = overlay(text)
    assert layered.status == "complete" and not layered.skipped and unknown_lines(reading) == 0
    helper = next(r for r in reading.routines if r.name == "Помощник")
    assert helper.roles == {"handler_helper"}
    assert bool(validate_layers(layered).issues) is hint


@pytest.mark.parametrize(
    "body,hint",
    [
        ("КонвертацияСвойств.Очистить();", True),
        ('КонвертацияСвойств.Найти("Х", "СвойствоФормата");', True),
        ("КонвертацияСвойств = Неопределено;", True),
        ("КонвертацияСвойств.Имя = 1;", True),
        ("КонвертацияСвойств[0].СвойствоФормата = 1;", True),
        ("Сообщить(КонвертацияСвойств);", False),
    ],
)
def test_conversion_properties_lexical_hint(body, hint):
    old = "СтарыйПисатель(ПолученныеДанные, ДанныеИБ, КонвертацияСвойств, КомпонентыОбмена);"
    layered, reading = overlay(EXTENSION.replace(old, old + "\n" + body))
    assert layered.status == "complete" and not layered.skipped and unknown_lines(reading) == 0
    assert bool(validate_layers(layered).issues) is hint


def only_hook(target, parameters, body="А = 1;", kind="После", function=False):
    start, end = ("Функция", "КонецФункции") if function else ("Процедура", "КонецПроцедуры")
    return f'&{kind}("{target}")\n{start} Перехват({parameters})\n{body}\n{end}\n'


def compose_hook(text, base=None):
    base = base_document() if base is None else base
    layer = LayerDescriptor("L01", 1, "Слой", "memory", None, "")
    reading = read_extension_text(
        text, layer=layer, targets={r.name.casefold(): r for r in base.routines}
    )
    return compose_manager(base, readings=[reading])


@pytest.mark.parametrize(
    "target,parameters,body,function,target_class",
    [
        ("ПередКонвертацией", "КомпонентыОбмена", "А = 1;", False, "conversion_event"),
        ("ПослеКонвертации", "КомпонентыОбмена", "А = 1;", False, "conversion_event"),
        ("ПередОтложеннымЗаполнением", "КомпонентыОбмена", "А = 1;", False, "conversion_event"),
        ("ВерсияФорматаМенеджераОбмена", "", 'Возврат "3";', True, "base_routine"),
        (
            "ВыполнитьФункциюМодуляМенеджера",
            "ИмяФункции, Параметры",
            "Возврат ПродолжитьВызов(ИмяФункции, Параметры);",
            True,
            "function_dispatcher",
        ),
    ],
)
def test_unmodeled_hook_is_located_without_rule_taint(
    target, parameters, body, function, target_class
):
    layered = compose_hook(only_hook(target, parameters, body, function=function))
    assert layered.status == "partial" and len(layered.skipped) == 1
    skip = layered.skipped[0]
    assert skip.reason == "unmodeled_hook" and target in skip.raw and target_class in skip.raw
    assert skip.origin.span.line_start == 2 and skip.origin.procedure == "Перехват"
    assert all(rule_view(c).certainty == "known" for c in layered.contexts)
    report = validate_layers(layered)
    assert not report.issues and any(target in s.reason for s in report.skipped)
    clean = compose_manager(base_document())
    assert clean.status == "complete" and not clean.skipped
    for current, original in zip(layered.contexts, clean.contexts, strict=True):
        actual_links = validate_effective_links(layered, current)
        baseline_links = validate_effective_links(clean, original)
        assert actual_links.issues == baseline_links.issues
        assert actual_links.skipped == baseline_links.skipped


@pytest.mark.parametrize(
    "target,parameters",
    [
        ("ДобавитьПКО_Товар", "ПравилаКонвертации"),
        (
            "ДобавитьПКС",
            "РодительПКС, СвойствоКонфигурации, СвойствоФормата, "
            'ИспользуетсяАлгоритмКонвертации = 0, ПравилоКонвертацииСвойства = "", '
            'ПространствоИмен = ""',
        ),
    ],
)
def test_builder_hook_lowers_its_rules_only(target, parameters):
    layered = compose_hook(only_hook(target, parameters))
    assert any(s.reason == "unmodeled_hook" and target in s.raw for s in layered.skipped)
    for context in layered.contexts:
        assert any(v.certainty == "unknown" for v in context.entities if v.collection == "pko")
        assert all(v.certainty == "known" for v in context.entities if v.collection == "pod")
    assert compose_manager(base_document()).status == "complete"


@pytest.mark.parametrize("kind", ["Перед", "После", "Вместо", "ИзменениеИКонтроль"])
def test_typical_handler_hook_is_body_change_not_skip(kind):
    layered = compose_hook(
        only_hook(
            "СтарыйОтправитель", "ДанныеИБ, ДанныеXDTO, КомпонентыОбмена, СтекВыгрузки", kind=kind
        )
    )
    assert layered.status == "complete" and not layered.skipped
    assert unknown_lines(layered.readings[0]) == 0
    context = select_context(layered)
    doc = effective_document(layered, context)
    rule = next(r for r in doc.pko if r.name == "Товар")
    event = next(e for e in rule.events if e.event == "ПриОтправкеДанных")
    assert isinstance(event, LayerHandlerBinding)
    assert len(event.body_changes) == 1
    change = event.body_changes[0]
    assert (
        change.kind
        == {
            "Перед": "before",
            "После": "after",
            "Вместо": "around",
            "ИзменениеИКонтроль": "change_control",
        }[kind]
    )
    assert change.origin.span.line_start == 1 and change.target_name == "СтарыйОтправитель"
    assert any(r.name == "Перехват" and "handler" in r.roles for r in doc.routines)
    assert not validate_layers(layered, context=context).issues


def test_unsupported_event_without_branch_has_a_reading_skip():
    text = only_hook(
        "ЗаполнитьПравилаКонвертацииОбъектов",
        "НаправлениеОбмена, ПравилаКонвертации",
        'П = ПравилаКонвертации.Найти("Товар", "ИмяПКО");\nЕсли П <> Неопределено Тогда\n'
        'П.ПослеКонвертацииОбъекта = "НетТакого";\nКонецЕсли;',
    )
    layered = compose_hook(text)
    assert layered.status == "partial"
    assert any(
        s.reason == "unsupported_event" and "ПослеКонвертацииОбъекта" in s.raw
        for s in layered.skipped
    )
    assert any(
        "Событие читателем не поддержано" in s.reason for s in validate_layers(layered).skipped
    )
    assert not validate_layers(layered).issues
    clean = compose_hook(text.replace("ПослеКонвертацииОбъекта", "ПослеЗагрузкиВсехДанных"))
    assert not clean.skipped
    assert any(i.check == "ed.layer.handler.unreachable" for i in validate_layers(clean).issues)


@pytest.mark.parametrize(
    "arguments",
    [
        "Параметры.КомпонентыОбмена, Параметры.Объект",
        "Параметры.Объект",
        "Параметры.Объект, Параметры.ОбъектМодифицирован",
    ],
)
def test_event_keys_may_be_reordered_or_partial_with_defaults(arguments):
    text = only_hook(
        "ЗаполнитьПравилаКонвертацииОбъектов",
        "НаправлениеОбмена, ПравилаКонвертации",
        'П = ПравилаКонвертации.Найти("Товар", "ИмяПКО");\nЕсли П <> Неопределено Тогда\n'
        'П.ПослеЗагрузкиВсехДанных = "Отложить";\nКонецЕсли;',
    )
    text += f"""&Вместо("ВыполнитьПроцедуруМодуляМенеджера")
Процедура Диспетчер(ИмяПроцедуры, Параметры)
Если ИмяПроцедуры = "Отложить" Тогда
Отложить({arguments});
Иначе
ПродолжитьВызов(ИмяПроцедуры, Параметры);
КонецЕсли;
КонецПроцедуры
Процедура Отложить(А, Б = Неопределено)
Сообщить(А);
КонецПроцедуры
"""
    layered = compose_hook(text)
    assert (
        layered.status == "complete"
        and not layered.skipped
        and unknown_lines(layered.readings[0]) == 0
    )
    assert (
        next(
            c for c in select_context(layered).dispatch_chains if c.target_name == "Отложить"
        ).resolution
        == "call"
    )
    invalid = compose_hook(text.replace("Параметры.Объект", "Параметры.Чужой"))
    assert invalid.skipped and unknown_lines(invalid.readings[0]) > 0


def test_first_call_of_non_handler_is_not_previous_handler_mismatch():
    base = base_document()
    source = (
        base.files[0].text
        + "\nПроцедура ПомощникБазы(ДанныеИБ, ДанныеXDTO, КомпонентыОбмена, СтекВыгрузки)\n"
        "Сообщить(1);\nКонецПроцедуры\n"
    )
    base = read_manager_text(source)
    text = EXTENSION.replace(SEND_CALL, SEND_CALL.replace("СтарыйОтправитель", "ПомощникБазы"))
    layered = compose_hook(text, base)
    reading = layered.readings[0]
    assert (
        not layered.skipped
        and unknown_lines(reading) == 0
        and not [p for p in layered.previous_calls if p.event == "ПриОтправкеДанных"]
    )
    wrong, _ = overlay(
        EXTENSION.replace(SEND_CALL, SEND_CALL.replace("СтарыйОтправитель", "СтарыйПисатель"))
    )
    assert any(s.reason == "previous_handler_mismatch" for s in wrong.skipped)


def test_function_dispatcher_hook_skips_functional_binding_without_unreachable_error():
    fill = only_hook(
        "ЗаполнитьПравилаОбработкиДанных",
        "НаправлениеОбмена, ПравилаОбработкиДанных",
        'П = ПравилаОбработкиДанных.Найти("Товары", "Имя");\n'
        'Если П <> Неопределено Тогда\nП.ВыборкаДанных = "Выбрать";\nКонецЕсли;',
    )
    function = only_hook(
        "ВыполнитьФункциюМодуляМенеджера",
        "ИмяФункции, Параметры",
        "Возврат ПродолжитьВызов(ИмяФункции, Параметры);",
        function=True,
    )
    layered = compose_hook(fill + function)
    context = select_context(layered)
    assert (
        next(c for c in context.dispatch_chains if c.target_name == "Выбрать").resolution
        == "unknown"
    )
    assert all(v.certainty == "known" for v in context.entities)
    assert not validate_layers(layered, context=context).issues
    assert any(
        s.check == "ed.layer.handler.unreachable"
        for s in validate_layers(layered, context=context).skipped
    )
    links = validate_effective_links(layered, context)
    assert not [i for i in links.issues if i.check == "ed.handler.missing"]
    assert any(s.check == "ed.handler.missing" for s in links.skipped)
