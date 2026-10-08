"""Справочник инструментов `docs/tools.md` совпадает с тем, что генерирует сервер."""

import inspect
import json
import sys
from pathlib import Path
from typing import Any

import anyio
import pytest
from mcp import Client
from mcp.server.mcpserver import MCPServer

from kd_rules_mcp.server import INSTRUCTIONS, _without_schema_titles, create_server
from kd_rules_mcp.service import Kd2Service, Settings

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
    assert f"Tools: {len(grouped)}." in dump_tools.render()


def test_tool_schema_context_budget(tmp_path: Path) -> None:
    """Публичные описания укладываются в бюджет контекста, а имена 1С остаются кириллицей."""
    service = Kd2Service(Settings(cache_dir=tmp_path / "cache", workspace=tmp_path / "workspace"))
    tools = anyio.run(dump_tools._tools, service)
    serialized = json.dumps(
        [
            {"name": tool.name, "description": tool.description, "inputSchema": tool.input_schema}
            for tool in tools
        ],
        ensure_ascii=False,
    )
    # Виды правил и синтаксис адресов нужны агенту без локального docs/tools.md.
    # Перенос регистрации добавляет отдельную схему preview/write (#67).
    assert len(serialized) <= 48_000, f"Схемы: {len(serialized)} знаков"
    descriptions = sum(len(tool.description or "") for tool in tools)
    assert descriptions >= 6_000, f"Описания инструментов: {descriptions} знаков"
    for tool in tools:
        assert len(tool.description or "") <= 450, tool.name
        if tool.name == "registration_retarget":
            size = len(
                json.dumps(
                    {
                        "name": tool.name,
                        "description": tool.description,
                        "inputSchema": tool.input_schema,
                    },
                    ensure_ascii=False,
                )
            )
            assert size <= 1_300, f"registration_retarget: {size} знаков"
    cyrillic = sum("\u0400" <= char <= "\u04ff" for char in serialized)
    assert cyrillic / len(serialized) <= 0.08, f"Кириллица: {cyrillic / len(serialized):.2%}"
    # Писатель и перенос регистрации добавили по строке о порядке работы.
    assert len(INSTRUCTIONS) <= 2_100, f"INSTRUCTIONS: {len(INSTRUCTIONS)} знаков"


def test_schema_titles_keep_named_properties_and_literal_values() -> None:
    """Поле title и литералы пользователя не должны исчезать вместе с заголовками схем."""
    literal = {"title": "user value"}
    schema = {
        "title": "Arguments",
        "type": "object",
        "properties": {
            "title": {"title": "Title", "type": "string"},
            "payload": {
                "anyOf": [
                    {"title": "Payload", "type": "object", "additionalProperties": True},
                    {"type": "null"},
                ],
                "default": literal,
                "examples": [literal],
                "enum": [literal],
                "const": literal,
            },
        },
        "required": ["title"],
    }
    original = json.dumps(schema)
    compact = _without_schema_titles(schema)
    assert compact == {
        "type": "object",
        "properties": {
            "title": {"type": "string"},
            "payload": {
                "anyOf": [{"type": "object", "additionalProperties": True}, {"type": "null"}],
                "default": literal,
                "examples": [literal],
                "enum": [literal],
                "const": literal,
            },
        },
        "required": ["title"],
    }
    assert json.dumps(schema) == original


def test_public_schemas_preserve_calls_and_argument_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Клиент видит только удаление title; ошибки типов/границ и ответы остаются прежними."""
    service = Kd2Service(Settings(cache_dir=tmp_path / "cache", workspace=tmp_path / "workspace"))
    raw_server = create_server(service)
    compact_server = create_server(service)
    monkeypatch.setattr(raw_server, "list_tools", lambda: MCPServer.list_tools(raw_server))

    def leaves(value: Any, path: tuple[Any, ...] = ()) -> dict[tuple[Any, ...], Any]:
        if isinstance(value, dict) and value:
            return {
                leaf: item
                for key, child in value.items()
                for leaf, item in leaves(child, (*path, key)).items()
            }
        if isinstance(value, list) and value:
            return {
                leaf: item
                for index, child in enumerate(value)
                for leaf, item in leaves(child, (*path, index)).items()
            }
        return {path: value}

    async def exercise(server: MCPServer) -> tuple[list[Any], list[Any], Any]:
        async with Client(server) as client:
            tools = (await client.list_tools()).tools
            errors = []
            for arguments in (
                {"structure_id": []},
                {"structure_id": "missing", "limit": []},
                {"structure_id": "missing", "limit": 0},
            ):
                result = await client.call_tool("structure_objects", arguments)
                assert result.is_error
                errors.append(result.model_dump())
            valid = await client.call_tool("structure_list", {})
            assert not valid.is_error
            return [tool.model_dump() for tool in tools], errors, valid.model_dump()

    before, before_errors, before_valid = anyio.run(exercise, raw_server)
    after, after_errors, after_valid = anyio.run(exercise, compact_server)
    for tool in before:
        tool["description"] = inspect.cleandoc(tool["description"] or "")
    before_leaves, after_leaves = leaves(before), leaves(after)
    removed = before_leaves.keys() - after_leaves.keys()
    assert removed
    assert all(path[-1] == "title" and path[-2] != "properties" for path in removed)
    assert not after_leaves.keys() - before_leaves.keys()
    assert all(before_leaves[path] == value for path, value in after_leaves.items())
    assert before_errors == after_errors
    assert before_valid == after_valid


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
