"""project_list: имена серверов как у setup_local, папка проекта и рабочая папка путём агента."""

import json
import sys
from pathlib import Path

import pytest
import yaml
from mcp import Client

from kd_rules_mcp.projects import LocalSettings, load_catalog, load_local
from kd_rules_mcp.server import create_server
from kd_rules_mcp.service import Kd2Service, PathMap, Settings

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
    assert by_id["alpha"]["code_mcp"] == ["code-a", "code-graph"]
    assert by_id["alpha"]["code_mcp_server"] == ["alpha-code-a", "alpha-code-graph"]
    sandbox = by_id["alpha"]["bases"]["sandbox"]
    assert sandbox["data_mcp"] == "data-a"
    assert sandbox["data_mcp_server"] == "alpha-data-a"
    assert sandbox["login"] is False
    assert by_id["alpha"]["folder"] == HOST_PROJECT
    assert "folder" not in by_id["beta"]


@pytest.mark.anyio
async def test_project_list_server_names_match_setup_local(tmp_path: Path) -> None:
    """Оба набора имён: без префикса — как в .mcp.json, с префиксом — ключи setup_local."""
    settings, local = _prepare(tmp_path)
    catalog = load_catalog(settings.projects_file)
    servers, _warnings = setup_local.mcp_servers(catalog, local)
    async with Client(create_server(Kd2Service(settings))) as client:
        listed = (await client.call_tool("project_list", {})).structured_content
    assert listed is not None
    plain: list[str] = []
    prefixed: list[str] = []
    for project in listed["projects"]:
        plain.extend(project["code_mcp"])
        prefixed.extend(project["code_mcp_server"])
        assert project["code_mcp_server"] == [
            f"{project['project']}-{name}" for name in project["code_mcp"]
        ]
        for base in project["bases"].values():
            if "data_mcp" not in base:
                continue
            plain.append(base["data_mcp"])
            prefixed.append(base["data_mcp_server"])
            assert base["data_mcp_server"] == f"{project['project']}-{base['data_mcp']}"
    assert plain == ["code-a", "code-graph", "data-a"]
    assert prefixed
    assert set(prefixed) <= servers.keys()
    assert set(plain).isdisjoint(servers.keys())
    assert set(listed["shared_mcp"]) <= servers.keys()


USER_LOGIN = "СекретныйАгент"
USER_ENV = "ПользовательСреды"
SECRET = "ПарольКоторыйНельзя"

LOGIN_CATALOG = """
projects:
  alpha:
    name: Альфа
    configurations:
      full: {dump: main}
    bases:
      with_login:
        {role: песочница, configuration: full, connection: 'File="C:\\\\A";'}
      from_env:
        {role: песочница, configuration: full, connection: 'File="C:\\\\B";', dev_env: .dev.env}
      plain:
        {role: боевая, configuration: full, connection: 'File="C:\\\\C";'}
"""


def _login_settings(tmp_path: Path, *, with_local: bool) -> Settings:
    alpha = tmp_path / "alpha"
    alpha.mkdir()
    (alpha / ".dev.env").write_text(f"IB_USER={USER_ENV}\nIB_PASSWORD={SECRET}\n", encoding="utf-8")
    catalog = tmp_path / "projects.yaml"
    catalog.write_text(LOGIN_CATALOG, encoding="utf-8")
    if with_local:
        (tmp_path / "projects.local.yaml").write_text(
            yaml.safe_dump(
                {
                    "projects": {"alpha": str(alpha)},
                    "logins": {"alpha.with_login": {"user": USER_LOGIN, "password": SECRET}},
                },
                allow_unicode=True,
            ),
            encoding="utf-8",
        )
    return Settings(
        cache_dir=tmp_path / "cache",
        workspace=tmp_path / "ws",
        projects_file=catalog,
        project_dirs={"alpha": alpha},
    )


@pytest.mark.anyio
async def test_project_list_login_flag_hides_credentials(tmp_path: Path) -> None:
    """`logins` и `IB_USER` дают `login: true`; значения в ответ не попадают."""
    settings = _login_settings(tmp_path, with_local=True)
    async with Client(create_server(Kd2Service(settings))) as client:
        listed = (await client.call_tool("project_list", {})).structured_content
    assert listed is not None
    bases = {item["project"]: item for item in listed["projects"]}["alpha"]["bases"]
    assert bases["with_login"]["login"] is True
    assert bases["from_env"]["login"] is True
    assert bases["plain"]["login"] is False
    assert all("login" in base for base in bases.values())
    text = json.dumps(listed, ensure_ascii=False)
    assert USER_LOGIN not in text
    assert USER_ENV not in text
    assert SECRET not in text


@pytest.mark.anyio
async def test_project_list_login_flag_without_local_file_uses_dev_env(tmp_path: Path) -> None:
    """Нет `projects.local.yaml` — признак только по `.dev.env`, `logins` не видны."""
    settings = _login_settings(tmp_path, with_local=False)
    async with Client(create_server(Kd2Service(settings))) as client:
        listed = (await client.call_tool("project_list", {})).structured_content
    assert listed is not None
    bases = {item["project"]: item for item in listed["projects"]}["alpha"]["bases"]
    assert bases["with_login"]["login"] is False
    assert bases["from_env"]["login"] is True
    assert bases["plain"]["login"] is False
    text = json.dumps(listed, ensure_ascii=False)
    assert USER_ENV not in text
    assert SECRET not in text
