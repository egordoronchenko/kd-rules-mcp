"""Какие папки скиллов относятся к этому серверу.

Имя: `kd`, необязательная цифра 2 или 3, дефис и хвост (`kd2-rules-build`, `kd-install`,
`kd3-rules`). Чужие папки (скиллы других инструментов) не совпадают:
сравнение — со всем именем каталога, не с куском пути.
"""

import re
from pathlib import Path

# Без `\Z` — чтобы вставлять в более длинные шаблоны (ссылка на справочник соседнего скилла).
SKILL_NAME = r"kd[23]?-[\w-]+"
SKILL_NAME_RE = re.compile(rf"{SKILL_NAME}\Z")


def is_our_skill(name: str) -> bool:
    """Имя каталога — скилл этого сервера, а не чужая папка рядом."""
    return SKILL_NAME_RE.fullmatch(name) is not None


def our_skills(folder: Path) -> list[Path]:
    """Каталоги скиллов сервера прямо в `folder` (нет папки — пустой список)."""
    if not folder.is_dir():
        return []
    return sorted(path for path in folder.iterdir() if path.is_dir() and is_our_skill(path.name))
