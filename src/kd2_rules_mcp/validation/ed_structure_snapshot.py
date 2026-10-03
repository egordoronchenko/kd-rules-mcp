"""Неизменяемый DTO и общий контекст проверок ED; нового формата хранения нет."""

import sqlite3
from collections import Counter, defaultdict
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

from kd2_rules_mcp.ed import model as ed
from kd2_rules_mcp.ed.address import AddressIndex
from kd2_rules_mcp.ed.model import Expr
from kd2_rules_mcp.ed.schema.profile import Applicability, ValidationProfile
from kd2_rules_mcp.structures.xmldump import KINDS
from kd2_rules_mcp.validation.report import ValidationReport

# Явные коллекции BSL; структура хранит виды в единственном числе.
COLLECTIONS = MappingProxyType({item[2].casefold(): item[1] for item in KINDS.values()})

# Эти стандартные реквизиты не обязательно входят в выгрузку структуры.
# Отсутствие строки структуры не доказывает отсутствия реквизита платформы.
REFERENCE_ATTRIBUTES = frozenset({"ссылка", "версияданных", "пометкаудаления"})
PREDEFINED_ATTRIBUTES = REFERENCE_ATTRIBUTES | {"предопределенный", "имяпредопределенныхданных"}
STANDARD_ATTRIBUTES = MappingProxyType(
    {
        "справочник": PREDEFINED_ATTRIBUTES,
        "документ": REFERENCE_ATTRIBUTES,
        "планвидовхарактеристик": PREDEFINED_ATTRIBUTES,
        "плансчетов": PREDEFINED_ATTRIBUTES,
        "планвидоврасчета": PREDEFINED_ATTRIBUTES,
        "планобмена": REFERENCE_ATTRIBUTES,
        "бизнеспроцесс": REFERENCE_ATTRIBUTES,
        "задача": REFERENCE_ATTRIBUTES,
    }
)


def standard_attribute(owner: "StructureObject", path: str, table: bool = False) -> bool:
    name = path.casefold()
    if table:
        return name.rsplit(".", 1)[-1] == "номерстроки"
    return name in STANDARD_ATTRIBUTES.get(owner.kind.casefold(), ())


def metadata_key(expression: Expr | None) -> tuple[tuple[str, str] | None, str]:
    """Неопределено — источник-структура (XDTO:1461–1469)."""
    if expression is None:
        return None, "configuration_object_unavailable"
    if expression.raw.strip().casefold() == "неопределено":
        return None, "structure_source"
    parts = expression.reference_parts
    if len(parts) != 3 or parts[0].casefold() != "метаданные":
        return None, "dynamic_configuration_object"
    kind = COLLECTIONS.get(parts[1].casefold())
    return (
        ((kind.casefold(), parts[2].casefold()), "resolved")
        if kind
        else (None, "unknown_collection")
    )


@dataclass(frozen=True, slots=True)
class StructureProperty:
    id: int
    path: str
    kind: str
    parent_kind: str
    types: tuple[str, ...]
    unresolved: tuple[str, ...]
    qualifiers: Mapping[str, str | int]


@dataclass(frozen=True, slots=True)
class StructureObject:
    id: int
    kind: str
    name: str
    type_name: str
    properties: Mapping[tuple[str, str], tuple[StructureProperty, ...]]
    values: frozenset[str]

    def property(self, path: str, table: bool = False) -> tuple[StructureProperty, ...]:
        return self.properties.get((path.casefold(), "ТабличнаяЧасть" if table else ""), ())


@dataclass(frozen=True, slots=True)
class StructureSnapshot:
    objects: Mapping[tuple[str, str], StructureObject]
    by_type: Mapping[str, StructureObject]

    @classmethod
    def load(cls, connection: sqlite3.Connection) -> "StructureSnapshot":
        """Считывает все свойства пакетно; qualifiers и unresolved не теряются."""
        columns = [row[1] for row in connection.execute("PRAGMA table_info(properties)")]
        properties = [
            dict(zip(columns, row, strict=True))
            for row in connection.execute("SELECT * FROM properties")
        ]
        parents = {p["id"]: p for p in properties}
        sets = {
            ident: tuple(types.splitlines())
            for ident, types in connection.execute("SELECT id, types FROM type_sets")
        }
        grouped: dict[int, dict[tuple[str, str], list[StructureProperty]]] = {}
        qualifier_names = (
            "number_length",
            "number_precision",
            "number_nonnegative",
            "string_length",
            "string_fixed",
            "date_parts",
            "usage",
            "indexing",
            "autoregistration",
        )
        for prop in properties:
            parent_kind = parents[prop["parent_id"]]["kind"] if prop["parent_id"] else ""
            row = StructureProperty(
                prop["id"],
                prop["path"],
                prop["kind"],
                parent_kind,
                sets.get(prop["type_set_id"], ()),
                tuple((prop["unresolved"] or "").splitlines()),
                MappingProxyType({k: prop[k] for k in qualifier_names}),
            )
            grouped.setdefault(prop["object_id"], {}).setdefault(
                (prop["path"].casefold(), parent_kind), []
            ).append(row)
        values: dict[int, set[str]] = {}
        for ident, name in connection.execute("SELECT object_id, name FROM object_values"):
            values.setdefault(ident, set()).add(name.casefold())
        objects = {}
        for ident, kind, name, type_name in connection.execute(
            "SELECT id, kind, name, type_name FROM objects WHERE is_group=0"
        ):
            objects[(kind.casefold(), name.casefold())] = StructureObject(
                ident,
                kind,
                name,
                type_name,
                MappingProxyType({k: tuple(v) for k, v in grouped.get(ident, {}).items()}),
                frozenset(values.get(ident, ())),
            )
        return cls(
            MappingProxyType(objects),
            MappingProxyType({o.type_name.casefold(): o for o in objects.values()}),
        )

    def search(
        self, owner: StructureObject, path: str
    ) -> tuple[tuple[StructureProperty, ...], str]:
        """Путь поиска через ссылку — только при единственном разрешённом типе."""
        parts = path.split(".")
        current = owner
        if (
            len(parts) == 2
            and parts[-1].casefold() == "номерстроки"
            and any(p.kind == "ТабличнаяЧасть" for p in owner.property(parts[0]))
        ):
            return (), "standard_attribute"
        for part in parts[:-1]:
            props = current.property(part)
            if not props:
                return (), "standard_attribute" if standard_attribute(current, part) else "missing"
            if len(props) != 1 or props[0].unresolved or len(props[0].types) != 1:
                return (), "unresolved_configuration_type"
            next_owner = self.by_type.get(props[0].types[0].casefold())
            if next_owner is None:
                return (), "unresolved_configuration_type"
            current = next_owner
        found = current.property(parts[-1])
        return found, (
            "resolved"
            if found
            else "standard_attribute"
            if standard_attribute(current, parts[-1])
            else "missing"
        )


class CheckContext:
    """Покрытие и пропуски считают уникальные экземпляры, без повторов направления."""

    def __init__(self, document: ed.EdDocument, index: AddressIndex, profile: ValidationProfile):
        self.report = ValidationReport()
        self.index = index
        self.applicability = Applicability.build(document, profile)
        self.coverage: Counter[str] = Counter()
        self.skips: dict[tuple[str, str], dict[str, str]] = defaultdict(dict)
        self.instance: tuple[str, str] | None = None
        self.covered: dict[str, set[tuple[str, str]]] = defaultdict(set)

    def count(self, key: str) -> None:
        if self.instance is not None and self.instance not in self.covered[key]:
            self.covered[key].add(self.instance)
            self.coverage[key] += 1

    def address(self, entity: ed.Entity) -> str:
        return self.index.by_id[entity.entity_id][0]

    def skip(self, check: str, reason: str, entity: ed.Entity) -> None:
        self.skips[(check, reason)][entity.entity_id] = self.address(entity)
        if reason == "opaque_condition":
            self.count("opaque_conditions")
        elif reason == "handler_may_supply":
            self.count("handler_may_supply")
        elif check.startswith("ed.schema.") and reason in (
            "owner_type_unavailable",
            "partial_schema",
            "ambiguous",
            "not_table",
        ):
            self.count("unresolved_schema")

    def start(
        self, check: str, entity: ed.Entity, direction: str, parent: ed.Entity | None = None
    ) -> bool:
        self.instance = (check, entity.entity_id)
        state = self.applicability.evaluate(entity, direction, parent)
        if state is False:
            self.count("not_applicable")
            return False
        if state is None:
            self.skip(check, "opaque_condition", entity)
            return False
        return True

    def checked(self) -> None:
        self.count("checked")

    def finish(self) -> ValidationReport:
        self.report.issues = list(dict.fromkeys(self.report.issues))
        for (check, reason), instances in sorted(self.skips.items()):
            addresses = list(dict.fromkeys(instances.values()))[:5]
            self.report.skip(check, f"{reason}: {len(instances)}; " + ", ".join(addresses))
        return self.report
