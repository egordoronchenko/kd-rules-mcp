"""Проверка развёрнутого контейнера (спецификация `mcp-service`, развёртывание).

Тесты с маркером `deploy` пропускаются, пока не задана `KD2_DEPLOY_URL`
(например `http://localhost:8060/mcp`). Первый поднимает структуру `deploy-check`,
сохраняет правила в рабочую папку и отказывается писать в исходники. Второй
запускают после пересоздания контейнера: та же структура берётся из кэша.
"""

import json
import os
import shutil
from pathlib import Path
from typing import Any

import pytest
from mcp import Client

# Пути агента — этой машины: тестовые данные копируются в рабочую папку репозитория, которую сервер
# видит всегда (docker-compose.yml), — так тест не зависит от того, где лежат проекты.
ROOT = Path(__file__).resolve().parents[1]
DATA = Path(__file__).parent / "data"
WORKSPACE = ROOT / "workspace"
CHECK_DIR = WORKSPACE / "deploy-check"
STRUCTURE_AGENT = str(CHECK_DIR / "dump")
RULES_AGENT = str(CHECK_DIR / "exchange_rules.xml")
SAVED_AGENT = str(WORKSPACE / "deploy-check.xml")
FORBIDDEN_AGENT = str(DATA / "x.xml")
WORKSPACE_AGENT = str(WORKSPACE)
STRUCTURE_ID = "deploy-check"


def _stage_inputs() -> None:
    """Копия тестовой выгрузки и правил в рабочую папку; время изменения файлов сохраняется,
    поэтому повторный запуск не меняет отпечаток выгрузки."""
    shutil.copytree(DATA / "xmldump" / "main", CHECK_DIR / "dump", dirs_exist_ok=True)
    shutil.copy2(DATA / "exchange_rules.xml", CHECK_DIR / "exchange_rules.xml")


pytestmark = [
    pytest.mark.deploy,
    pytest.mark.anyio,
    pytest.mark.skipif(
        not os.environ.get("KD2_DEPLOY_URL"),
        reason="переменная KD2_DEPLOY_URL не задана",
    ),
]


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


async def _call(client: Client, tool: str, /, **arguments: Any) -> dict[str, Any]:
    result = await client.call_tool(tool, arguments)
    assert not result.is_error, f"{tool}: {result.content}"
    assert result.structured_content is not None, tool
    return result.structured_content


async def _error(client: Client, tool: str, /, **arguments: Any) -> dict[str, Any]:
    result = await client.call_tool(tool, arguments)
    assert result.is_error, tool
    text = "".join(getattr(item, "text", "") for item in result.content)
    return json.loads(text[text.index("{") :])


async def test_load_structure_save_rules_and_reject_project_write() -> None:
    """Загрузка структуры, запись в рабочую папку и отказ писать в исходники проекта."""
    saved_path = Path(SAVED_AGENT)
    if saved_path.is_file():
        saved_path.unlink()
    forbidden = Path(FORBIDDEN_AGENT)
    assert not forbidden.exists()
    _stage_inputs()
    async with Client(os.environ["KD2_DEPLOY_URL"]) as client:
        loaded = await _call(
            client,
            "structure_load_xml",
            structure_id=STRUCTURE_ID,
            configuration_path=STRUCTURE_AGENT,
        )
        print(
            f"DEPLOY first load: elapsed_s={loaded['elapsed_s']} reused={loaded['reused']}",
            flush=True,
        )
        assert loaded["structure_id"] == STRUCTURE_ID
        assert loaded["counts"]["objects"] > 0
        opened = await _call(client, "rules_open", path=RULES_AGENT)
        saved = await _call(
            client,
            "rules_save",
            project_id=opened["project_id"],
            path=SAVED_AGENT,
        )
        rejected = await _error(
            client,
            "rules_save",
            project_id=opened["project_id"],
            path=FORBIDDEN_AGENT,
        )
    assert saved["path"] == SAVED_AGENT
    assert saved_path.is_file()
    assert rejected["code"] == "path_outside_workspace"
    assert rejected["workspace"] == WORKSPACE_AGENT
    assert not forbidden.exists()


async def test_structure_reused_after_container_recreate() -> None:
    """После пересоздания контейнера структура `deploy-check` читается из кэша."""
    _stage_inputs()
    async with Client(os.environ["KD2_DEPLOY_URL"]) as client:
        loaded = await _call(
            client,
            "structure_load_xml",
            structure_id=STRUCTURE_ID,
            configuration_path=STRUCTURE_AGENT,
        )
        print(
            f"DEPLOY reuse load: elapsed_s={loaded['elapsed_s']} reused={loaded['reused']}",
            flush=True,
        )
        listed = await _call(client, "structure_list")
    assert loaded["reused"] is True
    assert STRUCTURE_ID in [item["structure_id"] for item in listed["structures"]]
