"""Лексические границы BSL без выполнения кода и поиска форм по всему файлу."""

import hashlib
from collections import OrderedDict
from dataclasses import dataclass

from .errors import EdFormatError
from .model import SourceFile, SourceSpan, SourceTag


@dataclass(frozen=True, slots=True)
class Token:
    kind: str
    value: str
    start: int
    end: int

    @property
    def folded(self) -> str:
        return self.value.casefold()


@dataclass(frozen=True, slots=True)
class Statement:
    tokens: tuple[Token, ...]
    span: SourceSpan
    raw_text: str
    regions: tuple[str, ...] = ()
    tag_ids: tuple[str, ...] = ()

    @property
    def head(self) -> str:
        return self.tokens[0].folded if self.tokens[0].kind == "identifier" else ""


@dataclass(frozen=True, slots=True)
class Lexed:
    tokens: tuple[Token, ...]
    statements: tuple[Statement, ...]
    warnings: tuple[tuple[str, SourceSpan], ...]
    tags: tuple[SourceTag, ...] = ()


# Один и тот же файл конфигурации разбирается заново для каждого расширения.
# Ключ — идентификатор и хеш самого текста: объявленный sha256 иногда не обновляют,
# когда подменяют текст, а позиции в разборе зависят от файла.
_LEX_CACHE: OrderedDict[tuple[str, str], Lexed] = OrderedDict()
_LEX_CACHE_MAX = 16


def tokenize(text: str) -> tuple[Token, ...]:
    """Выделяет токены; комментарии и директивы не смешиваются со строками."""
    result: list[Token] = []
    i = 0
    size = len(text)
    while i < size:
        char = text[i]
        if char.isspace():
            i += 1
            continue
        start = i
        if text.startswith("//", i) or char in "#&":
            end = text.find("\n", i)
            i = size if end < 0 else end
            kind = "comment" if char == "/" else "directive"
            result.append(Token(kind, text[start:i].rstrip("\r"), start, i))
        elif char == '"':
            i += 1
            value: list[str] = []
            while i < size:
                if text[i] == '"':
                    if i + 1 < size and text[i + 1] == '"':
                        value.append('"')
                        i += 2
                        continue
                    i += 1
                    break
                if text[i] in "\r\n":
                    if text.startswith("\r\n", i):
                        i += 1
                    i += 1
                    while i < size and text[i] in " \t":
                        i += 1
                    # BSL допускает строки комментариев между продолжениями литерала.
                    while text.startswith("//", i):
                        end = text.find("\n", i)
                        i = size if end < 0 else end + 1
                        while i < size and text[i] in " \t":
                            i += 1
                    if i >= size or text[i] != "|":
                        raise EdFormatError(f"Нет маркера продолжения BSL, позиция {i}")
                    value.append("\n")
                    i += 1
                else:
                    value.append(text[i])
                    i += 1
            else:
                raise EdFormatError("Незакрытая строка BSL")
            result.append(Token("string", "".join(value), start, i))
        elif char.isalpha() or char == "_":
            i += 1
            while i < size and (text[i].isalnum() or text[i] == "_"):
                i += 1
            result.append(Token("identifier", text[start:i], start, i))
        elif char.isdigit():
            i += 1
            while i < size and (text[i].isdigit() or text[i] == "."):
                i += 1
            result.append(Token("number", text[start:i], start, i))
        elif char == "'":
            i = text.find("'", start + 1)
            if i < 0:
                raise EdFormatError("Незакрытый литерал даты BSL")
            i += 1
            result.append(Token("date", text[start + 1 : i - 1], start, i))
        else:
            i += 2 if text[i : i + 2] in ("<=", ">=", "<>") else 1
            result.append(Token("symbol", text[start:i], start, i))
    return tuple(result)


def split_arguments(tokens: tuple[Token, ...]) -> tuple[tuple[Token, ...], ...]:
    """Аргументы без внешних скобок; пустые позиции не теряются."""
    if not tokens:
        return ()
    parts: list[tuple[Token, ...]] = []
    stack: list[str] = []
    start = 0
    for i, token in enumerate(tokens):
        if token.kind != "symbol":
            continue
        if token.value in ("(", "["):
            stack.append(token.value)
        elif token.value in (")", "]"):
            expected = "(" if token.value == ")" else "["
            if not stack or stack.pop() != expected:
                raise EdFormatError("Нарушена вложенность аргументов")
        elif token.value == "," and not stack:
            parts.append(tokens[start:i])
            start = i + 1
    if stack:
        raise EdFormatError("Незакрытые скобки аргументов")
    parts.append(tokens[start:])
    return tuple(parts)


def lex(source: SourceFile) -> Lexed:
    """Разделяет токены на операторы и структурные заголовки, сохраняя контекст."""
    key = (source.file_id, hashlib.sha256(source.text.encode()).hexdigest())
    cached = _LEX_CACHE.get(key)
    if cached is not None:
        _LEX_CACHE.move_to_end(key)
        return cached
    parsed = _lex(source)
    _LEX_CACHE[key] = parsed
    if len(_LEX_CACHE) > _LEX_CACHE_MAX:
        _LEX_CACHE.popitem(last=False)
    return parsed


def _lex(source: SourceFile) -> Lexed:
    all_tokens = tokenize(source.text)
    tokens: list[Token] = []
    contexts: list[tuple[tuple[str, ...], tuple[str, ...]]] = []
    regions: list[str] = []
    tags: list[str] = []
    tag_starts: list[int] = []
    tag_spans: list[SourceTag] = []
    warnings: list[tuple[str, SourceSpan]] = []
    for token in all_tokens:
        if token.kind == "comment":
            if token.value.startswith("//++ "):
                tags.append(token.value[5:].strip())
                tag_starts.append(token.start)
            elif token.value.startswith("//-- "):
                name = token.value[5:].strip()
                if not tags or tags[-1] != name:
                    warnings.append(("unbalanced_tag", source.span(token.start, token.end)))
                else:
                    tags.pop()
                    start = tag_starts.pop()
                    tag_spans.append(
                        SourceTag(
                            name, source.span(start, token.end), source.text[start : token.end]
                        )
                    )
            continue
        tokens.append(token)
        contexts.append((tuple(regions), tuple(tags)))
        if token.kind == "directive":
            words = token.value.split()
            if words[0].casefold() == "#область":
                regions.append(" ".join(words[1:]))
            elif words[0].casefold() == "#конецобласти":
                if not regions:
                    warnings.append(("unbalanced_region", source.span(token.start, token.end)))
                else:
                    regions.pop()
    if regions or tags:
        warnings.append(("unclosed_context", source.span(0, len(source.text))))
    for name, start in zip(tags, tag_starts, strict=True):
        tag_spans.append(SourceTag(name, source.span(start, len(source.text)), source.text[start:]))
    result: list[Statement] = []
    i = 0
    singles = {"иначе", "конецесли", "конеццикла", "попытка", "исключение", "конецпопытки"}
    endings = {"конецпроцедуры", "конецфункции"}
    headers = {"процедура", "функция"}
    while i < len(tokens):
        start = i
        first = tokens[i]
        head = first.folded if first.kind == "identifier" else ""
        if first.kind == "directive" or head in singles | endings:
            i += 1
            if i < len(tokens) and tokens[i].value == ";":
                i += 1
        else:
            stack: list[str] = []
            opened = False
            while i < len(tokens):
                token = tokens[i]
                if i > start and token.kind == "directive":
                    break
                if token.kind == "symbol":
                    if token.value in ("(", "["):
                        stack.append(token.value)
                        opened = True
                    elif token.value in (")", "]"):
                        expected = "(" if token.value == ")" else "["
                        if not stack or stack.pop() != expected:
                            raise EdFormatError("Несогласованные скобки BSL")
                    elif token.value == ";" and not stack:
                        i += 1
                        break
                if not stack:
                    if head in headers and opened:
                        i += 1
                        if i < len(tokens) and tokens[i].folded == "экспорт":
                            i += 1
                        break
                    if token.kind == "identifier":
                        if head in ("если", "иначеесли") and token.folded == "тогда":
                            i += 1
                            break
                        if head in ("для", "пока") and token.folded == "цикл":
                            i += 1
                            break
                        if i > start and token.folded in endings | headers | singles | {
                            "иначеесли"
                        }:
                            break
                i += 1
            if stack:
                raise EdFormatError("Незакрытые скобки BSL")
        if i == start:
            raise EdFormatError("Не удалось выделить оператор")
        span = source.span(tokens[start].start, tokens[i - 1].end)
        region_path, tag_path = contexts[start]
        result.append(
            Statement(
                tuple(tokens[start:i]),
                span,
                source.text[span.char_start : span.char_end],
                region_path,
                tag_path,
            )
        )
    return Lexed(
        all_tokens,
        tuple(result),
        tuple(warnings),
        tuple(sorted(tag_spans, key=lambda tag: tag.span.char_start)),
    )


def normalized(tokens: tuple[Token, ...]) -> tuple[str, ...]:
    """Сравнение формы по токенам с сохранением регистра строковых литералов."""
    return tuple('"' + t.value if t.kind == "string" else t.folded for t in tokens)
