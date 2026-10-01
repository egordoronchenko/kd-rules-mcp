"""Настройка машины по projects.local.yaml: Docker и подключение агентов.

Читает общий `projects.yaml` и личный `projects.local.yaml` и пишет (все три — в git не попадают):

- `docker-compose.override.yml` — папки проектов подключаются к контейнеру только на чтение как
  `/projects/<проект>`, папки живых правил (`rules_dir`) — на запись как `/rules/<проект>`,
  переменные `KD2_PROJECT_DIRS`, `KD2_RULES_DIRS` и `KD2_PATH_MAP` (перевод путей агента);
- `.mcp.json` (Claude Code) и `.cursor/mcp.json` (Cursor) — наш сервер, общие серверы 1С и серверы
  поиска по коду каждого проекта с префиксом `<проект>-` и серверы данных песочниц (с
  Basic-авторизацией логином базы, если он есть); адреса берутся из `.mcp.json` проектов.

Запуск: `uv run python scripts/setup_local.py`, затем `docker compose up -d`.
"""

import json
from pathlib import Path
from typing import Any

import yaml

from kd2_rules_mcp.console import utf8_stdout
from kd2_rules_mcp.projects import (
    Catalog,
    LocalSettings,
    base_login,
    basic_auth,
    load_catalog,
    load_local,
    project_mcp_servers,
    project_rules_dirs,
    resolve,
)

ROOT = Path(__file__).resolve().parents[1]

SERVER = "kd2-rules-mcp"
HEADER = "# Сгенерировано scripts/setup_local.py из projects.local.yaml — не править руками.\n"


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
    path_map.append(f"{workspace}=/data/workspace")
    path_map.append(f"{ROOT / 'structures'}=/structures")
    if local.workspace is not None:
        volumes.append(f"{_posix(local.workspace)}:/data/workspace")
    service: dict[str, Any] = {
        "environment": {
            "KD2_PROJECT_DIRS": ";".join(dirs),
            "KD2_RULES_DIRS": ";".join(rules),
            "KD2_PATH_MAP": ";".join(path_map),
        }
    }
    if volumes:
        service["volumes"] = volumes
    body = yaml.safe_dump(
        {"services": {SERVER: service}}, allow_unicode=True, sort_keys=False, width=1000
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
    servers: dict[str, dict[str, Any]] = {SERVER: {"url": local.server_url}}
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
    servers, warnings = mcp_servers(catalog, local)
    warnings += missing_rules_dirs(catalog, local)
    claude = {"mcpServers": {name: {"type": "http", **entry} for name, entry in servers.items()}}
    cursor = {"mcpServers": servers}
    _write_json(ROOT / ".mcp.json", claude)
    _write_json(ROOT / ".cursor" / "mcp.json", cursor)

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


def _posix(path: Path) -> str:
    return str(path).replace("\\", "/")


def _write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    path.write_bytes(text.encode("utf-8"))


if __name__ == "__main__":
    main()
