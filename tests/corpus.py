"""Корпус реальных макетов правил обмена из XML-выгрузок конфигураций 1С.

Корпус задаётся переменной окружения `KD2_CORPUS_DIRS`: пути к каталогам XML-выгрузок
конфигураций (выгрузка Конфигуратора «в файлы»), разделённые `os.pathsep` (`;` на Windows,
`:` в остальных системах). В каждом каталоге ищутся макеты
`ExchangePlans/*/Templates/<вид>/Ext/Template.txt` видов `ПравилаОбмена`,
`ПравилаОбменаКорреспондента` и `ПравилаРегистрации`.

Имя конфигурации в идентификаторе теста — имя каталога выгрузки; безличные имена вроде
`Main`, `src`, `cf` заменяются именем ближайшего содержательного родителя.
Без переменной тесты корпуса пропускаются с причиной.
"""

import os
from dataclasses import dataclass
from pathlib import Path

import pytest

CORPUS_ENV = "KD2_CORPUS_DIRS"

EXCHANGE_KINDS = ("ПравилаОбмена", "ПравилаОбменаКорреспондента")
REGISTRATION_KINDS = ("ПравилаРегистрации",)
ALL_KINDS = EXCHANGE_KINDS + REGISTRATION_KINDS

# Безличные имена каталогов выгрузки: вместо них в идентификаторе берётся имя родителя.
_GENERIC_NAMES = frozenset(
    {"main", "src", "cf", "config", "configuration", "conf", "xml", "dump", "проект"}
)


@dataclass(frozen=True)
class CorpusFile:
    """Один макет правил корпуса."""

    project: str
    exchange_plan: str
    kind: str
    path: Path

    @property
    def id(self) -> str:
        """Короткий идентификатор для имени параметра теста."""
        return f"{self.project}/{self.exchange_plan}/{self.kind}"


def _label(path: Path) -> str:
    """Имя конфигурации для идентификатора: первый небезличный каталог снизу вверх."""
    for part in (path, *path.parents):
        if part.name and part.name.lower() not in _GENERIC_NAMES:
            return part.name
    return path.name or str(path)


def corpus_dirs() -> dict[str, Path]:
    """Каталоги выгрузок из `KD2_CORPUS_DIRS`: имя конфигурации → каталог, в порядке переменной."""
    result: dict[str, Path] = {}
    for raw in os.environ.get(CORPUS_ENV, "").split(os.pathsep):
        if not raw.strip():
            continue
        path = Path(raw.strip())
        label = base = _label(path)
        number = 2
        while label in result:
            label = f"{base}-{number}"
            number += 1
        result[label] = path
    return result


def missing_dirs() -> dict[str, Path]:
    """Заданные каталоги, в которых нет `ExchangePlans`."""
    return {
        label: path
        for label, path in corpus_dirs().items()
        if not (path / "ExchangePlans").is_dir()
    }


def corpus_files(kinds: tuple[str, ...] = ALL_KINDS) -> list[CorpusFile]:
    """Все доступные макеты правил заданных видов, в стабильном порядке."""
    found: list[CorpusFile] = []
    for label, root in corpus_dirs().items():
        plans = root / "ExchangePlans"
        if not plans.is_dir():
            continue
        for kind in kinds:
            for path in sorted(plans.glob(f"*/Templates/{kind}/Ext/Template.txt")):
                found.append(CorpusFile(label, path.parents[3].name, kind, path))
    return found


def corpus_params(kinds: tuple[str, ...] = ALL_KINDS) -> list:
    """Параметры для `pytest.mark.parametrize`: макеты корпуса и пропуски недоступных каталогов."""
    if not corpus_dirs():
        reason = f"Корпус не задан: переменная {CORPUS_ENV} пуста"
        return [pytest.param(None, id="корпус-не-задан", marks=pytest.mark.skip(reason=reason))]
    params: list = [pytest.param(item, id=item.id) for item in corpus_files(kinds)]
    for label, path in missing_dirs().items():
        reason = f"Корпус недоступен: нет каталога {path / 'ExchangePlans'}"
        params.append(pytest.param(None, id=f"{label}/недоступен", marks=pytest.mark.skip(reason)))
    if not params:
        reason = f"В каталогах {CORPUS_ENV} нет макетов видов {', '.join(kinds)}"
        params.append(pytest.param(None, id="нет-макетов", marks=pytest.mark.skip(reason=reason)))
    return params
