"""Сборка упаковок знаний: копии справочников в скилле, установка в проект и папка обмена."""

import hashlib
import json
import os
import re
import sys
from collections.abc import Mapping
from pathlib import Path

import pytest
import yaml

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
    # KD-EXCHANGE.md — не справочник, а файл в корне папки обмена (его пишет установка).
    short = set(re.findall(r"`(?:references/)?([\w-]+\.md)`", text)) - {
        "SKILL.md",
        "KD-EXCHANGE.md",
    }
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


def _no_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """Токен из projects.local.yaml не подмешивается в запись папки обмена."""
    monkeypatch.setattr(build_packs, "default_server_headers", lambda: None)


def _side_project(folder: Path, servers: Mapping[str, object] | None = None) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    if servers is not None:
        body = json.dumps({"mcpServers": servers}, ensure_ascii=False, indent=2) + "\n"
        (folder / ".mcp.json").write_bytes(body.encode("utf-8"))
    return folder


def _fingerprints(folder: Path) -> dict[str, tuple[str, int]]:
    """Содержимое (sha256) и st_mtime_ns каждого файла папки."""
    found: dict[str, tuple[str, int]] = {}
    for path in folder.rglob("*"):
        if not path.is_file():
            continue
        stat = path.stat()
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        found[path.relative_to(folder).as_posix()] = (digest, stat.st_mtime_ns)
    return found


def _servers(folder: Path) -> dict[str, object]:
    return json.loads((folder / ".mcp.json").read_text(encoding="utf-8"))["mcpServers"]


def test_exchange_side_two_sides_assembles(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Две стороны: скиллы, kd-rules-mcp, серверы обеих сторон и KD-EXCHANGE.md."""
    _no_token(monkeypatch)
    bp = _side_project(tmp_path / "bp", {"1c-data-mcp": {"type": "http", "url": "http://bp/mcp"}})
    do = _side_project(tmp_path / "do", {"1c-data-mcp": {"type": "http", "url": "http://do/mcp"}})
    dest = tmp_path / "exchange"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "build_packs.py",
            "--dest",
            str(dest),
            "--client",
            "claude",
            "--server-url",
            URL,
            "--side",
            f"bp={bp}",
            "--side",
            f"do={do}",
        ],
    )
    build_packs.main()
    assert _skill_files(".claude/skills") <= _tree(dest)
    assert not (dest / ".cursor").exists()
    assert _servers(dest)["kd-rules-mcp"] == {"type": "http", "url": URL}
    assert _servers(dest)["bp-1c-data-mcp"] == {"url": "http://bp/mcp", "type": "http"}
    assert _servers(dest)["do-1c-data-mcp"] == {"url": "http://do/mcp", "type": "http"}
    text = (dest / "KD-EXCHANGE.md").read_text(encoding="utf-8")
    assert text.startswith("# Папка обмена: bp ↔ do\n")


def test_exchange_side_one_refuses(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    dest = tmp_path / "nested" / "exchange"
    bp = _side_project(tmp_path / "bp", {})
    monkeypatch.setattr(sys, "argv", ["build_packs.py", "--dest", str(dest), "--side", f"bp={bp}"])
    with pytest.raises(SystemExit, match="нужны ровно две стороны"):
        build_packs.main()
    assert not dest.exists()
    assert not (tmp_path / "nested").exists()
    with pytest.raises(SystemExit, match="нужны ровно две стороны"):
        build_packs.install_exchange(dest, [("bp", bp), ("do", bp), ("x", bp)], "claude", URL)
    assert not (tmp_path / "nested").exists()


def test_exchange_side_cyrillic_name_refuses(tmp_path: Path) -> None:
    dest = tmp_path / "exchange"
    bp = tmp_path / "bp"
    do = tmp_path / "do"
    with pytest.raises(SystemExit, match="недопустимое имя стороны «БП»"):
        build_packs.install_exchange(dest, [("БП", bp), ("do", do)], "claude", URL)
    assert not dest.exists()


def test_exchange_side_same_names_refuse(tmp_path: Path) -> None:
    dest = tmp_path / "exchange"
    bp = _side_project(tmp_path / "bp", {})
    do = _side_project(tmp_path / "do", {})
    before_bp = _fingerprints(bp)
    before_do = _fingerprints(do)
    with pytest.raises(SystemExit, match="одинаковое имя стороны «bp»"):
        build_packs.install_exchange(dest, [("bp", bp), ("bp", do)], "claude", URL)
    assert not dest.exists()
    assert _fingerprints(bp) == before_bp
    assert _fingerprints(do) == before_do


def test_exchange_side_missing_folder_refuses(tmp_path: Path) -> None:
    dest = tmp_path / "exchange"
    bp = _side_project(tmp_path / "bp", {})
    (bp / "keep.txt").write_bytes(b"ok\n")
    missing = tmp_path / "no-do"
    before = _fingerprints(bp)
    with pytest.raises(SystemExit, match="нет папки стороны do") as caught:
        build_packs.install_exchange(dest, [("bp", bp), ("do", missing)], "claude", URL)
    assert str(missing) in str(caught.value)
    assert not dest.exists()
    assert _fingerprints(bp) == before


def test_exchange_side_broken_side_mcp_refuses(tmp_path: Path) -> None:
    dest = tmp_path / "exchange"
    bp = _side_project(tmp_path / "bp")
    broken = bp / ".mcp.json"
    broken.write_bytes(b"{")
    do = _side_project(tmp_path / "do", {})
    before_do = _fingerprints(do)
    with pytest.raises(SystemExit, match="не JSON") as caught:
        build_packs.install_exchange(dest, [("bp", bp), ("do", do)], "claude", URL)
    assert str(broken.resolve()) in str(caught.value)
    assert broken.read_bytes() == b"{"
    assert not dest.exists()
    assert _fingerprints(do) == before_do


def test_exchange_side_broken_dest_mcp_refuses(tmp_path: Path) -> None:
    dest = tmp_path / "exchange"
    dest.mkdir()
    (dest / ".mcp.json").write_bytes(b"{")
    (dest / "keep.txt").write_bytes(b"keep\n")
    bp = _side_project(tmp_path / "bp", {"a": {"url": "http://bp/mcp"}})
    do = _side_project(tmp_path / "do", {"b": {"url": "http://do/mcp"}})
    before_dest = _fingerprints(dest)
    before_bp = _fingerprints(bp)
    before_do = _fingerprints(do)
    with pytest.raises(SystemExit, match="не JSON"):
        build_packs.install_exchange(dest, [("bp", bp), ("do", do)], "claude", URL)
    assert _fingerprints(dest) == before_dest
    assert _fingerprints(bp) == before_bp
    assert _fingerprints(do) == before_do
    assert not (dest / ".claude").exists()


def test_exchange_side_bad_token_refuses(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    dest = tmp_path / "exchange"
    monkeypatch.setattr(
        sys,
        "argv",
        ["build_packs.py", "--dest", str(dest), "--side", "bp", "--side", f"do={tmp_path}"],
    )
    with pytest.raises(SystemExit, match="имя=папка"):
        build_packs.main()
    assert not dest.exists()


def test_exchange_side_without_dest_is_argparse_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    commands = (
        ["build_packs.py", "--side", "bp=C:/nope"],
        ["build_packs.py", "--check", "--side", "bp=C:/nope"],
        ["build_packs.py", "--write", "--side", "bp=C:/nope"],
    )
    for argv in commands:
        monkeypatch.setattr(sys, "argv", argv)
        with pytest.raises(SystemExit) as caught:
            build_packs.main()
        assert caught.value.code == 2
        assert "--dest" in capsys.readouterr().err


def test_exchange_separate_inside_project_refuses(tmp_path: Path) -> None:
    bp = _side_project(tmp_path / "bp", {"a": {"url": "http://bp/mcp"}})
    (bp / "keep.txt").write_bytes(b"bp\n")
    do = _side_project(tmp_path / "do", {"b": {"url": "http://do/mcp"}})
    (do / "keep.txt").write_bytes(b"do\n")
    dest = bp / "Обмен"
    before_bp = _fingerprints(bp)
    before_do = _fingerprints(do)
    with pytest.raises(SystemExit, match="папка обмена должна быть вне папок проектов"):
        build_packs.install_exchange(dest, [("bp", bp), ("do", do)], "claude", URL)
    assert not dest.exists()
    assert _fingerprints(bp) == before_bp
    assert _fingerprints(do) == before_do


def test_exchange_separate_project_inside_refuses(tmp_path: Path) -> None:
    work = tmp_path / "work"
    work.mkdir()
    (work / "keep.txt").write_bytes(b"work\n")
    do = work / "ДО"
    _side_project(do, {"b": {"url": "http://do/mcp"}})
    bp = _side_project(tmp_path / "bp", {"a": {"url": "http://bp/mcp"}})
    before_work = _fingerprints(work)
    before_bp = _fingerprints(bp)
    with pytest.raises(SystemExit, match="папка обмена должна быть вне папок проектов"):
        build_packs.install_exchange(work, [("bp", bp), ("do", do)], "claude", URL)
    assert _fingerprints(work) == before_work
    assert _fingerprints(bp) == before_bp
    assert not (work / ".claude").exists()
    assert not (work / "KD-EXCHANGE.md").exists()


def test_exchange_separate_same_folder_refuses(tmp_path: Path) -> None:
    bp = _side_project(tmp_path / "bp", {"a": {"url": "http://bp/mcp"}})
    (bp / ".cursor").mkdir()
    (bp / ".cursor" / "mcp.json").write_bytes(b'{"mcpServers":{}}\n')
    do = _side_project(tmp_path / "do", {"b": {"url": "http://do/mcp"}})
    before_bp = _fingerprints(bp)
    before_do = _fingerprints(do)
    with pytest.raises(SystemExit, match="папка обмена должна быть вне папок проектов"):
        build_packs.install_exchange(bp, [("bp", bp), ("do", do)], "claude", URL)
    assert _fingerprints(bp) == before_bp
    assert _fingerprints(do) == before_do


def test_exchange_separate_sibling_prefix_allowed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Имя папки `exchange-bp` не считается вложенным в `exchange`."""
    _no_token(monkeypatch)
    dest = tmp_path / "exchange"
    bp = _side_project(tmp_path / "exchange-bp", {"1c-data-mcp": {"url": "http://bp/mcp"}})
    do = _side_project(tmp_path / "other", {"catalog": {"url": "http://do/mcp"}})
    before_bp = _fingerprints(bp)
    before_do = _fingerprints(do)
    build_packs.install_exchange(dest, [("bp", bp), ("do", do)], "claude", URL)
    assert _servers(dest)["bp-1c-data-mcp"] == {"url": "http://bp/mcp"}
    assert _fingerprints(bp) == before_bp
    assert _fingerprints(do) == before_do


def test_exchange_servers_prefixes_own() -> None:
    built = build_packs.exchange_servers(
        [
            (
                "bp",
                {"1c-data-mcp": {"note": "лишнее", "type": "http", "url": "http://bp/mcp"}},
            ),
            ("do", {"1c-data-mcp": {"type": "http", "url": "http://do/mcp"}}),
        ]
    )
    assert built.entries == {
        "bp-1c-data-mcp": {"url": "http://bp/mcp", "type": "http"},
        "do-1c-data-mcp": {"url": "http://do/mcp", "type": "http"},
    }
    assert built.owners == {"bp-1c-data-mcp": "bp", "do-1c-data-mcp": "do"}
    assert list(built.entries) == ["bp-1c-data-mcp", "do-1c-data-mcp"]
    assert built.stdio == {"bp": [], "do": []}
    assert built.has_headers is False


def test_exchange_servers_skip_own_server_and_legacy_name() -> None:
    own = {"type": "http", "url": "http://localhost:8060/mcp"}
    built = build_packs.exchange_servers(
        [
            ("bp", {"kd-rules-mcp": own, "graph": {"url": "http://bp/graph"}}),
            ("do", {"kd2-rules-mcp": own, "kd2-local": {"command": "kd"}}),
        ]
    )
    assert built.entries == {"bp-graph": {"url": "http://bp/graph"}}
    assert built.stdio == {"bp": [], "do": ["kd2-local"]}


def test_exchange_servers_shared_once() -> None:
    built = build_packs.exchange_servers(
        [
            (
                "bp",
                {
                    "1c-syntax-checker-mcp": {"type": "http", "url": "http://syntax/mcp"},
                    "kd-rules-mcp": {"type": "http", "url": "http://kd/mcp"},
                    "1c-data-mcp": {"url": "http://bp/mcp"},
                },
            ),
            (
                "do",
                {
                    "1c-syntax-checker-mcp": {"type": "http", "url": "http://syntax/mcp"},
                    "kd-rules-mcp": {"type": "http", "url": "http://other/mcp"},
                    "1c-data-mcp": {"url": "http://do/mcp"},
                },
            ),
        ]
    )
    assert built.entries == {
        "1c-syntax-checker-mcp": {"url": "http://syntax/mcp", "type": "http"},
        "bp-1c-data-mcp": {"url": "http://bp/mcp"},
        "do-1c-data-mcp": {"url": "http://do/mcp"},
    }
    assert "kd-rules-mcp" not in built.entries
    assert built.owners["1c-syntax-checker-mcp"] == "обе"
    assert built.owners["bp-1c-data-mcp"] == "bp"
    assert built.owners["do-1c-data-mcp"] == "do"
    assert list(built.entries) == [
        "1c-syntax-checker-mcp",
        "bp-1c-data-mcp",
        "do-1c-data-mcp",
    ]


def test_exchange_servers_prefix_not_doubled() -> None:
    built = build_packs.exchange_servers(
        [
            ("bp", {"bp-graph": {"url": "http://bp-graph/mcp"}}),
            ("do", {"catalog": {"url": "http://do-catalog/mcp"}}),
        ]
    )
    assert built.entries == {
        "bp-graph": {"url": "http://bp-graph/mcp"},
        "do-catalog": {"url": "http://do-catalog/mcp"},
    }
    assert "bp-bp-graph" not in built.entries
    assert built.owners == {"bp-graph": "bp", "do-catalog": "do"}


def test_exchange_servers_stdio_not_transferred(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    built = build_packs.exchange_servers(
        [
            ("bp", {"1c-data-mcp": {"url": "http://bp/mcp"}}),
            ("do", {"local-stdio": {"command": "node", "args": ["srv.js"]}}),
        ]
    )
    assert "local-stdio" not in built.entries
    assert "do-local-stdio" not in built.entries
    assert built.stdio == {"bp": [], "do": ["local-stdio"]}
    _no_token(monkeypatch)
    bp = _side_project(tmp_path / "bp", {"1c-data-mcp": {"url": "http://bp/mcp"}})
    do = _side_project(tmp_path / "do", {"local-stdio": {"command": "node", "args": ["srv.js"]}})
    dest = tmp_path / "exchange"
    report = build_packs.install_exchange(dest, [("bp", bp), ("do", do)], "claude", URL)
    assert "local-stdio" not in _servers(dest)
    assert "do-local-stdio" not in _servers(dest)
    assert "do: local-stdio" in "\n".join(report)


def test_exchange_servers_headers_warning(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    built = build_packs.exchange_servers(
        [
            (
                "bp",
                {
                    "db": {
                        "type": "http",
                        "url": "http://db/mcp",
                        "headers": {"Authorization": "Basic eA=="},
                    }
                },
            ),
            ("do", {"catalog": {"url": "http://do/mcp"}}),
        ]
    )
    assert built.has_headers is True
    assert built.entries["bp-db"] == {
        "url": "http://db/mcp",
        "type": "http",
        "headers": {"Authorization": "Basic eA=="},
    }
    assert built.entries["do-catalog"] == {"url": "http://do/mcp"}
    _no_token(monkeypatch)
    bp = _side_project(
        tmp_path / "bp",
        {
            "db": {
                "type": "http",
                "url": "http://db/mcp",
                "headers": {"Authorization": "Basic eA=="},
            }
        },
    )
    do = _side_project(tmp_path / "do", {"catalog": {"url": "http://do/mcp"}})
    dest = tmp_path / "exchange"
    report = build_packs.install_exchange(dest, [("bp", bp), ("do", do)], "claude", URL)
    assert _servers(dest)["bp-db"] == {
        "url": "http://db/mcp",
        "type": "http",
        "headers": {"Authorization": "Basic eA=="},
    }
    warnings = [line for line in report if "учётные данные" in line]
    assert warnings == [".mcp.json папки обмена содержит учётные данные — коммитить его нельзя"]


def test_exchange_servers_missing_mcp_json(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    built = build_packs.exchange_servers(
        [("bp", {"1c-data-mcp": {"url": "http://bp/mcp"}}), ("do", {})]
    )
    assert built.entries == {"bp-1c-data-mcp": {"url": "http://bp/mcp"}}
    assert built.owners == {"bp-1c-data-mcp": "bp"}
    _no_token(monkeypatch)
    bp = _side_project(tmp_path / "bp", {"1c-data-mcp": {"url": "http://bp/mcp"}})
    do = tmp_path / "do"
    do.mkdir()
    (do / "keep.txt").write_bytes(b"do\n")
    before_do = _fingerprints(do)
    dest = tmp_path / "exchange"
    report = build_packs.install_exchange(dest, [("bp", bp), ("do", do)], "claude", URL)
    assert "сторона do без серверов" in report
    assert _servers(dest)["bp-1c-data-mcp"] == {"url": "http://bp/mcp"}
    assert _fingerprints(do) == before_do


def test_exchange_servers_name_conflict_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _no_token(monkeypatch)
    bp_servers = {
        "bp-x": {"url": "http://shared/mcp"},
        "x": {"url": "http://own/mcp"},
    }
    do_servers = {"bp-x": {"url": "http://shared/mcp"}}
    with pytest.raises(SystemExit, match="конфликт имени «bp-x»") as caught:
        build_packs.exchange_servers([("bp", bp_servers), ("do", do_servers)])
    text = str(caught.value)
    assert "общий сервер «bp-x»" in text
    assert "сторона bp, сервер «x»" in text
    dest = tmp_path / "exchange"
    bp = _side_project(tmp_path / "bp", bp_servers)
    do = _side_project(tmp_path / "do", do_servers)
    before_bp = _fingerprints(bp)
    before_do = _fingerprints(do)
    with pytest.raises(SystemExit, match="конфликт имени «bp-x»"):
        build_packs.install_exchange(dest, [("bp", bp), ("do", do)], "claude", URL)
    assert not dest.exists()
    assert _fingerprints(bp) == before_bp
    assert _fingerprints(do) == before_do


def test_exchange_reinstall_same_bytes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _no_token(monkeypatch)
    bp = _side_project(tmp_path / "bp", {"1c-data-mcp": {"url": "http://bp/mcp"}})
    do = _side_project(tmp_path / "do", {"catalog": {"url": "http://do/mcp"}})
    dest = tmp_path / "exchange"
    sides = [("bp", bp), ("do", do)]
    build_packs.install_exchange(dest, sides, "claude", URL)
    mcp = dest / ".mcp.json"
    blob = mcp.read_bytes()
    mtime = mcp.stat().st_mtime_ns
    report = build_packs.install_exchange(dest, sides, "claude", URL)
    assert mcp.read_bytes() == blob
    assert mcp.stat().st_mtime_ns == mtime
    assert "серверы сторон уже подключены" in report
    assert not any(line.startswith("обновлён:") for line in report)


def test_exchange_reinstall_updates_url(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _no_token(monkeypatch)
    bp = _side_project(tmp_path / "bp", {"1c-data-mcp": {"type": "http", "url": "http://bp/mcp"}})
    do = _side_project(tmp_path / "do", {"catalog": {"url": "http://do/mcp"}})
    dest = tmp_path / "exchange"
    sides = [("bp", bp), ("do", do)]
    build_packs.install_exchange(dest, sides, "claude", URL)
    _side_project(bp, {"1c-data-mcp": {"type": "http", "url": "http://bp/new"}})
    report = build_packs.install_exchange(dest, sides, "claude", URL)
    assert _servers(dest)["bp-1c-data-mcp"] == {"url": "http://bp/new", "type": "http"}
    assert _servers(dest)["do-catalog"] == {"url": "http://do/mcp"}
    assert "обновлён: bp-1c-data-mcp" in report
    assert "серверы сторон уже подключены" not in report


def test_exchange_reinstall_keeps_manual_server(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _no_token(monkeypatch)
    bp = _side_project(tmp_path / "bp", {"1c-data-mcp": {"url": "http://bp/mcp"}})
    do = _side_project(tmp_path / "do", {"catalog": {"url": "http://do/mcp"}})
    dest = tmp_path / "exchange"
    sides = [("bp", bp), ("do", do)]
    build_packs.install_exchange(dest, sides, "claude", URL)
    mcp = dest / ".mcp.json"
    data = json.loads(mcp.read_text(encoding="utf-8"))
    data["mcpServers"]["my-tool"] = {"url": "http://my/mcp", "headers": {"X-Tool": "1"}}
    mcp.write_bytes((json.dumps(data, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
    report = build_packs.install_exchange(dest, sides, "claude", URL)
    assert _servers(dest)["my-tool"] == {"url": "http://my/mcp", "headers": {"X-Tool": "1"}}
    assert _servers(dest)["bp-1c-data-mcp"] == {"url": "http://bp/mcp"}
    assert "обновлён: my-tool" not in report
    assert "серверы сторон уже подключены" in report


def test_exchange_reinstall_reports_disappeared(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _no_token(monkeypatch)
    bp = _side_project(
        tmp_path / "bp",
        {
            "1c-data-mcp": {"url": "http://bp/mcp"},
            "old-mcp": {"type": "http", "url": "http://old/mcp"},
        },
    )
    do = _side_project(tmp_path / "do", {"catalog": {"url": "http://do/mcp"}})
    dest = tmp_path / "exchange"
    sides = [("bp", bp), ("do", do)]
    build_packs.install_exchange(dest, sides, "claude", URL)
    mcp = dest / ".mcp.json"
    blob = mcp.read_bytes()
    mtime = mcp.stat().st_mtime_ns
    _side_project(bp, {"1c-data-mcp": {"url": "http://bp/mcp"}})
    report = build_packs.install_exchange(dest, sides, "claude", URL)
    assert mcp.read_bytes() == blob
    assert mcp.stat().st_mtime_ns == mtime
    assert _servers(dest)["bp-old-mcp"] == {"url": "http://old/mcp", "type": "http"}
    assert _servers(dest)["bp-1c-data-mcp"] == {"url": "http://bp/mcp"}
    assert "у стороны bp больше нет: bp-old-mcp" in report


def test_exchange_cursor_copies_both_sides(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _no_token(monkeypatch)
    bp = _side_project(tmp_path / "bp", {"1c-data-mcp": {"type": "http", "url": "http://bp/mcp"}})
    do = _side_project(
        tmp_path / "do",
        {
            "1c-data-mcp": {"type": "http", "url": "http://do/mcp"},
            "local-stdio": {"command": "node"},
        },
    )
    dest = tmp_path / "exchange"
    report = build_packs.install_exchange(
        dest, [("bp", bp), ("do", do)], "claude", URL, cursor=True
    )
    cursor = json.loads((dest / ".cursor" / "mcp.json").read_text(encoding="utf-8"))
    assert cursor["mcpServers"] == {
        "kd-rules-mcp": {"url": URL},
        "bp-1c-data-mcp": {"url": "http://bp/mcp"},
        "do-1c-data-mcp": {"url": "http://do/mcp"},
    }
    assert "local-stdio" not in cursor["mcpServers"]
    assert "do: local-stdio" in "\n".join(report)
    assert _servers(dest)["bp-1c-data-mcp"] == {"url": "http://bp/mcp", "type": "http"}


def test_exchange_entry_empty_folder(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _no_token(monkeypatch)
    syntax = {"type": "http", "url": "http://syntax/mcp"}
    bp = _side_project(
        tmp_path / "bp",
        {"1c-syntax-checker-mcp": syntax, "1c-data-mcp": {"url": "http://bp/mcp"}},
    )
    do = _side_project(
        tmp_path / "do",
        {"1c-syntax-checker-mcp": syntax, "1c-data-mcp": {"url": "http://do/mcp"}},
    )
    dest = tmp_path / "exchange"
    build_packs.install_exchange(dest, [("bp", bp), ("do", do)], "claude", URL)
    raw = (dest / "KD-EXCHANGE.md").read_bytes()
    assert b"\r" not in raw
    text = raw.decode("utf-8")
    lines = text.splitlines()
    assert lines[0] == "# Папка обмена: bp ↔ do"
    assert len(lines) <= 50
    assert f"- bp: `{bp.resolve()}`" in lines
    assert f"- do: `{do.resolve()}`" in lines
    assert "| сервер | сторона |" in lines
    assert "| 1c-syntax-checker-mcp | обе |" in lines
    assert "| bp-1c-data-mcp | bp |" in lines
    assert "| do-1c-data-mcp | do |" in lines
    # Выгрузка и регистрация — у источника, загрузка — копия корреспондента у приёмника.
    assert (
        "| bp → do | `ExchangeRules.xml` bp | `CorrespondentExchangeRules.xml` do "
        "| `RegistrationRules.xml` bp |"
    ) in lines
    assert (
        "| do → bp | `ExchangeRules.xml` do | `CorrespondentExchangeRules.xml` bp "
        "| `RegistrationRules.xml` do |"
    ) in lines
    assert (
        "источника. Совпадение копий проверять `rules_diff`, а не хешем файла: концы строк" in lines
    )
    assert "- Правила обмена правятся из этой папки." in lines
    assert "- Код и данные стороны — её серверами (префикс `bp-` или `do-`)." in lines
    assert "- Работа по проекту базы — в папке проекта; папка обмена не внутри проектов." in lines
    assert "- `.mcp.json` этой папки не читать: в нём учётные данные сторон." in lines
    assert (dest / "AGENTS.md").read_bytes() == (
        "Папка обмена правилами kd-rules-mcp — прочитай KD-EXCHANGE.md перед работой.\n"
    ).encode()
    assert (dest / "CLAUDE.md").read_bytes() == b"@AGENTS.md\n"


def test_exchange_entry_keeps_custom_agents(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _no_token(monkeypatch)
    bp = _side_project(tmp_path / "bp", {"1c-data-mcp": {"url": "http://bp/mcp"}})
    do = _side_project(tmp_path / "do", {"catalog": {"url": "http://do/mcp"}})
    dest = tmp_path / "exchange"
    dest.mkdir()
    agents = dest / "AGENTS.md"
    original = "Свои правила команды.\n".encode()
    agents.write_bytes(original)
    mtime = agents.stat().st_mtime_ns
    report = build_packs.install_exchange(dest, [("bp", bp), ("do", do)], "claude", URL)
    assert agents.read_bytes() == original
    assert agents.stat().st_mtime_ns == mtime
    assert (
        "AGENTS.md: добавьте строку «Папка обмена правилами kd-rules-mcp — "
        "прочитай KD-EXCHANGE.md перед работой.»"
    ) in report
    assert (dest / "CLAUDE.md").read_bytes() == b"@AGENTS.md\n"


def test_exchange_entry_reinstall_has_no_hint_for_own_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _no_token(monkeypatch)
    bp = _side_project(tmp_path / "bp", {})
    do = _side_project(tmp_path / "do", {})
    dest = tmp_path / "exchange"
    sides = [("bp", bp), ("do", do)]
    build_packs.install_exchange(dest, sides, "claude", URL)
    report = build_packs.install_exchange(dest, sides, "claude", URL)
    assert [line for line in report if "добавьте строку" in line] == []


def test_exchange_entry_custom_claude_without_import_gets_hint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _no_token(monkeypatch)
    bp = _side_project(tmp_path / "bp", {})
    do = _side_project(tmp_path / "do", {})
    dest = tmp_path / "exchange"
    dest.mkdir()
    (dest / "CLAUDE.md").write_bytes("Свои правила; про @AGENTS.md в тексте.\n".encode())
    report = build_packs.install_exchange(dest, [("bp", bp), ("do", do)], "claude", URL)
    assert (
        "CLAUDE.md: добавьте строку «Папка обмена правилами kd-rules-mcp — "
        "прочитай KD-EXCHANGE.md перед работой.»"
    ) in report


def test_exchange_entry_agents_client_mentions_rules(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _no_token(monkeypatch)
    bp = _side_project(tmp_path / "bp", {})
    do = _side_project(tmp_path / "do", {})
    dest = tmp_path / "exchange"
    build_packs.install_exchange(dest, [("bp", bp), ("do", do)], "agents", URL)
    assert (dest / "AGENTS.md").read_bytes() == (
        "Папка обмена правилами kd-rules-mcp — прочитай KD-EXCHANGE.md перед работой.\n"
        "Правила обмена КД 2 и сервер kd-rules-mcp — "
        "прочитай KD-RULES.md перед работой с ними.\n"
    ).encode()
    assert (dest / "CLAUDE.md").read_bytes() == b"@AGENTS.md\n"
    assert (dest / "KD-RULES.md").is_file()
    assert (dest / ".agents" / "skills").is_dir()
    assert not (dest / ".claude").exists()


def test_exchange_entry_reinstall_rewrites_only_description(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _no_token(monkeypatch)
    bp = _side_project(tmp_path / "bp", {"1c-data-mcp": {"url": "http://bp/mcp"}})
    do = _side_project(tmp_path / "do", {"catalog": {"url": "http://do/mcp"}})
    dest = tmp_path / "exchange"
    sides = [("bp", bp), ("do", do)]
    build_packs.install_exchange(dest, sides, "claude", URL)
    description = dest / "KD-EXCHANGE.md"
    previous = description.stat().st_mtime_ns - 10_000_000_000
    os.utime(description, ns=(previous, previous))
    before = _fingerprints(dest)
    build_packs.install_exchange(dest, sides, "claude", URL)
    after = _fingerprints(dest)
    assert set(after) == set(before)
    for name, finger in after.items():
        if name == "KD-EXCHANGE.md":
            assert finger[0] == before[name][0]
            assert finger[1] != before[name][1]
        else:
            assert finger == before[name]


def test_exchange_sides_untouched(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _no_token(monkeypatch)
    bp = _side_project(tmp_path / "bp", {"1c-data-mcp": {"url": "http://bp/mcp"}})
    do = _side_project(tmp_path / "do", {"catalog": {"url": "http://do/mcp"}})
    for folder in (bp, do):
        cursor = folder / ".cursor"
        cursor.mkdir()
        (cursor / "mcp.json").write_bytes(b'{"mcpServers":{"local":{"url":"http://local/mcp"}}}\n')
        skill = folder / ".claude" / "skills" / "local"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_bytes(b"side\n")
        (folder / "keep.txt").write_bytes(b"keep\n")
    before_bp = _fingerprints(bp)
    before_do = _fingerprints(do)
    dest = tmp_path / "exchange"
    build_packs.install_exchange(dest, [("bp", bp), ("do", do)], "claude", URL)
    assert _fingerprints(bp) == before_bp
    assert _fingerprints(do) == before_do
    assert (dest / "KD-EXCHANGE.md").is_file()
    assert before_bp.keys() >= {".mcp.json", ".cursor/mcp.json", ".claude/skills/local/SKILL.md"}


def test_exchange_sides_untouched_help(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    text = build_packs.__doc__ or ""
    for phrase in ("--side", "KD-EXCHANGE.md", "stdio", "префикс", "учётные данные"):
        assert phrase in text
    monkeypatch.setattr(sys, "argv", ["build_packs.py", "--help"])
    with pytest.raises(SystemExit) as caught:
        build_packs.main()
    assert caught.value.code == 0
    help_text = capsys.readouterr().out
    assert "--side" in help_text
    assert "KD-EXCHANGE.md" in help_text
    assert "ИМЯ=ПАПКА" in help_text


def _profile_files(tmp_path: Path, *, technology: str = "kd2") -> tuple[Path, Path]:
    projects = {
        name: {"name": name, "configurations": {"full": {"dump": "main"}}} for name in ("bp", "do")
    }
    sides = [
        {
            "project": name,
            "plan": f"План{name}",
            **(
                {"module": f"Manager{name}"}
                if technology == "ed"
                else {"rules": f"rules/{name}/Комплект"}
            ),
        }
        for name in projects
    ]
    profile = {
        "id": "bp-do",
        "plan": "План",
        "technology": technology,
        "sides": sides,
        "directions": [["bp", "do"]],
    }
    catalog = tmp_path / "projects.yaml"
    local = tmp_path / "projects.local.yaml"
    catalog.write_text(
        yaml.safe_dump({"projects": projects, "exchanges": [profile]}, allow_unicode=True),
        encoding="utf-8",
    )
    local.write_text(
        yaml.safe_dump({"projects": {name: str(tmp_path / name) for name in projects}}),
        encoding="utf-8",
    )
    for name in projects:
        (tmp_path / name).mkdir()
    return catalog, local


def test_exchange_profile_single_direction(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _no_token(monkeypatch)
    catalog, local = _profile_files(tmp_path)
    dest = tmp_path / "exchange"
    report = build_packs.install_exchange(
        dest,
        [],
        "claude",
        URL,
        exchange="bp-do",
        projects_file=catalog,
        local_file=local,
    )
    text = (dest / "KD-EXCHANGE.md").read_text(encoding="utf-8")
    assert "направления — из профиля обмена bp-do" in report
    rows = [line for line in text.splitlines() if " → " in line]
    assert rows == [
        "| bp → do | `rules/bp/Комплект/ExchangeRules.xml` bp (файла нет) | "
        "`rules/do/Комплект/CorrespondentExchangeRules.xml` do (файла нет) | "
        "`rules/bp/Комплект/RegistrationRules.xml` bp (файла нет) |"
    ]
    assert "Планbp" in text and "Планdo" in text and "`kd2`" in text
    assert "rules_dir" not in text


def test_exchange_profile_rules_folder_from_rules_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Без `rules` в профиле — `<rules_dir>/<план стороны>`; есть ли файл — видно в таблице."""
    _no_token(monkeypatch)
    catalog, local = _profile_files(tmp_path)
    data = yaml.safe_load(catalog.read_text(encoding="utf-8"))
    for side in data["exchanges"][0]["sides"]:
        del side["rules"]
        data["projects"][side["project"]]["rules_dir"] = "ПравилаОбмена"
    catalog.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    kit = tmp_path / "bp" / "ПравилаОбмена" / "Планbp"
    kit.mkdir(parents=True)
    (kit / "ExchangeRules.xml").write_text("test", encoding="utf-8")
    dest = tmp_path / "exchange"
    build_packs.install_exchange(
        dest, [], "claude", URL, exchange="bp-do", projects_file=catalog, local_file=local
    )
    text = (dest / "KD-EXCHANGE.md").read_text(encoding="utf-8")
    rows = [line for line in text.splitlines() if " → " in line]
    assert rows == [
        "| bp → do | `ПравилаОбмена/Планbp/ExchangeRules.xml` bp | "
        "`ПравилаОбмена/Планdo/CorrespondentExchangeRules.xml` do (файла нет) | "
        "`ПравилаОбмена/Планbp/RegistrationRules.xml` bp (файла нет) |"
    ]
    assert "Папка комплекта не задана в профиле (bp, do)" in text


def test_exchange_side_matching_profile_uses_custom_side_names(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _no_token(monkeypatch)
    catalog, local = _profile_files(tmp_path)
    dest = tmp_path / "exchange"
    report = build_packs.install_exchange(
        dest,
        [("right", tmp_path / "do"), ("left", tmp_path / "bp")],
        "claude",
        URL,
        projects_file=catalog,
        local_file=local,
    )
    text = (dest / "KD-EXCHANGE.md").read_text(encoding="utf-8")
    assert "направления — из профиля обмена bp-do" in report
    assert "| left → right | `rules/bp/Комплект/ExchangeRules.xml` left (файла нет) |" in text
    assert "right → left" not in text


def test_exchange_side_broken_catalog_keeps_old_behavior(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """С --side каталог — только подсказка: битый projects.yaml не останавливает установку."""
    _no_token(monkeypatch)
    catalog, local = _profile_files(tmp_path)
    catalog.write_text("exchanges: [{plan: План, rule: x}]\n", encoding="utf-8")
    dest = tmp_path / "exchange"
    report = build_packs.install_exchange(
        dest,
        [("bp", tmp_path / "bp"), ("do", tmp_path / "do")],
        "claude",
        URL,
        projects_file=catalog,
        local_file=local,
    )
    text = (dest / "KD-EXCHANGE.md").read_text(encoding="utf-8")
    assert not any("профиля обмена" in line for line in report)
    assert "| bp → do | `ExchangeRules.xml` bp |" in text and "do → bp" in text


def test_exchange_side_catalog_type_error_keeps_old_behavior(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Кривой тип поля — TypeError, не ProjectConfigError; прежний --side всё равно работает."""
    _no_token(monkeypatch)
    catalog, local = _profile_files(tmp_path)
    catalog.write_text(
        "projects:\n  bp:\n    configurations:\n      full: {dump: main, extensions: 5}\n",
        encoding="utf-8",
    )
    dest = tmp_path / "exchange"
    build_packs.install_exchange(
        dest,
        [("bp", tmp_path / "bp"), ("do", tmp_path / "do")],
        "claude",
        URL,
        projects_file=catalog,
        local_file=local,
    )
    assert "| bp → do | `ExchangeRules.xml` bp |" in (dest / "KD-EXCHANGE.md").read_text(
        encoding="utf-8"
    )


def test_exchange_profile_kd2_keeps_rules_diff_hint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _no_token(monkeypatch)
    catalog, local = _profile_files(tmp_path)
    dest = tmp_path / "exchange"
    build_packs.install_exchange(
        dest, [], "claude", URL, exchange="bp-do", projects_file=catalog, local_file=local
    )
    text = (dest / "KD-EXCHANGE.md").read_text(encoding="utf-8")
    assert "`rules_diff`" in text and "## Правила" in text


def test_exchange_profile_ed_lists_modules(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _no_token(monkeypatch)
    catalog, local = _profile_files(tmp_path, technology="ed")
    dest = tmp_path / "exchange"
    build_packs.install_exchange(
        dest, [], "claude", URL, exchange="bp-do", projects_file=catalog, local_file=local
    )
    text = (dest / "KD-EXCHANGE.md").read_text(encoding="utf-8")
    assert "| bp → do | `Managerbp` | `Managerdo` |" in text
    assert "`ed`" in text and "ExchangeRules.xml" not in text


def test_exchange_cli_profile(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _no_token(monkeypatch)
    _profile_files(tmp_path)
    monkeypatch.setattr(build_packs, "ROOT", tmp_path)
    dest = tmp_path / "exchange"
    monkeypatch.setattr(
        sys,
        "argv",
        ["build_packs.py", "--dest", str(dest), "--exchange", "bp-do", "--server-url", URL],
    )
    # Содержимое упаковки не зависит от папки каталога.
    monkeypatch.setattr(build_packs, "stale_copies", lambda: [])
    monkeypatch.setattr(build_packs, "pack_files", lambda client: {})
    build_packs.main()
    assert "bp → do" in (dest / "KD-EXCHANGE.md").read_text(encoding="utf-8")


def test_exchange_and_side_are_argparse_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    dest = tmp_path / "not-created"
    monkeypatch.setattr(
        sys,
        "argv",
        ["build_packs.py", "--dest", str(dest), "--exchange", "bp-do", "--side", "bp=unused"],
    )
    with pytest.raises(SystemExit) as caught:
        build_packs.main()
    assert caught.value.code == 2
    assert "--exchange нельзя вместе с --side" in capsys.readouterr().err
    assert not dest.exists()


@pytest.mark.parametrize(
    "fault,message",
    [
        ("unknown", "нет.*bp-do"),
        ("legacy", "без сторон"),
        ("missing-folder", "папка проекта.*do"),
        ("bad-name", "--side"),
    ],
)
def test_exchange_profile_refuses_before_writing(tmp_path: Path, fault: str, message: str) -> None:
    catalog, local = _profile_files(tmp_path)
    data = yaml.safe_load(catalog.read_text(encoding="utf-8"))
    exchange = "bp-do"
    if fault == "unknown":
        exchange = "absent"
    elif fault == "legacy":
        data["exchanges"] = [{"id": "bp-do", "plan": "План", "projects": ["bp", "do"]}]
    elif fault == "missing-folder":
        local.write_text(
            yaml.safe_dump({"projects": {"bp": str(tmp_path / "bp")}}), encoding="utf-8"
        )
    else:
        data["projects"]["БП"] = data["projects"].pop("bp")
        data["exchanges"][0]["sides"][0]["project"] = "БП"
        data["exchanges"][0]["directions"] = [["БП", "do"]]
    catalog.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    dest = tmp_path / "not-created"
    before = {name: _fingerprints(tmp_path / name) for name in ("bp", "do")}
    with pytest.raises(SystemExit, match=message):
        build_packs.install_exchange(
            dest, [], "claude", URL, exchange=exchange, projects_file=catalog, local_file=local
        )
    assert not dest.exists()
    assert {name: _fingerprints(tmp_path / name) for name in ("bp", "do")} == before


def test_exchange_without_profile_keeps_exact_legacy_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _no_token(monkeypatch)
    bp, do = tmp_path / "bp", tmp_path / "do"
    bp.mkdir()
    do.mkdir()
    dest = tmp_path / "exchange"
    build_packs.install_exchange(
        dest,
        [("bp", bp), ("do", do)],
        "claude",
        URL,
        projects_file=tmp_path / "absent.yaml",
        local_file=tmp_path / "absent.local.yaml",
    )
    direction_rows = (
        "| bp → do | `ExchangeRules.xml` bp | `CorrespondentExchangeRules.xml` do | "
        "`RegistrationRules.xml` bp |\n"
        "| do → bp | `ExchangeRules.xml` do | `CorrespondentExchangeRules.xml` bp | "
        "`RegistrationRules.xml` do |"
    )
    expected = f"""# Папка обмена: bp ↔ do

- bp: `{bp.resolve()}`
- do: `{do.resolve()}`

| сервер | сторона |
| --- | --- |

## Направления и файлы правил

Комплект БСП лежит в папке живых правил стороны (`rules_dir` в `project_list`,
внутри — папка плана обмена). Какой файл работает в каком направлении:

| направление | выгрузка | загрузка | регистрация |
| --- | --- | --- | --- |
{direction_rows}

Загружает приёмник по копии правил корреспондента из своего комплекта, а не по файлу
источника. Совпадение копий проверять `rules_diff`, а не хешем файла: концы строк
в репозиториях сторон могут отличаться.

## Правила

- Правила обмена правятся из этой папки.
- Код и данные стороны — её серверами (префикс `bp-` или `do-`).
- Работа по проекту базы — в папке проекта; папка обмена не внутри проектов.
- `.mcp.json` этой папки не читать: в нём учётные данные сторон.
  Список серверов — таблица выше.
"""
    assert (dest / "KD-EXCHANGE.md").read_bytes() == expected.encode("utf-8")
