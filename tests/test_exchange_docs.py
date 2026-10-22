"""Инструкция и скиллы описывают папку обмена.

Изменение exchange-folder, требование «Инструкция выделяет папку обмена».
"""

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SKILLS = ROOT / ".claude" / "skills"

INSTALL_TEXTS = {
    "README.md": ROOT / "README.md",
    "docs/INSTALL.md": ROOT / "docs" / "INSTALL.md",
    "kd-install": SKILLS / "kd-install" / "references" / "run-and-connect.md",
}
SECOND_SIDE_SKILLS = ("kd2-rules-build", "kd3-rules")


def _text(path: Path) -> str:
    return path.read_bytes().decode("utf-8")


@pytest.mark.parametrize("name", sorted(INSTALL_TEXTS))
def test_exchange_docs_install_texts_describe_exchange_folder(name: str) -> None:
    text = _text(INSTALL_TEXTS[name])
    assert "папк" in text.lower() and "обмена" in text
    assert text.count("--side") >= 2, f"{name}: команда с двумя --side"
    assert "вне папок проектов баз" in text or "отдельную от папок проектов баз" in text, (
        f"{name}: не выделено, что папка обмена отдельно от проектов баз"
    )


def test_exchange_docs_kd_install_step_names_exchange_folder() -> None:
    text = _text(SKILLS / "kd-install" / "SKILL.md")
    assert "папка обмена вне проектов баз" in text


def test_exchange_docs_readme_links_install_section() -> None:
    readme = _text(ROOT / "README.md")
    install = _text(ROOT / "docs" / "INSTALL.md")
    assert "docs/INSTALL.md#папка-обмена-правите-обмен-двух-баз" in readme
    assert "### Папка обмена (правите обмен двух баз)" in install


@pytest.mark.parametrize("skill", SECOND_SIDE_SKILLS)
def test_exchange_docs_skills_have_second_side_section(skill: str) -> None:
    text = _text(SKILLS / skill / "SKILL.md")
    assert "Вторая сторона обмена" in text
    assert "KD-EXCHANGE.md" in text
    assert "references/second-side.md" in text
