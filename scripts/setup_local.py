"""Настройка машины по projects.local.yaml: Docker и подключение агентов.

Читает общий `projects.yaml` и личный `projects.local.yaml` и пишет (в git не попадают):

- `.env` — публикация compose: `KD2_BIND` (если задан `bind`), `KD2_PUBLISHED_PORT` (если `port`),
  а при `instance` ещё `KD2_CONTAINER`, `KD2_CACHE_VOLUME` и `COMPOSE_PROJECT_NAME`. Нет ни одного
  из этих полей — файла нет (и прежний сгенерированный удаляется): в `docker-compose.yml` остаются
  умолчания;
- `docker-compose.override.yml` — папки проектов подключаются к контейнеру только на чтение как
  `/projects/<проект>`, папки живых правил (`rules_dir`) — на запись как `/rules/<проект>`,
  переменные `KD2_PROJECT_DIRS`, `KD2_RULES_DIRS` и `KD2_PATH_MAP` (перевод путей агента);
  при `token` — `KD2_TOKEN`. Порт сюда не пишется: его задаёт `.env`;
- `.mcp.json` (Claude Code) и `.cursor/mcp.json` (Cursor) — наш сервер, общие серверы 1С и серверы
  поиска по коду каждого проекта с префиксом `<проект>-` и серверы данных песочниц (с
  Basic-авторизацией логином базы, если он есть); адреса берутся из `.mcp.json` проектов.
  При `token` у `kd-rules-mcp` — заголовок `Authorization: Bearer`.

Запуск: `uv run python scripts/setup_local.py`, затем `docker compose up -d`.
Если имена контейнера и проекта compose уже не прежние, а старый контейнер или проект
ещё запущен, скрипт печатает одну строку — чем его остановить — и сам ничего не меняет.
Docker не установлен или не отвечает — молчит.
"""

import json
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import yaml

from kd_rules_mcp.console import utf8_stdout
from kd_rules_mcp.projects import (
    DEFAULT_COMPOSE_PROJECT,
    DEFAULT_CONTAINER_NAME,
    DEFAULT_PUBLISHED_PORT,
    Catalog,
    LocalSettings,
    base_login,
    basic_auth,
    bearer_auth,
    compose_env,
    load_catalog,
    load_local,
    project_mcp_servers,
    project_rules_dirs,
    resolve,
)

ROOT = Path(__file__).resolve().parents[1]

SERVER = "kd-rules-mcp"
# Прежние имена. Строки с LEGACY_ скрипт переименования не меняет: после смены имён
# setup предупреждает, что старый контейнер ещё занимает порт.
LEGACY_CONTAINER = "kd2_rules_mcp"
LEGACY_COMPOSE_PROJECT = "kd2-rules-mcp"
HEADER = "# Сгенерировано scripts/setup_local.py из projects.local.yaml — не править руками.\n"
_DOCKER_TIMEOUT_S = 10


def _compose_bind(bind: str) -> str:
    """Адрес для `KD2_BIND`; IPv6 — в квадратных скобках, иначе compose не разберёт публикацию."""
    return f"[{bind}]" if ":" in bind else bind


def render_env(local: LocalSettings) -> str | None:
    """Текст `.env` или None, если хватает умолчаний `docker-compose.yml`.

    `KD2_BIND` — только при `bind`, `KD2_PUBLISHED_PORT` — только при `port`, имена контейнера,
    тома и проекта — только при `instance`.
    """
    lines: list[str] = []
    if local.bind:
        lines.append(f"KD2_BIND={_compose_bind(local.bind)}")
    if local.port is not None:
        lines.append(f"KD2_PUBLISHED_PORT={local.port}")
    if local.instance:
        names = compose_env(local)
        lines.append(f"KD2_CONTAINER={names['KD2_CONTAINER']}")
        lines.append(f"KD2_CACHE_VOLUME={names['KD2_CACHE_VOLUME']}")
        lines.append(f"COMPOSE_PROJECT_NAME={names['COMPOSE_PROJECT_NAME']}")
    if not lines:
        return None
    return HEADER + "\n".join(lines) + "\n"


def write_env_file(path: Path, local: LocalSettings) -> bool:
    """Пишет `.env` или удаляет свой прежний, чтобы старые порт и имена не остались в силе.

    Чужой `.env` (без `HEADER`, то есть написанный не этим скриптом — например, ручные
    `COMPOSE_PROJECT_NAME`/`COMPOSE_FILE` второго экземпляра) не трогается: иначе compose
    вернулся бы к именам по умолчанию и пересоздал бы контейнер другого клона. Возвращает,
    остался ли на месте чужой файл, когда свой писать нечего.
    """
    text = render_env(local)
    if text is None:
        own = HEADER.rstrip("\n").encode("utf-8")  # концы строк файла могут быть любыми
        if path.is_file() and not path.read_bytes().startswith(own):
            return True
        path.unlink(missing_ok=True)
        return False
    path.write_bytes(text.encode("utf-8"))
    return False


def compose_override(catalog: Catalog, local: LocalSettings) -> str:
    """Подключение папок проектов и перевод путей для контейнера."""
    volumes: list[str] = []
    dirs: list[str] = []
    path_map: list[str] = []
    for project_id, folder in local.project_dirs.items():
        catalog.project(project_id)
        target = f"/projects/{project_id}"
        volumes.append(f"{_posix(folder)}:{target}:ro")
        dirs.append(f"{project_id}={target}")
        path_map.append(f"{folder}={target}")
    rules: list[str] = []
    for project_id, folder in existing_rules_dirs(catalog, local).items():
        # Единственное место проекта на запись; путь агента длиннее папки проекта, поэтому
        # PathMap переводит файлы внутри неё сюда, а не в /projects (там только чтение).
        target = f"/rules/{project_id}"
        volumes.append(f"{_posix(folder)}:{target}")
        rules.append(f"{project_id}={target}")
        path_map.append(f"{folder}={target}")
    workspace = local.workspace or ROOT / "workspace"
    extensions: list[str] = []
    for project_id, folder in local.project_dirs.items():
        for configuration in catalog.project(project_id).configurations.values():
            for index, relative in enumerate(configuration.writable_extensions):
                path = resolve(folder, relative)
                if not path.is_dir():
                    continue
                key = f"{project_id}.{configuration.id}.{index}"
                target = f"/extensions/{key}"
                volumes.append(f"{_posix(path)}:{target}")
                extensions.append(f"{key}={target}")
                path_map.append(f"{path}={target}")
    path_map.append(f"{workspace}=/data/workspace")
    path_map.append(f"{ROOT / 'structures'}=/structures")
    if local.workspace is not None:
        volumes.append(f"{_posix(local.workspace)}:/data/workspace")
    service: dict[str, Any] = {
        "environment": {
            "KD2_PROJECT_DIRS": ";".join(dirs),
            "KD2_RULES_DIRS": ";".join(rules),
            "KD2_PATH_MAP": ";".join(path_map),
            "KD2_EXTENSION_DIRS": ";".join(extensions),
        }
    }
    if local.token:
        service["environment"]["KD2_TOKEN"] = local.token
    if volumes:
        service["volumes"] = volumes
    body = yaml.dump(
        {"services": {SERVER: service}},
        allow_unicode=True,
        sort_keys=False,
        width=1000,
    )
    return HEADER + body


def existing_rules_dirs(catalog: Catalog, local: LocalSettings) -> dict[str, Path]:
    """Папки живых правил, которые есть на диске; нет папки — Docker создал бы её сам (root)."""
    found = project_rules_dirs(catalog, local.project_dirs)
    return {project_id: folder for project_id, folder in found.items() if folder.is_dir()}


def missing_rules_dirs(catalog: Catalog, local: LocalSettings) -> list[str]:
    """Предупреждения о `rules_dir`, которых нет на диске."""
    found = project_rules_dirs(catalog, local.project_dirs)
    return [
        f"{project_id}: нет папки правил {folder} — создайте её и повторите (запись не подключена)"
        for project_id, folder in found.items()
        if not folder.is_dir()
    ]


def mcp_servers(
    catalog: Catalog, local: LocalSettings
) -> tuple[dict[str, dict[str, Any]], list[str]]:
    """Серверы для агентов и предупреждения о тех, что не найдены."""
    ours: dict[str, Any] = {"url": local.server_url}
    if local.token:
        ours["headers"] = bearer_auth(local.token)
    servers: dict[str, dict[str, Any]] = {SERVER: ours}
    warnings: list[str] = []
    if catalog.shared_mcp_from:
        known = _project_servers(catalog, local, catalog.shared_mcp_from, warnings)
        for name in catalog.shared_mcp:
            _add(servers, name, known.get(name), f"{catalog.shared_mcp_from}: {name}", warnings)
    for project_id in local.project_dirs:
        project = catalog.project(project_id)
        known = _project_servers(catalog, local, project_id, warnings)
        for name in project.code_mcp:
            _add(
                servers, f"{project_id}-{name}", known.get(name), f"{project_id}: {name}", warnings
            )
        for base in project.bases.values():
            if not (base.data_mcp and base.is_sandbox):
                continue
            name = f"{project_id}-{base.data_mcp}"
            _add(
                servers, name, known.get(base.data_mcp), f"{project_id}: {base.data_mcp}", warnings
            )
            login = base_login(local, project_id, base)
            if name in servers and login is not None:
                # HTTP-сервис ИБ с пользователями без Basic-авторизации пользователем 1С — 401.
                servers[name]["headers"] = basic_auth(login)
    return servers, warnings


def main() -> None:
    utf8_stdout()
    if not (ROOT / "projects.yaml").is_file():
        # Без файла Docker смонтировал бы на его место пустой каталог.
        raise SystemExit(
            "Нет projects.yaml: скопируйте projects.example.yaml и опишите свои проекты"
        )
    local_file = ROOT / "projects.local.yaml"
    if not local_file.is_file():
        raise SystemExit(
            "Нет projects.local.yaml: скопируйте projects.local.example.yaml "
            "и укажите папки проектов"
        )
    catalog = load_catalog(ROOT / "projects.yaml")
    local = load_local(local_file)
    missing = [pid for pid, folder in local.project_dirs.items() if not folder.is_dir()]
    if missing:
        raise SystemExit(
            f"Нет папок проектов: {', '.join(missing)} — проверьте projects.local.yaml"
        )

    (ROOT / "docker-compose.override.yml").write_bytes(
        compose_override(catalog, local).encode("utf-8")
    )
    foreign_env = write_env_file(ROOT / ".env", local)
    servers, warnings = mcp_servers(catalog, local)
    warnings += missing_rules_dirs(catalog, local)
    if foreign_env:
        warnings.append(
            ".env написан не этим скриптом и оставлен как есть: имена контейнера и тома берутся "
            "из него — сверьте `docker compose config` перед `up`"
        )
    for url_warning in (localhost_server_url_warning(local), server_url_port_warning(local)):
        if url_warning:
            warnings.append(url_warning)
    claude = {"mcpServers": {name: {"type": "http", **entry} for name, entry in servers.items()}}
    cursor = {"mcpServers": servers}
    _write_json(ROOT / ".mcp.json", claude)
    _write_json(ROOT / ".cursor" / "mcp.json", cursor)

    names = compose_env(local)
    host = _compose_bind(local.bind) if local.bind else "127.0.0.1"
    print(
        f"Контейнер {names['KD2_CONTAINER']}, порт {host}:{names['KD2_PUBLISHED_PORT']}, "
        f"том {names['KD2_CACHE_VOLUME']}"
    )
    notice = legacy_runtime_notice()
    if notice:
        print(notice)
    print(f"Проекты: {', '.join(local.project_dirs) or 'нет'}")
    writable = existing_rules_dirs(catalog, local)
    if writable:
        print("Папки правил на запись: " + ", ".join(f"{p} → {f}" for p, f in writable.items()))
    print(f"Серверы агентов: {len(servers)} ({local.server_url} — {SERVER})")
    for warning in warnings:
        print(f"Предупреждение: {warning}")
    print("Дальше: docker compose up -d; в Claude Code одобрить новые серверы (claude mcp list).")


def _project_servers(
    catalog: Catalog, local: LocalSettings, project_id: str, warnings: list[str]
) -> dict[str, dict[str, Any]]:
    servers = project_mcp_servers(catalog, local, project_id)
    if servers is None:
        project = catalog.project(project_id)
        folder = local.project_dirs.get(project_id)
        if folder is None or not project.mcp_config:
            warnings.append(
                f"{project_id}: папка или mcp_config не заданы — серверы проекта пропущены"
            )
        else:
            path = resolve(folder, project.mcp_config)
            warnings.append(f"{project_id}: нет {path} — серверы проекта пропущены")
        return {}
    return servers


def _add(
    servers: dict[str, dict[str, Any]],
    name: str,
    entry: dict[str, Any] | None,
    label: str,
    warnings: list[str],
) -> None:
    url = entry.get("url") if isinstance(entry, dict) else None
    if not url:
        warnings.append(f"{label} — нет в .mcp.json проекта (или это не HTTP-сервер), пропущен")
        return
    servers[name] = {"url": str(url)}


def legacy_runtime_notice(
    runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
) -> str | None:
    """Одна строка, чем остановить прежний контейнер или проект compose.

    Пока текущие имена совпадают с прежними, это ещё не «старый» контейнер — молчим.
    Команды только читают (`docker ps`, `docker compose ls`). Нет docker — None.
    """
    if (
        LEGACY_CONTAINER == DEFAULT_CONTAINER_NAME
        and LEGACY_COMPOSE_PROJECT == DEFAULT_COMPOSE_PROJECT
    ):
        return None
    run = runner or subprocess.run
    try:
        listed = run(
            ["docker", "ps", "--filter", f"name=^{LEGACY_CONTAINER}$", "--format", "{{.Names}}"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=_DOCKER_TIMEOUT_S,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if listed.returncode != 0:
        return None
    names = [line.strip() for line in (listed.stdout or "").splitlines()]
    container = LEGACY_CONTAINER in names
    project = _legacy_compose_project(run)
    if project and container:
        return (
            f"Прежний контейнер {LEGACY_CONTAINER} и проект compose {LEGACY_COMPOSE_PROJECT} "
            "ещё запущены и занимают порт. "
            f"Остановите: docker compose -p {LEGACY_COMPOSE_PROJECT} down"
        )
    if project:
        return (
            f"Прежний проект compose {LEGACY_COMPOSE_PROJECT} ещё запущен и занимает порт. "
            f"Остановите: docker compose -p {LEGACY_COMPOSE_PROJECT} down"
        )
    if container:
        return (
            f"Прежний контейнер {LEGACY_CONTAINER} ещё запущен и занимает порт. "
            f"Остановите: docker stop {LEGACY_CONTAINER}"
        )
    return None


def _legacy_compose_project(run: Callable[..., subprocess.CompletedProcess[str]]) -> bool:
    try:
        listed = run(
            ["docker", "compose", "ls", "--format", "json"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=_DOCKER_TIMEOUT_S,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    if listed.returncode != 0:
        return False
    return _compose_names(listed.stdout or "")


def _compose_names(stdout: str) -> bool:
    text = stdout.strip()
    if not text:
        return False
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        payload = []
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                payload.append(json.loads(line))
            except json.JSONDecodeError:
                return False
    if isinstance(payload, dict):
        payload = [payload]
    if not isinstance(payload, list):
        return False
    return any(
        isinstance(item, dict) and item.get("Name") == LEGACY_COMPOSE_PROJECT for item in payload
    )


def localhost_server_url_warning(local: LocalSettings) -> str | None:
    """`bind` задан, а `server_url` всё ещё петлевой — клиентам с других машин он не подойдёт."""
    if local.bind is None:
        return None
    host = (urlsplit(local.server_url).hostname or "").casefold()
    if host not in {"localhost", "127.0.0.1", "::1"}:
        return None
    return (
        f"bind задан ({local.bind}), а server_url остался {local.server_url} — "
        "для клиентов на других машинах укажите адрес этого интерфейса"
    )


def server_url_port_warning(local: LocalSettings) -> str | None:
    """Порт в `server_url` не совпал с публикуемым — клиент подключится не туда."""
    published = local.port if local.port is not None else DEFAULT_PUBLISHED_PORT
    url_port = urlsplit(local.server_url).port
    if url_port == published:
        return None
    shown = "не указан" if url_port is None else str(url_port)
    return (
        f"порт публикации {published}, а в server_url — {shown} ({local.server_url}): "
        "укажите тот же порт"
    )


def _posix(path: Path) -> str:
    return str(path).replace("\\", "/")


def _write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    path.write_bytes(text.encode("utf-8"))


if __name__ == "__main__":
    main()
