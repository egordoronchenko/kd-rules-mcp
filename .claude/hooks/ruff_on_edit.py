"""Хук после правки файла агентом: форматирует .py-файл ruff и проверяет линтером.

Подключён в `.claude/settings.json` (PostToolUse). Cursor CLI читает хуки оттуда же (проверено
26.09.2026 на CLI 2026.09.10), поэтому отдельного `.cursor/hooks.json` нет — иначе ruff
запускался бы дважды. Скрипт сам определяет, кто его вызвал:

- Claude Code: оставшиеся замечания уходят в stderr с кодом выхода 2 — правка считается
  незавершённой, пока файл не станет чистым;
- Cursor (во входе есть `cursor_version`): замечания возвращаются JSON-полем `additional_context`
  с кодом 0 — другого канала обратной связи у `postToolUse` Cursor нет. Вход Cursor на Windows
  передаёт через PowerShell, поэтому он может начинаться с BOM.
"""

import io
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path


def edited_file(payload: dict) -> Path | None:
    """Путь правленого файла из входа хука (Claude и Cursor кладут его в tool_input.file_path)."""
    tool_input = payload.get("tool_input") or {}
    if isinstance(tool_input, str):
        try:
            tool_input = json.loads(tool_input)
        except json.JSONDecodeError:
            tool_input = {}
    file_path = tool_input.get("file_path") or tool_input.get("path") or payload.get("file_path")
    return Path(file_path) if file_path else None


def ruff_problems(path: Path) -> str | None:
    """Форматирует файл, исправляет что можно; возвращает оставшиеся замечания или None."""
    ruff = shutil.which("ruff")
    if ruff is None:
        return "ruff не найден в PATH: uv tool install ruff"
    # Без цветов: Cursor задаёт FORCE_COLOR, и ANSI-коды попадали бы в замечания агенту.
    env = {k: v for k, v in os.environ.items() if k not in ("FORCE_COLOR", "CLICOLOR_FORCE")}
    env["NO_COLOR"] = "1"
    subprocess.run([ruff, "format", "--quiet", str(path)], check=False, env=env)
    check = subprocess.run(
        [ruff, "check", "--fix", "--quiet", str(path)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
        env=env,
    )
    if check.returncode != 0:
        return f"ruff check: {path}\n{check.stdout}{check.stderr}"
    return None


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        if isinstance(stream, io.TextIOWrapper):
            stream.reconfigure(encoding="utf-8")

    payload = json.loads(sys.stdin.buffer.read().decode("utf-8-sig").strip() or "{}")
    from_cursor = "cursor_version" in payload
    path = edited_file(payload)
    problems = None
    if path is not None and path.suffix == ".py" and path.is_file():
        problems = ruff_problems(path)

    if from_cursor:
        answer = {"additional_context": problems} if problems else {}
        print(json.dumps(answer, ensure_ascii=False))
        return 0
    if problems:
        print(problems, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
