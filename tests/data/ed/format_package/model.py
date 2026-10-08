"""Вымышленная схема: объект, ключ, ссылка, две колонки, перечисление."""

from pathlib import Path

from kd_rules_mcp.authoring.ed.format_package import (
    FormatFacet,
    FormatPackage,
    FormatProperty,
    FormatType,
)
from kd_rules_mcp.ed.schema import QName, load_schema
from kd_rules_mcp.ed.schema.xdto import XS

OWN = "urn:fiction:extension"
BASE = "urn:fiction:base"


def sample_model() -> FormatPackage:
    def q(local: str) -> QName:
        return QName(OWN, local)

    def prop(name: str, typ: QName, **kwargs) -> FormatProperty:
        return FormatProperty(q(name), typ, **kwargs)

    return FormatPackage(
        "fmt_Package",
        OWN,
        "1.20",
        BASE,
        (
            FormatType(q("ItemRef"), "reference", base=QName(BASE, "Ref")),
            FormatType(
                q("Color"),
                "enumeration",
                base=QName(XS, "string"),
                variety="atomic",
                facets=tuple(
                    FormatFacet("enumeration", s, QName(XS, "string"))
                    for s in ("Red", "Green", "Blue")
                ),
            ),
            FormatType(
                q("ItemKey"),
                "key",
                (prop("Ref", q("ItemRef"), lower=0), prop("Code", QName(XS, "string"))),
                open=True,
                sequenced=True,
            ),
            FormatType(
                q("ItemRowsRow"),
                "row",
                (prop("Text", QName(XS, "string")), prop("Amount", QName(XS, "decimal"), lower=0)),
                sequenced=True,
            ),
            FormatType(
                q("ItemRows"), "table", (prop("Row", q("ItemRowsRow"), lower=0, upper=None),)
            ),
            FormatType(
                q("Item"),
                "object",
                (
                    prop("Key", q("ItemKey")),
                    prop("External", QName(BASE, "ExternalRef"), lower=0, nillable=True),
                    prop("Parent", q("ItemKey"), lower=0),
                    prop("Color", q("Color"), lower=0),
                    prop("Rows", q("ItemRows"), lower=0, upper=1),
                ),
                base=QName(BASE, "Object"),
                open=True,
                sequenced=True,
                exported=True,
                key_property=q("Key"),
            ),
        ),
        load_schema(Path(__file__).with_name("base.bin")),
    )
