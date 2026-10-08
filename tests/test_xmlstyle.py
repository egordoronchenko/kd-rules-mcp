"""Юнит-тесты стиля XML: определение оформления, экранирование и запись."""

import pytest

from kd_rules_mcp.kd2.xmlstyle import (
    BOM,
    XmlStyle,
    XmlWriter,
    detect_style,
    escape_attr,
    escape_text,
    preserve_line_endings,
)

DECLARATION = '<?xml version="1.0" encoding="UTF-8"?>'


def _style(
    *,
    bom: bool = False,
    declaration: str | None = None,
    newline: str = "\n",
    indent: str = "\t",
    final_newline: bool = False,
) -> XmlStyle:
    """Стиль с явными полями: умолчания `XmlStyle` в этих тестах не подмешиваются."""
    return XmlStyle(
        bom=bom,
        declaration=declaration,
        newline=newline,
        indent=indent,
        final_newline=final_newline,
    )


def _sample(
    *,
    bom: bool,
    declaration: str | None,
    newline: str,
    indent: str,
    final_newline: bool,
) -> bytes:
    """Небольшой XML-файл с заданным оформлением."""
    lines: list[str] = []
    if declaration is not None:
        lines.append(declaration)
    lines.append("<Корень>")
    lines.append(f"{indent}<Дочерний/>")
    lines.append("</Корень>")
    text = newline.join(lines)
    if final_newline:
        text += newline
    data = text.encode("utf-8")
    return BOM + data if bom else data


@pytest.mark.parametrize("bom", [True, False], ids=["bom", "no-bom"])
@pytest.mark.parametrize(
    "declaration",
    [DECLARATION, None],
    ids=["declaration", "no-declaration"],
)
@pytest.mark.parametrize("newline", ["\r\n", "\n"], ids=["crlf", "lf"])
@pytest.mark.parametrize("indent", ["\t", "  "], ids=["tab", "spaces"])
@pytest.mark.parametrize(
    "final_newline",
    [True, False],
    ids=["final-newline", "no-final-newline"],
)
def test_detect_style_reads_decoration(
    bom: bool,
    declaration: str | None,
    newline: str,
    indent: str,
    final_newline: bool,
) -> None:
    """Стиль повторяет BOM, объявление, перевод строки, отступ и перевод в конце файла."""
    raw = _sample(
        bom=bom,
        declaration=declaration,
        newline=newline,
        indent=indent,
        final_newline=final_newline,
    )
    assert detect_style(raw) == _style(
        bom=bom,
        declaration=declaration,
        newline=newline,
        indent=indent,
        final_newline=final_newline,
    )


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("&", "&amp;"),
        ("<", "&lt;"),
        (">", "&gt;"),
        ("A & B < C > D", "A &amp; B &lt; C &gt; D"),
        ("до\rпосле", "до&#13;после"),
        ("&\r<", "&amp;&#13;&lt;"),
    ],
)
def test_escape_text(source: str, expected: str) -> None:
    """В тексте экранируются `&`, `<`, `>` и CR; амперсанд в `&#13;` не экранируется повторно."""
    assert escape_text(source) == expected


def test_escape_text_keeps_quote_newline_and_tab() -> None:
    """Кавычка, LF и табуляция в тексте остаются как есть."""
    assert escape_text('кавычка " и\nтаб\t') == 'кавычка " и\nтаб\t'


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("&", "&amp;"),
        ("<", "&lt;"),
        (">", "&gt;"),
        ('скажи "да"', "скажи &quot;да&quot;"),
        ("первая\nвторая", "первая&#10;вторая"),
        ("поле\tзначение", "поле&#9;значение"),
        ("до\rпосле", "до&#13;после"),
        ("a\r\nb", "a&#13;&#10;b"),
        ('&\n\t"<>', "&amp;&#10;&#9;&quot;&lt;&gt;"),
    ],
)
def test_escape_attr(source: str, expected: str) -> None:
    """В атрибуте экранируются `&`, `<`, `>`, кавычка, переводы строки, табуляция и CR."""
    assert escape_attr(source) == expected


def test_writer_empty_element_is_self_closing() -> None:
    """Пустой элемент пишется как `<Тег/>`."""
    writer = XmlWriter(_style())
    writer.element("Тег", [], "")
    assert writer.result() == "<Тег/>".encode()


def test_writer_element_with_text_is_one_line() -> None:
    """Элемент с текстом пишется в одну строку, спецсимволы текста экранируются."""
    writer = XmlWriter(_style(newline="\r\n"))
    writer.element("Тег", [], 'текст & <значение> "x"\r')
    assert writer.result() == '<Тег>текст &amp; &lt;значение&gt; "x"&#13;</Тег>'.encode()


def test_writer_escapes_attribute_on_empty_element() -> None:
    """Кавычка, перевод строки и табуляция в атрибуте становятся ссылками на символы."""
    writer = XmlWriter(_style())
    writer.element("Тег", [("Имя", 'a "b"\n\t')], "")
    assert writer.result() == '<Тег Имя="a &quot;b&quot;&#10;&#9;"/>'.encode()


@pytest.mark.parametrize("indent", ["\t", "  "], ids=["tab", "spaces"])
@pytest.mark.parametrize("newline", ["\r\n", "\n"], ids=["crlf", "lf"])
def test_writer_nests_elements_with_style_indent(indent: str, newline: str) -> None:
    """Вложенные элементы отступаются по стилю, пустой лист — `<Лист/>`, текст — в одну строку."""
    writer = XmlWriter(_style(newline=newline, indent=indent))
    writer.start("Корень", [])
    writer.element("Дочерний", [], "значение")
    writer.start("Ветка", [])
    writer.element("Лист", [], "")
    writer.end("Ветка")
    writer.end("Корень")
    expected = newline.join(
        [
            "<Корень>",
            f"{indent}<Дочерний>значение</Дочерний>",
            f"{indent}<Ветка>",
            f"{indent * 2}<Лист/>",
            f"{indent}</Ветка>",
            "</Корень>",
        ]
    )
    assert writer.result() == expected.encode()


@pytest.mark.parametrize("final_newline", [False, True], ids=["no-final-newline", "final-newline"])
def test_writer_result_applies_bom_declaration_and_final_newline(final_newline: bool) -> None:
    """`result()` добавляет BOM и объявление; перевод в конце есть только при `final_newline`."""
    writer = XmlWriter(
        _style(
            bom=True,
            declaration=DECLARATION,
            newline="\r\n",
            indent="\t",
            final_newline=final_newline,
        )
    )
    writer.start("Корень", [])
    writer.element("Тег", [], "")
    writer.end("Корень")
    body = f"{DECLARATION}\r\n<Корень>\r\n\t<Тег/>\r\n</Корень>"
    if final_newline:
        body += "\r\n"
    assert writer.result() == BOM + body.encode("utf-8")


def _bodies(data: bytes) -> list[bytes]:
    """Строки без концов."""
    bodies: list[bytes] = []
    for line in data.splitlines(keepends=True):
        if line.endswith(b"\r\n"):
            bodies.append(line[:-2])
        elif line.endswith(b"\n") or line.endswith(b"\r"):
            bodies.append(line[:-1])
        else:
            bodies.append(line)
    return bodies


def test_preserve_line_endings_keeps_handler_lf_and_neighbor_crlf() -> None:
    """CRLF в XML и LF в трёх строках обработчика; добавленные строки берут конец строки выше."""
    old = (
        BOM
        + b"<Root>\r\n"
        + b"\t<Handler>\r\n"
        + b"\t\tline one\n"
        + b"\t\tline two\n"
        + b"\t\tline three\n"
        + b"\t</Handler>\r\n"
        + b"</Root>"
    )
    new = (
        BOM
        + b"<Root>\r\n"
        + b"\t<Added1/>\r\n"
        + b"\t<Added2/>\r\n"
        + b"\t<Handler>\r\n"
        + b"\t\tline one\r\n"
        + b"\t\tline two\r\n"
        + b"\t\tline three\r\n"
        + b"\t</Handler>\r\n"
        + b"</Root>"
    )
    result = preserve_line_endings(old, new)
    assert result == (
        BOM
        + b"<Root>\r\n"
        + b"\t<Added1/>\r\n"
        + b"\t<Added2/>\r\n"
        + b"\t<Handler>\r\n"
        + b"\t\tline one\n"
        + b"\t\tline two\n"
        + b"\t\tline three\n"
        + b"\t</Handler>\r\n"
        + b"</Root>"
    )
    assert _bodies(result) == _bodies(new)


def test_preserve_line_endings_same_text_returns_old_bytes() -> None:
    """Без изменений содержимого результат совпадает со старым файлом, включая BOM."""
    old = BOM + b"a\r\nb\nc\r\n"
    new = BOM + b"a\nb\r\nc\n"
    result = preserve_line_endings(old, new)
    assert result == old
    assert _bodies(result) == _bodies(new)


def test_preserve_line_endings_uniform_crlf_matches_new() -> None:
    """Единый CRLF у старого файла: результат совпадает с новым."""
    old = b"a\r\nb\r\nc\r\n"
    new = b"a\r\nX\r\nb\r\nc\r\n"
    assert preserve_line_endings(old, new) == new


def test_preserve_line_endings_empty_old_returns_new() -> None:
    """Пустой старый файл не подменяет переводы строк нового."""
    new = b"a\r\nb\n"
    assert preserve_line_endings(b"", new) == new


def test_preserve_line_endings_delete_keeps_neighbor_endings() -> None:
    """Удаление не меняет концы соседей; неизменённая последняя строка остаётся без перевода."""
    old = b"a\r\nDEL\nb\nlast"
    new = b"a\r\nb\r\nlast"
    assert preserve_line_endings(old, new) == b"a\r\nb\nlast"


def test_preserve_line_endings_changed_last_line_uses_new_ending() -> None:
    """Изменённая последняя строка берёт конец из нового файла."""
    assert preserve_line_endings(b"a\nlast", b"a\r\nchanged") == b"a\nchanged"


def test_preserve_line_endings_diffs_only_the_changed_middle() -> None:
    """Правка в середине: строки до и после неё сохраняют свои концы, новая — как у строки выше."""
    old = b"a\r\nb\nc\nd\r\ne\n"
    new = b"a\nb\nX\nY\nd\ne\n"
    assert preserve_line_endings(old, new) == b"a\r\nb\nX\nY\nd\r\ne\n"


@pytest.mark.parametrize(
    ("new", "expected"),
    [
        (b"a\nB\nC\nd\ne\n", b"a\r\nB\r\nC\r\nd\r\ne\n"),
        (b"a\nB\nd\ne\n", b"a\r\nB\r\nd\r\ne\n"),
    ],
)
def test_preserve_line_endings_over_budget_falls_back(
    monkeypatch: pytest.MonkeyPatch, new: bytes, expected: bytes
) -> None:
    """Середина сверх предела диффа: сопоставление по месту либо грубый дифф, без потери строк."""
    from kd_rules_mcp.kd2 import xmlstyle

    monkeypatch.setattr(xmlstyle, "_DIFF_BUDGET", 0)
    old = b"a\r\nb\nc\nd\r\ne\n"
    assert preserve_line_endings(old, new) == expected
