"""Дифф авторского оригинала по смысловым адресам; без шума source-map."""

from dataclasses import dataclass, fields
from typing import Any

from .canonical import canonical_value, model_addresses
from .writer_model import CodeUnit, ManagerModel, RetainedBlock


@dataclass(frozen=True, slots=True)
class ManagerChange:
    address: str
    action: str
    fields: tuple[str, ...]
    before: Any
    after: Any


@dataclass(frozen=True, slots=True)
class ManagerDiff:
    changes: tuple[ManagerChange, ...]
    id_map: tuple[tuple[str, str], ...]

    @property
    def equal(self) -> bool:
        return not self.changes


def compare_models(before: ManagerModel, after: ManagerModel) -> ManagerDiff:
    old_addresses, new_addresses = model_addresses(before), model_addresses(after)
    old = {m.logical_id: m for m in before.members()}
    new = {m.logical_id: m for m in after.members()}
    pairs = {key: key for key in old if key in new}
    matched_targets = set(pairs.values())
    unmatched = {new_addresses[key]: key for key in new if key not in matched_targets}
    for key in old:
        if key not in pairs and old_addresses[key] in unmatched:
            pairs[key] = unmatched.pop(old_addresses[key])
    matched_targets = set(pairs.values())
    # Общие токены пары позволяют показать переименование только у самого узла,
    # а не как ложное изменение всех родительских списков и ссылок.
    old_ids = {key: f"member:{n}" for n, key in enumerate(old)}
    old_ids.update(
        {key: "source:" + address for key, address in old_addresses.items() if key not in old_ids}
    )
    new_ids = {target: old_ids[key] for key, target in pairs.items()}
    new_ids.update({key: "new:" + new_addresses[key] for key in new if key not in new_ids})
    new_ids.update(
        {key: "source:" + address for key, address in new_addresses.items() if key not in new_ids}
    )
    changes = []
    child_fields = (
        "events",
        "properties",
        "groups",
        "search_sets",
        "mappings",
    )

    def own(member, ids):
        excluded = set(child_fields)
        if isinstance(member, RetainedBlock):
            excluded |= {"file_id", "source_hash", "char_start", "char_end"}
        if isinstance(member, CodeUnit):
            excluded |= {"file_id", "body_start", "body_end"}
        return {
            f.name: ids[getattr(member, f.name).logical_id]
            if f.name == "identification"
            else canonical_value(getattr(member, f.name), ids, f.name)
            for f in fields(member)
            if f.name not in excluded and not f.name.startswith("_")
        }

    for key, member in old.items():
        target = pairs.get(key)
        if target is not None and member is new[target]:
            continue
        left = own(member, old_ids)
        if target is None:
            changes.append(ManagerChange(old_addresses[key], "delete", (), left, None))
            continue
        right = own(new[target], new_ids)
        different = tuple(k for k in left if left[k] != right[k])
        if different:
            changes.append(
                ManagerChange(
                    new_addresses[target],
                    "update",
                    different,
                    {k: left[k] for k in different},
                    {k: right[k] for k in different},
                )
            )
    for key, member in new.items():
        if key not in matched_targets:
            changes.append(
                ManagerChange(new_addresses[key], "create", (), None, own(member, new_ids))
            )
    for key in (
        "header",
        "host",
        "format_bindings",
        "executor_profile",
        "dispatcher_unknown_policy",
    ):
        if getattr(before, key) is getattr(after, key):
            continue
        left, right = (
            canonical_value(getattr(before, key), old_ids),
            canonical_value(getattr(after, key), new_ids),
        )
        if left != right:
            changes.append(ManagerChange("Конвертация/" + key, "update", (key,), left, right))
    old_containers = {c.logical_id: c for c in before.layouts}
    new_containers = {c.logical_id: c for c in after.layouts}
    new_by_address = {new_addresses[c.logical_id]: c for c in after.layouts}
    for key, container in old_containers.items():
        other = new_containers.get(key) or new_by_address.get(old_addresses[key])
        if other is container or other is None:
            continue
        a = [
            old_ids.get(e.logical_id, old_addresses.get(e.logical_id, e.logical_id))
            for e in container.elements
        ]
        b = [
            new_ids.get(e.logical_id, new_addresses.get(e.logical_id, e.logical_id))
            for e in other.elements
        ]
        if a != b:
            changes.append(
                ManagerChange(new_addresses[other.logical_id] + "/order", "order", ("order",), a, b)
            )
        metadata = ("kind", "owner_id", "state", "direction", "branch")
        if container.kind not in ("rule", "module"):
            metadata += ("name", "signature")
        left = {name: canonical_value(getattr(container, name), old_ids, name) for name in metadata}
        right = {name: canonical_value(getattr(other, name), new_ids, name) for name in metadata}
        different = tuple(name for name in metadata if left[name] != right[name])
        if different:
            changes.append(
                ManagerChange(
                    new_addresses[other.logical_id],
                    "update",
                    different,
                    {name: left[name] for name in different},
                    {name: right[name] for name in different},
                )
            )
        elements = {
            new_ids.get(e.logical_id, new_addresses.get(e.logical_id, e.logical_id)): e
            for e in other.elements
        }
        for element in container.elements:
            target = elements.get(
                old_ids.get(
                    element.logical_id, old_addresses.get(element.logical_id, element.logical_id)
                )
            )
            if target is None or target is element:
                continue
            left = canonical_value(element, old_ids)
            right = canonical_value(target, new_ids)
            different = tuple(
                name for name in left if name != "logical_id" and left[name] != right[name]
            )
            if different:
                changes.append(
                    ManagerChange(
                        new_addresses[target.logical_id],
                        "update",
                        different,
                        {name: left[name] for name in different},
                        {name: right[name] for name in different},
                    )
                )
    return ManagerDiff(tuple(changes), tuple(pairs.items()))
