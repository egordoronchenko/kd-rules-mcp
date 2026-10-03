"""Адреса ED: нейтральные стороны, явные конфликты, без зависимости от КД 2."""

from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from urllib.parse import quote

from .model import EdDocument, Entity, PropertyRule, Routine


class AmbiguousAddressError(LookupError):
    """Неквалифицированному адресу соответствуют несколько сущностей."""

    def __init__(self, address: str, candidates: tuple[str, ...]):
        super().__init__(f"Неоднозначный адрес: {address}")
        self.candidates = candidates


class EntityNotFoundError(LookupError):
    """Сущность по адресу отсутствует в снимке."""


def escape_segment(value: str) -> str:
    """Экранирует только зарезервированные символы, сохраняя прочий Unicode."""
    return "".join(
        ("%7E" if char == "~" else quote(char, safe=""))
        if char in "%/#~" or ord(char) < 32 or ord(char) == 127
        else char
        for char in value
    )


def property_key(format_name: str, configuration_name: str) -> str:
    value = format_name or configuration_name
    return escape_segment(value) if value else "~empty"


@dataclass(frozen=True, slots=True)
class AddressIndex:
    by_address: Mapping[str, Entity]
    by_id: Mapping[str, tuple[str, ...]]
    conflicts: Mapping[str, tuple[str, ...]]

    def find(self, address: str) -> Entity:
        key = address.casefold()
        if key in self.conflicts:
            raise AmbiguousAddressError(address, self.conflicts[key])
        for candidate, entity in self.by_address.items():
            if candidate.casefold() == key:
                return entity
        raise EntityNotFoundError(address)


def build_addresses(document: EdDocument) -> AddressIndex:
    """Сначала квалифицирует родителей, затем локальные адреса дочерних строк."""
    addresses: dict[str, Entity] = {}
    by_id: dict[str, list[str]] = defaultdict(list)
    conflicts: dict[str, tuple[str, ...]] = {}

    def add_level(rows: list[tuple[str, Entity]]) -> None:
        groups: dict[str, list[tuple[str, Entity]]] = defaultdict(list)
        for address, entity in rows:
            groups[address.casefold()].append((address, entity))
        for key, group in groups.items():
            group.sort(key=lambda row: (row[1].span.file_id, row[1].span.char_start))
            names: list[str] = []
            for n, (address, entity) in enumerate(group, 1):
                final = f"{address}#{n}" if len(group) > 1 else address
                addresses[final] = entity
                by_id[entity.entity_id].append(final)
                names.append(final)
            if len(group) > 1:
                conflicts[key] = tuple(names)

    rows: list[tuple[str, Entity]] = [("Конвертация", document.conversion)]
    for prefix, entities in (
        ("ПКО", document.pko),
        ("ПОД", document.pod),
        ("ПКПД", document.pkpd),
        ("Параметр", document.parameters),
    ):
        rows.extend((f"{prefix}/{escape_segment(e.name)}", e) for e in entities)
    for routine in document.routines:
        for role, prefix in (
            ("algorithm", "Алгоритм"),
            ("handler", "Обработчик"),
            ("support", "Служебный"),
            ("dispatcher", "Диспетчер"),
            ("event", "Событие"),
        ):
            if role in routine.roles or (role == "handler" and "callback" in routine.roles):
                rows.append((f"{prefix}/{escape_segment(routine.name)}", routine))
    for fragment in document.unknown:
        span = fragment.span
        rows.append(
            (
                f"Неизвестное/{escape_segment(span.file_id)}/"
                f"{span.line_start}-{span.line_end}-{span.char_start}",
                fragment,
            )
        )
    add_level(rows)
    rows = []
    for rule in document.pko:
        parent = by_id[rule.entity_id][0]
        for prop in rule.properties:
            rows.append(
                (
                    parent
                    + "/ПКС/"
                    + property_key(prop.format_property, prop.configuration_property),
                    prop,
                )
            )
        for group in rule.groups:
            rows.append(
                (
                    parent
                    + "/ПКТЧ/"
                    + property_key(group.format_property, group.configuration_property),
                    group,
                )
            )
        rows.extend((f"{parent}/Поиск/{item.ordinal}", item) for item in rule.search_sets)
    for rule in document.pkpd:
        parent = by_id[rule.entity_id][0]
        rows.extend((f"{parent}/Значение/{item.ordinal}", item) for item in rule.mappings)
    add_level(rows)
    rows = []
    for rule in document.pko:
        for group in rule.groups:
            parent = by_id[group.entity_id][0]
            rows.extend(
                (
                    parent
                    + "/ПКС/"
                    + property_key(item.format_property, item.configuration_property),
                    item,
                )
                for item in group.properties
            )
    add_level(rows)
    return AddressIndex(
        MappingProxyType(addresses),
        MappingProxyType({key: tuple(value) for key, value in by_id.items()}),
        MappingProxyType(conflicts),
    )


def locate(document: EdDocument, line: int, file_id: str | None = None) -> tuple[Entity, ...]:
    """Сущности строки от самого узкого диапазона к владельцу."""
    source = next((f for f in document.files if file_id is None or f.file_id == file_id), None)
    if source is None:
        raise EntityNotFoundError("Файл не найден")
    document.coverage.classify_line(line)
    found = [
        e
        for e in document.entities()
        if e.span.file_id == source.file_id
        and e.span.line_start <= line <= e.span.line_end
        and e.kind not in ("conversion", "diagnostic", "guard", "use", "binding")
        and not (isinstance(e, Routine) and not e.roles)
    ]
    group_ids = {e.group_id for e in found if isinstance(e, PropertyRule) and e.group_id}
    found.extend(
        group
        for rule in document.pko
        for group in rule.groups
        if group.entity_id in group_ids and group not in found
    )
    return tuple(
        sorted(
            found,
            key=lambda e: (
                {
                    "pks": 0,
                    "unknown": 0,
                    "search": 0,
                    "value": 0,
                    "pktch": 1,
                    "pko": 2,
                    "pod": 2,
                    "pkpd": 2,
                }.get(e.kind, 3),
                e.span.char_end - e.span.char_start,
                e.kind,
                e.entity_id,
            ),
        )
    )
