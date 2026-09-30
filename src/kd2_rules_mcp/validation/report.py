"""Отчёт проверки: замечания с уровнем, адресом правила и текстом, плюс невыполненные проверки
(спецификация `rules-validation`, «Отчёт проверки»)."""

from dataclasses import dataclass, field
from enum import StrEnum


class Level(StrEnum):
    """Уровень замечания."""

    ERROR = "ошибка"
    WARNING = "предупреждение"


@dataclass(frozen=True, slots=True)
class Issue:
    """Замечание проверки.

    `check` — идентификатор проверки (`format.unknown_tag`, `structure.pks_target` …), по нему
    замечания группируются и фильтруются; `address` — адрес правила (модуль `address`).
    """

    level: Level
    check: str
    address: str
    message: str

    def to_dict(self) -> dict[str, str]:
        """Компактное представление для ответа инструмента."""
        return {
            "level": self.level.value,
            "check": self.check,
            "address": self.address,
            "message": self.message,
        }


@dataclass(frozen=True, slots=True)
class Skipped:
    """Проверка, которую не удалось выполнить (например, нет структуры приёмника)."""

    check: str
    reason: str

    def to_dict(self) -> dict[str, str]:
        """Компактное представление для ответа инструмента."""
        return {"check": self.check, "reason": self.reason}


@dataclass(slots=True)
class ValidationReport:
    """Результат одной или нескольких проверок."""

    issues: list[Issue] = field(default_factory=list)
    skipped: list[Skipped] = field(default_factory=list)

    def error(self, check: str, address: str, message: str) -> None:
        """Добавляет ошибку."""
        self.issues.append(Issue(Level.ERROR, check, address, message))

    def warning(self, check: str, address: str, message: str) -> None:
        """Добавляет предупреждение."""
        self.issues.append(Issue(Level.WARNING, check, address, message))

    def skip(self, check: str, reason: str) -> None:
        """Отмечает проверку как невыполненную."""
        self.skipped.append(Skipped(check, reason))

    def extend(self, other: "ValidationReport") -> None:
        """Добавляет замечания и невыполненные проверки другого отчёта."""
        self.issues.extend(other.issues)
        self.skipped.extend(other.skipped)

    @property
    def errors(self) -> list[Issue]:
        """Только ошибки."""
        return [issue for issue in self.issues if issue.level is Level.ERROR]

    @property
    def warnings(self) -> list[Issue]:
        """Только предупреждения."""
        return [issue for issue in self.issues if issue.level is Level.WARNING]

    @property
    def has_errors(self) -> bool:
        """Есть ли ошибки."""
        return any(issue.level is Level.ERROR for issue in self.issues)

    def summary(self) -> str:
        """Итог одной строкой; при невыполненных проверках — с оговоркой о неполноте."""
        errors, warnings = len(self.errors), len(self.warnings)
        if errors:
            text = f"Ошибок: {errors}, предупреждений: {warnings}"
        elif warnings:
            text = f"Ошибок нет, предупреждений: {warnings}"
        else:
            text = "Ошибок и предупреждений нет"
        if self.skipped:
            checks = ", ".join(sorted({item.check for item in self.skipped}))
            text += f"; не выполнены проверки: {checks} — результат неполный"
        return text

    def counts(self) -> dict[str, int]:
        """Число замечаний по идентификаторам проверок."""
        result: dict[str, int] = {}
        for issue in self.issues:
            result[issue.check] = result.get(issue.check, 0) + 1
        return result
