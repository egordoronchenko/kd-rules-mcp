"""Сборка упаковок знаний: копии справочников в скилле и установка скиллов в чужой проект."""

import json
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import build_packs  # noqa: E402 — скрипт из scripts/, не пакет
import our_skills  # noqa: E402 — скрипт из scripts/, не пакет

URL = "http://kd2.example:8061/mcp"


def _tree(folder: Path) -> set[str]:
    return {path.relative_to(folder).as_posix() for path in folder.rglob("*") if path.is_file()}


def _skill_files(prefix: str) -> set[str]:
    skills = build_packs.SKILLS
    return {
        f"{prefix}/{path.relative_to(skills).as_posix()}"
        for skill in our_skills.our_skills(skills)
        for path in skill.rglob("*")
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


def test_dest_claude_installs_skills_and_server(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(build_packs, "default_server_headers", lambda: None)
    (tmp_path / ".cursor").mkdir()
    other = {"mcpServers": {"proj-1c-code": {"type": "http", "url": "http://x/mcp"}}}
    (tmp_path / ".mcp.json").write_text(json.dumps(other), encoding="utf-8")
    (tmp_path / ".cursor" / "mcp.json").write_text(json.dumps({"mcpServers": {}}), encoding="utf-8")

    build_packs.install(tmp_path, "claude", URL)

    expected = _skill_files(".claude/skills") | {".mcp.json", ".cursor/mcp.json"}
    assert _tree(tmp_path) == expected
    claude = json.loads((tmp_path / ".mcp.json").read_text(encoding="utf-8"))["mcpServers"]
    assert claude["proj-1c-code"] == other["mcpServers"]["proj-1c-code"]
    assert claude["kd-rules-mcp"] == {"type": "http", "url": URL}
    cursor = json.loads((tmp_path / ".cursor" / "mcp.json").read_text(encoding="utf-8"))
    assert cursor["mcpServers"]["kd-rules-mcp"] == {"url": URL}
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
        ".claude/skills/kd-install/SKILL.md": b"uv run python scripts/setup_local.py",
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
            "См. https://github.com/egordoronchenko/kd-rules-mcp/blob/main/docs/workflow.md и "
            "`<папка проекта>\\.claude\\skills\\mcp-1c-tools\\docs\\<сервер>.md`; рабочая папка — "
            "`workspace` в `project_list`.\n"
        ).encode()
    }
    assert build_packs.portability_hits(fine) == []


def test_dest_agents_installs_skills_and_rules_entry(tmp_path: Path) -> None:
    report = build_packs.install(tmp_path, "agents", URL)

    expected = _skill_files(".agents/skills") | {"KD-RULES.md", ".mcp.json"}
    assert _tree(tmp_path) == expected
    assert not (tmp_path / ".claude").exists()
    assert any("AGENTS.md" in line for line in report), "строку в AGENTS.md добавляет человек"
    installed = {name: (tmp_path / name).read_bytes() for name in _tree(tmp_path)}
    assert build_packs.portability_hits(installed) == []


def test_rules_entry_is_short_and_points_into_pack() -> None:
    """KD-RULES.md — точка входа клиента без скиллов: короткий, каждый названный файл есть."""
    text = build_packs.RULES_ENTRY.read_text(encoding="utf-8")
    assert len(text.splitlines()) <= 60
    files = build_packs.pack_files("agents")
    named = set(re.findall(r"`(\.agents/skills/[^`]+)`", text))
    assert named
    # Файл — точное имя; папка (`…/`) или маска — хотя бы один файл под ней.
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
    report = build_packs.install(tmp_path, "claude", URL)
    assert not (tmp_path / ".cursor").exists()
    assert not (tmp_path / ".agents").exists()
    assert (
        "Cursor: `.cursor/mcp.json` в проекте нет — добавьте сервер "
        "в настройках MCP Cursor или запустите с `--cursor`"
    ) in report


def test_cursor_flag_copies_http_servers_and_skips_stdio(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """При создании `.cursor/mcp.json` HTTP-серверы переносятся, stdio — нет."""
    monkeypatch.setattr(build_packs, "default_server_headers", lambda: None)
    (tmp_path / ".mcp.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "code": {
                        "type": "http",
                        "url": "http://code/mcp",
                        "headers": {"Authorization": "Basic x"},
                    },
                    "meta": {"url": "http://meta/mcp"},
                    "local-stdio": {"command": "node", "args": ["srv.js"]},
                }
            }
        ),
        encoding="utf-8",
    )
    report = build_packs.install(tmp_path, "claude", URL, cursor=True)
    servers = json.loads((tmp_path / ".cursor" / "mcp.json").read_text(encoding="utf-8"))[
        "mcpServers"
    ]
    assert set(servers) == {"code", "meta", "kd-rules-mcp"}
    assert servers["code"] == {"url": "http://code/mcp", "headers": {"Authorization": "Basic x"}}
    assert "type" not in servers["code"]
    assert servers["meta"] == {"url": "http://meta/mcp"}
    assert servers["kd-rules-mcp"] == {"url": URL}
    assert "local-stdio" not in servers
    assert "перенесено серверов из .mcp.json: 2 (новых: code, meta)" in report
    assert "не перенесены: local-stdio" in report


def test_cursor_flag_adds_missing_servers_to_existing_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Существующий файл: недостающие HTTP дописываются, свои записи и заголовки нет."""
    headers = {"Authorization": "Bearer kept"}
    monkeypatch.setattr(build_packs, "default_server_headers", lambda: headers)
    kd2 = {"url": URL, "headers": headers}
    kept = {"url": "http://old/mcp", "headers": {"X-Custom": "1"}}
    (tmp_path / ".mcp.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "code": {
                        "type": "http",
                        "url": "http://code/mcp",
                        "headers": {"Authorization": "Basic x"},
                    },
                    "kept-1c": {"url": "http://new/mcp"},
                    "meta": {"url": "http://meta/mcp"},
                    "local-stdio": {"command": "node", "args": ["srv.js"]},
                    "kd-rules-mcp": {
                        "type": "http",
                        "url": "http://other/mcp",
                        "headers": {"Authorization": "Basic from-mcp"},
                    },
                }
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / ".cursor").mkdir()
    (tmp_path / ".cursor" / "mcp.json").write_text(
        json.dumps({"mcpServers": {"kd-rules-mcp": kd2, "kept-1c": kept}}),
        encoding="utf-8",
    )

    report = build_packs.install(tmp_path, "claude", URL, cursor=True)

    servers = json.loads((tmp_path / ".cursor" / "mcp.json").read_text(encoding="utf-8"))[
        "mcpServers"
    ]
    assert servers["kd-rules-mcp"] == kd2
    assert servers["kept-1c"] == kept
    assert servers["code"] == {"url": "http://code/mcp", "headers": {"Authorization": "Basic x"}}
    assert "type" not in servers["code"]
    assert servers["meta"] == {"url": "http://meta/mcp"}
    assert "local-stdio" not in servers
    assert "перенесено серверов из .mcp.json: 2 (новых: code, meta)" in report
    assert "не перенесены: local-stdio" in report


def test_cursor_flag_keeps_edited_url_and_reports_nothing_new(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Запись сервера 1С с другим url не заменяется; переносить нечего — отдельная строка."""
    monkeypatch.setattr(build_packs, "default_server_headers", lambda: None)
    kept = {"url": "http://old/mcp"}
    (tmp_path / ".mcp.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "code": {"type": "http", "url": "http://new/mcp"},
                    "local-stdio": {"command": "node", "args": ["srv.js"]},
                }
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / ".cursor").mkdir()
    (tmp_path / ".cursor" / "mcp.json").write_text(
        json.dumps({"mcpServers": {"kd-rules-mcp": {"url": URL}, "code": kept}}),
        encoding="utf-8",
    )

    report = build_packs.install(tmp_path, "claude", URL, cursor=True)

    servers = json.loads((tmp_path / ".cursor" / "mcp.json").read_text(encoding="utf-8"))[
        "mcpServers"
    ]
    assert servers["code"] == kept
    assert servers["kd-rules-mcp"] == {"url": URL}
    assert "серверы 1С в `.cursor/mcp.json` уже есть" in report
    assert "не перенесены: local-stdio" in report
    assert not any(line.startswith("перенесено серверов") for line in report)


def test_existing_cursor_without_flag_names_missing_servers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Без --cursor существующий файл не пополняется, недостающие серверы 1С называются."""
    monkeypatch.setattr(build_packs, "default_server_headers", lambda: None)
    (tmp_path / ".cursor").mkdir()
    (tmp_path / ".mcp.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "code": {"type": "http", "url": "http://code/mcp"},
                    "meta": {"url": "http://meta/mcp"},
                    "local-stdio": {"command": "node", "args": ["srv.js"]},
                }
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / ".cursor" / "mcp.json").write_text(
        json.dumps({"mcpServers": {"kd-rules-mcp": {"url": URL}}}),
        encoding="utf-8",
    )

    report = build_packs.install(tmp_path, "claude", URL)

    servers = json.loads((tmp_path / ".cursor" / "mcp.json").read_text(encoding="utf-8"))[
        "mcpServers"
    ]
    assert set(servers) == {"kd-rules-mcp"}
    assert servers["kd-rules-mcp"] == {"url": URL}
    assert (
        "Cursor: в `.cursor/mcp.json` нет серверов 1С: code, meta — "
        "запустите с `--cursor` или добавьте их в настройках MCP Cursor"
    ) in report
    hint = next(line for line in report if "нет серверов 1С" in line)
    assert "local-stdio" not in hint


def test_dest_cursor_flag_creates_cursor_mcp_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(build_packs, "default_server_headers", lambda: None)
    monkeypatch.setattr(
        sys,
        "argv",
        ["build_packs.py", "--dest", str(tmp_path), "--server-url", URL, "--cursor"],
    )
    build_packs.main()
    config = json.loads((tmp_path / ".cursor" / "mcp.json").read_text(encoding="utf-8"))
    assert config["mcpServers"] == {"kd-rules-mcp": {"url": URL}}


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


def _mcp_server(project: Path) -> dict[str, object]:
    config = json.loads((project / ".mcp.json").read_text(encoding="utf-8"))
    return config["mcpServers"]["kd-rules-mcp"]


def test_dest_copies_authorization_header_from_local_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Токен из projects.local.yaml попадает в запись; без токена ключа headers нет."""
    repo = tmp_path / "repo"
    repo.mkdir()
    local = repo / "projects.local.yaml"
    local.write_text("token: secret-token\n", encoding="utf-8")
    original_root = build_packs.ROOT
    real_headers = build_packs.default_server_headers

    def headers() -> dict[str, str] | None:
        # ROOT нужен сборке скиллов; подменяем его только на чтение локального токена.
        build_packs.ROOT = repo
        try:
            return real_headers()
        finally:
            build_packs.ROOT = original_root

    monkeypatch.setattr(build_packs, "default_server_headers", headers)

    project = tmp_path / "project"
    project.mkdir()
    bare = {"mcpServers": {"kd-rules-mcp": {"type": "http", "url": URL}}}
    (project / ".mcp.json").write_text(json.dumps(bare), encoding="utf-8")
    report = build_packs.install(project, "claude", URL)
    assert _mcp_server(project) == {
        "type": "http",
        "url": URL,
        "headers": {"Authorization": "Bearer secret-token"},
    }
    assert "заголовок Authorization добавлен" in report

    other = tmp_path / "other"
    other.mkdir()
    local.write_text("server_url: http://127.0.0.1:8060/mcp\n", encoding="utf-8")
    report = build_packs.install(other, "claude", URL)
    assert "headers" not in _mcp_server(other)
    assert "заголовок Authorization добавлен" not in report


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


def test_install_replaces_previous_server_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Прежний ключ сервера заменяется текущим и не остаётся второй записью."""
    monkeypatch.setattr(build_packs, "default_server_headers", lambda: None)
    old = build_packs.LEGACY_SERVER
    (tmp_path / ".mcp.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    old: {"type": "http", "url": "http://old/mcp"},
                    "proj-1c-code": {"type": "http", "url": "http://x/mcp"},
                }
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / ".cursor").mkdir()
    (tmp_path / ".cursor" / "mcp.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    old: {"url": "http://old/mcp"},
                    "proj-1c-code": {"url": "http://x/mcp"},
                }
            }
        ),
        encoding="utf-8",
    )

    build_packs.install(tmp_path, "claude", URL)

    for path in (tmp_path / ".mcp.json", tmp_path / ".cursor" / "mcp.json"):
        servers = json.loads(path.read_text(encoding="utf-8"))["mcpServers"]
        assert servers[build_packs.SERVER]["url"] == URL
        assert [name for name in servers if name in {old, build_packs.SERVER}] == [
            build_packs.SERVER
        ]
        assert servers["proj-1c-code"]["url"] == "http://x/mcp"


def test_install_removes_obsolete_marked_names(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Устаревшие папка и файл снимаются одной строкой, если это наша упаковка."""
    monkeypatch.setattr(build_packs, "default_server_headers", lambda: None)
    skill = "kd2-retired-pack"
    rules = "KD2-RETIRED.md"
    mark = "# Правила обмена КД 2 и сервер marker"
    monkeypatch.setattr(build_packs, "LEGACY_SKILL_NAMES", (skill,))
    monkeypatch.setattr(build_packs, "LEGACY_RULES_FILE", rules)
    monkeypatch.setattr(build_packs, "LEGACY_RULES_MARK", mark)
    folder = tmp_path / ".claude" / "skills" / skill
    folder.mkdir(parents=True)
    (folder / "SKILL.md").write_text(f"---\nname: {skill}\ndescription: d\n---\n", encoding="utf-8")
    unmarked = tmp_path / ".claude" / "skills" / "kd2-user-notes"
    unmarked.mkdir(parents=True)
    (unmarked / "SKILL.md").write_text("свои заметки\n", encoding="utf-8")
    (tmp_path / rules).write_text(mark + "\nдальше\n", encoding="utf-8")
    (tmp_path / "NOTES.md").write_text(mark + "\n", encoding="utf-8")
    foreign = tmp_path / ".claude" / "skills" / "project-skill"
    foreign.mkdir(parents=True)
    (foreign / "SKILL.md").write_text("x", encoding="utf-8")

    report = build_packs.install(tmp_path, "claude", URL)

    assert not folder.exists()
    assert not (tmp_path / rules).exists()
    assert (unmarked / "SKILL.md").is_file()
    assert (foreign / "SKILL.md").is_file()
    assert (tmp_path / "NOTES.md").is_file()
    assert [line for line in report if line.startswith("удалены устаревшие")] == [
        f"удалены устаревшие имена поставки: {skill}, {rules}"
    ]
