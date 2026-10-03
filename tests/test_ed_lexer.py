"""Лексические границы: операторы выделяются до распознавания форм."""

import pytest

from kd2_rules_mcp.ed import EdFormatError, read_manager_text
from kd2_rules_mcp.ed.lexer import lex, split_arguments, tokenize


def test_arguments_strings_comments_and_nested_calls():
    tokens = tokenize('F("a,b""c", G(1, A[2]), , "// literal"); // хвост')
    parts = split_arguments(tokens[2:-3])
    assert len(parts) == 4
    assert parts[0][0].value == 'a,b"c'
    assert [token.value for token in parts[1]] == ["G", "(", "1", ",", "A", "[", "2", "]", ")"]
    assert parts[2] == ()
    assert parts[3][0].value == "// literal"
    assert tokens[-1].kind == "comment"


def test_multiline_with_interleaved_comment_and_crlf():
    text = '"один\r\n\t// комментарий между продолжениями\r\n\t|два ""x"""'
    (token,) = tokenize(text)
    assert token.value == 'один\nдва "x"'
    assert text[token.start : token.end] == text


@pytest.mark.parametrize("text", ['"незакрыто', '"a\nwrong"', "'незакрыто"])
def test_unclosed_literals(text):
    with pytest.raises(EdFormatError):
        tokenize(text)


@pytest.mark.parametrize("text", ["A(1", "A]", "(A]"])
def test_bad_argument_brackets(text):
    with pytest.raises(EdFormatError):
        split_arguments(tokenize(text))


def test_semicolon_inside_string_and_missing_semicolon_before_end_if():
    text = """Процедура ЗаполнитьПараметрыКонвертации(ПараметрыКонвертации) Экспорт
Если Истина Тогда
ПараметрыКонвертации.Вставить("a;КонецПроцедуры");
Иначе
ПараметрыКонвертации.Вставить("b")
КонецЕсли;
КонецПроцедуры"""
    doc = read_manager_text(text)
    statements = lex(doc.files[0]).statements
    assert [st.head for st in statements] == [
        "процедура",
        "если",
        "параметрыконвертации",
        "иначе",
        "параметрыконвертации",
        "конецесли",
        "конецпроцедуры",
    ]
    assert [p.name for p in doc.parameters] == ["a;КонецПроцедуры", "b"]
    for st in statements:
        assert text[st.span.char_start : st.span.char_end] == st.raw_text


def test_regions_tags_and_directives_do_not_leak_from_literals():
    text = """#Если Сервер Тогда
#Область Внешняя
#Область Внутренняя
//++ метка
Процедура ЗаполнитьПараметрыКонвертации(ПараметрыКонвертации) Экспорт
ПараметрыКонвертации.Вставить("#КонецОбласти //++ нет");
КонецПроцедуры
//-- метка
#КонецОбласти
#КонецОбласти
#КонецЕсли"""
    doc = read_manager_text(text)
    param = doc.parameters[0]
    assert param.regions == ("Внешняя", "Внутренняя")
    assert param.tag_ids == ("метка",)
    assert len(param.guards) == 1
    assert doc.guards[0].guard_kind == "preprocessor"


def test_unbalanced_region_is_diagnostic():
    doc = read_manager_text("""#КонецОбласти
Процедура ЗаполнитьПараметрыКонвертации(ПараметрыКонвертации)
КонецПроцедуры""")
    assert "unbalanced_region" in {d.code for d in doc.diagnostics}
