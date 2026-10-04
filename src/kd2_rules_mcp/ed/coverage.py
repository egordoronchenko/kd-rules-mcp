"""Полное непересекающееся покрытие текста и консервативная классификация строк."""

from collections.abc import Iterable

from .lexer import Token
from .model import Classification, Coverage, CoverageSegment, SourceFile, SourceSpan

KINDS = tuple(Classification)


def build_coverage(
    source: SourceFile,
    tokens: tuple[Token, ...],
    marks: Iterable[tuple[SourceSpan, Classification]],
    entity_spans: Iterable[SourceSpan],
    *,
    lexical_error: bool = False,
) -> Coverage:
    """Поздние метки уточняют ранние; trivia не становится пониманием BSL."""
    size = len(source.text)
    painted = bytearray([3]) * size
    for span, kind in marks:
        painted[span.char_start : span.char_end] = bytes([KINDS.index(kind)]) * (
            span.char_end - span.char_start
        )
    classes = bytearray([3 if lexical_error else 0]) * size
    for token in tokens:
        if token.kind != "comment":
            classes[token.start : token.end] = painted[token.start : token.end]
    segments: list[CoverageSegment] = []
    start = 0
    for i in range(1, size + 1):
        if i == size or classes[i] != classes[start]:
            segments.append(CoverageSegment(source.span(start, i), KINDS[classes[start]]))
            start = i
    line_classes: list[Classification] = []
    ends = (*source.line_offsets[1:], size)
    for left, right in zip(source.line_offsets, ends, strict=True):
        line_classes.append(KINDS[max(classes[left:right], default=0)])
    covered: set[int] = set()
    for span in entity_spans:
        covered.update(range(span.line_start, span.line_end + 1))
    covered = {line for line in covered if line_classes[line - 1] != Classification.UNKNOWN}
    total = source.lines
    unknown = line_classes.count(Classification.UNKNOWN)
    return Coverage(
        tuple(segments),
        tuple(line_classes),
        len(covered),
        len(covered) / total,
        (total - unknown) / total,
    )
