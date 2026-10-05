"""Сборка открытого репозитория из этого: разрешённые файлы → проверка → коммит [→ push].

Открытая копия руками не правится. Скрипт берёт отслеживаемые git файлы этого репозитория, кроме
перечисленных в `publish/exclude.txt`, раскладывает их в каталог открытой копии (удаляя там то,
чего больше нет), ищет в текстах и путях слова из `publish/forbidden.txt`, прогоняет в собранной
копии ruff, pyright и pytest (без переменных `KD2_*` — как на чистой машине) и коммитит от автора,
заданного в git открытой копии. Строки соавторов не добавляются. Отправка — только с `--push`.

Если каталога `publish/` нет (скрипт запущен в самой открытой копии), публиковать нечего.

Запуск: `uv run python scripts/publish.py -m "сообщение" [--target КАТАЛОГ] [--push] [--no-checks]`;
каталог по умолчанию — `KD2_PUBLISH_DIR` или соседний `kd2-rules-mcp-public`.
"""

import argparse
import fnmatch
import os
import re
import shutil
import subprocess
from pathlib import Path

from kd2_rules_mcp.console import utf8_stdout

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "publish"
BINARY = {".epf", ".erf", ".cf", ".cfe", ".dt", ".1cd", ".bin", ".png", ".jpg", ".ico", ".zip"}


def main() -> None:
    utf8_stdout()
    parser = argparse.ArgumentParser(description="Сборка и публикация открытого репозитория")
    parser.add_argument("-m", "--message", required=True, help="сообщение коммита открытой копии")
    parser.add_argument("--target", type=Path, help="каталог открытой копии (git-репозиторий)")
    parser.add_argument("--push", action="store_true", help="после коммита отправить в origin")
    parser.add_argument("--no-checks", action="store_true", help="не запускать ruff/pyright/pytest")
    args = parser.parse_args()

    if not CONFIG.is_dir():
        raise SystemExit("Нет каталога publish/: публикация делается из рабочего репозитория")
    target = (args.target or _default_target()).resolve()
    if not (target / ".git").is_dir():
        raise SystemExit(f"{target} — не git-репозиторий: склонируйте открытый репозиторий туда")
    if _git(ROOT, "status", "--porcelain").strip():
        raise SystemExit("В рабочем репозитории есть незакоммиченные изменения — сначала коммит")

    files = _published_files()
    _sync(files, target)
    hits = _forbidden_hits(target, files)
    if hits:
        print("Запретные слова — публикация остановлена:")
        for hit in hits[:50]:
            print(f"  {hit}")
        _git(target, "checkout", "--", ".")
        _git(target, "clean", "-fdq")
        raise SystemExit(1)
    print(f"Собрано файлов: {len(files)}; запретных слов нет")

    if not args.no_checks:
        _checks(target)

    _git(target, "add", "-A")
    status = _git(target, "status", "--short")
    if not status.strip():
        print("Изменений нет — открытая копия уже совпадает")
        return
    print(status.rstrip())
    _git(target, "commit", "-q", "-m", args.message)
    print(_git(target, "log", "-1", "--format=%h %an <%ae> | %s").strip())
    if args.push:
        _git(target, "push", "-q")
        print("Отправлено в origin")
    else:
        print("Не отправлено: проверьте и запустите с --push (или git push в открытой копии)")


def _default_target() -> Path:
    configured = os.environ.get("KD2_PUBLISH_DIR")
    return Path(configured) if configured else ROOT.parent / "kd2-rules-mcp-public"


def _published_files() -> list[str]:
    """Отслеживаемые файлы, кроме исключённых."""
    patterns = _lines(CONFIG / "exclude.txt")
    tracked = _git(ROOT, "-c", "core.quotePath=false", "ls-files").splitlines()
    return [path for path in tracked if not any(_excluded(path, p) for p in patterns)]


def _excluded(path: str, pattern: str) -> bool:
    if pattern.endswith("/"):
        prefix = pattern.rstrip("/")
        parts = path.split("/")
        return any(
            fnmatch.fnmatchcase("/".join(parts[:depth]), prefix) for depth in range(1, len(parts))
        )
    return fnmatch.fnmatchcase(path, pattern)


def _sync(files: list[str], target: Path) -> None:
    """Раскладывает файлы в открытую копию и удаляет там отслеживаемые, которых больше нет."""
    wanted = set(files)
    for path in _git(target, "-c", "core.quotePath=false", "ls-files").splitlines():
        if path not in wanted:
            (target / path).unlink(missing_ok=True)
            _remove_empty_parents(target, (target / path).parent)
    for path in files:
        source, destination = ROOT / path, target / path
        destination.parent.mkdir(parents=True, exist_ok=True)
        if not destination.is_file() or destination.read_bytes() != source.read_bytes():
            shutil.copyfile(source, destination)


def _remove_empty_parents(target: Path, folder: Path) -> None:
    """Удаляет опустевшие каталоги до корня копии: пустая папка скилла сервера
    (`our_skills.is_our_skill`) — тоже копия для теста `test_skills.py`."""
    while folder != target and folder.is_dir() and not any(folder.iterdir()):
        folder.rmdir()
        folder = folder.parent


def _forbidden_hits(target: Path, files: list[str]) -> list[str]:
    """Совпадения запретных слов: `файл:строка: слово` (и в путях файлов)."""
    words = [re.compile(line, re.IGNORECASE) for line in _lines(CONFIG / "forbidden.txt")]
    hits: list[str] = []
    for path in files:
        for word in words:
            if match := word.search(path):
                hits.append(f"{path}: путь содержит «{match.group(0)}»")
        if Path(path).suffix.lower() in BINARY:
            continue
        text = (target / path).read_bytes().decode("utf-8", errors="replace")
        for number, line in enumerate(text.splitlines(), 1):
            for word in words:
                if match := word.search(line):
                    hits.append(f"{path}:{number}: «{match.group(0)}»")
    return hits


def _checks(target: Path) -> None:
    """ruff, pyright и pytest в открытой копии — без переменных KD2_* и чужого окружения."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("KD2_") and k != "VIRTUAL_ENV"}
    commands = [
        ["uv", "sync", "-q"],
        ["uv", "run", "ruff", "format", "--check", "-q"],
        ["uv", "run", "ruff", "check", "-q"],
        ["uvx", "pyright"],
        ["uv", "run", "pytest", "-q", "-p", "no:cacheprovider"],
    ]
    for command in commands:
        result = subprocess.run(
            command,
            cwd=target,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        tail = (result.stdout + result.stderr).strip().splitlines()[-1:] or [""]
        print(f"{' '.join(command[:3])}: {'OK' if result.returncode == 0 else 'ОШИБКА'} {tail[0]}")
        if result.returncode != 0:
            print((result.stdout + result.stderr)[-3000:])
            raise SystemExit("Проверки в открытой копии не прошли — коммит не сделан")


def _lines(path: Path) -> list[str]:
    text = path.read_text(encoding="utf-8")
    return [line.strip() for line in text.splitlines() if line.strip() and not line.startswith("#")]


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, encoding="utf-8"
    )
    if result.returncode != 0:
        raise SystemExit(f"git {' '.join(args)}: {result.stderr.strip()}")
    return result.stdout


if __name__ == "__main__":
    main()
