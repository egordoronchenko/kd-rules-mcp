"""project_list: имена серверов как у setup_local, папка проекта и рабочая папка путём агента."""

import json
import sys
from pathlib import Path

import pytest
import yaml
from mcp import Client

from kd2_rules_mcp.projects import LocalSettings, load_catalog, load_local
from kd2_rules_mcp.server import create_server
from kd2_rules_mcp.service import Kd2Service, PathMap, Settings

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import setup_local  # noqa: E402 — скрипт из scripts/, не пакет

HOST_PROJECT = r"D:\Work\Альфа"
HOST_WORKSPACE = r"D:\Work\КД\workspace"

CATALOG = """
projects:
  alpha:
    name: Альфа
    mcp_config: Проект/.mcp.json
    configurations:
      full:
        dump: Проект/main
    bases:
      sandbox:
        {role: песочница, configuration: full, connection: 'File="C:\\\\A";', data_mcp: data-a}
    code_mcp: [code-a, code-graph]
  beta:
    name: Бета
    configurations:
      full: {dump: Проект/main}
shared_mcp: {from: alpha, names: [docs]}
exchanges:
  - {plan: ОбменАльфаБета, projects: [alpha, beta]}
"""


def _prepare(tmp_path: Path) -> tuple[Settings, LocalSettings]:
    alpha = (tmp_path / "Альфа").resolve()
    workspace = (tmp_path / "ws").resolve()
    mcp_dir = alpha / "Проект"
    mcp_dir.mkdir(parents=True)
    (mcp_dir / ".mcp.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "code-a": {"url": "http://h/code"},
                    "code-graph": {"url": "http://h/graph"},
                    "data-a": {"url": "http://h/data"},
                    "docs": {"url": "http://h/docs"},
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    catalog_path = tmp_path / "projects.yaml"
    catalog_path.write_text(CATALOG, encoding="utf-8")
    local_path = tmp_path / "projects.local.yaml"
    local_path.write_text(
        yaml.safe_dump({"projects": {"alpha": str(alpha)}}, allow_unicode=True),
        encoding="utf-8",
    )
    settings = Settings(
        cache_dir=tmp_path / "cache",
        workspace=workspace,
        path_map=PathMap.parse(rf"{HOST_PROJECT}={alpha};{HOST_WORKSPACE}={workspace}"),
        projects_file=catalog_path,
        project_dirs={"alpha": alpha},
    )
    return settings, load_local(local_path)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.anyio
async def test_project_list_reports_agent_paths_and_server_names(tmp_path: Path) -> None:
    settings, _local = _prepare(tmp_path)
    async with Client(create_server(Kd2Service(settings))) as client:
        listed = (await client.call_tool("project_list", {})).structured_content
    assert listed is not None
    assert listed["workspace"] == HOST_WORKSPACE
    assert listed["shared_mcp"] == ["docs"]
    by_id = {item["project"]: item for item in listed["projects"]}
    assert by_id["alpha"]["code_mcp"] == ["alpha-code-a", "alpha-code-graph"]
    assert by_id["alpha"]["bases"]["sandbox"]["data_mcp"] == "alpha-data-a"
    assert by_id["alpha"]["folder"] == HOST_PROJECT
    assert "folder" not in by_id["beta"]


@pytest.mark.anyio
async def test_project_list_server_names_match_setup_local(tmp_path: Path) -> None:
    """Имена code_mcp и data_mcp из project_list есть среди ключей mcp_servers."""
    settings, local = _prepare(tmp_path)
    catalog = load_catalog(settings.projects_file)
    servers, _warnings = setup_local.mcp_servers(catalog, local)
    async with Client(create_server(Kd2Service(settings))) as client:
        listed = (await client.call_tool("project_list", {})).structured_content
    assert listed is not None
    names: list[str] = []
    for project in listed["projects"]:
        names.extend(project["code_mcp"])
        for base in project["bases"].values():
            if "data_mcp" in base:
                names.append(base["data_mcp"])
    assert names
    assert set(names) <= servers.keys()
    assert set(listed["shared_mcp"]) <= servers.keys()
