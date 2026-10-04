"""DTO второго среза: зависимости и контракт для писателя B, без файловой записи."""

import hashlib
import json
from dataclasses import asdict, dataclass
from typing import Any

from .model import (
    AddAlgorithmicHeaderProperty,
    AddHeaderProperty,
    AttributeDraft,
    AuthoringPreconditionError,
    AuthoringTarget,
    Failure,
    HandlerEvent,
    Notice,
    Operation,
    PreserveMissingHeaderProperty,
    SetObjectHandler,
    digest,
    operation_dependencies,
    order_operations,
)

# T:79,85,87; XDTO:8548–8558,8600–8609,8655–8665 — порядок метода менеджера.
EVENT_PARAMETERS: dict[HandlerEvent, tuple[str, ...]] = {
    "ПриОтправкеДанных": ("ДанныеИБ", "ДанныеXDTO", "КомпонентыОбмена", "СтекВыгрузки"),
    "ПриКонвертацииДанныхXDTO": ("ДанныеXDTO", "ПолученныеДанные", "КомпонентыОбмена"),
    "ПередЗаписьюПолученныхДанных": (
        "ПолученныеДанные",
        "ДанныеИБ",
        "КонвертацияСвойств",
        "КомпонентыОбмена",
    ),
}
DISPATCHER = "ВыполнитьПроцедуруМодуляМенеджера"


def operation_kind(operation: Operation) -> str:
    return {
        AddHeaderProperty: "add_header_property",
        SetObjectHandler: "set_object_handler",
        PreserveMissingHeaderProperty: "preserve_missing_header_property",
        AddAlgorithmicHeaderProperty: "add_algorithmic_header_property",
    }[AddHeaderProperty if isinstance(operation, AddHeaderProperty) else type(operation)]


def handler_name(prefix: str, canonical_pko: str, direction: str, event: str) -> str:
    """Имя слота не зависит от тела; canonical_pko — имя модели, не адрес с #."""
    return prefix + "ПКО_" + digest((direction, canonical_pko, event))[:16]


def operation_from_input(value: dict[str, Any]) -> Operation:
    """Чистый декодер resolved-target; транспортные session-ID разрешает сервис C."""
    payload = dict(value)
    kind = payload.pop("kind", "add_header_property")
    payload["target"] = AuthoringTarget(**payload["target"])
    constructors = {
        "set_object_handler": SetObjectHandler,
        "preserve_missing_header_property": PreserveMissingHeaderProperty,
        "add_algorithmic_header_property": AddAlgorithmicHeaderProperty,
    }
    if kind == "add_header_property":
        draft = payload.get("new_attribute")
        payload["new_attribute"] = AttributeDraft(**draft) if draft else None
        return AddHeaderProperty(**payload)
    if kind not in constructors:
        raise ValueError("Неизвестный вид операции авторинга")
    return constructors[kind](**payload)


def canonical_operation_dict(operation: Operation) -> dict[str, Any]:
    """Legacy побайтно продолжает manifest v1, без нового поля kind."""
    if isinstance(operation, AddHeaderProperty):
        from .manifest import operation_dict

        return operation_dict(operation)
    value = asdict(operation)
    value.update(
        kind=operation_kind(operation),
        operation_id=operation.operation_id,
        dependencies=list(operation_dependencies(operation)),
    )
    return value


def canonical_operations_bytes(operations: tuple[Operation, ...]) -> bytes:
    return (
        json.dumps(
            [canonical_operation_dict(op) for op in order_operations(operations)],
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
        )
        + "\n"
    ).encode("utf-8")


def preservation_marker(
    properties: tuple[AddHeaderProperty, ...],
    marker: str | None,
    *,
    has_received_data: bool = True,
) -> str | None:
    """Модель preset для B: к именам формата добавляются имена исключаемых реквизитов.

    Вход — уже проверенные парные ПКС одного ПКО. Это unit-oracle, не исполнение BSL.
    XDTO:6753–6764,7030–7047; КОС:2249–2254,3465–3481 — учёт отсутствия и перенос.
    """
    if marker is None or not has_received_data:
        return marker
    names = [name.strip() for name in marker.split(",") if name]
    missing = set(names)
    additions = []
    for prop in order_operations(properties):
        if prop.format_property in missing and prop.configuration_attribute not in missing:
            additions.append(prop.configuration_attribute)
            missing.add(prop.configuration_attribute)
    return ",".join((*names, *additions)) if additions else marker


def merge_operations(
    previous: tuple[Operation, ...],
    incoming: tuple[Operation, ...],
    *,
    drop_operations: tuple[str, ...] = (),
) -> tuple[Operation, ...]:
    """Drop не каскадный: потерянную зависимость требуется удалить/заменить явно."""
    known = {op.operation_id: op for op in previous}
    unknown = set(drop_operations) - known.keys()
    if unknown:
        raise AuthoringPreconditionError(
            (Failure("ed.author.handler_property_dependency", "", "Снятие неизвестной операции"),)
        )
    remaining = {key: op for key, op in known.items() if key not in drop_operations}
    remaining.update((op.operation_id, op) for op in incoming)
    ordered = order_operations(tuple(remaining.values()))
    slots: dict[tuple, str] = {}
    for op in ordered:
        if isinstance(op, SetObjectHandler):
            slot = (op.target.direction, op.target.pko_address, op.event)
            if slot in slots:
                raise AuthoringPreconditionError(
                    (
                        Failure(
                            "ed.author.handler_slot_conflict",
                            op.target.pko_address,
                            "Изменение тела требует явного снятия прежней операции",
                        ),
                    )
                )
            slots[slot] = op.operation_id
    return ordered


@dataclass(frozen=True, slots=True)
class HandlerBindingPlan:
    """Одна будущая процедура; прежний вызов B обязан отрисовать первым ровно один раз."""

    target: AuthoringTarget
    event: str
    handler_name: str
    parameters: tuple[str, ...]
    operation_ids: tuple[str, ...]
    previous_name: str = ""
    previous_arguments: tuple[str, ...] = ()
    previous_branch_hash: str = ""
    previous_routine_hash: str = ""
    runtime_verified: bool = False
    manager_interface: int = 0


@dataclass(frozen=True, slots=True)
class HandlerOperationsPlan:
    operations: tuple[Operation, ...]
    bindings: tuple[HandlerBindingPlan, ...]
    notices: tuple[Notice, ...]
    canonical_bytes: bytes
    runtime_verified: bool = False
    dispatcher_name: str = ""
    dispatcher_order: tuple[str, ...] = ()
    manager_interface: int = 0

    @property
    def decision_hash(self) -> str:
        return hashlib.sha256(self.canonical_bytes).hexdigest()
