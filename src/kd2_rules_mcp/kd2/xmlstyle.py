"""Стиль XML-файла правил и запись XML в стиле `ЗаписьXML` платформы 1С.

1С пишет правила в UTF-8 с BOM, с отступом табуляцией на уровень, переводами строк CRLF,
пустые элементы — как `<Тег/>`, а в тексте экранирует только `&`, `<`, `>` (CDATA не использует,
см. дайджест §2.1). Стиль исходного файла запоминается при импорте и повторяется при экспорте.
"""

import difflib
import re
from dataclasses import dataclass

BOM = b"\xef\xbb\xbf"
_DECLARATION = re.compile(rb"^<\?xml[^>]*\?>")
_INDENT = re.compile(rb"\n([ \t]+)<")


@dataclass(frozen=True, slots=True)
class XmlStyle:
    """Оформление файла, не влияющее на смысл правил."""

    bom: bool = True
    declaration: str | None = None
    newline: str = "\r\n"
    indent: str = "\t"
    final_newline: bool = False


# Стиль новых файлов — как у писателя КД 2.1.8.2 (образцы корпуса без объявления XML).
KD_STYLE = XmlStyle()


def detect_style(raw: bytes) -> XmlStyle:
    """Определяет стиль по байтам исходного файла."""
    bom = raw.startswith(BOM)
    body = raw[len(BOM) :] if bom else raw
    match = _DECLARATION.match(body)
    declaration = match.group(0).decode("utf-8") if match else None
    newline = "\r\n" if b"\r\n" in body[:4096] else "\n"
    indent_match = _INDENT.search(body[:4096])
    indent = indent_match.group(1).decode("ascii") if indent_match else "\t"
    return XmlStyle(
        bom=bom,
        declaration=declaration,
        newline=newline,
        indent=indent,
        final_newline=body.endswith(b"\n"),
    )


def escape_text(value: str) -> str:
    """Экранирование текста элемента, как у `ЗаписьXML.ЗаписатьТекст`."""
    value = value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return value.replace("\r", "&#13;")


def escape_attr(value: str) -> str:
    """Экранирование значения атрибута; переводы строк сохраняются ссылками на символы."""
    value = escape_text(value).replace('"', "&quot;")
    return value.replace("\n", "&#10;").replace("\t", "&#9;")


class XmlWriter:
    """Построчная запись XML с отступами по стилю."""

    def __init__(self, style: XmlStyle) -> None:
        self.style = style
        self._parts: list[str] = []
        self._depth = 0

    def _open_tag(self, tag: str, attrs: list[tuple[str, str]]) -> str:
        rendered = "".join(f' {name}="{escape_attr(value)}"' for name, value in attrs)
        return f"<{tag}{rendered}"

    def _line(self, content: str) -> None:
        self._parts.append(self.style.indent * self._depth + content)

    def element(self, tag: str, attrs: list[tuple[str, str]], text: str) -> None:
        """Элемент без дочерних элементов: с текстом или пустой `<Тег/>`."""
        head = self._open_tag(tag, attrs)
        if text:
            body = escape_text(text).replace("\n", self.style.newline)
            self._line(f"{head}>{body}</{tag}>")
        else:
            self._line(f"{head}/>")

    def text_line(self, text: str) -> None:
        """Текст между элементами отдельной строкой на уровне соседних элементов."""
        self._line(escape_text(text).replace("\n", self.style.newline))

    def start(self, tag: str, attrs: list[tuple[str, str]]) -> None:
        """Начало элемента с дочерними элементами."""
        self._line(self._open_tag(tag, attrs) + ">")
        self._depth += 1

    def end(self, tag: str) -> None:
        """Конец элемента с дочерними элементами."""
        self._depth -= 1
        self._line(f"</{tag}>")

    def result(self) -> bytes:
        """Готовый файл в байтах со стилем: BOM, объявление, переводы строк."""
        lines = list(self._parts)
        if self.style.declaration is not None:
            lines.insert(0, self.style.declaration)
        text = self.style.newline.join(lines)
        if self.style.final_newline:
            text += self.style.newline
        data = text.encode("utf-8")
        return BOM + data if self.style.bom else data


def _split_lines(data: bytes) -> list[tuple[bytes, bytes]]:
    """Содержимое строки и её конец: CRLF, LF, CR или пусто."""
    lines: list[tuple[bytes, bytes]] = []
    for raw in data.splitlines(keepends=True):
        if raw.endswith(b"\r\n"):
            lines.append((raw[:-2], b"\r\n"))
        elif raw.endswith(b"\n") or raw.endswith(b"\r"):
            lines.append((raw[:-1], raw[-1:]))
        else:
            lines.append((raw, b""))
    return lines


def preserve_line_endings(old: bytes, new: bytes) -> bytes:
    """Переводы строк старого файла у строк, которые не менялись; у новых — как у строки выше."""
    old_lines = _split_lines(old)
    new_lines = _split_lines(new)
    if not old_lines:
        return new
    matcher = difflib.SequenceMatcher(
        None,
        [text for text, _ending in old_lines],
        [text for text, _ending in new_lines],
        autojunk=False,
    )
    parts: list[bytes] = []
    # Первая новая строка файла не имеет строки выше: берём конец первой строки старого.
    previous = old_lines[0][1]
    last = len(new_lines) - 1
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "delete":
            continue
        if tag == "equal":
            for old_index, new_index in zip(range(i1, i2), range(j1, j2), strict=True):
                ending = old_lines[old_index][1]
                parts.append(new_lines[new_index][0] + ending)
                previous = ending
            continue
        for new_index in range(j1, j2):
            # Последняя строка, если она новая, берёт конец из new, а не у строки выше.
            ending = new_lines[new_index][1] if new_index == last else previous
            parts.append(new_lines[new_index][0] + ending)
            previous = ending
    return b"".join(parts)
