"""Входы ревью круга: миграция, направленные адреса и обязательные колонки."""

from pathlib import Path
from unittest.mock import patch

import pytest

from kd_rules_mcp.authoring.ed.manager_operations import parse_operation, preview
from kd_rules_mcp.ed.reader import read_manager_text
from kd_rules_mcp.ed.writer import new_manager, render
from kd_rules_mcp.ed.writer_import import import_manager
from kd_rules_mcp.ed.writer_model import dump_model, load_model
from kd_rules_mcp.validation.ed_writer import validate_writer
from tests.test_ed_writer_code import round_trip
from tests.test_ed_writer_values import commit_batch

DATA = Path(__file__).parent / "data/ed/writer"


@pytest.mark.parametrize("release", ["w1", "w2", "w3"])
def test_imported_release_snapshot_preserves_ids_decisions_and_source(release):
    model = load_model((DATA / f"snapshot-import-{release}.ed.json").read_bytes())
    original = (DATA / "snapshot-import-source.bsl").read_bytes()
    assert render(model).data == original.replace(b'"Code", "Code"', b'"Number", "Code"')
    assert not [
        i for i in validate_writer(model, render(model).data).issues if i.check == "ed.writer.read"
    ]
    decision = next(d for d in model.decisions if d.client_id == "change-code")
    prop = model.pko[0].properties[0]
    assert decision.result_ids == (prop.logical_id,)
    assert all(g.state == "editable" for r in model.pko for g in r.groups)
    assert model.header.clear_data_column
    round_trip(model)
    replay = parse_operation(
        {
            "client_id": "change-code",
            "kind": "property",
            "action": "update",
            "target_id": prop.logical_id,
            "patch": {"configuration_property": "Number"},
        }
    )
    replayed = preview(model, (replay,), expected_revision=model.revision)
    assert not replayed.failures
    assert replayed.skipped == ("change-code",)
    changed = commit_batch(
        model,
        [
            parse_operation(
                {
                    "client_id": "change-again",
                    "kind": "property",
                    "action": "update",
                    "target_id": prop.logical_id,
                    "patch": {"configuration_property": "OtherCode"},
                }
            )
        ],
    )
    assert changed.decisions[: len(model.decisions)] == model.decisions
    assert changed.pko[0].logical_id == model.pko[0].logical_id
    round_trip(changed)


def test_current_imported_snapshot_does_not_repeat_migration():
    model = load_model((DATA / "snapshot-import-w2.ed.json").read_bytes())
    with patch(
        "kd_rules_mcp.ed.writer_snapshot.migrate_imported",
        side_effect=AssertionError("Повторная миграция"),
    ):
        assert load_model(dump_model(model)).revision == model.revision


def twins(direction="both"):
    rows = [
        {
            "client_id": "owner",
            "kind": "pko",
            "action": "create",
            "patch": {"name": "Owner", "directions": [direction]},
        },
        {
            "client_id": "send",
            "kind": "pko",
            "action": "create",
            "patch": {"name": "TwinSend", "directions": ["send"]},
        },
        {
            "client_id": "receive",
            "kind": "pko",
            "action": "create",
            "patch": {"name": "TwinReceive", "directions": ["receive"]},
        },
    ]
    model = commit_batch(new_manager(), [parse_operation(row) for row in rows])
    text = render(model).text.replace('"TwinSend"', '"Twin"').replace('"TwinReceive"', '"Twin"')
    return import_manager(read_manager_text(text), project_id=model.project_id)[0]


@pytest.mark.parametrize(
    "owner_direction,target_direction,accepted",
    [
        ("both", "send", False),
        ("both", "receive", False),
        ("send", "send", True),
        ("send", "receive", False),
        ("receive", "receive", True),
        ("receive", "send", False),
    ],
)
def test_explicit_twin_address_must_match_every_owner_direction(
    owner_direction, target_direction, accepted
):
    model = twins(owner_direction)
    op = parse_operation(
        {
            "client_id": "link",
            "kind": "property",
            "action": "create",
            "owner_id": {"address": "ПКО/Owner"},
            "patch": {
                "configuration_property": "Link",
                "format_property": "Link",
                "property_kind": "reference",
                "conversion": {"address": "ПКО/Twin~" + target_direction},
            },
        }
    )
    plan = preview(model, (op,), expected_revision=model.revision)
    assert not plan.failures if accepted else plan.failures
    if accepted:
        round_trip(plan.model)
    else:
        assert "исполнитель" in plan.failures[0].message
        assert target_direction in plan.failures[0].message


def test_twin_name_is_resolved_in_each_owner_direction():
    model = twins()
    changed = commit_batch(
        model,
        [
            parse_operation(
                {
                    "client_id": "link",
                    "kind": "property",
                    "action": "create",
                    "owner_id": {"address": "ПКО/Owner"},
                    "patch": {
                        "configuration_property": "Link",
                        "format_property": "Link",
                        "property_kind": "reference",
                        "conversion": {"kind": "pko", "name": "Twin"},
                    },
                }
            )
        ],
    )
    assert '"Link", "Link", , "Twin"' in render(changed).text
    round_trip(changed)
