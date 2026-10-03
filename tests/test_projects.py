"""Профили проектов: общий projects.yaml, личный projects.local.yaml, генерация настроек машины."""

import base64
import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml
from mcp import Client

from kd2_rules_mcp.projects import (
    LocalSettings,
    ProjectConfigError,
    compose_env,
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


def test_load_local_port_and_instance(tmp_path: Path) -> None:
    """`port` и `instance` читаются; пробел в суффиксе и порт 0 — отказ."""
    path = tmp_path / "projects.local.yaml"
    path.write_text("port: 8061\ninstance: stand\n", encoding="utf-8")
    settings = load_local(path)
    assert settings.port == 8061
    assert settings.instance == "stand"
    assert compose_env(settings) == {
        "KD2_CONTAINER": "kd2_rules_mcp_stand",
        "KD2_CACHE_VOLUME": "kd2_structures_cache_stand",
        "COMPOSE_PROJECT_NAME": "kd2-rules-mcp-stand",
        "KD2_PUBLISHED_PORT": "8061",
    }
    path.write_text("projects: {}\n", encoding="utf-8")
    bare = load_local(path)
    assert bare.port is None
    assert bare.instance is None
    assert compose_env(bare)["KD2_PUBLISHED_PORT"] == "8060"
    path.write_text("instance: a b\n", encoding="utf-8")
    with pytest.raises(ProjectConfigError, match="instance"):
        load_local(path)
    path.write_text("port: 0\n", encoding="utf-8")
    with pytest.raises(ProjectConfigError, match="1…65535"):
        load_local(path)


def test_compose_override_ports_and_bearer(tmp_path: Path) -> None:
    """В override нет ports; bind уходит в .env, token — в KD2_TOKEN и Bearer."""
    catalog = load_catalog(_write_catalog(tmp_path))
    plain = LocalSettings()
    plain_service = yaml.safe_load(setup_local.compose_override(catalog, plain))["services"][
        "kd2-rules-mcp"
    ]
    assert "ports" not in plain_service
    assert "KD2_TOKEN" not in plain_service["environment"]
    assert setup_local.render_env(plain) is None
    servers, _warnings = setup_local.mcp_servers(catalog, plain)
    assert "headers" not in servers["kd2-rules-mcp"]

    bound = LocalSettings(
        bind="192.0.2.10", token="секрет", server_url="http://192.0.2.10:8060/mcp"
    )
    service = yaml.safe_load(setup_local.compose_override(catalog, bound))["services"][
        "kd2-rules-mcp"
    ]
    assert "ports" not in service
    assert service["environment"]["KD2_TOKEN"] == "секрет"
    env = setup_local.render_env(bound)
    assert env is not None
    assert "KD2_BIND=192.0.2.10" in env
    assert "KD2_PUBLISHED_PORT" not in env
    assert "KD2_CONTAINER" not in env
    servers, _warnings = setup_local.mcp_servers(catalog, bound)
    claude = {"mcpServers": {name: {"type": "http", **entry} for name, entry in servers.items()}}
    assert claude["mcpServers"]["kd2-rules-mcp"]["headers"] == {"Authorization": "Bearer секрет"}
    assert servers["kd2-rules-mcp"]["headers"]["Authorization"] == "Bearer секрет"
    assert setup_local.localhost_server_url_warning(bound) is None
    loop = LocalSettings(bind="192.0.2.10", token="секрет")
    assert setup_local.localhost_server_url_warning(loop) is not None


def test_env_file_port_instance_and_server_url_warning(tmp_path: Path) -> None:
    """По умолчанию .env не пишется; port и instance — все переменные имён; порт URL сверяется."""
    catalog = load_catalog(_write_catalog(tmp_path))
    target = tmp_path / ".env"
    # Чужой .env (без заголовка скрипта — ручные переменные второго экземпляра) остаётся на месте.
    target.write_text("COMPOSE_PROJECT_NAME=other\n", encoding="utf-8")
    assert setup_local.write_env_file(target, LocalSettings()) is True
    assert target.read_text(encoding="utf-8") == "COMPOSE_PROJECT_NAME=other\n"
    # Свой прежний .env удаляется, чтобы старые порт и имена не остались в силе.
    target.write_text(setup_local.HEADER + "KD2_PUBLISHED_PORT=1\n", encoding="utf-8")
    assert setup_local.write_env_file(target, LocalSettings()) is False
    assert not target.exists()

    local = LocalSettings(port=8061, instance="stand")
    text = setup_local.render_env(local)
    assert text is not None and "не править" in text
    assert "KD2_BIND" not in text
    for line in (
        "KD2_PUBLISHED_PORT=8061",
        "KD2_CONTAINER=kd2_rules_mcp_stand",
        "KD2_CACHE_VOLUME=kd2_structures_cache_stand",
        "COMPOSE_PROJECT_NAME=kd2-rules-mcp-stand",
    ):
        assert line in text
    service = yaml.safe_load(setup_local.compose_override(catalog, local))["services"][
        "kd2-rules-mcp"
    ]
    assert "ports" not in service
    warning = setup_local.server_url_port_warning(local)
    assert warning is not None and "8061" in warning and "8060" in warning
    matched = LocalSettings(port=8061, server_url="http://localhost:8061/mcp")
    assert setup_local.server_url_port_warning(matched) is None
    assert setup_local.server_url_port_warning(LocalSettings()) is None


def test_compose_config_resolves_publication_from_env(tmp_path: Path) -> None:
    """`docker compose config` подставляет .env; контейнер не запускается.

    Имя контейнера из одного символа Compose v5 не принимает (шаблон требует два), поэтому
    в проверке `xx`, а том `y` и проект `z` — как заданы.
    """
    compose = ROOT / "docker-compose.yml"
    bare = _compose_config(tmp_path / "bare", compose)
    bare_service = bare["services"]["kd2-rules-mcp"]
    assert _published(bare_service) == ("127.0.0.1", "8060")
    assert len(bare_service["ports"]) == 1
    assert bare_service["container_name"] == "kd2_rules_mcp"
    assert bare["volumes"]["kd2_cache"]["name"] == "kd2_structures_cache"
    assert bare["name"] == "kd2-rules-mcp"

    project = tmp_path / "with-env"
    project.mkdir()
    env_file = project / ".env"
    env_file.write_text(
        "KD2_PUBLISHED_PORT=8061\nKD2_CONTAINER=xx\nKD2_CACHE_VOLUME=y\nCOMPOSE_PROJECT_NAME=z\n",
        encoding="utf-8",
        newline="\n",
    )
    loaded = _compose_config(project, compose, env_file)
    service = loaded["services"]["kd2-rules-mcp"]
    assert _published(service) == ("127.0.0.1", "8061")
    assert len(service["ports"]) == 1
    assert service["container_name"] == "xx"
    assert loaded["volumes"]["kd2_cache"]["name"] == "y"
    assert loaded["name"] == "z"


def _compose_config(directory: Path, compose: Path, env_file: Path | None = None) -> dict[str, Any]:
    """Разобранный `docker compose config` без подъёма контейнера."""
    directory.mkdir(parents=True, exist_ok=True)
    command = [
        "docker",
        "compose",
        "--project-directory",
        str(directory),
        "-f",
        str(compose),
        "config",
        "--format",
        "json",
    ]
    if env_file is not None:
        command[2:2] = ["--env-file", str(env_file)]
    completed = subprocess.run(command, check=True, capture_output=True, text=True)
    return json.loads(completed.stdout)


def _published(service: dict[str, Any]) -> tuple[str, str]:
    port = service["ports"][0]
    return str(port["host_ip"]), str(port["published"])


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
