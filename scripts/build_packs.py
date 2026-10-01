"""Упаковки знаний агента: справочники внутри скилла и установка скиллов в папку проекта 1С.

Источник правды — скиллы `.claude/skills/kd2-*` и документы `docs/`. Копий для отдельных
клиентов в репозитории нет: `.claude/skills` читают Claude Code, Cursor и OpenCode.

1. `--write` / `--check` — кладёт в `.claude/skills/kd2-rules-build/references/` копии
   `docs/rules/mcp-1c.md`, `docs/checks.md`, `docs/tools.md`, `docs/glossary.md` под теми же
   именами, чтобы скилл был самодостаточен в чужом проекте. Копия — шапка «откуда она» и текст
   источника; относительные ссылки в нём ведут на соседние копии или в открытый репозиторий.
   `--check` — копии совпадают со сборкой (иначе код выхода 1).
2. `--dest <папка проекта> --client claude|agents` — ставит скиллы `kd2-*` в проект:
   `claude` — `.claude/skills/` (Claude Code, Cursor, OpenCode), `agents` — `.agents/skills/` и
   `KD2-RULES.md` в корень (Codex и клиенты, которые `.claude/skills` не читают). Обе сразу
   нельзя: Cursor и OpenCode увидели бы каждый скилл дважды. В `.mcp.json` проекта (и в
   `.cursor/mcp.json`, если он есть) добавляется сервер `kd2-rules-mcp`, другие серверы не
   трогаются. Адрес — `--server-url`, по умолчанию `server_url` из `projects.local.yaml` этого
   репозитория. Это правка чужого репозитория — только с согласия человека.

Запуск: `uv run python scripts/build_packs.py --check` (или `--write`);
`uv run python scripts/build_packs.py --dest <папка проекта> --client claude [--server-url URL]`.
"""

import argparse
import json
import re
from pathlib import Path
from typing import Any

from kd2_rules_mcp.projects import DEFAULT_SERVER_URL, load_local

ROOT = Path(__file__).resolve().parents[1]
SKILLS = ROOT / ".claude" / "skills"
REFERENCES = SKILLS / "kd2-rules-build" / "references"
RULES_ENTRY = ROOT / "docs" / "rules" / "KD2-RULES.md"
REPO_URL = "https://github.com/egordoronchenko/kd2-rules-mcp"
SERVER = "kd2-rules-mcp"

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
PORTABLE_SKILLS = {"kd2-rules-build", "kd2-exchange-pitfalls"}
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
    for skill in sorted(path for path in SKILLS.glob("kd2-*") if path.is_dir()):
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


def install(dest: Path, client: str, server_url: str | None = None) -> list[str]:
    """Ставит упаковку в папку проекта; возвращает строки отчёта."""
    dest = dest.resolve()
    if not dest.is_dir():
        raise SystemExit(f"Нет папки проекта {dest}")
    if dest == ROOT:
        raise SystemExit("Это репозиторий сервера: скиллы здесь — источник, ставить их не нужно")
    files = pack_files(client)
    for other, other_root in CLIENT_ROOTS.items():
        if other != client and any((dest / other_root).glob("kd2-*")):
            raise SystemExit(
                f"В проекте уже стоит упаковка {other} ({other_root}/kd2-*): Cursor и OpenCode "
                "видели бы каждый скилл дважды — удалите её или ставьте ту же"
            )
    # Конфигурации MCP читаются до записи: битый файл останавливает установку, ничего не меняя.
    configs = [(dest / ".mcp.json", True)]
    if (dest / ".cursor" / "mcp.json").is_file():
        configs.append((dest / ".cursor" / "mcp.json", False))
    loaded = [(path, read_mcp_config(path), typed) for path, typed in configs]
    report = _remove_stale(dest, CLIENT_ROOTS[client], {dest / name for name in files})
    written = 0
    for name, content in files.items():
        path = dest / name
        if path.is_file() and path.read_bytes() == content:
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        written += 1
    report.append(f"упаковка {client}: файлов {len(files)}, записано {written}")
    url = server_url or default_server_url()
    report.extend(add_server(path, data, url, typed=typed) for path, data, typed in loaded)
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


def add_server(path: Path, data: dict[str, Any], url: str, *, typed: bool) -> str:
    """Добавляет или обновляет `kd2-rules-mcp` в `mcpServers`, не трогая остальные серверы."""
    shown = f".cursor/{path.name}" if path.parent.name == ".cursor" else path.name
    servers = data.setdefault("mcpServers", {})
    entry: dict[str, Any] = {"type": "http", "url": url} if typed else {"url": url}
    if servers.get(SERVER) == entry:
        return f"{shown}: {SERVER} уже подключён ({url})"
    action = "обновлён" if SERVER in servers else "добавлен"
    servers[SERVER] = entry
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes((json.dumps(data, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
    return f"{shown}: {SERVER} {action} ({url})"


def default_server_url() -> str:
    """`server_url` из `projects.local.yaml` этого репозитория, иначе адрес по умолчанию."""
    local = ROOT / "projects.local.yaml"
    return load_local(local).server_url if local.is_file() else DEFAULT_SERVER_URL


def _remove_stale(dest: Path, root: str, wanted: set[Path]) -> list[str]:
    """Удаляет из `kd2-*` проекта файлы, которых больше нет в упаковке (после обновления)."""
    report: list[str] = []
    for skill in sorted((dest / root).glob("kd2-*")):
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
    parser = argparse.ArgumentParser(description="Упаковки знаний агента kd2-rules-mcp")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true", help="копии справочников актуальны")
    mode.add_argument("--write", action="store_true", help="пересобрать копии справочников")
    mode.add_argument("--dest", type=Path, help="папка проекта 1С, куда поставить скиллы")
    parser.add_argument("--client", choices=sorted(CLIENT_ROOTS), default="claude")
    parser.add_argument("--server-url", help="адрес сервера для .mcp.json проекта")
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
        print("\n".join(install(args.dest, args.client, args.server_url)))


if __name__ == "__main__":
    main()
