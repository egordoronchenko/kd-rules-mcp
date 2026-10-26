"""Упаковки знаний агента: справочники внутри скилла и установка скиллов в папку проекта 1С.

Источник правды — скиллы сервера в `.claude/skills` (`our_skills.is_our_skill`: имена `kd-`,
`kd2-` или `kd3-`) и документы `docs/`. Копий для отдельных
клиентов в репозитории нет: `.claude/skills` читают Claude Code, Cursor и OpenCode.

1. `--write` / `--check` — кладёт в `.claude/skills/kd2-rules-build/references/` копии
   `docs/rules/mcp-1c.md`, `docs/checks.md`, `docs/tools.md`, `docs/glossary.md` под теми же
   именами, чтобы скилл был самодостаточен в чужом проекте. Копия — шапка «откуда она» и текст
   источника; относительные ссылки в нём ведут на соседние копии или в открытый репозиторий.
   `--check` — копии совпадают со сборкой (иначе код выхода 1).
2. `--dest <папка проекта> --client claude|agents` — ставит скиллы сервера в проект:
   `claude` — `.claude/skills/` (Claude Code, Cursor, OpenCode), `agents` — `.agents/skills/` и
   `KD-RULES.md` в корень (Codex и клиенты, которые `.claude/skills` не читают). Обе сразу
   нельзя: Cursor и OpenCode увидели бы каждый скилл дважды. В `.mcp.json` проекта добавляется
   сервер `kd-rules-mcp`, другие серверы не трогаются. `--cursor` создаёт `.cursor/mcp.json`,
   если файла нет, и дописывает туда недостающие HTTP-серверы из `.mcp.json` (существующие
   записи не меняет; stdio с `command` не переносит). Без флага существующий файл серверами 1С
   не пополняется: скрипт называет, каких нет. Без флага и без файла скрипт сообщает, что сервер
   нужно добавить в настройках MCP Cursor. Адрес — `--server-url`, по умолчанию
   `server_url` из `projects.local.yaml` этого репозитория. Заголовок `Authorization` — из
   `token` того же файла, если токен задан. Это правка чужого репозитория — только с согласия
   человека.
3. `--dest <папка обмена> --side <имя>=<папка> --side <имя>=<папка>` — папка обмена двух
   проектов; `--exchange <id>` вместо `--side` берёт стороны и направления из профиля
   `projects.yaml`, папки — из `projects.local.yaml`. При совпадении папок `--side` с одним
   профилем направления и файлы тоже берутся из него. Сторон ровно две; имя — строчные
   латинские буквы, цифры и дефис, с буквы; папки
   существуют. Папка обмена не совпадает со стороной, не лежит внутри неё и не содержит её
   (по разрешённым путям). Иначе отказ до любой записи, папка обмена не создаётся. Проекты
   сторон только читаются. Скиллы и `kd-rules-mcp` — как при обычном `--dest`. HTTP-серверы
   сторон (`url`, и `type`/`headers`, если они есть) переносятся в `.mcp.json`: `kd-rules-mcp`
   стороны пропускается; одинаковые имя и запись — общий сервер без префикса; остальные — под
   `<сторона>-<имя>`, префикс не удваивается. Конфликт итоговых имён — отказ с обоими
   источниками. Записи с `command` (stdio) не переносятся и называются в отчёте. Нет
   `.mcp.json` — установка продолжается, сторона без серверов названа в отчёте. Есть `headers`
   — предупреждение, что файл содержит учётные данные и коммитить его нельзя. Повтор —
   слияние без удаления: та же запись не трогается, другая заменяется («обновлён»), чужие
   имена остаются; исчезнувший сервер с префиксом стороны не удаляется и называется в отчёте.
   Неизменившийся `.mcp.json` не переписывается. `--cursor` переносит HTTP из уже собранного
   `.mcp.json` в `.cursor/mcp.json`. Пишется `KD-EXCHANGE.md`. `AGENTS.md` и `CLAUDE.md`
   создаются, только если их нет.

Запуск: `uv run python scripts/build_packs.py --check` (или `--write`);
`uv run python scripts/build_packs.py --dest <папка проекта> --client claude`
`[--cursor] [--server-url URL]`;
`uv run python scripts/build_packs.py --dest <папка обмена> --side <имя>=<проект>`
`--side <имя>=<проект> [--cursor] [--client claude|agents] [--server-url URL]`.
"""

import argparse
import json
import os
import re
import shutil
from collections.abc import Sequence
from pathlib import Path, PurePosixPath
from typing import Any, NamedTuple

import our_skills

from kd_rules_mcp.console import utf8_stdout
from kd_rules_mcp.projects import (
    DEFAULT_SERVER_URL,
    Exchange,
    ProjectConfigError,
    bearer_auth,
    load_catalog,
    load_local,
    resolve,
)

ROOT = Path(__file__).resolve().parents[1]
SKILLS = ROOT / ".claude" / "skills"
REFERENCES = SKILLS / "kd2-rules-build" / "references"
RULES_ENTRY = ROOT / "docs" / "rules" / "KD-RULES.md"
REPO_URL = "https://github.com/egordoronchenko/kd-rules-mcp"
SERVER = "kd-rules-mcp"
# Прежние имена поставки. Строки с LEGACY_ скрипт переименования не меняет.
LEGACY_SKILL_NAMES = ("kd2-ed-rules", "kd2-install")
LEGACY_RULES_FILE = "KD2-RULES.md"
LEGACY_SERVER = "kd2-rules-mcp"
LEGACY_RULES_MARK = "# Правила обмена КД 2 и сервер kd2-rules-mcp"
# Записи нашего сервера у сторон папки обмена: у папки своя, переносить их нельзя.
OWN_SERVERS = {SERVER, LEGACY_SERVER}

# Документ репозитория → имя копии в references/ скилла kd2-rules-build.
COPIES: dict[str, str] = {
    "docs/rules/mcp-1c.md": "mcp-1c.md",
    "docs/checks.md": "checks.md",
    "docs/tools.md": "tools.md",
    "docs/glossary.md": "glossary.md",
}
CLIENT_ROOTS = {"claude": ".claude/skills", "agents": ".agents/skills"}
# Имя стороны папки обмена: префикс сервера, как `<проект>-` в setup_local.mcp_servers.
SIDE_NAME = re.compile(r"^[a-z][a-z0-9-]*$")
# Общий сервер в таблице KD-EXCHANGE.md и в принадлежности сборки.
SHARED_OWNER = "обе"
EXCHANGE_HINT = "Папка обмена правилами kd-rules-mcp — прочитай KD-EXCHANGE.md перед работой."
# Строка `@AGENTS.md` в CLAUDE.md — импорт файла, а не упоминание в тексте.
AGENTS_IMPORT = re.compile(r"(?m)^\s*@AGENTS\.md\s*$")
RULES_HINT = (
    "Правила обмена КД 2 и сервер kd-rules-mcp — прочитай KD-RULES.md перед работой с ними."
)
LINK = re.compile(r"\]\(([^)\s]+)\)")
SKIP_PARTS = {"__pycache__"}
# Тексты, которые работают в папке проекта 1С без этого репозитория: его путей в них быть не должно.
# kd-install не входит — он ведёт установку из клона сервера. Копии справочников сервера
# (checks, tools, glossary) описывают сам сервер и оговаривают в шапке, что пути — от клона.
PORTABLE_SKILLS = {"kd2-rules-build", "kd2-exchange-pitfalls", "kd3-rules"}
SERVER_DOCS = {"checks.md", "tools.md", "glossary.md"}
REPO_PATHS = {
    "setup_local.py": re.compile(r"setup_local"),
    # Каталог репозитория в начале пути; `<папка проекта>\…\docs\` — уже путь проекта, не наш.
    "scripts/": re.compile(r"(?<![\w.\\/-])scripts[/\\]"),
    "workspace/": re.compile(r"(?<![\w.\\/-])workspace[/\\]"),
    "docs/": re.compile(r"(?<![\w.\\/-])docs[/\\]"),
    "src/kd_rules_mcp": re.compile(r"src[/\\]kd_rules_mcp"),
    "mcp-1c.mdc": re.compile(r"mcp-1c\.mdc"),
}
URL = re.compile(r"https?://\S+")
HEADER = (
    "> Копия справочника из репозитория сервера kd-rules-mcp\n"
    "> ({url}), собирается вместе со скиллом — правится источник.\n"
    "> Пути вида `kdbase\\…`, `projects.yaml`, `src/…` в тексте — от папки клона сервера.\n\n"
)


def render_copy(source: str) -> str:
    """Текст копии документа `source` (путь от корня репозитория) для references/ скилла."""
    path = ROOT / source
    text = path.read_bytes().decode("utf-8")
    body = LINK.sub(lambda match: f"]({_relink(path, match.group(1))})", text)
    return HEADER.format(url=f"{REPO_URL}/blob/main/{source}") + body


def rendered_copies() -> dict[Path, str]:
    """Все копии: путь в references/ → текст."""
    return {REFERENCES / name: render_copy(source) for source, name in COPIES.items()}


def stale_copies() -> list[str]:
    """Копии, которые отличаются от сборки или отсутствуют (пути от корня репозитория)."""
    return [
        path.relative_to(ROOT).as_posix()
        for path, text in rendered_copies().items()
        if not path.is_file() or path.read_bytes() != text.encode("utf-8")
    ]


def write_copies() -> list[str]:
    """Пишет устаревшие копии; возвращает, что переписано."""
    changed = stale_copies()
    for path, text in rendered_copies().items():
        if path.relative_to(ROOT).as_posix() in changed:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(text.encode("utf-8"))
    return changed


def pack_files(client: str) -> dict[str, bytes]:
    """Файлы упаковки: путь от папки проекта (через `/`) → содержимое."""
    if client not in CLIENT_ROOTS:
        raise SystemExit(f"Неизвестный клиент «{client}»: {', '.join(CLIENT_ROOTS)}")
    root = CLIENT_ROOTS[client]
    generated = {path: text.encode("utf-8") for path, text in rendered_copies().items()}
    files: dict[str, bytes] = {}
    for skill in our_skills.our_skills(SKILLS):
        for path in sorted(skill.rglob("*")):
            if path.is_file() and not SKIP_PARTS & set(path.parts):
                files[f"{root}/{path.relative_to(SKILLS).as_posix()}"] = path.read_bytes()
    # Копии справочников — всегда свежая сборка, даже если в репозитории не сделан --write.
    for path, content in generated.items():
        files[f"{root}/{path.relative_to(SKILLS).as_posix()}"] = content
    if client == "agents":
        if not RULES_ENTRY.is_file():
            raise SystemExit("Нет docs/rules/KD-RULES.md — упаковку agents не собрать")
        files["KD-RULES.md"] = RULES_ENTRY.read_bytes()
    return dict(sorted(files.items()))


def portability_hits(files: dict[str, bytes]) -> list[str]:
    """Пути этого репозитория в переносимых текстах упаковки: `файл:строка: что найдено`."""
    hits: list[str] = []
    for name, content in files.items():
        if not _is_portable(name):
            continue
        text = URL.sub("", content.decode("utf-8"))
        for number, line in enumerate(text.splitlines(), 1):
            hits.extend(
                f"{name}:{number}: {label}"
                for label, pattern in REPO_PATHS.items()
                if pattern.search(line)
            )
        if "kdbase" in text and "клон" not in text:
            hits.append(f"{name}: скрипты kdbase без оговорки «из клона сервера»")
    return hits


def install(
    dest: Path, client: str, server_url: str | None = None, *, cursor: bool = False
) -> list[str]:
    """Ставит упаковку в папку проекта; возвращает строки отчёта.

    `cursor` — дописать в `.cursor/mcp.json` HTTP-серверы из `.mcp.json`, которых там ещё нет,
    и создать файл, если его нет. Существующие записи не меняются. Без флага файл не пополняется
    этими серверами: если каких-то нет, их имена попадают в отчёт. Заголовок `Authorization`
    берётся из `token` в `projects.local.yaml`, если токен задан.
    """
    dest = dest.resolve()
    if not dest.is_dir():
        raise SystemExit(f"Нет папки проекта {dest}")
    if dest == ROOT:
        raise SystemExit("Это репозиторий сервера: скиллы здесь — источник, ставить их не нужно")
    files = pack_files(client)
    for other, other_root in CLIENT_ROOTS.items():
        if other != client and our_skills.our_skills(dest / other_root):
            raise SystemExit(
                f"В проекте уже стоит упаковка {other} ({other_root}): Cursor и OpenCode "
                "видели бы каждый скилл дважды — удалите её или ставьте ту же"
            )
    # Конфигурации MCP читаются до записи: битый файл останавливает установку, ничего не меняя.
    configs = [(dest / ".mcp.json", True)]
    cursor_config = dest / ".cursor" / "mcp.json"
    # Без --cursor файл не создаём: у Cursor серверы часто лежат в глобальных настройках.
    if cursor or cursor_config.is_file():
        configs.append((cursor_config, False))
    loaded = [(path, read_mcp_config(path), typed) for path, typed in configs]
    report = remove_obsolete_pack(dest, CLIENT_ROOTS[client], set(files))
    report.extend(_remove_stale(dest, CLIENT_ROOTS[client], {dest / name for name in files}))
    written = 0
    for name, content in files.items():
        path = dest / name
        if path.is_file() and path.read_bytes() == content:
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        written += 1
    report.append(f"упаковка {client}: файлов {len(files)}, записано {written}")
    # Перенос дописывает серверы в тот же словарь, что потом пишет add_server. Если запись
    # kd-rules-mcp уже совпадает, add_server файл не переписывает — тогда пишем сами.
    added_servers: list[str] = []
    cursor_target: dict[str, Any] | None = None
    if cursor:
        source = _loaded(loaded, dest / ".mcp.json")
        cursor_target = _loaded(loaded, cursor_config)
        added_servers, skipped = transfer_http_servers(source, cursor_target)
        if added_servers:
            report.append(
                "перенесено серверов из .mcp.json: "
                f"{len(added_servers)} (новых: {', '.join(added_servers)})"
            )
        else:
            report.append("серверы 1С в `.cursor/mcp.json` уже есть")
        if skipped:
            report.append("не перенесены: " + ", ".join(skipped))
    url = server_url or default_server_url()
    headers = default_server_headers()
    report.extend(
        add_server(path, data, url, typed=typed, headers=headers) for path, data, typed in loaded
    )
    if added_servers and cursor_target is not None:
        _write_mcp_config(cursor_config, cursor_target)
    if headers is not None:
        report.append("заголовок Authorization добавлен")
    if not cursor and not cursor_config.is_file():
        report.append(
            "Cursor: `.cursor/mcp.json` в проекте нет — добавьте сервер "
            "в настройках MCP Cursor или запустите с `--cursor`"
        )
    elif not cursor and cursor_config.is_file():
        missing = _missing_http_servers(
            _loaded(loaded, dest / ".mcp.json"), _loaded(loaded, cursor_config)
        )
        if missing:
            report.append(
                "Cursor: в `.cursor/mcp.json` нет серверов 1С: "
                + ", ".join(missing)
                + " — запустите с `--cursor` или добавьте их в настройках MCP Cursor"
            )
    if client == "agents":
        report.append(
            "добавьте в AGENTS.md проекта строку «Правила обмена КД 2 и сервер kd-rules-mcp — "
            "прочитай KD-RULES.md перед работой с ними»"
        )
    return report


def read_mcp_config(path: Path) -> dict[str, Any]:
    """Содержимое `.mcp.json` (пустое, если файла нет); не тот формат — отказ без правки."""
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_bytes().decode("utf-8-sig") or "{}")
    except json.JSONDecodeError as error:
        raise SystemExit(f"{path}: не JSON ({error}) — файл не тронут") from error
    if not isinstance(data, dict) or not isinstance(data.get("mcpServers", {}), dict):
        raise SystemExit(f"{path}: ожидался объект JSON с объектом mcpServers — файл не тронут")
    return data


def _loaded(loaded: list[tuple[Path, dict[str, Any], bool]], path: Path) -> dict[str, Any]:
    """Словарь конфига MCP, уже прочитанный в `install`."""
    return next(data for item_path, data, _typed in loaded if item_path == path)


def _mcp_payload(data: dict[str, Any]) -> bytes:
    """Байты конфига MCP: UTF-8, LF, тот же вид, что пишет `_write_mcp_config`."""
    return (json.dumps(data, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def _write_mcp_config(path: Path, data: dict[str, Any]) -> None:
    """Записывает конфиг MCP целиком (UTF-8, LF)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(_mcp_payload(data))


def _http_server_copy(entry: Any) -> dict[str, Any] | None:
    """Копия HTTP-записи для Cursor: `url` и `headers`. Stdio и пустой `url` — None."""
    if not isinstance(entry, dict) or "command" in entry:
        return None
    url = entry.get("url")
    if not isinstance(url, str) or not url:
        return None
    copied: dict[str, Any] = {"url": url}
    headers = entry.get("headers")
    if isinstance(headers, dict):
        copied["headers"] = headers
    return copied


def _http_server_names(data: dict[str, Any]) -> list[str]:
    """Имена HTTP-серверов в конфиге (с `url`, без `command`), в порядке файла."""
    servers = data.get("mcpServers") or {}
    if not isinstance(servers, dict):
        return []
    return [str(name) for name, entry in servers.items() if _http_server_copy(entry) is not None]


def _missing_http_servers(source: dict[str, Any], target: dict[str, Any]) -> list[str]:
    """HTTP-серверы `.mcp.json`, которых нет в конфиге Cursor, кроме `kd-rules-mcp`."""
    present = target.get("mcpServers") or {}
    if not isinstance(present, dict):
        present = {}
    return [name for name in _http_server_names(source) if name != SERVER and name not in present]


def transfer_http_servers(
    source: dict[str, Any], target: dict[str, Any]
) -> tuple[list[str], list[str]]:
    """Дописывает в конфиг Cursor HTTP-серверы из `.mcp.json`, которых там ещё нет.

    Существующие имена не меняются. Запись — `url` и `headers`, если они есть; поле `type`
    не пишется. Серверы с `command` (stdio) не копируются: их имена — второй список.
    Первый список — имена, которые добавлены.
    """
    servers = source.get("mcpServers") or {}
    if not isinstance(servers, dict):
        return [], []
    dest_servers = target.setdefault("mcpServers", {})
    added: list[str] = []
    skipped: list[str] = []
    for name, entry in servers.items():
        if isinstance(entry, dict) and "command" in entry:
            skipped.append(str(name))
            continue
        copied = _http_server_copy(entry)
        if copied is None:
            continue
        key = str(name)
        if key in dest_servers:
            continue
        dest_servers[key] = copied
        added.append(key)
    return added, skipped


def add_server(
    path: Path,
    data: dict[str, Any],
    url: str,
    *,
    typed: bool,
    headers: dict[str, str] | None = None,
) -> str:
    """Добавляет или обновляет `kd-rules-mcp` в `mcpServers`, не трогая остальные серверы.

    Сравнение «уже подключён» — по всей записи, включая `headers`.
    """
    shown = f".cursor/{path.name}" if path.parent.name == ".cursor" else path.name
    servers = data.setdefault("mcpServers", {})
    _take_legacy_server(servers)
    entry: dict[str, Any] = {"type": "http", "url": url} if typed else {"url": url}
    if headers is not None:
        entry["headers"] = headers
    if servers.get(SERVER) == entry:
        return f"{shown}: {SERVER} уже подключён ({url})"
    action = "обновлён" if SERVER in servers else "добавлен"
    servers[SERVER] = entry
    _write_mcp_config(path, data)
    return f"{shown}: {SERVER} {action} ({url})"


def default_server_url() -> str:
    """`server_url` из `projects.local.yaml` этого репозитория, иначе адрес по умолчанию."""
    local = ROOT / "projects.local.yaml"
    return load_local(local).server_url if local.is_file() else DEFAULT_SERVER_URL


def default_server_headers() -> dict[str, str] | None:
    """Заголовок `Authorization: Bearer` из `token` в `projects.local.yaml`; нет токена — `None`."""
    local = ROOT / "projects.local.yaml"
    if not local.is_file():
        return None
    token = load_local(local).token
    return bearer_auth(token) if token else None


def _take_legacy_server(servers: dict[str, Any]) -> None:
    """Снимает прежний ключ, если он отличается от текущего: второй записи не остаётся."""
    if LEGACY_SERVER == SERVER or LEGACY_SERVER not in servers:
        return
    previous = servers.pop(LEGACY_SERVER)
    if SERVER not in servers and isinstance(previous, dict):
        servers[SERVER] = previous


class ExchangeServers(NamedTuple):
    """Сборка HTTP-серверов папки обмена без чтения файлов.

    `entries` — итоговая запись по имени. `owners` — имя стороны или «обе» для общего.
    `stdio` — имена записей с `command` по сторонам. `has_headers` — есть `headers`
    хотя бы у одной перенесённой записи.
    """

    entries: dict[str, dict[str, Any]]
    owners: dict[str, str]
    stdio: dict[str, list[str]]
    has_headers: bool


def _http_fields(entry: Any) -> dict[str, Any] | None:
    """Поля HTTP-записи для переноса: `url` и, если есть, `type` и `headers`.

    Запись с `command` и запись без непустого `url` не переносятся.
    """
    if not isinstance(entry, dict) or "command" in entry:
        return None
    url = entry.get("url")
    if not isinstance(url, str) or not url:
        return None
    copied: dict[str, Any] = {"url": url}
    if "type" in entry:
        copied["type"] = entry["type"]
    if "headers" in entry:
        headers = entry["headers"]
        copied["headers"] = dict(headers) if isinstance(headers, dict) else headers
    return copied


def _stdio_names(servers: dict[str, Any]) -> list[str]:
    """Имена stdio-записей стороны, кроме записей нашего сервера."""
    return [
        str(name)
        for name, entry in servers.items()
        if name not in OWN_SERVERS and isinstance(entry, dict) and "command" in entry
    ]


def exchange_servers(sides: Sequence[tuple[str, dict[str, Any]]]) -> ExchangeServers:
    """Собирает серверы папки обмена из двух объектов `mcpServers`.

    Общий сервер — одно и то же имя и запись-словарь у обеих сторон (сравнение всего
    словаря, не урезанной копии). `kd-rules-mcp` и его прежнее имя не переносятся. Итоговое
    имя `kd-rules-mcp` (сторона `kd` и сервер `rules-mcp`) — конфликт с записью установки.
    """
    if len(sides) != 2:
        raise SystemExit("нужны ровно две стороны")
    left_name, left = sides[0]
    right_name, right = sides[1]
    stdio = {left_name: _stdio_names(left), right_name: _stdio_names(right)}
    shared: list[str] = []
    for name, entry in left.items():
        if name in OWN_SERVERS or name not in right or not isinstance(entry, dict):
            continue
        other = right[name]
        if "command" in entry or not isinstance(other, dict) or "command" in other:
            continue
        if entry == other and _http_fields(entry) is not None:
            shared.append(str(name))
    entries: dict[str, dict[str, Any]] = {}
    owners: dict[str, str] = {}
    sources: dict[str, str] = {}

    def place(final: str, copied: dict[str, Any], owner: str, source: str) -> None:
        if final == SERVER:
            raise SystemExit(f"конфликт имени «{SERVER}»: запись установки и {source}")
        if final in entries:
            raise SystemExit(f"конфликт имени «{final}»: {sources[final]} и {source}")
        entries[final] = copied
        owners[final] = owner
        sources[final] = source

    for name in shared:
        copied = _http_fields(left[name])
        if copied is None:
            continue
        place(name, copied, SHARED_OWNER, f"общий сервер «{name}»")
    for side_name, servers in ((left_name, left), (right_name, right)):
        prefix = f"{side_name}-"
        for name, entry in servers.items():
            if name in OWN_SERVERS or name in shared:
                continue
            copied = _http_fields(entry)
            if copied is None:
                continue
            final = str(name) if str(name).startswith(prefix) else f"{prefix}{name}"
            place(final, copied, side_name, f"сторона {side_name}, сервер «{name}»")
    has_headers = any("headers" in entry for entry in entries.values())
    return ExchangeServers(entries, owners, stdio, has_headers)


def _contains(parent: Path, child: Path) -> bool:
    """`child` совпадает с `parent` или лежит внутри (разрешённые пути, без регистра)."""
    parent_text = os.path.normcase(str(parent))
    child_text = os.path.normcase(str(child))
    if parent_text == child_text:
        return True
    return child_text.startswith(parent_text.rstrip("\\/") + os.sep)


def _validate_exchange_sides(
    dest: Path, sides: Sequence[tuple[str, Path]]
) -> list[tuple[str, Path]]:
    """Проверки сторон и путей до любой записи. Пути сторон — разрешённые."""
    if len(sides) != 2:
        raise SystemExit("нужны ровно две стороны")
    seen: set[str] = set()
    for name, _folder in sides:
        if SIDE_NAME.fullmatch(name) is None:
            raise SystemExit(
                f"недопустимое имя стороны «{name}»: "
                "строчные латинские буквы, цифры и дефис, с буквы"
            )
        if name in seen:
            raise SystemExit(f"одинаковое имя стороны «{name}»")
        seen.add(name)
    dest_resolved = dest.resolve()
    resolved: list[tuple[str, Path]] = []
    for name, folder in sides:
        if not folder.is_dir():
            raise SystemExit(f"нет папки стороны {name}: {folder}")
        folder_resolved = folder.resolve()
        if _contains(folder_resolved, dest_resolved) or _contains(dest_resolved, folder_resolved):
            raise SystemExit("папка обмена должна быть вне папок проектов")
        resolved.append((name, folder_resolved))
    return resolved


def _side_servers(folder: Path) -> tuple[dict[str, Any], bool]:
    """`mcpServers` стороны и признак, что файл `.mcp.json` есть.

    Битый файл — отказ `read_mcp_config` (в тексте имя файла), ничего не записано.
    """
    path = folder / ".mcp.json"
    if not path.is_file():
        return {}, False
    servers = read_mcp_config(path).get("mcpServers") or {}
    return servers, True


def _check_exchange_configs(dest: Path) -> None:
    """Читает конфиги папки обмена до записи: битый JSON останавливает установку."""
    for path in (dest / ".mcp.json", dest / ".cursor" / "mcp.json"):
        if path.is_file():
            read_mcp_config(path)


def _merge_exchange_servers(
    servers: dict[str, Any], built: ExchangeServers, side_names: Sequence[str]
) -> list[str]:
    """Вливает порождённые записи в `mcpServers`, не удаляя чужие. Строки отчёта."""
    lines: list[str] = []
    generated = set(built.entries)
    for name, entry in built.entries.items():
        if servers.get(name) == entry:
            continue
        if name in servers:
            lines.append(f"обновлён: {name}")
        else:
            lines.append(f"добавлен: {name}")
        servers[name] = entry
    for side in side_names:
        prefix = f"{side}-"
        for name in servers:
            if str(name).startswith(prefix) and name not in generated:
                lines.append(f"у стороны {side} больше нет: {name}")
    return lines


def _render_exchange_md(sides: Sequence[tuple[str, Path]], built: ExchangeServers) -> str:
    """Текст `KD-EXCHANGE.md`: стороны, серверы, три правила. UTF-8 дописывает вызывающий."""
    left_name, left_path = sides[0]
    right_name, right_path = sides[1]
    lines = [
        f"# Папка обмена: {left_name} ↔ {right_name}",
        "",
        f"- {left_name}: `{left_path}`",
        f"- {right_name}: `{right_path}`",
        "",
        "| сервер | сторона |",
        "| --- | --- |",
    ]
    lines.extend(f"| {name} | {owner} |" for name, owner in built.owners.items())
    prefixes = " или ".join(f"`{name}-`" for name, _path in sides)
    lines.extend(
        [
            "",
            "## Направления и файлы правил",
            "",
            "Комплект БСП лежит в папке живых правил стороны (`rules_dir` в `project_list`,",
            "внутри — папка плана обмена). Какой файл работает в каком направлении:",
            "",
            "| направление | выгрузка | загрузка | регистрация |",
            "| --- | --- | --- | --- |",
        ]
    )
    lines.extend(
        f"| {source} → {target} | `ExchangeRules.xml` {source} | "
        f"`CorrespondentExchangeRules.xml` {target} | `RegistrationRules.xml` {source} |"
        for source, target in ((left_name, right_name), (right_name, left_name))
    )
    lines.extend(
        [
            "",
            "Загружает приёмник по копии правил корреспондента из своего комплекта, а не по файлу",
            "источника. Совпадение копий проверять `rules_diff`, а не хешем файла: концы строк",
            "в репозиториях сторон могут отличаться.",
            "",
            "## Правила",
            "",
            "- Правила обмена правятся из этой папки.",
            f"- Код и данные стороны — её серверами (префикс {prefixes}).",
            "- Работа по проекту базы — в папке проекта; папка обмена не внутри проектов.",
            "- `.mcp.json` этой папки не читать: в нём учётные данные сторон.",
            "  Список серверов — таблица выше.",
            "",
        ]
    )
    return "\n".join(lines)


def _note_missing_exchange_hint(path: Path) -> str | None:
    """Строка отчёта, если существующий файл не ведёт к `KD-EXCHANGE.md`.

    `CLAUDE.md` ведёт к нему и строкой `@AGENTS.md`: так его создаёт установка, и
    Claude Code подключает `AGENTS.md` целиком.
    """
    text = _read_utf8(path)
    if text is not None and "KD-EXCHANGE.md" in text:
        return None
    if text is not None and path.name == "CLAUDE.md" and AGENTS_IMPORT.search(text):
        return None
    return f"{path.name}: добавьте строку «{EXCHANGE_HINT}»"


def _exchange_profile(
    sides: Sequence[tuple[str, Path]],
    exchange: str | None,
    projects_file: Path,
    local_file: Path,
) -> tuple[Sequence[tuple[str, Path]], Exchange | None, dict[str, str]]:
    """Выбирает профиль до записи; без файлов каталога прежний --side работает как раньше."""
    if exchange is None and (not projects_file.is_file() or not local_file.is_file()):
        return sides, None, {}
    if exchange is None:
        # С --side каталог — только подсказка: никакой сбой его разбора (и не только
        # ProjectConfigError — кривой тип поля даёт TypeError) не ломает прежнюю установку.
        try:
            catalog = load_catalog(projects_file)
            local = load_local(local_file)
        except Exception:
            return sides, None, {}
        profile = None
    else:
        try:
            catalog = load_catalog(projects_file)
            profile = catalog.exchange(exchange)
            local = load_local(local_file)
        except ProjectConfigError as error:
            raise SystemExit(str(error)) from None
        if not profile.sides:
            raise SystemExit(f"Обмен «{profile.id}»: профиль без сторон (sides)")
    if profile is not None:
        chosen = []
        for side in profile.sides:
            if SIDE_NAME.fullmatch(side.project) is None:
                raise SystemExit(
                    f"Проект «{side.project}» не годится в имя стороны; задайте --side"
                )
            folder = local.project_dirs.get(side.project)
            if folder is None:
                raise SystemExit(f"Не задана папка проекта «{side.project}» в projects.local.yaml")
            chosen.append((side.project, folder))
        return chosen, profile, {s.project: s.project for s in profile.sides}
    actual = {os.path.normcase(str(folder.resolve())): name for name, folder in sides}
    matches = []
    for item in catalog.exchanges:
        if not item.sides:
            continue
        folders = {s.project: local.project_dirs.get(s.project) for s in item.sides}
        if any(folder is None for folder in folders.values()):
            continue
        paths = {
            project: os.path.normcase(str(folder.resolve()))
            for project, folder in folders.items()
            if folder is not None
        }
        if len(set(paths.values())) == 2 and set(paths.values()) == set(actual):
            matches.append((item, {project: actual[path] for project, path in paths.items()}))
    if len(matches) > 1:
        raise SystemExit(
            "Папки совпали с несколькими профилями; выберите --exchange: "
            + ", ".join(p.id for p, _ in matches)
        )
    return (sides, *matches[0]) if matches else (sides, None, {})


def _render_profile_exchange_md(
    sides: Sequence[tuple[str, Path]],
    built: ExchangeServers,
    profile: Exchange,
    names: dict[str, str],
) -> str:
    """Подставляет только раздел направлений; прежний текст без профиля не меняется."""
    original = _render_exchange_md(sides, built)
    start, _, rest = original.partition("## Направления и файлы правил")
    _, _, end = rest.partition("## Правила")
    lines = [
        "## Направления и файлы правил",
        "",
        f"Профиль обмена: `{profile.id}`; технология: `{profile.technology or 'не задана'}`.",
        "",
    ]
    for side in profile.sides:
        lines.append(
            f"- {names[side.project]}: план `{side.plan}`, "
            f"технология `{profile.technology or 'не задана'}`."
        )
    by_project = {s.project: s for s in profile.sides}
    lines.append("")
    # Технология не задана, а у сторон только модули — это тоже универсальный формат по описанию.
    modules_only = any(s.module for s in profile.sides) and not any(s.rules for s in profile.sides)
    if profile.technology == "ed" or (profile.technology is None and modules_only):
        lines.extend(
            ["| направление | модуль источника | модуль приёмника |", "| --- | --- | --- |"]
        )
        lines.extend(
            f"| {names[source]} → {names[target]} | `{by_project[source].module or 'не задан'}` | "
            f"`{by_project[target].module or 'не задан'}` |"
            for source, target in profile.directions
        )
    else:
        lines.extend(
            ["| направление | выгрузка | загрузка | регистрация |", "| --- | --- | --- | --- |"]
        )

        folders = dict(sides)

        def file(project: str, filename: str) -> str:
            folder = by_project[project].rules
            if not folder:
                return "не задан"
            side_folder = folders.get(names[project])
            missing = (
                side_folder is not None and not (resolve(side_folder, folder) / filename).is_file()
            )
            return (
                f"`{PurePosixPath(folder.replace(chr(92), '/')) / filename}` {names[project]}"
                + (" (файла нет)" if missing else "")
            )

        for source, target in profile.directions:
            lines.append(
                f"| {names[source]} → {names[target]} | {file(source, 'ExchangeRules.xml')} | "
                f"{file(target, 'CorrespondentExchangeRules.xml')} | "
                f"{file(source, 'RegistrationRules.xml')} |"
            )
        defaults = [names[s.project] for s in profile.sides if s.rules_default]
        if defaults:
            lines.extend(
                [
                    "",
                    f"Папка комплекта не задана в профиле ({', '.join(defaults)}) — взята папка",
                    "плана в живых правилах проекта (`rules_dir`).",
                ]
            )
        lines.extend(
            [
                "",
                "Пути — от папки проекта стороны. Загружает приёмник по копии правил",
                "корреспондента из своего комплекта, а не по файлу источника. Совпадение копий",
                "проверять `rules_diff`, а не хешем файла: концы строк в репозиториях сторон",
                "могут отличаться.",
            ]
        )
    return start + "\n".join(lines) + "\n\n## Правила" + end


def _write_exchange_entries(dest: Path, client: str) -> list[str]:
    """Создаёт `AGENTS.md` и `CLAUDE.md`, только если файлов ещё нет."""
    notes: list[str] = []
    agents = dest / "AGENTS.md"
    if agents.is_file():
        note = _note_missing_exchange_hint(agents)
        if note is not None:
            notes.append(note)
    else:
        body = EXCHANGE_HINT + "\n"
        if client == "agents":
            body += RULES_HINT + "\n"
        agents.write_bytes(body.encode("utf-8"))
    claude = dest / "CLAUDE.md"
    if claude.is_file():
        note = _note_missing_exchange_hint(claude)
        if note is not None:
            notes.append(note)
    else:
        claude.write_bytes(b"@AGENTS.md\n")
    return notes


def install_exchange(
    dest: Path,
    sides: Sequence[tuple[str, Path]],
    client: str,
    server_url: str | None = None,
    *,
    cursor: bool = False,
    exchange: str | None = None,
    projects_file: Path | None = None,
    local_file: Path | None = None,
) -> list[str]:
    """Ставит упаковку в папку обмена и подключает серверы сторон.

    Сначала проверки (стороны, пути, JSON, конфликт имён) — до создания папки и любой
    записи. Затем `install` без `--cursor`, слияние серверов, при `cursor` перенос HTTP
    из собранного `.mcp.json`, `KD-EXCHANGE.md` и точки входа. Отчёт — строки `install`
    и строки этого режима.
    """
    if exchange is not None and sides:
        raise SystemExit("--exchange нельзя вместе с --side")
    sides, profile, names = _exchange_profile(
        sides,
        exchange,
        projects_file or ROOT / "projects.yaml",
        local_file or ROOT / "projects.local.yaml",
    )
    resolved = _validate_exchange_sides(dest, sides)
    loaded: list[tuple[str, dict[str, Any]]] = []
    missing: list[str] = []
    for name, folder in resolved:
        servers, found = _side_servers(folder)
        loaded.append((name, servers))
        if not found:
            missing.append(name)
    _check_exchange_configs(dest)
    # Конфликт имён — тоже до создания папки.
    built = exchange_servers(loaded)
    dest.mkdir(parents=True, exist_ok=True)
    report = install(dest, client, server_url, cursor=False)
    if profile is not None:
        report.append(f"направления — из профиля обмена {profile.id}")
    # Строки install про «запустите с --cursor» относятся к вызову без переноса.
    # Здесь перенос идёт следом, из уже собранного файла.
    if cursor:
        report = [line for line in report if not line.startswith("Cursor:")]
    for name in missing:
        report.append(f"сторона {name} без серверов")
    skipped_stdio = [f"{side}: {name}" for side, names in built.stdio.items() for name in names]
    if skipped_stdio:
        report.append("stdio не перенесены: " + ", ".join(skipped_stdio))
    mcp_path = dest / ".mcp.json"
    data = read_mcp_config(mcp_path)
    servers = data.setdefault("mcpServers", {})
    report.extend(_merge_exchange_servers(servers, built, [name for name, _folder in resolved]))
    payload = _mcp_payload(data)
    if mcp_path.is_file() and mcp_path.read_bytes() == payload:
        report.append("серверы сторон уже подключены")
    else:
        _write_mcp_config(mcp_path, data)
    if built.has_headers:
        report.append(".mcp.json папки обмена содержит учётные данные — коммитить его нельзя")
    if cursor:
        report.extend(_transfer_exchange_cursor(dest, data))
    text = (
        _render_profile_exchange_md(resolved, built, profile, names)
        if profile
        else _render_exchange_md(resolved, built)
    )
    (dest / "KD-EXCHANGE.md").write_bytes(text.encode("utf-8"))
    report.extend(_write_exchange_entries(dest, client))
    return report


def _transfer_exchange_cursor(dest: Path, source: dict[str, Any]) -> list[str]:
    """Переносит HTTP из собранного `.mcp.json` в `.cursor/mcp.json` (создаёт файл)."""
    cursor_path = dest / ".cursor" / "mcp.json"
    existed = cursor_path.is_file()
    cursor_data = read_mcp_config(cursor_path)
    added, skipped = transfer_http_servers(source, cursor_data)
    lines: list[str] = []
    if added or not existed:
        _write_mcp_config(cursor_path, cursor_data)
    if added:
        lines.append(f"перенесено серверов из .mcp.json: {len(added)} (новых: {', '.join(added)})")
    else:
        lines.append("серверы 1С в `.cursor/mcp.json` уже есть")
    if skipped:
        lines.append("не перенесены: " + ", ".join(skipped))
    return lines


def _parse_exchange_sides(raw: Sequence[str]) -> list[tuple[str, Path]]:
    """`--side имя=папка` → пары. Без `=` — отказ до записи."""
    parsed: list[tuple[str, Path]] = []
    for item in raw:
        name, sep, folder = item.partition("=")
        if not sep or not name or not folder:
            raise SystemExit(f"не разобрать сторону «{item}»: нужно имя=папка")
        parsed.append((name, Path(folder)))
    return parsed


def _read_utf8(path: Path) -> str | None:
    if not path.is_file():
        return None
    try:
        return path.read_bytes().decode("utf-8-sig")
    except UnicodeDecodeError:
        return None


def _packaged_skill(folder: Path) -> bool:
    """Скилл нашей упаковки: имя наше и в шапке SKILL.md то же поле `name`."""
    if not folder.is_dir() or not our_skills.is_our_skill(folder.name):
        return False
    text = _read_utf8(folder / "SKILL.md")
    if text is None or not text.startswith("---"):
        return False
    parts = text.split("---", 2)
    if len(parts) < 3:
        return False
    pattern = rf"(?m)^name:\s*['\"]?{re.escape(folder.name)}['\"]?\s*$"
    return re.search(pattern, parts[1]) is not None


def remove_obsolete_pack(dest: Path, root: str, packed: set[str]) -> list[str]:
    """Снимает папки и файл прежней поставки, если их нет в текущей упаковке.

    Папка — только с признаком упаковки (`name` в шапке SKILL.md совпадает с каталогом).
    Файл правил — только с текстом прежней точки входа и только когда это уже не текущее имя.
    Отчёт — одна строка.
    """
    prefix = root.strip("/") + "/"
    shipped = {name[len(prefix) :].split("/", 1)[0] for name in packed if name.startswith(prefix)}
    removed: list[str] = []
    for legacy in LEGACY_SKILL_NAMES:
        if legacy in shipped:
            continue
        folder = dest / root / legacy
        if _packaged_skill(folder):
            shutil.rmtree(folder)
            removed.append(legacy)
    if RULES_ENTRY.name != LEGACY_RULES_FILE and LEGACY_RULES_FILE not in packed:
        rules = dest / LEGACY_RULES_FILE
        text = _read_utf8(rules)
        if text is not None and text.startswith(LEGACY_RULES_MARK):
            rules.unlink()
            removed.append(LEGACY_RULES_FILE)
    if not removed:
        return []
    return ["удалены устаревшие имена поставки: " + ", ".join(removed)]


def _shipped_skill_names(dest: Path, root: str, wanted: set[Path]) -> set[str]:
    root_path = dest / root
    shipped: set[str] = set()
    for path in wanted:
        try:
            rel = path.relative_to(root_path).as_posix()
        except ValueError:
            continue
        shipped.add(rel.split("/", 1)[0])
    return shipped


def _remove_stale(dest: Path, root: str, wanted: set[Path]) -> list[str]:
    """Удаляет из поставленных скиллов файлы, которых больше нет в упаковке."""
    shipped = _shipped_skill_names(dest, root, wanted)
    report: list[str] = []
    for skill in our_skills.our_skills(dest / root):
        if skill.name not in shipped:
            continue
        for path in sorted(skill.rglob("*"), reverse=True):
            if path.is_file() and path not in wanted:
                path.unlink()
                report.append(f"удалён устаревший {path.relative_to(dest).as_posix()}")
            elif path.is_dir() and not any(path.iterdir()):
                path.rmdir()
    return report


def _is_portable(name: str) -> bool:
    """Файл упаковки (путь от папки проекта) должен работать без этого репозитория."""
    if name == "KD-RULES.md":
        return True
    _, _, inner = name.partition("skills/")
    skill, _, rest = inner.partition("/")
    return skill in PORTABLE_SKILLS and rest.removeprefix("references/") not in SERVER_DOCS


def _relink(source: Path, target: str) -> str:
    """Относительная ссылка → соседняя копия в references/ или адрес в открытом репозитории."""
    if "://" in target or target.startswith(("#", "mailto:")):
        return target
    link, hash_mark, anchor = target.partition("#")
    try:
        relative = (source.parent / link).resolve().relative_to(ROOT).as_posix()
    except ValueError:
        return target
    if relative in COPIES:
        return f"{COPIES[relative]}{hash_mark}{anchor}"
    return f"{REPO_URL}/blob/main/{relative}{hash_mark}{anchor}"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Упаковки знаний агента kd-rules-mcp",
        epilog=(
            "Папка обмена двух проектов: --dest <папка> --side <имя>=<проект> "
            "--side <имя>=<проект>. Скиллы и kd-rules-mcp, HTTP-серверы сторон "
            "(общий без префикса, свой — <сторона>-), файл KD-EXCHANGE.md. "
            "Или --dest <папка> --exchange <id>: стороны, планы, файлы и направления "
            "из projects.yaml, папки проектов из projects.local.yaml. При совпадении --side "
            "с профилем он используется автоматически. "
            "Проекты сторон не изменяются."
        ),
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true", help="копии справочников актуальны")
    mode.add_argument("--write", action="store_true", help="пересобрать копии справочников")
    mode.add_argument(
        "--dest",
        type=Path,
        help="папка проекта 1С или папка обмена (вместе с --side)",
    )
    parser.add_argument("--client", choices=sorted(CLIENT_ROOTS), default="claude")
    parser.add_argument("--server-url", help="адрес сервера для .mcp.json проекта")
    parser.add_argument("--exchange", help="id профиля обмена из projects.yaml (вместо --side)")
    parser.add_argument(
        "--side",
        action="append",
        metavar="ИМЯ=ПАПКА",
        help=(
            "сторона папки обмена (ровно две, только вместе с --dest): "
            "имя из строчных латинских букв и папка её проекта"
        ),
    )
    parser.add_argument(
        "--cursor",
        action="store_true",
        help=(
            "дописать недостающие HTTP-серверы из .mcp.json в .cursor/mcp.json"
            " (создать файл, если его нет; в папке обмена — после сборки серверов сторон)"
        ),
    )
    return parser


def main() -> None:
    utf8_stdout()
    parser = _parser()
    args = parser.parse_args()
    if args.exchange is not None and args.side:
        parser.error("--exchange нельзя вместе с --side")
    if args.exchange is not None and args.dest is None:
        parser.error("--exchange указывается только вместе с --dest")
    if args.side and args.dest is None:
        parser.error("--side указывается только вместе с --dest")

    if args.check:
        stale = stale_copies()
        if stale:
            print("Копии справочников устарели — uv run python scripts/build_packs.py --write:")
            print("\n".join(f"  {name}" for name in stale))
        hits = portability_hits(pack_files("claude"))
        if hits:
            print("Пути этого репозитория в переносимых текстах:")
            print("\n".join(f"  {hit}" for hit in hits))
        if stale or hits:
            raise SystemExit(1)
        print(f"Копии справочников актуальны: {len(COPIES)}; путей репозитория в упаковке нет")
    elif args.write:
        changed = write_copies()
        print(f"Переписано копий: {len(changed)}")
        print("\n".join(f"  {name}" for name in changed))
    else:
        if stale_copies():
            print("Внимание: копии справочников в репозитории устарели (--write); ставится сборка")
        dest = args.dest
        if dest is None:
            parser.error("нужна папка --dest")
        if args.side or args.exchange is not None:
            lines = install_exchange(
                dest,
                _parse_exchange_sides(args.side or []),
                args.client,
                args.server_url,
                cursor=args.cursor,
                exchange=args.exchange,
            )
        else:
            lines = install(dest, args.client, args.server_url, cursor=args.cursor)
        print("\n".join(lines))


if __name__ == "__main__":
    main()
