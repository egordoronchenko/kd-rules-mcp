"""Извлекает тело раздела версии CHANGELOG.md для релиза GitHub."""

import argparse
import re
import sys
from pathlib import Path

HEADER = re.compile(r"^## \[([^\]]+)\](?:\s.*)?$")


def changelog_section(text: str, version: str) -> str:
    """Возвращает один непустой раздел без заголовка; неизвестная версия — ошибка."""
    wanted = version.removeprefix("v")
    sections: list[list[str]] = []
    current: list[str] | None = None
    fence: str | None = None
    for line in text.splitlines():
        stripped = line.lstrip()
        if stripped.startswith(("```", "~~~")):
            marker = stripped[0]
            if fence is None:
                fence = marker
            elif fence == marker:
                fence = None
        heading = HEADER.fullmatch(line) if fence is None else None
        if heading:
            current = None
            if heading[1] == wanted:
                current = []
                sections.append(current)
        elif current is not None:
            current.append(line)
    if len(sections) != 1:
        raise ValueError(f"CHANGELOG.md: нужен ровно один раздел [{wanted}]")
    body = "\n".join(sections[0]).strip()
    if not body:
        raise ValueError(f"CHANGELOG.md: раздел [{wanted}] пуст")
    return body + "\n"


def main(argv: list[str] | None = None) -> None:
    """Пишет раздел в stdout либо в файл, который передаётся action-gh-release."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("version", help="версия или тег v<версия>")
    parser.add_argument("--changelog", type=Path, default=Path("CHANGELOG.md"))
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    try:
        body = changelog_section(args.changelog.read_text(encoding="utf-8"), args.version)
    except (OSError, ValueError) as error:
        parser.error(str(error))
    if args.output:
        args.output.write_bytes(body.encode("utf-8"))
    else:
        # В stdout — байтами UTF-8: консоль Windows в cp1251 не кодирует «→» и другие знаки текста.
        sys.stdout.buffer.write(body.encode("utf-8"))
        sys.stdout.flush()


if __name__ == "__main__":
    main()
