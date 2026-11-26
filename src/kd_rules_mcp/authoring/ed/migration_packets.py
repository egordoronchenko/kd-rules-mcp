"""Пакеты черновика: порядок зависимостей, стабильные ссылки и лимит писателя."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from kd_rules_mcp.authoring.ed.manager_operations import MAX_OPERATIONS, parse_operation
from kd_rules_mcp.ed.writer_model import digest


@dataclass(frozen=True, slots=True)
class DraftOperation:
    origin: str
    address: str
    operation: dict[str, Any]
    group: str = ""


@dataclass(frozen=True, slots=True)
class Packet:
    index: int
    expected_revision: str | None
    operations: list[dict[str, Any]]


def operation(
    origin: str, kind: str, patch: dict, address: str, *, group: str = "", **fields: Any
) -> DraftOperation:
    payload = {"kind": kind, "action": "create", "patch": patch, **fields}
    # Хеш учитывает и владельца/конвертацию, а не только patch.
    role, _, path = origin.partition(":")
    # В client_id входит код, а не навигационная подпись вида «ПКО «Код»».
    if "«" in path and "»" in path:
        _, _, tail = path.partition("«")
        code, _, remainder = tail.partition("»")
        remainder = remainder.removeprefix(" / ПКС ").strip(" /")
        path = code + ("/" + remainder if remainder else "")
    client = f"mig/{role}/{kind}/{path}"[:190] + "/" + digest(payload)[:8]
    return DraftOperation(origin, address, {"client_id": client, **payload}, group)


def packets(operations: list[DraftOperation], revision: str) -> list[Packet]:
    """Ссылки до упаковки адресные; внутри пакета заменяются на client_id."""
    rank = {
        "pkpd": 0,
        "value_mapping": 1,
        "pko": 2,
        "table_part": 3,
        "property": 4,
        "pod": 5,
        "parameter": 6,
    }
    units: dict[str, list[DraftOperation]] = {}
    addresses: dict[str, str] = {}
    for index, entry in enumerate(sorted(operations, key=lambda e: rank[e.operation["kind"]])):
        key = entry.group or f"operation:{index}"
        units.setdefault(key, []).append(entry)
        addresses[entry.address] = key

    def address_references(value: Any):
        if isinstance(value, dict):
            if set(value) == {"address"}:
                yield value["address"]
            else:
                for child in value.values():
                    yield from address_references(child)
        elif isinstance(value, list):
            for child in value:
                yield from address_references(child)

    dependencies = {
        key: {
            addresses[a]
            for entry in entries
            for a in address_references(entry.operation)
            if a in addresses and addresses[a] != key
        }
        for key, entries in units.items()
    }
    batches: list[list[DraftOperation]] = []
    current: list[DraftOperation] = []
    remaining = dict(units)
    while remaining:
        ready = next(
            (key for key in remaining if not dependencies[key].intersection(remaining)), None
        )
        selected = {ready} if ready is not None else {next(iter(remaining))}
        # Цикл ссылок: каркасы всех участников должны предшествовать их ПКС.
        if ready is None:
            pending = list(selected)
            while pending:
                for key in dependencies[pending.pop()].intersection(remaining):
                    if key not in selected:
                        selected.add(key)
                        pending.append(key)
        group = [
            entry for key, entries in remaining.items() if key in selected for entry in entries
        ]
        group.sort(key=lambda e: rank[e.operation["kind"]])
        if len(group) <= MAX_OPERATIONS and len(current) + len(group) > MAX_OPERATIONS:
            batches.append(current)
            current = []
        for row in group:
            if len(current) == MAX_OPERATIONS:
                batches.append(current)
                current = []
            current.append(row)
        for key in selected:
            del remaining[key]
    if current:
        batches.append(current)

    def references(value: Any, known: dict[str, str]) -> Any:
        if isinstance(value, dict):
            if set(value) == {"address"} and value["address"] in known:
                return {"client_id": known[value["address"]]}
            return {k: references(v, known) for k, v in value.items()}
        if isinstance(value, list):
            return [references(v, known) for v in value]
        return value

    result = []
    for batch in batches:
        batch.sort(key=lambda e: rank[e.operation["kind"]])
        known: dict[str, str] = {}
        rows = []
        for entry in batch:
            # Хеш именно выдаваемой операции, включая разрешённые client_id-ссылки.
            row = references(entry.operation, known)
            row["client_id"] = (
                row["client_id"].rsplit("/", 1)[0]
                + "/"
                + digest({k: v for k, v in row.items() if k != "client_id"})[:8]
            )
            parse_operation(row)
            rows.append(row)
            known[entry.address] = row["client_id"]
        result.append(Packet(len(result), revision if not result else None, rows))
    return result
