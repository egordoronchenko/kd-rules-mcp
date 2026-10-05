"""W2 D2: собственный код, точные тела, зависимые рамки и отрицательные пробы."""

from dataclasses import replace

import pytest

from kd2_rules_mcp.authoring.ed.manager_operations import (
    AlgorithmPatch,
    ConversionEventPatch,
    HandlerPatch,
    ManagerOperation,
    ManagerOperationError,
    OperationKind,
    PkoPatch,
    PodPatch,
    PropertyPatch,
    apply,
    parse_operation,
    preview,
)
from kd2_rules_mcp.ed.canonical import canonicalize, model_addresses
from kd2_rules_mcp.ed.diff import compare_models
from kd2_rules_mcp.ed.reader import read_manager_text
from kd2_rules_mcp.ed.writer import new_manager, render
from kd2_rules_mcp.ed.writer_import import code_occurrences, import_manager
from kd2_rules_mcp.ed.writer_model import Reference, TextStyle, dump_model, load_model
from kd2_rules_mcp.validation.ed_writer import _code_checks
from kd2_rules_mcp.validation.report import ValidationReport


def execute(model, op):
    plan = preview(model, (op,), expected_revision=model.revision)
    assert not plan.failures, plan.failures
    return apply(
        model,
        (op,),
        expected_revision=model.revision,
        expected_preview_hash=plan.preview_hash,
        confirmations=tuple((n.code, n.notice_hash) for n in plan.notices),
    )


def round_trip(model):
    assert canonicalize(load_model(dump_model(model))) == canonicalize(model)
    for mode in ("preserve", "canonical"):
        output = render(model, mode)
        back = import_manager(
            read_manager_text(output.data.decode("utf-8")),
            project_id=model.project_id,
            manager_name=model.header.manager_name,
        )[0]
        diff = compare_models(model, back)
        assert diff.equal, [(c.address, c.fields) for c in diff.changes]
        assert canonicalize(model) == canonicalize(back)
        assert render(back, mode).data == output.data


def code_model():
    model = new_manager(style=TextStyle(newline="\n", bom=False))
    return execute(
        model,
        ManagerOperation(
            "rule", "pko", "create", patch=PkoPatch(name="Item", directions=("send", "receive"))
        ),
    )


def handler(model, event="ПриОтправкеДанных", body="X = 1;", client="handler"):
    return execute(
        model,
        ManagerOperation(
            client,
            "handler",
            "create",
            owner_id=model.pko[0].logical_id,
            patch=HandlerPatch(event=event, body=body),
        ),
    )


def code_report(model):
    report = ValidationReport()
    _code_checks(
        model, read_manager_text(render(model).data.decode("utf-8")), report, model_addresses(model)
    )
    return report


def test_handler_create_replace_delete_and_exact_body():
    model = handler(code_model(), body="X = 1;\n\nY = 2;")
    unit = next(u for u in model.code_units if "handler" in u.roles)
    assert unit.body == "\n\tX = 1;\n\t\n\tY = 2;\n"
    assert model.dispatcher_cases[0].target.target_id == unit.logical_id
    round_trip(model)
    body = '\r\n  // spacing\r\n  Text = "first\r\n|second";\r\n\t\r\n'
    model = execute(
        model,
        ManagerOperation(
            "body",
            "handler",
            "update",
            target_id=model.pko[0].events[0].logical_id,
            patch=HandlerPatch(body=body),
        ),
    )
    assert next(u for u in model.code_units if u.logical_id == unit.logical_id).body == body
    assert body.encode() in render(model, "canonical").data
    round_trip(model)
    model = execute(
        model, ManagerOperation("delete", "handler", "delete", target_id=unit.logical_id)
    )
    assert not model.pko[0].events and not model.dispatcher_cases
    assert all(u.logical_id != unit.logical_id for u in model.code_units)
    round_trip(model)


@pytest.mark.parametrize(
    "direction,event,kind,first",
    [
        ("send", "ПриОбработке", "procedure", "ДанныеИБ"),
        ("receive", "ПриОбработке", "procedure", "ДанныеXDTO"),
        ("send", "ВыборкаДанных", "function", "КомпонентыОбмена"),
    ],
)
def test_pod_handler_signatures(direction, event, kind, first):
    model = new_manager(style=TextStyle(newline="\n", bom=False))
    model = execute(
        model,
        ManagerOperation(
            "pod", "pod", "create", patch=PodPatch(name="Items", directions=(direction,))
        ),
    )
    model = execute(
        model,
        ManagerOperation(
            "h",
            "handler",
            "create",
            owner_id=model.pod[0].logical_id,
            patch=HandlerPatch(event=event, body="ReturnValue = 1;"),
        ),
    )
    unit = next(u for u in model.code_units if "handler" in u.roles)
    assert unit.signature.routine_kind == kind and unit.signature.parameters[0].name == first
    assert not code_report(model).issues
    round_trip(model)


def test_conversion_event_replacement_and_clear():
    model = new_manager(style=TextStyle(newline="\n", bom=False))
    event = model.conversion_events[0]
    body = '\n\tMessage = """";\n\t// untouched\n'
    model = execute(
        model,
        ManagerOperation(
            "event",
            "conversion_event",
            "update",
            target_id=event.logical_id,
            patch=ConversionEventPatch(body=body),
        ),
    )
    assert body in render(model, "canonical").text
    round_trip(model)
    model = execute(
        model,
        ManagerOperation(
            "clear", "conversion_event", "update", target_id=event.logical_id, clear=("body",)
        ),
    )
    assert next(u for u in model.code_units if u.logical_id == event.target.target_id).body == "\n"
    round_trip(model)


def test_algorithm_rename_calls_only_and_dependencies():
    model = code_model()
    model = execute(
        model,
        ManagerOperation(
            "algorithm",
            "algorithm",
            "create",
            patch=AlgorithmPatch(
                name="Compute",
                routine_kind="function",
                parameters='Знач Value = "x"',
                exported=True,
                body="Возврат Value;",
            ),
        ),
    )
    algorithm = next(u for u in model.code_units if u.name == "Compute")
    model = handler(
        model, body='Result = Compute();\nOther.Compute();\nName = "Compute";\n// Compute();'
    )
    occurrences = [o for o in code_occurrences(model) if o.target_id == algorithm.logical_id]
    assert [o.kind for o in occurrences] == ["algorithm_call", "algorithm_literal"]
    op = ManagerOperation(
        "rename",
        "algorithm",
        "update",
        target_id=algorithm.logical_id,
        patch=AlgorithmPatch(name="Changed", parameters='Знач Value = "long"'),
    )
    plan = preview(model, (op,), expected_revision=model.revision)
    assert not plan.failures
    assert {n.code for n in plan.notices} == {
        "computed_algorithm_calls",
        "code_name_literals",
        "algorithm_signature_calls",
    }
    with pytest.raises(ManagerOperationError):
        apply(
            model, (op,), expected_revision=model.revision, expected_preview_hash=plan.preview_hash
        )
    model = execute(model, op)
    text = render(model).text
    assert "Result = Changed();" in text and "Other.Compute();" in text
    assert 'Name = "Compute";' in text and "// Compute();" in text
    round_trip(model)
    plan = preview(
        model,
        (ManagerOperation("delete", "algorithm", "delete", target_id=algorithm.logical_id),),
        expected_revision=model.revision,
    )
    assert plan.failures[0].reason == "dangling_reference"


def test_deferred_algorithm_binding_and_signature():
    model = code_model()
    model = execute(
        model,
        ManagerOperation(
            "algorithm",
            "algorithm",
            "create",
            patch=AlgorithmPatch(
                name="Deferred", parameters="Объект, ПараметрыКонвертации", body="X = 1;"
            ),
        ),
    )
    algorithm = next(u for u in model.code_units if u.name == "Deferred")
    model = execute(
        model,
        ManagerOperation(
            "bind",
            "handler",
            "create",
            owner_id=model.pko[0].logical_id,
            patch=HandlerPatch(
                event="ПослеЗагрузкиВсехДанных",
                target=Reference("code_unit", algorithm.logical_id, algorithm.name, "resolved"),
            ),
        ),
    )
    assert "Параметры.КомпонентыОбмена.ПараметрыКонвертации" in render(model).text
    round_trip(model)
    bad = ManagerOperation(
        "function",
        "algorithm",
        "update",
        target_id=algorithm.logical_id,
        patch=AlgorithmPatch(routine_kind="function"),
    )
    assert preview(model, (bad,), expected_revision=model.revision).failures
    model = execute(
        model,
        ManagerOperation(
            "sig",
            "algorithm",
            "update",
            target_id=algorithm.logical_id,
            patch=AlgorithmPatch(parameters="Объект"),
        ),
    )
    assert len(model.dispatcher_cases[0].arguments) == 2
    assert any(i.check == "ed.writer.dispatcher_arguments" for i in code_report(model).issues)
    round_trip(model)
    model = execute(
        model,
        ManagerOperation(
            "restore",
            "handler",
            "update",
            target_id=model.pko[0].events[0].logical_id,
            patch=HandlerPatch(restore_dispatcher=True),
        ),
    )
    assert len(model.dispatcher_cases[0].arguments) == 1
    round_trip(model)
    model = execute(
        model,
        ManagerOperation(
            "unbind", "handler", "delete", target_id=model.pko[0].events[0].logical_id
        ),
    )
    model = execute(
        model, ManagerOperation("delete", "algorithm", "delete", target_id=algorithm.logical_id)
    )
    assert not model.dispatcher_cases
    round_trip(model)


def test_rule_rename_updates_frames_and_keeps_code_literals():
    model = handler(code_model(), body='Rule = "Item";\nKey = Components.ПКОПоИмени(Variable);')
    owner = model.pko[0]
    model = execute(
        model,
        ManagerOperation(
            "ref",
            "property",
            "create",
            owner_id=owner.logical_id,
            patch=PropertyPatch(
                configuration_property="Parent",
                format_property="Parent",
                property_kind="reference",
                conversion=Reference("pko", owner.logical_id, owner.name, "resolved"),
            ),
        ),
    )
    op = ManagerOperation(
        "rename", "pko", "update", target_id=owner.logical_id, patch=PkoPatch(name="Renamed")
    )
    plan = preview(model, (op,), expected_revision=model.revision)
    assert not plan.failures and any(n.code == "code_name_literals" for n in plan.notices)
    model = execute(model, op)
    assert model.pko[0].properties[0].conversion.name == "Renamed"
    unit = next(u for u in model.code_units if "handler" in u.roles)
    assert unit.name == "ПКО_Renamed_ПриОтправкеДанных" and 'Rule = "Item";' in unit.body
    assert model.dispatcher_cases[0].name == unit.name
    round_trip(model)
    model = execute(model, ManagerOperation("delete", "pko", "delete", target_id=owner.logical_id))
    assert not model.pko and not model.dispatcher_cases
    round_trip(model)


def test_missing_binding_is_editable_and_not_repaired():
    model = handler(code_model())
    text = render(model).text.replace('"ПКО_Item_ПриОтправкеДанных";', '"Absent";')
    model, _ = import_manager(read_manager_text(text), project_id=model.project_id)
    event = model.pko[0].events[0]
    assert event.state == "editable" and event.target.resolution == "missing"
    assert render(model).text == text
    checks = {i.check for i in code_report(model).issues}
    assert "ed.writer.binding_method" in checks and "ed.writer.handler_binding" in checks
    round_trip(model)


def test_json_schema_and_idempotent_code_operations():
    model = code_model()
    op = parse_operation(
        {
            "client_id": "handler",
            "kind": "handler",
            "action": "create",
            "owner_id": model.pko[0].logical_id,
            "patch": {"event": "ПриОтправкеДанных", "body": "X = 1;"},
        }
    )
    plan = preview(model, (op,), expected_revision=model.revision)
    changed = execute(model, op)
    assert (
        apply(
            changed,
            (op,),
            expected_revision=model.revision,
            expected_preview_hash=plan.preview_hash,
        )
        is changed
    )
    with pytest.raises(ValueError):
        parse_operation(
            {"client_id": "x", "kind": "algorithm", "action": "create", "patch": {"unknown": True}}
        )
    bad = replace(op, client_id="bad", patch=HandlerPatch(event="ПослеКонвертацииОбъекта", body=""))
    assert preview(model, (bad,), expected_revision=model.revision).failures


def test_repeated_rule_rename_changes_only_reference_token_in_retained_group():
    model = code_model()
    text = render(model).text.replace(
        "\tСвойстваШапки = ПравилоКонвертации.Свойства;",
        "\tСвойстваШапки = ПравилоКонвертации.Свойства;\n"
        '\tСвойстваТЧ = ДобавитьПКТЧ(ПравилоКонвертации, "Rows", "Rows");\n'
        '\tДобавитьПКС(СвойстваТЧ, "Ref", "Ref", 0, "Item");\n'
        '\tДобавитьПКС(СвойстваТЧ, "Other", "Other", , "Item");',
    )
    model, _ = import_manager(read_manager_text(text), project_id=model.project_id)
    owner = model.pko[0]
    assert owner.groups[0].state == "retained"
    for n, name in enumerate(("LongerRuleName", "Short")):
        model = execute(
            model,
            ManagerOperation(
                f"rename{n}", "pko", "update", target_id=owner.logical_id, patch=PkoPatch(name=name)
            ),
        )
        assert all(p.conversion.name == name for p in model.pko[0].groups[0].properties)
        assert model.pko[0].groups[0].state == "retained"
        round_trip(model)


def test_exact_body_with_indentation_before_closing():
    model = handler(code_model())
    event = model.pko[0].events[0]
    body = "\n\tX = 2;\n  "
    model = execute(
        model,
        ManagerOperation(
            "body-indent",
            "handler",
            "update",
            target_id=event.logical_id,
            patch=HandlerPatch(body=body),
        ),
    )
    assert body in render(model, "canonical").text
    round_trip(model)


def test_handler_external_call_protects_deletion_and_reports_rename():
    model = handler(code_model())
    method = next(u for u in model.code_units if "handler" in u.roles)
    model = execute(
        model,
        ManagerOperation(
            "caller",
            "algorithm",
            "create",
            patch=AlgorithmPatch(name="Caller", body=method.name + "();"),
        ),
    )
    targets: tuple[tuple[OperationKind, str], ...] = (
        ("handler", method.logical_id),
        ("pko", model.pko[0].logical_id),
    )
    for kind, target in targets:
        plan = preview(
            model,
            (ManagerOperation("delete-" + kind, kind, "delete", target_id=target),),
            expected_revision=model.revision,
        )
        assert plan.failures[0].reason == "dangling_reference"
        assert plan.failures[0].references
    op = ManagerOperation(
        "rename",
        "pko",
        "update",
        target_id=model.pko[0].logical_id,
        patch=PkoPatch(name="Other"),
    )
    plan = preview(model, (op,), expected_revision=model.revision)
    assert not plan.failures
    assert any(n.code == "code_handler_references" for n in plan.notices)


def test_missing_binding_can_be_explicitly_rebound_to_existing_code():
    model = handler(code_model())
    method = next(u for u in model.code_units if "handler" in u.roles)
    text = render(model).text.replace('"ПКО_Item_ПриОтправкеДанных";', '"Absent";')
    model, _ = import_manager(read_manager_text(text), project_id=model.project_id)
    method = next(u for u in model.code_units if u.name == method.name)
    model = execute(
        model,
        ManagerOperation(
            "rebind",
            "handler",
            "update",
            target_id=model.pko[0].events[0].logical_id,
            patch=HandlerPatch(
                target=Reference("code_unit", method.logical_id, method.name, "resolved")
            ),
        ),
    )
    assert not code_report(model).issues
    round_trip(model)


def test_function_dispatcher_is_created_and_removed_with_last_branch():
    source = render(new_manager(style=TextStyle(newline="\n", bom=False))).text
    document = read_manager_text(source)
    function = next(
        r for r in document.routines if "dispatcher" in r.roles and r.routine_kind == "function"
    )
    model = import_manager(
        read_manager_text(source.replace(function.raw_text, "")), project_id="function"
    )[0]
    model = execute(
        model,
        ManagerOperation("pod", "pod", "create", patch=PodPatch(name="Rows", directions=("send",))),
    )
    model = execute(
        model,
        ManagerOperation(
            "function",
            "handler",
            "create",
            owner_id=model.pod[0].logical_id,
            patch=HandlerPatch(event="ВыборкаДанных", body="Возврат 1;"),
        ),
    )
    assert (
        sum(
            "dispatcher" in u.roles and u.signature.routine_kind == "function"
            for u in model.code_units
        )
        == 1
    )
    round_trip(model)
    model = execute(
        model,
        ManagerOperation(
            "delete", "handler", "delete", target_id=model.pod[0].events[0].logical_id
        ),
    )
    assert not any(
        "dispatcher" in u.roles and u.signature.routine_kind == "function" for u in model.code_units
    )
    round_trip(model)


def test_algorithm_parameter_expression_is_retained_exactly_in_frame():
    model = execute(
        code_model(),
        ManagerOperation(
            "expression",
            "algorithm",
            "create",
            patch=AlgorithmPatch(
                name="Expression", parameters="Value = GlobalDefault", body="X = Value;"
            ),
        ),
    )
    assert "Expression(Value = GlobalDefault)" in render(model, "canonical").text
    round_trip(model)


def test_dispatcher_unknown_else_is_not_regenerated():
    model = handler(code_model())
    text = render(model).text.replace(
        "\tКонецЕсли;\nКонецПроцедуры",
        "\tИначе\n\t\tOther();\n\tКонецЕсли;\nКонецПроцедуры",
    )
    model = import_manager(read_manager_text(text), project_id=model.project_id)[0]
    unit = next(
        u
        for u in model.code_units
        if "dispatcher" in u.roles and u.signature.routine_kind == "procedure"
    )
    assert unit.state == "retained"
    assert render(model).text == text
    plan = preview(
        model,
        (
            ManagerOperation(
                "rename",
                "pko",
                "update",
                target_id=model.pko[0].logical_id,
                patch=PkoPatch(name="Other"),
            ),
        ),
        expected_revision=model.revision,
    )
    assert plan.failures[0].reason == "opaque_context_changed"


def test_algorithm_call_in_opaque_declaration_is_indexed_for_deletion():
    model = execute(
        code_model(),
        ManagerOperation(
            "algorithm", "algorithm", "create", patch=AlgorithmPatch(name="OpaqueCall", body="")
        ),
    )
    text = render(model).text.replace(
        "\tСвойстваШапки = ПравилоКонвертации.Свойства;",
        "\tСвойстваШапки = ПравилоКонвертации.Свойства;\n\tOpaqueCall();",
    )
    model = import_manager(read_manager_text(text), project_id=model.project_id)[0]
    algorithm = next(u for u in model.code_units if u.name == "OpaqueCall")
    occurrences = [o for o in code_occurrences(model) if o.target_id == algorithm.logical_id]
    assert [o.kind for o in occurrences] == ["algorithm_call"]
    for operation, reason in (
        (
            ManagerOperation("delete", "algorithm", "delete", target_id=algorithm.logical_id),
            "dangling_reference",
        ),
        (
            ManagerOperation(
                "update",
                "algorithm",
                "update",
                target_id=algorithm.logical_id,
                patch=AlgorithmPatch(name="Changed"),
            ),
            "opaque_context_changed",
        ),
    ):
        plan = preview(
            model,
            (operation,),
            expected_revision=model.revision,
        )
        assert plan.failures[0].reason == reason and plan.failures[0].references


def test_orphan_handler_deletion_removes_its_branch():
    model = handler(code_model())
    output = render(model)
    binding = next(e for e in output.report.entries if e.kind == "binding")
    text = (output.data[: binding.byte_start] + output.data[binding.byte_end :]).decode("utf-8")
    model = import_manager(read_manager_text(text), project_id=model.project_id)[0]
    method = next(u for u in model.code_units if "handler" in u.roles)
    assert not model.pko[0].events and model.dispatcher_cases
    model = execute(
        model, ManagerOperation("orphan", "handler", "delete", target_id=method.logical_id)
    )
    assert not model.dispatcher_cases
    assert all(u.logical_id != method.logical_id for u in model.code_units)
    round_trip(model)
