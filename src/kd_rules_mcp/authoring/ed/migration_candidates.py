"""Подбор черновика поверх общего подбора писателя, без выбора за агента."""

from __future__ import annotations

import sqlite3
from dataclasses import replace
from typing import Any

from kd_rules_mcp.authoring.ed import manager_candidates as candidates
from kd_rules_mcp.ed.model import EdDocument, PredefinedRule
from kd_rules_mcp.ed.schema.model import EdSchema, SchemaType
from kd_rules_mcp.ed.schema.profile import Applicability, ValidationProfile
from kd_rules_mcp.ed.schema.resolver import table_row
from kd_rules_mcp.ed.writer_model import digest
from kd_rules_mcp.kd2.model import object_name
from kd_rules_mcp.validation.ed_structure_snapshot import (
    StructureObject,
    StructureSnapshot,
    metadata_key,
)


class CandidateContext:
    def __init__(
        self,
        structure: sqlite3.Connection,
        schema: EdSchema,
        reference: EdDocument | dict[str, EdDocument] | None,
    ):
        self.structure = structure
        self.schema = schema
        self.reference = reference
        self.snapshot = StructureSnapshot.load(structure)
        self.objects: dict[str, list[dict[str, Any]]] = {}
        self.properties: dict[tuple, list[dict[str, Any]]] = {}

    def reference_for(self, role: str) -> EdDocument | None:
        return self.reference.get(role) if isinstance(self.reference, dict) else self.reference

    def host_object(self, name: str) -> StructureObject | None:
        head, sep, tail = object_name(name).partition(".")
        return self.snapshot.objects.get((head, tail)) if sep else None

    def resolve_type(self, name: str, enumeration: bool = False) -> SchemaType | None:
        namespace = ""
        if name.startswith("{") and "}" in name:
            namespace, name = name[1:].split("}", 1)
        found = [
            t
            for q, t in self.schema.types.items()
            if q.local.casefold() == name.casefold()
            and (
                q.namespace == namespace
                if namespace
                else q.namespace in (self.schema.base_namespace, *self.schema.extension_namespaces)
            )
            and (
                bool(candidates._enumeration_values(self.schema, t))
                if enumeration
                else t.kind == "object"
            )
        ]
        base = [t for t in found if t.qname and t.qname.namespace == self.schema.base_namespace]
        if not namespace and len(base) == 1:
            return base[0]
        return found[0] if len(found) == 1 else None

    def type_name(self, typ: SchemaType) -> str:
        assert typ.qname is not None
        return (
            typ.qname.local if typ.qname.namespace == self.schema.base_namespace else str(typ.qname)
        )

    def object_rows(self, host: StructureObject, role: str, enumeration: bool) -> list[dict]:
        if role not in self.objects:
            rows = []
            page = candidates.object_candidates(
                self.structure, self.schema, direction=role, limit=200
            )
            while True:
                rows.extend(page["items"])
                if not page["has_more"]:
                    break
                page = candidates.object_candidates(
                    self.structure,
                    self.schema,
                    direction=role,
                    offset=page["offset"] + len(page["items"]),
                    limit=200,
                )
            self.objects[role] = rows
        full = f"{host.kind}.{host.name}"
        rows = [
            r for r in self.objects[role] if object_name(r["configuration"]) == object_name(full)
        ]
        rows = [
            {**r, "format_type": "{" + r["namespace"] + "}" + r["format_type"]}
            if r.get("namespace") and r["namespace"] != self.schema.base_namespace
            else r
            for r in rows
        ]
        if enumeration:
            rows = [
                {
                    "format_type": self.type_name(t),
                    "confidence": "точно",
                    "reason": "совпадает имя",
                    "auto": False,
                }
                for q, t in self.schema.types.items()
                if q.local.casefold().removeprefix("перечисление.") == host.name.casefold()
                and self.resolve_type(str(q), True) is not None
            ]
        reference = self.reference_for(role)
        if reference:
            profile = ValidationProfile.build(self.schema, None, role)
            applicability = Applicability.build(reference, profile)
            rules = reference.pkpd if enumeration else reference.pko
            for rule in rules:
                opposite = applicability.evaluate(rule, role) is False
                key, _ = metadata_key(
                    rule.configuration_type.value
                    if isinstance(rule, PredefinedRule)
                    else rule.configuration_object.value
                )
                field = rule.format_type if isinstance(rule, PredefinedRule) else rule.format_object
                if key != (host.kind.casefold(), host.name.casefold()) or not field.value:
                    continue
                typ = self.resolve_type(field.value, enumeration)
                if typ:
                    rows.append(
                        {
                            "format_type": self.type_name(typ),
                            "confidence": "reference_reverse" if opposite else "reference",
                            "reason": "в типовом — для другого направления"
                            if opposite
                            else "так в типовом модуле",
                            "auto": False,
                        }
                    )
        return unique(rows, "format_type")

    def property_rows(
        self,
        host: StructureObject,
        typ: SchemaType,
        role: str,
        *,
        table: str = "",
        row: SchemaType | None = None,
        groups: bool = False,
    ) -> list[dict]:
        key = (host.id, typ.id, role, table, row.id if row else "", groups)
        if key in self.properties:
            return self.properties[key]
        if row is None:
            rows = []
            offset = 0
            while True:
                page = candidates.property_candidates(
                    self.structure,
                    self.schema,
                    f"{host.kind}.{host.name}",
                    self.type_name(typ),
                    direction=role,
                    offset=offset,
                    limit=200,
                    reference_document=self.reference_for(role),
                )
                section = "table_parts" if groups else "properties"
                rows.extend(page[section]["items"])
                if not page[section]["has_more"]:
                    break
                offset += 200
            reference = self.reference_for(role)
            profile = ValidationProfile.build(self.schema, None, role)
            applicability = Applicability.build(reference, profile) if reference else None
            reverse_needed = reference is not None and any(
                metadata_key(r.configuration_object.value)[0]
                == (host.kind.casefold(), host.name.casefold())
                and self.resolve_type(r.format_object.value or "") == typ
                and applicability is not None
                and applicability.evaluate(r, role) is False
                for r in reference.pko
            )
            if reference is not None and reverse_needed and not groups:
                offset = 0
                while True:
                    page = candidates.property_candidates(
                        self.structure,
                        self.schema,
                        f"{host.kind}.{host.name}",
                        self.type_name(typ),
                        direction="receive" if role == "send" else "send",
                        offset=offset,
                        limit=200,
                        reference_document=reference,
                    )
                    rows.extend(
                        {
                            **r,
                            "confidence": "reference_reverse",
                            "reason": "в типовом — для другого направления",
                        }
                        for r in page["properties"]["items"]
                        if r.get("class") == "reference_module" or r.get("references")
                    )
                    if not page["properties"]["has_more"]:
                        break
                    offset += 200
        else:
            # Общий классификатор писателя применяется к полям одной конкретной строки.
            # Никакого поиска листового имени среди всех табличных частей.
            attributes = [
                candidates._Attribute(replace(p, path=p.path[len(table) + 1 :], parent_kind=""), "")
                for group in host.properties.values()
                for p in group
                if p.path.casefold().startswith(table.casefold() + ".")
                and "." not in p.path[len(table) + 1 :]
            ]
            header, _ = candidates._format_properties(self.schema, row)
            rows, _, _ = candidates._match_properties(
                digest(key),
                f"{host.kind}.{host.name}",
                self.type_name(typ),
                role,
                self.schema,
                attributes,
                [p for p in header if not p.wrapper and not p.nested],
            )
        rows.extend(self.reference_table_rows(host, typ, role, table=table, row=row, groups=groups))
        self.properties[key] = rows
        return rows

    def reference_table_rows(self, host, typ, role, *, table, row, groups) -> list[dict]:
        """Направленные декларации групп/строк, которых нет у подбора шапки."""
        reference = self.reference_for(role)
        if reference is None or (not groups and row is None):
            return []
        profile = ValidationProfile.build(self.schema, None, role)
        applicability = Applicability.build(reference, profile)
        result = []
        for rule in reference.pko:
            key, _ = metadata_key(rule.configuration_object.value)
            if key != (host.kind.casefold(), host.name.casefold()) or not rule.format_object.value:
                continue
            target = self.resolve_type(rule.format_object.value)
            if target is None or target.id != typ.id:
                continue
            opposite = applicability.evaluate(rule, role) is False
            for group in rule.groups:
                fields = self.format_fields(typ if groups else row, groups=groups)
                if not groups:
                    expected_row = self.row_type(typ, group.format_property)
                    if (
                        group.configuration_property.casefold() != table.casefold()
                        or expected_row is None
                        or expected_row.id != row.id
                    ):
                        continue
                for prop in (group,) if groups else group.properties:
                    path = (
                        prop.configuration_property
                        if groups
                        else table + "." + prop.configuration_property
                    )
                    attrs = host.property(path, table=not groups)
                    if len(attrs) != 1 or (groups and attrs[0].kind != "ТабличнаяЧасть"):
                        continue
                    for field in fields:
                        if prop.format_property.casefold() not in (
                            field.leaf.casefold(),
                            field.path.casefold(),
                        ) or (prop.namespace and prop.namespace != field.namespace):
                            continue
                        classified = (
                            candidates._classify(self.schema, profile, field.prop, attrs[0], role)
                            if not groups
                            else None
                        )
                        result.append(
                            {
                                "configuration": attrs[0].path
                                if groups
                                else attrs[0].path[len(table) + 1 :],
                                "format_path": field.path,
                                "format_name": field.leaf,
                                "namespace": field.namespace,
                                "class": classified.match if classified else "table_part",
                                "confidence": "reference_reverse" if opposite else "reference",
                                "reason": "в типовом — для другого направления"
                                if opposite
                                else "так в типовом модуле",
                                "auto": False,
                            }
                        )
        return result

    def format_fields(self, typ: SchemaType, *, groups: bool = False) -> list:
        header, tables = candidates._format_properties(self.schema, typ)
        return [p for p in (tables if groups else header) if not p.wrapper and not p.nested]

    def enum_rows(
        self, host: StructureObject, typ: SchemaType, value: str, role: str
    ) -> list[dict]:
        literals = candidates._enumeration_values(self.schema, typ) or ()
        rows = [
            {"format_value": f, "auto": False, "reason": "совпадает имя"}
            for f in literals
            if f.casefold() == value.casefold()
        ]
        reference = self.reference_for(role)
        if reference:
            for rule in reference.pkpd:
                key, _ = metadata_key(rule.configuration_type.value)
                if (
                    key != (host.kind.casefold(), host.name.casefold())
                    or not rule.format_type.value
                ):
                    continue
                reference_type = self.resolve_type(rule.format_type.value, True)
                if reference_type is None or reference_type.id != typ.id:
                    continue
                for mapping in rule.mappings:
                    parts = mapping.configuration_value.reference_parts
                    literal = mapping.format_value.literal_value
                    if (
                        mapping.direction == role
                        and len(parts) == 3
                        and parts[1].casefold() == host.name.casefold()
                        and parts[2].casefold() == value.casefold()
                        and literal in literals
                    ):
                        rows.append(
                            {"format_value": literal, "auto": False, "reason": "так в типовом ПКПД"}
                        )
        return unique(rows, "format_value")

    def row_type(self, typ: SchemaType, name: str) -> SchemaType | None:
        found = [
            p
            for p in self.format_fields(typ, groups=True)
            if name.casefold() in (p.leaf.casefold(), p.path.casefold())
        ]
        return table_row(self.schema, found[0].prop) if len(found) == 1 else None


def candidate_rank(row: dict) -> tuple[int, bool]:
    text = str(row.get("reason", "")) + " " + str(row.get("confidence", ""))
    return (
        0
        if "совпадает имя" in text or row.get("confidence") == "точно"
        else 1
        if "типовом" in text or "reference" in text
        else 2
        if "синоним" in text
        else 3,
        row.get("confidence") == "reference_reverse",
    )


def unique(rows: list[dict], key: str, *, default_namespace: str = "") -> list[dict]:
    result: dict[object, dict] = {}
    namespaces: dict[str, set[str]] = {}
    for row in rows:
        if row.get("namespace"):
            namespaces.setdefault(str(row.get(key, "")).casefold(), set()).add(row["namespace"])
    for row in sorted(rows, key=candidate_rank):
        # Один путь, предложенный по имени и типовым модулем, остаётся одним кандидатом.
        path = str(row.get(key, "")).casefold()
        namespace = row.get("namespace") or default_namespace
        if not namespace and len(namespaces.get(path, ())) == 1:
            namespace = next(iter(namespaces[path]))
        ident = (path, namespace) if key == "format_path" else row.get(key)
        result.setdefault(ident, {**row, "auto": False})
    return sorted(result.values(), key=lambda r: (*candidate_rank(r), str(r.get(key, ""))))
