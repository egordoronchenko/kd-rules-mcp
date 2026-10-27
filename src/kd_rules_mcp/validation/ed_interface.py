"""Интерфейс модуля менеджера: методы, которые исполнитель БСП вызывает у менеджера.

Проверка обычного чтения, без проекта авторинга (design Р1 изменения kd3-silent-fixes).
Контракты — общие с писателем: `ed/executor_profile.py::_CONTRACTS` (имя, вид, параметры, строки
XDTO). У писателя форма проверяется точнее (`ed.writer.entrypoint`); здесь — только то, без чего
вызов исполнителя упадёт: метод есть, экспортный, функция там, где нужен результат, и формальные
принимают вызов.
"""

from __future__ import annotations

from collections import defaultdict

from kd_rules_mcp.ed.address import AddressIndex
from kd_rules_mcp.ed.executor_profile import (
    BSP_3_1_12_XDTO,
    ExecutorCapabilities,
    RoutineContract,
)
from kd_rules_mcp.ed.model import EdDocument, Entity, Routine
from kd_rules_mcp.validation.report import ValidationReport

REQUIRED = "ed.conversion.required"
SIGNATURE = "ed.conversion.signature"
# Отсутствие трёх событий уже ловит ed_links; функции версии нет — откат на «1» (XDTO:4770–4774).
_PRESENT_ONLY = frozenset(
    {
        "передконвертацией",
        "послеконвертации",
        "передотложеннымзаполнением",
        "версияформатаменеджераобмена",
    }
)
# XDTO:ВыборкаДанных:8486–8493 — вызывается только при заполненном обработчике выборки ПОД.
_SELECTION_DISPATCHER = "выполнитьфункциюмодуляменеджера"


def validate_interface(
    document: EdDocument,
    addresses: AddressIndex,
    *,
    extensions: bool = False,
    profile: ExecutorCapabilities = BSP_3_1_12_XDTO,
) -> ValidationReport:
    """`extensions`: у модуля есть слои расширений — отсутствующий метод может добавить слой."""
    report = ValidationReport()
    # XDTO:4716–4728 — версия «3» строкой выбирает интерфейс 3, любая другая — прежний вызов.
    interface = 3 if document.manager_version == 3 else 1
    routines: dict[str, list[Routine]] = defaultdict(list)
    for routine in document.routines:
        routines[routine.name.casefold()].append(routine)
    selection = any(
        binding.event == "ВыборкаДанных" and binding.target_name.strip()
        for rule in document.pod
        for binding in rule.events
    )
    for contract in profile.contracts:
        if interface not in contract.interfaces:
            continue
        name = contract.name.casefold()
        found = routines.get(name, [])
        if not found:
            optional = (
                name in _PRESENT_ONLY
                or not contract.required
                or (name == _SELECTION_DISPATCHER and not selection)
            )
            if optional:
                continue
            if extensions:
                report.skip(
                    REQUIRED,
                    f"Метод «{contract.name}» не найден в базовом модуле; его может добавить "
                    "расширение — состав методов слоёв не проверялся",
                )
                continue
            report.error(
                REQUIRED,
                _address(addresses, document.conversion),
                f"Нет метода «{contract.name}»: исполнитель вызывает его у менеджера, "
                f"обмен остановится с ошибкой ({contract.evidence}).",
            )
            continue
        problem = _signature_problem(found, contract)
        if problem and contract.required:
            report.error(
                SIGNATURE,
                _address(addresses, found[0]),
                f"«{contract.name}»: {problem}; исполнитель не сможет вызвать метод "
                f"({contract.evidence}).",
            )
        elif problem:
            # Необязательный метод вызывается в Попытка с пустым Исключение: обмен идёт,
            # обработчик молча не выполняется.
            report.warning(
                SIGNATURE,
                _address(addresses, found[0]),
                f"«{contract.name}»: {problem}; исполнитель вызывает метод в «Попытка» и "
                f"подавляет ошибку — обработчик молча не выполняется ({contract.evidence}).",
            )
    return report


def _signature_problem(found: list[Routine], contract: RoutineContract) -> str | None:
    if len(found) > 1:
        return f"объявлен {len(found)} раз"
    routine = found[0]
    expected = contract.signature
    if not routine.exported:
        return "нет «Экспорт»"
    if expected.routine_kind == "function" and routine.routine_kind != "function":
        return "объявлена процедура, а исполнитель берёт результат функции"
    passed = len(expected.parameters)
    formals = routine.parameters
    if len(formals) < passed:
        return f"формальных параметров {len(formals)}, исполнитель передаёт {passed}"
    if any(item.default is None for item in formals[passed:]):
        return (
            f"формальных параметров {len(formals)}, исполнитель передаёт {passed}, "
            "у лишних нет значения по умолчанию"
        )
    return None


def _address(index: AddressIndex, entity: Entity) -> str:
    found = index.by_id.get(entity.entity_id, ())
    return found[0] if found else "Конвертация"
