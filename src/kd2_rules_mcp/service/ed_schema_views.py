"""Компактные JSON-проекции схемы; XML и полный граф в списки не попадают."""

import json
from collections import Counter
from dataclasses import asdict
from typing import Any

from kd2_rules_mcp.ed.schema.model import EdSchema, OriginStep, SchemaProperty, SchemaType
from kd2_rules_mcp.ed.schema.resolver import (
    effective_properties,
    is_reference,
    property_type,
    table_row,
)
from kd2_rules_mcp.errors import EdSchemaResourceLimitError
from kd2_rules_mcp.service.ed_views import short, validate_page
from kd2_rules_mcp.service.views import slice_rows


def origin_view(origin: tuple[OriginStep, ...]) -> list[dict[str, Any]]:
    return [
        {
            "role": s.role,
            "namespace": s.namespace,
            "span": asdict(s.span),
            "via": str(s.via) if s.via else None,
        }
        for s in dict.fromkeys(origin)
    ]


def counts(schema: EdSchema) -> dict[str, int]:
    result: Counter[str] = Counter()
    for package in schema.packages:
        result.update(package.counts)
    return dict(sorted(result.items()))


def type_row(schema: EdSchema, typ: SchemaType) -> dict[str, Any]:
    return {
        "qname": str(typ.qname) if typ.qname else typ.id,
        "kind": typ.kind,
        "base": str(typ.base) if typ.base else None,
        "property_count": len(schema.inherited[typ.id]),
        "enum_count": sum(f.kind == "enumeration" for f in typ.facets),
        "status": typ.status,
    }


def types_page(
    schema: EdSchema,
    namespace: str | None,
    kind: str | None,
    text: str | None,
    offset: int,
    limit: int,
) -> dict[str, Any]:
    validate_page(offset, limit)
    if kind not in (None, "object", "value"):
        raise ValueError("Вид типа: object или value")
    rows = [
        type_row(schema, t)
        for q, t in sorted(schema.types.items(), key=lambda p: str(p[0]))
        if (namespace is None or q.namespace == namespace)
        and (kind is None or t.kind == kind)
        and (text is None or text.casefold() in str(q).casefold())
    ]
    return slice_rows(rows, offset, limit)


def property_row(
    schema: EdSchema, prop: SchemaProperty, physical: tuple[str, ...]
) -> dict[str, Any]:
    target = property_type(schema, prop)
    return {
        "name": prop.name.local,
        "physical_path": ".".join(physical),
        "effective_path": prop.name.local,
        "type": str(prop.type_ref) if prop.type_ref else prop.type_id,
        "lower": prop.lower,
        "upper": prop.upper,
        "nillable": prop.nillable,
        "form": prop.form,
        "explicit_attributes": sorted(prop.explicit_attributes),
        "status": prop.status,
        "reference": bool(target and is_reference(schema, target)),
        "table_row": ((row.qname and str(row.qname)) or row.id)
        if (row := table_row(schema, prop))
        else None,
        "origin": origin_view(prop.origin),
    }


def type_view(
    schema: EdSchema, typ: SchemaType, section: str, offset: int, limit: int
) -> dict[str, Any]:
    validate_page(offset, limit)
    if section not in ("properties", "values", "facets"):
        raise ValueError("Раздел типа: properties, values или facets")
    result = type_row(schema, typ)
    result.update(
        origin=origin_view(typ.origin),
        open=typ.open,
        abstract=typ.abstract,
        ordered=typ.ordered,
        sequenced=typ.sequenced,
        variety=typ.variety,
        members=[str(q) for q in typ.members],
        explicit_attributes=sorted(typ.explicit_attributes),
    )
    if section == "properties":
        pairs = effective_properties(schema, typ)
        selected = pairs[offset : offset + limit]
        rows = [property_row(schema, p, tuple(q.local for q in path)) for p, path in selected]
        # Происхождение — общий граф страницы, свойства ссылаются на номера его узлов.
        graph: list[dict[str, Any]] = []
        for row in rows:
            indices = []
            for step in row["origin"]:
                if step not in graph:
                    graph.append(step)
                indices.append(graph.index(step))
            row["origin"] = indices
        result["origin_graph"] = graph
        result[section] = {
            "items": rows,
            "total": len(pairs),
            "offset": offset,
            "limit": limit,
            "has_more": offset + len(rows) < len(pairs),
        }
    else:
        facets = [f for f in typ.facets if (f.kind == "enumeration") == (section == "values")]
        page = slice_rows(facets, offset, limit)
        page["items"] = [
            {
                "kind": f.kind,
                "lexical": short(f.lexical),
                "value_type": str(f.value_type) if f.value_type else None,
                "fixed": f.fixed,
                "span": asdict(f.span),
            }
            for f in page["items"]
        ]
        result[section] = page
    # limit задаёт максимум строк; дополнительный бюджет не скрывает оставшиеся строки.
    page = result[section]
    while len(json.dumps(result, ensure_ascii=False).encode("utf-8")) > 20000:
        if len(page["items"]) <= 1:
            raise EdSchemaResourceLimitError("Одна строка схемы превышает бюджет ответа 20 КБ")
        page["items"].pop()
        page["has_more"] = offset + len(page["items"]) < page["total"]
        if section == "properties":
            used = sorted({i for row in page["items"] for i in row["origin"]})
            graph = result["origin_graph"]
            result["origin_graph"] = [graph[i] for i in used]
            for row in page["items"]:
                row["origin"] = [used.index(i) for i in row["origin"]]
    return result
