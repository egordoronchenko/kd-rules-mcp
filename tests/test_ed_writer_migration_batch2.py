"""Входы второго захода переноса: старые снимки, поиск и адресные ссылки."""

import base64
import json
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import pytest

from kd2_rules_mcp.authoring.ed.manager_operations import (
    ManagerOperation,
    PkoPatch,
    apply,
    parse_operation,
    preview,
)
from kd2_rules_mcp.authoring.ed.workspace import ManagerWorkspace
from kd2_rules_mcp.ed.writer import new_manager, render
from kd2_rules_mcp.ed.writer_model import (
    SNAPSHOT_SCHEMA_SHA256,
    SNAPSHOT_STORAGE_VERSION,
    dump_model,
    load_model,
    pack_json,
    snapshot_schema_fingerprint,
    text_hash,
    unpack_json,
)
from tests.test_ed_writer_code import execute, round_trip

DATA = Path(__file__).parent / "data/ed/writer"


@pytest.mark.parametrize("release", ["w1", "w2"])
def test_old_snapshot_accepts_new_entity_families(release):
    model = load_model((DATA / f"snapshot-{release}.ed.json").read_bytes())
    rule = model.pko[0]
    packets = [
        {
            "client_id": "send",
            "kind": "handler",
            "action": "create",
            "owner_id": rule.logical_id,
            "patch": {"event": "ПриОтправкеДанных", "body": "ДанныеXDTO = Неопределено;"},
        },
        {
            "client_id": "predefined",
            "kind": "pkpd",
            "action": "create",
            "patch": {
                "name": "Colors",
                "directions": ["both"],
                "configuration_type": {
                    "state": "reference",
                    "reference_parts": ["Метаданные", "Перечисления", "Colors"],
                },
                "format_type": {"state": "string", "value": "Color"},
            },
        },
        {
            "client_id": "table",
            "kind": "table_part",
            "action": "create",
            "owner_id": rule.logical_id,
            "patch": {"configuration_property": "Rows", "format_property": "Rows"},
        },
        {
            "client_id": "parameter",
            "kind": "parameter",
            "action": "create",
            "patch": {"name": "Flag", "default": {"state": "boolean", "value": False}},
        },
        {
            "client_id": "algorithm",
            "kind": "algorithm",
            "action": "create",
            "patch": {"name": "Normalize", "body": "Возврат;"},
        },
        {
            "client_id": "conversion",
            "kind": "conversion_event",
            "action": "update",
            "address": "Событие/ПередКонвертацией",
            "patch": {"body": "\n\tВозврат;\n"},
        },
    ]
    operations = tuple(parse_operation(p) for p in packets)
    plan = preview(model, operations, expected_revision=model.revision)
    assert not plan.failures, plan.failures
    changed = apply(
        model,
        operations,
        expected_revision=model.revision,
        expected_preview_hash=plan.preview_hash,
        confirmations=tuple((n.code, n.notice_hash) for n in plan.notices),
    )
    assert changed.pko[0].logical_id == rule.logical_id
    round_trip(changed)


def bank_model():
    return execute(
        new_manager(),
        ManagerOperation(
            "bank", "pko", "create", patch=PkoPatch(name="Bank", directions=("receive",))
        ),
    )


def test_search_handler_generator_frame_and_dispatcher_are_editable():
    model = bank_model()
    model = execute(
        model,
        parse_operation(
            {
                "client_id": "search",
                "kind": "handler",
                "action": "create",
                "owner_id": model.pko[0].logical_id,
                "patch": {"event": "АлгоритмПоиска", "body": "ДанныеИБ = Неопределено;"},
            }
        ),
    )
    text = render(model).text
    assert "Процедура ПКО_Bank_АлгоритмПоиска(ДанныеИБ, ПолученныеДанные, КомпонентыОбмена)" in text
    assert "ПравилоКонвертации.АлгоритмПоиска" in text
    assert (
        "ПКО_Bank_АлгоритмПоиска(Параметры.ДанныеИБ,Параметры.ПолученныеДанные,Параметры.КомпонентыОбмена);"
        in "".join(text.split())
    )
    round_trip(model)


def test_address_owner_from_previous_packet_accepts_handler_and_property():
    model = bank_model()
    for packet in (
        {
            "client_id": "handler",
            "kind": "handler",
            "action": "create",
            "owner_id": {"address": "ПКО/Bank"},
            "patch": {"event": "ПередЗаписьюПолученныхДанных", "body": "ДанныеИБ = Неопределено;"},
        },
        {
            "client_id": "property",
            "kind": "property",
            "action": "create",
            "owner_id": {"address": "ПКО/Bank"},
            "patch": {"configuration_property": "Code", "format_property": "Code"},
        },
    ):
        model = execute(model, parse_operation(packet))
    round_trip(model)


def test_snapshot_schema_is_pinned_to_version_and_rejects_unknown_schema():
    known = {5: "ecfb5f2da1648d8ab72be2f0e0b26863bcbc062c5830bab76d366b1f038e3759"}
    assert (
        snapshot_schema_fingerprint() == SNAPSHOT_SCHEMA_SHA256 == known[SNAPSHOT_STORAGE_VERSION]
    )
    payload = json.loads(dump_model(bank_model()))
    assert payload["storage_version"] == SNAPSHOT_STORAGE_VERSION
    payload["schema_sha256"] = "0" * 64
    with pytest.raises(ValueError, match="схема"):
        load_model(json.dumps(payload))


def test_legacy_revision_is_verified_before_migration():
    payload = json.loads((DATA / "snapshot-w1.ed.json").read_bytes())
    value = unpack_json(payload["model"])
    value["pko"][0]["name"] = "ChangedWithoutRevision"
    payload["model"] = pack_json(value)
    with pytest.raises(ValueError, match="Ревизия"):
        load_model(json.dumps(payload))


@pytest.mark.parametrize("version", [0, 6])
def test_unknown_snapshot_version_is_rejected(version):
    payload = json.loads((DATA / "snapshot-w1.ed.json").read_bytes())
    payload["storage_version"] = version
    with pytest.raises(ValueError, match="формат"):
        load_model(json.dumps(payload))


@pytest.mark.parametrize("release", ["w1", "w2"])
def test_workspace_reads_legacy_without_writing_and_saves_current_version(tmp_path, release):
    payload = json.loads((DATA / f"snapshot-{release}.ed.json").read_bytes())
    folder = tmp_path / ".ed-projects" / ("legacy-" + release)
    (folder / "bodies").mkdir(parents=True)
    for key, content in payload.pop("blobs").items():
        (folder / "bodies" / f"{key}.bsl").write_bytes(base64.b64decode(content))
    payload["blob_hashes"] = [p.stem for p in (folder / "bodies").iterdir()]
    (folder / "import-report.json").write_text(json.dumps(payload.pop("import_report")), "utf-8")
    manifest = folder / "manager.ed.json"
    manifest.write_text(json.dumps(payload), "utf-8")
    before = manifest.read_bytes()
    workspace = ManagerWorkspace(tmp_path)
    project = workspace.get("legacy-" + release)
    assert manifest.read_bytes() == before
    op = parse_operation(
        {
            "client_id": "send",
            "kind": "handler",
            "action": "create",
            "owner_id": project.model.pko[0].logical_id,
            "patch": {"event": "ПриОтправкеДанных", "body": "ДанныеXDTO = Неопределено;"},
        }
    )
    plan = workspace.preview(project.id, (op,), expected_revision=project.model.revision)
    assert not plan.failures
    changed = workspace.apply(
        project.id,
        (op,),
        expected_revision=project.model.revision,
        expected_preview_hash=plan.preview_hash,
    )
    assert json.loads(manifest.read_bytes())["storage_version"] == SNAPSHOT_STORAGE_VERSION
    assert ManagerWorkspace(tmp_path).get(project.id).model == changed.model
    round_trip(changed.model)


def test_snapshot_migration_keeps_nonempty_retained_dispatcher():
    with patch("kd2_rules_mcp.ed.writer_snapshot.migrate_v3", side_effect=lambda m: m):
        model = load_model((DATA / "snapshot-w1.ed.json").read_bytes())
    unit = next(u for u in model.code_units if u.name == "ВыполнитьПроцедуруМодуляМенеджера")
    leaf = next(e for c in model.layouts for e in c.elements if e.entity_id == unit.logical_id)
    body = "\n\tX = 1;\n"
    units = tuple(
        replace(u, body=body, sha256=text_hash(body)) if u == unit else u for u in model.code_units
    )
    blocks = tuple(
        replace(
            b, text=b.text.replace("\n\t\n", body), sha256=text_hash(b.text.replace("\n\t\n", body))
        )
        if b.logical_id == leaf.block_id
        else b
        for b in model.retained_blocks
    )
    payload = json.loads(
        dump_model(replace(model, code_units=units, retained_blocks=blocks).with_revision())
    )
    payload["storage_version"] = 3
    migrated = load_model(json.dumps(payload))
    assert (
        next(u for u in migrated.code_units if u.logical_id == unit.logical_id).state == "retained"
    )
    assert (
        next(b for b in migrated.retained_blocks if b.logical_id == leaf.block_id).text
        == next(b for b in blocks if b.logical_id == leaf.block_id).text
    )


def test_address_references_resolve_all_positions_and_conversion_targets_sequentially():
    model = bank_model()
    packets = [
        {
            "client_id": "target",
            "kind": "pko",
            "action": "create",
            "patch": {"name": "Target", "directions": ["both"]},
        },
        {
            "client_id": "table",
            "kind": "table_part",
            "action": "create",
            "owner_id": {"address": "ПКО/Bank"},
            "patch": {"configuration_property": "Rows", "format_property": "Rows"},
        },
        {
            "client_id": "link",
            "kind": "property",
            "action": "create",
            "owner_id": {"address": "ПКО/Bank/ПКТЧ/Rows"},
            "patch": {
                "configuration_property": "Link",
                "format_property": "Link",
                "property_kind": "reference",
                "conversion": {"address": "ПКО/Target"},
            },
        },
        {
            "client_id": "tail",
            "kind": "property",
            "action": "create",
            "owner_id": {"address": "ПКО/Bank/ПКТЧ/Rows"},
            "container_id": {"address": "ПКО/Bank/ПКТЧ/Rows"},
            "after_id": {"address": "ПКО/Bank/ПКТЧ/Rows/ПКС/Link"},
            "patch": {
                "configuration_property": "Tail",
                "format_property": "Tail",
                "property_kind": "reference",
                "conversion": {"kind": "pko", "target_id": {"address": "ПКО/Target"}},
            },
        },
        {
            "client_id": "move",
            "kind": "property",
            "action": "move",
            "target_id": {"address": "ПКО/Bank/ПКТЧ/Rows/ПКС/Link"},
            "container_id": {"address": "ПКО/Bank"},
            "after_id": None,
        },
        {
            "client_id": "pod",
            "kind": "pod",
            "action": "create",
            "patch": {
                "name": "Receive",
                "directions": ["receive"],
                "used_pko": [{"address": "ПКО/Bank"}],
            },
        },
    ]
    operations = tuple(parse_operation(p) for p in packets)
    plan = preview(model, operations, expected_revision=model.revision)
    assert not plan.failures, plan.failures
    changed = apply(
        model,
        operations,
        expected_revision=model.revision,
        expected_preview_hash=plan.preview_hash,
        confirmations=tuple((n.code, n.notice_hash) for n in plan.notices),
    )
    assert changed == plan.model
    assert changed.pko[0].properties[0].configuration_property == "Link"
    assert changed.pko[0].groups[0].properties[0].configuration_property == "Tail"
    round_trip(changed)


def test_address_targets_accept_algorithm_and_handler_binding():
    model = bank_model()
    for packet in [
        {
            "client_id": "algorithm",
            "kind": "algorithm",
            "action": "create",
            "patch": {
                "name": "Search",
                "parameters": "ДанныеИБ, ПолученныеДанные, КомпонентыОбмена",
                "body": "ДанныеИБ = Неопределено;",
            },
        },
        {
            "client_id": "search",
            "kind": "handler",
            "action": "create",
            "owner_id": {"address": "ПКО/Bank"},
            "patch": {"event": "АлгоритмПоиска", "target": {"address": "Код/Search"}},
        },
        {
            "client_id": "other",
            "kind": "pko",
            "action": "create",
            "patch": {"name": "Other", "directions": ["receive"]},
        },
        {
            "client_id": "shared",
            "kind": "handler",
            "action": "create",
            "owner_id": {"address": "ПКО/Other"},
            "patch": {
                "event": "АлгоритмПоиска",
                "target": {"address": "ПКО/Bank/Событие/АлгоритмПоиска"},
            },
        },
    ]:
        model = execute(model, parse_operation(packet))
    assert model.pko[0].events[0].target.target_id == model.pko[1].events[0].target.target_id
    round_trip(model)


def test_ambiguous_rule_address_lists_direction_qualified_options():
    model = execute(
        bank_model(),
        ManagerOperation(
            "send-bank",
            "pko",
            "create",
            patch=PkoPatch(name="SendBank", directions=("send",)),
        ),
    )
    model = replace(model, pko=(model.pko[0], replace(model.pko[1], name="Bank"))).with_revision()
    packet = {
        "client_id": "handler",
        "kind": "handler",
        "action": "create",
        "owner_id": {"address": "ПКО/Bank"},
        "patch": {"event": "ПередЗаписьюПолученныхДанных", "body": "ДанныеИБ = Неопределено;"},
    }
    plan = preview(model, (parse_operation(packet),), expected_revision=model.revision)
    assert plan.failures[0].reason == "model_invalid"
    assert set(plan.failures[0].references) == {"ПКО/Bank~send", "ПКО/Bank~receive"}
    packet["owner_id"] = {"address": "ПКО/Bank~receive"}
    execute(model, parse_operation(packet))


@pytest.mark.parametrize("address", ["ПКО/Missing", "Код/Missing"])
def test_missing_address_never_skips_operation(address):
    model = bank_model()
    op = parse_operation(
        {
            "client_id": "missing",
            "kind": "property",
            "action": "create",
            "owner_id": {"address": address},
            "patch": {"configuration_property": "Code", "format_property": "Code"},
        }
    )
    plan = preview(model, (op,), expected_revision=model.revision)
    assert plan.failures and len(plan.operations) == 1
    assert plan.operations[0].result_id == ""
    assert "missing" in plan.failures[0].message


def test_recognized_extension_event_reports_writer_limitation():
    model = bank_model()
    op = parse_operation(
        {
            "client_id": "extension",
            "kind": "handler",
            "action": "create",
            "owner_id": model.pko[0].logical_id,
            "patch": {"event": "ПослеКонвертацииОбъекта", "body": "Возврат;"},
        }
    )
    plan = preview(model, (op,), expected_revision=model.revision)
    assert plan.failures[0].reason == "unsupported_form"
    assert "писателем не поддержано" in plan.failures[0].message
