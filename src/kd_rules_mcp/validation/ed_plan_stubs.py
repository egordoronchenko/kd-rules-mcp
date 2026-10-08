"""Объекты состава без ПОД отправки: исключение XDTO:8342–8357, пустой ПКО XDTO:8402–8432."""

from kd_rules_mcp.ed.model import EdDocument
from kd_rules_mcp.ed.writer_model import ManagerModel
from kd_rules_mcp.structures.xmldump import KINDS
from kd_rules_mcp.validation.ed_structure_snapshot import (
    COLLECTIONS,
    StructureObject,
    StructureSnapshot,
)

CHECK = "ed.plan.pod_missing"


def missing_plan_pods(
    model: ManagerModel | EdDocument,
    structure: StructureSnapshot,
    plan_name: str,
    additions: tuple[tuple[str, str, str], ...] = (),
    registration_objects: tuple[str, ...] = (),
) -> tuple[StructureObject, ...] | None:
    """Штатный состав и доставка объединяются; ПОД получения не покрывает отправку."""
    plan = structure.objects.get(("планобмена", plan_name.casefold()))
    if plan is None or not any(
        p.kind == "СоставПланаОбмена" for rows in plan.properties.values() for p in rows
    ):
        return None
    objects = {}
    for rows in plan.properties.values():
        for prop in rows:
            if prop.kind == "ЭлементСоставаПланаОбмена":
                for name in (*prop.types, *prop.unresolved):
                    obj = structure.by_type.get(name.casefold())
                    if obj:
                        objects[(obj.kind.casefold(), obj.name.casefold())] = obj
    for tag, name, _ in additions:
        key = KINDS[tag][1].casefold(), name.casefold()
        if key in structure.objects:
            objects[key] = structure.objects[key]
    for full_name in registration_objects:
        kind, _, name = full_name.partition(".")
        key = kind.casefold(), name.casefold()
        if key in structure.objects:
            objects[key] = structure.objects[key]
    covered = set()
    if isinstance(model, ManagerModel):
        selections = [
            (pod.directions, pod.configuration_selection.reference_parts) for pod in model.pod
        ]
    else:
        selections = [
            (
                tuple(u.direction for u in model.rule_uses if u.rule_id == pod.entity_id),
                pod.configuration_selection.value.reference_parts
                if pod.configuration_selection.value
                else (),
            )
            for pod in model.pod
        ]
    for directions, parts in selections:
        if not set(directions).intersection(("send", "both")):
            continue
        if len(parts) == 3 and parts[0].casefold() == "метаданные":
            kind = COLLECTIONS.get(parts[1].casefold())
            if kind:
                covered.add((kind.casefold(), parts[2].casefold()))
    return tuple(objects[key] for key in sorted(objects) if key not in covered)
