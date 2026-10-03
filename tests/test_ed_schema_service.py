"""Контракт снимков схем, страниц, ошибок и выбора конфигурации проекта."""

import shutil
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from kd2_rules_mcp.errors import (
    EdSchemaAmbiguousImportError,
    EdSchemaNotFoundError,
    EdSchemaProfileMismatchError,
    EdSchemaResourceLimitError,
    EdSchemaTypeNotFoundError,
)
from kd2_rules_mcp.service import Kd2Service, Settings
from kd2_rules_mcp.service import ed_schema as service_module

DATA = Path(__file__).parent / "data/ed/schema"


@pytest.fixture
def service(tmp_path):
    return Kd2Service(Settings(cache_dir=tmp_path / "cache", workspace=tmp_path / "workspace"))


def open_base(service, path=None, **kwargs):
    return service.ed_schema_open(
        "1.2",
        path=str(path or DATA / "base.bin"),
        imports={"urn:test:message": str(DATA / "message.bin")},
        **kwargs,
    )


def test_origin_is_opt_in(service):
    ident = open_base(service)["schema_id"]
    short = service.ed_schema_type(ident, "Item")
    assert "origin" not in short and "origin_graph" not in short
    assert all("origin" not in p for p in short["properties"]["items"])
    full = service.ed_schema_type(ident, "Item", include_origin=True)
    assert full["origin"] and full["origin_graph"]
    assert all(p["origin"] for p in full["properties"]["items"])
    for key in ("qname", "kind", "base"):
        assert full[key] == short[key]
    assert full["properties"]["total"] == short["properties"]["total"]


def test_open_pages_and_close(service):
    opened = open_base(service)
    assert opened["status"] == "complete"
    assert not opened["reused"] and not opened["source_changed"]
    assert open_base(service)["reused"]
    ident = opened["schema_id"]
    page = service.ed_schema_types(
        ident, namespace="urn:test:base", kind="object", text="Item", limit=1
    )
    assert page["total"] == 2
    assert page["has_more"]
    assert not service.ed_schema_types(ident, offset=100)["items"]
    detail = service.ed_schema_type(ident, "{urn:test:base}Item", limit=1)
    assert detail["properties"]["has_more"]
    values = service.ed_schema_type(
        ident, "{urn:test:base}Choice", section="values", offset=1, limit=1
    )
    assert values["values"]["items"][0]["lexical"] == ""
    assert service.ed_schema_close(ident)["closed"]
    assert not service.ed_schema_close(ident)["closed"]
    with pytest.raises(EdSchemaNotFoundError):
        service.ed_schema_types(ident)
    assert open_base(service)["schema_id"] == ident


def test_changed_dependency_keeps_snapshot(service, tmp_path):
    path = tmp_path / "base.bin"
    dependency = tmp_path / "message.bin"
    shutil.copyfile(DATA / "base.bin", path)
    shutil.copyfile(DATA / "message.bin", dependency)
    args = dict(format_version="1.2", path=str(path), imports={"urn:test:message": str(dependency)})
    first = service.ed_schema_open(**args)
    old = service.ed_schema_type(first["schema_id"], "{urn:test:base}Item")
    dependency.write_text(
        dependency.read_text(encoding="utf-8").replace("100", "99"), encoding="utf-8"
    )
    second = service.ed_schema_open(**args)
    assert second["reused"] and second["source_changed"]
    assert service.ed_schema_type(first["schema_id"], "{urn:test:base}Item") == old
    service.ed_schema_close(first["schema_id"])
    assert service.ed_schema_open(**args)["schema_id"] != first["schema_id"]


@pytest.mark.parametrize("limit", [0, 201, True])
def test_bad_pages(service, limit):
    ident = open_base(service)["schema_id"]
    with pytest.raises(ValueError):
        service.ed_schema_types(ident, limit=limit)
    with pytest.raises(ValueError):
        service.ed_schema_type(ident, "{urn:test:base}Item", limit=limit)


def test_bad_arguments(service):
    for args in [
        {},
        {"path": str(DATA / "base.bin"), "project": "test"},
        {"project": "test"},
        {"path": str(DATA / "base.bin"), "package": "Test"},
    ]:
        with pytest.raises(ValueError):
            service.ed_schema_open("1.2", **args)
    ident = open_base(service)["schema_id"]
    with pytest.raises(EdSchemaTypeNotFoundError):
        service.ed_schema_type(ident, "{urn:test:base}Absent")
    with pytest.raises(EdSchemaTypeNotFoundError):
        service.ed_schema_type(ident, "Absent")
    with pytest.raises(ValueError):
        service.ed_schema_type(ident, "{urn:test:base}Item", section="unknown")
    with pytest.raises(ValueError):
        service.ed_schema_types(ident, offset=-1)
    with pytest.raises(ValueError):
        service.ed_schema_types(ident, kind="unknown")


def setup_project(tmp_path):
    root = tmp_path / "project"
    packages = root / "main/XDTOPackages"
    packages.mkdir(parents=True)
    shutil.copyfile(DATA / "package-metadata.xml", packages / "TestPackage.xml")
    (packages / "TestPackage/Ext").mkdir(parents=True)
    shutil.copyfile(DATA / "base.bin", packages / "TestPackage/Ext/Package.bin")
    (packages / "Message/Ext").mkdir(parents=True)
    shutil.copyfile(DATA / "message.bin", packages / "Message/Ext/Package.bin")
    text = (DATA / "package-metadata.xml").read_text(encoding="utf-8")
    (packages / "Message.xml").write_text(
        text.replace("TestPackage", "Message").replace("urn:test:base", "urn:test:message"),
        encoding="utf-8",
    )
    catalog = tmp_path / "projects.yaml"
    catalog.write_text(
        "projects:\n  test:\n    name: Test\n"
        "    configurations:\n      full:\n        dump: main\n",
        encoding="utf-8",
    )
    return root, packages, catalog


def test_project_metadata_and_ambiguous_import(tmp_path):
    root, packages, catalog = setup_project(tmp_path)
    service = Kd2Service(
        Settings(
            cache_dir=tmp_path / "cache",
            workspace=tmp_path / "workspace",
            projects_file=catalog,
            project_dirs={"test": root},
        )
    )
    opened = service.ed_schema_open("1.2", project="test", package="TestPackage")
    assert opened["package"] == "TestPackage" and opened["status"] == "complete"
    service.ed_schema_close(opened["schema_id"])
    shutil.copyfile(packages / "Message.xml", packages / "Duplicate.xml")
    with pytest.raises(EdSchemaAmbiguousImportError):
        service.ed_schema_open("1.2", project="test", package="TestPackage")
    opened = service.ed_schema_open(
        "1.2",
        project="test",
        package="TestPackage",
        imports={"urn:test:message": str(packages / "Message.xml")},
    )
    assert opened["status"] == "complete"
    service.ed_schema_close(opened["schema_id"])
    description = packages / "TestPackage.xml"
    description.write_text(
        description.read_text(encoding="utf-8").replace("urn:test:base", "urn:test:other"),
        encoding="utf-8",
    )
    with pytest.raises(EdSchemaProfileMismatchError):
        service.ed_schema_open("1.2", path=str(description))


def test_concurrent_open_and_registry_limits(service, monkeypatch):
    with ThreadPoolExecutor(max_workers=4) as pool:
        responses = list(pool.map(lambda _: open_base(service), range(8)))
    assert len({r["schema_id"] for r in responses}) == 1
    assert sum(not r["reused"] for r in responses) == 1
    monkeypatch.setattr(service_module, "MAX_SCHEMAS", 1)
    with pytest.raises(EdSchemaResourceLimitError):
        service.ed_schema_open("1.2", path=str(DATA / "facets.bin"))
    monkeypatch.setattr(service_module, "MAX_SCHEMAS", 256)
    monkeypatch.setattr(service_module, "MAX_STORED_BYTES", 1)
    with pytest.raises(EdSchemaResourceLimitError):
        service.ed_schema_open("1.2", path=str(DATA / "facets.bin"))


def test_short_type_name_only_in_active_namespaces(service):
    ident = open_base(service)["schema_id"]
    short = service.ed_schema_type(ident, "Item")
    assert short == service.ed_schema_type(ident, "{urn:test:base}Item")
    # Тип импортированного пакета коротким именем не находится: импорт пространство не активирует.
    assert service.ed_schema_type(ident, "{urn:test:message}Object")["kind"] == "object"
    with pytest.raises(EdSchemaTypeNotFoundError):
        service.ed_schema_type(ident, "Object")


def test_local_type_navigation(service):
    ident = service.ed_schema_open("1.2", path=str(DATA / "inline.bin"))["schema_id"]
    envelope = service.ed_schema_type(ident, "{urn:test:inline}Envelope")
    local = envelope["properties"]["items"][0]["type"]
    assert service.ed_schema_type(ident, local)["kind"] == "object"
