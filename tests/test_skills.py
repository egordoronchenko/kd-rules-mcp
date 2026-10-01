"""Скиллы проекта: копия для Cursor совпадает с копией Claude, справочники скилла на месте."""

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CLAUDE = ROOT / ".claude" / "skills"
CURSOR = ROOT / ".cursor" / "skills"
SKILLS = sorted(path.name for path in CLAUDE.glob("kd2-*") if path.is_dir())
REFERENCE = re.compile(r"`(references/[^`]+\.md)`")


def _files(folder: Path) -> dict[str, bytes]:
    return {
        path.relative_to(folder).as_posix(): path.read_bytes()
        for path in sorted(folder.rglob("*"))
        if path.is_file()
    }


def test_skills_found() -> None:
    assert SKILLS


@pytest.mark.parametrize("skill", SKILLS)
def test_cursor_copy_matches(skill: str) -> None:
    claude, cursor = _files(CLAUDE / skill), _files(CURSOR / skill)
    assert sorted(claude) == sorted(cursor)
    differ = [name for name in claude if claude[name] != cursor[name]]
    assert differ == [], f"Копии для Cursor отличаются: {differ}"


@pytest.mark.parametrize("skill", SKILLS)
def test_references_are_linked(skill: str) -> None:
    folder = CLAUDE / skill
    text = (folder / "SKILL.md").read_text(encoding="utf-8")
    linked = set(REFERENCE.findall(text))
    present = {path.relative_to(folder).as_posix() for path in folder.glob("references/*.md")}
    assert sorted(linked - present) == [], "SKILL.md ссылается на несуществующие справочники"
    assert sorted(present - linked) == [], "Справочник не упомянут в SKILL.md"
