"""Состав плана, доставка заимствований и регрессии пятого захода."""

import sqlite3

import pytest
from lxml import etree

from kd_rules_mcp.authoring.ed.manager_operations import parse_operation, preview
from kd_rules_mcp.authoring.ed.manager_render import ManagerManifest
from kd_rules_mcp.ed.writer import new_manager
from kd_rules_mcp.errors import (
    EdAuthoringAckRequiredError,
    EdAuthoringPreconditionError,
    EdSchemaNotFoundError,
    StructureNotFoundError,
)
from kd_rules_mcp.server import error_payload
from tests.test_ed_writer_code import execute, round_trip
from tests.test_ed_writer_tables import table_model
from tests.test_service_ed_writer import (
    PLAN,
    apply_packet,
    build,
    manager_operations,
)
from tests.test_service_ed_writer import (
    writer_setup as writer_setup,
)


def add_content(service, included):
    with sqlite3.connect(service.store.path("host")) as conn:
        cur = conn.execute(
            "INSERT INTO objects(kind,name,type_name) VALUES(?,?,?)",
            ("ПланОбмена", PLAN, "ПланОбменаСсылка." + PLAN),
        )
        ident = cur.lastrowid
        cur = conn.execute(
            "INSERT INTO properties(object_id,kind,name,path,is_group) VALUES(?,?,?,?,1)",
            (ident, "СоставПланаОбмена", "{Состав}", "{Состав}"),
        )
        group = cur.lastrowid
        if included:
            conn.execute(
                "INSERT OR IGNORE INTO type_sets(types) VALUES(?)", ("СправочникСсылка.Должности",)
            )
            type_id = conn.execute(
                "SELECT id FROM type_sets WHERE types=?", ("СправочникСсылка.Должности",)
            ).fetchone()[0]
            conn.execute(
                "INSERT INTO properties(object_id,parent_id,kind,name,path,type_set_id) "
                "VALUES(?,?,?,?,?,?)",
                (
                    ident,
                    group,
                    "ЭлементСоставаПланаОбмена",
                    "Должности",
                    "{Состав}.Должности",
                    type_id,
                ),
            )


@pytest.mark.parametrize("included", [False, True])
def test_plan_content_warning_and_delivery(writer_setup, included):
    service, args, host = writer_setup
    (host / "Catalogs").mkdir(exist_ok=True)
    (host / "Catalogs/Должности.xml").write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<MetaDataObject xmlns="http://v8.1c.ru/8.3/MDClasses" version="2.20">'
        '<Catalog uuid="44444444-4444-4444-8444-444444444444"><InternalInfo>'
        '<GeneratedType xmlns="http://v8.1c.ru/8.3/xcf/readable" '
        'name="CatalogRef.Должности" category="Ref"/>'
        "</InternalInfo><Properties>"
        "<Name>Должности</Name></Properties><ChildObjects/></Catalog></MetaDataObject>",
        encoding="utf-8",
    )
    add_content(service, included)
    created = service.ed_create(**args)
    _, _, applied = apply_packet(service, created, manager_operations())
    checked = service.ed_validate(
        applied["document_id"], schema_id=args["schema_id"], structure_id="host"
    )
    issues = [i for i in checked["issues"]["items"] if i["check"] == "ed.plan.content_missing"]
    assert len(issues) == (0 if included else 1)
    result = build(service, applied)
    notice_page = build(service, applied, section="notices")
    notices = [n for n in notice_page["items"] if n.get("check") == "ed.plan.content_missing"]
    assert len(notices) == len(issues)
    if not included:
        with pytest.raises(EdAuthoringAckRequiredError):
            build(service, applied, mode="write", expected_preview_hash=result["build_hash"])
    written = build(
        service,
        applied,
        mode="write",
        expected_preview_hash=result["build_hash"],
        acknowledged_notices=result["required_acknowledgements"],
    )
    from pathlib import Path

    root = Path(written["output_dir"])
    path = root / "extension/ExchangePlans" / PLAN / "Ext/Content.xml"
    assert path.exists() is not included
    manifest = ManagerManifest.from_bytes((root / "manifest.json").read_bytes())
    if included:
        assert "plan_content_additions" not in manifest.to_dict()
    else:
        content = etree.parse(str(path))
        assert content.findtext("{*}Item/{*}Metadata") == "Catalog.Должности"
        assert content.findtext("{*}Item/{*}AutoRecord") == "Deny"
        assert content.findtext("{*}ExtensionProperty/{*}Item/{*}State") == "Modify"
        obj = etree.parse(str(root / "extension/Catalogs/Должности.xml"))
        assert obj.findtext("{*}Catalog/{*}Properties/{*}ObjectBelonging") == "Adopted"
        assert obj.findtext("{*}Catalog/{*}Properties/{*}ExtendedConfigurationObject")
        assert manifest.to_dict()["plan_content_additions"] == ["Catalog.Должности"]
        instruction = (root / "instruction.md").read_text("utf-8")
        assert "Состав плана дополнен" in instruction
        # Д-П2 07.10.2026: переменная цикла затирала версию формата в инструкции.
        assert "subscription_adopted_objects" not in instruction
        assert "1.20" in instruction


@pytest.mark.parametrize("direction,notice", [("send", False), ("receive", True), ("both", True)])
def test_d6_table_notice_follows_owner_direction(direction, notice):
    model = execute(
        new_manager(),
        parse_operation(
            {
                "client_id": "owner",
                "kind": "pko",
                "action": "create",
                "patch": {"name": "Item", "directions": [direction]},
            }
        ),
    )
    op = parse_operation(
        {
            "client_id": "rows",
            "kind": "table_part",
            "action": "create",
            "owner_id": model.pko[0].logical_id,
            "patch": {"configuration_property": "", "format_property": "Rows"},
        }
    )
    result = preview(model, (op,), expected_revision=model.revision)
    assert not result.failures
    assert any(n.code == "table_part_replace" for n in result.notices) is notice
    round_trip(result.model)


def test_d7_conflict_names_persistent_decision_and_entity():
    model = table_model()
    op = parse_operation(
        {
            "client_id": "rule",
            "kind": "pko",
            "action": "create",
            "patch": {"name": "Other", "directions": ["receive"]},
        }
    )
    result = preview(model, (op,), expected_revision=model.revision)
    assert result.failures[0].address == "ПКО/Item"
    assert "rule" in result.failures[0].message and "ПКО/Item" in result.failures[0].message
    assert result.failures[0].references == ("ПКО/Item",)


def test_d9_read_and_edit_algorithm_address_aliases(writer_setup):
    service, args, _ = writer_setup
    created = service.ed_create(**args)
    _, _, applied = apply_packet(
        service,
        created,
        [
            {
                "client_id": "algo",
                "kind": "algorithm",
                "action": "create",
                "patch": {"name": "Normalize", "body": "Возврат;"},
            }
        ],
    )
    for address in ("Алгоритм/Normalize", "Код/Normalize"):
        assert service.ed_get(applied["document_id"], address)["address"] == "Код/Normalize"
        result = service.ed_apply(
            "positions",
            applied["revision"],
            [
                {
                    "client_id": "edit-" + address,
                    "kind": "algorithm",
                    "action": "update",
                    "address": address,
                    "patch": {"body": "\n\tВозврат;\n"},
                }
            ],
        )
        assert not result["failures"]["total"]
    assert (
        service.ed_list(applied["document_id"], "algorithm")["items"][0]["address"]
        == "Код/Normalize"
    )


@pytest.mark.parametrize("tool", ["validate", "candidates", "build", "apply"])
def test_d10_schema_recovery_parameters_are_serialized(writer_setup, monkeypatch, tool):
    service, args, _ = writer_setup
    created = service.ed_create(**args)
    service.ed_schema_close(args["schema_id"])
    if tool == "build":
        metadata = service._manager_metadata("positions")
        metadata["schema_packages"] = []
        monkeypatch.setattr(service, "_manager_metadata", lambda _: metadata)
    if tool == "apply":

        def missing(*a, **kw):
            raise EdSchemaNotFoundError("Схема формата не открыта")

        monkeypatch.setattr(service, "_manager_inputs", missing)
    with pytest.raises((EdSchemaNotFoundError, EdAuthoringPreconditionError)) as error:
        if tool == "validate":
            service.ed_validate(created["document_id"], schema_id=args["schema_id"])
        elif tool == "candidates":
            service.ed_authoring_candidates(
                {
                    "project_id": "positions",
                    "schema_id": args["schema_id"],
                    "structure_id": "host",
                    "direction": "send",
                },
                "objects",
                scope="manager",
            )
        elif tool == "build":
            build(service, created)
        else:
            service.ed_apply("positions", created["revision"], [])
    data = error_payload(error.value, service)
    call = next(c for c in data["reopen_calls"] if c["tool"] == "ed_schema_open")
    assert call["arguments"]["format_version"] == "1.20"
    assert call["arguments"].get("path")


def test_d10_structure_recovery_parameters(writer_setup, monkeypatch):
    service, args, _ = writer_setup
    created = service.ed_create(**args)

    def missing(*a, **kw):
        raise StructureNotFoundError("Структура не открыта")

    monkeypatch.setattr(service, "_ed_structure_snapshot", missing)
    with pytest.raises(StructureNotFoundError) as error:
        build(service, created)
    data = error_payload(error.value, service)
    assert data["reopen_calls"][0]["tool"] == "structure_load_xml"
    assert data["reopen_calls"][0]["arguments"]["structure_id"] == "host"
