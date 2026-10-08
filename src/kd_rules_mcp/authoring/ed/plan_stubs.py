"""ПОД-заглушки — часть доставки, долговечный проект ими не изменяется (#73)."""

from dataclasses import replace

from kd_rules_mcp.authoring.ed.manager_operations import (
    ManagerOperationError,
    parse_operation,
    preview,
)
from kd_rules_mcp.ed.writer_model import ManagerModel
from kd_rules_mcp.structures.xmldump import KINDS
from kd_rules_mcp.validation.ed_structure_snapshot import StructureObject

COMMENT = "// объект состава плана без правил: регистрируется, не отправляется (#73)"


def add_plan_stubs(
    model: ManagerModel, objects: tuple[StructureObject, ...]
) -> tuple[ManagerModel, tuple[dict[str, str], ...]]:
    """Пустой список ПКО и отсутствие обработчика допустимы: XDTO:794–906,8406–8432."""
    occupied = {r.name.casefold() for r in (*model.pod, *model.pko)}
    procedures = {u.name.casefold() for u in model.code_units}
    procedures.update(r.procedure_name.casefold() for r in (*model.pod, *model.pko))
    clients = {d.client_id for d in model.decisions}
    collections = {value[1].casefold(): value[2] for value in KINDS.values()}
    rows = []
    operations = []
    for obj in objects:
        base = f"{obj.kind}_{obj.name}_Отправка_Заглушка"
        name = base
        suffix = 1
        while name.casefold() in occupied or ("ДобавитьПОД_" + name).casefold() in procedures:
            suffix += 1
            name = base + "_" + str(suffix)
        occupied.add(name.casefold())
        client = "delivery-pod-stub/" + name
        while client in clients:
            client += "/delivery"
        clients.add(client)
        rows.append({"object": obj.kind + "." + obj.name, "name": name})
        operations.append(
            parse_operation(
                {
                    "client_id": client,
                    "kind": "pod",
                    "action": "create",
                    "patch": {
                        "name": name,
                        "directions": ["send"],
                        "configuration_selection": {
                            "state": "reference",
                            "reference_parts": [
                                "Метаданные",
                                collections[obj.kind.casefold()],
                                obj.name,
                            ],
                        },
                        "clear_data": {"state": "boolean", "value": False},
                    },
                }
            )
        )
    # Общий авторинг ограничивает пакет сотней операций; состав плана может быть больше.
    for offset in range(0, len(operations), 100):
        planned = preview(
            model, tuple(operations[offset : offset + 100]), expected_revision=model.revision
        )
        if planned.failures:
            raise ManagerOperationError(planned.failures)
        model = planned.model
    stub_names = {row["name"] for row in rows}
    stub_ids = {r.logical_id for r in model.pod if r.name in stub_names}
    if stub_ids:
        model = replace(
            model,
            layouts=tuple(
                replace(
                    container,
                    elements=tuple(
                        replace(element, trailing_comment=COMMENT)
                        if element.entity_id in stub_ids and element.field == "name"
                        else element
                        for element in container.elements
                    ),
                )
                for container in model.layouts
            ),
        ).with_revision()
    return model, tuple(rows)
