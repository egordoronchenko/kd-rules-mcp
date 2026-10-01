"""Профили проектов 1С: общий `projects.yaml` и личный `projects.local.yaml`.

Общий файл (в git) описывает проект → конфигурации (выгрузка и расширения, пути от папки проекта)
→ базы (роль, конфигурация, строка соединения), серверы кода проекта и обмены. Личный файл
машины говорит только, где лежит папка каждого проекта, — поэтому общий файл одинаков у всех,
а проекты могут лежать на любых дисках. Путь выгрузки = папка проекта + путь из общего файла.

Сервер в контейнере получает папки проектов переменной `KD2_PROJECT_DIRS` (`id=путь;…`),
которую пишет `scripts/setup_local.py`.
"""

import base64
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from kd2_rules_mcp.errors import Kd2Error

SANDBOX = "песочница"
PRODUCTION = "боевая"
ROLES = (SANDBOX, PRODUCTION)
DEFAULT_SERVER_URL = "http://localhost:8060/mcp"


class ProjectConfigError(Kd2Error):
    """Ошибка в файле проектов или неизвестный проект, конфигурация, база."""


@dataclass(frozen=True, slots=True)
class Configuration:
    """Конфигурация проекта: выгрузка и расширения в порядке наложения (пути от папки проекта)."""

    id: str
    dump: str
    extensions: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Base:
    """Информационная база проекта."""

    id: str
    role: str
    configuration: str
    connection: str
    # Сервер данных ИБ (1c-data-mcp) из .mcp.json проекта; агентам подключается только у песочниц.
    data_mcp: str = ""
    # .dev.env проекта (от папки проекта) с IB_USER / IB_PASSWORD этой базы — логин для проверок.
    dev_env: str = ""

    @property
    def is_sandbox(self) -> bool:
        """В базу можно подключаться скриптам и писать."""
        return self.role == SANDBOX


@dataclass(frozen=True, slots=True)
class Project:
    """Проект 1С."""

    id: str
    name: str
    configurations: dict[str, Configuration]
    bases: dict[str, Base]
    code_mcp: tuple[str, ...] = ()
    # .mcp.json проекта (путь от папки проекта): адреса серверов кода и общих серверов.
    mcp_config: str = ""
    # Папка живых правил обмена (путь от папки проекта) — серверу разрешена запись в неё.
    rules_dir: str = ""


@dataclass(frozen=True, slots=True)
class Exchange:
    """План обмена и проекты, между которыми он работает."""

    plan: str
    projects: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Catalog:
    """Все проекты общего файла."""

    projects: dict[str, Project]
    exchanges: tuple[Exchange, ...] = ()
    shared_mcp_from: str = ""
    shared_mcp: tuple[str, ...] = ()

    def project(self, project_id: str) -> Project:
        """Проект по идентификатору или ошибка со списком известных."""
        found = self.projects.get(project_id)
        if found is None:
            known = ", ".join(self.projects) or "нет"
            raise ProjectConfigError(f"Проекта «{project_id}» нет в projects.yaml (есть: {known})")
        return found

    def configuration(self, project_id: str, configuration_id: str) -> Configuration:
        """Конфигурация проекта или ошибка со списком его конфигураций."""
        project = self.project(project_id)
        found = project.configurations.get(configuration_id)
        if found is None:
            known = ", ".join(project.configurations) or "нет"
            raise ProjectConfigError(
                f"У проекта «{project_id}» нет конфигурации «{configuration_id}» (есть: {known})"
            )
        return found

    def base(self, project_id: str, base_id: str) -> Base:
        """База проекта или ошибка со списком его баз."""
        project = self.project(project_id)
        found = project.bases.get(base_id)
        if found is None:
            known = ", ".join(project.bases) or "нет"
            raise ProjectConfigError(
                f"У проекта «{project_id}» нет базы «{base_id}» (есть: {known})"
            )
        return found


@dataclass(frozen=True, slots=True)
class LocalSettings:
    """Личные настройки машины."""

    project_dirs: dict[str, Path] = field(default_factory=dict)
    server_url: str = DEFAULT_SERVER_URL
    workspace: Path | None = None
    # Платформа 1С (`1cv8.exe`) для проверки через базу КД; не задана — последняя установленная.
    onec_platform: Path | None = None
    # Пользователь 1С баз с авторизацией: «<проект>.<база>» → (имя, пароль). Только в личном файле.
    logins: dict[str, tuple[str, str]] = field(default_factory=dict)

    def login(self, project_id: str, base_id: str) -> tuple[str, str] | None:
        """Пользователь и пароль базы, если заданы."""
        return self.logins.get(f"{project_id}.{base_id}")


def with_login(connection: str, login: tuple[str, str] | None) -> str:
    """Строка соединения с пользователем 1С (без пользователя — как есть)."""
    if login is None:
        return connection
    user, password = login
    return f'{connection.rstrip(";")};Usr="{user}";Pwd="{password}";'


def base_login(local: LocalSettings, project_id: str, base: Base) -> tuple[str, str] | None:
    """Логин базы: `logins` личного файла, иначе `IB_USER` / `IB_PASSWORD` из .dev.env проекта."""
    login = local.login(project_id, base.id)
    folder = local.project_dirs.get(project_id)
    if login is None and folder is not None and base.dev_env:
        login = dev_env_login(resolve(folder, base.dev_env))
    return login


def project_rules_dirs(catalog: Catalog, project_dirs: dict[str, Path]) -> dict[str, Path]:
    """Папки живых правил проектов с `rules_dir`, чьи папки заданы: проект → путь."""
    return {
        project_id: resolve(folder, catalog.projects[project_id].rules_dir)
        for project_id, folder in project_dirs.items()
        if project_id in catalog.projects and catalog.projects[project_id].rules_dir
    }


def basic_auth(login: tuple[str, str]) -> dict[str, str]:
    """Заголовок Basic-авторизации пользователем 1С (HTTP-сервисы ИБ с пользователями)."""
    token = base64.b64encode(f"{login[0]}:{login[1]}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


def project_mcp_servers(
    catalog: Catalog, local: LocalSettings, project_id: str
) -> dict[str, Any] | None:
    """`mcpServers` из .mcp.json проекта; нет папки, `mcp_config` или файла — None."""
    project = catalog.project(project_id)
    folder = local.project_dirs.get(project_id)
    if folder is None or not project.mcp_config:
        return None
    path = resolve(folder, project.mcp_config)
    if not path.is_file():
        return None
    servers = json.loads(path.read_text(encoding="utf-8")).get("mcpServers", {})
    return servers if isinstance(servers, dict) else {}


def data_endpoint(
    catalog: Catalog, local: LocalSettings, project_id: str, base_id: str
) -> tuple[str, dict[str, str]]:
    """Адрес сервера данных базы-песочницы (`data_mcp`) и заголовки авторизации."""
    base = catalog.base(project_id, base_id)
    if not base.is_sandbox:
        raise ProjectConfigError(
            f"База {project_id}.{base_id} — «{base.role}»: скрипты работают только с песочницами"
        )
    if not base.data_mcp:
        raise ProjectConfigError(f"У базы {project_id}.{base_id} не задан data_mcp")
    servers = project_mcp_servers(catalog, local, project_id) or {}
    entry = servers.get(base.data_mcp)
    url = entry.get("url") if isinstance(entry, dict) else None
    if not url:
        raise ProjectConfigError(
            f"Сервера {base.data_mcp} нет в .mcp.json проекта {project_id} "
            "(или папка проекта не задана)"
        )
    login = base_login(local, project_id, base)
    return str(url), basic_auth(login) if login is not None else {}


def dev_env_login(path: Path) -> tuple[str, str] | None:
    """`IB_USER` / `IB_PASSWORD` из .dev.env проекта; пустой пользователь или нет файла — None."""
    if not path.is_file():
        return None
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        key, sep, value = line.strip().partition("=")
        if sep and key in ("IB_USER", "IB_PASSWORD"):
            values[key] = value.strip().strip('"').strip("'")
    user = values.get("IB_USER", "")
    return (user, values.get("IB_PASSWORD", "")) if user else None


def load_catalog(path: Path) -> Catalog:
    """Разбирает общий `projects.yaml`."""
    data = _read_yaml(path)
    projects = {
        str(project_id): _project(str(project_id), body)
        for project_id, body in _mapping(data.get("projects"), "projects").items()
    }
    exchanges = tuple(
        Exchange(str(item.get("plan", "")), tuple(str(p) for p in item.get("projects", ())))
        for item in data.get("exchanges", ()) or ()
    )
    for exchange in exchanges:
        for project_id in exchange.projects:
            if project_id not in projects:
                raise ProjectConfigError(
                    f"Обмен «{exchange.plan}» ссылается на неизвестный проект «{project_id}»"
                )
    shared = _mapping(data.get("shared_mcp") or {}, "shared_mcp")
    return Catalog(
        projects=projects,
        exchanges=exchanges,
        shared_mcp_from=str(shared.get("from", "")),
        shared_mcp=tuple(str(name) for name in shared.get("names", ()) or ()),
    )


def load_local(path: Path) -> LocalSettings:
    """Разбирает личный `projects.local.yaml`."""
    data = _read_yaml(path)
    dirs = {
        str(project_id): Path(str(folder))
        for project_id, folder in _mapping(data.get("projects") or {}, "projects").items()
    }
    workspace = data.get("workspace")
    platform = data.get("onec_platform")
    logins: dict[str, tuple[str, str]] = {}
    for key, item in _mapping(data.get("logins") or {}, "logins").items():
        body = _mapping(item, f"logins.{key}")
        user = str(body.get("user") or "").strip()
        if "." not in str(key) or not user:
            raise ProjectConfigError(f"logins.{key}: ключ «<проект>.<база>» и непустой «user»")
        logins[str(key)] = (user, str(body.get("password") or ""))
    return LocalSettings(
        project_dirs=dirs,
        server_url=str(data.get("server_url") or DEFAULT_SERVER_URL),
        workspace=Path(str(workspace)) if workspace else None,
        onec_platform=Path(str(platform)) if platform else None,
        logins=logins,
    )


def parse_project_dirs(text: str, variable: str = "KD2_PROJECT_DIRS") -> dict[str, Path]:
    """`id=путь;…` из `KD2_PROJECT_DIRS` (или `KD2_RULES_DIRS`)."""
    dirs: dict[str, Path] = {}
    for chunk in text.split(";"):
        if not chunk.strip():
            continue
        project_id, sep, folder = chunk.partition("=")
        if not sep or not project_id.strip() or not folder.strip():
            raise ProjectConfigError(f"Неверный элемент {variable}: «{chunk}»")
        dirs[project_id.strip()] = Path(folder.strip())
    return dirs


def resolve(folder: Path, relative: str) -> Path:
    """Путь внутри папки проекта (разделители `/` и `\\` равноправны)."""
    return folder.joinpath(*[part for part in relative.replace("\\", "/").split("/") if part])


def _project(project_id: str, body: Any) -> Project:
    data = _mapping(body, f"projects.{project_id}")
    configurations: dict[str, Configuration] = {}
    for config_id, config in _mapping(
        data.get("configurations"), f"{project_id}.configurations"
    ).items():
        item = _mapping(config, f"{project_id}.configurations.{config_id}")
        dump = str(item.get("dump", "")).strip()
        if not dump:
            raise ProjectConfigError(f"У конфигурации {project_id}.{config_id} нет «dump»")
        extensions = tuple(str(ext) for ext in item.get("extensions", ()) or ())
        configurations[str(config_id)] = Configuration(str(config_id), dump, extensions)
    if not configurations:
        raise ProjectConfigError(f"У проекта «{project_id}» нет конфигураций")
    bases: dict[str, Base] = {}
    for base_id, base in _mapping(data.get("bases") or {}, f"{project_id}.bases").items():
        item = _mapping(base, f"{project_id}.bases.{base_id}")
        role = str(item.get("role", ""))
        if role not in ROLES:
            raise ProjectConfigError(
                f"Роль базы {project_id}.{base_id} — «{role}», допустимо: {', '.join(ROLES)}"
            )
        config_id = str(item.get("configuration", ""))
        if config_id not in configurations:
            raise ProjectConfigError(
                f"База {project_id}.{base_id} ссылается на неизвестную конфигурацию «{config_id}»"
            )
        connection = str(item.get("connection", "")).strip()
        if not connection:
            raise ProjectConfigError(f"У базы {project_id}.{base_id} нет «connection»")
        data_mcp = str(item.get("data_mcp") or "").strip()
        dev_env = str(item.get("dev_env") or "").strip()
        bases[str(base_id)] = Base(str(base_id), role, config_id, connection, data_mcp, dev_env)
    return Project(
        id=project_id,
        name=str(data.get("name", project_id)),
        configurations=configurations,
        bases=bases,
        code_mcp=tuple(str(name) for name in data.get("code_mcp", ()) or ()),
        mcp_config=str(data.get("mcp_config", "")),
        rules_dir=str(data.get("rules_dir") or "").strip(),
    )


def _read_yaml(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise ProjectConfigError(f"Нет файла {path}")
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as error:
        raise ProjectConfigError(f"{path.name} не разбирается: {error}") from error
    return _mapping(data, path.name)


def _mapping(value: Any, where: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ProjectConfigError(f"«{where}» должен быть словарём")
    return value
