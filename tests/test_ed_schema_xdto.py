"""Синтетические формы XDTO, безопасность XML и явные ограничения."""

from dataclasses import FrozenInstanceError
from pathlib import Path
from typing import Any, cast

import pytest

from kd2_rules_mcp.ed.schema import load_schema, xdto
from kd2_rules_mcp.ed.schema.errors import EdSchemaFormatError, EdSchemaResourceLimitError
from kd2_rules_mcp.ed.schema.model import QName

DATA = Path(__file__).parent / "data/ed/schema"


def test_inline_and_explicit_attributes():
    package = xdto.read_package(DATA / "inline.bin")
    assert len(package.types) == 3
    assert sum(t.qname is None for t in package.types) == 2
    assert package.counts["typeDef"] == 2
    for typ in package.types:
        assert typ.origin[-1].span.line > 0
        assert typ.id.endswith(typ.origin[-1].span.xpath)
    prop = package.types[-1].properties[0]
    assert prop.lower == 1 and prop.upper == 1 and not prop.nillable
    assert "lowerBound" not in prop.explicit_attributes
    with pytest.raises(FrozenInstanceError):
        cast(Any, prop).lower = 2
    with pytest.raises(TypeError):
        cast(Any, package.counts)["typeDef"] = 0


def test_facets_members_and_enumerations():
    package = xdto.read_package(DATA / "facets.bin")
    assert [f.lexical for f in package.types[1].facets if f.kind == "pattern"] == [
        "[a-z]+",
        "[A-Z]+",
    ]
    assert {f.kind for f in package.types[0].facets} == {"totalDigits", "fractionDigits"}
    assert package.types[2].members == (QName(xdto.XS, "string"), QName(xdto.XS, "decimal"))
    base = xdto.read_package(DATA / "base.bin")
    assert [f.lexical for f in base.types[-1].facets] == ["A", "", "A"]
    assert base.types[3].properties[0].upper is None
    message = xdto.read_package(DATA / "message.bin")
    assert message.types[1].properties[0].upper == 100


def test_broken_qname_partial():
    package = xdto.read_package(DATA / "broken-qname.bin")
    assert package.types[0].status == "partial"
    assert package.types[0].properties[0].status == "partial"
    assert package.diagnostics[0].code == "unknown_qname"


@pytest.mark.parametrize(
    "xml",
    [
        '<!DOCTYPE package [<!ENTITY x "value">]><package/>',
        '<!DOCTYPE package SYSTEM "https://example.invalid/schema"><package/>',
        '<!DOCTYPE package [<!ENTITY x SYSTEM "file:///secret">]><package>&x;</package>',
        '<package xmlns:xi="http://www.w3.org/2001/XInclude"><xi:include href="https://example.invalid"/></package>',
        "<broken>",
    ],
)
def test_unsafe_or_broken_xml(tmp_path, xml):
    path = tmp_path / "bad.bin"
    path.write_text(xml, encoding="utf-8")
    with pytest.raises(EdSchemaFormatError):
        xdto.read_package(path)


def test_limits(tmp_path, monkeypatch):
    monkeypatch.setattr(xdto, "MAX_FILE_BYTES", 10)
    with pytest.raises(EdSchemaResourceLimitError):
        xdto.read_package(DATA / "base.bin")
    monkeypatch.setattr(xdto, "MAX_FILE_BYTES", 100000)
    monkeypatch.setattr(xdto, "MAX_XML_DEPTH", 2)
    with pytest.raises(EdSchemaResourceLimitError):
        xdto.read_package(DATA / "inline.bin")


def test_xsd_and_metadata(tmp_path):
    path = tmp_path / "input.xsd"
    path.write_text("<schema/>", encoding="utf-8")
    with pytest.raises(EdSchemaFormatError, match="XSD не поддерживается"):
        xdto.read_package(path)
    name, uri, revision, source = xdto.metadata(DATA / "package-metadata.xml")
    assert (name, uri, revision) == ("TestPackage", "urn:test:base", "1.2.3")
    assert source.kind == "metadata"


def test_element_nsmap_and_repeated_prefix(tmp_path):
    path = tmp_path / "prefix.bin"
    path.write_text(
        '<package xmlns="http://v8.1c.ru/8.1/xdto" xmlns:t="urn:test:first" '
        'targetNamespace="urn:test:prefix"><objectType name="Item" base="t:Parent">'
        '<property xmlns:t="urn:test:second" name="Value" type="t:Type"/>'
        "</objectType></package>",
        encoding="utf-8",
    )
    typ = xdto.read_package(path).types[0]
    assert typ.base == QName("urn:test:first", "Parent")
    assert typ.properties[0].type_ref == QName("urn:test:second", "Type")
    path.write_text(
        path.read_text(encoding="utf-8").replace("xmlns:t=", "xmlns:r=").replace('="t:', '="r:'),
        encoding="utf-8",
    )
    renamed = xdto.read_package(path).types[0]
    assert renamed.base == typ.base and renamed.properties[0].type_ref == typ.properties[0].type_ref


def test_ambiguous_type_and_unknown_attributes(tmp_path):
    path = tmp_path / "unknown.bin"
    path.write_text(
        '<package xmlns="http://v8.1c.ru/8.1/xdto" '
        'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" '
        'xmlns:xs="http://www.w3.org/2001/XMLSchema" targetNamespace="urn:test:partial">'
        '<objectType name="Item" unknown="raw"><property name="Value" type="xs:string" '
        'form="unexpected"><typeDef xsi:type="ValueType" base="xs:string"/></property>'
        '<unexpected raw="value"/></objectType></package>',
        encoding="utf-8",
    )
    package = xdto.read_package(path)
    assert package.types[-1].status == "partial"
    assert package.types[-1].properties[0].status == "partial"
    assert {d.code for d in package.diagnostics} == {
        "unsupported_attribute",
        "ambiguous_property_type",
        "unsupported_form",
        "unsupported_node",
    }
    assert any("raw" in d.message for d in package.diagnostics)


def test_capitalized_property_form_is_complete(tmp_path):
    path = tmp_path / "form.bin"
    path.write_text(
        '<package xmlns="http://v8.1c.ru/8.1/xdto" '
        'xmlns:xs="http://www.w3.org/2001/XMLSchema" targetNamespace="urn:test:form">'
        '<objectType name="Item">'
        '<property name="Code" type="xs:string" form="Attribute"/>'
        '<property name="Name" type="xs:string" form="Element"/>'
        '<property name="Body" type="xs:string" form="Text"/>'
        '<property name="Note" type="xs:string" form="attribute"/>'
        "</objectType></package>",
        encoding="utf-8",
    )
    package = xdto.read_package(path)
    assert [prop.form for prop in package.types[0].properties] == [
        "attribute",
        "element",
        "text",
        "attribute",
    ]
    assert package.types[0].status == "complete"
    assert package.diagnostics == ()
    schema = load_schema(path)
    assert schema.status == "complete"


def test_metadata_rejects_path_components(tmp_path):
    path = tmp_path / "description.xml"
    path.write_text(
        (DATA / "package-metadata.xml")
        .read_text(encoding="utf-8")
        .replace("TestPackage", "../Outside"),
        encoding="utf-8",
    )
    with pytest.raises(EdSchemaFormatError):
        xdto.metadata(path)
