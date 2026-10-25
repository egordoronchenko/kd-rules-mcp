"""Лексика BSL обработчиков для проверок: литералы, комментарии, начало оператора.

Один сканер на все проверки по коду обработчиков и на граф вызовов. Литерал продолжается на
следующих строках до закрывающей кавычки (`""` — символ литерала); строка, которая между строками
литерала начинается с `//`, — комментарий и литерал не закрывает; `//` внутри литерала — текст.
Незакрытый литерал идёт до конца текста: это синтаксическая ошибка, её находит синтакс-чекер.

Представления `strip_comments` и `bsl_code` той же длины, что исходный текст, с теми же
переводами строк: позиция в одном — позиция в другом и в исходнике.
"""

import re
from bisect import bisect_right
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from functools import lru_cache

# Слова, после которых начинается новый оператор (русские и английские).
STATEMENT_KEYWORDS = frozenset(
    {"тогда", "иначе", "цикл", "попытка", "исключение", "конецесли", "конеццикла"}
    | {"конецпопытки", "then", "else", "do", "try", "except", "endif", "enddo", "endtry"}
)

_OUTSIDE = re.compile(r'"|//')
_INSIDE = re.compile(r'"|\n')
_NOT_NEWLINE = re.compile(r"[^\n]")
_CACHE = 4096


@dataclass(frozen=True, slots=True)
class Literal:
    """Литерал: `start` — открывающая кавычка, `end` — позиция за закрывающей (или конец текста).

    `cuts` — строки-комментарии внутри литерала: перевод строки перед комментарием, начало `//`
    и конец строки комментария.
    """

    start: int
    end: int
    closed: bool
    cuts: tuple[tuple[int, int, int], ...] = ()


@dataclass(frozen=True, slots=True)
class _Scan:
    literals: tuple[Literal, ...]
    comments: tuple[tuple[int, int], ...]
    starts: tuple[int, ...]


def read_literal(text: str, start: int) -> Literal:
    """Литерал, который открывает кавычка в позиции `start`."""
    index = start + 1
    cuts: list[tuple[int, int, int]] = []
    while (found := _INSIDE.search(text, index)) is not None:
        at = found.start()
        if found.group() == '"':
            if text.startswith('""', at):
                index = at + 2
                continue
            return Literal(start, at + 1, True, tuple(cuts))
        after = at + 1
        while after < len(text) and text[after] in " \t\r":
            after += 1
        if text.startswith("//", after):
            end = _line_end(text, after)
            cuts.append((at, after, end))
            index = end
            continue
        index = at + 1
    return Literal(start, len(text), False, tuple(cuts))


def literal_value(text: str, literal: Literal) -> str:
    """Содержимое литерала без кавычек, без строк-комментариев, `""` → `"`; `|` остаются."""
    inner_end = literal.end - 1 if literal.closed else literal.end
    parts: list[str] = []
    position = literal.start + 1
    for newline, _comment, end in literal.cuts:
        parts.append(text[position:newline])
        position = end
    parts.append(text[position:inner_end])
    return "".join(parts).replace('""', '"')


@lru_cache(maxsize=_CACHE)
def _scan(text: str) -> _Scan:
    literals: list[Literal] = []
    comments: list[tuple[int, int]] = []
    index = 0
    while (found := _OUTSIDE.search(text, index)) is not None:
        start = found.start()
        if found.group() == "//":
            index = _line_end(text, start)
            comments.append((start, index))
            continue
        literal = read_literal(text, start)
        literals.append(literal)
        comments.extend((comment, end) for _newline, comment, end in literal.cuts)
        index = literal.end
    return _Scan(tuple(literals), tuple(comments), tuple(item.start for item in literals))


def _line_end(text: str, index: int) -> int:
    end = text.find("\n", index)
    return len(text) if end < 0 else end


def _blank(text: str, spans: list[tuple[int, int]]) -> str:
    """Интервалы заменены пробелами, переводы строк на месте."""
    parts: list[str] = []
    position = 0
    for start, end in sorted(spans):
        if end <= position:
            continue
        start = max(start, position)
        parts.append(text[position:start])
        parts.append(_NOT_NEWLINE.sub(" ", text[start:end]))
        position = end
    parts.append(text[position:])
    return "".join(parts)


@lru_cache(maxsize=_CACHE)
def strip_comments(text: str) -> str:
    """Текст с комментариями, заменёнными пробелами; литералы целы, длина та же."""
    return _blank(text, list(_scan(text).comments))


@lru_cache(maxsize=_CACHE)
def bsl_code(text: str) -> str:
    """Только код: комментарии и содержимое литералов — пробелы; кавычки и строки на месте."""
    scan = _scan(text)
    spans = list(scan.comments)
    for literal in scan.literals:
        spans.append((literal.start + 1, literal.end - 1 if literal.closed else literal.end))
    return _blank(text, spans)


def literals(text: str) -> list[str]:
    """Значения литералов текста по порядку (`literal_value`)."""
    return [literal_value(text, literal) for literal in _scan(text).literals]


def in_literal(text: str, index: int) -> bool:
    """Позиция внутри литерала, между его кавычками."""
    scan = _scan(text)
    position = bisect_right(scan.starts, index) - 1
    if position < 0:
        return False
    literal = scan.literals[position]
    return literal.start < index and (index < literal.end - 1 or not literal.closed)


def code_matches(pattern: re.Pattern[str], text: str) -> Iterator[re.Match[str]]:
    """Совпадения шаблона в тексте без комментариев, начатые в коде, а не внутри литерала.

    Совпадение, начатое в литерале, отбрасывается, и поиск продолжается со следующей позиции:
    ложное совпадение не прячет настоящее. Литерал-ключ внутри шаблона читается как есть.
    """
    return _matches(pattern, text, statements=False)


def statement_matches(pattern: re.Pattern[str], text: str) -> Iterator[re.Match[str]]:
    """`code_matches`, начатые в начале оператора: присваивание, а не сравнение.

    Отвергнутое сравнение не съедает остаток строки: `Если Х = 1 Тогда Х = 2;` находит второе.
    """
    return _matches(pattern, text, statements=True)


def _matches(pattern: re.Pattern[str], text: str, *, statements: bool) -> Iterator[re.Match[str]]:
    kept = strip_comments(text)
    code = bsl_code(text)
    index = 0
    while index <= len(kept) and (match := pattern.search(kept, index)) is not None:
        start = match.start()
        if in_literal(text, start) or (statements and not statement_start(code, start)):
            index = start + 1
            continue
        yield match
        index = max(match.end(), start + 1)


def join_code(parts: Iterable[str]) -> str:
    """Тексты одним кодом: между ними `;` на своей строке.

    Часть может кончаться без `;` (`Выполнить(Алгоритмы.А)`) или комментарием; без разделителя
    первый оператор следующей части не начинал бы оператор.
    """
    return "\n;\n".join(parts)


def previous_char(code: str, index: int) -> int | None:
    """Позиция последнего непробельного символа перед `index`."""
    cursor = index - 1
    while cursor >= 0 and code[cursor] in " \t\r\n":
        cursor -= 1
    return cursor if cursor >= 0 else None


def statement_start(code: str, index: int) -> bool:
    """Имя в `index` начинает оператор: начало текста, после `;`, метки, ключевого слова или
    строки препроцессора `#…`. После `(`, `,`, `=`, операции и другого слова — выражение.

    `code` — текст без комментариев (`strip_comments` или `bsl_code`).
    """
    before = previous_char(code, index)
    if before is None or code[before] in ";:":
        return True
    line_start = code.rfind("\n", 0, before + 1) + 1
    if code[line_start : before + 1].lstrip().startswith("#"):
        return True
    if not (code[before].isalnum() or code[before] == "_"):
        return False
    start = before
    while start > 0 and (code[start - 1].isalnum() or code[start - 1] == "_"):
        start -= 1
    return code[start : before + 1].casefold() in STATEMENT_KEYWORDS


def string_end(text: str, start: int) -> int:
    """Позиция за литералом, который открывает кавычка в `start` (`read_literal`)."""
    return read_literal(text, start).end
