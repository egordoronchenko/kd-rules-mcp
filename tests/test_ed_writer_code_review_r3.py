"""Остатки W2: точный литерал восстановления, определения и ссылки пакета."""

import pytest

from kd_rules_mcp.authoring.ed.manager_operations import (
    HandlerPatch,
    ManagerOperation,
    parse_operation,
    preview,
)
from kd_rules_mcp.ed.writer import render
from tests.test_ed_writer_code import execute, round_trip
from tests.test_ed_writer_code_review import base, branch_text, checks, imported, planned
from tests.test_ed_writer_code_review_r2 import OTHER


@pytest.mark.parametrize(
    "alias,branch_exists", [(OTHER.lower(), False), ("Alias", True), (OTHER, True)]
)
def test_n5_restore_uses_binding_literal_and_existing_alias_is_noop(alias, branch_exists):
    original = base()
    text = render(original).text
    branch = branch_text(original, OTHER)
    preserved_branch = branch.replace(f'"{OTHER}"', f'"{alias}"').replace(
        OTHER + "(", OTHER.lower() + "("
    )
    text = text.replace(branch, preserved_branch if branch_exists else "")
    text = text.replace(f'= "{OTHER}";', f'= "{alias}";')
    model = imported(text)
    operation = ManagerOperation(
        "restore",
        "handler",
        "update",
        target_id=model.pko[1].events[0].logical_id,
        patch=HandlerPatch(restore_dispatcher=True),
    )
    plan = planned(model, operation)
    assert not plan.failures
    assert bool([n for n in plan.notices if n.code == "handler_execution_changed"]) != branch_exists
    changed = execute(model, operation)
    assert sum(c.name == alias for c in changed.dispatcher_cases) == 1
    if alias != OTHER:
        assert not any(c.name == OTHER for c in changed.dispatcher_cases)
    assert not checks(changed)["ed.writer.handler_dispatcher"]
    if branch_exists:
        assert render(changed).text == text
    round_trip(changed)


def test_n6_retained_handler_definition_is_not_an_external_use():
    header = f"Процедура {OTHER}(ДанныеИБ, ДанныеXDTO, КомпонентыОбмена, СтекВыгрузки)"
    text = render(base()).text.replace(
        header, header.replace("СтекВыгрузки)", "СтекВыгрузки = ТекущаяДата())")
    )
    model = imported(text)
    assert next(u for u in model.code_units if u.name == OTHER).state == "retained"
    operation = ManagerOperation("delete", "pko", "delete", target_id=model.pko[1].logical_id)
    assert not planned(model, operation).failures
    round_trip(execute(model, operation))


def test_move_requires_a_destination():
    with pytest.raises(ValueError, match="Перемещение требует"):
        parse_operation(
            {"client_id": "move", "kind": "property", "action": "move", "target_id": "x"}
        )
    assert (
        parse_operation(
            {
                "client_id": "move",
                "kind": "property",
                "action": "move",
                "target_id": "x",
                "after_id": None,
            }
        ).after_id
        is None
    )


@pytest.mark.parametrize("source_kind", ["handler", "algorithm"])
def test_handler_target_resolves_earlier_method_client_id(source_kind):
    model = base()
    prefix = []
    if source_kind == "algorithm":
        prefix.append(
            {
                "client_id": "method",
                "kind": "algorithm",
                "action": "create",
                "patch": {
                    "name": "SendMethod",
                    "parameters": "ДанныеИБ, ДанныеXDTO, КомпонентыОбмена, СтекВыгрузки",
                    "body": "Возврат;",
                },
            }
        )
    else:
        prefix.append(
            {
                "client_id": "source",
                "kind": "pko",
                "action": "create",
                "patch": {"name": "Source", "directions": ["send"]},
            }
        )
        prefix.append(
            {
                "client_id": "method",
                "kind": "handler",
                "action": "create",
                "owner_id": {"client_id": "source"},
                "patch": {"event": "ПриОтправкеДанных", "body": "Возврат;"},
            }
        )
    packet = tuple(
        parse_operation(p)
        for p in [
            *prefix,
            {
                "client_id": "bind",
                "kind": "handler",
                "action": "update",
                "target_id": model.pko[1].events[0].logical_id,
                "patch": {"target": {"client_id": "method"}},
            },
        ]
    )
    plan = preview(model, packet, expected_revision=model.revision)
    assert not plan.failures, plan.failures
    event = plan.model.pko[1].events[0]
    assert event.target.target_id in {u.logical_id for u in plan.model.code_units}
    assert (
        event.target.name.endswith("ПриОтправкеДанных")
        if source_kind == "handler"
        else event.target.name == "SendMethod"
    )
