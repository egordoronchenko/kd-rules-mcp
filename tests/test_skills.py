"""Скиллы kd2-*: один источник в `.claude/skills`, без копий для клиентов; справочники на месте;
тексты, которые работают в папке проекта 1С, не ссылаются на этот репозиторий."""

import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import build_packs  # noqa: E402 — скрипт из scripts/, не пакет

SKILLS_DIR = ROOT / ".claude" / "skills"
SKILLS = sorted(path.name for path in SKILLS_DIR.glob("kd2-*") if path.is_dir())
REFERENCE = re.compile(r"`(references/[^`]+\.md)`")
OTHER_SKILL_REFERENCE = re.compile(r"`(kd2-[\w-]+/references/[^`]+\.md)`")
MARKDOWN_LINK = re.compile(r"\]\(([^)\s]+)\)")


def _texts(skill: str) -> dict[Path, str]:
    return {
        path: path.read_bytes().decode("utf-8")
        for path in sorted((SKILLS_DIR / skill).rglob("*.md"))
    }


def test_skills_found() -> None:
    assert SKILLS


def test_no_client_copies() -> None:
    """Cursor и OpenCode читают `.claude/skills` сами: вторая копия дала бы каждый скилл дважды."""
    copies = [
        path.relative_to(ROOT).as_posix()
        for folder in (ROOT / ".cursor" / "skills", ROOT / ".agents" / "skills")
        for path in folder.glob("kd2-*")
    ]
    assert copies == [], "Копии скиллов вне .claude/skills — источник один"
    assert not (ROOT / ".cursor" / "rules" / "mcp-1c.mdc").exists(), (
        "Правило серверов 1С — docs/rules/mcp-1c.md и его копия в скилле, без .mdc"
    )


@pytest.mark.parametrize("skill", SKILLS)
def test_references_are_linked(skill: str) -> None:
    folder = SKILLS_DIR / skill
    text = (folder / "SKILL.md").read_text(encoding="utf-8")
    linked = set(REFERENCE.findall(text))
    present = {path.relative_to(folder).as_posix() for path in folder.glob("references/*.md")}
    assert sorted(linked - present) == [], "SKILL.md ссылается на несуществующие справочники"
    assert sorted(present - linked) == [], "Справочник не упомянут в SKILL.md"


@pytest.mark.parametrize("skill", SKILLS)
def test_cross_skill_references_exist(skill: str) -> None:
    """`kd2-…/references/….md` из текстов скилла — существующие файлы соседних скиллов."""
    missing = sorted(
        {
            f"{path.name}: {name}"
            for path, text in _texts(skill).items()
            for name in OTHER_SKILL_REFERENCE.findall(text)
            if not (SKILLS_DIR / name).is_file()
        }
    )
    assert missing == []


@pytest.mark.parametrize("skill", SKILLS)
def test_relative_links_stay_in_skills(skill: str) -> None:
    """Относительные ссылки разрешаются внутри скиллов — в чужом проекте другого нет."""
    broken: list[str] = []
    for path, text in _texts(skill).items():
        for target in MARKDOWN_LINK.findall(text):
            if "://" in target or target.startswith(("#", "mailto:")):
                continue
            resolved = (path.parent / target.partition("#")[0]).resolve()
            if not resolved.is_relative_to(SKILLS_DIR) or not resolved.exists():
                broken.append(f"{path.relative_to(SKILLS_DIR).as_posix()}: {target}")
    assert broken == []


def test_skill_texts_are_portable() -> None:
    """В `kd2-rules-build` и `kd2-exchange-pitfalls` нет путей этого репозитория (`setup_local.py`,
    `scripts/`, `workspace/`, `docs/`, `src/kd2_rules_mcp`), а скрипты `kdbase` — с оговоркой
    «из клона сервера»: в чужом проекте репозитория нет."""
    assert build_packs.portability_hits(build_packs.pack_files("claude")) == []
