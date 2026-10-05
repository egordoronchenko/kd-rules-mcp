"""Смысловая форма ED: без физических координат, с порядком и явными связями."""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, fields, is_dataclass
from decimal import Decimal
from functools import lru_cache
from typing import Any

from .address import escape_segment
from .writer_model import (
    CodeUnit,
    LayoutContainer,
    LayoutElement,
    ManagerModel,
    Member,
    RetainedBlock,
    Value,
    digest,
)


@lru_cache(maxsize=32_768)
def _escaped(name: str) -> str:
    return escape_segment(name)


def model_addresses(model: ManagerModel) -> dict[str, str]:
    """Квалификация повторов локальна родителю; логические ID не перенумеровываются."""
    if model._cached_addresses is not None:
        return dict(model._cached_addresses)
    result: dict[str, str] = {}
    used_addresses: set[str] = set()
    containers = {c.logical_id: c for c in model.layouts}
    layout_order = {}

    def scan_order(key):
        layout_order[key] = len(layout_order)
        for element in containers[key].elements:
            if element.block_id:
                layout_order.setdefault(element.block_id, len(layout_order))
            if element.container_id:
                scan_order(element.container_id)
            elif element.entity_id:
                layout_order.setdefault(element.entity_id, len(layout_order))

    for key in model.root_layouts:
        scan_order(key)

    def level(prefix: str, members: tuple[Member, ...]) -> None:
        members = tuple(sorted(members, key=lambda m: layout_order.get(m.logical_id, 10**9)))
        counts = Counter(item.name.casefold() for item in members)
        seen: dict[str, int] = defaultdict(int)
        for item in members:
            key = item.name.casefold()
            seen[key] += 1
            address = prefix + "/" + _escaped(item.name)
            if counts[key] > 1:
                directions = getattr(item, "directions", ())
                address += "~" + ("+".join(directions) or str(seen[key]))
                if address in used_addresses:
                    address += "#" + str(seen[key])
            result[item.logical_id] = address
            used_addresses.add(address)

    for name, items in (
        ("ПКО", model.pko),
        ("ПОД", model.pod),
        ("ПКПД", model.pkpd),
        ("Параметр", model.parameters),
        ("Код", model.code_units),
        ("Событие", model.conversion_events),
        (
            "Использование",
            tuple(sorted(model.rule_uses, key=lambda u: layout_order.get(u.logical_id, 10**9))),
        ),
        ("Условие", model.guards),
        ("Ветка", model.dispatcher_cases),
    ):
        level(name, items)
    for rule in model.pko:
        parent = result[rule.logical_id]
        level(parent + "/ПКС", rule.properties)
        level(parent + "/ПКТЧ", rule.groups)
        level(parent + "/Событие", rule.events)
        result[rule.identification.logical_id] = parent + "/Идентификация"
        searches = {s.logical_id: s for s in rule.identification.search_sets}
        order = (
            model.ordered_entity_ids(rule.logical_id)
            if any(c.logical_id == rule.logical_id for c in model.layouts)
            else tuple(searches)
        )
        for n, key in enumerate((key for key in order if key in searches), 1):
            search = searches[key]
            result[search.logical_id] = parent + f"/Поиск/{n}"
        for group in rule.groups:
            level(result[group.logical_id] + "/ПКС", group.properties)
    for rule in model.pod:
        level(result[rule.logical_id] + "/Событие", rule.events)
    for rule in model.pkpd:
        for n, value in enumerate(rule.mappings, 1):
            result[value.logical_id] = result[rule.logical_id] + f"/Значение/{n}"
    for container in model.layouts:
        result.setdefault(
            container.logical_id,
            result[container.owner_id] + "/Значения/" + _escaped(container.name)
            if container.kind == "values" and container.owner_id in result
            else "Раскладка/Модуль"
            if container.kind == "module"
            else "Раскладка/" + _escaped(container.name),
        )

    def chains(key):
        count = 0
        directions = defaultdict(int)
        for element in containers[key].elements:
            if element.container_id is None:
                continue
            child = containers[element.container_id]
            if child.kind == "conditional" and child.branch == "chain":
                count += 1
                result[child.logical_id] = result[key] + f"/Цепочка/{count}"
            elif child.kind == "conditional":
                directions[child.direction] += 1
                result[child.logical_id] = (
                    result[key]
                    + "/Условие/"
                    + str(child.direction)
                    + f"/{directions[child.direction]}"
                )
            chains(child.logical_id)

    for key in model.root_layouts:
        chains(key)
    block_entities = {
        e.block_id: e.entity_id
        for c in model.layouts
        for e in c.elements
        if e.block_id is not None and e.entity_id is not None
    }
    guard_counts = defaultdict(int)
    by_block = {b.logical_id: b for b in model.retained_blocks}
    for guard in model.guards:
        if guard.inside_leaf_id:
            block = by_block[guard.inside_leaf_id]
            guard_counts[block.logical_id] += 1
            base = result.get(block_entities.get(block.logical_id, "")) or (
                result.get(block.owner_id or "", "Раскладка") + "/Текст/" + block.sha256[:16]
            )
            result[guard.logical_id] = base + f"/Условие/{guard_counts[block.logical_id]}"
    block_counts = defaultdict(int)
    for block in sorted(
        model.retained_blocks, key=lambda b: layout_order.get(b.logical_id, len(layout_order))
    ):
        entity_id = block_entities.get(block.logical_id)
        base = result.get(entity_id or "")
        if base is None:
            base = (
                result.get(block.owner_id or "", "Раскладка")
                + f"/Текст/{block.kind}/{block.sha256[:16]}"
            )
        else:
            base += "/Сохранено"
        block_counts[base] += 1
        result[block.logical_id] = base + (
            f"~{block_counts[base]}" if block_counts[base] > 1 else ""
        )
    for element in (e for c in model.layouts for e in c.elements if e.field.startswith("header.")):
        if element.entity_id is not None:
            result[element.entity_id] = "Конвертация"
    for container in model.layouts:
        result.setdefault(container.logical_id, "Раскладка/" + _escaped(container.name))
        counts = defaultdict(int)
        for element in container.elements:
            name = result.get(
                element.entity_id or element.block_id or element.container_id or "", element.field
            )
            counts[name] += 1
            result.setdefault(
                element.logical_id,
                result[container.logical_id] + "/Оператор/" + name + f"/{counts[name]}",
            )
    unknown = 0
    source_owners = {b.owner_id for b in model.retained_blocks}
    for entry in model.source_map:
        if entry.logical_id not in result and entry.logical_id in source_owners:
            if entry.address == "Конвертация":
                result[entry.logical_id] = "Конвертация"
            elif entry.address.startswith("Неизвестное/"):
                unknown += 1
                result[entry.logical_id] = f"Источник/Неизвестное/{unknown}"
            elif any(kind in entry.reader_id for kind in (":routine:", ":version:")):
                result[entry.logical_id] = "Источник/" + entry.address
    object.__setattr__(model, "_cached_addresses", result)
    return dict(result)


_ROOT_PHYSICAL = frozenset(
    {
        "source_map",
        "source_files",
        "revision",
        "project_id",
        "decisions",
        "confirmations",
        "import_report",
        "module_styles",
    }
)
_LINKS = frozenset(
    {
        "logical_id",
        "target_id",
        "parent_id",
        "owner_id",
        "entity_id",
        "block_id",
        "container_id",
        "inside_leaf_id",
    }
)


def canonical_value(
    value: Any, ids: dict[str, str], field: str = "", path: tuple[str, ...] = ()
) -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        excluded = _ROOT_PHYSICAL if isinstance(value, ManagerModel) else frozenset()
        if isinstance(value, RetainedBlock):
            excluded |= {"file_id", "source_hash", "char_start", "char_end"}
        if isinstance(value, CodeUnit):
            excluded |= {"file_id", "body_start", "body_end", "origin"}
        if isinstance(value, LayoutElement):
            excluded |= {"source"}
        if isinstance(value, LayoutContainer):
            excluded |= {"opening", "closing"}
            if value.kind == "module":
                excluded |= {"name"}
        result = {
            f.name: canonical_value(getattr(value, f.name), ids, f.name, (*path, f.name))
            for f in fields(value)
            if f.name not in excluded and not f.name.startswith("_")
        }
        if isinstance(value, Value) and value.state == "number":
            number = format(Decimal(str(value.value)), "f")
            result["value"] = number.rstrip("0").rstrip(".") if "." in number else number
        return result
    if isinstance(value, dict):
        return {
            key: canonical_value(item, ids, key, (*path, key))
            for key, item in value.items()
            if not (not path and "project_id" in value and key in _ROOT_PHYSICAL)
        }
    if isinstance(value, list | tuple):
        return [
            canonical_value(
                item,
                ids,
                "target_id" if field in ("guards", "root_layouts") else "",
                (*path, "*"),
            )
            for item in value
        ]
    if field in _LINKS and isinstance(value, str):
        return ids.get(value, "unresolved:" + value)
    return value


@dataclass(frozen=True, slots=True)
class CanonicalModel:
    value: dict[str, Any]
    id_map: tuple[tuple[str, str], ...]

    @property
    def sha256(self) -> str:
        return digest(self.value)


def canonical_model(model: ManagerModel) -> CanonicalModel:
    ids = model_addresses(model)
    value = canonical_value(model, ids)

    # Направления правила — множество: порядок использований в точках входа задаёт раскладка,
    # а не порядок кортежа (после повторного чтения он всегда «отправка, получение»).
    for catalog in ("pko", "pod", "pkpd"):
        for rule in value.get(catalog, ()):
            if isinstance(rule, dict) and isinstance(rule.get("directions"), list):
                rule["directions"] = sorted(
                    rule["directions"], key=("send", "receive", "both").index
                )

    # Каталоги не задают порядок вывода. Он только в elements контейнеров.
    def catalogs(row):
        if isinstance(row, dict):
            for key, item in row.items():
                if (
                    key
                    in {
                        "pko",
                        "pod",
                        "pkpd",
                        "parameters",
                        "code_units",
                        "conversion_events",
                        "rule_uses",
                        "guards",
                        "dispatcher_cases",
                        "retained_blocks",
                        "layouts",
                        "properties",
                        "groups",
                        "events",
                        "search_sets",
                    }
                    and item
                    and isinstance(item[0], dict)
                    and "logical_id" in item[0]
                ):
                    item.sort(key=lambda member: member["logical_id"])
                catalogs(item)
        elif isinstance(row, list):
            for item in row:
                catalogs(item)

    catalogs(value)
    return CanonicalModel(value, tuple(ids.items()))


def canonicalize(model: ManagerModel) -> dict[str, Any]:
    return canonical_model(model).value
