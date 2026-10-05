"""Сервис полного менеджера: сквозной путь, долговечность и изоляция прежних контрактов."""

import ast
import inspect
import json
import os
import re
import shutil
import sqlite3
from dataclasses import fields
from pathlib import Path
from time import perf_counter
from types import SimpleNamespace
from typing import get_args

import anyio
import pytest
from lxml import etree
from mcp import Client

from kd2_rules_mcp.authoring.ed import manager_operations as writer_operations
from kd2_rules_mcp.authoring.ed.manager_render import ManagerRoute, render_manager_kit
from kd2_rules_mcp.ed import executor_profile
from kd2_rules_mcp.ed.writer import render
from kd2_rules_mcp.ed.writer_model import json_value, logical_id
from kd2_rules_mcp.errors import (
    EdAuthoringAckRequiredError,
    EdAuthoringIoError,
    EdAuthoringPathError,
    EdAuthoringPreconditionError,
    EdAuthoringStaleError,
    ProjectNotFoundError,
)
from kd2_rules_mcp.server import INSTRUCTIONS, create_server, error_payload
from kd2_rules_mcp.service import Kd2Service, Settings
from tests.test_ed_writer_candidates import structure
from tests.test_ed_writer_model import SYNTHETIC
from tests.test_ed_writer_profiles import verified_detection
from tests.test_service_ed_authoring import make_dump
from tests.test_service_ed_authoring import setup as setup

DATA = Path(__file__).parent / "data/ed/writer"
PLAN = "ПланФормата"


def size(value):
    return len(json.dumps(value, ensure_ascii=False, indent=2).encode("utf-8"))


def reopen_writer_schema(service, root):
    return service.ed_schema_open(
        "1.20",
        path=str(root / "Schemas/format.bin"),
        imports={"urn:test:writer-message": str(DATA / "message.bin")},
    )


def string_value(value):
    return {"state": "string", "value": value}


def manager_operations(project_id="positions", *, split=False, ten=False):
    """Только JSON, который мог бы передать агент: никаких правок библиотечной модели."""
    rows = []
    ref = {"state": "reference", "reference_parts": ["Метаданные", "Справочники", "Должности"]}
    previous = None
    for direction in ("send", "receive") if split else ("both",):
        client = "pko-" + direction
        rule_id = logical_id(project_id, client)
        name = "Должности" + ("_" + direction if split else "")
        rows.append(
            {
                "client_id": client,
                "kind": "pko",
                "action": "create",
                "patch": {
                    "name": name,
                    "directions": ["send", "receive"] if direction == "both" else [direction],
                    "configuration_object": ref,
                    "format_object": string_value("Справочник.Должности"),
                },
            }
        )
        previous_property = None
        for prop in ("Наименование", "НаименованиеКраткое"):
            prop_client = client + "-" + prop
            rows.append(
                {
                    "client_id": prop_client,
                    "kind": "property",
                    "action": "create",
                    "owner_id": rule_id,
                    "container_id": rule_id,
                    "after_id": previous_property,
                    "patch": {"configuration_property": prop, "format_property": prop},
                }
            )
            previous_property = logical_id(project_id, prop_client)
        if direction != "send":
            rows.append(
                {
                    "client_id": "search",
                    "kind": "identification",
                    "action": "update",
                    "target_id": logical_id(project_id, client + "/identification"),
                    "patch": {
                        "mode": string_value(
                            "СначалаПоУникальномуИдентификаторуПотомПоПолямПоиска"
                        ),
                        "search_sets": [["Наименование"]],
                    },
                }
            )
        for pod_direction in (direction,) if split else ("send", "receive"):
            patch = {
                "name": "Должности_" + ("Отправка" if pod_direction == "send" else "Получение"),
                "directions": [pod_direction],
                "used_pko": [
                    {"kind": "pko", "target_id": rule_id, "name": name, "resolution": "resolved"}
                ],
            }
            if pod_direction == "send":
                patch.update(
                    configuration_selection=ref, clear_data={"state": "boolean", "value": False}
                )
            else:
                patch["format_selection"] = string_value("Справочник.Должности")
            rows.append(
                {
                    "client_id": "pod-" + pod_direction,
                    "kind": "pod",
                    "action": "create",
                    "patch": patch,
                }
            )
        previous = rule_id
    assert previous is not None
    if ten:
        for n in range(10 - len(rows)):
            rows.append(
                {
                    "client_id": "identity-" + str(n),
                    "kind": "manager",
                    "action": "update",
                    "patch": {
                        "manager_name": "МенеджерОбмена",
                        "title": string_value("PilotManager"),
                    },
                }
            )
    return rows


@pytest.fixture
def writer_setup(tmp_path, monkeypatch):
    root = tmp_path / "host"
    make_dump(root)
    plan = root / "ExchangePlans" / (PLAN + ".xml")
    tree = etree.parse(str(plan))
    tree.getroot().set("version", "2.20")
    tree.write(str(plan), encoding="utf-8")
    _, texts, profile = verified_detection()
    monkeypatch.setattr(executor_profile, "PROFILES", (profile,))
    for name, text in texts.items():
        path = root / "CommonModules" / name / "Ext/Module.bsl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    service = Kd2Service(Settings(cache_dir=tmp_path / "cache", workspace=tmp_path / "workspace"))
    conn = structure()
    conn.execute("UPDATE properties SET string_length=10 WHERE name='Наименование'")
    conn.executemany(
        "INSERT OR REPLACE INTO meta(key,value) VALUES (?,?)",
        [
            ("input_hash", "synthetic"),
            ("source", "xml"),
            ("source_path", str(root.resolve())),
            ("extensions", "[]"),
        ],
    )
    conn.commit()
    destination = sqlite3.connect(service.store.path("host"))
    conn.backup(destination)
    destination.close()
    conn.close()
    # Корпус кандидатов содержит обязательные поля вне двух ПКС этого сценария.
    # Здесь схема отдельная: дополнительные поля необязательны, исходник не меняется.
    schema_path = root / "Schemas/format.bin"
    schema_path.parent.mkdir()
    schema_tree = etree.parse(str(DATA / "format.bin"))
    for node in schema_tree.iter("{http://v8.1c.ru/8.1/xdto}property"):
        if node.get("name") not in ("Наименование", "НаименованиеКраткое"):
            node.set("lowerBound", "0")
    schema_tree.write(str(schema_path), encoding="utf-8")
    opened = service.ed_schema_open(
        "1.20",
        path=str(schema_path),
        imports={"urn:test:writer-message": str(DATA / "message.bin")},
    )
    args = {
        "project_id": "positions",
        "configuration_path": str(root),
        "plan": PLAN,
        "format_version": "1.20",
        "schema_id": opened["schema_id"],
        "structure_id": "host",
    }
    return service, args, root


def apply_packet(service, created, operations):
    args = {
        "project_id": created["project_id"],
        "expected_revision": created["revision"],
        "operations": operations,
    }
    preview = service.ed_apply(**args)
    assert not preview["failures"]["total"], preview
    assert preview["revision"] == created["revision"]
    result = service.ed_apply(**args, mode="apply", expected_preview_hash=preview["preview_hash"])
    assert result["revision"] == preview["future_revision"]
    return args, preview, result


def build(service, created, **kwargs):
    return service.ed_authoring_build(
        scope="manager",
        project_id=created["project_id"],
        expected_revision=created["revision"],
        **kwargs,
    )


def files_at(path):
    return {p.relative_to(path).as_posix(): p.read_bytes() for p in path.rglob("*") if p.is_file()}


@pytest.mark.parametrize(
    "missing_schema,missing_structure", [(True, False), (True, True), (False, True)]
)
def test_migration_reopen_missing_inputs_refuses_before_loading(
    writer_setup, monkeypatch, missing_schema, missing_structure
):
    service, args, root = writer_setup
    service.ed_create(**args)
    service = Kd2Service(service.settings)
    if not missing_schema:
        reopen_writer_schema(service, root)
    if missing_structure:
        monkeypatch.setattr(service.store, "exists", lambda _: False)

    def forbidden(*a, **kw):
        pytest.fail("Повторное создание не должно загружать маршруты, профиль или структуру")

    for name in ("_manager_reference", "_manager_detection", "_manager_structure_provenance"):
        monkeypatch.setattr(service, name, forbidden)
    monkeypatch.setattr(service.store, "load_xml", forbidden)
    start = perf_counter()
    with pytest.raises(EdAuthoringPreconditionError) as error:
        service.ed_create(**args)
    assert perf_counter() - start < 1
    assert error.value.details["failures"][0]["id"] == "manager_inputs_not_open"
    calls = error.value.details["reopen_calls"]
    if missing_schema:
        assert calls[0]["tool"] == "ed_schema_open"
        assert calls[0]["arguments"]["format_version"] == "1.20"
    if missing_structure:
        assert calls[-1] == {
            "tool": "structure_load_xml",
            "arguments": {
                "structure_id": "host",
                "path": args["configuration_path"],
                "extensions": [],
            },
        }


def test_migration_missing_project_inputs_return_catalog_reopen_calls(writer_setup, monkeypatch):
    service, args, _ = writer_setup
    service.ed_create(**args)
    folder = service.manager_workspace.directory / args["project_id"]
    metadata = json.loads((folder / "creation.json").read_bytes())
    metadata["arguments"].update(project="УчебныйПроект", configuration="full")
    (folder / "creation.json").write_text(json.dumps(metadata), encoding="utf-8")
    restarted = Kd2Service(service.settings)
    monkeypatch.setattr(restarted.store, "exists", lambda _: False)
    with pytest.raises(EdAuthoringPreconditionError) as caught:
        restarted.ed_create(**(args | {"project": "УчебныйПроект"}))
    assert caught.value.details["reopen_calls"] == [
        {
            "tool": "ed_schema_open",
            "arguments": {
                "format_version": "1.20",
                "project": "УчебныйПроект",
                "configuration": "full",
            },
        },
        {
            "tool": "structure_load_project",
            "arguments": {
                "project_id": "УчебныйПроект",
                "configuration_id": "full",
                "structure_id": "host",
            },
        },
    ]


@pytest.mark.parametrize("reference", ["auto", "explicit"])
def test_migration_property_reference_keeps_name_pair_and_rename(writer_setup, reference):
    service, args, root = writer_setup
    created = service.ed_create(**args)
    module = root / "CommonModules/Менеджер2/Ext/Module.bsl"
    module.write_text(
        (DATA / "pilot.bsl")
        .read_text(encoding="utf-8")
        .replace('"Наименование",        "Наименование"', '"Код", "Наименование"'),
        encoding="utf-8",
    )
    document_id = "auto" if reference == "auto" else service.ed_open(str(module))["project_id"]
    result = service.ed_authoring_candidates(
        scope="manager",
        kind="properties",
        reference_document_id=document_id,
        target={
            "project_id": created["project_id"],
            "schema_id": args["schema_id"],
            "structure_id": "host",
            "direction": "send",
            "configuration_object": "Справочник.Должности",
            "format_type": "Справочник.Должности",
        },
    )
    rows = [r for r in result["properties"]["items"] if r["configuration"] == "Код"]
    assert {r["format_name"] for r in rows} == {"Код", "Наименование"}
    typical = next(r for r in rows if r["class"] == "reference_module")
    assert typical["property_kind"] == "direct" and typical["rule_name"] is None
    assert typical["reason"] == "так в типовом модуле" and not typical["auto"]
    assert typical["origin"]["document_id"] != "auto"
    assert set(result) >= {
        "properties",
        "table_parts",
        "unmatched_format",
        "unmatched_configuration",
    }


def test_long_packet_pages_explain_size_limit_and_keep_every_operation(writer_setup):
    service, args, _ = writer_setup
    created = service.ed_create(**args)
    packet = [
        {
            "client_id": f"long-{n}",
            "kind": "manager",
            "action": "update",
            "patch": {"title": string_value(str(n) + "x" * 1600)},
        }
        for n in range(5)
    ]
    rows, offset = [], 0
    while True:
        page = service.ed_apply(
            created["project_id"],
            created["revision"],
            packet,
            section="operations",
            offset=offset,
            limit=50,
        )
        rows.extend(page["items"])
        assert page["next_offset"] == offset + len(page["items"])
        if not page["has_more"]:
            break
        assert page["truncated_by"] == "size"
        assert page["next_offset"] > offset
        offset = page["next_offset"]
    assert len(rows) == page["total"] == 5


@pytest.mark.parametrize("length", [5000, 0])
def test_send_string_overflow_r4_needs_build_ack_after_restart(writer_setup, length):
    service, args, _ = writer_setup
    with sqlite3.connect(service.store.path("host")) as connection:
        connection.execute(
            "UPDATE properties SET string_length=? WHERE name='Наименование'", (length,)
        )
        connection.execute("UPDATE meta SET value='u8-r4' WHERE key='input_hash'")
    created = service.ed_create(**args)
    _, _, applied = apply_packet(service, created, manager_operations(split=True))
    report = service.ed_validate(
        applied["document_id"], schema_id=args["schema_id"], structure_id="host"
    )
    issue = next(i for i in report["issues"]["items"] if "(send)" in i["message"])
    assert issue["level"] == "предупреждение"
    assert issue["address"] in issue["message"]
    assert "не выгрузится" in issue["message"] and "XDTO:6103–6109" in issue["message"]
    service = Kd2Service(service.settings)
    preview = build(service, applied)
    assert preview["validation"]["warnings"] == 1
    assert any(n.startswith("ed.schema.value_range:") for n in preview["required_acknowledgements"])
    with pytest.raises(EdAuthoringAckRequiredError):
        build(service, applied, mode="write", expected_preview_hash=preview["build_hash"])
    written = build(
        service,
        applied,
        mode="write",
        expected_preview_hash=preview["build_hash"],
        acknowledged_notices=preview["required_acknowledgements"],
    )
    assert written["written"]


def test_auto_r4_extension_names_request_project_instead_of_read_error(writer_setup):
    service, args, _ = writer_setup
    with sqlite3.connect(service.store.path("host")) as connection:
        connection.execute("UPDATE meta SET value='[\"ExampleExtension\"]' WHERE key='extensions'")
    with pytest.raises(EdAuthoringPreconditionError) as error:
        service.ed_authoring_candidates(
            scope="manager",
            kind="objects",
            reference_document_id="auto",
            target={"schema_id": args["schema_id"], "structure_id": "host", "direction": "send"},
        )
    assert error.value.details["failures"][0]["id"] == "structure_extension_paths_unavailable"
    assert "project_id" in str(error.value) and "явный документ" in str(error.value)


def test_auto_r4_recovers_extension_paths_from_source(writer_setup, tmp_path):
    service, args, _ = writer_setup
    extension = tmp_path / "extension"
    extension.mkdir()
    (extension / "Configuration.xml").write_text(
        '<MetaDataObject xmlns="http://v8.1c.ru/8.3/MDClasses" version="2.20">'
        '<Configuration uuid="c7c3b722-aedf-4ebc-aed5-6a905455f6fe">'
        "<Properties><Name>ExampleExtension</Name>"
        "<ConfigurationExtensionPurpose>Customization</ConfigurationExtensionPurpose>"
        "</Properties></Configuration></MetaDataObject>",
        encoding="utf-8",
    )
    with sqlite3.connect(service.store.path("host")) as connection:
        connection.execute("UPDATE meta SET value='[\"ExampleExtension\"]' WHERE key='extensions'")
        connection.execute(
            "INSERT INTO meta(key,value) VALUES ('extension_paths',?)",
            (json.dumps([str(extension)]),),
        )
    page = service.ed_authoring_candidates(
        scope="manager",
        kind="objects",
        reference_document_id="auto",
        target={"schema_id": args["schema_id"], "structure_id": "host", "direction": "send"},
    )
    assert "items" in page


def test_offset_r4_does_not_hide_sibling_failures(writer_setup):
    service, args, _ = writer_setup
    created = service.ed_create(**args)
    good = [
        {
            "client_id": f"t{n}",
            "kind": "manager",
            "action": "update",
            "patch": {"title": string_value("T" * 300)},
        }
        for n in range(8)
    ]
    bad = [
        {"client_id": f"bad{n}", "kind": "table_part", "action": "create", "patch": {}}
        for n in range(3)
    ]
    page = service.ed_apply(
        "positions", created["revision"], good + bad, section="operations", offset=5, limit=3
    )
    assert page["offset"] == 5
    assert page["failures"]["offset"] == 0
    assert len(page["failures"]["items"]) == page["failures"]["total"] == 3


def test_create_offset_r4_keeps_profile_notice_visible(writer_setup):
    service, args, root = writer_setup
    for path in root.glob("CommonModules/ОбменДанными*/Ext/Module.bsl"):
        path.write_bytes(path.read_bytes() + b"// changed\n")
    page = service.ed_create(**args, offset=1, limit=1)
    assert page["import_report"]["offset"] == 1
    assert page["notices_offset"] == 0
    assert page["notices"][0]["id"] == "executor_profile_unverified"


def test_create_offset_r4_only_pages_selected_input_differences(writer_setup, monkeypatch):
    service, args, _ = writer_setup
    service.ed_create(**args)
    differences = [{"field": f"input-{n}", "existing": "old", "current": "new"} for n in range(3)]
    monkeypatch.setattr(service, "_manager_input_differences", lambda *a: ({}, differences))
    page = service.ed_create(**args, offset=2, limit=2)
    notice = page["notices"][0]
    assert notice["differences"] == differences[:2] and notice["has_more"]
    following = service.ed_create(
        **args, section="differences", offset=notice["next_offset"], limit=2
    )
    assert following["notices"][0]["differences"] == differences[2:]
    assert not following["notices"][0]["has_more"]
    assert following["import_report"]["offset"] == following["notices_offset"] == 0


def test_preview_lock_r4_reports_busy_not_stale(writer_setup):
    service, args, _ = writer_setup
    created = service.ed_create(**args)
    lock = service.manager_workspace.directory / "positions" / ".lock"
    lock.write_text(json.dumps({"pid": os.getpid(), "created": 9e12}), encoding="utf-8")
    try:
        with pytest.raises(EdAuthoringPreconditionError) as error:
            service.ed_apply("positions", created["revision"], manager_operations())
        assert error.value.details["failures"][0]["id"] == "project_busy"
        assert "повторите" in str(error.value)
    finally:
        lock.unlink()
    assert service.ed_apply("positions", created["revision"], manager_operations())["preview_hash"]


def test_reference_manager_null_r4_has_version_reason(writer_setup):
    service, args, _ = writer_setup
    page = service.ed_create(**{**args, "schema_id": None, "format_version": "9.99"})
    assert page["reference_manager"] is None
    assert page["reference_manager_reason"]["id"] == "format_version_not_mapped"
    assert "9.99" in page["reference_manager_reason"]["message"]


def test_apply_replay_and_build_pages_advance_through_all_rows(writer_setup):
    service, args, _ = writer_setup
    created = service.ed_create(**args)
    packet = [
        {
            "client_id": str(n) + "x" * 180,
            "kind": "manager",
            "action": "update",
            "patch": {"title": string_value(str(n))},
        }
        for n in range(50)
    ]
    preview = service.ed_apply(args["project_id"], created["revision"], packet)
    call = {
        "project_id": args["project_id"],
        "expected_revision": created["revision"],
        "mode": "apply",
        "expected_preview_hash": preview["preview_hash"],
    }
    applied = service.ed_apply(**call)
    assert applied["has_more"]
    rows = list(applied["items"])
    offset = applied["next_offset"]
    while True:
        page = service.ed_apply(**call, offset=offset)
        assert page["offset"] == offset and page["next_offset"] == offset + len(page["items"])
        rows.extend(page["items"])
        if not page["has_more"]:
            break
        assert page["next_offset"] > offset
        offset = page["next_offset"]
    assert len(rows) == page["total"]
    for section in ("summary", "operations", "notices", "files", "issues_after", "skipped"):
        offset, rows = 0, []
        while True:
            page = build(service, applied, section=section, offset=offset)
            rows.extend(page["items"])
            assert page["next_offset"] == offset + len(page["items"])
            if not page["has_more"]:
                break
            assert page["next_offset"] > offset
            offset = page["next_offset"]
        assert len(rows) == page["total"]


def test_rebind_notice_pages_keep_all_long_diagnostics(writer_setup, monkeypatch):
    from kd2_rules_mcp.validation.report import ValidationReport

    service, args, _ = writer_setup
    service.ed_create(**args)
    report = ValidationReport()
    for n in range(45):
        report.warning("ed.writer.test", f"ПКО/{n}", "diagnostic " * 40)
    monkeypatch.setattr(service, "_manager_rebind_report", lambda *a: report)
    rows, offset = [], 0
    while True:
        page = service.ed_create(
            **{**args, "mode": "rebind"}, section="notices", offset=offset, limit=50
        )
        rows.extend(page["notices"])
        assert page["notices_next_offset"] == offset + len(page["notices"])
        if not page["notices_has_more"]:
            break
        assert page["notices_next_offset"] > offset
        offset = page["notices_next_offset"]
    assert len(rows) == page["notice_count"] == 45


def test_apply_failure_subpages_can_be_continued(writer_setup):
    service, args, _ = writer_setup
    created = service.ed_create(**args)
    packet = [
        {"client_id": f"missing-{n}", "kind": "pko", "action": "delete", "address": f"ПКО/Нет{n}"}
        for n in range(10)
    ]
    first = service.ed_apply(args["project_id"], created["revision"], packet)
    assert first["failures"]["total"] == 10 and first["failures"]["has_more"]
    offset = first["failures"]["next_offset"]
    following = service.ed_apply(
        args["project_id"], created["revision"], packet, section="failures", offset=offset
    )
    assert following["failures"]["offset"] == offset
    assert first["failures"]["items"] != following["failures"]["items"]
    assert not following["failures"]["has_more"]


def test_apply_saved_preview_after_restart_and_replay(writer_setup):
    service, args, _ = writer_setup
    created = service.ed_create(**args)
    preview = service.ed_apply(created["project_id"], created["revision"], manager_operations())
    service = Kd2Service(service.settings)
    result = service.ed_apply(
        created["project_id"],
        created["revision"],
        mode="apply",
        expected_preview_hash=preview["preview_hash"],
    )
    assert result["applied"] and result["revision"] == preview["future_revision"]
    assert service.ed_overview(result["document_id"])["counts"]["pko"] == 1
    replay = service.ed_apply(
        created["project_id"],
        created["revision"],
        mode="apply",
        expected_preview_hash=preview["preview_hash"],
    )
    assert replay["replayed"] and replay["revision"] == result["revision"]


def test_manager_primitive_validation_and_ranges_preserve_reader_contract(writer_setup):
    service, args, root = writer_setup
    with sqlite3.connect(service.store.path("host")) as connection:
        connection.execute("UPDATE properties SET string_length=20 WHERE name='Наименование'")
    created = service.ed_create(**args)
    _, _, applied = apply_packet(service, created, manager_operations())
    inputs = {"schema_id": args["schema_id"], "structure_id": "host"}
    report = service.ed_validate(applied["document_id"], **inputs)
    ranges = [i for i in report["issues"]["items"] if i["check"] == "ed.schema.value_range"]
    assert len(ranges) == 1 and "20" in ranges[0]["message"] and "10" in ranges[0]["message"]
    assert ranges[0]["level"] == "предупреждение"
    assert report["summary"].get("info", 0) == 0 and report["summary"]["warnings"] == 1
    assert (
        service.ed_validate(applied["document_id"], **inputs, level="предупреждение")["issues"][
            "items"
        ]
        == ranges
    )
    assert not any(
        s["check"] == "ed.schema.type_incompatible" and "non_atomic_type" in s["reason"]
        for s in service.ed_validate(applied["document_id"], **inputs, section="skipped")[
            "skipped"
        ]["items"]
    )
    source = root / "generated.bsl"
    source.write_text(
        render(service.manager_workspace.get(args["project_id"]).model).text, encoding="utf-8"
    )
    regular = service.ed_open(str(source))
    ordinary_report = service.ed_validate(regular["project_id"], **inputs)
    assert not ordinary_report["issues"]["items"]


def test_manager_skipped_type_explains_conversion_action(writer_setup):
    service, args, root = writer_setup
    created = service.ed_create(**args)
    operations = manager_operations()
    operations[1]["patch"]["format_property"] = "Ссылка"
    _, _, applied = apply_packet(service, created, operations)
    inputs = {"schema_id": args["schema_id"], "structure_id": "host", "section": "skipped"}
    rows = service.ed_validate(applied["document_id"], **inputs)["skipped"]["items"]
    row = next(r for r in rows if r["reason"].startswith("non_atomic_type:"))
    assert "ПКО" in row["hint"] and "алгоритм" in row["hint"]
    assert ";" in row["reason"] and "алгоритм" not in row["reason"]
    source = root / "generated.bsl"
    source.write_text(
        render(service.manager_workspace.get(args["project_id"]).model).text, encoding="utf-8"
    )
    regular = service.ed_open(str(source))
    ordinary = service.ed_validate(regular["project_id"], **inputs)["skipped"]["items"]
    assert all("hint" not in r for r in ordinary)


def test_missing_or_evicted_saved_preview_requests_packet(writer_setup):
    service, args, _ = writer_setup
    created = service.ed_create(**args)
    previews = []
    for n in range(10):
        previews.append(
            service.ed_apply(
                created["project_id"],
                created["revision"],
                [
                    {
                        "client_id": str(n),
                        "kind": "manager",
                        "action": "update",
                        "patch": {"title": string_value(str(n))},
                    }
                ],
            )
        )
    for missing in ("0" * 64, previews[0]["preview_hash"]):
        with pytest.raises(EdAuthoringPreconditionError) as error:
            service.ed_apply(
                created["project_id"],
                created["revision"],
                mode="apply",
                expected_preview_hash=missing,
            )
        assert error.value.details["failures"][0]["id"] == "preview_packet_missing"
        assert "operations" in str(error.value)


def test_create_exposes_typical_manager_and_missing_plan_candidates(writer_setup):
    service, args, root = writer_setup
    created = service.ed_create(**args)
    assert created["reference_manager"] == {
        "name": "Менеджер2",
        "path": str(root / "CommonModules/Менеджер2/Ext/Module.bsl"),
    }
    with pytest.raises(EdAuthoringPreconditionError) as error:
        service.ed_create(**{**args, "project_id": "wrong-plan", "plan": "НетПлана"})
    assert error.value.details["plan_candidates"]["items"] == [{"plan": PLAN}]


def test_object_candidates_auto_and_empty_page_hint(writer_setup):
    service, args, root = writer_setup
    service.ed_create(**args)
    module = root / "CommonModules/Менеджер2/Ext/Module.bsl"
    module.write_text((DATA / "pilot.bsl").read_text(encoding="utf-8"), encoding="utf-8")
    # Пара из типового модуля имеет смысловое переименование, по имени её нет.
    with sqlite3.connect(service.store.path("host")) as connection:
        connection.execute(
            "UPDATE objects SET name='ШтатныеПозиции', synonym='' WHERE name='Должности'"
        )
        connection.commit()
    module.write_text(
        module.read_text(encoding="utf-8").replace(
            "Метаданные.Справочники.Должности", "Метаданные.Справочники.ШтатныеПозиции"
        ),
        encoding="utf-8",
    )
    target = {
        "project_id": args["project_id"],
        "schema_id": args["schema_id"],
        "structure_id": "host",
        "direction": "send",
    }
    empty = service.ed_authoring_candidates(
        scope="manager", target=target, kind="objects", text="Штатные"
    )
    assert empty["notices"][0]["hint"] == 'reference_document_id="auto"'
    auto = service.ed_authoring_candidates(
        scope="manager", target=target, kind="objects", text="Штатные", reference_document_id="auto"
    )
    assert len(auto["items"]) == 1
    assert auto["items"][0]["confidence"] == "reference"
    assert auto["items"][0]["origin"]["document_id"] != "auto"
    module.write_text(module.read_text(encoding="utf-8") + "\n// changed\n", encoding="utf-8")
    with pytest.raises(EdAuthoringStaleError) as error:
        service.ed_authoring_candidates(
            scope="manager",
            target=target,
            kind="objects",
            text="Штатные",
            reference_document_id="auto",
        )
    assert error.value.details["reference_document_id"] == auto["items"][0]["origin"]["document_id"]


def test_end_to_end_restart_navigation_delivery_and_close(writer_setup):
    service, args, root = writer_setup
    created = service.ed_create(**args)
    assert created["executor_profile"]["verified"]
    assert created["counts"]["pko"] == 0
    for direction in ("send", "receive"):
        target = {"schema_id": args["schema_id"], "structure_id": "host", "direction": direction}
        objects = service.ed_authoring_candidates(scope="manager", target=target, kind="objects")
        assert any(c["configuration"] == "Справочник.Должности" for c in objects["items"])
        props = service.ed_authoring_candidates(
            scope="manager",
            target={
                **target,
                "configuration_object": "Справочник.Должности",
                "format_type": "Справочник.Должности",
            },
            kind="properties",
        )
        assert {"Наименование", "НаименованиеКраткое"} <= {
            r["configuration"] for r in props["properties"]["items"]
        }
    snapshot_before = files_at(service.manager_workspace.directory)
    packet = manager_operations(split=True)
    preview = service.ed_apply(created["project_id"], created["revision"], packet)
    assert snapshot_before == {
        p: b
        for p, b in files_at(service.manager_workspace.directory).items()
        if "/previews/" not in p
    }
    assert service.ed_overview(created["document_id"])["counts"]["pko"] == 0
    service = Kd2Service(service.settings)
    reopen_writer_schema(service, root)
    reopened = service.ed_create(**args)
    assert reopened["existing"] and reopened["revision"] == created["revision"]
    applied = service.ed_apply(
        created["project_id"],
        created["revision"],
        packet,
        mode="apply",
        expected_preview_hash=preview["preview_hash"],
    )
    with pytest.raises(ProjectNotFoundError):
        service.ed_overview(created["document_id"])
    document_id = applied["document_id"]
    assert service.ed_overview(document_id)["counts"]["pko"] == 2
    rows = service.ed_list(document_id, "pko")
    assert len(rows["items"]) == 2
    rule = service.ed_get(document_id, rows["items"][0]["address"], include_text=True)
    assert "ДобавитьПКО" in rule["text"]["text"]
    assert service.ed_locate(document_id, rule["span"]["line_start"])["matches"]["total"]
    opened = service.ed_schema_open(
        "1.20",
        path=str(root / "Schemas/format.bin"),
        imports={"urn:test:writer-message": str(DATA / "message.bin")},
    )
    assert opened["schema_id"] == args["schema_id"]
    validated = service.ed_validate(document_id, schema_id=args["schema_id"], structure_id="host")
    assert validated["writer"]["executor_profile"]["verified"]
    assert validated["summary"]["errors"] == 0, validated
    assert validated["summary"]["warnings"] == 0, validated
    preview = build(service, applied)
    assert size(preview) <= 8192
    metadata = service._manager_metadata("positions")
    model = service.manager_workspace.get("positions").model
    host, _ = service._manager_host(metadata, ManagerRoute(PLAN, "1.20"))
    direct = render_manager_kit(
        model,
        render(model, "canonical"),
        host,
        ManagerRoute(PLAN, "1.20"),
        executor_profile_id=model.executor_profile.profile_id,
        creation_fingerprint=metadata["creation_fingerprint"],
    )
    written = build(service, applied, mode="write", expected_preview_hash=preview["build_hash"])
    assert written["written"] and size(written) <= 8192
    destination = Path(written["output_dir"])
    assert files_at(destination) == dict(direct.files)
    repeated = build(service, applied)
    assert repeated["status"] == "unchanged"
    assert service.ed_close("positions") == {"project_id": "positions", "closed": True}
    assert not (service.manager_workspace.directory / "positions").exists()
    assert files_at(destination) == dict(direct.files)
    with pytest.raises(ProjectNotFoundError):
        service.ed_overview(document_id)


def test_stale_revisions_hashes_and_durable_exact_replay(writer_setup):
    service, args, root = writer_setup
    created = service.ed_create(**args)
    packet_args, preview, applied = apply_packet(service, created, manager_operations())
    assert service.ed_apply(
        **packet_args, mode="apply", expected_preview_hash=preview["preview_hash"]
    ) == {**applied, "replayed": True, "applied": False, "reason": "already_applied"}
    service = Kd2Service(service.settings)
    assert service.ed_apply(
        **packet_args, mode="apply", expected_preview_hash=preview["preview_hash"]
    ) == {**applied, "replayed": True, "applied": False, "reason": "already_applied"}
    with pytest.raises(EdAuthoringStaleError) as error:
        service.ed_apply(**packet_args, mode="apply", expected_preview_hash="other")
    assert error.value.details["revision"] == applied["revision"]
    update = [
        {
            "client_id": "title",
            "kind": "manager",
            "action": "update",
            "patch": {"manager_name": "ИнойМенеджер"},
        }
    ]
    with pytest.raises(EdAuthoringStaleError) as error:
        service.ed_apply(
            "positions", applied["revision"], update, mode="apply", expected_preview_hash="other"
        )
    assert error.value.details["preview_hash"]
    reopen_writer_schema(service, root)
    assert service.ed_create(**args)["revision"] == applied["revision"]
    with pytest.raises(EdAuthoringPreconditionError) as error:
        service.ed_create(**{**args, "format_version": "1.21"})
    assert error.value.details["differences"][0]["field"] == "format_version"


def test_import_keeps_original_and_reports_states(writer_setup):
    service, args, root = writer_setup
    path = root / "CommonModules/Типовой/Ext/Module.bsl"
    path.parent.mkdir(parents=True)
    shutil.copyfile(DATA / "pilot.bsl", path)
    source = service.ed_open(str(path))
    before = service.ed_validate(source["project_id"])
    created = service.ed_create(**{**args, "mode": "import", "document_id": source["project_id"]})
    assert created["import_report"]["counts"]["pko"]["editable"] == 1
    assert created["counts"]["pks"] == 2
    model = service.manager_workspace.get("positions").model
    assert render(model, "preserve").data == path.read_bytes() == (DATA / "pilot.bsl").read_bytes()
    assert service.ed_validate(source["project_id"]) == before
    assert service.ed_list(created["document_id"], "pod")["total"] == 2
    rows = created["import_report"]["items"]
    offset = created["import_report"]["next_offset"]
    more = created["import_report"]["has_more"]
    while more:
        following = service.ed_create(
            **{**args, "mode": "import", "document_id": source["project_id"]},
            offset=offset,
            limit=2,
        )
        assert following["existing"] and size(following) <= 8192
        rows += following["import_report"]["items"]
        offset, more = (
            following["import_report"]["next_offset"],
            following["import_report"]["has_more"],
        )
    assert len(rows) == len(model.import_report.entries) + len(model.import_report.diagnostics)


def test_unsupported_form_and_invalid_packet_are_atomic(writer_setup):
    service, args, _ = writer_setup
    with pytest.raises(EdAuthoringPreconditionError, match="интерфейс"):
        service.ed_create(**args, interface_version=3)
    assert not service.manager_workspace.ids()
    created = service.ed_create(**args)
    bad = [{"client_id": "future", "kind": "routine", "action": "create", "patch": {}}]
    preview = service.ed_apply("positions", created["revision"], bad)
    assert preview["failures"]["total"]
    with pytest.raises(EdAuthoringPreconditionError) as error:
        service.ed_apply(
            "positions",
            created["revision"],
            bad,
            mode="apply",
            expected_preview_hash=preview["preview_hash"],
        )
    assert error.value.details["failures"][0]["id"] == "unsupported_form"
    assert service.ed_create(**args)["revision"] == created["revision"]


def test_unverified_profile_requires_hash_bound_build_ack(writer_setup):
    service, args, root = writer_setup
    for path in root.glob("CommonModules/ОбменДанными*/Ext/Module.bsl"):
        path.write_bytes(path.read_bytes() + b"// changed\n")
    created = service.ed_create(**args)
    assert not created["executor_profile"]["verified"]
    assert created["notices"][0]["id"] == "executor_profile_unverified"
    preview = build(service, created)
    assert preview["required_acknowledgements"] == ["executor_profile_unverified"]
    with pytest.raises(EdAuthoringAckRequiredError):
        build(service, created, mode="write", expected_preview_hash=preview["build_hash"])
    result = build(
        service,
        created,
        mode="write",
        expected_preview_hash=preview["build_hash"],
        acknowledged_notices=preview["required_acknowledgements"],
    )
    assert result["written"]
    assert service.ed_validate(created["document_id"], check_prefix="ed.writer.profile")["issues"][
        "total"
    ]


def test_writer_warnings_need_build_acknowledgements(writer_setup):
    service, args, _ = writer_setup
    created = service.ed_create(**args)
    _, _, applied = apply_packet(service, created, manager_operations()[:1])
    preview = build(service, applied)
    assert preview["validation"]["warnings"] == 2
    required = preview["required_acknowledgements"]
    assert len(required) == 2 and all(n.startswith("ed.writer.") for n in required)
    with pytest.raises(EdAuthoringAckRequiredError):
        build(service, applied, mode="write", expected_preview_hash=preview["build_hash"])
    written = build(
        service,
        applied,
        mode="write",
        expected_preview_hash=preview["build_hash"],
        acknowledged_notices=required,
    )
    assert written["written"] and not written["runtime_verified"]


def test_profile_is_rechecked_for_validate_build_and_publication(writer_setup, monkeypatch):
    service, args, root = writer_setup
    created = service.ed_create(**args)
    before = build(service, created)
    module = next(root.glob("CommonModules/ОбменДанными*/Ext/Module.bsl"))
    module.write_bytes(module.read_bytes() + b"// changed\n")
    assert not service.ed_validate(created["document_id"])["writer"]["executor_profile"]["verified"]
    with pytest.raises(EdAuthoringStaleError):
        build(service, created, mode="write", expected_preview_hash=before["build_hash"])
    current = build(service, created)
    original = service._write_artifact

    def race(*args, **kwargs):
        module.write_bytes(module.read_bytes() + b"// race\n")
        return original(*args, **kwargs)

    monkeypatch.setattr(service, "_write_artifact", race)
    with pytest.raises(EdAuthoringStaleError):
        build(
            service,
            created,
            mode="write",
            expected_preview_hash=current["build_hash"],
            acknowledged_notices=current["required_acknowledgements"],
        )
    assert not Path(current["output_dir"]).exists()


def test_build_sections_and_scope_arguments(writer_setup):
    service, args, _ = writer_setup
    created = service.ed_create(**args)
    _, _, applied = apply_packet(service, created, manager_operations())
    for section in ("summary", "files", "operations", "notices", "issues_after", "skipped"):
        response = build(service, applied, section=section, limit=1)
        assert response["section"] == section and size(response) <= 8192
        assert "Module.bsl" not in json.dumps(response.get("items", [])) or section in (
            "files",
            "summary",
        )
    with pytest.raises(ValueError, match="прежнего авторинга"):
        build(service, applied, operations=[])
    with pytest.raises(EdAuthoringStaleError):
        build(service, created)
    with pytest.raises(EdAuthoringPreconditionError):
        build(service, applied, route={"format_version": "1.21"})


def test_owned_kit_is_not_overwritten(writer_setup):
    service, args, _ = writer_setup
    created = service.ed_create(**args)
    preview = build(service, created)
    written = build(service, created, mode="write", expected_preview_hash=preview["build_hash"])
    instruction = Path(written["output_dir"]) / "instruction.md"
    instruction.write_bytes(b"foreign edit")
    with pytest.raises(EdAuthoringPreconditionError):
        build(service, created)
    assert instruction.read_bytes() == b"foreign edit"


def test_existing_overlay_calls_are_unchanged(setup):
    service, arguments, _ = setup
    target = {
        "project": arguments["project"],
        "configuration": arguments["configuration"],
        **arguments["operations"][0]["target"],
    }
    for kind in ("format", "configuration"):
        assert service.ed_authoring_candidates(target, kind) == service.ed_authoring_candidates(
            target, kind, scope="overlay"
        )
    assert service.ed_authoring_build(**arguments) == service.ed_authoring_build(
        **arguments, scope="overlay"
    )


def test_two_new_tools_over_transport(writer_setup):
    service, args, _ = writer_setup

    async def scenario():
        async with Client(create_server(service)) as client:
            tools = (await client.list_tools()).tools
            assert {"ed_create", "ed_apply"} <= {t.name for t in tools}
            result = await client.call_tool("ed_create", args)
            assert not result.is_error, result.content
            created = result.structured_content
            assert created is not None
            result = await client.call_tool(
                "ed_apply",
                {
                    "project_id": "positions",
                    "expected_revision": created["revision"],
                    "operations": manager_operations(),
                },
            )
            assert not result.is_error and result.structured_content is not None
            assert result.structured_content["preview_hash"]
            preview_hash = result.structured_content["preview_hash"]
            result = await client.call_tool(
                "ed_apply",
                {
                    "project_id": "positions",
                    "expected_revision": created["revision"],
                    "mode": "apply",
                    "expected_preview_hash": preview_hash,
                },
            )
            assert not result.is_error and result.structured_content is not None
            assert result.structured_content["applied"]

    anyio.run(scenario)


def test_reference_candidates_bind_real_inputs_and_reject_another_host(writer_setup, tmp_path):
    service, args, root = writer_setup
    service.ed_create(**args)
    path = root / "CommonModules/Типовой/Ext/Module.bsl"
    path.parent.mkdir(parents=True)
    path.write_text(
        (DATA / "pilot.bsl")
        .read_text("utf-8-sig")
        .replace("Справочник.Должности", "Справочник.Склады"),
        encoding="utf-8",
    )
    reference = service.ed_open(str(path))["project_id"]
    target = {
        "schema_id": args["schema_id"],
        "structure_id": "host",
        "direction": "send",
        "project_id": "positions",
    }
    ordinary = service.ed_authoring_candidates(scope="manager", kind="objects", target=target)
    assert not any(
        r["configuration"] == "Справочник.Должности" and r["format_type"] == "Справочник.Склады"
        for r in ordinary["items"]
    )

    def candidate():
        result = service.ed_authoring_candidates(
            scope="manager", kind="objects", target=target, reference_document_id=reference
        )
        rows = result["items"]
        offset = result["next_offset"]
        while result["has_more"]:
            result = service.ed_authoring_candidates(
                scope="manager",
                kind="objects",
                target=target,
                reference_document_id=reference,
                offset=offset,
            )
            rows += result["items"]
            offset = result["next_offset"]
        return next(r for r in rows if r["confidence"] == "reference")

    before = candidate()
    assert before["configuration"] == "Справочник.Должности"
    assert before["format_type"] == "Справочник.Склады"
    assert before["reason"] == "так в типовом модуле" and not before["auto"]
    connection = sqlite3.connect(service.store.path("host"))
    connection.executemany(
        "INSERT OR REPLACE INTO meta(key,value) VALUES (?,?)",
        [("source", "xml"), ("source_path", str(root))],
    )
    connection.execute(
        "UPDATE properties SET string_length=string_length+1 WHERE name=?", ("Наименование",)
    )
    connection.commit()
    connection.close()
    assert candidate()["candidate_id"] != before["candidate_id"]
    target.pop("project_id")
    assert candidate()["configuration"] == "Справочник.Должности"
    foreign = tmp_path / "foreign.bsl"
    foreign.write_bytes(path.read_bytes())
    opened = service.ed_open(str(foreign))["project_id"]
    with pytest.raises(EdAuthoringPreconditionError, match="той же конфигурации"):
        service.ed_authoring_candidates(
            scope="manager", kind="objects", target=target, reference_document_id=opened
        )


def test_hundred_operations_have_compact_summary_and_full_pages(writer_setup):
    service, args, _ = writer_setup
    created = service.ed_create(**args)
    operations = [
        {
            "client_id": "title-" + str(n),
            "kind": "manager",
            "action": "update",
            "patch": {"title": string_value("Title " + str(n))},
        }
        for n in range(100)
    ]
    preview = service.ed_apply("positions", created["revision"], operations)
    assert size(preview) <= 8192 and preview["operation_count"] == 100
    assert "Title 99" not in json.dumps(preview)
    page = service.ed_apply("positions", created["revision"], operations, section="operations")
    assert page["total"] == 100 and page["has_more"]
    assert page["items"][0]["operation"]["patch"]["title"]["value"] == "Title 0"
    applied = service.ed_apply(
        "positions",
        created["revision"],
        operations,
        mode="apply",
        expected_preview_hash=preview["preview_hash"],
    )
    assert size(applied) <= 8192 and applied["applied"]


def test_creation_from_another_service_is_recovered_without_overwriting_inputs(writer_setup):
    service, args, root = writer_setup
    another = Kd2Service(service.settings)
    created = service.ed_create(**args)
    reopen_writer_schema(another, root)
    assert another.ed_create(**args)["existing"]
    assert another.ed_create(**args)["revision"] == created["revision"]
    with pytest.raises(EdAuthoringPreconditionError) as error:
        another.ed_create(**{**args, "format_version": "1.21"})
    assert error.value.details["differences"]
    assert service.ed_create(**args)["revision"] == created["revision"]
    _, _, applied = apply_packet(service, created, manager_operations())
    with pytest.raises(EdAuthoringStaleError) as error:
        another.ed_apply("positions", created["revision"], [])
    assert error.value.details["revision"] == applied["revision"]
    assert another.ed_create(**args)["revision"] == applied["revision"]


def test_creation_resolves_project_list_configuration(setup):
    service, old_args, root = setup
    created = service.ed_create(
        "catalog",
        project=old_args["project"],
        configuration=old_args["configuration"],
        plan=PLAN,
        format_version="1.20",
    )
    assert created["counts"]["pko"] == 0
    assert service.manager_workspace.get("catalog").model.host.configuration == str(root.resolve())
    assert service.ed_create(
        "catalog",
        project=old_args["project"],
        configuration=old_args["configuration"],
        plan=PLAN,
        format_version="1.20",
    )["existing"]


def documented_writer_packets():
    """Метки связывают исполняемые примеры со справочником, без копий JSON в тесте."""
    doc = (Path(__file__).parents[1] / "docs/tools.md").read_text("utf-8")
    section = doc.split("## Writing a manager module", 1)[1]
    blocks = re.findall(r"<!-- ed-writer-example: ([\w-]+) -->\n```json\n(.*?)\n```", section, re.S)
    assert blocks and len(blocks) == section.count("<!-- ed-writer-example:")
    assert len({name for name, _ in blocks}) == len(blocks)
    return [(name, json.loads(text)) for name, text in blocks]


@pytest.mark.parametrize("name,packet", documented_writer_packets())
def test_documented_two_property_example_uses_service_ids(writer_setup, name, packet):
    """Каждый опубликованный пакет проходит сервисный preview и apply на тестовом проекте."""
    service, args, _ = writer_setup
    created = service.ed_create(**args)
    assert isinstance(packet, list) and packet
    preview = service.ed_apply("positions", created["revision"], packet, section="operations")
    assert preview["failures"]["total"] == 0, preview
    assert preview["operation_count"] == len(packet)
    assert preview["skipped"]["total"] == 0
    applied = service.ed_apply(
        "positions",
        created["revision"],
        mode="apply",
        expected_preview_hash=preview["preview_hash"],
        confirmations=preview["required_confirmations"],
    )
    assert applied["applied"] and applied["revision"] == preview["future_revision"]
    if name == "handler":
        model = service.manager_workspace.get("positions").model
        events = [e for r in model.pko for e in r.events]
        assert len(events) == 2
        assert events[0].target.target_id == events[1].target.target_id
    if name == "catalog":
        checked = service.ed_validate(
            applied["document_id"], schema_id=args["schema_id"], structure_id="host"
        )
        assert checked["summary"]["errors"] == checked["summary"]["warnings"] == 0, checked
    if name == "table_part":
        model = service.manager_workspace.get("positions").model
        rule = model.pko[0]
        assert rule.groups[0].properties[0].format_property == "Quantity"
        # Ограничения рядом с исполняемым примером: имя-идентификатор и повтор без регистра.
        for client, configuration, format_name in (
            ("invalid-name", "Строки 2", "OtherRows"),
            ("case-duplicate", "ДругиеСтроки", "rows"),
        ):
            operation = writer_operations.parse_operation(
                {
                    "client_id": client,
                    "kind": "table_part",
                    "action": "create",
                    "owner_id": rule.logical_id,
                    "patch": {
                        "configuration_property": configuration,
                        "format_property": format_name,
                    },
                }
            )
            plan = writer_operations.preview(model, (operation,), expected_revision=model.revision)
            assert any(f.reason == "model_invalid" for f in plan.failures)


def writer_doc_table(marker):
    text = (Path(__file__).parents[1] / "docs/tools.md").read_text("utf-8")
    table = text.split(f"<!-- ed-writer-{marker} -->", 1)[1].split(
        f"<!-- /ed-writer-{marker} -->", 1
    )[0]
    rows = [line.split("|")[1:-1] for line in table.splitlines() if line.startswith("|")]
    result = {}
    for row in rows[2:]:
        kind = row[0].strip().strip("`")
        assert kind not in result, kind
        result[kind] = row[1:]
    return result


def writer_action_is_rejected(kind, action):
    """Читает ограничения действий из диспетчера операций, а не из списка в тесте."""
    source = ast.parse(inspect.getsource(writer_operations))
    functions = {n.name: n for n in source.body if isinstance(n, ast.FunctionDef)}
    context = {
        "op": SimpleNamespace(kind=kind, action=action, target_id=None, owner_id=None, clear=()),
        "target_id": None,
        "_PATCHES": writer_operations._PATCHES,
    }

    def guard(node):
        # Только закрытые условия kind/action; условия по модели не определяют поддержку действия.
        if any(isinstance(n, (ast.Call, ast.Subscript)) for n in ast.walk(node)):
            return None
        if any(isinstance(n, ast.Name) and n.id not in context for n in ast.walk(node)):
            return None
        try:
            return bool(
                eval(
                    compile(ast.Expression(node), "<action-guard>", "eval"),
                    {"__builtins__": {}},
                    context,
                )
            )
        except AttributeError:
            return None

    def rejected(statements):
        for statement in statements:
            if isinstance(statement, ast.If):
                truth = guard(statement.test)
                if truth is not None and rejected(statement.body if truth else statement.orelse):
                    return True
            elif isinstance(statement, ast.Expr) and isinstance(statement.value, ast.Call):
                call = statement.value
                if (
                    isinstance(call.func, ast.Name)
                    and call.func.id == "_fail"
                    and call.args
                    and isinstance(call.args[0], ast.Constant)
                    and call.args[0].value == "unsupported_form"
                ):
                    return True
            elif isinstance(statement, ast.Return) and isinstance(statement.value, ast.Call):
                call = statement.value
                if (
                    isinstance(call.func, ast.Name)
                    and call.func.id in functions
                    and rejected(functions[call.func.id].body)
                ):
                    return True
        return False

    return rejected(functions["_apply_one"].body)


def test_documented_writer_kinds_and_actions_match_code():
    rows = writer_doc_table("kinds")
    kinds = set(get_args(writer_operations.OperationKind))
    actions = set(get_args(writer_operations.Action))
    assert set(rows) == kinds
    for kind, row in rows.items():
        documented = set(re.findall(r"`([^`]+)`", row[0]))
        assert documented <= actions, (kind, documented - actions)
        supported = {action for action in actions if not writer_action_is_rejected(kind, action)}
        assert documented == supported, (kind, documented, supported)


def test_documented_writer_patch_fields_match_dtos():
    rows = writer_doc_table("patches")
    assert set(rows) == set(writer_operations._PATCHES)
    for kind, dto in writer_operations._PATCHES.items():
        documented = set(re.findall(r"`([^`]+)`", rows[kind][0]))
        expected = {f.name for f in fields(dto)}
        assert expected <= documented, (kind, expected - documented)


def title_op(client, value):
    return {
        "client_id": client,
        "kind": "manager",
        "action": "update",
        "patch": {"title": string_value(value)},
    }


def write_kit(service, created):
    preview = build(service, created)
    return build(
        service,
        created,
        mode="write",
        expected_preview_hash=preview["build_hash"],
        acknowledged_notices=preview["required_acknowledgements"],
    )


def test_kit_owner_cannot_be_acknowledged_away(writer_setup):
    service, args, _ = writer_setup
    args = {**args, "identity": {"name": "кд3м_Менеджер"}}
    first = service.ed_create(**args)
    written = write_kit(service, first)
    destination = Path(written["output_dir"])
    before = files_at(destination)
    manifest = json.loads(before["manifest.json"])
    assert manifest["project_id"] == "positions"
    assert manifest["creation_fingerprint"]
    second = service.ed_create(**{**args, "project_id": "other"})
    for mode in ("preview", "write"):
        with pytest.raises(EdAuthoringPreconditionError) as caught:
            build(
                service,
                second,
                mode=mode,
                expected_preview_hash="anything",
                acknowledged_notices=["kit_owned_by_other_project"],
            )
        assert caught.value.details["failures"][0]["id"] == "kit_owned_by_other_project"
        assert all(word in str(caught.value) for word in ("positions", "other", "identity.name"))
    assert files_at(destination) == before


def test_recreated_project_with_changed_inputs_requires_kit_ack(writer_setup):
    service, args, root = writer_setup
    created = service.ed_create(**args)
    written = write_kit(service, created)
    original = files_at(Path(written["output_dir"]))
    service.ed_close("positions")
    path = root / "Configuration.xml"
    path.write_bytes(path.read_bytes() + b"\n<!-- changed creation input -->\n")
    recreated = service.ed_create(**args)
    preview = build(service, recreated)
    assert "kit_creation_inputs_changed" in preview["required_acknowledgements"]
    with pytest.raises(EdAuthoringAckRequiredError):
        build(service, recreated, mode="write", expected_preview_hash=preview["build_hash"])
    assert files_at(Path(written["output_dir"])) == original
    updated = write_kit(service, recreated)
    assert updated["written"]
    assert build(service, recreated)["status"] == "unchanged"


def test_own_kit_can_be_updated_after_model_edit(writer_setup):
    service, args, _ = writer_setup
    created = service.ed_create(**args)
    written = write_kit(service, created)
    destination = Path(written["output_dir"])
    before = files_at(destination)
    _, _, applied = apply_packet(service, created, [title_op("new-title", "Updated")])
    changed = write_kit(service, applied)
    assert changed["written"] and Path(changed["output_dir"]) == destination
    assert files_at(destination) != before
    assert build(service, applied)["status"] == "unchanged"


def test_foreign_dump_structure_is_rejected(writer_setup, tmp_path):
    service, args, _ = writer_setup
    root = tmp_path / "foreign-host"
    make_dump(root)
    structure = service.structure_load_xml("foreign", str(root))
    with pytest.raises(EdAuthoringPreconditionError) as caught:
        service.ed_create(**{**args, "structure_id": structure["structure_id"]})
    assert caught.value.details["failures"][0]["id"] == "ed.author.snapshot_mismatch"


@pytest.mark.parametrize("content", [b"{", b"{}"])
def test_unreadable_kit_manifest_refuses_with_failures(writer_setup, content):
    service, args, _ = writer_setup
    created = service.ed_create(**args)
    written = write_kit(service, created)
    manifest = Path(written["output_dir"]) / "manifest.json"
    manifest.write_bytes(content)
    with pytest.raises(EdAuthoringPreconditionError) as caught:
        build(service, created)
    assert caught.value.details["failures"]
    assert manifest.read_bytes() == content


def test_failed_apply_does_not_publish_receipt(writer_setup):
    service, args, _ = writer_setup
    created = service.ed_create(**args)
    packet = [title_op("title", "A")]
    preview = service.ed_apply("positions", created["revision"], packet)
    folder = service.manager_workspace.directory / "positions"
    lock = folder / ".lock"
    lock.write_text(json.dumps({"pid": os.getpid(), "created": 9e12}), encoding="utf-8")
    with pytest.raises(EdAuthoringStaleError):
        service.ed_apply(
            "positions",
            created["revision"],
            packet,
            mode="apply",
            expected_preview_hash=preview["preview_hash"],
        )
    assert not list((folder / "applied").glob("*.json"))
    lock.unlink()
    result = service.ed_apply(
        "positions",
        created["revision"],
        packet,
        mode="apply",
        expected_preview_hash=preview["preview_hash"],
    )
    assert result["revision"] == service.manager_workspace.get("positions").model.revision


def test_empty_packet_does_not_claim_a_revision(writer_setup):
    service, args, _ = writer_setup
    created = service.ed_create(**args)
    before = files_at(service.manager_workspace.directory)
    preview = service.ed_apply("positions", created["revision"], [])
    assert preview["future_revision"] == created["revision"]
    assert not preview["applied"] and preview["reason"] == "no_operations"
    result = service.ed_apply(
        "positions",
        created["revision"],
        [],
        mode="apply",
        expected_preview_hash=preview["preview_hash"],
    )
    assert not result["applied"] and result["revision"] == created["revision"]
    assert {
        p: b
        for p, b in files_at(service.manager_workspace.directory).items()
        if "/previews/" not in p
    } == before


def test_old_receipt_returns_current_revision_and_live_document(writer_setup):
    service, args, _ = writer_setup
    created = service.ed_create(**args)
    packet, preview, first = apply_packet(service, created, [title_op("first", "A")])
    _, _, latest = apply_packet(service, first, [title_op("second", "B")])
    service = Kd2Service(service.settings)
    result = service.ed_apply(**packet, mode="apply", expected_preview_hash=preview["preview_hash"])
    assert result["replayed"]
    assert not result["applied"] and result["reason"] == "already_applied"
    assert result["revision"] == latest["revision"]
    assert result["document_id"] == latest["document_id"]
    assert service.ed_overview(result["document_id"])["counts"]["pko"] == 0


def test_preview_omits_non_durable_document_after_restart(writer_setup):
    service, args, _ = writer_setup
    created = service.ed_create(**args)
    service = Kd2Service(service.settings)
    preview = service.ed_apply("positions", created["revision"], [title_op("first", "A")])
    assert "document_id" not in preview


@pytest.mark.parametrize("tool", ["ed_overview", "ed_list", "ed_get", "ed_locate", "ed_validate"])
def test_navigation_rejects_snapshot_changed_by_other_process(writer_setup, tool):
    service, args, root = writer_setup
    created = service.ed_create(**args)
    other = Kd2Service(service.settings)
    reopen_writer_schema(other, root)
    other.ed_create(**args)
    _, _, updated = apply_packet(other, created, manager_operations())
    extra = {
        "ed_list": {"kind": "pko"},
        "ed_get": {"address": "Конвертация"},
        "ed_locate": {"line": 1},
    }.get(tool, {})
    with pytest.raises(EdAuthoringStaleError) as caught:
        getattr(service, tool)(created["document_id"], **extra)
    assert caught.value.details["revision"] == updated["revision"]


@pytest.mark.parametrize("restart", [False, True])
def test_close_removes_damaged_project_but_keeps_kit(writer_setup, restart):
    service, args, _ = writer_setup
    created = service.ed_create(**args)
    written = write_kit(service, created)
    kit = files_at(Path(written["output_dir"]))
    folder = service.manager_workspace.directory / "positions"
    (folder / "manager.ed.json").write_bytes(b"{")
    if restart:
        service = Kd2Service(service.settings)
    assert service.ed_close("positions")["closed"]
    assert not folder.exists()
    assert files_at(Path(written["output_dir"])) == kit


def test_orphan_creation_is_closeable_and_conflict_has_differences(writer_setup):
    service, args, _ = writer_setup
    folder = service.manager_workspace.directory / "positions"
    folder.mkdir(parents=True)
    (folder / "creation.json").write_text(
        json.dumps({"arguments": {"format_version": "different"}}), encoding="utf-8"
    )
    with pytest.raises(EdAuthoringPreconditionError) as caught:
        service.ed_create(**args)
    assert caught.value.details["differences"]
    assert service.ed_close("positions")["closed"] and not folder.exists()


@pytest.mark.parametrize("content", [b"{", b'{"x":1}'])
def test_corrupt_receipt_has_meaningful_error(writer_setup, content):
    service, args, _ = writer_setup
    created = service.ed_create(**args)
    packet, preview, _ = apply_packet(service, created, [title_op("first", "A")])
    receipt = next((service.manager_workspace.directory / "positions/applied").glob("*.json"))
    receipt.write_bytes(content)
    with pytest.raises(EdAuthoringPreconditionError) as caught:
        service.ed_apply(**packet, mode="apply", expected_preview_hash=preview["preview_hash"])
    payload = error_payload(caught.value)
    assert payload["code"] == "ed_authoring_precondition"
    assert payload["failures"][0]["id"] == "receipt_corrupt"


def test_route_refusal_uses_authoring_failure_envelope(writer_setup):
    service, args, _ = writer_setup
    with pytest.raises(EdAuthoringPreconditionError) as caught:
        service.ed_create(**{**args, "format_version": "1 20"})
    assert error_payload(caught.value)["failures"][0]["id"] == "ed.author.route_scope_conflict"


def test_import_reports_unchecked_extensions(writer_setup):
    service, args, root = writer_setup
    path = root / "CommonModules/Типовой/Ext/Module.bsl"
    path.parent.mkdir(parents=True)
    path.write_bytes((DATA / "pilot.bsl").read_bytes())
    source = service.ed_open(str(path))
    created = service.ed_create(**{**args, "mode": "import", "document_id": source["project_id"]})
    assert any(n["id"] == "extensions_unverified" for n in created["notices"])
    checked = service.ed_create(
        **{
            **args,
            "project_id": "checked",
            "mode": "import",
            "document_id": source["project_id"],
            "extensions": [],
        }
    )
    assert not any(n["id"] == "extensions_unverified" for n in checked["notices"])


def test_properties_reject_unknown_project(writer_setup):
    service, args, _ = writer_setup
    target = {
        "schema_id": args["schema_id"],
        "structure_id": "host",
        "direction": "send",
        "configuration_object": "Справочник.Должности",
        "format_type": "Справочник.Должности",
        "project_id": "unknown",
    }
    with pytest.raises(ProjectNotFoundError):
        service.ed_authoring_candidates(scope="manager", kind="properties", target=target)


def test_reference_and_plain_object_page_have_same_shape(writer_setup):
    service, args, root = writer_setup
    service.ed_create(**args)
    path = root / "CommonModules/Типовой/Ext/Module.bsl"
    path.parent.mkdir(parents=True)
    path.write_bytes((DATA / "pilot.bsl").read_bytes())
    reference = service.ed_open(str(path))["project_id"]
    target = {
        "schema_id": args["schema_id"],
        "structure_id": "host",
        "direction": "send",
        "project_id": "positions",
    }
    plain = service.ed_authoring_candidates(scope="manager", kind="objects", target=target, limit=1)
    with_reference = service.ed_authoring_candidates(
        scope="manager", kind="objects", target=target, reference_document_id=reference, limit=1
    )
    assert plain.keys() == with_reference.keys()
    assert plain["next_offset"] == plain["offset"] + len(plain["items"])


@pytest.mark.parametrize(
    "field,value", [("source_path", "foreign"), ("extensions", '["ForeignExtension"]')]
)
@pytest.mark.parametrize("kind", ["create", "objects", "properties", "unbound"])
def test_structure_provenance_is_checked(writer_setup, field, value, kind):
    service, args, _ = writer_setup
    if kind != "create":
        service.ed_create(**{**args, "structure_id": None if kind == "unbound" else "host"})
    with sqlite3.connect(service.store.path("host")) as connection:
        connection.execute("INSERT OR REPLACE INTO meta(key,value) VALUES (?,?)", (field, value))
    with pytest.raises(EdAuthoringPreconditionError) as caught:
        if kind == "create":
            service.ed_create(**args)
        else:
            target = {
                "schema_id": args["schema_id"],
                "structure_id": "host",
                "direction": "send",
                "project_id": "positions",
            }
            if kind == "properties":
                target.update(
                    configuration_object="Справочник.Должности", format_type="Справочник.Должности"
                )
            service.ed_authoring_candidates(
                scope="manager",
                kind="properties" if kind == "properties" else "objects",
                target=target,
            )
    assert caught.value.details["failures"][0]["id"] == "ed.author.snapshot_mismatch"


def test_existing_creation_reports_changes_and_rebind_preserves_rules_and_receipts(writer_setup):
    service, args, root = writer_setup
    created = service.ed_create(**args)
    packet, preview, applied = apply_packet(service, created, manager_operations())
    original = service.manager_workspace.get("positions").model
    receipt_files = files_at(service.manager_workspace.directory / "positions/applied")
    with sqlite3.connect(service.store.path("host")) as connection:
        connection.execute("UPDATE meta SET value='new-structure' WHERE key='input_hash'")
    schema_path = root / "Schemas/format.bin"
    schema_path.write_bytes(schema_path.read_bytes() + b"\n<!-- changed -->\n")
    config_path = root / "Configuration.xml"
    config_path.write_bytes(config_path.read_bytes() + b"\n<!-- changed -->\n")
    existing = service.ed_create(**args)
    assert existing["existing"] and existing["revision"] == applied["revision"]
    fields = {d["field"] for n in existing["notices"] for d in n.get("differences", [])}
    assert {"structure", "schema", "configuration"} <= fields
    with pytest.raises(EdAuthoringStaleError):
        service.ed_apply("positions", applied["revision"], [])
    # Новая схема читается явно, чтобы rebind принимал именно свежий снимок.
    service.ed_schema_close(args["schema_id"])
    opened = service.ed_schema_open(
        "1.20",
        path=str(schema_path),
        imports={"urn:test:writer-message": str(DATA / "message.bin")},
    )
    rebound = service.ed_create(**{**args, "mode": "rebind", "schema_id": opened["schema_id"]})
    current = service.manager_workspace.get("positions").model
    assert rebound["existing"] and rebound["rebound"]
    assert current.revision == rebound["revision"] != original.revision
    assert current.pko == original.pko and current.pod == original.pod
    assert current.decisions[: len(original.decisions)] == original.decisions
    assert files_at(service.manager_workspace.directory / "positions/applied") == receipt_files
    assert "validation" in rebound and rebound["executor_profile"]["verified"]
    restarted = Kd2Service(service.settings)
    reopen_writer_schema(restarted, root)
    assert (
        restarted.ed_create(**{**args, "schema_id": opened["schema_id"]})["revision"]
        == current.revision
    )
    replay = restarted.ed_apply(
        **packet, mode="apply", expected_preview_hash=preview["preview_hash"]
    )
    assert replay["replayed"] and replay["revision"] == current.revision


def test_confirmed_operation_repeat_keeps_saved_revision(writer_setup):
    service, args, root = writer_setup
    path = root / "CommonModules/Синтетика/Ext/Module.bsl"
    path.parent.mkdir(parents=True)
    path.write_text(
        SYNTHETIC + "\n#Область Алгоритмы\nПроцедура Business(КомпонентыОбмена)\n"
        "ОбменДаннымиXDTOСервер.ПКОПоИмени(КомпонентыОбмена, RuleName);\n"
        "КонецПроцедуры\n#КонецОбласти\n",
        encoding="utf-8",
    )
    source = service.ed_open(str(path))
    created = service.ed_create(
        **{
            **args,
            "mode": "import",
            "document_id": source["project_id"],
            "schema_id": None,
            "structure_id": None,
        }
    )
    model = service.manager_workspace.get("positions").model
    rename = {
        "client_id": "rename",
        "kind": "pko",
        "action": "update",
        "target_id": model.pko[0].logical_id,
        "patch": {"name": "Renamed"},
    }
    preview = service.ed_apply("positions", created["revision"], [rename])
    confirmations = preview["required_confirmations"]
    assert confirmations
    applied = service.ed_apply(
        "positions",
        created["revision"],
        [rename],
        mode="apply",
        expected_preview_hash=preview["preview_hash"],
        confirmations=confirmations,
    )
    repeat_preview = service.ed_apply("positions", applied["revision"], [rename])
    assert repeat_preview["future_revision"] == applied["revision"]
    assert repeat_preview["reason"] == "already_applied"
    again = service.ed_apply(
        "positions",
        applied["revision"],
        [rename],
        mode="apply",
        expected_preview_hash=repeat_preview["preview_hash"],
    )
    current = service.manager_workspace.get("positions").model
    assert not again["applied"] and again["revision"] == current.revision == applied["revision"]
    assert current.confirmations == tuple((c["code"], c["notice_hash"]) for c in confirmations)
    assert service.ed_overview(again["document_id"])


def test_rebind_reports_new_structure_validation_and_redetects_profile(writer_setup):
    service, args, root = writer_setup
    created = service.ed_create(**args)
    _, _, applied = apply_packet(service, created, manager_operations())
    with sqlite3.connect(service.store.path("host")) as connection:
        connection.execute("DELETE FROM properties WHERE name=?", ("НаименованиеКраткое",))
        connection.execute("UPDATE meta SET value='new' WHERE key='input_hash'")
    module = next(root.glob("CommonModules/ОбменДанными*/Ext/Module.bsl"))
    module.write_bytes(module.read_bytes() + b"// changed executor\n")
    rebound = service.ed_create("positions", mode="rebind")
    assert rebound["revision"] != applied["revision"]
    assert not rebound["executor_profile"]["verified"]
    assert rebound["validation"]["warnings"] > 0
    assert any(n["id"].startswith("ed.structure.") for n in rebound["notices"])
    assert (
        service.ed_validate(
            rebound["document_id"], schema_id=args["schema_id"], structure_id="host"
        )["summary"]["warnings"]
        > 0
    )


def test_server_schema_explains_rule_addresses_and_writer_workflow(writer_setup):
    service, _, _ = writer_setup

    async def tools():
        async with Client(create_server(service)) as client:
            return (await client.list_tools()).tools

    schemas = {t.name: t.input_schema for t in anyio.run(tools)}
    assert all(name in INSTRUCTIONS for name in ("ed_create", "ed_apply"))
    assert "Snapshots stay in memory" not in INSTRUCTIONS
    kind = schemas["rules_get"]["properties"]["kind"]["description"]
    assert all(k in kind for k in ("pko", "pks", "pvd", "pod", "algorithm", "pkz", "pro"))
    key = schemas["rules_get"]["properties"]["key"]["description"]
    assert "/" in key and "#" in key


def test_manager_id_may_resemble_unopened_reader_snapshot(writer_setup):
    service, args, _ = writer_setup
    created = service.ed_create(**{**args, "project_id": "ed-unopened-012345abcdef"})
    assert created["revision"]


@pytest.mark.parametrize(
    "field", ["host", "format_bindings", "executor_profile", "future_binding", "interface_version"]
)
@pytest.mark.parametrize("mode", ["preview", "apply"])
def test_apply_rejects_project_bindings_r2(writer_setup, field, mode):
    service, args, _ = writer_setup
    created = service.ed_create(**args)
    model = service.manager_workspace.get("positions").model
    values = {
        "host": {
            **json_value(model.host),
            "configuration": "C:/elsewhere",
            "structure_id": "",
            "structure_hash": "",
        },
        "format_bindings": [
            *json_value(model.format_bindings),
            {**json_value(model.format_bindings[0]), "key": "1.21"},
        ],
        "executor_profile": {"profile_id": "unknown-profile", "receive_mode": "ordinary"},
        "future_binding": {},
        "interface_version": 3,
    }
    operation = {**title_op("binding", "Never applied"), "patch": {field: values[field]}}
    with pytest.raises(EdAuthoringPreconditionError) as caught:
        service.ed_apply(
            "positions",
            created["revision"],
            [title_op("first", "Atomic"), operation],
            mode=mode,
            expected_preview_hash="unused",
        )
    assert caught.value.details["failures"][0]["id"] == "manager_binding_requires_rebind"
    assert "rebind" in str(caught.value)
    assert service.manager_workspace.get("positions").model == model
    assert not (service.manager_workspace.directory / "positions/applied").exists()
    if field == "host":
        with sqlite3.connect(service.store.path("host")) as connection:
            connection.execute("UPDATE meta SET value='changed' WHERE key='input_hash'")
        with pytest.raises(EdAuthoringStaleError):
            service.ed_apply("positions", created["revision"], [])
    if field == "format_bindings":
        with pytest.raises(EdAuthoringPreconditionError):
            build(service, created, route={"format_version": "1.21"})


@pytest.mark.parametrize("fault", ["after_journal", "after_model", "before_final", "after_final"])
@pytest.mark.parametrize("apply_first", [False, True])
@pytest.mark.parametrize("restart", [False, True])
def test_rebind_recovers_each_write_r2(
    writer_setup, tmp_path, monkeypatch, fault, apply_first, restart
):
    import kd2_rules_mcp.authoring.ed.workspace as workspace_module
    import kd2_rules_mcp.service.ed_writer as service_module

    service, args, root = writer_setup
    created = service.ed_create(**args)
    _, _, created = apply_packet(
        service, created, [*manager_operations(), title_op("before", "Keep")]
    )
    old_model = service.manager_workspace.get("positions").model
    folder = service.manager_workspace.directory / "positions"
    receipts = files_at(folder / "applied")
    other = tmp_path / "other-host"
    shutil.copytree(root, other)
    with (
        sqlite3.connect(service.store.path("host")) as source,
        sqlite3.connect(service.store.path("other")) as target,
    ):
        source.backup(target)
        target.execute("UPDATE meta SET value=? WHERE key='source_path'", (str(other.resolve()),))
    original_json, original_write = service_module._atomic_json, workspace_module._atomic_write
    writes = []

    def flaky_json(path, value, **kwargs):
        final = path.name == "creation.json" and "pending_rebind" not in value
        if final and fault == "before_final":
            raise OSError("Injected before final metadata")
        original_json(path, value, **kwargs)
        writes.append(path.name)
        if (not final and fault == "after_journal") or (final and fault == "after_final"):
            raise OSError("Injected after metadata write")

    def flaky_write(path, content):
        original_write(path, content)
        writes.append(path.name)
        if path.name == "manager.ed.json" and fault == "after_model":
            raise OSError("Injected after model write")

    with monkeypatch.context() as patch:
        patch.setattr(service_module, "_atomic_json", flaky_json)
        patch.setattr(workspace_module, "_atomic_write", flaky_write)
        with pytest.raises(EdAuthoringIoError) as caught:
            service.ed_create(
                "positions", mode="rebind", configuration_path=str(other), structure_id="other"
            )
        assert error_payload(caught.value, service)["code"] == "ed_authoring_io"
    assert writes
    restarted = Kd2Service(service.settings) if restart else service
    saved = restarted._manager_project("positions").model
    if apply_first:
        _, _, applied = apply_packet(
            restarted,
            {"project_id": "positions", "revision": saved.revision},
            [title_op("after", "After recovery")],
        )
        saved = restarted.manager_workspace.get("positions").model
        assert applied["revision"] == saved.revision
    target_args = (
        args
        if fault == "after_journal"
        else {**args, "configuration_path": str(other), "structure_id": "other"}
    )
    if restart:
        reopen_writer_schema(restarted, root)
    reopened = restarted.ed_create(**target_args)
    metadata = restarted._manager_metadata("positions")
    assert "pending_rebind" not in json.loads((folder / "creation.json").read_bytes())
    assert saved.host.configuration == metadata["arguments"]["configuration_path"]
    assert saved.host.structure_id == metadata["arguments"]["structure_id"]
    assert saved.pko == old_model.pko and saved.pod == old_model.pod
    assert saved.decisions[: len(old_model.decisions)] == old_model.decisions
    assert files_at(folder / "applied").items() >= receipts.items()
    if fault != "after_final":
        assert {
            "id": "rebind_recovered",
            "outcome": "rolled_back" if fault == "after_journal" else "committed",
        } in reopened["notices"]
    assert build(restarted, reopened)["status"] == "ready"
    assert restarted.ed_create(**target_args)["revision"] == saved.revision


def test_successful_rebind_has_only_current_notices_r2(writer_setup):
    service, args, _ = writer_setup
    service.ed_create(**args)
    with sqlite3.connect(service.store.path("host")) as connection:
        connection.execute("UPDATE meta SET value='fresh' WHERE key='input_hash'")
    rebound = service.ed_create("positions", mode="rebind")
    assert rebound["rebound"]
    assert not any(n["id"] == "creation_inputs_changed" for n in rebound["notices"])


def test_apply_cannot_clear_project_bindings_r2(writer_setup):
    service, args, _ = writer_setup
    created = service.ed_create(**args)
    operation = {**title_op("clear-bindings", "Never applied"), "clear": ["host"]}
    with pytest.raises(EdAuthoringPreconditionError) as caught:
        service.ed_apply("positions", created["revision"], [operation])
    assert caught.value.details["failures"][0]["id"] == "manager_binding_requires_rebind"
    assert service.manager_workspace.get("positions").model.revision == created["revision"]


def test_generated_reader_cannot_take_manager_id_r2(writer_setup):
    service, args, _ = writer_setup
    first = service.ed_create(**args)
    reserved_by_project = first["document_id"]
    service.ed_close(reserved_by_project)
    service.ed_create(**{**args, "project_id": reserved_by_project})
    with pytest.raises(EdAuthoringPreconditionError) as caught:
        service.ed_create(**args)
    assert caught.value.details["failures"][0]["id"] == "document_id_owned_by_manager"
    assert service.ed_close(reserved_by_project)["closed"]
    assert service.ed_create(**args)["document_id"] == reserved_by_project


@pytest.mark.parametrize(
    "changes,field",
    [
        ({"plan": "OtherPlan"}, "plan"),
        ({"format_version": "1.21"}, "format_version"),
        ({"interface_version": 3}, "interface_version"),
        ({"configuration": "Other"}, "configuration"),
        ({"mode": "import", "document_id": "other"}, "mode"),
        ({"project": "OtherProject", "configuration_path": None}, "project"),
        ({"configuration_path": "C:/elsewhere"}, "configuration_path"),
        ({"schema_id": "unopened"}, "schema_id"),
        ({"structure_id": "foreign"}, "structure_id"),
        ({"identity": {"name": "OtherExtension"}}, "identity"),
    ],
)
def test_repeated_create_rejects_different_arguments_r2(writer_setup, changes, field):
    service, args, _ = writer_setup
    created = service.ed_create(**args)
    with pytest.raises(EdAuthoringPreconditionError) as caught:
        service.ed_create(**{**args, **changes})
    assert field in {d["field"] for d in caught.value.details["differences"]}
    assert service.manager_workspace.get("positions").model.revision == created["revision"]


def test_timestamp_like_manager_id_is_allowed_r2(writer_setup):
    service, args, _ = writer_setup
    created = service.ed_create(**{**args, "project_id": "ed-pilot-202610051230"})
    assert service.ed_close(created["project_id"])["closed"]


def test_reader_manager_id_collisions_both_orders_r2(writer_setup):
    service, args, _ = writer_setup
    opened = service.ed_open(str(DATA / "pilot.bsl"))
    identifier = opened["project_id"]
    with pytest.raises(EdAuthoringPreconditionError):
        service.ed_create(**{**args, "project_id": identifier})
    service.ed_close(identifier)
    created = service.ed_create(**{**args, "project_id": identifier})
    for reader in (service, Kd2Service(service.settings)):
        with pytest.raises(EdAuthoringPreconditionError) as caught:
            reader.ed_open(str(DATA / "pilot.bsl"))
        assert caught.value.details["failures"][0]["id"] == "document_id_owned_by_manager"
    assert service.ed_close(created["project_id"])["closed"]
    assert service.ed_open(str(DATA / "pilot.bsl"))["project_id"] == identifier


def test_long_reader_id_closes_r2(writer_setup, tmp_path):
    service, _, _ = writer_setup
    path = tmp_path / ("М" * 120 + ".bsl")
    path.write_bytes((DATA / "pilot.bsl").read_bytes())
    opened = service.ed_open(str(path))
    assert len(opened["project_id"]) > 128
    assert service.ed_close(opened["project_id"])["closed"]
    with pytest.raises(ProjectNotFoundError):
        service.ed_close(opened["project_id"])


def test_default_extension_names_are_distinct_and_bounded_r2(writer_setup, tmp_path):
    original, args, root = writer_setup
    # Короткий workspace оставляет место для длинного project_id на Windows;
    # тест проверяет имена 1С, а не настройку Win32 long paths.
    service = Kd2Service(
        Settings(cache_dir=original.settings.cache_dir, workspace=tmp_path.parent / "names")
    )
    service.ed_schema_open(
        "1.20",
        path=str(root / "Schemas/format.bin"),
        imports={"urn:test:writer-message": str(DATA / "message.bin")},
    )
    identifiers = ["positions", "a-b", "a.b", "М" * 75 + "-а", "М" * 75 + "-б"]
    outputs, names, uuids = [], [], []
    for identifier in identifiers:
        created = service.ed_create(**{**args, "project_id": identifier})
        name = service._manager_metadata(identifier)["arguments"]["identity"]["name"]
        assert re.fullmatch(r"[\w]+", name) and len(name) <= 59
        written = write_kit(service, created)
        manifest = json.loads((Path(written["output_dir"]) / "manifest.json").read_bytes())
        names.append(name)
        outputs.append(written["output_dir"])
        uuids.append(manifest["identity_map"]["artifact_uuid"])
    assert len(set(names)) == len(set(outputs)) == len(set(uuids)) == len(identifiers)


def test_rebind_uses_catalog_extensions_r2(writer_setup, tmp_path):
    service, args, root = writer_setup
    service.ed_create(**{**args, "structure_id": None})
    extension = tmp_path / "extension"
    extension.mkdir()
    (extension / "Configuration.xml").write_text(
        "<MetaDataObject><Configuration><Properties><Name>TestExtension</Name><ConfigurationExtensionPurpose>Customization</ConfigurationExtensionPurpose></Properties><ChildObjects/></Configuration></MetaDataObject>",
        encoding="utf-8",
    )
    module = executor_profile.PROFILES[0].modules[0]
    path = extension / "CommonModules" / module.name / "Ext/Module.bsl"
    path.parent.mkdir(parents=True)
    path.write_text(
        f'&Перед("{module.procedures[0]}")\nПроцедура Hook()\nКонецПроцедуры\n', encoding="utf-8"
    )
    catalog = tmp_path / "catalog.yaml"
    catalog.write_text(
        "projects:\n  Example:\n    configurations:\n      Main:\n"
        "        dump: .\n        extensions:\n          - ../extension\n",
        encoding="utf-8",
    )
    restarted = Kd2Service(
        Settings(
            cache_dir=service.settings.cache_dir,
            workspace=service.settings.workspace,
            projects_file=catalog,
            project_dirs={"Example": root},
        )
    )
    restarted.ed_schema_open(
        "1.20",
        path=str(root / "Schemas/format.bin"),
        imports={"urn:test:writer-message": str(DATA / "message.bin")},
    )
    rebound = restarted.ed_create(
        "positions", mode="rebind", project="Example", configuration="Main"
    )
    metadata = restarted._manager_metadata("positions")
    assert metadata["arguments"]["extensions"] == [str(extension.resolve())]
    assert metadata["arguments"]["extensions_checked"] is True
    assert not rebound["executor_profile"]["verified"]
    explicit = restarted.ed_create(
        "positions", mode="rebind", project="Example", configuration="Main", extensions=[]
    )
    assert explicit["executor_profile"]["verified"]


def test_explicit_legacy_extension_keeps_uuid_seed_r2(writer_setup, tmp_path):
    service, args, root = writer_setup
    identity = {"name": "кд3м_Менеджер"}
    first = service.ed_create(**{**args, "identity": identity})
    original = json.loads(
        (Path(write_kit(service, first)["output_dir"]) / "manifest.json").read_bytes()
    )
    assert original["identity_map"]["artifact_uuid"] == "6c0733d6-071d-5cdc-95f7-37639c1a05d5"
    other = Kd2Service(
        Settings(cache_dir=service.settings.cache_dir, workspace=tmp_path / "other-workspace")
    )
    other.ed_schema_open(
        "1.20",
        path=str(root / "Schemas/format.bin"),
        imports={"urn:test:writer-message": str(DATA / "message.bin")},
    )
    second = other.ed_create(**{**args, "project_id": "another-project", "identity": identity})
    current = json.loads(
        (Path(write_kit(other, second)["output_dir"]) / "manifest.json").read_bytes()
    )
    assert current["identity_map"] == original["identity_map"]


@pytest.mark.parametrize("explicit_first", [False, True])
def test_extension_selection_difference_names_public_argument(writer_setup, explicit_first):
    service, args, _ = writer_setup
    first = args | ({"extensions": []} if explicit_first else {})
    second = args | ({} if explicit_first else {"extensions": []})
    service.ed_create(**first)
    with pytest.raises(EdAuthoringPreconditionError) as caught:
        service.ed_create(**second)
    differences = caught.value.details["differences"]
    assert {d["field"] for d in differences} == {"extensions"}
    assert differences[0]["existing"] == ([] if explicit_first else None)
    assert differences[0]["requested"] == (None if explicit_first else [])


@pytest.mark.skipif(os.name != "nt", reason="Ограничение путей Win32")
def test_long_windows_project_path_refuses_before_writing(writer_setup):
    service, args, _ = writer_setup
    before = files_at(service.manager_workspace.directory)
    with pytest.raises(EdAuthoringPathError) as caught:
        service.ed_create(**(args | {"project_id": "x" * 128}))
    assert error_payload(caught.value, service)["code"] == "ed_authoring_path"
    assert files_at(service.manager_workspace.directory) == before


def test_default_extension_suffix_uses_platform_letters_and_preserves_saved_name(writer_setup):
    service, args, root = writer_setup
    identifier = "ﬁ²٣-Яё_A1"
    created = service.ed_create(**(args | {"project_id": identifier}))
    name = service._manager_metadata(identifier)["arguments"]["identity"]["name"]
    assert re.fullmatch(r"[A-Za-zА-Яа-яЁё0-9_]+", name)
    assert "____Яё_A1" in name
    # Старое имя уже сохранённого проекта не пересчитывается после обновления сервера.
    folder = service.manager_workspace.directory / identifier
    metadata = service._manager_metadata(identifier)
    metadata["arguments"]["identity"]["name"] = "кд3м_Менеджер_ﬁ²٣"
    (folder / "creation.json").write_text(json.dumps(metadata, ensure_ascii=False), "utf-8")
    restarted = Kd2Service(service.settings)
    restarted.ed_schema_open(
        "1.20",
        path=str(root / "Schemas/format.bin"),
        imports={"urn:test:writer-message": str(DATA / "message.bin")},
    )
    repeated = restarted.ed_create(**(args | {"project_id": identifier}))
    assert repeated["revision"] == created["revision"]
    restarted.ed_create(identifier, mode="rebind")
    assert (
        restarted._manager_metadata(identifier)["arguments"]["identity"]["name"]
        == "кд3м_Менеджер_ﬁ²٣"
    )
    explicit = service.ed_create(
        **(args | {"project_id": "explicit-name", "identity": {"name": "кд3м_ﬁ²٣"}})
    )
    assert (
        service._manager_metadata(explicit["project_id"])["arguments"]["identity"]["name"]
        == "кд3м_ﬁ²٣"
    )


def test_stale_reader_can_close_without_removing_current_manager(writer_setup):
    service, args, root = writer_setup
    created = service.ed_create(**args)
    other = Kd2Service(service.settings)
    reopen_writer_schema(other, root)
    other.ed_create(**args)
    _, _, applied = apply_packet(other, created, manager_operations())
    assert service.ed_close(created["document_id"])["closed"]
    assert service.ed_create(**args)["revision"] == applied["revision"]
