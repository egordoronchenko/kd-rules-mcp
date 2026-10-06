"""Общая проверка смыслового круга для валидации и сборки менеджера."""

from dataclasses import dataclass
from typing import Any

from .canonical import canonicalize
from .model import EdDocument
from .writer_import import import_manager
from .writer_model import ManagerModel


@dataclass(frozen=True, slots=True)
class ReadbackMismatch:
    address: str
    field: str

    @property
    def message(self) -> str:
        return "Модель повторного чтения не равна исходной; поле: " + self.field


def check_readback(model: ManagerModel, document: EdDocument) -> ReadbackMismatch | None:
    """Сравнивает все смысловые поля; тела остаются непрозрачным точным текстом."""
    back, _ = import_manager(
        document,
        project_id=model.project_id,
        manager_name=model.header.manager_name,
        host=model.host,
        format_bindings=model.format_bindings,
        executor_profile=model.executor_profile,
    )
    return first_mismatch(canonicalize(model), canonicalize(back))


def first_mismatch(
    before: Any, after: Any, address: str = "Конвертация", field: str = "model"
) -> ReadbackMismatch | None:
    """Выбирает первое различие в порядке полей модели, с адресом владельца."""
    if before == after:
        return None
    if isinstance(before, dict) and isinstance(after, dict):
        address = before.get("logical_id", address)
        for key in dict.fromkeys((*before, *after)):
            child_field = key if field == "model" or key not in ("value", "state") else field
            if key not in before or key not in after:
                return ReadbackMismatch(address, child_field)
            found = first_mismatch(before[key], after[key], address, child_field)
            if found:
                return found
    elif isinstance(before, list) and isinstance(after, list):
        for left, right in zip(before, after, strict=False):
            found = first_mismatch(left, right, address, field)
            if found:
                return found
        if len(before) != len(after):
            extra = before[len(after)] if len(before) > len(after) else after[len(before)]
            return ReadbackMismatch(
                extra.get("logical_id", address) if isinstance(extra, dict) else address, field
            )
    return ReadbackMismatch(address, field)
