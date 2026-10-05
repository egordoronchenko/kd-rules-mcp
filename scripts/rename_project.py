"""Одноразовое переименование проекта kd2-rules-mcp → kd-rules-mcp.

Повторный запуск ничего не меняет. После переименования скрипт удаляют.
Не трогает историю (`reference/`, `openspec/`, `docs/plans/`, выпущенные разделы
`CHANGELOG.md`), чужие записи `uv.lock`, двоичные файлы и строки из списка
`ALLOWANCES` (у каждой — причина).

Запуск из корня репозитория: `python scripts/rename_project.py --check` или `--write`.
Только стандартная библиотека. После записи на этом репозитории — `ruff format`
(короче ставшие строки) и генераторы справочников. Зерно УИДов расширений и
байтовые эталоны авторинга не переписываются.
"""

import argparse
import contextlib
import io
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

# Дольше — раньше, чтобы короткое имя не съело кусок длинного. Пары не вложены друг в друга.
TOKENS: tuple[tuple[str, str], ...] = (
    ("kd2-rules-mcp", "kd-rules-mcp"),
    ("kd2_rules_mcp", "kd_rules_mcp"),
    ("kd2-ed-rules", "kd3-rules"),
    ("kd2-install", "kd-install"),
    ("KD2-RULES.md", "KD-RULES.md"),
)

# Каталоги, которые не читаем: эталон, локальные данные, кэши. Причина — в ALLOWANCES
# либо это служебные каталоги инструментов.
SKIP_DIR_NAMES = {
    ".git",
    ".venv",
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    ".uv-cache",
    "node_modules",
}
SKIP_PREFIXES = (
    "reference/",
    "base/",
    "structures/",
    "workspace/",
    "course/",
    "cache/",
    "kdbase/build/",
    "kdbase/run/",
)
BINARY_SUFFIXES = {
    ".epf",
    ".erf",
    ".cf",
    ".cfe",
    ".dt",
    ".1cd",
    ".bin",
    ".png",
    ".jpg",
    ".jpeg",
    ".ico",
    ".zip",
    ".xlsx",
    ".pyc",
    ".sqlite",
    ".db",
}


@dataclass(frozen=True)
class Allowance:
    """Где старое имя остаётся намеренно.

    `path` с «/» на конце — дерево. `line_contains` — только такие строки файла.
    `release_history` — строки `CHANGELOG.md` ниже раздела Unreleased.
    """

    path: str
    reason: str
    line_contains: str | None = None
    release_history: bool = False


# Порядок важен только внутри одного файла: первое совпадение задаёт причину.
ALLOWANCES: tuple[Allowance, ...] = (
    Allowance("docs/plans/", "история: планы и постановки не переписываются"),
    Allowance("openspec/", "история: спецификации не входят в переименование"),
    Allowance("reference/", "история: эталон формата, только чтение"),
    Allowance("base/", "локальные данные машины, не исходники"),
    Allowance("structures/", "локальные данные машины, не исходники"),
    Allowance("workspace/", "локальные данные машины, не исходники"),
    Allowance("course/", "локальные данные машины, не исходники"),
    Allowance("cache/", "локальные данные машины, не исходники"),
    Allowance(
        "CHANGELOG.md",
        "история: выпущенные разделы CHANGELOG.md",
        release_history=True,
    ),
    Allowance(
        "CHANGELOG.md",
        "заметка о переименовании в CHANGELOG",
        line_contains="допишет принимающий",
    ),
    Allowance(
        "README.md",
        "заметка о переименовании в README",
        line_contains="уже установивших",
    ),
    Allowance(
        "docs/INSTALL.md",
        "заметка о переименовании в INSTALL",
        line_contains="уже установивших",
    ),
    Allowance(
        "scripts/build_packs.py",
        "совместимость установки",
        line_contains="LEGACY_",
    ),
    Allowance(
        "scripts/setup_local.py",
        "совместимость установки: прежний контейнер и проект compose",
        line_contains="LEGACY_",
    ),
    Allowance(
        "src/kd2_rules_mcp/authoring/ed/identity.py",
        "зерно идентификаторов расширений: от него зависят УИДы уже установленных расширений",
        line_contains="EXTENSION_IDENTITY_SEED",
    ),
    Allowance(
        "src/kd_rules_mcp/authoring/ed/identity.py",
        "зерно идентификаторов расширений: от него зависят УИДы уже установленных расширений",
        line_contains="EXTENSION_IDENTITY_SEED",
    ),
    Allowance(
        "scripts/rename_project.py",
        "скрипт переименования: таблица замен и разрешённые места",
    ),
    Allowance(
        "tests/test_rename_project.py",
        "тесты скрипта держат старые имена как вход",
    ),
)


@dataclass
class Report:
    pending: list[str]
    allowed: dict[str, int]
    lines_changed: int
    files_moved: int
    generators: list[tuple[str, int]]


def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _utf8_stdout() -> None:
    for stream in (sys.stdout, sys.stderr):
        if isinstance(stream, io.TextIOWrapper):
            stream.reconfigure(encoding="utf-8", errors="replace")


def _replace_tokens(text: str) -> str:
    for old, new in TOKENS:
        text = text.replace(old, new)
    return text


def _tokens_in(text: str) -> list[str]:
    return [old for old, _new in TOKENS if old in text]


def allowance_for(rel: str, line: str | None, *, frozen: bool) -> str | None:
    """Причина, по которой старое имя в этом месте оставляем; иначе None."""
    for item in ALLOWANCES:
        if item.path.endswith("/"):
            if rel.startswith(item.path) or rel + "/" == item.path:
                return item.reason
            continue
        if rel != item.path:
            continue
        if item.release_history:
            if frozen:
                return item.reason
            continue
        if item.line_contains is None:
            return item.reason
        if line is not None and item.line_contains in line:
            return item.reason
    return None


def _skipped(rel: str) -> bool:
    return any(rel.startswith(prefix) for prefix in SKIP_PREFIXES)


def _walk(root: Path) -> list[str]:
    found: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [name for name in dirnames if name not in SKIP_DIR_NAMES]
        rel_dir = Path(dirpath).relative_to(root).as_posix()
        if rel_dir == ".":
            rel_dir = ""
        if rel_dir and _skipped(rel_dir + "/"):
            dirnames.clear()
            continue
        kept: list[str] = []
        for name in dirnames:
            child = f"{rel_dir}/{name}" if rel_dir else name
            if _skipped(child + "/"):
                continue
            kept.append(name)
        dirnames[:] = kept
        for name in filenames:
            rel = f"{rel_dir}/{name}" if rel_dir else name
            if not _skipped(rel):
                found.append(rel)
    return found


def _read_text(path: Path) -> str | None:
    if path.suffix.lower() in BINARY_SUFFIXES:
        return None
    data = path.read_bytes()
    if b"\0" in data:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _frozen_changelog_lines(text: str) -> set[int]:
    """Номера строк ниже раздела Unreleased (выпущенные версии)."""
    lines = text.splitlines()
    seen_unreleased = False
    frozen_from: int | None = None
    for number, line in enumerate(lines, 1):
        if line.startswith("## [Unreleased]"):
            seen_unreleased = True
            continue
        if seen_unreleased and line.startswith("## ["):
            frozen_from = number
            break
    if frozen_from is None:
        return set()
    return set(range(frozen_from, len(lines) + 1))


def _project_lock_lines(text: str) -> set[int]:
    """Строки блока `[[package]]` самого проекта (`source = { editable = "." }`)."""
    lines = text.splitlines()
    starts = [index for index, line in enumerate(lines) if line.startswith("[[package]]")]
    numbers: set[int] = set()
    for index, start in enumerate(starts):
        end = starts[index + 1] - 1 if index + 1 < len(starts) else len(lines) - 1
        block = "\n".join(lines[start : end + 1])
        if 'source = { editable = "." }' in block:
            numbers.update(range(start + 1, end + 2))
    return numbers


def _record(allowed: dict[str, int], reason: str) -> None:
    allowed[reason] = allowed.get(reason, 0) + 1


def _edit_file(rel: str, text: str, *, write: bool) -> tuple[str, int, list[str], dict[str, int]]:
    frozen = _frozen_changelog_lines(text) if rel == "CHANGELOG.md" else set()
    lock_lines = _project_lock_lines(text) if rel == "uv.lock" else set()
    pending: list[str] = []
    allowed: dict[str, int] = {}
    changed = 0
    new_lines: list[str] = []
    for number, line in enumerate(text.splitlines(keepends=True), 1):
        found = _tokens_in(line)
        reason = allowance_for(rel, line, frozen=number in frozen)
        if rel == "uv.lock" and number not in lock_lines and found:
            reason = reason or "uv.lock: запись не этого проекта"
        if reason is not None:
            if found:
                _record(allowed, reason)
            new_lines.append(line)
            continue
        if not found:
            new_lines.append(line)
            continue
        updated = _replace_tokens(line)
        if write:
            if updated != line:
                changed += 1
            new_lines.append(updated)
            if _tokens_in(updated):
                pending.append(f"{rel}:{number}: {_tokens_in(updated)[0]}")
        else:
            new_lines.append(line)
            pending.append(f"{rel}:{number}: {found[0]}")
    return "".join(new_lines), changed, pending, allowed


def renamed_path(rel: str) -> str:
    parts: list[str] = []
    for part in rel.split("/"):
        parts.append(_replace_tokens(part))
    return "/".join(parts)


def _git(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(root), "-c", "core.quotepath=false", *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def _tracked(root: Path) -> set[str] | None:
    git_entry = root / ".git"
    if not git_entry.exists():
        return None
    result = _git(root, "ls-files")
    if result.returncode != 0:
        return None
    return set(result.stdout.splitlines())


def _move(root: Path, rel: str, new_rel: str, tracked: set[str] | None) -> None:
    destination = root / new_rel
    destination.parent.mkdir(parents=True, exist_ok=True)
    if tracked is not None and rel in tracked:
        result = _git(root, "mv", "--", rel, new_rel)
        if result.returncode != 0:
            raise SystemExit(f"git mv {rel} {new_rel}: {result.stderr.strip()}")
        return
    (root / rel).rename(destination)


def _prune_empty(root: Path) -> None:
    for dirpath, _dirnames, _filenames in os.walk(root, topdown=False):
        current = Path(dirpath)
        if current == root or ".git" in current.parts or current.name in SKIP_DIR_NAMES:
            continue
        rel = current.relative_to(root).as_posix()
        if _skipped(rel + "/"):
            continue
        # dirnames — снимок обхода, не то, что осталось после rmdir детей.
        if not any(current.iterdir()):
            with contextlib.suppress(OSError):
                current.rmdir()


def _scan_paths(files: list[str]) -> tuple[list[str], dict[str, int]]:
    pending: list[str] = []
    allowed: dict[str, int] = {}
    for rel in files:
        found = _tokens_in(rel)
        if not found:
            continue
        reason = allowance_for(rel, None, frozen=False)
        if reason is not None:
            _record(allowed, reason)
            continue
        pending.append(f"{rel}: путь содержит «{found[0]}»")
    return pending, allowed


def _add_counts(total: dict[str, int], part: dict[str, int]) -> None:
    for reason, count in part.items():
        total[reason] = total.get(reason, 0) + count


def apply(root: Path, *, write: bool, generators: bool = False) -> Report:
    """Проверяет или переименовывает дерево `root`. Генераторы — только по запросу."""
    files = _walk(root)
    pending: list[str] = []
    allowed: dict[str, int] = {}
    lines_changed = 0
    path_pending, path_allowed = _scan_paths(files)
    if not write:
        pending.extend(path_pending)
    _add_counts(allowed, path_allowed)

    for rel in files:
        path = root / rel
        text = _read_text(path)
        if text is None:
            continue
        new_text, changed, file_pending, file_allowed = _edit_file(rel, text, write=write)
        pending.extend(file_pending)
        _add_counts(allowed, file_allowed)
        lines_changed += changed
        if write and changed:
            path.write_bytes(new_text.encode("utf-8"))

    files_moved = 0
    if write:
        tracked = _tracked(root)
        # Повторный обход: содержимое уже новое, пути ещё старые.
        for rel in sorted(_walk(root), key=len, reverse=True):
            new_rel = renamed_path(rel)
            if new_rel == rel or not (root / rel).is_file():
                continue
            _move(root, rel, new_rel, tracked)
            files_moved += 1
        _prune_empty(root)
        files = _walk(root)
        pending, allowed = [], {}
        path_pending, path_allowed = _scan_paths(files)
        pending.extend(path_pending)
        _add_counts(allowed, path_allowed)
        for rel in files:
            text = _read_text(root / rel)
            if text is None:
                continue
            _new, _changed, file_pending, file_allowed = _edit_file(rel, text, write=False)
            pending.extend(file_pending)
            _add_counts(allowed, file_allowed)

    generator_codes: list[tuple[str, int]] = []
    if write and generators and (lines_changed or files_moved or pending):
        generator_codes = _generators(root)
        # Генераторы могли поправить копии; ещё раз смотрим, не всплыло ли старое имя.
        pending, allowed = [], {}
        files = _walk(root)
        path_pending, path_allowed = _scan_paths(files)
        pending.extend(path_pending)
        _add_counts(allowed, path_allowed)
        for rel in files:
            text = _read_text(root / rel)
            if text is None:
                continue
            _new, _changed, file_pending, file_allowed = _edit_file(rel, text, write=False)
            pending.extend(file_pending)
            _add_counts(allowed, file_allowed)
    return Report(pending, allowed, lines_changed, files_moved, generator_codes)


def _generators(root: Path) -> list[tuple[str, int]]:
    """Форматирует код и справочники. Код выхода каждого шага — в отчёте.

    Эталоны авторинга не пересобираются: УИДы в них считаются от замороженного зерна.
    """
    codes: list[tuple[str, int]] = []
    # Имена короче: ruff склеивает строки, которые раньше не влезали в лимит.
    steps = (
        ("ruff format .", ["ruff", "format", "."]),
        ("scripts/dump_tools.py --write", ["python", "scripts/dump_tools.py", "--write"]),
        ("scripts/build_packs.py --write", ["python", "scripts/build_packs.py", "--write"]),
    )
    for label, args in steps:
        result = subprocess.run(
            ["uv", "run", *args],
            cwd=root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        print(f"{label}: код {result.returncode}")
        if result.returncode != 0:
            tail = (result.stdout + "\n" + result.stderr).strip()
            if tail:
                print(tail[-2000:])
        codes.append((label, result.returncode))
    return codes


def _print_allowances() -> None:
    print("Разрешённые места:")
    for item in ALLOWANCES:
        if item.release_history:
            extra = ", строки ниже Unreleased"
        elif item.line_contains:
            extra = f", строки с «{item.line_contains}»"
        elif item.path.endswith("/"):
            extra = ", всё дерево"
        else:
            extra = ", весь файл"
        print(f"  {item.path}{extra} — {item.reason}")


def main(argv: list[str] | None = None) -> None:
    _utf8_stdout()
    parser = argparse.ArgumentParser(
        description="Переименование проекта kd2-rules-mcp → kd-rules-mcp"
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true", help="показать, что ещё не переименовано")
    mode.add_argument("--write", action="store_true", help="переименовать")
    args = parser.parse_args(argv)
    root = repo_root()
    real = Path(__file__).resolve().parents[1]
    report = apply(
        root,
        write=args.write,
        generators=args.write and root.resolve() == real.resolve(),
    )
    if args.check:
        _print_allowances()
        if report.pending:
            print(f"Осталось переименовать ({len(report.pending)}):")
            print("\n".join(f"  {hit}" for hit in report.pending))
            raise SystemExit(1)
        print("Старых имён вне разрешённых мест нет.")
        return
    print(f"Перемещено файлов: {report.files_moved}")
    print(f"Заменено строк: {report.lines_changed}")
    for label, code in report.generators:
        print(f"{label}: код {code}")
    if report.pending:
        print(f"После записи старое имя ещё есть ({len(report.pending)}):")
        print("\n".join(f"  {hit}" for hit in report.pending[:50]))
        raise SystemExit(1)
    if any(code != 0 for _label, code in report.generators):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
