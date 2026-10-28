"""Снимки, совместимость HEAD и атомарные операции пакетов формата."""

import hashlib
import json
from dataclasses import replace

import pytest

from kd_rules_mcp.authoring.ed.format_package import format_package_data, format_package_hash
from kd_rules_mcp.authoring.ed.manager_operations import apply, parse_operation, preview
from kd_rules_mcp.ed.canonical import canonical_model
from kd_rules_mcp.ed.writer import new_manager
from kd_rules_mcp.ed.writer_model import (
    FormatBinding,
    FormatPackageEntity,
    decode_dto,
    dump_model,
    load_model,
    model_addresses,
)
from tests.data.ed.format_extension.model import OBJECT, URI, package_c
from tests.test_ed_format_package_kit import (
    test_without_format_package_baseline_after_install_instruction_fix as check_kit_baseline,
)


def entity(package=None):
    package = package or package_c()
    return decode_dto(
        FormatPackageEntity,
        {**format_package_data(package), "logical_id": "package-C", "name": package.metadata_name},
    )


def manager(*, packages=True):
    package = package_c()
    return replace(
        new_manager(project_id="format-model"),
        format_bindings=(FormatBinding(package.base_version, package.base_namespace, ""),),
        format_packages=(entity(package),) if packages else (),
    ).with_revision()


def execute_format(model, *payloads):
    operations = tuple(parse_operation(p) for p in payloads)
    schema = package_c().base_schema
    planned = preview(model, operations, expected_revision=model.revision, format_schema=schema)
    assert not planned.failures, planned.failures
    return apply(
        model,
        operations,
        expected_revision=model.revision,
        expected_preview_hash=planned.preview_hash,
        format_schema=schema,
    )


def operation(kind, action, **kwargs):
    return {"client_id": kind + action, "kind": kind, "action": action, **kwargs}


def test_head_empty_snapshot_revision_canonical_and_every_kit_file():
    # Значения получены на HEAD до изменения модели 28.10.2026.
    model = new_manager(project_id="format-baseline", manager_name="Baseline")
    assert (
        hashlib.sha256(dump_model(model)).hexdigest()
        == "25b6560a43640f5130478ee9b6462c8f8d6cec8731086f25837210efa2ea8c0c"
    )
    assert model.revision == "c57c3b177537a3adf4aa9270fad01669647f7c03995af8ff2db786d378f1b735"
    assert (
        canonical_model(model).sha256
        == "58b6fb52ba5d9f9a44de5fa8ee58ccc2839d3e85ec70a6dcadb901261852a91a"
    )
    assert dump_model(load_model(dump_model(model))) == dump_model(model)
    check_kit_baseline()


def test_snapshot_with_package_and_derived_addresses():
    model = manager()
    restored = load_model(dump_model(model))
    assert restored == model
    assert dump_model(restored) == dump_model(model)
    assert format_package_data(restored.format_packages[0]) == format_package_data(package_c())
    assert format_package_hash(restored.format_packages[0]) == format_package_hash(package_c())
    addresses = set(model_addresses(model).values())
    assert "Пакет/fmt_УчебныйПакетC/Тип/" + OBJECT + "/Свойство/Родитель" in addresses


def test_create_package_default_binding_and_determinism(tmp_path):
    model = manager(packages=False)
    data = format_package_data(package_c())
    del data["base_version"], data["base_namespace"]
    op = operation("format_package", "create", patch=data)
    first = execute_format(model, op)
    second = execute_format(load_model(dump_model(model)), op)
    assert first.revision == second.revision
    assert dump_model(first) == dump_model(second)
    assert first.format_packages[0].metadata_name == "fmt_УчебныйПакетC"
    from tests.test_ed_format_package import declaration_model
    from tests.test_ed_format_package_kit import combined_kit

    declaration = declaration_model(tmp_path)
    one = combined_kit(declaration, model=first, package=package_c())
    two = combined_kit(declaration, model=second, package=package_c())
    assert one.files == two.files


def test_type_property_create_update_move_delete_and_hash():
    model = manager()
    package_address = "Пакет/fmt_УчебныйПакетC"
    model = execute_format(
        model,
        operation(
            "format_type",
            "create",
            owner_id={"address": package_address},
            position_mode="default",
            patch={"name": "Служебный", "role": "value"},
        ),
    )
    address = package_address + "/Тип/Служебный"
    model = execute_format(
        model,
        operation(
            "format_type",
            "update",
            address=address,
            patch={
                "name": "Служебный2",
                "base": {"namespace": "http://www.w3.org/2001/XMLSchema", "local": "string"},
            },
        ),
    )
    address += "2"
    model = execute_format(model, operation("format_type", "move", address=address, after_id=None))
    assert model.format_packages[0].types[0].name.local == "Служебный2"
    model = execute_format(model, operation("format_type", "delete", address=address))
    owner = package_address + "/Тип/" + OBJECT
    before_hash = format_package_hash(model.format_packages[0])
    model = execute_format(
        model,
        operation(
            "format_property",
            "create",
            owner_id={"address": owner},
            position_mode="default",
            patch={
                "name": "Описание",
                "type": {"namespace": "http://www.w3.org/2001/XMLSchema", "local": "string"},
                "upper": None,
            },
        ),
    )
    assert model.format_packages[0].types[-1].properties[-1].upper is None
    model = execute_format(
        model,
        operation(
            "format_property",
            "update",
            address=owner + "/Свойство/Описание",
            patch={"name": "Описание2", "lower": 0},
        ),
    )
    model = execute_format(
        model,
        operation("format_property", "move", address=owner + "/Свойство/Описание2", after_id=None),
    )
    assert model.format_packages[0].types[-1].properties[0].name.local == "Описание2"
    model = execute_format(
        model, operation("format_property", "delete", address=owner + "/Свойство/Описание2")
    )
    assert format_package_hash(model.format_packages[0]) == before_hash


@pytest.mark.parametrize(
    "patch",
    [
        {"name": "Пустой", "role": "object"},
        {"name": OBJECT, "role": "value"},
        {"name": "Ошибка", "role": "value", "base": {"namespace": URI, "local": "НетТипа"}},
    ],
)
def test_invalid_type_is_atomic(patch):
    model = manager()
    planned = preview(
        model,
        (parse_operation(operation("format_type", "create", owner_id="package-C", patch=patch)),),
        expected_revision=model.revision,
        format_schema=package_c().base_schema,
    )
    assert [f.reason for f in planned.failures] == ["format_package_invalid"]
    assert planned.model is model


def test_schema_required_and_empty_package_allowed():
    model = manager(packages=False)
    op = parse_operation(
        operation(
            "format_package",
            "create",
            patch={"metadata_name": "fmt_Empty", "namespace": "urn:fiction:empty"},
        )
    )
    planned = preview(model, (op,), expected_revision=model.revision)
    assert [f.reason for f in planned.failures] == ["format_schema_required"]
    updated = execute_format(
        model,
        operation(
            "format_package",
            "create",
            patch={"metadata_name": "fmt_Empty", "namespace": "urn:fiction:empty"},
        ),
    )
    assert updated.format_packages[0].types == ()


def test_unused_property_and_package_delete_change_hash():
    model = manager()
    previous = format_package_hash(model.format_packages[0])
    model = execute_format(
        model,
        operation(
            "format_property",
            "delete",
            address="Пакет/fmt_УчебныйПакетC/Тип/" + OBJECT + "/Свойство/Родитель",
        ),
    )
    assert format_package_hash(model.format_packages[0]) != previous
    model = execute_format(
        model, operation("format_package", "delete", address="Пакет/fmt_УчебныйПакетC")
    )
    assert model.format_packages == ()


def test_explicit_null_changes_operation_hash_and_result():
    model = manager()
    address = "Пакет/fmt_УчебныйПакетC/Тип/" + OBJECT + "/Свойство/Родитель"
    payload = operation("format_property", "update", address=address, patch={"lower": 0})
    first = parse_operation(payload)
    second = parse_operation({**payload, "patch": {"lower": 0, "upper": None}})
    schema = package_c().base_schema
    one = preview(model, (first,), expected_revision=model.revision, format_schema=schema)
    two = preview(model, (second,), expected_revision=model.revision, format_schema=schema)
    assert not one.failures and not two.failures
    assert one.operations[0].operation_hash != two.operations[0].operation_hash
    assert one.future_revision != two.future_revision


def test_move_rejects_equal_property_anchor_from_other_owner():
    from kd_rules_mcp.ed.format_model import FormatType
    from kd_rules_mcp.ed.schema import QName

    model = manager()
    package = model.format_packages[0]
    source_type = package.types[2]
    other = FormatType(QName(URI, "ДругойКлюч"), "key", source_type.properties)
    model = replace(
        model, format_packages=(replace(package, types=(*package.types, other)),)
    ).with_revision()
    payload = operation(
        "format_property",
        "move",
        address="Пакет/fmt_УчебныйПакетC/Тип/КлючЗадачи/Свойство/Наименование",
        after_id={"address": "Пакет/fmt_УчебныйПакетC/Тип/ДругойКлюч/Свойство/Ссылка"},
    )
    planned = preview(
        model,
        (parse_operation(payload),),
        expected_revision=model.revision,
        format_schema=package_c().base_schema,
    )
    assert [f.reason for f in planned.failures] == ["format_package_invalid"]
    assert planned.model is model


def test_workspace_empty_snapshot_uses_previous_schema(tmp_path):
    from kd_rules_mcp.authoring.ed.workspace import ManagerWorkspace
    from kd_rules_mcp.ed.writer_model import EMPTY_SNAPSHOT_SCHEMA_SHA256, SNAPSHOT_SCHEMA_SHA256

    workspace = ManagerWorkspace(tmp_path)
    model = manager(packages=False)
    workspace.create(model)
    saved = json.loads((workspace.directory / model.project_id / "manager.ed.json").read_bytes())
    assert saved["schema_sha256"] == EMPTY_SNAPSHOT_SCHEMA_SHA256
    other = replace(manager(), project_id="with-package").with_revision()
    workspace.create(other)
    saved = json.loads((workspace.directory / other.project_id / "manager.ed.json").read_bytes())
    assert saved["schema_sha256"] == SNAPSHOT_SCHEMA_SHA256
