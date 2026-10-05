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
   `KD2-RULES.md` в корень (Codex и клиенты, которые `.claude/skills` не читают). Обе сразу
   нельзя: Cursor и OpenCode увидели бы каждый скилл дважды. В `.mcp.json` проекта добавляется
   сервер `kd2-rules-mcp`, другие серверы не трогаются. `--cursor` создаёт `.cursor/mcp.json`,
   если файла нет, и дописывает туда недостающие HTTP-серверы из `.mcp.json` (существующие
   записи не меняет; stdio с `command` не переносит). Без флага существующий файл серверами 1С
   не пополняется: скрипт называет, каких нет. Без флага и без файла скрипт сообщает, что сервер
   нужно добавить в настройках MCP Cursor. Адрес — `--server-url`, по умолчанию
   `server_url` из `projects.local.yaml` этого репозитория. Заголовок `Authorization` — из
   `token` того же файла, если токен задан. Это правка чужого репозитория — только с согласия
   человека.

Запуск: `uv run python scripts/build_packs.py --check` (или `--write`);
`uv run python scripts/build_packs.py --dest <папка проекта> --client claude`
`[--cursor] [--server-url URL]`.
"""

import argparse
import json
import re
import shutil
from pathlib import Path
from typing import Any

import our_skills

from kd2_rules_mcp.console import utf8_stdout
from kd2_rules_mcp.projects import DEFAULT_SERVER_URL, bearer_auth, load_local

ROOT = Path(__file__).resolve().parents[1]
SKILLS = ROOT / ".claude" / "skills"
REFERENCES = SKILLS / "kd2-rules-build" / "references"
RULES_ENTRY = ROOT / "docs" / "rules" / "KD2-RULES.md"
REPO_URL = "https://github.com/egordoronchenko/kd2-rules-mcp"
SERVER = "kd2-rules-mcp"
# Прежние имена поставки. Строки с LEGACY_ скрипт переименования не меняет.
LEGACY_SKILL_NAMES = ("kd2-ed-rules", "kd2-install")
LEGACY_RULES_FILE = "KD2-RULES.md"
LEGACY_SERVER = "kd2-rules-mcp"
LEGACY_RULES_MARK = "# Правила обмена КД 2 и сервер kd2-rules-mcp"

# Документ репозитория → имя копии в references/ скилла kd2-rules-build.
COPIES: dict[str, str] = {
    "docs/rules/mcp-1c.md": "mcp-1c.md",
    "docs/checks.md": "checks.md",
    "docs/tools.md": "tools.md",
    "docs/glossary.md": "glossary.md",
}
CLIENT_ROOTS = {"claude": ".claude/skills", "agents": ".agents/skills"}
LINK = re.compile(r"\]\(([^)\s]+)\)")
SKIP_PARTS = {"__pycache__"}
# Тексты, которые работают в папке проекта 1С без этого репозитория: его путей в них быть не должно.
# kd2-install не входит — он ведёт установку из клона сервера. Копии справочников сервера
# (checks, tools, glossary) описывают сам сервер и оговаривают в шапке, что пути — от клона.
PORTABLE_SKILLS = {"kd2-rules-build", "kd2-exchange-pitfalls", "kd2-ed-rules"}
SERVER_DOCS = {"checks.md", "tools.md", "glossary.md"}
REPO_PATHS = {
    "setup_local.py": re.compile(r"setup_local"),
    # Каталог репозитория в начале пути; `<папка проекта>\…\docs\` — уже путь проекта, не наш.
    "scripts/": re.compile(r"(?<![\w.\\/-])scripts[/\\]"),
    "workspace/": re.compile(r"(?<![\w.\\/-])workspace[/\\]"),
    "docs/": re.compile(r"(?<![\w.\\/-])docs[/\\]"),
    "src/kd2_rules_mcp": re.compile(r"src[/\\]kd2_rules_mcp"),
    "mcp-1c.mdc": re.compile(r"mcp-1c\.mdc"),
}
URL = re.compile(r"https?://\S+")
HEADER = (
    "> Копия справочника из репозитория сервера kd2-rules-mcp\n"
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
            raise SystemExit("Нет docs/rules/KD2-RULES.md — упаковку agents не собрать")
        files["KD2-RULES.md"] = RULES_ENTRY.read_bytes()
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
    # kd2-rules-mcp уже совпадает, add_server файл не переписывает — тогда пишем сами.
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
            "добавьте в AGENTS.md проекта строку «Правила обмена КД 2 и сервер kd2-rules-mcp — "
            "прочитай KD2-RULES.md перед работой с ними»"
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


def _write_mcp_config(path: Path, data: dict[str, Any]) -> None:
    """Записывает конфиг MCP целиком (UTF-8, LF)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes((json.dumps(data, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))


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
    """HTTP-серверы `.mcp.json`, которых нет в конфиге Cursor, кроме `kd2-rules-mcp`."""
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
    """Добавляет или обновляет `kd2-rules-mcp` в `mcpServers`, не трогая остальные серверы.

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
    if name == "KD2-RULES.md":
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


def main() -> None:
    utf8_stdout()
    parser = argparse.ArgumentParser(description="Упаковки знаний агента kd2-rules-mcp")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true", help="копии справочников актуальны")
    mode.add_argument("--write", action="store_true", help="пересобрать копии справочников")
    mode.add_argument("--dest", type=Path, help="папка проекта 1С, куда поставить скиллы")
    parser.add_argument("--client", choices=sorted(CLIENT_ROOTS), default="claude")
    parser.add_argument("--server-url", help="адрес сервера для .mcp.json проекта")
    parser.add_argument(
        "--cursor",
        action="store_true",
        help=(
            "дописать недостающие HTTP-серверы из .mcp.json в .cursor/mcp.json"
            " (создать файл, если его нет)"
        ),
    )
    args = parser.parse_args()

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
        print("\n".join(install(args.dest, args.client, args.server_url, cursor=args.cursor)))


if __name__ == "__main__":
    main()
