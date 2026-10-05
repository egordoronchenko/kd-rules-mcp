"""Скрипт переименования проекта: замены, исключения, повторный запуск, --check."""

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import rename_project  # noqa: E402 — скрипт из scripts/, не пакет


def _write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))


def _git(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


def _tree(root: Path) -> None:
    _git(root, "init", "-q")
    _write(
        root,
        "pyproject.toml",
        'name = "kd2-rules-mcp"\n[project.scripts]\nkd2-rules-mcp = "kd2_rules_mcp:main"\n',
    )
    _write(
        root,
        "src/kd2_rules_mcp/__init__.py",
        'from kd2_rules_mcp.kd2.model import Kd2Error\n\nTOKEN = "KD2_TOKEN"\n',
    )
    _write(root, "src/kd2_rules_mcp/kd2/model.py", "class Kd2Error: ...\n")
    _write(root, "src/kd2_rules_mcp/kd2-ed-rules-note.txt", "kd2-ed-rules\n")
    _write(
        root,
        ".claude/skills/kd2-ed-rules/SKILL.md",
        "---\nname: kd2-ed-rules\n---\n\nСервер kd2-rules-mcp.\n",
    )
    _write(
        root,
        ".claude/skills/kd2-install/SKILL.md",
        "---\nname: kd2-install\n---\n",
    )
    _write(
        root,
        ".claude/skills/kd2-rules-build/SKILL.md",
        "---\nname: kd2-rules-build\n---\n\nсм. kd2-install и kd2-rules-mcp\n",
    )
    _write(root, "docs/rules/KD2-RULES.md", "Файл KD2-RULES.md и kd2-ed-rules.\n")
    _write(root, "docs/plans/old.md", "история kd2-rules-mcp\n")
    _write(root, "reference/old.xml", "эталон kd2_rules_mcp\n")
    _write(root, "openspec/old.md", "спека kd2-install\n")
    _write(
        root,
        "CHANGELOG.md",
        "\n".join(
            (
                "# Журнал",
                "",
                "https://github.com/egordoronchenko/kd2-rules-mcp/issues",
                "",
                "## [Unreleased]",
                "",
                "- сервер kd2-rules-mcp",
                (
                    "- Переименование `kd2-rules-mcp` → `kd-rules-mcp`: "
                    "заметка для уже установивших сервер — допишет принимающий."
                ),
                "",
                "## [0.1.0]",
                "",
                "- было kd2-rules-mcp",
                "",
            )
        ),
    )
    _write(
        root,
        "uv.lock",
        "\n".join(
            (
                "[[package]]",
                'name = "other"',
                'source = { registry = "https://pypi.org/simple" }',
                "# чужой kd2-rules-mcp",
                "",
                "[[package]]",
                'name = "kd2-rules-mcp"',
                'version = "0.1.0"',
                'source = { editable = "." }',
                "",
                "[[package]]",
                'name = "zzz"',
                "",
            )
        ),
    )
    _write(
        root,
        "README.md",
        "\n".join(
            (
                "Сервер kd2-rules-mcp.",
                "Заметка для уже установивших: ключ kd2-rules-mcp остаётся.",
                "",
            )
        ),
    )
    _write(
        root,
        "scripts/build_packs.py",
        'SERVER = "kd2-rules-mcp"\nLEGACY_SERVER = "kd2-rules-mcp"\n',
    )
    _write(
        root,
        "scripts/setup_local.py",
        "\n".join(
            (
                'SERVER = "kd2-rules-mcp"',
                'LEGACY_CONTAINER = "kd2_rules_mcp"',
                'LEGACY_COMPOSE_PROJECT = "kd2-rules-mcp"',
                "",
            )
        ),
    )
    _write(
        root,
        "src/kd2_rules_mcp/authoring/ed/identity.py",
        "\n".join(
            (
                'EXTENSION_IDENTITY_SEED = "kd2-rules-mcp/ed-authoring/v1/"',
                'LOGGER = "kd2_rules_mcp"',
                "",
            )
        ),
    )
    _write(root, "scripts/rename_project.py", "таблица kd2-rules-mcp\n")
    binary = root / "tests" / "data.bin"
    binary.parent.mkdir(parents=True, exist_ok=True)
    binary.write_bytes(b"kd2-rules-mcp\x00\xff")
    _git(root, "add", "-A")
    # Файл вне индекса: перенос обычным rename, не git mv.
    _write(root, "notes/kd2-install.txt", "не в git\n")


def test_replaces_table_and_keeps_exclusions(tmp_path: Path) -> None:
    _tree(tmp_path)
    report = rename_project.apply(tmp_path, write=True)

    assert (tmp_path / "pyproject.toml").read_text(encoding="utf-8") == (
        'name = "kd-rules-mcp"\n[project.scripts]\nkd-rules-mcp = "kd_rules_mcp:main"\n'
    )
    init = (tmp_path / "src/kd_rules_mcp/__init__.py").read_text(encoding="utf-8")
    assert "kd_rules_mcp.kd2.model" in init
    assert "Kd2Error" in init
    assert "KD2_TOKEN" in init
    assert not (tmp_path / "src/kd2_rules_mcp").exists()
    assert (tmp_path / "src/kd_rules_mcp/kd2/model.py").is_file()
    assert (tmp_path / "src/kd_rules_mcp/kd3-rules-note.txt").read_text(encoding="utf-8") == (
        "kd3-rules\n"
    )
    skill = tmp_path / ".claude/skills/kd3-rules/SKILL.md"
    assert "name: kd3-rules" in skill.read_text(encoding="utf-8")
    assert (tmp_path / ".claude/skills/kd-install/SKILL.md").is_file()
    kept = (tmp_path / ".claude/skills/kd2-rules-build/SKILL.md").read_text(encoding="utf-8")
    assert "name: kd2-rules-build" in kept
    assert "kd-install" in kept
    assert "kd-rules-mcp" in kept
    assert (tmp_path / "docs/rules/KD-RULES.md").read_text(encoding="utf-8") == (
        "Файл KD-RULES.md и kd3-rules.\n"
    )
    assert (tmp_path / "docs/plans/old.md").read_text(encoding="utf-8") == "история kd2-rules-mcp\n"
    assert (tmp_path / "reference/old.xml").read_text(encoding="utf-8") == "эталон kd2_rules_mcp\n"
    assert (tmp_path / "openspec/old.md").read_text(encoding="utf-8") == "спека kd2-install\n"
    changelog = (tmp_path / "CHANGELOG.md").read_text(encoding="utf-8")
    assert "github.com/egordoronchenko/kd-rules-mcp/issues" in changelog
    assert "- сервер kd-rules-mcp" in changelog
    assert "допишет принимающий" in changelog
    assert "`kd2-rules-mcp` → `kd-rules-mcp`" in changelog
    assert "- было kd2-rules-mcp" in changelog
    lock = (tmp_path / "uv.lock").read_text(encoding="utf-8")
    assert 'name = "kd-rules-mcp"' in lock
    assert "# чужой kd2-rules-mcp" in lock
    readme = (tmp_path / "README.md").read_text(encoding="utf-8")
    assert readme.splitlines()[0] == "Сервер kd-rules-mcp."
    assert "ключ kd2-rules-mcp остаётся" in readme
    packs = (tmp_path / "scripts/build_packs.py").read_text(encoding="utf-8")
    assert packs == 'SERVER = "kd-rules-mcp"\nLEGACY_SERVER = "kd2-rules-mcp"\n'
    local = (tmp_path / "scripts/setup_local.py").read_text(encoding="utf-8")
    assert 'SERVER = "kd-rules-mcp"' in local
    assert 'LEGACY_CONTAINER = "kd2_rules_mcp"' in local
    assert 'LEGACY_COMPOSE_PROJECT = "kd2-rules-mcp"' in local
    identity = (tmp_path / "src/kd_rules_mcp/authoring/ed/identity.py").read_text(encoding="utf-8")
    assert 'EXTENSION_IDENTITY_SEED = "kd2-rules-mcp/ed-authoring/v1/"' in identity
    assert 'LOGGER = "kd_rules_mcp"' in identity
    assert "kd2-rules-mcp" in (tmp_path / "scripts/rename_project.py").read_text(encoding="utf-8")
    assert (tmp_path / "tests/data.bin").read_bytes() == b"kd2-rules-mcp\x00\xff"
    assert (tmp_path / "notes/kd-install.txt").is_file()
    listed = set(_git(tmp_path, "ls-files").stdout.splitlines())
    assert "src/kd_rules_mcp/__init__.py" in listed
    assert "src/kd2_rules_mcp/__init__.py" not in listed
    assert ".claude/skills/kd3-rules/SKILL.md" in listed
    assert "docs/rules/KD-RULES.md" in listed
    assert "notes/kd-install.txt" not in listed
    assert report.files_moved > 0
    assert report.lines_changed > 0
    assert report.pending == []


def test_second_run_changes_nothing(tmp_path: Path) -> None:
    _tree(tmp_path)
    rename_project.apply(tmp_path, write=True)
    snapshot = {
        path.relative_to(tmp_path).as_posix(): path.read_bytes()
        for path in tmp_path.rglob("*")
        if path.is_file() and ".git" not in path.parts
    }
    again = rename_project.apply(tmp_path, write=True)
    assert again.lines_changed == 0
    assert again.files_moved == 0
    assert again.pending == []
    current = {
        path.relative_to(tmp_path).as_posix(): path.read_bytes()
        for path in tmp_path.rglob("*")
        if path.is_file() and ".git" not in path.parts
    }
    assert current == snapshot


def test_check_fails_before_write_and_passes_after(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _tree(tmp_path)
    before = rename_project.apply(tmp_path, write=False)
    assert any(hit.startswith("pyproject.toml:") for hit in before.pending)
    assert any("docs/rules/KD2-RULES.md" in hit for hit in before.pending)
    assert not any(hit.startswith("docs/plans/") for hit in before.pending)
    assert not any(hit.startswith("reference/") for hit in before.pending)
    assert not any(hit.startswith("openspec/") for hit in before.pending)
    reasons = [item.reason for item in rename_project.ALLOWANCES]
    text = "\n".join(reasons)
    assert "история" in text
    assert "заметка о переименовании в README" in text
    assert "заметка о переименовании в CHANGELOG" in text
    assert "совместимость установки" in text
    assert "зерно идентификаторов расширений" in text
    assert "прежний контейнер" in text

    monkeypatch.setattr(rename_project, "repo_root", lambda: tmp_path)
    monkeypatch.setattr(sys, "argv", ["rename_project.py", "--check"])
    with pytest.raises(SystemExit) as exc:
        rename_project.main()
    assert exc.value.code == 1

    rename_project.apply(tmp_path, write=True)
    after = rename_project.apply(tmp_path, write=False)
    assert after.pending == []
    monkeypatch.setattr(sys, "argv", ["rename_project.py", "--check"])
    rename_project.main()
