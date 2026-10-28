"""Пакет C: вымышленные задачи, ссылки, группы, строки и перечисление."""

from pathlib import Path

from kd_rules_mcp.authoring.ed.format_package import FormatPackage
from kd_rules_mcp.ed.format_model import FormatFacet, FormatProperty, FormatType
from kd_rules_mcp.ed.schema import QName, load_schema
from kd_rules_mcp.ed.schema.xdto import XS

URI = "urn:fiction:task-format:C"
OBJECT = "Справочник.УчебнаяЗадача"


def package_c(schema=None, version="1.20"):
    schema = schema or load_schema(Path(__file__).parent.parent / "format_package/base.bin")

    def q(local):
        return QName(URI, local)

    def prop(name, typ, **kwargs):
        return FormatProperty(q(name), typ, **kwargs)

    reference = FormatType(q("ЗадачаСсылка"), "reference", base=QName(XS, "string"))
    enumeration = FormatType(
        q("СостояниеЗадачи"),
        "enumeration",
        base=QName(XS, "string"),
        facets=tuple(
            FormatFacet("enumeration", s, QName(XS, "string")) for s in ("Новая", "Завершена")
        ),
    )
    key = FormatType(
        q("КлючЗадачи"),
        "key",
        (
            prop("Ссылка", reference.name),
            prop("Наименование", QName(XS, "string")),
            prop("ЭтоГруппа", QName(XS, "boolean")),
        ),
    )
    row = FormatType(
        q("УчебнаяСтрока"),
        "row",
        (prop("Текст", QName(XS, "string")), prop("Количество", QName(XS, "decimal"))),
    )
    table = FormatType(q("ШагиЗадачи"), "table", (prop("Строка", row.name, lower=0, upper=None),))
    obj = FormatType(
        q(OBJECT),
        "object",
        (
            prop("КлючевыеСвойства", key.name),
            prop("Родитель", key.name, lower=0),
            prop("Шаги", table.name, lower=0),
            prop("Состояние", enumeration.name, lower=0),
        ),
        exported=True,
        key_property=q("КлючевыеСвойства"),
    )
    return FormatPackage(
        "fmt_УчебныйПакетC",
        URI,
        version,
        schema.base_namespace,
        (reference, enumeration, key, row, table, obj),
        schema,
    )
