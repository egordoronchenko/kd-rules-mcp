"""Папка живых правил проекта (`rules_dir`): запись только в неё и в рабочую папку (задача #4)."""

import json
import re
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml
from mcp import Client

from kd2_rules_mcp.authoring.workspace import RulesWorkspace
from kd2_rules_mcp.errors import WorkspacePathError
from kd2_rules_mcp.projects import ProjectConfigError, load_catalog, load_local
from kd2_rules_mcp.server import create_server
from kd2_rules_mcp.service import Kd2Service, PathMap, Settings

ROOT = Path(__file__).resolve().parents[1]
DATA = Path(__file__).parent / "data"
RULES = DATA / "exchange_rules.xml"
sys.path.insert(0, str(ROOT / "scripts"))

import setup_local  # noqa: E402 — скрипт из scripts/, не пакет

CATALOG = """
projects:
  alpha:
    name: Альфа
    rules_dir: Проект/ПравилаОбмена
    configurations: {full: {dump: Проект/main}}
  beta:
    name: Бета
    rules_dir: Правила
    configurations: {full: {dump: main}}
  gamma:
    name: Гамма
    configurations: {full: {dump: main}}
"""


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


async def _call(client: Client, tool: str, /, **arguments: Any) -> dict[str, Any]:
    result = await client.call_tool(tool, arguments)
    assert not result.is_error, result.content
    assert result.structured_content is not None
    return result.structured_content


async def _error(client: Client, tool: str, /, **arguments: Any) -> dict[str, Any]:
    result = await client.call_tool(tool, arguments)
    assert result.is_error, tool
    text = "".join(getattr(item, "text", "") for item in result.content)
    # SDK добавляет «Error executing tool <имя>: » перед текстом ошибки.
    return json.loads(text[text.index("{") :])


def test_workspace_writes_into_allowed_folder_only(tmp_path: Path) -> None:
    workspace = RulesWorkspace(tmp_path / "ws")
    project = workspace.open_rules(RULES).project
    rules = tmp_path / "project" / "rules"
    rules.mkdir(parents=True)
    saved = workspace.save(project.id, rules / "ExchangeRules.xml", allowed=[rules])
    assert saved == (rules / "ExchangeRules.xml").resolve() and saved.is_file()
    assert workspace.save(project.id, "rel.xml", allowed=[rules]).parent == workspace.root.resolve()
    for outside in (tmp_path / "project" / "x.xml", rules / ".." / "x.xml"):
        with pytest.raises(WorkspacePathError, match=re.escape(str(rules.resolve()))):
            workspace.save(project.id, outside, allowed=[rules])
    assert not (tmp_path / "project" / "x.xml").exists()
    # Без разрешённых папок — прежнее поведение.
    with pytest.raises(WorkspacePathError, match=r"путь вне рабочей папки\. "):
        workspace.save(project.id, rules / "y.xml")


@pytest.mark.anyio
async def test_container_paths_rules_dir_writable_project_read_only(tmp_path: Path) -> None:
    """Как в контейнере: проект смонтирован только на чтение, папка правил — отдельно на запись."""
    mounted = tmp_path / "projects" / "alpha"
    (mounted / "Проект").mkdir(parents=True)
    rules = tmp_path / "rules" / "alpha"
    rules.mkdir(parents=True)
    workspace = tmp_path / "ws"
    host = r"D:\Work\Альфа"
    path_map = PathMap.parse(
        rf"{host}={mounted};{host}\Проект\ПравилаОбмена={rules};D:\Work\КД\workspace={workspace}"
    )
    settings = Settings(
        cache_dir=tmp_path / "cache",
        workspace=workspace,
        path_map=path_map,
        rules_dirs={"alpha": rules},
    )
    async with Client(create_server(Kd2Service(settings))) as client:
        opened = await _call(client, "rules_open", path=str(RULES))
        saved = await _call(
            client,
            "rules_save",
            project_id=opened["project_id"],
            path=rf"{host}\Проект\ПравилаОбмена\БП-ДО\ExchangeRules.xml",
        )
        rejected = await _error(
            client, "rules_save", project_id=opened["project_id"], path=rf"{host}\Проект\x.xml"
        )
        exported = await _call(
            client,
            "handlers_export",
            project_id=opened["project_id"],
            folder=rf"{host}\Проект\ПравилаОбмена\handlers",
        )
    assert saved["path"] == rf"{host}\Проект\ПравилаОбмена\БП-ДО\ExchangeRules.xml"
    assert (rules / "БП-ДО" / "ExchangeRules.xml").is_file()
    assert exported["folder"] == rf"{host}\Проект\ПравилаОбмена\handlers"
    assert rejected["code"] == "path_outside_workspace"
    assert rejected["writable"] == [r"D:\Work\КД\workspace", rf"{host}\Проект\ПравилаОбмена"]
    # В тексте — пути агента, а не пути сервера (в контейнере агенту они ничего не скажут).
    assert rf"{host}\Проект\x.xml" in rejected["message"]
    assert str(rules) not in rejected["message"] and str(mounted) not in rejected["message"]
    assert not (mounted / "Проект" / "x.xml").exists()


@pytest.mark.anyio
async def test_local_run_takes_rules_dirs_from_projects(tmp_path: Path) -> None:
    """Без KD2_RULES_DIRS: `rules_dir` проектов от их папок; project_list показывает папку."""
    catalog = tmp_path / "projects.yaml"
    catalog.write_text(CATALOG, encoding="utf-8")
    alpha, beta = tmp_path / "Альфа", tmp_path / "Бета"
    (alpha / "Проект" / "ПравилаОбмена").mkdir(parents=True)
    beta.mkdir()  # папки правил у беты нет
    settings = Settings(
        cache_dir=tmp_path / "cache",
        workspace=tmp_path / "ws",
        projects_file=catalog,
        project_dirs={"alpha": alpha, "beta": beta},
    )
    target = alpha / "Проект" / "ПравилаОбмена" / "ExchangeRules.xml"
    async with Client(create_server(Kd2Service(settings))) as client:
        listed = await _call(client, "project_list")
        opened = await _call(client, "rules_open", path=str(RULES))
        await _call(client, "rules_save", project_id=opened["project_id"], path=str(target))
        rejected = await _error(
            client, "rules_save", project_id=opened["project_id"], path=str(beta / "x.xml")
        )
    by_id = {item["project"]: item for item in listed["projects"]}
    assert by_id["alpha"]["rules_dir"] == {
        "path": str((alpha / "Проект" / "ПравилаОбмена").resolve()),
        "writable": True,
    }
    assert by_id["beta"]["rules_dir"]["writable"] is False
    assert "rules_dir" not in by_id["gamma"]
    assert target.is_file()
    assert rejected["code"] == "path_outside_workspace"


def test_setup_mounts_existing_rules_dir_for_writing(tmp_path: Path) -> None:
    catalog_file = tmp_path / "projects.yaml"
    catalog_file.write_text(CATALOG, encoding="utf-8")
    catalog = load_catalog(catalog_file)
    alpha, beta = tmp_path / "Альфа", tmp_path / "Бета"
    rules = alpha / "Проект" / "ПравилаОбмена"
    rules.mkdir(parents=True)
    beta.mkdir()
    local_file = tmp_path / "projects.local.yaml"
    local_file.write_text(
        yaml.safe_dump({"projects": {"alpha": str(alpha), "beta": str(beta)}}, allow_unicode=True),
        encoding="utf-8",
    )
    local = load_local(local_file)
    service = yaml.safe_load(setup_local.compose_override(catalog, local))["services"][
        "kd2-rules-mcp"
    ]
    posix = str(rules).replace("\\", "/")
    assert f"{posix}:/rules/alpha" in service["volumes"]
    assert f"{str(alpha).replace(chr(92), '/')}:/projects/alpha:ro" in service["volumes"]
    assert not any("/rules/beta" in volume for volume in service["volumes"])
    assert service["environment"]["KD2_RULES_DIRS"] == "alpha=/rules/alpha"
    assert f"{rules}=/rules/alpha" in service["environment"]["KD2_PATH_MAP"]
    warnings = setup_local.missing_rules_dirs(catalog, local)
    assert len(warnings) == 1 and warnings[0].startswith("beta: нет папки правил")


def test_rules_dirs_env() -> None:
    settings = Settings.from_env({"KD2_RULES_DIRS": "alpha=/rules/alpha; beta=/rules/beta"})
    assert settings.rules_dirs == {"alpha": Path("/rules/alpha"), "beta": Path("/rules/beta")}
    with pytest.raises(ProjectConfigError, match="KD2_RULES_DIRS"):
        Settings.from_env({"KD2_RULES_DIRS": "alpha"})
