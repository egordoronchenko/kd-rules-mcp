"""Полнотекстовый поиск `reference_search` по справочникам скиллов сервера (design Р5–Р10).

Индекс — SQLite FTS5 в памяти процесса; перестраивается, когда меняется отпечаток корпуса (пути,
размеры, время изменения файлов). Единица индекса — блок строк: абзац, пункт списка, строка таблицы,
заголовок; ближайший заголовок индексируется вместе с блоком. Запрос — слова, а не синтаксис FTS:
каждое слово становится фразой в кавычках; русские слова ищутся префиксом основы после отсечения
окончания, идентификаторы 1С (`ПослеЗагрузки`) — точно, `ё` и `е` не различаются.
"""

import re
import sqlite3
import threading
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from kd_rules_mcp.reference import normalize, our_skills

# Блок кода длиннее этого режется на части, чтобы фрагмент оставался коротким.
MAX_BLOCK_LINES = 40
SNIPPET_BEFORE = 60
SNIPPET_LENGTH = 220
# Слово запроса: буквы и цифры; `.`, `_`, `:`, `-` внутри идентификатора оставляют его одним словом.
_WORD = re.compile(r"\w+(?:[.:\-]\w+)*")
_TOKEN = re.compile(r"[^\W_]+")
_LIST_ITEM = re.compile(r"^\s*(?:[-*+]|\d+\.)\s")
_HEADING = re.compile(r"#{1,6}\s")
_FENCE = re.compile(r"\s*(`{3,}|~{3,})(\w*)")
_CYRILLIC = re.compile(r"[а-я]+\Z")
_LATIN = re.compile(r"[a-z]+\Z")
# Идентификатор 1С: заглавная буква внутри слова (`ПослеЗагрузки`, `ОбменДаннымиСервер`).
_CAMEL = re.compile(r"[а-яёa-z][А-ЯЁA-Z]")
# Латинские двойники кириллических букв — в слове, где есть кириллица.
_LOOKALIKE = str.maketrans("aceopxyk", "асеорхук")
MIN_STEM = 4
# Служебные слова не участвуют в поиске: они есть почти в каждом блоке и сужают «все слова».
_STOP_TEXT = "не и в во на для при по с со у к о об от до из за что как то же или а но ли бы это"
STOP_WORDS = frozenset(_STOP_TEXT.split())
# Окончания русских слов (после `ё` → `е`): отсекается самое длинное, основа — не короче MIN_STEM.
_ENDINGS_TEXT = """
ированием ирования ированный ирование ировать ируется ируются ировал ирует
ением ениям ениях ения ение ании ание ания остями ость ости
ились ались ялись ился ался ется ются ится ятся ться тся ишь ешь ите ете ала ило
ыми ими ами ями ого его ому ему ых их ым им ой ей ый ий ая яя ое ее ые ие ов ев ах ях ом ем ам ям
ую юю ью ок ек ть ся сь а я ы и е у ю о ь й
"""
ENDINGS: tuple[str, ...] = tuple(sorted(set(_ENDINGS_TEXT.split()), key=lambda e: (-len(e), e)))


@dataclass(frozen=True)
class Block:
    file: str
    line: int
    heading: str
    text: str


@dataclass(frozen=True)
class Term:
    """Слово запроса: выражение FTS5 и шаблон для отметки в фрагменте."""

    word: str
    fts: str
    pattern: re.Pattern[str]


def corpus_files(root: Path) -> list[Path]:
    """`SKILL.md` и справочники скиллов сервера, по пути."""
    return sorted(path for skill in our_skills(root) for path in skill.rglob("*.md"))


def fingerprint(root: Path, files: list[Path]) -> tuple[tuple[str, int, int], ...]:
    stats = [(path, path.stat()) for path in files]
    return tuple((p.relative_to(root).as_posix(), s.st_size, s.st_mtime_ns) for p, s in stats)


def blocks(name: str, lines: list[str]) -> Iterator[Block]:
    """Блоки файла: заголовок, строка таблицы, пункт списка с продолжением, абзац, кусок кода.

    Примеры ответов JSON (```json) не индексируются: это образцы инструментов, а не знания, и
    они перебивают настоящие справочники по словам из примера.
    """
    heading = ""
    current: list[str] = []
    start = 0
    fence = ""  # открывающая ограда блока кода; пусто — вне блока
    skip = False

    def flush() -> Iterator[Block]:
        if current:
            if not skip:
                yield Block(name, start + 1, heading, "\n".join(current))
            current.clear()

    for index, line in enumerate(lines):
        mark = _FENCE.match(line)
        if fence:
            ticks = mark.group(1) if mark is not None else ""
            if ticks[:1] == fence[0] and len(ticks) >= len(fence) and mark and not mark.group(2):
                current.append(line)
                yield from flush()
                fence, skip = "", False
                continue
            if len(current) >= MAX_BLOCK_LINES:
                yield from flush()
                start = index
            current.append(line)
            continue
        if mark is not None:
            yield from flush()
            fence, skip, start = mark.group(1), mark.group(2).lower() == "json", index
            current.append(line)
            continue
        if not line.strip():
            yield from flush()
            continue
        is_heading = _HEADING.match(line) is not None
        if is_heading or line.startswith("|") or _LIST_ITEM.match(line) or not current:
            yield from flush()
            start = index
        if is_heading:
            heading = line.lstrip("#").strip()
            yield Block(name, index + 1, heading, line)
            continue
        current.append(line)
        if line.startswith("|"):
            yield from flush()
    yield from flush()


def stem(word: str) -> str:
    """Основа русского слова: отсечено самое длинное окончание, основа не короче MIN_STEM."""
    for ending in ENDINGS:
        if word.endswith(ending) and len(word) - len(ending) >= MIN_STEM:
            return word[: -len(ending)]
    return word


def _phrase(text: str) -> str:
    return '"' + text.replace('"', '""') + '"'


def _word_terms(query: str) -> list[tuple[str, str]]:
    """Слова запроса (как в запросе, нормализованное) без повторов."""
    found: dict[str, str] = {}
    for word in _WORD.findall(query):
        norm = normalize(word)
        if re.search(r"[а-я]", norm) and re.search(r"[a-z]", norm):
            norm = norm.translate(_LOOKALIKE)
        if _TOKEN.search(norm) and norm not in found:
            found[norm] = word
    return [(word, norm) for norm, word in found.items()]


def terms(query: str) -> tuple[list[Term], list[str]]:
    """Слова запроса без синтаксиса FTS и служебные слова, которые не искались.

    Кавычки, скобки и `*` отбрасываются; `AND`/`OR`/`NOT`/`NEAR` ищутся как обычные слова (в
    кавычках они не операторы). Запрос только из служебных слов ищется ими.
    """
    words = _word_terms(query)
    meaningful = [(word, norm) for word, norm in words if norm not in STOP_WORDS]
    ignored = [word for word, norm in words if norm in STOP_WORDS] if meaningful else []
    found: list[Term] = []
    for word, norm in meaningful or words:
        tokens = _TOKEN.findall(norm)
        if len(tokens) > 1:  # идентификатор: фраза из частей подряд
            fts = _phrase(" ".join(tokens))
            pattern = r"(?<![^\W_])" + r"[\W_]+".join(map(re.escape, tokens)) + r"(?![^\W_])"
        elif _CAMEL.search(word) or not (_CYRILLIC.match(norm) or _LATIN.match(norm)):
            # идентификатор 1С, число или смесь букв и цифр — точно
            fts = _phrase(norm)
            pattern = r"(?<![^\W_])" + re.escape(norm) + r"(?![^\W_])"
        elif _CYRILLIC.match(norm) and len(norm) > MIN_STEM:
            base = stem(norm)
            fts = _phrase(base) + "*"
            pattern = r"(?<![^\W_])" + re.escape(base) + r"[^\W_]*"
        elif len(norm) >= 3:
            fts = _phrase(norm) + "*"
            pattern = r"(?<![^\W_])" + re.escape(norm) + r"[^\W_]*"
        else:  # короткие слова — точно
            fts = _phrase(norm)
            pattern = r"(?<![^\W_])" + re.escape(norm) + r"(?![^\W_])"
        found.append(Term(word, fts, re.compile(pattern)))
    return found, ignored


def snippet(text: str, patterns: list[re.Pattern[str]]) -> str:
    """Фрагмент вокруг самого длинного совпавшего слова; совпадения в `[ ]`, пробелы схлопнуты."""
    flat = " ".join(text.split())
    norm = normalize(flat)
    spans = sorted(
        {(m.start(), m.end()) for pattern in patterns for m in pattern.finditer(norm) if m.end()}
    )
    anchor = min(spans, key=lambda span: (span[0] - span[1], span[0]))[0] if spans else 0
    begin = max(0, anchor - SNIPPET_BEFORE)
    end = min(len(flat), begin + SNIPPET_LENGTH)
    if begin > 0:
        space = flat.find(" ", begin)
        begin = space + 1 if 0 <= space < anchor else begin
    out: list[str] = ["…" if begin > 0 else ""]
    position = begin
    for low, high in spans:
        if low < position or high > end:
            continue
        out += [flat[position:low], "[", flat[low:high], "]"]
        position = high
    out.append(flat[position:end])
    if end < len(flat):
        out.append("…")
    return "".join(out)


class ReferenceIndex:
    """Индекс FTS5 в памяти; один на сервис, вызовы — под блокировкой."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._key: tuple[object, ...] | None = None
        self._db: sqlite3.Connection | None = None
        self._blocks: list[Block] = []

    def _ensure(self, root: Path) -> sqlite3.Connection:
        files = corpus_files(root)
        key = (str(root), fingerprint(root, files))
        if self._db is not None and key == self._key:
            return self._db
        found: list[Block] = []
        for path in files:
            name = path.relative_to(root).as_posix()
            text = path.read_text(encoding="utf-8", errors="replace")
            found += blocks(name, text.splitlines())
        db = sqlite3.connect(":memory:", check_same_thread=False)
        try:
            db.execute(
                "CREATE VIRTUAL TABLE blocks USING fts5(body, heading, tokenize='unicode61')"
            )
            db.executemany(
                "INSERT INTO blocks(rowid, body, heading) VALUES (?, ?, ?)",
                [(i, normalize(b.text), normalize(b.heading)) for i, b in enumerate(found)],
            )
        except BaseException:
            db.close()
            raise
        if self._db is not None:
            self._db.close()
        self._db, self._key, self._blocks = db, key, found
        return db

    def search(
        self, root: Path, query: str, offset: int, limit: int, skill: str | None = None
    ) -> dict[str, Any]:
        """Страница совпадений: по числу совпавших слов, затем bm25, затем файл и строка.

        Блоки со всеми словами идут первыми (`complete` — сколько их), за ними — с частью слов:
        без этого фрагмент с частью слов терялся, если где-то нашлись все. `skill` — начало имени
        каталога скилла (`kd2` — скиллы КД 2, `kd3-rules`); ненайденные слова считаются в нём же.
        """
        words, ignored = terms(query)
        with self._lock:
            db = self._ensure(root)

            def match(expression: str) -> list[int]:
                found = self._match(db, expression)
                if skill:
                    found = [r for r in found if self._blocks[r].file.startswith(skill)]
                return found

            sets = [set(match(term.fts)) for term in words]
            order = match(" OR ".join(term.fts for term in words))
            matched = {rowid: sum(rowid in found for found in sets) for rowid in order}
            rank = {rowid: place for place, rowid in enumerate(order)}
            rows = sorted(order, key=lambda rowid: (-matched[rowid], rank[rowid]))
            patterns = [term.pattern for term in words]
            page = rows[offset : offset + limit]
            hits = [
                {
                    "file": self._blocks[rowid].file,
                    "heading": self._blocks[rowid].heading,
                    "line": self._blocks[rowid].line,
                    "matched": matched[rowid],
                    "snippet": snippet(self._blocks[rowid].text, patterns),
                }
                for rowid in page
            ]
            return {
                "terms": [term.fts for term in words],
                "complete": sum(count == len(words) for count in matched.values()),
                "missing_terms": [
                    t.word for t, found in zip(words, sets, strict=True) if not found
                ],
                "ignored_terms": ignored,
                "items": hits,
                "total": len(rows),
            }

    @staticmethod
    def _match(db: sqlite3.Connection, expression: str) -> list[int]:
        cursor = db.execute(
            "SELECT rowid FROM blocks WHERE blocks MATCH ? ORDER BY bm25(blocks), rowid",
            (expression,),
        )
        return [row[0] for row in cursor]
