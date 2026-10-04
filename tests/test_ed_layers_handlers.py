"""Шаблоны §5.1–§5.5: три интерфейса и строгая граница чтения обработчиков."""

from collections import Counter
from dataclasses import replace
from pathlib import Path
from types import MappingProxyType

import pytest

from kd2_rules_mcp.authoring.ed.model import AttributeDraft
from kd2_rules_mcp.authoring.ed.operations import draft_property
from kd2_rules_mcp.ed.layer_model import LayerDescriptor, rule_view
from kd2_rules_mcp.ed.layer_reader import read_extension_text
from kd2_rules_mcp.ed.layers import compose_manager
from kd2_rules_mcp.ed.model import Classification
from kd2_rules_mcp.ed.reader import read_manager_text
from kd2_rules_mcp.ed.refs import binding_owners, build_references
from kd2_rules_mcp.ed.schema import load_schema
from kd2_rules_mcp.ed.schema.profile import ValidationProfile
from kd2_rules_mcp.validation.ed_layers import (
    validate_effective_links,
    validate_effective_schema,
    validate_effective_structure,
    validate_layers,
)
from kd2_rules_mcp.validation.ed_projection import effective_document, select_context
from tests.test_ed_layer_flow import HELPERS, MODULE
from tests.test_validation_ed_structure import snapshot

DATA = Path(__file__).parent / "data/ed/layers/handlers"
EXTENSION = (DATA / "extension.bsl").read_text(encoding="utf-8")


def base_document(version=2):
    text = MODULE.replace('Возврат "2";', f'Возврат "{version}";')
    text = text.replace("ПКО_Товар_ПриОтправкеДанных", "СтарыйОтправитель")
    text = text.replace("Метаданные.Справочники.Товары", "Метаданные.Справочники.Тест")
    text = text.replace('"Catalog.Product"', '"Справочник.Тест"').replace('"Наименование"', '"Код"')
    text = text.replace(
        '    ПравилоКонвертации.ПриОтправкеДанных = "СтарыйОтправитель";',
        '    ПравилоКонвертации.ПриОтправкеДанных = "СтарыйОтправитель";\n'
        '    ПравилоКонвертации.ПриКонвертацииДанныхXDTO = "СтарыйПреобразователь";\n'
        '    ПравилоКонвертации.ПередЗаписьюПолученныхДанных = "СтарыйПисатель";',
    )
    text = text.replace(
        "    КонецЕсли;\nКонецПроцедуры\n",
        '    ИначеЕсли ИмяПроцедуры = "СтарыйПреобразователь" Тогда\n'
        "        СтарыйПреобразователь(Параметры.ДанныеXDTO, Параметры.ПолученныеДанные, "
        "Параметры.КомпонентыОбмена);\n"
        '    ИначеЕсли ИмяПроцедуры = "СтарыйПисатель" Тогда\n'
        "        СтарыйПисатель(Параметры.ПолученныеДанные, Параметры.ДанныеИБ, "
        "Параметры.КонвертацияСвойств, Параметры.КомпонентыОбмена);\n"
        "    КонецЕсли;\nКонецПроцедуры\n",
    )
    text += (
        "Процедура СтарыйПреобразователь(ДанныеXDTO, ПолученныеДанные, КомпонентыОбмена)\n"
        "КонецПроцедуры\n"
        "Процедура СтарыйПисатель(ПолученныеДанные, ДанныеИБ, КонвертацияСвойств, "
        "КомпонентыОбмена)\n"
        "КонецПроцедуры\n"
    )
    start = text.index("Процедура ДобавитьПКО_Товар(")
    end = text.index("КонецПроцедуры", start) + len("КонецПроцедуры")
    other = (
        text[start:end]
        .replace("ДобавитьПКО_Товар", "ДобавитьПКО_Другой")
        .replace('ИмяПКО = "Товар"', 'ИмяПКО = "Другой"')
        .replace('    ПравилоКонвертации.ПриОтправкеДанных = "СтарыйОтправитель";\n', "")
    )
    text = (
        text.replace(
            "    ДобавитьПКО_Товар(ПравилаКонвертации);",
            "    ДобавитьПКО_Товар(ПравилаКонвертации);\n"
            "    ДобавитьПКО_Другой(ПравилаКонвертации);",
        )
        + "\n"
        + other
    )
    if version == 3:
        text = text.replace(
            "ЗаполнитьПравилаКонвертацииОбъектов(НаправлениеОбмена, ПравилаКонвертации)",
            "ЗаполнитьПравилаКонвертацииОбъектов(КомпонентыОбмена, ПравилаКонвертации, "
            "ТолькоЗаголовки)",
        )
    return read_manager_text(text, path=f"base-v{version}.bsl", file_id="base")


def extension_text(version=2):
    if version != 3:
        return EXTENSION
    text = EXTENSION.replace(
        "Дополнить(НаправлениеОбмена, ПравилаКонвертации)",
        "Дополнить(КомпонентыОбмена, ПравилаКонвертации, ТолькоЗаголовки)",
    ).replace("Если НаправлениеОбмена", "Если КомпонентыОбмена.НаправлениеОбмена")
    return text.replace(
        '\tЕсли КомпонентыОбмена.НаправлениеОбмена = "Получение" Тогда',
        "\tЕсли Не ТолькоЗаголовки Тогда\n"
        '\tЕсли КомпонентыОбмена.НаправлениеОбмена = "Получение" Тогда',
        1,
    ).replace("КонецПроцедуры", "\tКонецЕсли;\nКонецПроцедуры", 1)


def overlay(text=EXTENSION, version=2):
    document = base_document(version)
    layer = LayerDescriptor("L01-Обработчики", 1, "Обработчики", "memory", None, "")
    reading = read_extension_text(
        text,
        layer=layer,
        version=version,
        helpers=HELPERS,
        targets={routine.name.casefold(): routine for routine in document.routines},
        path="handlers.bsl",
        file_id="handlers",
    )
    layered = compose_manager(document, readings=[reading], version=version)
    return layered, layered.readings[0]


def unknown_lines(reading):
    return reading.coverage.line_classes.count(Classification.UNKNOWN)


def rows(report):
    return Counter(
        (issue.check, issue.level, issue.address, issue.message) for issue in report.issues
    )


def with_attribute(snap, object_key=("справочник", "тест"), name="доп_Заметка"):
    owner = snap.objects[object_key]
    props = dict(owner.properties)
    props[(name.casefold(), "")] = (draft_property(AttributeDraft(name, name, "string", {}), -100),)
    objects = dict(snap.objects)
    objects[object_key] = replace(owner, properties=MappingProxyType(props))
    return replace(
        snap,
        objects=MappingProxyType(objects),
        by_type=MappingProxyType({item.type_name.casefold(): item for item in objects.values()}),
    )


@pytest.mark.parametrize("version", [1, 2, 3])
def test_handler_templates_roles_coverage_binding_and_projection(version):
    layered, reading = overlay(extension_text(version), version)
    assert not reading.skips and not layered.skipped
    assert unknown_lines(reading) == 0
    assert layered.status == "complete"
    routines = {routine.name: routine for routine in reading.routines}
    assert routines["Маршрутизировать"].roles == {"dispatcher"}
    for name in ("СохранитьЗначение", "Отправить", "Преобразовать", "ПередЗаписью"):
        assert routines[name].roles == {"handler"}
        span = routines[name].body_span
        assert all(
            kind != Classification.UNKNOWN
            for kind in reading.coverage.line_classes[span.line_start - 1 : span.line_end]
        )
    assert {call.target_name for call in layered.previous_calls} == {
        "СтарыйОтправитель",
        "СтарыйПреобразователь",
        "СтарыйПисатель",
    }
    for call in layered.previous_calls:
        assert call.span.file_id == "handlers"
        assert reading.source.text[call.span.char_start : call.span.char_end].startswith(
            call.target_name
        )
    schema = load_schema(DATA / "schema.bin")
    snap = with_attribute(snapshot())
    baseline = compose_manager(layered.base, version=version)
    for direction in ("send", "receive"):
        context = select_context(layered, direction)
        assert rule_view(context).certainty == "known"
        assert all(version.certainty == "known" for version in context.entities)
        document = effective_document(layered, context)
        rule = next(rule for rule in document.pko if rule.name == "Товар")
        prop = next(prop for prop in rule.properties if prop.format_property == "Комментарий")
        assert prop.algorithm_flag == (1 if direction == "send" else 0)
        event = "ПриОтправкеДанных" if direction == "send" else "ПередЗаписьюПолученныхДанных"
        binding = next(binding for binding in rule.events if binding.event == event)
        name = "Отправить" if direction == "send" else "СохранитьЗначение"
        assert binding.target_name == name and binding.target_id == routines[name].entity_id
        assert binding.target_id is not None
        assert binding_owners(document)[binding.target_id] == (rule.entity_id,)
        refs = build_references(document)
        own_refs = [ref for ref in refs.entries if ref.owner_id == binding.target_id]
        assert own_refs and all(
            ref.direction == direction and ref.span.file_id == "handlers" for ref in own_refs
        )
        chains = {chain.target_name: chain for chain in context.dispatch_chains}
        assert chains[name].resolution == "call"
        case = chains[name].links[0].case
        assert case is not None
        assert case.target.reference_parts == (name,)
        assert chains["СтарыйОтправитель"].links[0].continues
        assert chains["СтарыйОтправитель"].resolution == "call"
        assert not validate_layers(layered, context=context).issues
        base_context = select_context(baseline, direction)
        profile = ValidationProfile.build(schema, "1.20", direction)
        for check in (
            lambda item, ctx: validate_effective_links(item, ctx),
            lambda item, ctx, profile=profile: validate_effective_schema(
                item, ctx, schema, profile, snap
            ),
            lambda item, ctx, profile=profile: validate_effective_structure(
                item, ctx, snap, profile
            ),
        ):
            assert not rows(check(layered, context)) - rows(check(baseline, base_context))
    if version == 3:
        for direction in ("send", "receive"):
            assert (
                effective_document(layered, select_context(layered, direction, True)).pko
                == effective_document(baseline, select_context(baseline, direction, True)).pko
            )


@pytest.mark.parametrize(
    "bad",
    [
        "nonliteral",
        "two_calls",
        "after",
        "extra",
        "wrong_continue",
        "wrong_args",
        "wrong_member",
        "composite",
        "hook_target",
        "duplicate_procedure",
        "collision",
        "unknown_filler",
        "lexical",
    ],
)
def test_handler_negative_inputs_retain_unknown_coverage(bad):
    text = EXTENSION
    replacements = {
        "nonliteral": ('ИмяПроцедуры = "СохранитьЗначение"', "ИмяПроцедуры = ИмяИзНастройки"),
        "two_calls": (
            '\tИначеЕсли ИмяПроцедуры = "Отправить"',
            '\t\tЛишнийВызов();\n\tИначеЕсли ИмяПроцедуры = "Отправить"',
        ),
        "no_continue": ("ПродолжитьВызов(ИмяПроцедуры, Параметры);", ""),
        "after": (
            '&Вместо("ВыполнитьПроцедуруМодуляМенеджера")',
            '&После("ВыполнитьПроцедуруМодуляМенеджера")',
        ),
        "extra": (
            "Процедура Маршрутизировать(ИмяПроцедуры, Параметры)",
            "Процедура Маршрутизировать(ИмяПроцедуры, Параметры)\nСообщить(ИмяПроцедуры);",
        ),
        "wrong_continue": (
            "ПродолжитьВызов(ИмяПроцедуры, Параметры);",
            "ПродолжитьВызов(Параметры, ИмяПроцедуры);",
        ),
        "wrong_args": (
            "Параметры.ПолученныеДанные, Параметры.ДанныеИБ",
            "ПолученныеДанные, Параметры.ДанныеИБ",
        ),
        "unknown_filler": (
            'Правило.ПриОтправкеДанных = "Отправить";',
            'Правило.Свойства.Очистить();\nПравило.ПриОтправкеДанных = "Отправить";',
        ),
        "wrong_member": (
            "Параметры.ПолученныеДанные, Параметры.ДанныеИБ",
            "Параметры.ЧужойПараметр, Параметры.ДанныеИБ",
        ),
        "composite": (
            'ИмяПроцедуры = "СохранитьЗначение"',
            'ИмяПроцедуры = "СохранитьЗначение" И Истина',
        ),
        "hook_target": (
            "СохранитьЗначение(Параметры.ПолученныеДанные, Параметры.ДанныеИБ,\n"
            "\t\t\tПараметры.КонвертацияСвойств, Параметры.КомпонентыОбмена);",
            "Дополнить(Параметры.НаправлениеОбмена, Параметры.ПравилаКонвертации);",
        ),
        "lexical": ('Отсутствующие = "";', 'Отсутствующие = "незакрытая строка;'),
    }
    if bad == "collision":
        text = text.replace("СохранитьЗначение", "СтарыйОтправитель")
    elif bad == "duplicate_procedure":
        text += "Процедура СохранитьЗначение()\nКонецПроцедуры\n"
    else:
        text = text.replace(*replacements[bad], 1)
    layered, reading = overlay(text)
    assert reading.skips or layered.skipped
    assert unknown_lines(reading) > 0
    assert layered.status == "partial"
    if bad not in ("unknown_filler", "lexical"):
        chains = select_context(layered).dispatch_chains
        assert chains and any(chain.resolution == "unknown" for chain in chains)
        if bad != "wrong_member":
            assert all(chain.resolution == "unknown" for chain in chains)
        result = validate_layers(layered, context=select_context(layered))
        assert not any(issue.check == "ed.layer.handler.unreachable" for issue in result.issues)
        assert any(skip.check == "ed.layer.handler.unreachable" for skip in result.skipped)


def test_assigned_handler_without_branch_is_unreachable_and_not_painted_opaque():
    start = EXTENSION.index('&Вместо("ВыполнитьПроцедуруМодуляМенеджера")')
    end = EXTENSION.index("Процедура СохранитьЗначение")
    layered, reading = overlay(EXTENSION[:start] + EXTENSION[end:])
    assert unknown_lines(reading) > 0
    assert all("handler" not in routine.roles for routine in reading.routines)
    result = validate_layers(layered, context=select_context(layered, "receive"))
    assert any(
        issue.check == "ed.layer.handler.unreachable" and issue.level.value == "ошибка"
        for issue in result.issues
    )


def test_unbound_prefix_and_unknown_filler_are_not_painted_as_handlers():
    layered, reading = overlay(
        EXTENSION + "Процедура ПКО_ЛожныйОбработчик()\nДелать();\nКонецПроцедуры\n"
    )
    routine = next(item for item in reading.routines if item.name == "ПКО_ЛожныйОбработчик")
    assert not routine.roles
    assert unknown_lines(reading) == 1
    bad, denied = overlay(
        EXTENSION.replace(
            'Правило.ПриОтправкеДанных = "Отправить";',
            'Правило.Свойства.Очистить();\nПравило.ПриОтправкеДанных = "Отправить";',
        )
    )
    assert bad.status == "partial"
    assert unknown_lines(denied) > 0
    assert any(
        segment.classification == Classification.OPAQUE_CODE for segment in denied.coverage.segments
    )
    assert layered.readings[0].source.text.endswith("КонецПроцедуры\n")


def test_previous_call_requires_first_direct_call_with_formal_arguments():
    _, reading = overlay()
    call = next(item for item in reading.previous_calls if item.target_name == "СтарыйОтправитель")
    assert len(call.arguments) == 4
    altered = EXTENSION.replace(
        "\tСтарыйОтправитель(ДанныеИБ, ДанныеXDTO, КомпонентыОбмена, СтекВыгрузки);",
        "\tСообщить(ДанныеИБ);\n"
        "\tСтарыйОтправитель(ДанныеИБ, ДанныеXDTO, КомпонентыОбмена, СтекВыгрузки);",
    )
    _, reading = overlay(altered)
    assert not any(item.target_name == "СтарыйОтправитель" for item in reading.previous_calls)
    assert unknown_lines(reading) == 0


def test_nested_literal_fallback_and_binding_alias_keep_the_effective_owner():
    text = EXTENSION.replace(
        '\tИначеЕсли ИмяПроцедуры = "Отправить" Тогда',
        '\tИначе\n\tЕсли ИмяПроцедуры = "Отправить" Тогда',
    ).replace("Процедура СохранитьЗначение", "КонецЕсли;\nПроцедура СохранитьЗначение", 1)
    # Закрывающий оператор относится к dispatcher, до окончания процедуры.
    text = text.replace("КонецПроцедуры\n\nКонецЕсли;", "КонецЕсли;\nКонецПроцедуры\n")
    text = text.replace(
        'Правило.ПриОтправкеДанных = "Отправить";', 'Правило.ПриОтправкеДанных = "КлючОтправки";'
    ).replace('ИмяПроцедуры = "Отправить"', 'ИмяПроцедуры = "КлючОтправки"')
    layered, reading = overlay(text)
    assert not reading.skips and unknown_lines(reading) == 0
    projected = effective_document(layered, select_context(layered))
    routine = next(item for item in projected.routines if item.name == "Отправить")
    assert binding_owners(projected)[routine.entity_id] == (projected.pko[0].entity_id,)
    assert all(
        ref.direction == "send"
        for ref in build_references(projected).entries
        if ref.owner_id == routine.entity_id
    )
    received = effective_document(layered, select_context(layered, "receive"))
    assert not any(ref.owner_id == routine.entity_id for ref in build_references(received).entries)


@pytest.mark.parametrize("version", [1, 2, 3])
def test_after_existing_before_write_is_bound_and_indexed(version):
    text = extension_text(version).replace(
        'Правило.ПередЗаписьюПолученныхДанных = "СохранитьЗначение";',
        'Правило.ПередЗаписьюПолученныхДанных = "ПередЗаписью";',
    )
    start = text.index('\tЕсли ИмяПроцедуры = "СохранитьЗначение"')
    end = text.index('\tИначеЕсли ИмяПроцедуры = "Отправить"', start)
    text = text[:start] + text[end:].replace("\tИначеЕсли", "\tЕсли", 1)
    start = text.index("Процедура СохранитьЗначение")
    end = text.index("Процедура Отправить", start)
    text = text[:start] + text[end:]
    layered, reading = overlay(text, version)
    assert not reading.skips and unknown_lines(reading) == 0
    document = effective_document(layered, select_context(layered, "receive"))
    routine = next(item for item in document.routines if item.name == "ПередЗаписью")
    assert routine.entity_id in binding_owners(document)
    previous = next(item for item in reading.previous_calls if item.routine_id == routine.entity_id)
    assert previous.target_name == "СтарыйПисатель"
    refs = [ref for ref in build_references(document).entries if ref.owner_id == routine.entity_id]
    assert refs and all(ref.direction == "receive" for ref in refs)
    assert not validate_layers(layered, context=select_context(layered, "receive")).issues


def test_effective_links_checks_new_body_in_its_file_and_direction():
    for target, expected in (("Товар", 0), ("НетТакогоПравила", 1)):
        text = EXTENSION.replace(
            '\tДанныеXDTO.Вставить("Комментарий", ВРег(ДанныеИБ.доп_Заметка));',
            f'\tОбменДаннымиXDTOСервер.ПКОПоИмени(КомпонентыОбмена, "{target}");',
        )
        layered, reading = overlay(text)
        assert not reading.skips and unknown_lines(reading) == 0
        sent = validate_effective_links(layered, select_context(layered))
        found = [issue for issue in sent.issues if issue.check == "ed.reference.code_rule_missing"]
        assert len(found) == expected
        if found:
            assert target in found[0].message and "Отправить" in found[0].address
        received = validate_effective_links(layered, select_context(layered, "receive"))
        assert not any(issue.check == "ed.reference.code_rule_missing" for issue in received.issues)


SEND_CALL = "СтарыйОтправитель(ДанныеИБ, ДанныеXDTO, КомпонентыОбмена, СтекВыгрузки);"
SEND_BODY = '\tДанныеXDTO.Вставить("Комментарий", ВРег(ДанныеИБ.доп_Заметка));'


@pytest.mark.parametrize(
    "parameters",
    [
        "ПолученныеДанные, ДанныеИБ, КонвертацияСвойств, КомпонентыОбмена",
        "ДанныеИБ, ДанныеXDTO, КомпонентыОбмена, ЧужойПараметр",
    ],
)
def test_dispatch_arguments_require_the_effective_event_signature(parameters):
    original = "ДанныеИБ, ДанныеXDTO, КомпонентыОбмена, СтекВыгрузки"
    args = ", ".join("Параметры." + name.strip() for name in parameters.split(","))
    text = EXTENSION.replace(
        "Процедура Отправить(" + original + ")", "Процедура Отправить(" + parameters + ")"
    )
    text = text.replace(
        "Отправить(Параметры.ДанныеИБ, Параметры.ДанныеXDTO,\n"
        "\t\t\tПараметры.КомпонентыОбмена, Параметры.СтекВыгрузки);",
        "Отправить(" + args + ");",
    )
    bad, reading = overlay(text)
    assert any(
        skip.reason == "event_signature" and skip.origin.span.file_id == "handlers"
        for skip in reading.skips
    )
    assert unknown_lines(reading) > 0
    result = validate_layers(bad, context=select_context(bad))
    assert any(skip.check == "ed.layer.handler.unreachable" for skip in result.skipped)
    assert not overlay()[0].skipped


@pytest.mark.parametrize(
    "replacement,witness,count,unknown",
    [
        ("Если Истина Тогда\n" + SEND_CALL + "\nКонецЕсли;", False, 0, False),
        (
            "СтарыйОтправитель(ДанныеXDTO, ДанныеИБ, КомпонентыОбмена, СтекВыгрузки);",
            False,
            0,
            False,
        ),
        ("СтарыйПисатель(ДанныеИБ, ДанныеXDTO, КомпонентыОбмена, СтекВыгрузки);", False, 0, True),
        (SEND_CALL + "\n" + SEND_CALL, True, 2, False),
        (SEND_CALL, True, 1, False),
    ],
)
def test_previous_call_is_bound_to_base_rule_event_and_counts_calls(
    replacement, witness, count, unknown
):
    layered, reading = overlay(EXTENSION.replace(SEND_CALL, replacement))
    calls = [call for call in layered.previous_calls if call.event == "ПриОтправкеДанных"]
    assert bool(calls) is witness
    if calls:
        assert len(calls) == 1
        call = calls[0]
        assert call.rule_name == "Товар" and call.target_name == "СтарыйОтправитель"
        assert call.call_count == count
        assert any(rule.entity_id == call.rule_id for rule in layered.base.pko)
    assert bool(unknown_lines(reading)) is unknown
    assert any(skip.reason == "previous_handler_mismatch" for skip in reading.skips) is unknown
    if unknown:
        for direction in ("send", "receive"):
            rules = {
                v.payload.name: v.certainty
                for v in select_context(layered, direction).entities
                if v.collection == "pko" and v.payload is not None
            }
            assert rules["Товар"] == ("unknown" if direction == "send" else "known")
            assert rules["Другой"] == "known"


def test_previous_call_rejects_another_base_handler_even_with_the_same_formals():
    document = base_document()
    text = document.files[0].text
    start = text.index("Процедура ДобавитьПКО_Другой")
    text = text[:start] + text[start:].replace(
        'ПередЗаписьюПолученныхДанных = "СтарыйПисатель";',
        'ПередЗаписьюПолученныхДанных = "СтарыйПисатель";\n'
        '    ПравилоКонвертации.ПриОтправкеДанных = "ДругойОтправитель";',
    )
    text += (
        "\nПроцедура ДругойОтправитель(ДанныеИБ, ДанныеXDTO, КомпонентыОбмена, СтекВыгрузки)\n"
        "КонецПроцедуры\n"
    )
    document = read_manager_text(text)
    reading = read_extension_text(
        EXTENSION.replace(SEND_CALL, SEND_CALL.replace("СтарыйОтправитель", "ДругойОтправитель")),
        layer=LayerDescriptor("L01", 1, "Слой", "memory", None, ""),
        helpers=HELPERS,
        targets={r.name.casefold(): r for r in document.routines},
    )
    layered = compose_manager(document, readings=[reading])
    assert any(skip.reason == "previous_handler_mismatch" for skip in layered.skipped)
    assert not any(call.event == "ПриОтправкеДанных" for call in layered.previous_calls)


def test_dispatch_branch_signature_must_match_every_event_binding():
    text = EXTENSION.replace(
        'ДругоеПравило.ПередЗаписьюПолученныхДанных = "ПередЗаписью";',
        'ДругоеПравило.ПередЗаписьюПолученныхДанных = "Отправить";',
    )
    _, reading = overlay(text)
    assert any(
        skip.reason == "event_signature" and skip.raw.startswith("Отправить(")
        for skip in reading.skips
    )
    assert not overlay()[0].skipped


@pytest.mark.parametrize(
    "assigned,literal",
    [
        ("Отправить", "отправить"),
        (" Отправить ", "Отправить"),
        ("отправить", "отправить"),
        (" Отправить ", " Отправить "),
    ],
)
def test_handler_key_is_exact_but_procedure_identifier_is_case_insensitive(assigned, literal):
    text = EXTENSION.replace(
        'Правило.ПриОтправкеДанных = "Отправить";', f'Правило.ПриОтправкеДанных = "{assigned}";'
    )
    text = text.replace('ИмяПроцедуры = "Отправить"', f'ИмяПроцедуры = "{literal}"')
    text = text.replace("\t\tОтправить(Параметры.", "\t\tоТПРАВИТЬ(Параметры.")
    layered, reading = overlay(text)
    context = select_context(layered)
    document = effective_document(layered, context)
    if assigned == literal:
        assert not reading.skips and unknown_lines(reading) == 0
        event = next(
            e
            for rule in document.pko
            if rule.name == "Товар"
            for e in rule.events
            if e.event == "ПриОтправкеДанных"
        )
        assert event.target_id == next(
            r.entity_id for r in reading.routines if r.name == "Отправить"
        )
    else:
        assert any(skip.reason == "event_signature" for skip in reading.skips)
        assert "handler" not in next(r.roles for r in reading.routines if r.name == "Отправить")
        assert not any(
            ref.span.file_id == "handlers" and ref.direction == "send"
            for ref in build_references(document).entries
        )
    issues = validate_layers(layered, context=context).issues
    assert bool(issues) is (assigned != literal)
    assert all(issue.check == "ed.layer.handler.unreachable" for issue in issues)


def test_dispatch_arguments_use_the_second_formal_parameter():
    text = EXTENSION.replace(
        "Маршрутизировать(ИмяПроцедуры, Параметры)", "Маршрутизировать(Имя, П)"
    ).replace("Если ИмяПроцедуры", "Если Имя")
    text = text.replace("ПродолжитьВызов(ИмяПроцедуры, Параметры)", "ПродолжитьВызов(Имя, П)")
    bad, reading = overlay(text)
    assert reading.skips and unknown_lines(reading) > 0 and bad.status == "partial"
    good, reading = overlay(text.replace("Параметры.", "П."))
    assert not reading.skips and unknown_lines(reading) == 0 and good.status == "complete"


def test_unknown_from_a_filler_call_is_not_overpainted_by_a_handler_role():
    text = EXTENSION.replace(
        'Правило.ПриОтправкеДанных = "Отправить";',
        'Правило.ПриОтправкеДанных = "Отправить";\n'
        "Отправить(Правило, ПравилаКонвертации, Неопределено, Неопределено);",
    )
    layered, reading = overlay(text.replace(SEND_BODY, "\tДанныеИБ.Очистить();"))
    assert reading.skips and unknown_lines(reading) > 0 and layered.status == "partial"
    assert any(skip.origin.procedure == "Отправить" for skip in reading.skips)
    assert unknown_lines(overlay()[1]) == 0


def test_same_procedure_name_in_two_files_uses_the_selected_dispatch_branch():
    base, first = overlay()
    second_text = """&Вместо("ВыполнитьПроцедуруМодуляМенеджера")
Процедура ВторойДиспетчер(ИмяПроцедуры, Параметры)
Если ИмяПроцедуры = "Отправить" Тогда
Отправить(Параметры.ДанныеИБ, Параметры.ДанныеXDTO,
Параметры.КомпонентыОбмена, Параметры.СтекВыгрузки);
Иначе
ПродолжитьВызов(ИмяПроцедуры, Параметры);
КонецЕсли;
КонецПроцедуры
Процедура Отправить(ДанныеИБ, ДанныеXDTO, КомпонентыОбмена, СтекВыгрузки)
ДанныеXDTO.Вставить("ИзВторого", Истина);
КонецПроцедуры
"""
    second = read_extension_text(
        second_text,
        layer=LayerDescriptor("L02", 2, "Второй", "memory", None, ""),
        targets={r.name.casefold(): r for r in base.base.routines},
        file_id="second",
    )
    layered = compose_manager(base.base, readings=[first, second])
    assert all(skip.reason == "event_signature" for skip in layered.skipped)
    doc = effective_document(layered, select_context(layered))
    rule = next(r for r in doc.pko if r.name == "Товар")
    binding = next(e for e in rule.events if e.event == "ПриОтправкеДанных")
    target = next(r for r in doc.routines if r.entity_id == binding.target_id)
    assert target.span.file_id == "second"
    refs = build_references(doc).entries
    assert any(ref.name == "ИзВторого" and ref.owner_id == target.entity_id for ref in refs)
    assert not any(
        ref.owner_id == next(r.entity_id for r in first.routines if r.name == "Отправить")
        for ref in refs
    )
    finalized_first = layered.readings[0]
    assert not next(r for r in finalized_first.routines if r.name == "Отправить").roles
    bad = compose_manager(
        base.base,
        readings=[
            first,
            replace(
                second,
                source=replace(
                    second.source,
                    text=second.source.text.replace("Параметры.СтекВыгрузки", "Параметры.Чужой"),
                ),
            ),
        ],
    )
    assert bad.skipped
    chains = {c.target_name: c.resolution for c in select_context(bad).dispatch_chains}
    assert chains["Отправить"] == "unknown"
    assert chains["ПередЗаписью"] == "call"


def test_around_branch_wins_over_a_base_branch_with_the_same_literal():
    text = EXTENSION.replace(
        'Правило.ПриОтправкеДанных = "Отправить";',
        'Правило.ПриОтправкеДанных = "СтарыйОтправитель";',
    )
    text = text.replace('ИмяПроцедуры = "Отправить"', 'ИмяПроцедуры = "СтарыйОтправитель"')
    layered, reading = overlay(text)
    assert not reading.skips and unknown_lines(reading) == 0
    handler = next(r for r in reading.routines if r.name == "Отправить")
    assert handler.roles == {"handler"}
    doc = effective_document(layered, select_context(layered))
    assert all(
        event.target_id == handler.entity_id
        for rule in doc.pko
        for event in rule.events
        if event.event == "ПриОтправкеДанных"
    )


def test_platform_annotation_does_not_clear_a_pending_hook():
    header = "Процедура Отправить(ДанныеИБ, ДанныеXDTO, КомпонентыОбмена, СтекВыгрузки)"
    _, reading = overlay(
        EXTENSION.replace(header, '&Перед("СтарыйОтправитель")\n&НаСервере\n' + header)
    )
    assert any(skip.reason == "opaque_dispatch" for skip in reading.skips)
    assert next(h for h in reading.hooks if h.routine.name == "Отправить").target_class == "handler"
    clean, reading = overlay(EXTENSION.replace(header, "&НаСервере\n" + header))
    assert not clean.skipped and unknown_lines(reading) == 0


def test_handler_reads_collection_count_without_mutating_it():
    layered, reading = overlay(
        EXTENSION.replace(
            SEND_BODY,
            "Количество = КомпонентыОбмена.ПравилаКонвертацииОбъектов.Количество();\n"
            "Сообщить(Количество);",
        )
    )
    assert not layered.skipped and unknown_lines(reading) == 0
    assert rule_view(select_context(layered)).certainty == "known"


def test_unbound_branch_has_no_handler_role_or_opaque_body():
    _, reading = overlay(
        EXTENSION.replace(
            'Правило.ПриОтправкеДанных = "Отправить";', 'Правило.ПриОтправкеДанных = "НетВетки";'
        )
    )
    routine = next(r for r in reading.routines if r.name == "Отправить")
    assert not routine.roles
    assert any(skip.reason == "event_signature" for skip in reading.skips)
    assert (
        Classification.UNKNOWN
        in reading.coverage.line_classes[
            routine.body_span.line_start - 1 : routine.body_span.line_end
        ]
    )
    assert unknown_lines(overlay()[1]) == 0


def test_literal_tree_without_continuation_proves_dispatcher_suppression():
    layered, reading = overlay(EXTENSION.replace("ПродолжитьВызов(ИмяПроцедуры, Параметры);", ""))
    assert not reading.skips and unknown_lines(reading) == 0
    chains = {c.target_name: c for c in select_context(layered).dispatch_chains}
    assert chains["СтарыйОтправитель"].resolution == "no_call"
    assert chains["Отправить"].resolution == "call"
    result = validate_layers(layered, context=select_context(layered, "receive"))
    # Прежний преобразователь второго ПКО не заменялся, но диспетчер его отсекает.
    assert any(issue.check == "ed.layer.dispatcher.suppressed" for issue in result.issues)
    assert not validate_layers(overlay()[0], context=select_context(overlay()[0])).issues
