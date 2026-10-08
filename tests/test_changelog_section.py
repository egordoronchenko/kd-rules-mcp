"""Тело релиза точно соответствует разделу версии, без соседних разделов."""

from pathlib import Path

import pytest

from scripts.changelog_section import changelog_section, main


def test_section_boundaries() -> None:
    text = (
        "# Журнал\n\n## [Unreleased]\nновое\n\n## [1.2.0] — дата\n\n"
        "### Добавлено\n- факт\n\n## [1.1.0]\nстарое\n"
    )
    assert changelog_section(text, "v1.2.0") == "### Добавлено\n- факт\n"
    assert changelog_section(text, "1.1.0") == "старое\n"


def test_fenced_heading_is_not_a_section() -> None:
    text = "## [1.0.0]\n```markdown\n## [0.9.0]\n```\n\n## [0.9.0]\nстарое\n"
    assert changelog_section(text, "1.0.0") == "```markdown\n## [0.9.0]\n```\n"


@pytest.mark.parametrize(
    "text", ["## [2.0.0]\nтекст", "## [1.0.0]\n", "## [1.0.0]\nа\n## [1.0.0]\nб"]
)
def test_missing_empty_or_duplicate_section(text: str) -> None:
    with pytest.raises(ValueError):
        changelog_section(text, "1.0.0")


def test_cli_writes_utf8_lf(tmp_path: Path) -> None:
    source = tmp_path / "CHANGELOG.md"
    target = tmp_path / "notes.md"
    source.write_bytes("## [1.2.3]\r\n\r\n- Новое\r\n".encode())
    main(["v1.2.3", "--changelog", str(source), "--output", str(target)])
    assert target.read_bytes() == "- Новое\n".encode()


def test_cli_refuses_missing_section(tmp_path: Path) -> None:
    source = tmp_path / "CHANGELOG.md"
    source.write_bytes(b"## [Unreleased]\nwork\n")
    with pytest.raises(SystemExit) as error:
        main(["v1.2.3", "--changelog", str(source)])
    assert error.value.code == 2
