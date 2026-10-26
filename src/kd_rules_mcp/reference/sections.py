"""Разделы справки `reference_get`: адреса заголовков в файлах скиллов, а не текст.

Раздел — файл скилла и заголовки `##` в нём (пусто — весь файл). Текст читается при вызове, поэтому
правка скилла видна сразу. Ключ выбирает строки таблиц раздела по первой ячейке (design Р3).
"""

import re
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from kd_rules_mcp.reference import normalize
from kd_rules_mcp.reference.search import STOP_WORDS, stem


@dataclass(frozen=True)
class Section:
    """Адрес раздела: файл относительно папки скиллов и заголовки `##` в нём."""

    id: str
    file: str
    headings: tuple[str, ...] = ()
    # Первая ячейка таблиц — коды протокола: числовой ключ сравнивается с ними точно.
    numeric_key: bool = False
    # Инструмент, у которого точные данные раздела (не дублируются в справке).
    see: str | None = None
    # Сколько первых ячеек строки сравнивается с ключом (в фазах вторая — вид правила).
    key_cells: int = 1


SECTIONS: tuple[Section, ...] = (
    Section("protocol", "kd2-exchange-pitfalls/references/protocol.md", numeric_key=True),
    Section("symptoms", "kd2-exchange-pitfalls/references/symptoms.md"),
    Section(
        "phases",
        "kd2-rules-build/references/handlers.md",
        ("Где и когда вызывается", "Исключение в обработчике"),
        key_cells=2,
    ),
    Section(
        "handler_params",
        "kd2-rules-build/references/handlers.md",
        ("Переменные, доступные везде", "Ключевые переменные по фазам"),
        see="handlers_context",
    ),
)
SECTIONS_BY_ID = {section.id: section for section in SECTIONS}

_CELL_SPLIT = re.compile(r"(?<!\\)\|")
_SEPARATOR = re.compile(r"^\|(?:\s*:?-{3,}:?\s*\|)+\s*$")
_CODES = re.compile(r"([0-9]+)(?:\s*[–-]\s*([0-9]+))?")
_NUMERIC_KEY = re.compile(r"\D*?([0-9]+)\D*")
_KEY_NOISE = str.maketrans("", "", '`«»"*')
_KEY_WORD = re.compile(r"[^\W_]+")


def read_lines(root: Path, section: Section) -> list[str]:
    return (root / section.file).read_text(encoding="utf-8").splitlines()


def file_title(lines: list[str]) -> str:
    """Первый заголовок `# …` файла."""
    return next((line[2:].strip() for line in lines if line.startswith("# ")), "")


def file_headings(lines: list[str]) -> list[str]:
    return [line[3:].strip() for line in lines if line.startswith("## ")]


def spans(lines: list[str], section: Section) -> list[tuple[int, int]]:
    """Диапазоны строк раздела `[начало, конец)`; заголовка нет — `KeyError` с его именем."""
    if not section.headings:
        return [(0, len(lines))]
    found: list[tuple[int, int]] = []
    for heading in section.headings:
        start = next((i for i, line in enumerate(lines) if line == f"## {heading}"), None)
        if start is None:
            raise KeyError(heading)
        end = next(
            (j for j in range(start + 1, len(lines)) if lines[j].startswith(("# ", "## "))),
            len(lines),
        )
        found.append((start, end))
    return found


def numbered(lines: list[str], section: Section) -> list[tuple[int, str]]:
    """Строки раздела с номерами строк файла (с 1)."""
    return [(i + 1, lines[i]) for start, end in spans(lines, section) for i in range(start, end)]


def parts(rows: list[tuple[int, str]]) -> list[dict[str, object]]:
    """Подряд идущие строки — одним текстом с номером первой строки."""
    result: list[dict[str, object]] = []
    for line, text in rows:
        if result and result[-1]["_next"] == line:
            result[-1]["text"] = f"{result[-1]['text']}\n{text}"
            result[-1]["_next"] = line + 1
        else:
            result.append({"line": line, "text": text, "_next": line + 1})
    for part in result:
        del part["_next"]
    return result


def _key_cells(row: str, count: int) -> str:
    cells = _CELL_SPLIT.split(row)
    return " ".join(cells[1 : 1 + count]) if len(cells) > 2 else ""


def table_rows(rows: list[tuple[int, str]]) -> Iterator[tuple[tuple[int, str], int, str]]:
    """Строки данных таблиц: (заголовок таблицы, номер строки, текст), без разделителей."""
    header: tuple[int, str] | None = None
    for index, (line, text) in enumerate(rows):
        if not text.startswith("|"):
            header = None
            continue
        if _SEPARATOR.match(text):
            continue
        following = rows[index + 1][1] if index + 1 < len(rows) else ""
        if _SEPARATOR.match(following):
            header = (line, text)
            continue
        if header is not None:
            yield header, line, text


def codes(cell: str) -> set[int]:
    """Коды первой ячейки таблицы протокола: числа, перечисления, диапазоны `31–34`."""
    found: set[int] = set()
    for low, high in _CODES.findall(cell):
        first = int(low)
        last = int(high) if high else first
        if last < first or last - first > 1000:
            last = first
        found.update(range(first, last + 1))
    return found


def key_text(text: str) -> str:
    return " ".join(normalize(text).translate(_KEY_NOISE).split())


def key_words(key: str) -> list[str]:
    """Слова ключа без служебных; ключ только из служебных — как есть."""
    words = _KEY_WORD.findall(key_text(key))
    return [w for w in words if w not in STOP_WORDS] or words


def matches_key(section: Section, cells: str, key: str, *, exact: bool) -> bool:
    """Код протокола — точно; слова ключа — целыми словами ячейки (`exact`) или началом основы."""
    code = _NUMERIC_KEY.fullmatch(key) if section.numeric_key else None
    if code is not None:
        return int(code.group(1)) in codes(cells)
    words = key_words(key)
    tokens = _KEY_WORD.findall(key_text(cells))
    if not words:
        return False
    if exact:
        return all(word in tokens for word in words)
    return all(any(token.startswith(stem(word)) for token in tokens) for word in words)


def rows_by_key(
    rows: list[tuple[int, str]], section: Section, key: str
) -> list[tuple[str, int, str]]:
    """Строки таблиц раздела по ключу: (заголовок таблицы, строка, текст).

    Сначала — строки, где все слова ключа стоят целыми словами (`ПослеЗагрузки` не находит
    `ПослеЗагрузкиОбъекта`); нет таких — где слова ключа начинают слова ячейки по основе
    (`задвоение` находит «Задвоились»).
    """
    data = [(header, line, text) for (_, header), line, text in table_rows(rows)]
    for exact in (True, False):
        found = [
            (header, line, text)
            for header, line, text in data
            if matches_key(section, _key_cells(text, section.key_cells), key, exact=exact)
        ]
        if found or (section.numeric_key and _NUMERIC_KEY.fullmatch(key)):
            return found
    return []


def group_rows(found: list[tuple[str, int, str]]) -> list[dict[str, object]]:
    """Строки под заголовками своих таблиц, в порядке файла."""
    tables: list[dict[str, object]] = []
    for header, line, text in found:
        if not tables or tables[-1]["header"] != header:
            tables.append({"header": header, "rows": []})
        rows = tables[-1]["rows"]
        assert isinstance(rows, list)
        rows.append({"line": line, "text": text})
    return tables
