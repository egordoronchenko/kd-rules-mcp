"""Профили проектов: общий projects.yaml, личный projects.local.yaml, генерация настроек машины."""

import base64
import json
import shutil
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml
from mcp import Client

from kd2_rules_mcp.projects import (
    LocalSettings,
    ProjectConfigError,
    dev_env_login,
    load_catalog,
    load_local,
    parse_project_dirs,
    resolve,
    with_login,
)
from kd2_rules_mcp.server import create_server
from kd2_rules_mcp.service import Kd2Service, Settings

ROOT = Path(__file__).resolve().parents[1]
DATA = Path(__file__).parent / "data"
sys.path.insert(0, str(ROOT / "scripts"))

import setup_local  # noqa: E402 — скрипт из scripts/, не пакет

CATALOG = """
projects:
  alpha:
    name: Альфа
    mcp_config: Проект/.mcp.json
    configurations:
      full:
        dump: Проект/main
        extensions: [Проект/ext]
    bases:
      sandbox:
        {role: песочница, configuration: full, connection: 'File="C:\\\\A";', data_mcp: data-a,
         dev_env: Проект/.dev.env}
      prod: {role: боевая, configuration: full, connection: 'Srvr="s";Ref="p";', data_mcp: data-a}
    code_mcp: [code-a, missing-a]
  beta:
    name: Бета
    configurations:
      full: {dump: Проект/main}
shared_mcp: {from: alpha, names: [docs]}
exchanges:
  - {plan: ОбменАльфаБета, projects: [alpha, beta]}
"""


def _write_catalog(folder: Path, text: str = CATALOG) -> Path:
    path = folder / "projects.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def test_repository_catalog_is_valid() -> None:
    """Пример каталога из репозитория разбирается и описывает типовые проекты."""
    catalog = load_catalog(ROOT / "projects.example.yaml")
    assert {"bp", "zup", "do"} <= set(catalog.projects)
    for project in catalog.projects.values():
        assert "full" in project.configurations
    assert catalog.base("bp", "bp_dev").is_sandbox


def test_catalog_lookup_errors_list_known(tmp_path: Path) -> None:
    catalog = load_catalog(_write_catalog(tmp_path))
    assert catalog.configuration("alpha", "full").extensions == ("Проект/ext",)
    assert not catalog.base("alpha", "prod").is_sandbox
    with pytest.raises(ProjectConfigError, match="alpha, beta"):
        catalog.project("gamma")
    with pytest.raises(ProjectConfigError, match="sandbox, prod"):
        catalog.base("alpha", "test")


@pytest.mark.parametrize(
    ("broken", "message"),
    [
        ("role: песочница", "role: тест"),
        ("configuration: full, connection: 'File", "configuration: none, connection: 'File"),
        ("projects: [alpha, beta]", "projects: [alpha, gamma]"),
    ],
)
def test_catalog_rejects_inconsistent_file(tmp_path: Path, broken: str, message: str) -> None:
    with pytest.raises(ProjectConfigError):
        load_catalog(_write_catalog(tmp_path, CATALOG.replace(broken, message, 1)))


def test_local_settings_and_dirs(tmp_path: Path) -> None:
    local = tmp_path / "projects.local.yaml"
    local.write_text(
        "projects:\n  alpha: D:\\Work\\Альфа\nserver_url: http://srv:8060/mcp\n", encoding="utf-8"
    )
    settings = load_local(local)
    assert settings.project_dirs == {"alpha": Path("D:\\Work\\Альфа")}
    assert settings.server_url == "http://srv:8060/mcp"
    assert parse_project_dirs("alpha=/projects/alpha; beta=/projects/beta") == {
        "alpha": Path("/projects/alpha"),
        "beta": Path("/projects/beta"),
    }
    assert resolve(Path("/p"), "Проект\\main") == Path("/p/Проект/main")


def test_load_local_bind_requires_token_off_loopback(tmp_path: Path) -> None:
    """Внешний bind без token — ошибка; с token поля читаются; без bind — None."""
    path = tmp_path / "projects.local.yaml"
    path.write_text("bind: 0.0.0.0\n", encoding="utf-8")
    with pytest.raises(ProjectConfigError, match="для сервера на внешнем интерфейсе задайте token"):
        load_local(path)
    path.write_text('bind: "0.0.0.0"\ntoken: "  "\n', encoding="utf-8")
    with pytest.raises(ProjectConfigError, match="задайте token"):
        load_local(path)
    path.write_text("bind: 0.0.0.0\ntoken: секрет\n", encoding="utf-8")
    settings = load_local(path)
    assert settings.bind == "0.0.0.0"
    assert settings.token == "секрет"
    path.write_text("projects: {}\n", encoding="utf-8")
    bare = load_local(path)
    assert bare.bind is None
    assert bare.token is None
    path.write_text("token: секрет\n", encoding="utf-8")
    token_only = load_local(path)
    assert token_only.bind is None
    assert token_only.token == "секрет"
    path.write_text("bind: 127.0.0.1\n", encoding="utf-8")
    assert load_local(path).token is None
    path.write_text("bind: LocalHost\n", encoding="utf-8")
    assert load_local(path).bind == "LocalHost"
    path.write_text("bind: '::1'\n", encoding="utf-8")
    assert load_local(path).bind == "::1"


def test_compose_override_ports_and_bearer(tmp_path: Path) -> None:
    """Без bind в override нет ports; с bind и token — !override, KD2_TOKEN и Bearer."""
    catalog = load_catalog(_write_catalog(tmp_path))
    plain = LocalSettings()
    plain_text = setup_local.compose_override(catalog, plain)
    plain_service = yaml.safe_load(plain_text)["services"]["kd2-rules-mcp"]
    assert "ports" not in plain_service
    assert "KD2_TOKEN" not in plain_service["environment"]
    servers, _warnings = setup_local.mcp_servers(catalog, plain)
    assert "headers" not in servers["kd2-rules-mcp"]

    bound = LocalSettings(
        bind="192.0.2.10", token="секрет", server_url="http://192.0.2.10:8060/mcp"
    )
    text = setup_local.compose_override(catalog, bound)
    assert "!override" in text
    assert "192.0.2.10:8060:8060" in text
    loaded = yaml.load(text, Loader=_OverrideLoader)
    service = loaded["services"]["kd2-rules-mcp"]
    assert service["ports"] == ["192.0.2.10:8060:8060"]
    assert service["environment"]["KD2_TOKEN"] == "секрет"
    servers, _warnings = setup_local.mcp_servers(catalog, bound)
    claude = {"mcpServers": {name: {"type": "http", **entry} for name, entry in servers.items()}}
    assert claude["mcpServers"]["kd2-rules-mcp"]["headers"] == {"Authorization": "Bearer секрет"}
    assert servers["kd2-rules-mcp"]["headers"]["Authorization"] == "Bearer секрет"
    assert setup_local.localhost_server_url_warning(bound) is None
    loop = LocalSettings(bind="192.0.2.10", token="секрет")
    assert setup_local.localhost_server_url_warning(loop) is not None


class _OverrideLoader(yaml.SafeLoader):
    """Разбирает тег compose `!override` как обычный список."""


def _construct_override(loader: yaml.SafeLoader, node: Any) -> list[object]:
    return list(loader.construct_sequence(node))


_OverrideLoader.add_constructor("!override", _construct_override)


def test_load_local_server_url_defaults_to_address(tmp_path: Path) -> None:
    path = tmp_path / "projects.local.yaml"
    path.write_text("projects: {}\n", encoding="utf-8")
    settings = load_local(path)
    assert settings.server_url == "http://localhost:8060/mcp"
    assert isinstance(settings.server_url, str)
    path.write_text("server_url: http://srv:9/mcp\n", encoding="utf-8")
    assert load_local(path).server_url == "http://srv:9/mcp"


def test_local_logins_extend_connection(tmp_path: Path) -> None:
    local = tmp_path / "projects.local.yaml"
    local.write_text(
        "logins:\n  alpha.sandbox: {user: Агент, password: секрет}\n", encoding="utf-8"
    )
    settings = load_local(local)
    login = settings.login("alpha", "sandbox")
    assert settings.login("alpha", "prod") is None
    assert with_login('Srvr="s";Ref="a";', login) == 'Srvr="s";Ref="a";Usr="Агент";Pwd="секрет";'
    assert with_login('File="C:\\A";', None) == 'File="C:\\A";'
    env = tmp_path / ".dev.env"
    env.write_text('# база\nIB_USER="Администратор"\nIB_PASSWORD=123\n', encoding="utf-8")
    assert dev_env_login(env) == ("Администратор", "123")
    env.write_text("IB_USER=\nIB_PASSWORD=x\n", encoding="utf-8")
    assert dev_env_login(env) is None
    assert dev_env_login(tmp_path / "нет.env") is None
    local.write_text("logins:\n  alpha: {user: Агент}\n", encoding="utf-8")
    with pytest.raises(ProjectConfigError, match=r"<проект>\.<база>"):
        load_local(local)


def test_setup_generates_mounts_and_agent_servers(tmp_path: Path) -> None:
    catalog = load_catalog(_write_catalog(tmp_path))
    alpha = tmp_path / "Альфа"
    (alpha / "Проект").mkdir(parents=True)
    (alpha / "Проект" / ".mcp.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "code-a": {"url": "http://h:1/mcp"},
                    "docs": {"url": "http://h:2/mcp"},
                    "data-a": {"url": "http://h:3/sandbox/hs/mcp"},
                }
            }
        ),
        encoding="utf-8",
    )
    (alpha / "Проект" / ".dev.env").write_text("IB_USER=Агент\nIB_PASSWORD=1\n", encoding="utf-8")
    local = load_local_from(tmp_path, {"alpha": str(alpha)})
    override = yaml.safe_load(setup_local.compose_override(catalog, local))
    service = override["services"]["kd2-rules-mcp"]
    assert f"{str(alpha).replace(chr(92), '/')}:/projects/alpha:ro" in service["volumes"]
    assert service["environment"]["KD2_PROJECT_DIRS"] == "alpha=/projects/alpha"
    assert f"{alpha}=/projects/alpha" in service["environment"]["KD2_PATH_MAP"]
    servers, warnings = setup_local.mcp_servers(catalog, local)
    assert servers["kd2-rules-mcp"] == {"url": local.server_url}
    assert servers["docs"] == {"url": "http://h:2/mcp"}
    assert servers["alpha-code-a"] == {"url": "http://h:1/mcp"}
    # Сервер данных песочницы — с Basic-авторизацией логином базы из .dev.env (UTF-8).
    token = base64.b64encode("Агент:1".encode()).decode()
    assert servers["alpha-data-a"] == {
        "url": "http://h:3/sandbox/hs/mcp",
        "headers": {"Authorization": f"Basic {token}"},
    }
    assert any("missing-a" in warning for warning in warnings)


def load_local_from(folder: Path, dirs: dict[str, str]) -> LocalSettings:
    path = folder / "projects.local.yaml"
    path.write_text(yaml.safe_dump({"projects": dirs}, allow_unicode=True), encoding="utf-8")
    return load_local(path)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.anyio
async def test_project_tools_load_by_name(tmp_path: Path) -> None:
    catalog = _write_catalog(tmp_path)
    alpha = tmp_path / "Альфа"
    shutil.copytree(DATA / "xmldump" / "main", alpha / "Проект" / "main")
    shutil.copytree(DATA / "xmldump" / "ext", alpha / "Проект" / "ext")
    settings = Settings(
        cache_dir=tmp_path / "cache",
        workspace=tmp_path / "ws",
        projects_file=catalog,
        project_dirs={"alpha": alpha},
    )
    async with Client(create_server(Kd2Service(settings))) as client:
        listed = (await client.call_tool("project_list", {})).structured_content
        assert listed is not None
        by_id = {item["project"]: item for item in listed["projects"]}
        assert by_id["alpha"]["available"] is True
        assert by_id["beta"]["available"] is False
        assert by_id["alpha"]["bases"]["prod"]["role"] == "боевая"
        # Сервер данных показывается только у песочницы.
        assert by_id["alpha"]["bases"]["sandbox"]["data_mcp"] == "data-a"
        assert by_id["alpha"]["bases"]["sandbox"]["data_mcp_server"] == "alpha-data-a"
        assert "data_mcp" not in by_id["alpha"]["bases"]["prod"]
        loaded = (
            await client.call_tool("structure_load_project", {"project": "alpha"})
        ).structured_content
        assert loaded is not None
        assert loaded["structure_id"] == "alpha-full"
        assert loaded["reused"] is False
        again = (
            await client.call_tool("structure_load_project", {"project": "alpha"})
        ).structured_content
        assert again is not None and again["reused"] is True
        missing = await client.call_tool("structure_load_project", {"project": "beta"})
        assert missing.is_error
        text = "".join(getattr(item, "text", "") for item in missing.content)
        assert "beta" in text
