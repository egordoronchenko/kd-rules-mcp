"""Ключи исполнителя, непривязанные ветки и свидетельства нижнего слоя."""

from dataclasses import replace

import pytest

from kd_rules_mcp.ed.address import build_addresses
from kd_rules_mcp.ed.forms import EVENT_INVOCATIONS
from kd_rules_mcp.ed.layer_model import LayerDescriptor, rule_view
from kd_rules_mcp.ed.layer_reader import read_extension_text
from kd_rules_mcp.ed.layers import compose_manager
from kd_rules_mcp.ed.refs import binding_owners, build_references
from kd_rules_mcp.validation.ed_layers import validate_effective_links, validate_layers
from kd_rules_mcp.validation.ed_links import validate_links
from kd_rules_mcp.validation.ed_projection import effective_document, select_context
from tests.test_ed_layers_handlers import (
    EXTENSION,
    SEND_BODY,
    SEND_CALL,
    base_document,
    overlay,
    unknown_lines,
)


def chains(layered, direction="send"):
    return {c.target_name: c.resolution for c in select_context(layered, direction).dispatch_chains}


def test_executor_signature_table_keeps_structure_keys_separate_from_formals():
    expected = {
        "ПриОбработке": ("procedure", ("ОбъектОбработки", "ИспользованиеПКО", "КомпонентыОбмена")),
        "ВыборкаДанных": ("function", ("КомпонентыОбмена",)),
        "ПриОтправкеДанных": (
            "procedure",
            ("ДанныеИБ", "ДанныеXDTO", "КомпонентыОбмена", "СтекВыгрузки"),
        ),
        "ПриКонвертацииДанныхXDTO": (
            "procedure",
            ("ДанныеXDTO", "ПолученныеДанные", "КомпонентыОбмена"),
        ),
        "ПередЗаписьюПолученныхДанных": (
            "procedure",
            ("ПолученныеДанные", "ДанныеИБ", "КонвертацияСвойств", "КомпонентыОбмена"),
        ),
        "ПослеЗагрузкиВсехДанных": (
            "procedure",
            ("Объект", "КомпонентыОбмена", "ОбъектМодифицирован"),
        ),
        "АлгоритмПоиска": ("procedure", ("ДанныеИБ", "ПолученныеДанные", "КомпонентыОбмена")),
    }
    assert {name: (s.kind, s.keys) for name, s in EVENT_INVOCATIONS.items()} == expected
    assert all(s.evidence.startswith("XDTO:") for s in EVENT_INVOCATIONS.values())


def pod_handler(event, key, formals):
    fill = f"""&После("ЗаполнитьПравилаОбработкиДанных")
Процедура ДополнитьОбработку(НаправлениеОбмена, ПравилаОбработкиДанных)
П = ПравилаОбработкиДанных.Найти("Товары", "Имя");
Если П <> Неопределено Тогда
П.{event} = "СвояОбработка";
КонецЕсли;
КонецПроцедуры
"""
    invocation = (
        f"СвояОбработка(Параметры.{key});"
        if event == "ВыборкаДанных"
        else (
            f"СвояОбработка(Параметры.{key}, Параметры.ИспользованиеПКО, "
            "Параметры.КомпонентыОбмена);"
        )
    )
    text = EXTENSION.replace(
        "\tИначе\n", f'\tИначеЕсли ИмяПроцедуры = "СвояОбработка" Тогда\n{invocation}\n\tИначе\n', 1
    )
    return fill + text + f"\nПроцедура СвояОбработка({formals})\nСообщить(1);\nКонецПроцедуры\n"


@pytest.mark.parametrize("key", ["ДанныеИБ", "ДанныеXDTO", "ОбъектОбработки"])
def test_pod_dispatch_uses_executor_key_and_free_formal_names(key):
    layered, reading = overlay(pod_handler("ПриОбработке", key, "Предмет, Использование, Контекст"))
    if key == "ОбъектОбработки":
        assert not layered.skipped and unknown_lines(reading) == 0
        assert chains(layered)["СвояОбработка"] == "call"
        for direction in ("send", "receive"):
            doc = effective_document(layered, select_context(layered, direction))
            pod = next(p for p in doc.pod if p.name == "Товары")
            assert next(e for e in pod.events if e.event == "ПриОбработке").target_id
            assert not validate_layers(layered, context=select_context(layered, direction)).issues
    else:
        assert layered.skipped and unknown_lines(reading) > 0
        assert chains(layered)["СвояОбработка"] == "unknown"
        assert chains(layered)["Отправить"] == "call"


def test_function_event_cannot_enter_procedure_dispatcher():
    layered, reading = overlay(pod_handler("ВыборкаДанных", "КомпонентыОбмена", "Контекст"))
    assert chains(layered)["СвояОбработка"] != "call"
    assert unknown_lines(reading) > 0
    issues = validate_layers(layered, context=select_context(layered)).issues
    issue = next(i for i in issues if i.check == "ed.layer.handler.unreachable")
    assert issue.level.value == "ошибка" and "/ПОД/Товары" in issue.address
    assert "function" in issue.message and "ВыборкаДанных" in issue.message
    assert not validate_layers(overlay()[0], context=select_context(overlay()[0])).issues


def test_formal_names_are_free_and_reference_index_uses_event_positions():
    header = "Процедура Отправить(ДанныеИБ, ДанныеXDTO, КомпонентыОбмена, СтекВыгрузки)"
    text = EXTENSION.replace(header, "Процедура Отправить(Источник, Формат, Контекст, Стек)")
    start = text.index("Процедура Отправить(")
    end = text.index("КонецПроцедуры", start)
    body = text[start:end].replace(SEND_CALL, "")
    for old, new in [
        ("ДанныеИБ", "Источник"),
        ("ДанныеXDTO", "Формат"),
        ("КомпонентыОбмена", "Контекст"),
    ]:
        body = body.replace(old, new)
    layered, reading = overlay(text[:start] + body + text[end:])
    assert not layered.skipped and unknown_lines(reading) == 0
    doc = effective_document(layered, select_context(layered))
    own = next(r for r in doc.routines if r.name == "Отправить")
    assert any(
        ref.owner_id == own.entity_id
        and ref.kind == "format_property"
        and ref.name == "Комментарий"
        for ref in build_references(doc).entries
    )


@pytest.mark.parametrize("event", ["ПриКонвертацииДанныхXDTO", "ПередЗаписьюПолученныхДанных"])
def test_extension_binding_into_base_dispatch_checks_each_event(event):
    fill = EXTENSION[: EXTENSION.index('&Вместо("ВыполнитьПроцедуруМодуляМенеджера")')]
    text = fill.replace(
        'Правило.ПриКонвертацииДанныхXDTO = "Преобразовать";',
        f'Правило.{event} = "СтарыйПисатель";',
    )
    text = text.replace('Правило.ПередЗаписьюПолученныхДанных = "СохранитьЗначение";', "")
    text = text.replace('ДругоеПравило.ПередЗаписьюПолученныхДанных = "ПередЗаписью";', "А = 1;")
    text = text.replace('Правило.ПриОтправкеДанных = "Отправить";', "А = 1;")
    layered, reading = overlay(text)
    assert not reading.skips and unknown_lines(reading) == 0
    ctx = select_context(layered, "receive")
    report = validate_layers(layered, context=ctx)
    if event == "ПриКонвертацииДанныхXDTO":
        issue = next(i for i in report.issues if i.check == "ed.layer.handler.unreachable")
        assert issue.level.value == "ошибка" and "/ПКО/Товар" in issue.address
        assert "ключи исполнителя" in issue.message and "ДанныеXDTO" in issue.message
        # Как для прочих ошибок связности, объяснённых слоем: второй дубль не выдаётся.
        assert not validate_effective_links(layered, ctx).issues
        assert any(
            s.check == "ed.handler.missing" and "Несовместимая сигнатура" in s.reason
            for s in validate_effective_links(layered, ctx).skipped
        )
        projected = effective_document(layered, ctx)
        assert any(
            i.check == "ed.handler.missing" and "ключи структуры" in i.message
            for i in validate_links(
                projected, build_addresses(projected), build_references(projected)
            ).issues
        )
    else:
        assert not report.issues and not validate_effective_links(layered, ctx).issues


def test_unsupported_event_has_a_located_skip():
    text = EXTENSION.replace(
        'Правило.ПриОтправкеДанных = "Отправить";', 'Правило.ПриУдаленииОбъектаИБ = "Отправить";'
    )
    layered, reading = overlay(text)
    assert any(
        s.reason == "event_signature"
        and "Событие читателем не поддержано" in s.raw
        and s.origin.span.line_start > 0
        for s in reading.skips
    )
    assert chains(layered)["Отправить"] == "unknown"
    assert unknown_lines(reading) > 0
    assert unknown_lines(overlay()[1]) == 0


def test_base_branch_target_parameter_count_is_checked_in_projection_too():
    doc = base_document()
    writer = next(c for c in doc.dispatcher_cases if c.literal_name == "СтарыйПисатель")
    converter = next(c for c in doc.dispatcher_cases if c.literal_name == "СтарыйПреобразователь")
    doc = replace(
        doc,
        dispatcher_cases=tuple(
            replace(c, arguments=converter.arguments) if c == writer else c
            for c in doc.dispatcher_cases
        ),
    )
    fill = """&После("ЗаполнитьПравилаКонвертацииОбъектов")
Процедура Дополнить(НаправлениеОбмена, ПравилаКонвертации)
Если НаправлениеОбмена = "Получение" Тогда
П = ПравилаКонвертации.Найти("Товар", "ИмяПКО");
Если П <> Неопределено Тогда
П.ПриКонвертацииДанныхXDTO = "СтарыйПисатель";
КонецЕсли;
КонецЕсли;
КонецПроцедуры
"""
    reading = read_extension_text(
        fill,
        layer=LayerDescriptor("L01", 1, "Слой", "memory", None, ""),
        targets={r.name.casefold(): r for r in doc.routines},
    )
    layered = compose_manager(doc, readings=[reading])
    ctx = select_context(layered, "receive")
    assert any(
        i.check == "ed.layer.handler.unreachable" and "ключи исполнителя" in i.message
        for i in validate_layers(layered, context=ctx).issues
    )
    projected = effective_document(layered, ctx)
    rule = next(r for r in projected.pko if r.name == "Товар")
    event = next(e for e in rule.events if e.event == "ПриКонвертацииДанныхXDTO")
    assert event.resolution == "invalid_signature" and event.target_id is None
    assert any(
        i.check == "ed.handler.missing" and "ключи структуры" in i.message
        for i in validate_links(
            projected, build_addresses(projected), build_references(projected)
        ).issues
    )
    good = replace(
        doc,
        routines=tuple(
            replace(
                r,
                parameters=next(
                    x for x in doc.routines if x.name == "СтарыйПреобразователь"
                ).parameters,
            )
            if r.name == "СтарыйПисатель"
            else r
            for r in doc.routines
        ),
    )
    clean = compose_manager(good, readings=[reading])
    clean_ctx = select_context(clean, "receive")
    assert not validate_layers(clean, context=clean_ctx).issues
    target = next(r for r in effective_document(clean, clean_ctx).pko if r.name == "Товар")
    assert next(e for e in target.events if e.event == "ПриКонвертацииДанныхXDTO").target_id


def test_unbound_branch_lowers_only_itself():
    text = EXTENSION.replace(
        "\tИначе\n",
        '\tИначеЕсли ИмяПроцедуры = "Лишняя" Тогда\nЛишняя(Параметры.КомпонентыОбмена);\n\tИначе\n',
        1,
    )
    text += "\nПроцедура Лишняя(Контекст)\nСообщить(1);\nКонецПроцедуры\n"
    layered, reading = overlay(text)
    assert reading.skips and unknown_lines(reading) > 0
    assert chains(layered)["Лишняя"] == "unknown"
    assert all(
        resolution == "call" for name, resolution in chains(layered).items() if name != "Лишняя"
    )
    assert all(v.certainty == "known" for c in layered.contexts for v in c.entities)
    assert rule_view(select_context(layered)).certainty == "known"
    assert unknown_lines(overlay()[1]) == 0


def extension_wrapper(literal, routine_name, previous):
    first = f"{previous}(А, Б, К, С);" if previous else ""
    return f'''&После("ЗаполнитьПравилаКонвертацииОбъектов")
Процедура Дополнить(НаправлениеОбмена, ПравилаКонвертации)
Если НаправлениеОбмена = "Отправка" Тогда
Правило = ПравилаКонвертации.Найти("Товар", "ИмяПКО");
Если Правило <> Неопределено Тогда
Правило.ПриОтправкеДанных = "{literal}";
КонецЕсли;
КонецЕсли;
КонецПроцедуры
&Вместо("ВыполнитьПроцедуруМодуляМенеджера")
Процедура Диспетчер(Имя, П)
Если Имя = "{literal}" Тогда
{routine_name}(П.ДанныеИБ, П.ДанныеXDTO, П.КомпонентыОбмена, П.СтекВыгрузки);
Иначе
ПродолжитьВызов(Имя, П);
КонецЕсли;
КонецПроцедуры
Процедура {routine_name}(А, Б, К, С)
{first}
Б.Вставить("Комментарий", 1);
КонецПроцедуры
'''


@pytest.mark.parametrize("previous,valid", [("СтарыйОтправитель", False), ("ПерваяОбертка", True)])
def test_previous_handler_is_from_the_lower_effective_layer(previous, valid):
    doc = base_document()
    texts = [
        extension_wrapper("ПервыйЛитерал", "ПерваяОбертка", None),
        extension_wrapper("ВторойЛитерал", "ВтораяОбертка", previous),
    ]
    readings = [
        read_extension_text(
            t,
            layer=LayerDescriptor(f"L0{i}", i, f"Слой{i}", "memory", None, ""),
            targets={r.name.casefold(): r for r in doc.routines},
            file_id=f"ext{i}",
        )
        for i, t in enumerate(texts, 1)
    ]
    layered = compose_manager(doc, readings=readings)
    second = layered.readings[1]
    mismatch = [s for s in second.skips if s.reason == "previous_handler_mismatch"]
    assert bool(mismatch) is (not valid)
    proofs = [p for p in second.previous_calls if p.event == "ПриОтправкеДанных"]
    assert bool(proofs) is valid
    if valid:
        assert proofs[0].target_name == "ПервыйЛитерал" and proofs[0].call_count == 1
        assert proofs[0].target_id == next(
            r.entity_id for r in layered.readings[0].routines if r.name == "ПерваяОбертка"
        )
        assert not layered.skipped and all(unknown_lines(r) == 0 for r in layered.readings)
        assert rule_view(select_context(layered)).certainty == "known"
        projection = effective_document(layered, select_context(layered))
        assert binding_owners(projection)[proofs[0].target_id]
    else:
        assert not proofs and unknown_lines(second) > 0
        assert chains(layered)["ВторойЛитерал"] == "call"
        assert chains(layered)["СтарыйПисатель"] == "call"


def test_previous_call_count_includes_transitive_helpers():
    text = EXTENSION.replace(
        SEND_BODY, "Помощник(ДанныеИБ, ДанныеXDTO, КомпонентыОбмена, СтекВыгрузки);"
    )
    text += (
        "\nПроцедура Помощник(ДанныеИБ, ДанныеXDTO, КомпонентыОбмена, СтекВыгрузки)\n"
        + SEND_CALL
        + "\nКонецПроцедуры\n"
    )
    layered, reading = overlay(text)
    assert not reading.skips and unknown_lines(reading) == 0
    assert next(p for p in layered.previous_calls if p.event == "ПриОтправкеДанных").call_count == 2
    layered, reading = overlay(text.replace(SEND_CALL, "", 1))
    assert not reading.skips and unknown_lines(reading) == 0
    assert not [p for p in layered.previous_calls if p.event == "ПриОтправкеДанных"]


def test_unbound_lower_branch_does_not_poison_other_chains():
    base = base_document()
    first = overlay()[1]
    # Второй слой перехватывает базовый литерал, который первый слой уже заменил.
    text = extension_wrapper("СтарыйОтправитель", "Новый", "СтарыйОтправитель")
    start = text.index('&Вместо("ВыполнитьПроцедуруМодуляМенеджера")')
    second = read_extension_text(
        text[start:],
        layer=LayerDescriptor("L02", 2, "Второй", "memory", None, ""),
        targets={r.name.casefold(): r for r in base.routines},
        file_id="second",
    )
    layered = compose_manager(base, readings=[first, second])
    assert layered.readings[1].skips
    assert chains(layered)["СтарыйОтправитель"] == "unknown"
    assert chains(layered)["Отправить"] == "call"
    assert chains(layered)["СохранитьЗначение"] == "call"
    assert rule_view(select_context(layered)).certainty == "known"
