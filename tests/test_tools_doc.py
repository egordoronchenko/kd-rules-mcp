"""Справочник инструментов `docs/tools.md` совпадает с тем, что генерирует сервер."""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import build_packs  # noqa: E402 — скрипт из scripts/, не пакет
import dump_tools  # noqa: E402 — скрипт из scripts/, не пакет


def _pin_current_doc(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`DOC` — копия актуального документа, `render` отдаёт его генерируемую часть."""
    text = dump_tools.DOC.read_text(encoding="utf-8")
    doc = tmp_path / "tools.md"
    doc.write_text(text, encoding="utf-8")
    _, generated, _ = dump_tools.split_doc(text)
    monkeypatch.setattr(dump_tools, "DOC", doc)
    monkeypatch.setattr(dump_tools, "render", lambda: generated)


def test_tools_doc_is_up_to_date() -> None:
    _, current, _ = dump_tools.split_doc(dump_tools.DOC.read_text(encoding="utf-8"))
    assert current == dump_tools.render(), (
        "docs/tools.md устарел: uv run python scripts/dump_tools.py --write"
    )


def test_every_tool_has_a_group() -> None:
    grouped = [name for names in dump_tools.GROUPS.values() for name in names]
    assert len(grouped) == len(set(grouped))
    assert f"Инструментов: {len(grouped)}." in dump_tools.render()


def test_write_refreshes_skill_copies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _pin_current_doc(tmp_path, monkeypatch)
    copied = [".claude/skills/kd2-rules-build/references/tools.md"]
    calls: list[list[str]] = []

    def write_copies() -> list[str]:
        calls.append(copied)
        return copied

    monkeypatch.setattr(build_packs, "write_copies", write_copies)
    monkeypatch.setattr(sys, "argv", ["dump_tools.py", "--write"])
    dump_tools.main()
    assert calls == [copied]
    assert copied[0] in capsys.readouterr().out


def test_check_fails_when_skill_copy_is_stale(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _pin_current_doc(tmp_path, monkeypatch)
    monkeypatch.setattr(build_packs, "stale_copies", lambda: ["x/tools.md"])
    monkeypatch.setattr(sys, "argv", ["dump_tools.py", "--check"])
    with pytest.raises(SystemExit) as caught:
        dump_tools.main()
    assert caught.value.code == 1
    assert "x/tools.md" in capsys.readouterr().err


def test_check_passes_when_document_and_copies_are_current(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _pin_current_doc(tmp_path, monkeypatch)
    monkeypatch.setattr(build_packs, "stale_copies", lambda: [])
    monkeypatch.setattr(sys, "argv", ["dump_tools.py", "--check"])
    dump_tools.main()
