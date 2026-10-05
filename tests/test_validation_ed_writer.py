"""Статические отказы писателя на собственных модулях, без выполнения BSL и обращения к базе."""

from dataclasses import replace

import pytest

from kd2_rules_mcp.authoring.ed.manager_operations import (
    ManagerOperation,
    PkoPatch,
    PropertyPatch,
    parse_operation,
)
from kd2_rules_mcp.ed.canonical import model_addresses
from kd2_rules_mcp.ed.executor_profile import BSP_3_1_12_XDTO, RuleColumn
from kd2_rules_mcp.ed.forms import EVENT_SIGNATURES
from kd2_rules_mcp.ed.reader import read_manager_text
from kd2_rules_mcp.ed.writer import new_manager, render
from kd2_rules_mcp.ed.writer_import import import_manager
from kd2_rules_mcp.ed.writer_model import Event, Reference, Value, dump_model, load_model
from kd2_rules_mcp.validation.ed_writer import validate_writer
from tests.test_ed_writer import execute, pilot_model
from tests.test_ed_writer_profiles import verified_detection


def report_for(model, text=None, **kwargs):
    detection = kwargs.pop("detection", verified_detection()[0])
    return validate_writer(
        model, render(model).data if text is None else text, detection=detection, **kwargs
    )


def test_address_reuse_does_not_leak_mutation_or_survive_rule_change():
    model = pilot_model()
    before = dump_model(model)
    expected = model_addresses(model)
    model_addresses(model).clear()
    assert model_addresses(model) == expected
    assert dump_model(model) == before
    changed = execute(
        model,
        ManagerOperation(
            "rename-cache",
            "pko",
            "update",
            target_id=model.pko[0].logical_id,
            patch=PkoPatch(name="Renamed"),
        ),
    )
    assert model_addresses(changed)[model.pko[0].logical_id] == "ПКО/Renamed"
    assert model_addresses(model) == expected


@pytest.mark.parametrize("factory", [new_manager, pilot_model])
@pytest.mark.parametrize("mode", ["preserve", "canonical"])
def test_empty_and_pilot_have_no_writer_issues(factory, mode):
    model = factory()
    assert not report_for(model, render(model, mode).data).issues


@pytest.mark.parametrize(
    "name",
    [
        c.name
        for c in BSP_3_1_12_XDTO.contracts
        if c.required and 2 in c.interfaces and c.name != "ВыполнитьФункциюМодуляМенеджера"
    ],
)
def test_every_required_method_is_checked_in_rendered_text(name):
    model = new_manager()
    output = render(model).text.replace(name + "(", "Убрано" + name + "(")
    report = report_for(model, output)
    assert any(i.check == "ed.writer.entrypoint" and name in i.message for i in report.issues)


def test_export_and_signature_and_literal_interface_are_required():
    model = new_manager(interface_version=3)
    output = render(model).text
    output = output.replace("ТолькоЗаголовки = Ложь", "Знач ТолькоЗаголовки = Ложь")
    output = output.replace("(ПараметрыКонвертации) Экспорт", "(ПараметрыКонвертации)")
    output = output.replace('Возврат "3";', 'Возврат "2";')
    assert (
        len([i for i in report_for(model, output).issues if i.check == "ed.writer.entrypoint"]) == 3
    )


def test_model_and_snapshot_profile_do_not_replace_current_detection():
    model = new_manager()
    report = validate_writer(model, render(model).data, profile=BSP_3_1_12_XDTO)
    assert [i.check for i in report.errors] == ["ed.writer.profile"]
    detection = verified_detection()[0]
    model = replace(model, executor_profile=detection.model_profile())
    model = load_model(dump_model(model))
    for selected in (None, detection.profile):
        report = validate_writer(model, render(model).data, profile=selected)
        assert [i.check for i in report.errors] == ["ed.writer.profile"]
        assert "Выгрузка исполнителя не сверялась" in report.errors[0].message
    assert not validate_writer(model, render(model).data, detection=detection).issues


def test_detection_does_not_verify_another_selected_profile():
    model = new_manager()
    model = replace(
        model, executor_profile=replace(model.executor_profile, profile_id="another-profile")
    )
    report = report_for(model)
    assert any(i.check == "ed.writer.profile" for i in report.errors)


@pytest.mark.parametrize("field", ["evidence", "runtime_verified", "revision", "sha256", "helpers"])
def test_manager_operation_rejects_profile_proofs(field):
    with pytest.raises(ValueError, match="DTO"):
        parse_operation(
            {
                "client_id": "forged",
                "kind": "manager",
                "action": "update",
                "patch": {"executor_profile": {"profile_id": "bsp-3.1.12-xdto", field: True}},
            }
        )


@pytest.mark.parametrize(
    "whole,expression,expected",
    [
        (False, "Клиент", True),
        (False, "Сервер", True),
        (True, "Клиент", True),
        (True, "Сервер Или ТолстыйКлиентОбычноеПриложение Или ВнешнееСоединение", False),
    ],
)
def test_preprocessor_does_not_certify_unavailable_entrypoint(whole, expression, expected):
    model = new_manager()
    source = render(model).text
    if whole:
        source = f"#Если {expression} Тогда\n{source}\n#КонецЕсли"
    else:
        routine = next(
            r
            for r in read_manager_text(source).routines
            if r.name == "ЗаполнитьПараметрыКонвертации"
        )
        source = (
            source[: routine.span.char_start]
            + f"#Если {expression} Тогда\n"
            + source[routine.span.char_start : routine.span.char_end]
            + "\n#КонецЕсли\n"
            + source[routine.span.char_end :]
        )
    assert (
        any(i.check == "ed.writer.entrypoint" for i in report_for(model, source).issues) == expected
    )


def test_optional_deletion_hook_is_checked_only_when_present():
    model = new_manager()
    base = render(model).text
    name = "ПередОбработкойУдаляемогоОбъекта"
    assert not report_for(model, base).issues
    correct = base + f"\nПроцедура {name}(КомпонентыОбмена, Объект) Экспорт\nКонецПроцедуры\n"
    assert not report_for(model, correct).issues
    report = report_for(
        model, correct.replace(f"{name}(КомпонентыОбмена, Объект)", f"{name}(Объект)")
    )
    assert any(i.check == "ed.writer.entrypoint" and name in i.message for i in report.issues)


def test_entrypoint_formal_names_and_unused_defaults_do_not_change_positional_contract():
    model = new_manager(interface_version=3)
    source = render(model).text.replace("(ПараметрыКонвертации) Экспорт", "(Параметры) Экспорт")
    source = source.replace("ТолькоЗаголовки = Ложь", "ТолькоЗаголовки")
    assert not report_for(model, source).issues


def test_interface_and_path_outside_profile_are_rejected():
    model = new_manager(interface_version=3)
    detection, _, profile = verified_detection()
    detection = replace(
        detection, profile=replace(profile, interfaces=(1, 2), receive_paths=("ordinary",))
    )
    report = report_for(model, detection=detection, receive_path="object")
    assert any(i.check == "ed.writer.profile" for i in report.errors)


def test_orphan_send_pko_and_missing_receive_type_are_named():
    model = pilot_model()
    changed = replace(model, pod=())
    report = report_for(changed, render(model).data)
    assert {i.check for i in report.issues} == {"ed.writer.send_pod", "ed.writer.receive_pod"}
    assert all(i.address == model_addresses(model)[model.pko[0].logical_id] for i in report.issues)


def test_unknown_send_reference_is_not_silently_treated_as_absence():
    model = pilot_model()
    pod = next(p for p in model.pod if p.directions == ("send",))
    changed = replace(
        model,
        pod=tuple(
            replace(p, used_pko=(Reference("pko", name="Compute()", resolution="computed"),))
            if p == pod
            else p
            for p in model.pod
        ),
    )
    report = report_for(changed, render(model).data)
    assert any(s.check == "ed.writer.send_pod" for s in report.skipped)
    assert not any(i.check == "ed.writer.send_pod" for i in report.issues)


def dependency_source(direction, children=("Nested",)):
    """Собственные правила с одним корнем ПОД; вложенные ПКО пока не связаны."""
    model = pilot_model()
    for name in children:
        model = execute(
            model,
            ManagerOperation(
                "create-" + name,
                "pko",
                "create",
                patch=PkoPatch(
                    name=name,
                    directions=(direction,),
                    format_object=Value("string", "Справочник." + name),
                ),
            ),
        )
        rule = model.pko[-1]
        model = execute(
            model,
            ManagerOperation(
                "property-" + name,
                "property",
                "create",
                owner_id=rule.logical_id,
                container_id=rule.logical_id,
                patch=PropertyPatch(configuration_property="Name", format_property="Name"),
            ),
        )
    return render(model).text


def imported_report(source):
    model = import_manager(read_manager_text(source), project_id="dependencies")[0]
    return report_for(model, source)


@pytest.mark.parametrize(
    "direction,event", [("receive", "ПриОтправкеДанных"), ("send", "ПриКонвертацииДанныхXDTO")]
)
def test_opposite_event_of_both_direction_rule_is_not_a_path(direction, event):
    source = add_code_path(
        dependency_source(direction),
        direction,
        'П = ОбменДаннымиXDTOСервер.ПКОПоИмени(КомпонентыОбмена, "Nested");',
        event=event,
    )
    check = "ed.writer." + ("send_pod" if direction == "send" else "receive_pod")
    assert any(
        i.check == check and i.address == "ПКО/Nested" for i in imported_report(source).issues
    )


def test_same_receive_type_without_used_pko_is_not_a_root_or_nested_path():
    source = dependency_source("receive", ("Twin", "Nested"))
    source = source.replace('"Справочник.Twin"', '"Справочник.Должности"')
    rule = next(r for r in read_manager_text(source).pko if r.name == "Twin")
    source = source.replace(
        rule.raw_text, rule.raw_text.replace('"Name", "Name"', '"Name", "Name", 0, "Nested"')
    )
    issues = {
        i.address for i in imported_report(source).issues if i.check == "ed.writer.receive_pod"
    }
    assert issues == {"ПКО/Twin", "ПКО/Nested"}


@pytest.mark.parametrize("direction", ["send", "receive"])
@pytest.mark.parametrize("reachable", [True, False])
@pytest.mark.parametrize("algorithm_region", [True, False])
def test_local_helper_chain_contributes_only_from_active_handler(
    direction, reachable, algorithm_region
):
    source = add_code_path(
        dependency_source(direction),
        direction,
        "First(КомпонентыОбмена);" if reachable else "Сообщить(1);",
    )
    if algorithm_region:
        source += "\n#Область Алгоритмы\n"
    source += "\nПроцедура First(КомпонентыОбмена)\n Second(КомпонентыОбмена);\nКонецПроцедуры\n"
    source += (
        "\nПроцедура Second(КомпонентыОбмена)\n First(КомпонентыОбмена);\n"
        ' П = ОбменДаннымиXDTOСервер.ПКОПоИмени(КомпонентыОбмена, "Nested");\n'
        "КонецПроцедуры\n"
    )
    if algorithm_region:
        source += "#КонецОбласти\n"
    check = "ed.writer." + ("send_pod" if direction == "send" else "receive_pod")
    assert (
        any(i.check == check and i.address == "ПКО/Nested" for i in imported_report(source).issues)
        != reachable
    )


@pytest.mark.parametrize("direction", ["send", "receive"])
@pytest.mark.parametrize(
    "expression,skipped_names",
    [
        ('"Pre_" + Compute()', {"Pre_A", "Pre_B_End"}),
        ('Compute() + "_End"', {"B_End", "Pre_B_End"}),
        ('("Pre_" + Compute() + "_End")', {"Pre_B_End"}),
        ('Compute("Pre_")', {"Pre_A", "Pre_B_End", "B_End", "Other"}),
        ('"Missing_" + Compute()', set()),
    ],
)
def test_computed_rule_name_is_limited_by_outer_literal_parts(direction, expression, skipped_names):
    children = ("Pre_A", "Pre_B_End", "B_End", "Other")
    source = add_code_path(
        dependency_source(direction, children), direction, "Инструкция.ИмяПКО = " + expression + ";"
    )
    report = imported_report(source)
    check = "ed.writer." + ("send_pod" if direction == "send" else "receive_pod")
    issues = {i.address.removeprefix("ПКО/") for i in report.issues if i.check == check}
    skips = [s for s in report.skipped if s.check == check]
    assert issues == set(children) - skipped_names
    assert {s.reason.split(": ", 1)[0].removeprefix("ПКО/") for s in skips} == skipped_names
    assert all(
        "Код/" in s.reason and "строка " in s.reason and "покрыто ПКО: " in s.reason for s in skips
    )


def add_code_path(source, direction, body, event=None, owner_name=None):
    """Привязанный к корню обработчик, читаемый существующим индексом ссылок."""
    event = event or ("ПриОтправкеДанных" if direction == "send" else "ПриКонвертацииДанныхXDTO")
    name = event + "_Demo"
    marker = "СвойстваШапки = ПравилоКонвертации.Свойства;"
    rules = read_manager_text(source).pko
    owner = next(r for r in rules if r.name == owner_name) if owner_name else rules[0]
    body_text = owner.raw_text.replace(
        marker, f'ПравилоКонвертации.{event} = "{name}";\n\t' + marker, 1
    )
    source = source.replace(owner.raw_text, body_text, 1)
    arguments = ", ".join(EVENT_SIGNATURES[event][0])
    return source + f"\nПроцедура {name}({arguments})\n\t{body}\nКонецПроцедуры\n"


@pytest.mark.parametrize("direction", ["send", "receive"])
def test_nested_property_and_table_part_are_paths_without_own_pod(direction):
    source = dependency_source(direction)
    check = "ed.writer." + ("send_pod" if direction == "send" else "receive_pod")
    assert any(
        i.check == check and i.address == "ПКО/Nested" for i in imported_report(source).issues
    )
    line = next(line for line in source.splitlines() if "ДобавитьПКС(СвойстваШапки," in line)
    linked = source.replace(line, '\tДобавитьПКС(СвойстваШапки, "Name", "Name", , "Nested");', 1)
    assert not any(i.check == check for i in imported_report(linked).issues)
    table = source.replace(
        line,
        '\tСвойстваТЧ = ДобавитьПКТЧ(ПравилоКонвертации, "Rows", "Rows");\n'
        '\tДобавитьПКС(СвойстваТЧ, "Name", "Name", , "Nested");',
        1,
    )
    assert not any(i.check == check for i in imported_report(table).issues)


@pytest.mark.parametrize("direction", ["send", "receive"])
def test_property_disabled_for_direction_is_not_a_path(direction):
    source = dependency_source(direction)
    line = next(line for line in source.splitlines() if "ДобавитьПКС(СвойстваШапки," in line)
    conf, fmt = ('"Name"', '""') if direction == "send" else ('""', '"Name"')
    source = source.replace(line, f'\tДобавитьПКС(СвойстваШапки, {conf}, {fmt}, , "Nested");', 1)
    check = "ed.writer." + ("send_pod" if direction == "send" else "receive_pod")
    assert any(
        i.check == check and i.address == "ПКО/Nested" for i in imported_report(source).issues
    )


@pytest.mark.parametrize("direction", ["send", "receive"])
@pytest.mark.parametrize(
    "body",
    [
        'Инструкция = Новый Структура("ИмяПКО,Значение", "Nested", Неопределено);',
        'Инструкция.ИмяПКО = "Nested";',
        'Правило = ОбменДаннымиXDTOСервер.ПКОПоИмени(КомпонентыОбмена, "Nested");',
    ],
)
def test_literal_rule_name_in_active_code_is_a_path(direction, body):
    source = add_code_path(dependency_source(direction), direction, body)
    check = "ed.writer." + ("send_pod" if direction == "send" else "receive_pod")
    report = imported_report(source)
    assert not any(i.check == check for i in report.issues)
    assert not any(s.check == check for s in report.skipped)


def test_processing_handler_usage_table_is_a_send_path():
    source = dependency_source("send")
    marker = "ПравилоОбработки.ИспользуемыеПКО.Добавить("
    source = source.replace(
        marker, 'ПравилоОбработки.ПриОбработке = "ПриОбработке_Demo";\n\t' + marker, 1
    )
    arguments = ", ".join(EVENT_SIGNATURES["ПриОбработке"][0])
    source += (
        f"\nПроцедура ПриОбработке_Demo({arguments})\n"
        '\tИспользованиеПКО.Вставить("Nested", Истина);\nКонецПроцедуры\n'
    )
    assert not any(i.check == "ed.writer.send_pod" for i in imported_report(source).issues)


@pytest.mark.parametrize("direction", ["send", "receive"])
def test_computed_rule_name_skips_absence_with_reason(direction):
    source = add_code_path(
        dependency_source(direction), direction, "Инструкция.ИмяПКО = ВычислитьИмя();"
    )
    check = "ed.writer." + ("send_pod" if direction == "send" else "receive_pod")
    report = imported_report(source)
    assert not any(i.check == check for i in report.issues)
    assert any(s.check == check and "вычисляется" in s.reason for s in report.skipped)


@pytest.mark.parametrize("direction", ["send", "receive"])
def test_computed_name_in_unreachable_handler_does_not_mask_orphan(direction):
    source = add_code_path(
        dependency_source(direction),
        direction,
        "Инструкция.ИмяПКО = ВычислитьИмя();",
        owner_name="Nested",
    )
    check = "ed.writer." + ("send_pod" if direction == "send" else "receive_pod")
    report = imported_report(source)
    assert any(i.check == check and i.address == "ПКО/Nested" for i in report.issues)
    assert not any(s.check == check for s in report.skipped)


@pytest.mark.parametrize("direction", ["send", "receive"])
def test_transitive_nested_rules_are_reachable(direction):
    source = dependency_source(direction, ("A", "B"))
    document = read_manager_text(source)
    for rule, target in ((document.pko[0], "A"), (document.pko[1], "B")):
        source = source.replace(
            rule.properties[0].raw_text,
            f'ДобавитьПКС(СвойстваШапки, "Name", "Name", , "{target}");',
            1,
        )
    check = "ed.writer." + ("send_pod" if direction == "send" else "receive_pod")
    assert not any(i.check == check for i in imported_report(source).issues)


@pytest.mark.parametrize("direction", ["send", "receive"])
def test_unreachable_cycle_is_not_a_path_and_comment_is_not_code(direction):
    source = dependency_source(direction, ("A", "B"))
    document = read_manager_text(source)
    for name, target in (("A", "B"), ("B", "A")):
        rule = next(r for r in document.pko if r.name == name)
        original = rule.properties[0].raw_text
        source = source.replace(
            original, f'ДобавитьПКС(СвойстваШапки, "Name", "Name", , "{target}");', 1
        )
    source = add_code_path(source, direction, '// Инструкция.ИмяПКО = "A";')
    check = "ed.writer." + ("send_pod" if direction == "send" else "receive_pod")
    assert {i.address for i in imported_report(source).issues if i.check == check} == {
        "ПКО/A",
        "ПКО/B",
    }


@pytest.mark.parametrize("selection", [False, True])
def test_function_dispatcher_is_required_only_for_processing_selection(selection):
    source = render(pilot_model()).text
    if selection:
        marker = "ПравилоОбработки.ИспользуемыеПКО.Добавить("
        source = source.replace(
            marker, 'ПравилоОбработки.ВыборкаДанных = "ВыборкаДанных_Demo";\n\t' + marker, 1
        )
        source += (
            "\nФункция ВыборкаДанных_Demo(КомпонентыОбмена)\n"
            "\tВозврат Неопределено;\nКонецФункции\n"
        )
    document = read_manager_text(source)
    routine = next(r for r in document.routines if r.name == "ВыполнитьФункциюМодуляМенеджера")
    source = source[: routine.span.char_start] + source[routine.span.char_end :]
    issues = [i for i in imported_report(source).issues if i.check == "ed.writer.entrypoint"]
    assert bool(issues) == selection
    assert all("ВыполнитьФункциюМодуляМенеджера" in i.message for i in issues)


def test_rule_name_limit_comes_from_profile_not_an_invented_global_constant():
    model = pilot_model()
    rule = model.pko[0]
    changed = execute(
        model,
        ManagerOperation(
            "long", "pko", "update", target_id=rule.logical_id, patch=PkoPatch(name="A" * 51)
        ),
    )
    detection, _, profile = verified_detection()
    assert not report_for(changed, detection=detection).issues
    limited = replace(
        profile,
        columns=(*profile.columns, RuleColumn("pko", "ИмяПКО", 50, "Синтетический исполнитель:1")),
    )
    # Замена, а не дубль колонки: name_limit читает единственное ограничение таблицы.
    limited = replace(
        limited,
        columns=(
            *(c for c in limited.columns[:-1] if (c.table, c.name) != ("pko", "ИмяПКО")),
            limited.columns[-1],
        ),
    )
    report = report_for(changed, detection=replace(detection, profile=limited))
    assert [i.check for i in report.errors] == ["ed.writer.name_length"]


@pytest.mark.parametrize(
    "event,path,expected",
    [
        ("ПослеКонвертацииОбъекта", "ordinary", True),
        ("ПослеКонвертацииОбъекта", "object", False),
        ("ПослеЗагрузкиВсехДанных", "ordinary", False),
        ("ПослеЗагрузкиВсехДанных", "object", True),
        ("НеизвестноеСобытие", "ordinary", True),
        ("ПриПолученииЗапросаВыгрузкиОбъекта", "object", True),
        ("АлгоритмПоиска", "ordinary", False),
        ("АлгоритмПоиска", "object", True),
    ],
)
def test_event_capabilities_distinguish_receive_paths(event, path, expected):
    model = pilot_model()
    rule = model.pko[0]
    binding = Event(
        logical_id="test-event",
        name=event,
        event=event,
        target=Reference("code_unit", name="Handler", resolution="missing"),
    )
    changed = replace(model, pko=(replace(rule, events=(binding,)),))
    report = report_for(changed, render(model).data, receive_path=path)
    issues = [i for i in report.issues if i.check == "ed.writer.event"]
    assert bool(issues) == expected
    if issues:
        assert issues[0].address == model_addresses(changed)[binding.logical_id]


def test_table_part_operation_of_interface_one_is_a_profile_error():
    model = pilot_model(1)
    op = ManagerOperation("tc", "table_part", "create", address="ПКО/Должности")
    assert any(
        i.check == "ed.writer.table_part_interface"
        for i in report_for(model, operations=(op,)).errors
    )


def test_interface_three_guard_must_precede_properties():
    model = pilot_model(3)
    text = render(model).text.replace("Если ТолькоЗаголовки Тогда", "Если Ложь Тогда")
    assert [i.check for i in report_for(model, text).issues] == ["ed.writer.headers_only"]
    text = render(model).text.replace("\r\n", "\n")
    text = text.replace(
        "Если ТолькоЗаголовки Тогда\n\t\tВозврат;\n\tКонецЕсли;",
        "Если Ложь Тогда\n\t\tЕсли ТолькоЗаголовки Тогда\n\t\t\tВозврат;\n"
        "\t\tКонецЕсли;\n\tКонецЕсли;",
    )
    assert any(i.check == "ed.writer.headers_only" for i in report_for(model, text).issues)


def test_dispatcher_link_checks_are_reused_and_strict_fallback_is_rejected():
    model = pilot_model()
    text = render(model).text.replace("\r\n", "\n")
    text = text.replace(
        "Процедура ВыполнитьПроцедуруМодуляМенеджера(ИмяПроцедуры, Параметры) Экспорт\n",
        "Процедура ВыполнитьПроцедуруМодуляМенеджера(ИмяПроцедуры, Параметры) Экспорт\n"
        '\tЕсли ИмяПроцедуры = "Handler" Тогда\n\t\tMissing();\n'
        '\tИначе\n\t\tВызватьИсключение "Unknown";\n\tКонецЕсли;\n',
    )
    checks = {i.check for i in report_for(model, text).issues}
    assert checks == {"ed.writer.dispatcher", "ed.dispatcher.target_missing"}
    bound = render(model).text.replace("\r\n", "\n")
    source = read_manager_text(bound)
    routine = next(r for r in source.routines if r.name == model.pko[0].procedure_name)
    offset = bound.index("ПравилоКонвертации.ИмяПКО", routine.body_span.char_start)
    bound = (
        bound[:offset] + '\n\tПравилоКонвертации.ПриОтправкеДанных = "Absent";\n' + bound[offset:]
    )
    assert any(i.check == "ed.handler.missing" for i in report_for(model, bound).issues)


@pytest.mark.parametrize("mode", ["preserve", "canonical"])
def test_retained_entrypoint_cannot_be_certified_as_ready(mode):
    model = new_manager()
    text = (
        render(model)
        .text.replace("\r\n", "\n")
        .replace(
            "Процедура ЗаполнитьПараметрыКонвертации(ПараметрыКонвертации) Экспорт\n",
            "Процедура ЗаполнитьПараметрыКонвертации(ПараметрыКонвертации) Экспорт\n"
            '\tПараметрыКонвертации.Вставить("P", Compute());\n',
        )
    )
    imported = import_manager(read_manager_text(text), project_id="opaque")[0]
    report = report_for(imported, render(imported, mode).data)
    assert any(i.check == "ed.writer.incomplete" for i in report.issues)


def test_helper_parameter_variant_and_semantics_are_not_bypassed_by_preserve():
    model = pilot_model()
    text = render(model).text.replace("\r\n", "\n").replace(', ПространствоИмен = ""', "")
    text = text.replace("\tНоваяСтрока.ПространствоИмен                = ПространствоИмен;\n", "")
    imported = import_manager(read_manager_text(text), project_id="helper")[0]
    report = report_for(imported)
    assert any(
        i.check == "ed.writer.incomplete" and "ДобавитьПКС" in i.address for i in report.issues
    )
    text = render(model).text
    helper = next(r for r in read_manager_text(text).routines if r.name == "ДобавитьПКС")
    text = text[: helper.span.char_start] + text[helper.span.char_end :]
    imported = import_manager(read_manager_text(text), project_id="no-helper")[0]
    assert any(i.check == "ed.writer.incomplete" for i in report_for(imported).issues)


def test_profile_checks_limits_of_retained_pcs_reference_and_identification_value():
    model = pilot_model()
    text = render(model).text.replace(
        '"Наименование");', '"Наименование", 0, "' + "A" * 101 + '");'
    )
    imported = import_manager(read_manager_text(text), project_id="limits")[0]
    assert imported.pko[0].properties[0].state == "retained"
    assert any(i.check == "ed.writer.name_length" for i in report_for(imported).errors)
    rule = model.pko[0]
    invalid = replace(
        model,
        pko=(
            replace(
                rule, identification=replace(rule.identification, mode=Value("string", "A" * 61))
            ),
        ),
    )
    report = report_for(invalid, render(model).data)
    assert any(
        i.check == "ed.writer.name_length" and i.address.endswith("Идентификация")
        for i in report.errors
    )


def test_read_failure_is_returned_as_validation_report():
    report = report_for(new_manager(), b"\xff")
    assert [i.check for i in report.errors] == ["ed.writer.read"]


def test_duplicate_methods_are_reported_without_case_sensitive_aliasing():
    model = new_manager()
    text = (
        render(model).text
        + "\nПроцедура передконвертацией(КомпонентыОбмена) Экспорт\nКонецПроцедуры\n"
    )
    assert {i.check for i in report_for(model, text).errors} == {
        "ed.writer.entrypoint",
        "ed.writer.name_collision",
    }


def test_handler_issue_address_maps_same_named_rules_by_procedure():
    model = execute(
        pilot_model(),
        ManagerOperation(
            "second",
            "pko",
            "create",
            patch=PkoPatch(
                name="Second",
                directions=("receive",),
                format_object=Value("string", "Catalog.Second"),
            ),
        ),
    )
    text = render(model).text.replace('"Second"', '"Должности"')
    doc = read_manager_text(text)
    second = next(r for r in doc.routines if r.name.endswith("Second"))
    offset = text.index("ПравилоКонвертации.ИмяПКО", second.body_span.char_start)
    text = (
        text[:offset]
        + 'ПравилоКонвертации.ПриКонвертацииДанныхXDTO = "Absent";\n\t'
        + text[offset:]
    )
    model = import_manager(read_manager_text(text), project_id="same")[0]
    report = report_for(model)
    issue = next(i for i in report.issues if i.check == "ed.handler.missing")
    second_rule = next(r for r in model.pko if r.procedure_name.endswith("Second"))
    assert issue.address == model_addresses(model)[second_rule.logical_id]
    assert issue.address.endswith("~receive")
