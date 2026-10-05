"""Регрессии независимого ревью D2 на вымышленных модулях."""

from collections import Counter

import pytest

from kd2_rules_mcp.authoring.ed.manager_operations import (
    AlgorithmPatch,
    HandlerPatch,
    ManagerOperation,
    ManagerOperationError,
    PkoPatch,
    PodPatch,
    apply,
    preview,
)
from kd2_rules_mcp.ed.canonical import model_addresses
from kd2_rules_mcp.ed.reader import read_manager_text
from kd2_rules_mcp.ed.writer import render
from kd2_rules_mcp.ed.writer_import import import_manager
from kd2_rules_mcp.ed.writer_model import Reference
from kd2_rules_mcp.validation.ed_writer import _code_checks, validate_writer
from kd2_rules_mcp.validation.report import ValidationReport
from tests.test_ed_writer_code import code_model, execute, handler, round_trip


def base():
    model = execute(
        code_model(),
        ManagerOperation(
            "other", "pko", "create", patch=PkoPatch(name="Other", directions=("both",))
        ),
    )
    model = execute(
        model,
        ManagerOperation(
            "compute",
            "algorithm",
            "create",
            patch=AlgorithmPatch(name="Compute", parameters="Value", body="Возврат;"),
        ),
    )
    model = handler(model, body="Compute(1);")
    return execute(
        model,
        ManagerOperation(
            "other-handler",
            "handler",
            "create",
            owner_id=model.pko[1].logical_id,
            patch=HandlerPatch(event="ПриОтправкеДанных", body="Y = 2;"),
        ),
    )


def imported(text):
    return import_manager(read_manager_text(text), project_id="review-d2")[0]


def planned(model, op):
    return preview(model, (op,), expected_revision=model.revision)


def checks(model):
    report = ValidationReport()
    _code_checks(model, read_manager_text(render(model).text), report, model_addresses(model))
    return Counter(i.check for i in report.issues)


def branch_text(model, name):
    output = render(model)
    case = next(c for c in model.dispatcher_cases if c.name == name)
    entry = next(e for e in output.report.entries if e.leaf_id == case.logical_id)
    return output.data[entry.byte_start : entry.byte_end].decode("utf-8")


@pytest.mark.parametrize("action", ["create", "delete", "body", "rename"])
def test_b1_unrelated_operations_do_not_repair_missing_branch(action):
    model = base()
    name = "ПКО_Other_ПриОтправкеДанных"
    text = render(model).text.replace(branch_text(model, name), "")
    model = imported(text)
    assert checks(model)["ed.writer.handler_dispatcher"] == 2
    op = {
        "create": ManagerOperation(
            "fresh", "algorithm", "create", patch=AlgorithmPatch(name="Fresh", body="")
        ),
        "delete": ManagerOperation("remove", "pko", "delete", target_id=model.pko[0].logical_id),
        "body": ManagerOperation(
            "body",
            "handler",
            "update",
            target_id=model.pko[0].events[0].logical_id,
            patch=HandlerPatch(body="\n\tX = 2;\n"),
        ),
        "rename": ManagerOperation(
            "rename",
            "pko",
            "update",
            target_id=model.pko[1].logical_id,
            patch=PkoPatch(name="Renamed"),
        ),
    }[action]
    changed = execute(model, op)
    assert checks(changed)["ed.writer.handler_dispatcher"] == 2
    assert len(changed.dispatcher_cases) == (0 if action == "delete" else 1)
    round_trip(changed)


def test_b1_case_insensitive_call_does_not_add_dead_branch():
    model = base()
    name = "ПКО_Other_ПриОтправкеДанных"
    text = render(model).text.replace(name + "(", name.lower() + "(", 1)
    model = imported(text)
    assert all(c.target.target_id for c in model.dispatcher_cases)
    changed = execute(
        model,
        ManagerOperation(
            "fresh", "algorithm", "create", patch=AlgorithmPatch(name="Fresh", body="")
        ),
    )
    assert len(changed.dispatcher_cases) == 2
    assert branch_text(changed, name) == branch_text(model, name)
    round_trip(changed)


def test_b1_explicit_restore_requires_execution_confirmation():
    model = base()
    name = "ПКО_Other_ПриОтправкеДанных"
    model = imported(render(model).text.replace(branch_text(model, name), ""))
    op = ManagerOperation(
        "restore",
        "handler",
        "update",
        target_id=model.pko[1].events[0].logical_id,
        patch=HandlerPatch(restore_dispatcher=True),
    )
    plan = planned(model, op)
    assert not plan.failures
    assert len(plan.notices) == 1
    assert plan.notices[0].code == "handler_execution_changed"
    assert "начнёт выполняться" in plan.notices[0].message
    with pytest.raises(ManagerOperationError) as error:
        apply(
            model, (op,), expected_revision=model.revision, expected_preview_hash=plan.preview_hash
        )
    assert error.value.failures[0].reason == "confirmation_required"
    changed = execute(model, op)
    assert not checks(changed)["ed.writer.handler_dispatcher"]
    round_trip(changed)
    repeated = planned(
        changed,
        ManagerOperation(
            "restore-again",
            "handler",
            "update",
            target_id=changed.pko[1].events[0].logical_id,
            patch=HandlerPatch(restore_dispatcher=True),
        ),
    )
    assert not repeated.notices


def test_b1_unrelated_changes_keep_empty_function_dispatcher():
    model = execute(
        base(),
        ManagerOperation(
            "pod",
            "pod",
            "create",
            patch=PodPatch(name="Selection", directions=("send",)),
        ),
    )
    model = execute(
        model,
        ManagerOperation(
            "selection",
            "handler",
            "create",
            owner_id=model.pod[0].logical_id,
            patch=HandlerPatch(event="ВыборкаДанных", body="Возврат Неопределено;"),
        ),
    )
    name = "ПОД_Selection_ВыборкаДанных"
    text = render(model).text.replace(branch_text(model, name) + "\tКонецЕсли;\n", "")
    model = imported(text)
    changed = handler(model, event="ПриКонвертацииДанныхXDTO", client="xdto")
    assert "Функция ВыполнитьФункциюМодуляМенеджера" in render(changed).text
    assert (
        checks(model)["ed.writer.handler_dispatcher"]
        == checks(changed)["ed.writer.handler_dispatcher"]
    )
    round_trip(changed)


@pytest.mark.parametrize("parameters", ["A, B", "", "Value = 0"])
def test_b2_signature_change_lists_calls_and_requires_confirmation(parameters):
    model = base()
    unit = next(u for u in model.code_units if u.name == "Compute")
    op = ManagerOperation(
        "signature",
        "algorithm",
        "update",
        target_id=unit.logical_id,
        patch=AlgorithmPatch(parameters=parameters),
    )
    plan = planned(model, op)
    assert not plan.failures
    notice = next(n for n in plan.notices if n.code == "algorithm_signature_calls")
    assert notice.references and all(":" in r for r in notice.references)
    if parameters != "Value = 0":
        assert "аргумент" in notice.message.lower()
    with pytest.raises(ManagerOperationError) as error:
        apply(
            model, (op,), expected_revision=model.revision, expected_preview_hash=plan.preview_hash
        )
    assert error.value.failures[0].reason == "confirmation_required"
    execute(model, op)


@pytest.mark.parametrize("value", ["(", ")"])
def test_b2_parentheses_in_strings_do_not_hide_argument_count(value):
    model = base()
    model = execute(
        model,
        ManagerOperation(
            "body",
            "handler",
            "update",
            target_id=model.pko[0].events[0].logical_id,
            patch=HandlerPatch(body=f'\n\tCompute("{value}", 1);\n'),
        ),
    )
    unit = next(u for u in model.code_units if u.name == "Compute")
    plan = planned(
        model,
        ManagerOperation(
            "parameters",
            "algorithm",
            "update",
            target_id=unit.logical_id,
            patch=AlgorithmPatch(parameters=""),
        ),
    )
    notice = next(n for n in plan.notices if n.code == "algorithm_signature_calls")
    assert "2 аргументов" in notice.message


def test_b2_function_to_procedure_reports_expression_and_unused_is_quiet():
    model = base()
    text = (
        render(model)
        .text.replace("Compute(1);", "R = Compute(1);")
        .replace("Процедура Compute", "Функция Compute")
    )
    start = text.index("Функция Compute")
    text = text[:start] + text[start:].replace("КонецПроцедуры", "КонецФункции", 1)
    model = imported(text)
    unit = next(u for u in model.code_units if u.name == "Compute")
    op = ManagerOperation(
        "kind",
        "algorithm",
        "update",
        target_id=unit.logical_id,
        patch=AlgorithmPatch(routine_kind="procedure"),
    )
    notice = next(n for n in planned(model, op).notices if n.code == "algorithm_signature_calls")
    assert "выражени" in notice.message and notice.references
    model = execute(
        model,
        ManagerOperation(
            "unused", "algorithm", "create", patch=AlgorithmPatch(name="Unused", body="")
        ),
    )
    unit = next(u for u in model.code_units if u.name == "Unused")
    quiet = planned(
        model,
        ManagerOperation(
            "quiet",
            "algorithm",
            "update",
            target_id=unit.logical_id,
            patch=AlgorithmPatch(parameters="Value"),
        ),
    )
    assert not quiet.failures and not quiet.notices


@pytest.mark.parametrize("variant", ["two_calls", "same_line", "comment", "indented_comment"])
def test_v1_non_template_frames_import_retained_and_round_trip(variant):
    model = base()
    text = render(model).text
    if variant == "two_calls":
        branch = branch_text(model, "ПКО_Item_ПриОтправкеДанных")
        text = text.replace(branch, branch + "\t\tCompute(1);\n")
        names = {"ВыполнитьПроцедуруМодуляМенеджера"}
    else:
        comment = {"same_line": "", "comment": "// c\n", "indented_comment": "\t// c\n"}[variant]
        text = text.replace(
            "Процедура Compute(Value)",
            "Процедура Fresh()\n\tX = 1;\n" + comment + "КонецПроцедуры Процедура Compute(Value)",
        )
        names = {"Fresh", "Compute"}
    model = imported(text)
    assert all(u.state == "retained" for u in model.code_units if u.name in names)
    assert any(e.reason for e in model.import_report.entries if e.state == "retained")
    assert render(model).text == text
    round_trip(model)


def test_v1_layout_overlap_is_retained_with_reason():
    model = base()
    first = branch_text(model, "ПКО_Item_ПриОтправкеДанных")
    second = branch_text(model, "ПКО_Other_ПриОтправкеДанных")
    text = render(model).text.replace(first, first + "\t\tCompute(1);\n")
    text = text.replace(second, second.split(" Тогда")[0] + " Тогда \n")
    model = imported(text)
    assert any(b.reason == "layout_overlap" for b in model.retained_blocks)
    assert render(model).text == text
    round_trip(model)


def test_v2_preprocessor_duplicate_methods_are_retained_and_diagnosed():
    text = render(base()).text
    alg = "Процедура Compute(Value)\n\tВозврат;\nКонецПроцедуры"
    text = text.replace(
        alg,
        "#Если Сервер Тогда\n"
        + alg
        + "\n#Иначе\n"
        + alg.replace("Compute", "compute")
        + "\n#КонецЕсли",
    )
    model = imported(text)
    units = [u for u in model.code_units if u.name.casefold() == "compute"]
    assert len(units) == 2 and all(u.state == "retained" for u in units)
    assert any(
        i.check == "ed.writer.name_collision"
        for i in validate_writer(model, render(model).data).issues
    )
    for unit in units:
        assert planned(
            model,
            ManagerOperation(
                "rename",
                "algorithm",
                "update",
                target_id=unit.logical_id,
                patch=AlgorithmPatch(name="Calc"),
            ),
        ).failures
    assert render(model).text == text
    round_trip(model)

    changed = execute(
        model,
        ManagerOperation(
            "unrelated",
            "algorithm",
            "create",
            patch=AlgorithmPatch(name="Fresh", body=""),
        ),
    )
    assert len([u for u in changed.code_units if u.name.casefold() == "compute"]) == 2
    round_trip(changed)


def test_v3_literal_case_mismatch_is_diagnosed_and_alias_preserved():
    model = base()
    name = "ПКО_Other_ПриОтправкеДанных"
    text = render(model).text
    mismatch = imported(text.replace('"' + name + '" Тогда', '"' + name.lower() + '" Тогда'))
    assert checks(mismatch)["ed.writer.handler_dispatcher"]
    alias = imported(text.replace('"' + name + '"', '"Alias"'))
    assert '"Alias"' in render(alias, "canonical").text
    assert next(c for c in alias.dispatcher_cases if c.name == "Alias").state == "retained"
    round_trip(alias)


@pytest.mark.parametrize(
    "symbol,label",
    [
        ("\r", "CR"),
        ("\x0b", "U+000B"),
        ("\x0c", "U+000C"),
        ("\x85", "U+0085"),
        ("\u2028", "U+2028"),
        ("\u2029", "U+2029"),
    ],
)
@pytest.mark.parametrize("kind", ["handler", "algorithm"])
def test_v4_forbidden_body_separators_are_rejected_with_position(symbol, label, kind):
    model = code_model()
    patch = (
        HandlerPatch(event="ПриОтправкеДанных", body="// a" + symbol + "Compute(5);")
        if kind == "handler"
        else AlgorithmPatch(name="Fresh", body="// a" + symbol + "Compute(5);")
    )
    plan = planned(
        model,
        ManagerOperation(
            "body",
            kind,
            "create",
            owner_id=model.pko[0].logical_id if kind == "handler" else None,
            patch=patch,
        ),
    )
    assert plan.failures and label in plan.failures[0].message and "4" in plan.failures[0].message


@pytest.mark.parametrize("kind", ["handler", "algorithm", "conversion_event"])
def test_v4_body_update_rejects_separator_and_create_normalizes_crlf(kind):
    model = base()
    if kind == "handler":
        target = model.pko[0].events[0].logical_id
        patch = HandlerPatch(body="// a\u2028Compute(5);")
    elif kind == "algorithm":
        target = next(u.logical_id for u in model.code_units if u.name == "Compute")
        patch = AlgorithmPatch(body="// a\u2028Compute(5);")
    else:
        from kd2_rules_mcp.authoring.ed.manager_operations import ConversionEventPatch

        target = model.conversion_events[0].logical_id
        patch = ConversionEventPatch(body="// a\u2028Compute(5);")
    assert planned(
        model, ManagerOperation("update", kind, "update", target_id=target, patch=patch)
    ).failures
    created = execute(
        model,
        ManagerOperation(
            "crlf",
            "algorithm",
            "create",
            patch=AlgorithmPatch(name="Fresh", body="// a\r\nX = 1;\r\n"),
        ),
    )
    assert next(u.body for u in created.code_units if u.name == "Fresh") == "\n\t// a\n\tX = 1;\n"
    round_trip(created)


@pytest.mark.parametrize("default,arguments,expected", [(False, 3, 1), (False, 5, 1), (True, 3, 0)])
def test_v5_dispatcher_argument_count_accounts_for_defaults(default, arguments, expected):
    model = base()
    name = "ПКО_Other_ПриОтправкеДанных"
    text = render(model).text
    branch = branch_text(model, name)
    start = branch.index(name + "(") + len(name) + 1
    end = branch.index(");", start)
    args = ["Параметры.ДанныеИБ"] * arguments
    text = text.replace(branch, branch[:start] + ", ".join(args) + branch[end:])
    if default:
        text = text.replace("СтекВыгрузки)\n\tY", "СтекВыгрузки = Неопределено)\n\tY")
    model = imported(text)
    assert checks(model)["ed.writer.dispatcher_arguments"] == expected


def test_m1_extended_event_does_not_block_other_code():
    model = base()
    text = render(model).text.replace("ПриОтправкеДанных", "ПослеКонвертацииОбъекта")
    model = imported(text)
    changed = execute(
        model,
        ManagerOperation(
            "fresh", "algorithm", "create", patch=AlgorithmPatch(name="Fresh", body="")
        ),
    )
    assert len(changed.dispatcher_cases) == len(model.dispatcher_cases)
    changed = handler(changed, event="ПриКонвертацииДанныхXDTO", client="another")
    round_trip(changed)


@pytest.mark.parametrize(
    "parameters,export",
    [
        ("Источник, Приемник, Компоненты, Стек", ""),
        ("данныеиб, данныеxdto, компонентыобмена, стеквыгрузки", " Экспорт"),
    ],
)
def test_m2_handler_signature_is_positional(parameters, export):
    text = render(base()).text.replace(
        "ДанныеИБ, ДанныеXDTO, КомпонентыОбмена, СтекВыгрузки)", parameters + ")" + export
    )
    assert not checks(imported(text))["ed.writer.handler_signature"]


@pytest.mark.parametrize(
    "name",
    [
        "ЗаполнитьПравилаОбработкиДанных",
        "ВерсияФорматаМенеджераОбмена",
        "ДобавитьПКО_Fresh",
        "ПКО_Item_ПриКонвертацииДанныхXDTO",
        "ПередОбработкойУдаляемогоОбъекта",
    ],
)
@pytest.mark.parametrize("action", ["create", "update"])
def test_m3_reserved_algorithm_names_are_rejected(name, action):
    model = base()
    unit = next(u for u in model.code_units if u.name == "Compute")
    patch = AlgorithmPatch(name=name, body="") if action == "create" else AlgorithmPatch(name=name)
    assert planned(
        model,
        ManagerOperation(
            "reserved",
            "algorithm",
            action,
            target_id=unit.logical_id if action == "update" else None,
            patch=patch,
        ),
    ).failures


@pytest.mark.parametrize("body", ["#КонецОбласти\nX = 1;", "#Область Broken\nX = 1;"])
@pytest.mark.parametrize("action", ["create", "update"])
def test_m4_unbalanced_regions_are_rejected(body, action):
    model = handler(code_model()) if action == "update" else code_model()
    patch = (
        HandlerPatch(body="\n" + body + "\n")
        if action == "update"
        else HandlerPatch(event="ПриОтправкеДанных", body=body)
    )
    assert planned(
        model,
        ManagerOperation(
            "regions",
            "handler",
            action,
            owner_id=model.pko[0].logical_id if action == "create" else None,
            target_id=model.pko[0].events[0].logical_id if action == "update" else None,
            patch=patch,
        ),
    ).failures


def test_m4_balanced_regions_ignore_comments_and_strings():
    model = handler(
        code_model(),
        body=(
            '// #КонецОбласти\nText = "#Область";\n'
            "#Область Outer\n#Область Inner\nX = 1;\n#КонецОбласти\n#КонецОбласти"
        ),
    )
    round_trip(model)


@pytest.mark.parametrize("placement", ["before_call", "after_call"])
def test_m5_canonical_keeps_dispatcher_internal_comment(placement):
    model = base()
    name = "ПКО_Item_ПриОтправкеДанных"
    branch = branch_text(model, name)
    commented = (
        branch.replace("\t\t" + name + "(", "\t\t// вызов Item\n\t\t" + name + "(")
        if placement == "before_call"
        else branch.replace(");\n", "); // вызов Item\n")
    )
    text = render(model).text.replace(branch, commented)
    model = imported(text)
    assert "// вызов Item" in render(model, "canonical").text
    round_trip(model)


def test_m5_neighbor_deletion_does_not_break_retained_branch():
    model = base()
    branch = branch_text(model, "ПКО_Other_ПриОтправкеДанных")
    model = imported(render(model).text.replace(branch, branch.replace(");\n", "); // Other\n")))
    plan = planned(
        model, ManagerOperation("delete", "pko", "delete", target_id=model.pko[0].logical_id)
    )
    assert plan.failures and plan.failures[0].reason == "opaque_context_changed"


@pytest.mark.parametrize("kind", ["handler", "pko"])
def test_m6_delete_keeps_section_comment(kind):
    name = "ПКО_Other_ПриОтправкеДанных"
    text = render(base()).text.replace(
        "Процедура " + name, "// ===== общий раздел =====\n// " + name + "\nПроцедура " + name
    )
    model = imported(text)
    key = model.pko[1].logical_id if kind == "pko" else model.pko[1].events[0].logical_id
    changed = execute(model, ManagerOperation("delete", kind, "delete", target_id=key))
    assert "// ===== общий раздел =====" in render(changed).text
    assert "// " + name not in render(changed).text
    round_trip(changed)


@pytest.mark.parametrize(
    "kind,name", [("pko", "ПКО_Other_ПриОтправкеДанных"), ("algorithm", "Compute")]
)
def test_m6_rename_keeps_owned_comment_in_frame(kind, name):
    text = render(base()).text.replace("Процедура " + name, "// " + name + "\nПроцедура " + name)
    model = imported(text)
    if kind == "pko":
        target = model.pko[1].logical_id
        patch = PkoPatch(name="Renamed")
        new_name = "ПКО_Renamed_ПриОтправкеДанных"
    else:
        target = next(u.logical_id for u in model.code_units if u.name == name)
        patch = AlgorithmPatch(name="Renamed")
        new_name = "Renamed"
    changed = execute(
        model, ManagerOperation("rename", kind, "update", target_id=target, patch=patch)
    )
    assert "// " + new_name + "\nПроцедура " + new_name in render(changed).text
    round_trip(changed)


def test_m7_rebinding_reports_orphaned_method():
    model = base()
    target = next(u for u in model.code_units if u.name == "ПКО_Item_ПриОтправкеДанных")
    op = ManagerOperation(
        "rebind",
        "handler",
        "update",
        target_id=model.pko[1].events[0].logical_id,
        patch=HandlerPatch(
            target=Reference("code_unit", target.logical_id, target.name, "resolved")
        ),
    )
    plan = planned(model, op)
    assert not plan.failures
    assert any(n.code == "orphan_handler" and n.references for n in plan.notices)
    assert checks(execute(model, op))["ed.writer.handler_binding"] == 1


def test_m8_self_recursion_does_not_count_as_algorithm_use():
    model = code_model()
    model = execute(
        model,
        ManagerOperation(
            "recursive",
            "algorithm",
            "create",
            patch=AlgorithmPatch(name="Recursive", body="Recursive();"),
        ),
    )
    assert checks(model)["ed.writer.algorithm_unused"] == 1
