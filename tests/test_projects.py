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

from kd_rules_mcp.projects import (
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
from kd_rules_mcp.server import create_server
from kd_rules_mcp.service import Kd2Service, Settings

ROOT = Path(__file__).resolve().parents[1]
DATA = Path(__file__).parent / "data"
sys.path.insert(0, str(ROOT / "scripts"))

import setup_local  # noqa: E402 — скрипт из scripts/, не пакет


@pytest.mark.parametrize(
    "value,expected",
    [
        (None, None),
        ([], ()),
        (
            ["192.0.2.10", "HOST.Example", "2001:db8:0:0::1"],
            ("192.0.2.10", "host.example", "2001:db8::1"),
        ),
    ],
)
def test_local_host_addresses(tmp_path: Path, value, expected):
    path = tmp_path / "projects.local.yaml"
    path.write_text(yaml.safe_dump({"host_addresses": value}), encoding="utf-8")
    assert load_local(path).host_addresses == expected


@pytest.mark.parametrize(
    "value", ["192.0.2.10", [False], [""], ["http://host/base"], ["host:8081"], ["host;other"]]
)
def test_local_host_addresses_rejects_non_hosts(tmp_path: Path, value):
    path = tmp_path / "projects.local.yaml"
    path.write_text(yaml.safe_dump({"host_addresses": value}), encoding="utf-8")
    with pytest.raises(ProjectConfigError, match="host_addresses"):
        load_local(path)


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
        "KD2_CONTAINER": "kd_rules_mcp_stand",
        "KD2_CACHE_VOLUME": "kd2_structures_cache_stand",
        "COMPOSE_PROJECT_NAME": "kd-rules-mcp-stand",
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
        "kd-rules-mcp"
    ]
    assert "ports" not in plain_service
    assert "KD2_TOKEN" not in plain_service["environment"]
    assert setup_local.render_env(plain) is None
    servers, _warnings = setup_local.mcp_servers(catalog, plain)
    assert "headers" not in servers["kd-rules-mcp"]

    bound = LocalSettings(
        bind="192.0.2.10", token="секрет", server_url="http://192.0.2.10:8060/mcp"
    )
    service = yaml.safe_load(setup_local.compose_override(catalog, bound))["services"][
        "kd-rules-mcp"
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
    assert claude["mcpServers"]["kd-rules-mcp"]["headers"] == {"Authorization": "Bearer секрет"}
    assert servers["kd-rules-mcp"]["headers"]["Authorization"] == "Bearer секрет"
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
        "KD2_CONTAINER=kd_rules_mcp_stand",
        "KD2_CACHE_VOLUME=kd2_structures_cache_stand",
        "COMPOSE_PROJECT_NAME=kd-rules-mcp-stand",
    ):
        assert line in text
    service = yaml.safe_load(setup_local.compose_override(catalog, local))["services"][
        "kd-rules-mcp"
    ]
    assert "ports" not in service
    warning = setup_local.server_url_port_warning(local)
    assert warning is not None and "8061" in warning and "8060" in warning
    matched = LocalSettings(port=8061, server_url="http://localhost:8061/mcp")
    assert setup_local.server_url_port_warning(matched) is None
    assert setup_local.server_url_port_warning(LocalSettings()) is None


def test_legacy_runtime_notice_names_the_stop_command(monkeypatch: pytest.MonkeyPatch) -> None:
    """Подставной docker: найден прежний контейнер или проект — одна строка с командой остановки."""
    monkeypatch.setattr(setup_local, "DEFAULT_CONTAINER_NAME", "other_container")
    monkeypatch.setattr(setup_local, "DEFAULT_COMPOSE_PROJECT", "other-project")
    calls: list[list[str]] = []

    def runner(args: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(list(args))
        if args[1] == "ps":
            return subprocess.CompletedProcess(args, 0, setup_local.LEGACY_CONTAINER + "\n", "")
        payload = json.dumps([{"Name": setup_local.LEGACY_COMPOSE_PROJECT, "Status": "running(1)"}])
        return subprocess.CompletedProcess(args, 0, payload, "")

    text = setup_local.legacy_runtime_notice(runner)
    assert text is not None
    assert "\n" not in text
    assert f"docker compose -p {setup_local.LEGACY_COMPOSE_PROJECT} down" in text
    assert setup_local.LEGACY_CONTAINER in text
    assert calls[0][:2] == ["docker", "ps"]
    assert calls[1][:3] == ["docker", "compose", "ls"]
    assert all("stop" not in call and "down" not in call and "rm" not in call for call in calls)

    def only_container(args: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        if args[1] == "ps":
            return subprocess.CompletedProcess(args, 0, setup_local.LEGACY_CONTAINER + "\n", "")
        return subprocess.CompletedProcess(args, 0, "[]", "")

    container_only = setup_local.legacy_runtime_notice(only_container)
    assert container_only is not None
    assert f"docker stop {setup_local.LEGACY_CONTAINER}" in container_only
    assert "down" not in container_only

    def missing(_args: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        raise FileNotFoundError

    assert setup_local.legacy_runtime_notice(missing) is None

    def unavailable(args: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args, 1, "", "Cannot connect to the Docker daemon")

    assert setup_local.legacy_runtime_notice(unavailable) is None

    def empty(args: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        out = "[]" if args[1] == "compose" else ""
        return subprocess.CompletedProcess(args, 0, out, "")

    assert setup_local.legacy_runtime_notice(empty) is None


def test_legacy_runtime_notice_silent_while_names_are_current(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Пока текущие имена совпадают с прежними, docker не спрашиваем."""
    monkeypatch.setattr(setup_local, "DEFAULT_CONTAINER_NAME", setup_local.LEGACY_CONTAINER)
    monkeypatch.setattr(setup_local, "DEFAULT_COMPOSE_PROJECT", setup_local.LEGACY_COMPOSE_PROJECT)
    calls: list[list[str]] = []

    def runner(args: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(list(args))
        return subprocess.CompletedProcess(args, 0, setup_local.LEGACY_CONTAINER, "")

    assert setup_local.legacy_runtime_notice(runner) is None
    assert calls == []


def test_compose_config_resolves_publication_from_env(tmp_path: Path) -> None:
    """`docker compose config` подставляет .env; контейнер не запускается.

    Имя контейнера из одного символа Compose v5 не принимает (шаблон требует два), поэтому
    в проверке `xx`, а том `y` и проект `z` — как заданы.
    """
    compose = ROOT / "docker-compose.yml"
    bare = _compose_config(tmp_path / "bare", compose)
    bare_service = bare["services"]["kd-rules-mcp"]
    assert _published(bare_service) == ("127.0.0.1", "8060")
    assert len(bare_service["ports"]) == 1
    assert bare_service["container_name"] == "kd_rules_mcp"
    assert bare["volumes"]["kd2_cache"]["name"] == "kd2_structures_cache"
    assert bare["name"] == "kd-rules-mcp"

    project = tmp_path / "with-env"
    project.mkdir()
    env_file = project / ".env"
    env_file.write_text(
        "KD2_PUBLISHED_PORT=8061\nKD2_CONTAINER=xx\nKD2_CACHE_VOLUME=y\nCOMPOSE_PROJECT_NAME=z\n",
        encoding="utf-8",
        newline="\n",
    )
    loaded = _compose_config(project, compose, env_file)
    service = loaded["services"]["kd-rules-mcp"]
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
    service = override["services"]["kd-rules-mcp"]
    assert f"{str(alpha).replace(chr(92), '/')}:/projects/alpha:ro" in service["volumes"]
    assert service["environment"]["KD2_PROJECT_DIRS"] == "alpha=/projects/alpha"
    assert f"{alpha}=/projects/alpha" in service["environment"]["KD2_PATH_MAP"]
    servers, warnings = setup_local.mcp_servers(catalog, local)
    assert servers["kd-rules-mcp"] == {"url": local.server_url}
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


def test_generated_configs_replace_previous_server_key(tmp_path: Path) -> None:
    """Генерация переписывает файлы целиком: прежний ключ не остаётся второй записью."""
    catalog = load_catalog(_write_catalog(tmp_path))
    local = LocalSettings()
    previous = "kd2" + "-rules-mcp"
    override = tmp_path / "docker-compose.override.yml"
    override.write_text(f"services:\n  {previous}: {{}}\n", encoding="utf-8")
    override.write_bytes(setup_local.compose_override(catalog, local).encode("utf-8"))
    services = yaml.safe_load(override.read_text(encoding="utf-8"))["services"]
    assert list(services) == [setup_local.SERVER]
    assert previous not in services or previous == setup_local.SERVER

    servers, _warnings = setup_local.mcp_servers(catalog, local)
    stale = {
        "mcpServers": {previous: {"url": "http://old/mcp"}, "keep": {"url": "http://keep/mcp"}}
    }
    for name in (".mcp.json", ".cursor/mcp.json"):
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(stale), encoding="utf-8")
        if name == ".mcp.json":
            payload = {
                "mcpServers": {key: {"type": "http", **entry} for key, entry in servers.items()}
            }
        else:
            payload = {"mcpServers": servers}
        setup_local._write_json(path, payload)
        written = json.loads(path.read_text(encoding="utf-8"))["mcpServers"]
        assert list(written) == list(servers)
        assert "keep" not in written
        assert previous not in written or previous == setup_local.SERVER


def _profile_catalog(tmp_path: Path, exchanges: list[dict]):
    data = yaml.safe_load(CATALOG)
    data["exchanges"] = exchanges
    return load_catalog(_write_catalog(tmp_path, yaml.safe_dump(data, allow_unicode=True)))


def _profile() -> dict:
    return {
        "plan": "План",
        "technology": "kd2",
        "sides": [
            {"project": "alpha", "base": "sandbox", "rules": "rules/alpha"},
            {"project": "beta", "rules": "rules/beta"},
        ],
    }


def test_exchange_old_form_and_duplicate_plan_ids(tmp_path: Path) -> None:
    old = {"plan": "План", "projects": ["alpha", "beta"]}
    exchange = _profile_catalog(tmp_path, [old]).exchanges[0]
    assert exchange.id == "План"
    assert exchange.sides == () and exchange.directions == ()
    repeated = _profile_catalog(tmp_path, [old, {**old, "projects": ["beta", "alpha"]}])
    assert [e.id for e in repeated.exchanges] == ["План@alpha+beta", "План@beta+alpha"]


def test_exchange_profile_defaults(tmp_path: Path) -> None:
    exchange = _profile_catalog(tmp_path, [_profile()]).exchanges[0]
    assert exchange.projects == ("alpha", "beta")
    assert exchange.directions == (("alpha", "beta"), ("beta", "alpha"))
    assert exchange.technology == "kd2"
    assert exchange.sides[0].base == "sandbox"
    assert exchange.sides[0].rules == "rules/alpha"
    assert exchange.sides[1].plan == "План"


@pytest.mark.parametrize(
    "patch, message",
    [
        (
            {"sides": [{"project": "alpha", "base": "missing"}, {"project": "beta"}]},
            r"sides\[0\].base.*missing",
        ),
        ({"sides": [{"project": "alpha"}]}, "ровно две"),
        ({"sides": [{"project": "alpha"}] * 3}, "ровно две"),
        ({"sides": [{"project": "alpha"}] * 2}, "одинаковые проекты"),
        ({"sides": [{"project": "absent"}, {"project": "beta"}]}, "sides.*project.*absent"),
        ({"projects": ["beta", "alpha"]}, "projects.*расходятся"),
        ({"technology": "xml"}, "technology.*kd2.*ed"),
        ({"technology": "ed"}, "sides.*rules.*ed"),
        (
            {"sides": [{"project": "alpha", "module": "Module"}, {"project": "beta"}]},
            "sides.*module.*kd2",
        ),
        ({"directions": [["alpha", "other"]]}, "directions.*проектов сторон"),
        ({"directions": [["alpha", "alpha"]]}, "directions.*разных"),
        ({"directions": [["alpha", "beta"], ["alpha", "beta"]]}, "directions.*повтор"),
        (
            {"sides": [{"project": "alpha", "rule": "rules"}, {"project": "beta"}]},
            "sides.*rule.*неизвестный ключ",
        ),
        ({"password": "not-a-real-secret"}, "password.*неизвестный ключ"),
    ],
)
def test_exchange_profile_refuses_invalid_fields(tmp_path: Path, patch: dict, message: str) -> None:
    with pytest.raises(ProjectConfigError, match=message):
        _profile_catalog(tmp_path, [{**_profile(), **patch}])


def test_exchange_directions_require_sides(tmp_path: Path) -> None:
    with pytest.raises(ProjectConfigError, match=r"directions.*без sides"):
        _profile_catalog(
            tmp_path, [{"plan": "План", "projects": ["alpha", "beta"], "directions": []}]
        )


def test_exchange_legacy_forms_still_read(tmp_path: Path) -> None:
    """Формы, которые прежний разбор принимал: пустой словарь и повтор одинакового элемента."""
    assert _profile_catalog(tmp_path, {}).exchanges == ()  # type: ignore[arg-type]
    old = {"plan": "План", "projects": ["alpha", "beta"]}
    ids = [e.id for e in _profile_catalog(tmp_path, [old, dict(old)]).exchanges]
    assert ids == ["План@alpha+beta", "План@alpha+beta#2"]


@pytest.mark.parametrize(
    "rules", ["../beta/kit", "a/../../etc", "/etc/kit", "C:/Rules/kit", "C:kit"]
)
def test_exchange_rules_stay_inside_project(tmp_path: Path, rules: str) -> None:
    profile = _profile()
    profile["sides"][0]["rules"] = rules
    with pytest.raises(ProjectConfigError, match=r"sides\[0\].rules.*от папки проекта"):
        _profile_catalog(tmp_path, [profile])


def test_exchange_empty_directions_refused(tmp_path: Path) -> None:
    with pytest.raises(ProjectConfigError, match=r"directions.*хотя бы одно"):
        _profile_catalog(tmp_path, [{**_profile(), "directions": []}])


def test_exchange_duplicate_explicit_ids(tmp_path: Path) -> None:
    with pytest.raises(ProjectConfigError, match=r"id.*одинаковые.*same"):
        _profile_catalog(tmp_path, [{**_profile(), "id": "same"}, {**_profile(), "id": "same"}])


def _rules_dir_catalog(tmp_path: Path, exchange: dict, rules_dir: str = "Проект/ПравилаОбмена"):
    data = yaml.safe_load(CATALOG)
    data["projects"]["alpha"]["rules_dir"] = rules_dir
    data["exchanges"] = [exchange]
    return load_catalog(_write_catalog(tmp_path, yaml.safe_dump(data, allow_unicode=True)))


def test_exchange_rules_default_from_rules_dir(tmp_path: Path) -> None:
    """Без `rules` — `<rules_dir>/<план стороны>`; явный `rules` главнее; без `rules_dir` — нет."""
    profile = {
        "plan": "План",
        "technology": "kd2",
        "sides": [{"project": "alpha", "plan": "ПланАльфы"}, {"project": "beta"}],
    }
    alpha, beta = _rules_dir_catalog(tmp_path, profile).exchanges[0].sides
    assert (alpha.rules, alpha.rules_default) == ("Проект/ПравилаОбмена/ПланАльфы", True)
    assert (beta.rules, beta.rules_default) == (None, False)
    profile["sides"][0]["rules"] = "свои/План"
    alpha = _rules_dir_catalog(tmp_path, profile).exchanges[0].sides[0]
    assert (alpha.rules, alpha.rules_default) == ("свои/План", False)


@pytest.mark.parametrize("rules_dir", ["Проект\\ПравилаОбмена\\", "Проект/ПравилаОбмена/"])
def test_exchange_rules_default_normalizes_separators(tmp_path: Path, rules_dir: str) -> None:
    profile = {"plan": "План", "sides": [{"project": "alpha"}, {"project": "beta"}]}
    alpha = _rules_dir_catalog(tmp_path, profile, rules_dir).exchanges[0].sides[0]
    assert alpha.rules == "Проект/ПравилаОбмена/План"


@pytest.mark.parametrize(
    "profile",
    [
        {"plan": "План", "technology": "ed", "sides": [{"project": "alpha"}, {"project": "beta"}]},
        {"plan": "План", "sides": [{"project": "alpha"}, {"project": "beta", "module": "М"}]},
        {"plan": "План", "projects": ["alpha", "beta"]},
    ],
    ids=["ed", "module-without-technology", "legacy"],
)
def test_exchange_rules_default_only_for_kd2_sides(tmp_path: Path, profile: dict) -> None:
    exchange = _rules_dir_catalog(tmp_path, profile).exchanges[0]
    assert all(side.rules is None for side in exchange.sides)


@pytest.mark.parametrize("rules_dir", ["../общие/ПравилаОбмена", "C:/Правила"])
def test_exchange_rules_default_skips_unusual_rules_dir(tmp_path: Path, rules_dir: str) -> None:
    """Прежде такой каталог читался: значения по умолчанию нет, отказа тоже."""
    profile = {"plan": "План", "sides": [{"project": "alpha"}, {"project": "beta"}]}
    alpha = _rules_dir_catalog(tmp_path, profile, rules_dir).exchanges[0].sides[0]
    assert alpha.rules is None
