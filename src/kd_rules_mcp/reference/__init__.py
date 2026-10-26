"""Справка и поиск по знаниям сервера (спецификация `reference-help`).

Тексты живут только в файлах скиллов сервера (`.claude/skills/kd-*`, `kd2-*`, `kd3-*`): разделы
справки — адреса заголовков в этих файлах (`sections`), поиск — индекс FTS5 в памяти по их строкам
(`search`). Второй копии текста в коде нет; это проверяет `tests/test_reference.py`.
"""

import re
from pathlib import Path

from kd_rules_mcp.errors import ReferenceUnavailableError

# То же правило имён, что `scripts/our_skills.py`: `kd`, необязательная цифра 2 или 3, дефис.
SKILL_NAME_RE = re.compile(r"kd[23]?-[\w-]+\Z")


def our_skills(root: Path) -> list[Path]:
    """Каталоги скиллов сервера в папке скиллов, по имени."""
    if not root.is_dir():
        return []
    return sorted(p for p in root.iterdir() if p.is_dir() and SKILL_NAME_RE.fullmatch(p.name))


def resolve_root(configured: Path | None) -> Path:
    """Папка скиллов: `KD2_REFERENCE_DIR`, иначе рядом с исходниками или в текущей папке.

    Пакет в образе ставится не в режиме редактирования, поэтому путь от `__file__` там не ведёт
    к скиллам; образ задаёт `KD2_REFERENCE_DIR=/app/.claude/skills`.
    """
    if configured is not None:
        candidates = [configured]
    else:
        candidates = [
            Path(__file__).resolve().parents[3] / ".claude" / "skills",
            Path.cwd() / ".claude" / "skills",
        ]
    for candidate in candidates:
        if our_skills(candidate):
            return candidate
    raise ReferenceUnavailableError(
        "Справочники скиллов сервера не найдены: "
        + ", ".join(str(c) for c in candidates)
        + " (задайте KD2_REFERENCE_DIR — папку .claude/skills сервера)"
    )


def normalize(text: str) -> str:
    """Нижний регистр и `ё` → `е`; длина строки не меняется (позиции совпадают с исходной)."""
    lowered = text.lower()
    if len(lowered) != len(text):  # редкие символы, у которых нижний регистр длиннее
        lowered = "".join(ch.lower() if len(ch.lower()) == 1 else ch for ch in text)
    return lowered.replace("ё", "е")
