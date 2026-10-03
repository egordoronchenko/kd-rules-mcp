"""Проверка установки: сервер отвечает, видит проекты из projects.yaml.

Подключается к серверу как агент (адрес — `server_url` из projects.local.yaml или первый аргумент),
печатает число инструментов и по каждому проекту — видна ли серверу его папка.
Запуск: `uv run python scripts/check_server.py [адрес]`; код выхода 0 — всё в порядке.
"""

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

from mcp import Client
from mcp.types import ListToolsResult

from kd2_rules_mcp.console import utf8_stdout
from kd2_rules_mcp.projects import LocalSettings, load_local

ROOT = Path(__file__).resolve().parents[1]


def server_url(argv: list[str] | None = None) -> str:
    """Адрес из аргумента, иначе из projects.local.yaml, иначе адрес по умолчанию."""
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("url", nargs="?", help="адрес сервера")
    args = parser.parse_args(argv)
    if args.url is not None:
        return args.url
    local = ROOT / "projects.local.yaml"
    return (load_local(local) if local.is_file() else LocalSettings()).server_url


async def check(url: str) -> int:
    async with Client(url) as client:
        tools = await client.list_tools()
        items = tools.tools if isinstance(tools, ListToolsResult) else tools
        print(f"Сервер {url}: {len(items)} инструментов")
        result = await client.call_tool("project_list", {})
        data = _payload(result)
    return print_projects(data)


def print_projects(data: Any) -> int:
    """Печатает проекты из ответа `project_list`; код 1 — ни одна папка не видна."""
    projects = data.get("projects", []) if isinstance(data, dict) else []
    if not projects:
        print("  проектов нет — опишите их в projects.yaml и перезапустите контейнер")
        return 1
    hidden = 0
    for project in projects:
        configs = ", ".join(project.get("configurations", {})) or "нет"
        if project.get("available"):
            print(f"  {project['project']}: папка видна, конфигурации: {configs}")
        else:
            hidden += 1
            print(
                f"  {project['project']}: папка НЕ видна — проверьте projects.local.yaml, "
                "setup_local.py, docker compose up -d"
            )
        _print_bases(project.get("bases"))
        rules = project.get("rules_dir")
        if rules:
            state = "запись есть" if rules.get("writable") else "НЕ подключена на запись"
            print(f"    папка правил {rules.get('path')}: {state}")
    return 1 if hidden == len(projects) else 0


def _print_bases(bases: Any) -> None:
    """Строка на базу: логин есть или нет, сервер данных — имя или «нет»."""
    if not isinstance(bases, dict):
        return
    for base_id, base in bases.items():
        if not isinstance(base, dict):
            continue
        login = "есть" if base.get("login") else "нет"
        server = base.get("data_mcp_server") or base.get("data_mcp") or "нет"
        role = base.get("role") or ""
        print(f"    база {base_id} ({role}): логин {login}, сервер данных: {server}")


def _payload(result: Any) -> Any:
    """Структурированный ответ инструмента или JSON из текстового."""
    if getattr(result, "structured_content", None):
        return result.structured_content
    text = "".join(getattr(item, "text", "") for item in getattr(result, "content", []))
    return json.loads(text) if text.strip().startswith("{") else {}


def main() -> None:
    utf8_stdout()
    url = server_url()
    try:
        code = asyncio.run(check(url))
    except Exception as error:  # сеть, контейнер не запущен, неверный адрес
        print(f"Сервер {url} недоступен: {error}")
        print("Проверьте: docker compose ps; docker compose logs kd2-rules-mcp --tail 30")
        code = 2
    sys.exit(code)


if __name__ == "__main__":
    main()
