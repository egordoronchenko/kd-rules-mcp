"""Сборка упаковок знаний: копии справочников в скилле и установка скиллов в чужой проект."""

import json
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import build_packs  # noqa: E402 — скрипт из scripts/, не пакет

URL = "http://kd2.example:8061/mcp"


def _tree(folder: Path) -> set[str]:
    return {path.relative_to(folder).as_posix() for path in folder.rglob("*") if path.is_file()}


def _skill_files(prefix: str) -> set[str]:
    skills = build_packs.SKILLS
    return {
        f"{prefix}/{path.relative_to(skills).as_posix()}"
        for path in skills.glob("kd2-*/**/*")
        if path.is_file() and "__pycache__" not in path.parts
    }


def test_copies_are_up_to_date() -> None:
    assert build_packs.stale_copies() == [], "uv run python scripts/build_packs.py --write"


def test_copies_link_only_to_pack_or_public_repo() -> None:
    names = set(build_packs.COPIES.values())
    for text in build_packs.rendered_copies().values():
        for target in build_packs.LINK.findall(text):
            if target.startswith(("#", build_packs.REPO_URL)):
                continue
            assert target.partition("#")[0] in names, target


def test_dest_claude_installs_skills_and_server(tmp_path: Path) -> None:
    (tmp_path / ".cursor").mkdir()
    other = {"mcpServers": {"proj-1c-code": {"type": "http", "url": "http://x/mcp"}}}
    (tmp_path / ".mcp.json").write_text(json.dumps(other), encoding="utf-8")
    (tmp_path / ".cursor" / "mcp.json").write_text(json.dumps({"mcpServers": {}}), encoding="utf-8")

    build_packs.install(tmp_path, "claude", URL)

    expected = _skill_files(".claude/skills") | {".mcp.json", ".cursor/mcp.json"}
    assert _tree(tmp_path) == expected
    claude = json.loads((tmp_path / ".mcp.json").read_text(encoding="utf-8"))["mcpServers"]
    assert claude["proj-1c-code"] == other["mcpServers"]["proj-1c-code"]
    assert claude["kd2-rules-mcp"] == {"type": "http", "url": URL}
    cursor = json.loads((tmp_path / ".cursor" / "mcp.json").read_text(encoding="utf-8"))
    assert cursor["mcpServers"]["kd2-rules-mcp"] == {"url": URL}
    copy = tmp_path / ".claude/skills/kd2-rules-build/references/mcp-1c.md"
    assert copy.read_bytes() == build_packs.render_copy("docs/rules/mcp-1c.md").encode("utf-8")


def test_installed_pack_has_no_repository_paths(tmp_path: Path) -> None:
    build_packs.install(tmp_path, "claude", URL)
    installed = {name: (tmp_path / name).read_bytes() for name in _tree(tmp_path)}
    assert build_packs.portability_hits(installed) == []


def test_portability_check_catches_repository_paths() -> None:
    broken = {
        ".claude/skills/kd2-rules-build/SKILL.md": (
            "Сервер из `.mcp.json` (его пишет `scripts/setup_local.py`), пишет в `workspace\\`.\n"
            "Правило — `docs/rules/mcp-1c.md`. Проверка — `uv run python kdbase/kd_check.py`.\n"
        ).encode(),
        ".claude/skills/kd2-install/SKILL.md": b"uv run python scripts/setup_local.py",
        ".claude/skills/kd2-rules-build/references/checks.md": b"kdbase\\kd_check.py docs/x",
    }
    hits = build_packs.portability_hits(broken)
    assert all(hit.startswith(".claude/skills/kd2-rules-build/SKILL.md") for hit in hits)
    assert {hit.rsplit(": ", 1)[1] for hit in hits} >= {
        "setup_local.py",
        "scripts/",
        "workspace/",
        "docs/",
    }
    assert any("kdbase" in hit for hit in hits)


def test_portability_check_allows_urls_and_project_paths() -> None:
    fine = {
        ".claude/skills/kd2-exchange-pitfalls/SKILL.md": (
            "См. https://github.com/egordoronchenko/kd2-rules-mcp/blob/main/docs/workflow.md и "
            "`<папка проекта>\\.claude\\skills\\mcp-1c-tools\\docs\\<сервер>.md`; рабочая папка — "
            "`workspace` в `project_list`.\n"
        ).encode()
    }
    assert build_packs.portability_hits(fine) == []


def test_dest_agents_installs_skills_and_rules_entry(tmp_path: Path) -> None:
    report = build_packs.install(tmp_path, "agents", URL)

    expected = _skill_files(".agents/skills") | {"KD2-RULES.md", ".mcp.json"}
    assert _tree(tmp_path) == expected
    assert not (tmp_path / ".claude").exists()
    assert any("AGENTS.md" in line for line in report), "строку в AGENTS.md добавляет человек"
    installed = {name: (tmp_path / name).read_bytes() for name in _tree(tmp_path)}
    assert build_packs.portability_hits(installed) == []


def test_rules_entry_is_short_and_points_into_pack() -> None:
    """KD2-RULES.md — точка входа клиента без скиллов: короткий, каждый названный файл есть."""
    text = build_packs.RULES_ENTRY.read_text(encoding="utf-8")
    assert len(text.splitlines()) <= 60
    files = build_packs.pack_files("agents")
    named = set(re.findall(r"`(\.agents/skills/[^`]+)`", text))
    assert named
    # Файл — точное имя; папка (`…/`) или маска (`kd2-*`) — хотя бы один файл под ней.
    missing = [name for name in named if not any(f.startswith(name.rstrip("*")) for f in files)]
    assert sorted(missing) == []
    references = {
        name.rsplit("/", 1)[1]
        for name in files
        if name.startswith(".agents/skills/kd2-rules-build/references/")
    }
    short = set(re.findall(r"`(?:references/)?([\w-]+\.md)`", text)) - {"SKILL.md"}
    assert sorted(short - references) == []


def test_dest_without_cursor_config_writes_only_mcp_json(tmp_path: Path) -> None:
    build_packs.install(tmp_path, "claude", URL)
    assert not (tmp_path / ".cursor").exists()
    assert not (tmp_path / ".agents").exists()


def test_dest_reinstall_removes_stale_files_only_in_own_skills(tmp_path: Path) -> None:
    stale = tmp_path / ".claude/skills/kd2-rules-build/references/old.md"
    foreign = tmp_path / ".claude/skills/project-skill/SKILL.md"
    for path in (stale, foreign):
        path.parent.mkdir(parents=True)
        path.write_text("x", encoding="utf-8")

    report = build_packs.install(tmp_path, "claude", URL)

    assert not stale.exists()
    assert foreign.is_file()
    assert any("old.md" in line for line in report)


def test_default_server_url_is_an_address() -> None:
    assert build_packs.default_server_url().startswith(("http://", "https://"))


def test_dest_refuses_second_package(tmp_path: Path) -> None:
    (tmp_path / ".agents/skills/kd2-rules-build").mkdir(parents=True)
    with pytest.raises(SystemExit, match="agents"):
        build_packs.install(tmp_path, "claude", URL)


def test_dest_refuses_server_repository() -> None:
    with pytest.raises(SystemExit):
        build_packs.install(ROOT, "claude", URL)


def test_dest_keeps_broken_mcp_json(tmp_path: Path) -> None:
    (tmp_path / ".mcp.json").write_text("{не json", encoding="utf-8")
    with pytest.raises(SystemExit, match="не тронут"):
        build_packs.install(tmp_path, "claude", URL)
    assert (tmp_path / ".mcp.json").read_text(encoding="utf-8") == "{не json"
