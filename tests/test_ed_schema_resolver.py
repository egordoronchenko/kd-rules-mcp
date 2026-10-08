"""Граф зависимостей и эффективные пути без произвольного выбора кандидатов."""

from pathlib import Path
from typing import Any, cast

import pytest

from kd_rules_mcp.ed.schema import QName, load_schema, resolve_property, resolver
from kd_rules_mcp.ed.schema.errors import (
    EdSchemaConflictError,
    EdSchemaProfileMismatchError,
    EdSchemaResourceLimitError,
)

DATA = Path(__file__).parent / "data/ed/schema"


def base():
    return load_schema(DATA / "base.bin", locate_import=lambda _: DATA / "message.bin")


def test_effective_paths_and_reference():
    schema = base()
    owner = QName("urn:test:base", "Item")
    key = resolve_property(schema, owner, "Code")
    assert key.status == "resolved"
    assert [q.local for q in key.physical_paths[0]] == ["КлючевыеСвойства", "Code"]
    assert resolve_property(schema, owner, "Title").status == "resolved"
    row = resolve_property(schema, owner, "Quantity", table="Lines")
    assert row.status == "resolved"
    assert [q.local for q in row.physical_paths[0]] == ["Lines", "Строка", "Quantity"]
    assert resolve_property(schema, owner, "Data.Title").status == "resolved"
    assert resolve_property(schema, owner, "Missing").status == "missing"
    assert resolve_property(schema, owner, " .Code").status == "unknown"
    assert resolver.is_reference(schema, schema.types[QName("urn:test:base", "ItemRef")])
    inherited = resolve_property(schema, owner, "AdditionalInfo")
    assert any(step.role == "inheritance" for step in inherited.origin)
    assert base() == schema
    with pytest.raises(TypeError):
        cast(Any, schema.types)[owner] = schema.types[owner]


def test_missing_import_and_cycle():
    schema = load_schema(DATA / "base.bin")
    assert schema.status == "partial"
    assert any(d.code == "unresolved_import" for d in schema.diagnostics)
    assert resolve_property(schema, QName("urn:test:base", "Item"), "Code").status == "resolved"
    assert resolve_property(schema, QName("urn:test:base", "Item"), "Absent").status == "unknown"
    paths = {"urn:test:a": DATA / "cycle-a.bin", "urn:test:b": DATA / "cycle-b.bin"}
    cyclic = load_schema(paths["urn:test:a"], locate_import=paths.get)
    assert len(cyclic.packages) == 2
    assert any(d.code == "inheritance_cycle" for d in cyclic.diagnostics)
    assert all(t.status == "partial" for t in cyclic.types.values())


def test_extensions_and_conflicts():
    schema = load_schema(
        DATA / "base.bin",
        extensions=(DATA / "extension.bin",),
        locate_import=lambda uri: DATA / "message.bin" if uri == "urn:test:message" else None,
    )
    assert schema.extension_namespaces == ("urn:test:extension",)
    assert QName("urn:test:base", "Item") in schema.types
    assert QName("urn:test:extension", "Item") in schema.types
    with pytest.raises(EdSchemaProfileMismatchError):
        load_schema(DATA / "base.bin", extensions=(DATA / "message.bin",))
    with pytest.raises(EdSchemaConflictError):
        load_schema(DATA / "conflict.bin")


@pytest.mark.parametrize(
    "limit", ["MAX_TOTAL_BYTES", "MAX_FILES", "MAX_TYPES", "MAX_PROPERTIES", "MAX_RESOLUTION_DEPTH"]
)
def test_limits(monkeypatch, limit):
    monkeypatch.setattr(resolver, limit, 1)
    with pytest.raises(EdSchemaResourceLimitError):
        base()


def test_ambiguous_wrappers_and_inline(tmp_path):
    path = tmp_path / "ambiguous.bin"
    path.write_text(
        """<package xmlns="http://v8.1c.ru/8.1/xdto" xmlns:t="urn:test:collision"
        xmlns:xs="http://www.w3.org/2001/XMLSchema" targetNamespace="urn:test:collision">
      <objectType name="ОбщиеСвойства"><property name="Name" type="xs:string"/></objectType>
      <objectType name="Item"><property name="First" type="t:ОбщиеСвойства"/>
      <property name="Second" type="t:ОбщиеСвойства"/></objectType>
    </package>""",
        encoding="utf-8",
    )
    result = resolve_property(load_schema(path), QName("urn:test:collision", "Item"), "Name")
    assert result.status == "ambiguous" and len(result.physical_paths) == 2
    schema = load_schema(DATA / "inline.bin")
    assert (
        resolve_property(schema, QName("urn:test:inline", "Envelope"), "Local.Value").status
        == "resolved"
    )


def test_transitive_import_without_inheritance_cycle(tmp_path):
    paths = {}
    for name, target in (("a", "b"), ("b", "c"), ("c", "a")):
        path = tmp_path / f"{name}.bin"
        path.write_text(
            '<package xmlns="http://v8.1c.ru/8.1/xdto" '
            f'targetNamespace="urn:test:{name}"><import namespace="urn:test:{target}"/>'
            '<objectType name="Same"/></package>',
            encoding="utf-8",
        )
        paths[f"urn:test:{name}"] = path
    schema = load_schema(paths["urn:test:a"], locate_import=paths.get)
    assert schema.status == "complete" and len(schema.packages) == 3
    assert len(schema.types) == 3
    graph = schema.types[QName("urn:test:c", "Same")].origin
    assert [s.namespace for s in graph if s.role == "import"][:2] == ["urn:test:b", "urn:test:c"]
    assert schema.extension_namespaces == ()


def test_import_does_not_activate_extension(tmp_path):
    path = tmp_path / "base.bin"
    path.write_text(
        (DATA / "base.bin")
        .read_text(encoding="utf-8")
        .replace(
            '<import namespace="urn:test:message"/>',
            '<import namespace="urn:test:message"/><import namespace="urn:test:extension"/>',
        ),
        encoding="utf-8",
    )
    schema = load_schema(
        path,
        locate_import={
            "urn:test:extension": DATA / "extension.bin",
            "urn:test:message": DATA / "message.bin",
        }.get,
    )
    assert schema.extension_namespaces == ()
    assert schema.status == "complete"


def test_conflict_across_imports_and_identical_definitions(tmp_path):
    path = tmp_path / "same.bin"
    path.write_text(
        '<package xmlns="http://v8.1c.ru/8.1/xdto" '
        'targetNamespace="urn:test:base"><objectType name="Item"/></package>',
        encoding="utf-8",
    )
    base_path = tmp_path / "base.bin"
    base_path.write_text(
        (DATA / "base.bin")
        .read_text(encoding="utf-8")
        .replace(
            '<import namespace="urn:test:message"/>',
            '<import namespace="urn:test:base"/>',
        ),
        encoding="utf-8",
    )
    with pytest.raises(EdSchemaConflictError):
        load_schema(base_path, locate_import=lambda _: path)
    path.write_text(
        '<package xmlns="http://v8.1c.ru/8.1/xdto" '
        'targetNamespace="urn:test:same"><objectType name="Item"/>'
        '<objectType name="Item"/></package>',
        encoding="utf-8",
    )
    assert len(load_schema(path).types) == 1
