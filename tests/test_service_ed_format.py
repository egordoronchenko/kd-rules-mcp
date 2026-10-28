"""Пакет проекта через сервис и MCP: импорт, сборка, перепривязка и F4."""

import hashlib
import json
from dataclasses import replace
from pathlib import Path

import anyio
import pytest
from lxml import etree
from mcp import Client

from kd_rules_mcp.authoring.ed.format_package import (
    format_package_data,
    format_package_hash,
    render_format_package,
)
from kd_rules_mcp.authoring.ed.workspace import ManagerWorkspace
from kd_rules_mcp.ed.schema import QName
from kd_rules_mcp.ed.writer_model import dump_model, logical_id
from kd_rules_mcp.errors import EdAuthoringPreconditionError
from kd_rules_mcp.server import create_server
from tests.data.ed.format_extension.model import OBJECT, URI, package_c
from tests.test_service_ed_writer import build, files_at, format_package_source
from tests.test_service_ed_writer import writer_setup as writer_setup


def apply_ops(service, created, operations):
    planned = service.ed_apply(created["project_id"], created["revision"], operations)
    assert not planned["failures"]["total"], planned["failures"]
    applied = service.ed_apply(
        created["project_id"],
        created["revision"],
        mode="apply",
        expected_preview_hash=planned["preview_hash"],
        confirmations=planned["required_confirmations"],
    )
    assert applied["revision"] == planned["future_revision"]
    return applied


def create_package(service, created, *, source=None, package=None):
    if source:
        patch = {"source": str(source)}
    else:
        assert package is not None
        patch = format_package_data(package)
    return apply_ops(
        service,
        created,
        [{"client_id": "package-C", "kind": "format_package", "action": "create", "patch": patch}],
    )


def write_kit(service, created, **kwargs):
    preview = build(service, created, **kwargs)
    result = build(
        service,
        created,
        mode="write",
        expected_preview_hash=preview["build_hash"],
        acknowledged_notices=preview["required_acknowledgements"],
        **kwargs,
    )
    return files_at(Path(result["output_dir"]))


@pytest.mark.parametrize("kind", ["relative", "absolute", "folder"])
def test_import_source_durable_detached_and_rebind(writer_setup, kind):
    service, args, root = writer_setup
    path = format_package_source(service, root)
    created = service.ed_create(**args)
    schema = service._manager_bound_schema(service._manager_metadata(created["project_id"]))
    original = service._authoring_format_package(str(path), schema, "1.20")
    if kind == "folder":
        path = path.parent / "xdto-folder"
        for relative, data in render_format_package(original).items():
            target = path / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        original = service._authoring_format_package(str(path), schema, "1.20")
    source = path.name if kind == "relative" else str(path)
    applied = create_package(service, created, source=source)
    model = service._manager_project(created["project_id"]).model
    package = model.format_packages[0]
    assert package.origin == "imported" and package.source_sha256
    source_file = next(path.rglob("Package.bin")) if path.is_dir() else path
    assert package.source_sha256 == hashlib.sha256(source_file.read_bytes()).hexdigest()
    assert format_package_hash(package) == format_package_hash(original)
    content = dump_model(model)
    changed_file = next(path.rglob("Package.bin")) if path.is_dir() else path
    changed_file.write_text("изменён после импорта", encoding="utf-8")
    assert dump_model(service._manager_project(created["project_id"]).model) == content
    restored = ManagerWorkspace(service.workspace.root).get(created["project_id"]).model
    assert restored.revision == applied["revision"] and dump_model(restored) == content
    rebound = service.ed_create(**args, mode="rebind")
    assert rebound["rebound"]
    assert format_package_hash(
        service._manager_project(created["project_id"]).model.format_packages[0]
    ) == format_package_hash(package)


@pytest.mark.parametrize(
    "field,value",
    [("namespace", "urn:wrong"), ("metadata_name", "Other"), ("client_id", "another")],
)
def test_import_patch_mismatch_refuses(writer_setup, field, value):
    service, args, root = writer_setup
    path = format_package_source(service, root)
    created = service.ed_create(**args)
    with pytest.raises(EdAuthoringPreconditionError) as caught:
        service.ed_apply(
            created["project_id"],
            created["revision"],
            [
                {
                    "client_id": "import",
                    "kind": "format_package",
                    "action": "create",
                    "patch": {"source": str(path), field: value},
                }
            ],
        )
    assert caught.value.details["failures"][0]["id"] == "format_package_invalid"
    assert not service._manager_project(created["project_id"]).model.format_packages


def test_model_and_file_package_same_bytes_uuid_manifest_and_conflict(writer_setup):
    service, args, root = writer_setup
    path = format_package_source(service, root)
    created = service.ed_create(**args)
    from_file = write_kit(service, created, format_package=str(path))
    applied = create_package(service, created, source=path)
    from_model = write_kit(service, applied)

    def package_files(files):
        return {p: b for p, b in files.items() if p.startswith("extension/XDTOPackages/")}

    assert package_files(from_model) == package_files(from_file)

    def ids(files):
        return etree.fromstring(files["extension/Configuration.xml"]).xpath(
            "//*[local-name()='XDTOPackage']/text()"
        )

    assert ids(from_model) == ids(from_file)
    manifest = json.loads(from_model["manifest.json"])
    model = service._manager_project(created["project_id"]).model
    assert manifest["format_package"]["model_sha256"] == format_package_hash(
        model.format_packages[0]
    )
    with pytest.raises(EdAuthoringPreconditionError) as caught:
        build(service, applied, format_package=str(path))
    assert caught.value.details["failures"][0]["id"] == "format_package_conflict"
    assert "model:Пакет/" in caught.value.details["failures"][0]["message"]
    data = json.loads(path.read_text("utf-8"))
    data = json.loads(json.dumps(data).replace(data["namespace"], data["namespace"] + ":second"))
    data["metadata_name"] += "Второй"
    second = path.with_name("second.json")
    second.write_text(json.dumps(data), encoding="utf-8")
    both = write_kit(service, applied, format_package=str(second))
    assert "extension/XDTOPackages/кд3м_ПакетВторой.xml" in both
    assert "extension/XDTOPackages/кд3м_Пакет.xml" in both


def test_rebind_other_version_reports_package_mismatch(writer_setup):
    service, args, root = writer_setup
    path = format_package_source(service, root)
    created = service.ed_create(**args)
    create_package(service, created, source=path)
    before = service._manager_project(created["project_id"]).model.format_packages[0]
    opened = service.ed_schema_open(
        "2.30",
        path=str(root / "Schemas/format.bin"),
        imports={
            "urn:test:writer-message": str(Path(__file__).parent / "data/ed/writer/message.bin")
        },
    )
    rebound = service.ed_create(
        **{**args, "format_version": "2.30", "schema_id": opened["schema_id"]}, mode="rebind"
    )
    assert ("ed.format.version_mismatch", "Пакет/кд3м_Пакет") in {
        (i["check"], i["address"]) for i in rebound["validation"]["issues"]
    }
    after = service._manager_project(created["project_id"]).model.format_packages[0]
    assert after == before and format_package_hash(after) == format_package_hash(before)


def test_ed_apply_import_over_mcp_and_missing_schema_failure(writer_setup):
    service, args, root = writer_setup
    path = format_package_source(service, root)
    created = service.ed_create(**args)

    async def scenario():
        async with Client(create_server(service)) as client:
            result = await client.call_tool(
                "ed_apply",
                {
                    "project_id": created["project_id"],
                    "expected_revision": created["revision"],
                    "operations": [
                        {
                            "client_id": "import",
                            "kind": "format_package",
                            "action": "create",
                            "patch": {"source": str(path)},
                        }
                    ],
                },
            )
            assert not result.is_error, result.content
            preview = result.structured_content
            assert preview and not preview["failures"]["total"]
            result = await client.call_tool(
                "ed_apply",
                {
                    "project_id": created["project_id"],
                    "expected_revision": created["revision"],
                    "mode": "apply",
                    "expected_preview_hash": preview["preview_hash"],
                },
            )
            assert not result.is_error, result.content
            assert result.structured_content and result.structured_content["applied"]

    anyio.run(scenario)
    created = service.ed_create(**{**args, "project_id": "without-schema", "schema_id": None})
    result = service.ed_apply(
        created["project_id"],
        created["revision"],
        [
            {
                "client_id": "empty",
                "kind": "format_package",
                "action": "create",
                "patch": {"metadata_name": "fmt_Empty", "namespace": "urn:fiction:empty"},
            }
        ],
    )
    assert result["failures"]["items"][0]["reason"] == "format_schema_required"


def test_preview_contract_without_model_packages(writer_setup):
    service, args, _ = writer_setup
    created = service.ed_create(**args)
    result = service.ed_apply(
        created["project_id"],
        created["revision"],
        [
            {
                "client_id": "unknown-type",
                "kind": "pko",
                "action": "create",
                "patch": {
                    "name": "Неизвестный",
                    "directions": ["send"],
                    "format_object": {"state": "string", "value": "НетТипа"},
                },
            }
        ],
    )
    assert not result["failures"]["total"]
    # Без init и без своего типа это вопрос версии базовой схемы (многоверсионный модуль):
    # контракт пакета не выдаёт ошибку, иначе ломается сборка импортированных типовых модулей.
    assert "ed.format.type_missing" not in {
        i["check"] for i in result.get("validation", {}).get("issues", [])
    }


def own_rules():
    rows = []
    for direction in ("send", "receive"):
        owner = "pko-" + direction
        rows.append(
            {
                "client_id": owner,
                "kind": "pko",
                "action": "create",
                "patch": {
                    "name": "Задача_" + direction,
                    "directions": [direction],
                    "format_object": {"state": "string", "value": OBJECT},
                    "extensions": [URI],
                },
            }
        )
        for index, name in enumerate(("КлючевыеСвойства.Наименование", "Состояние", "Родитель")):
            rows.append(
                {
                    "client_id": owner + str(index),
                    "kind": "property",
                    "action": "create",
                    "owner_id": {"client_id": owner},
                    "patch": {
                        "configuration_property": "Поле" + str(index),
                        "format_property": name,
                        "namespace": URI,
                    },
                }
            )
        table = owner + "-table"
        rows.append(
            {
                "client_id": table,
                "kind": "table_part",
                "action": "create",
                "owner_id": {"client_id": owner},
                "patch": {
                    "configuration_property": "Шаги",
                    "format_property": "Шаги",
                    "namespace": URI,
                },
            }
        )
        rows.append(
            {
                "client_id": table + "-text",
                "kind": "property",
                "action": "create",
                "owner_id": {"client_id": table},
                "patch": {
                    "configuration_property": "Текст",
                    "format_property": "Текст",
                    "namespace": URI,
                },
            }
        )
        rows.append(
            {
                "client_id": "pod-" + direction,
                "kind": "pod",
                "action": "create",
                "patch": {
                    "name": "Задачи_" + direction,
                    "directions": [direction],
                    "format_selection": {"state": "string", "value": OBJECT},
                },
            }
        )
    return rows


def test_service_type_property_crud_move_and_empty_package_build(writer_setup):
    service, args, root = writer_setup
    path = format_package_source(service, root)
    created = service.ed_create(**args)
    current = create_package(service, created, source=path)
    owner = "Пакет/кд3м_Пакет"
    packets = [
        {
            "kind": "format_type",
            "action": "create",
            "owner_id": {"address": owner},
            "position_mode": "default",
            "patch": {"name": "Значение", "role": "value"},
        },
        {
            "kind": "format_type",
            "action": "update",
            "address": owner + "/Тип/Значение",
            "patch": {"name": "НовоеЗначение"},
        },
        {
            "kind": "format_type",
            "action": "move",
            "address": owner + "/Тип/НовоеЗначение",
            "after_id": None,
        },
        {"kind": "format_type", "action": "delete", "address": owner + "/Тип/НовоеЗначение"},
        {
            "kind": "format_property",
            "action": "create",
            "owner_id": {"address": owner + "/Тип/ДопСвойства"},
            "position_mode": "default",
            "patch": {
                "name": "Новое",
                "type": {"namespace": "http://www.w3.org/2001/XMLSchema", "local": "string"},
            },
        },
        {
            "kind": "format_property",
            "action": "update",
            "address": owner + "/Тип/ДопСвойства/Свойство/Новое",
            "patch": {"name": "Другое", "lower": 0},
        },
        {
            "kind": "format_property",
            "action": "move",
            "address": owner + "/Тип/ДопСвойства/Свойство/Другое",
            "after_id": None,
        },
        {
            "kind": "format_property",
            "action": "delete",
            "address": owner + "/Тип/ДопСвойства/Свойство/Другое",
        },
        {
            "kind": "format_package",
            "action": "update",
            "address": owner,
            "patch": {"metadata_name": "кд3м_Переименованный"},
        },
        {"kind": "format_package", "action": "delete", "address": "Пакет/кд3м_Переименованный"},
    ]
    before = service._manager_project(created["project_id"]).model.format_packages[0]
    for index, packet in enumerate(packets):
        current = apply_ops(service, current, [{"client_id": "step" + str(index), **packet}])
    assert not service._manager_project(created["project_id"]).model.format_packages
    assert before.origin == "imported"
    current = apply_ops(
        service,
        current,
        [
            {
                "client_id": "empty",
                "kind": "format_package",
                "action": "create",
                "patch": {"metadata_name": "кд3м_Пустой", "namespace": "urn:fiction:empty"},
            }
        ],
    )
    with pytest.raises(EdAuthoringPreconditionError) as caught:
        build(service, current)
    assert caught.value.details["failures"][0]["id"] == "format_package_invalid"


@pytest.mark.parametrize(
    "patch",
    [
        {"name": "Пустой", "role": "object"},
        {"name": "ДопСвойства", "role": "value"},
        {
            "name": "Неизвестный",
            "role": "value",
            "base": {"namespace": "urn:missing", "local": "Unknown"},
        },
        {
            "name": "ПлохойURI",
            "role": "object",
            "properties": [
                {
                    "name": {"namespace": "urn:wrong", "local": "Поле"},
                    "type": {"namespace": "http://www.w3.org/2001/XMLSchema", "local": "string"},
                }
            ],
        },
    ],
)
def test_service_invariant_failure_is_atomic(writer_setup, patch):
    service, args, root = writer_setup
    path = format_package_source(service, root)
    created = service.ed_create(**args)
    current = create_package(service, created, source=path)
    before = dump_model(service._manager_project(created["project_id"]).model)
    packet = [
        {
            "client_id": "invalid",
            "kind": "format_type",
            "action": "create",
            "owner_id": {"address": "Пакет/кд3м_Пакет"},
            "patch": patch,
        }
    ]
    try:
        result = service.ed_apply(current["project_id"], current["revision"], packet)
    except EdAuthoringPreconditionError as error:
        assert error.details["failures"][0]["id"] == "format_package_invalid"
    else:
        assert result["failures"]["items"][0]["reason"] == "format_package_invalid"
    assert dump_model(service._manager_project(created["project_id"]).model) == before


def test_pair_manager_documents_compare_packages_and_skip_reader(writer_setup):
    service, args, root = writer_setup
    path = format_package_source(service, root)
    first = create_package(service, service.ed_create(**args), source=path)
    second = create_package(
        service, service.ed_create(**{**args, "project_id": "second"}), source=path
    )
    result = service.ed_pair_validate(
        first["document_id"], second["document_id"], version="1.20", check_prefix="ed.format."
    )
    assert not result["issues"]["items"]
    second = apply_ops(
        service,
        second,
        [
            {
                "client_id": "change-type",
                "kind": "format_type",
                "action": "update",
                "address": "Пакет/кд3м_Пакет/Тип/ДопСвойства",
                "patch": {"open": True},
            }
        ],
    )
    result = service.ed_pair_validate(
        first["document_id"],
        second["document_id"],
        version="1.20",
        check_prefix="ed.format.package_hash_mismatch",
    )
    assert result["issues"]["total"] == 1
    model = service._manager_project("positions").model
    from kd_rules_mcp.ed.writer import render

    raw_path = service.workspace.root / "reader.bsl"
    raw_path.write_bytes(render(model).data)
    reader = service.ed_open(str(raw_path))
    skipped = service.ed_pair_validate(
        first["document_id"],
        reader["project_id"],
        version="1.20",
        section="skipped",
        check_prefix="ed.format.",
    )
    assert {s["reason"] for s in skipped["skipped"]["items"]} == {"not_manager_documents"}


def test_f4_schema_roundtrip_own_properties_and_missing_property(writer_setup):
    service, args, root = writer_setup
    format_package_source(service, root)
    created = service.ed_create(**args)
    base = service._manager_bound_schema(service._manager_metadata(created["project_id"]))
    package = replace(package_c(base), metadata_name="кд3м_УчебныйПакетC")
    applied = create_package(service, created, package=package)
    kit = write_kit(service, applied)
    package_bin = service.workspace.root / "from-kit/Package.bin"
    package_bin.parent.mkdir()
    package_bin.write_bytes(
        kit["extension/XDTOPackages/" + package.metadata_name + "/Ext/Package.bin"]
    )
    opened = service.ed_schema_open(
        "1.20",
        path=str(root / "Schemas/format.bin"),
        extensions=[str(package_bin)],
        imports={
            "urn:test:writer-message": str(Path(__file__).parent / "data/ed/writer/message.bin")
        },
    )
    schema = service._ed_schemas[opened["schema_id"]].schema
    for typ in package.types:
        assert typ.name in schema.types
        assert {p.name for p in schema.types[typ.name].properties} == {
            p.name for p in typ.properties
        }
    assert QName(URI, "УчебнаяСтрока") in schema.types
    assert {
        f.lexical
        for f in schema.types[QName(URI, "СостояниеЗадачи")].facets
        if f.kind == "enumeration"
    } == {"Новая", "Завершена"}
    original_schema = service._ed_schemas[args["schema_id"]].schema
    applied = apply_ops(service, applied, own_rules())
    checked = service.ed_validate(
        applied["document_id"], schema_id=args["schema_id"], check_prefix="ed.schema.", limit=200
    )
    assert not checked["issues"]["items"], checked
    # Ревью 28.10, находка 5: схема, в которую уже входит пакет комплекта того же URI
    # (установленное расширение в выгрузке), — пакет проекта заменяет его, отказа нет.
    with_kit = service.ed_validate(
        applied["document_id"], schema_id=opened["schema_id"], check_prefix="ed.schema.", limit=200
    )
    assert not with_kit["issues"]["items"], with_kit
    assert service._ed_schemas[args["schema_id"]].schema is original_schema
    assert (
        service.workspace.root
        / ".ed-projects/positions/format/XDTOPackages"
        / (package.metadata_name + "/Ext/Package.bin")
    ).is_file()
    assert (
        service.ed_validate(applied["document_id"], check_prefix="ed.format.type_missing")[
            "issues"
        ]["total"]
        == 0
    )
    uninitialized = apply_ops(
        service,
        applied,
        [
            {
                "client_id": "without-init",
                "kind": "pko",
                "action": "update",
                "target_id": logical_id("positions", "pko-send"),
                "patch": {"extensions": []},
            }
        ],
    )
    assert (
        service.ed_validate(uninitialized["document_id"], check_prefix="ed.format.type_missing")[
            "issues"
        ]["total"]
        == 1
    )
    applied = apply_ops(
        service,
        uninitialized,
        [
            {
                "client_id": "restore-init",
                "kind": "pko",
                "action": "update",
                "target_id": logical_id("positions", "pko-send"),
                "patch": {"extensions": [URI]},
            }
        ],
    )
    broken = apply_ops(
        service,
        applied,
        [
            {
                "client_id": "wrong-property",
                "kind": "property",
                "action": "update",
                "target_id": logical_id("positions", "pko-send1"),
                "patch": {"format_property": "Несуществующее"},
            }
        ],
    )
    checked = service.ed_validate(
        broken["document_id"], schema_id=args["schema_id"], check_prefix="ed.schema.", limit=200
    )
    assert checked["issues"]["total"] >= 1
    assert any(
        i["check"] == "ed.schema.property_missing" and "Несуществующее" in i["message"]
        for i in checked["issues"]["items"]
    )
    applied = apply_ops(
        service,
        broken,
        [
            {
                "client_id": "pkpd-own",
                "kind": "pkpd",
                "action": "create",
                "patch": {
                    "name": "Состояния",
                    "configuration_type": {
                        "state": "reference",
                        "reference_parts": ["Метаданные", "Перечисления", "Состояния"],
                    },
                    "format_type": {"state": "string", "value": "СостояниеЗадачи"},
                    "directions": ["send"],
                },
            }
        ],
    )
    result = service.ed_validate(applied["document_id"], check_prefix="ed.format.pkpd_unsupported")
    assert result["issues"]["total"] == 1
