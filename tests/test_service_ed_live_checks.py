"""Новые предупреждения видны в навигации и требуют подтверждения сборки."""

import sqlite3
from pathlib import Path

import pytest
from lxml import etree

from kd_rules_mcp.authoring.ed.manifest import sha256
from kd_rules_mcp.errors import EdAuthoringAckRequiredError
from tests.test_service_ed_writer import DATA, apply_packet, build, manager_operations
from tests.test_service_ed_writer import writer_setup as writer_setup


def test_live_enum_warning_requires_build_ack(writer_setup):
    service, args, root = writer_setup
    path = root / "Schemas/format.bin"
    tree = etree.parse(str(path))
    owner = next(n for n in tree.iter() if n.get("name") == "Справочник.Должности")
    ns = "{http://v8.1c.ru/8.1/xdto}"
    etree.SubElement(
        owner, ns + "property", name="ВидРасчетов", type="t:ВидыРасчетов", lowerBound="0"
    )
    enum = etree.SubElement(tree.getroot(), ns + "valueType", name="ВидыРасчетов", base="xs:string")
    etree.SubElement(enum, ns + "enumeration").text = "Займы"
    tree.write(str(path), encoding="utf-8")
    service.ed_schema_close(args["schema_id"])
    args = args | {
        "schema_id": service.ed_schema_open(
            "1.20", path=str(path), imports={"urn:test:writer-message": str(DATA / "message.bin")}
        )["schema_id"]
    }
    created = service.ed_create(**args)
    operations = [
        {
            "client_id": "pod-enum",
            "kind": "pod",
            "action": "create",
            "patch": {
                "name": "Получение",
                "directions": ["receive"],
                "format_selection": {"state": "string", "value": "Справочник.Должности"},
            },
        },
        {
            "client_id": "enum-handler",
            "kind": "handler",
            "action": "create",
            "owner_id": {"client_id": "pod-enum"},
            "patch": {
                "event": "ПриОбработке",
                "body": "ВидРасчетов = Неопределено; "
                'ДанныеXDTO.Свойство("ВидРасчетов", ВидРасчетов); '
                'Если ВидРасчетов = "Займы" Тогда КонецЕсли;',
            },
        },
    ]
    _, _, applied = apply_packet(service, created, operations)
    report = service.ed_validate(applied["document_id"], schema_id=args["schema_id"])
    assert any(i["check"] == "ed.handler.format_enum_as_string" for i in report["issues"]["items"])
    preview = build(service, applied)
    assert any(
        n.startswith("ed.handler.format_enum_as_string:")
        for n in preview["required_acknowledgements"]
    )
    with pytest.raises(EdAuthoringAckRequiredError):
        build(service, applied, mode="write", expected_preview_hash=preview["build_hash"])
    assert build(
        service,
        applied,
        mode="write",
        expected_preview_hash=preview["build_hash"],
        acknowledged_notices=preview["required_acknowledgements"],
    )["written"]


def test_key_queries_are_manifest_bound_and_repeatable(writer_setup):
    service, args, root = writer_setup
    path = root / "Schemas/format.bin"
    tree = etree.parse(str(path))
    for node in tree.iter():
        if node.get("name") == "КлючевыеСвойства":
            node.set("lowerBound", "1")
    tree.write(str(path), encoding="utf-8")
    service.ed_schema_close(args["schema_id"])
    args = args | {
        "schema_id": service.ed_schema_open(
            "1.20", path=str(path), imports={"urn:test:writer-message": str(DATA / "message.bin")}
        )["schema_id"]
    }
    created = service.ed_create(**args)
    _, _, applied = apply_packet(service, created, manager_operations())
    preview = build(service, applied)
    written = build(
        service,
        applied,
        mode="write",
        expected_preview_hash=preview["build_hash"],
        acknowledged_notices=preview["required_acknowledgements"],
    )
    folder = Path(written["output_dir"])
    text = (folder / "instruction.md").read_text("utf-8")
    assert "ShowError" in text and "Данные.Наименование" in text
    import json

    manifest = json.loads((folder / "manifest.json").read_bytes())
    assert manifest["file_hashes"]["instruction.md"] == sha256(
        (folder / "instruction.md").read_bytes()
    )
    assert build(service, applied)["status"] == "unchanged"


def test_composite_reference_warning_requires_build_ack(writer_setup):
    from tests.test_ed_writer_candidates import add_prop

    service, args, _ = writer_setup
    connection = sqlite3.connect(service.store.path("host"))
    try:
        add_prop(
            connection,
            "Справочник",
            "Должности",
            "Реквизит",
            "Источник",
            ("СправочникСсылка.Должности", "СправочникСсылка.Классификатор"),
        )
        connection.commit()
    finally:
        connection.close()
    created = service.ed_create(**args)
    operations = [
        {
            "client_id": "pko",
            "kind": "pko",
            "action": "create",
            "patch": {
                "name": "Ссылка",
                "directions": ["send"],
                "configuration_object": {
                    "state": "reference",
                    "reference_parts": ["Метаданные", "Справочники", "Должности"],
                },
                "format_object": {"state": "string", "value": "Справочник.Должности"},
            },
        },
        {
            "client_id": "ref",
            "kind": "property",
            "action": "create",
            "owner_id": {"client_id": "pko"},
            "patch": {
                "configuration_property": "Источник",
                "format_property": "Классификатор",
                "property_kind": "reference",
                "conversion": {"client_id": "pko"},
            },
        },
    ]
    _, _, applied = apply_packet(service, created, operations)
    report = service.ed_validate(
        applied["document_id"], schema_id=args["schema_id"], structure_id="host"
    )
    assert any(i["check"] == "ed.schema.reference_type_partial" for i in report["issues"]["items"])
    preview = build(service, applied)
    assert any(
        n.startswith("ed.schema.reference_type_partial:")
        for n in preview["required_acknowledgements"]
    )
    with pytest.raises(EdAuthoringAckRequiredError):
        build(service, applied, mode="write", expected_preview_hash=preview["build_hash"])
