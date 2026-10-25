"""Общий разбор BSL обработчиков: литералы через строки, комментарии, начало оператора."""

import re

import pytest

from kd_rules_mcp.kd2.bsl_text import (
    bsl_code,
    code_matches,
    in_literal,
    join_code,
    literals,
    statement_matches,
    statement_start,
    string_end,
    strip_comments,
)
from kd_rules_mcp.kd2.queries import statement_assignment, tokens

# Литерал через строки: `//` и `""` в нём — текст, строка-комментарий с кавычкой между его строками
# литерал не закрывает. Синтакс-чекер (bsl-analyzer 0.4.0) принимает такой код без ошибок.
MULTILINE = (
    'Текст = "ВЫБРАТЬ // не комментарий\n'
    '// комментарий с кавычкой " внутри\n'
    '|\tПоле ""Имя"" // текст\n'
    '|ИЗ Таблица"; // хвост\n'
    "А = 1;"
)


def test_views_keep_length_and_lines() -> None:
    for view in (strip_comments(MULTILINE), bsl_code(MULTILINE)):
        assert len(view) == len(MULTILINE)
        assert view.count("\n") == MULTILINE.count("\n")
        assert view.endswith("А = 1;")


def test_multiline_literal_with_comment_mark_and_quotes() -> None:
    kept = strip_comments(MULTILINE)
    assert "ВЫБРАТЬ // не комментарий" in kept
    assert '|\tПоле ""Имя"" // текст' in kept
    assert "комментарий с кавычкой" not in kept
    assert "хвост" not in kept
    code = bsl_code(MULTILINE)
    for word in ("ВЫБРАТЬ", "Поле", "Имя", "ИЗ", "Таблица", "хвост", "комментарий"):
        assert word not in code
    assert code.startswith('Текст = "')
    assert code.count('"') == 2
    assert '"; ' in code


def test_literal_values_drop_comment_lines() -> None:
    assert literals(MULTILINE) == ['ВЫБРАТЬ // не комментарий\n|\tПоле "Имя" // текст\n|ИЗ Таблица']
    token = next(t for t in tokens(MULTILINE) if t.string)
    assert token.value == 'ВЫБРАТЬ // не комментарий\n\tПоле "Имя" // текст\nИЗ Таблица'
    assert token.closed


def test_unclosed_literal_runs_to_the_end() -> None:
    text = 'А = "без конца\nБ = 1;'
    assert bsl_code(text) == 'А = "' + " " * 9 + "\n" + " " * 6
    assert string_end(text, 4) == len(text)
    assert not tokens(text)[-1].closed


def test_string_end_skips_comment_line_with_quote() -> None:
    start = MULTILINE.index('"')
    end = string_end(MULTILINE, start)
    assert MULTILINE[end - 1 : end + 1] == '";'
    assert MULTILINE[start:end].endswith('ИЗ Таблица"')


def test_in_literal_positions() -> None:
    text = 'А = "Б"; В'
    assert not in_literal(text, text.index('"'))
    assert in_literal(text, text.index("Б"))
    assert not in_literal(text, text.rindex('"'))
    assert not in_literal(text, text.index("В"))


@pytest.mark.parametrize(
    "prefix",
    [
        "",
        "А = 1; ",
        "Если А Тогда ",
        "Иначе ",
        "Пока А Цикл ",
        "Попытка ",
        "Исключение ",
        "КонецЕсли\n",
        "КонецЦикла\n",
        "КонецПопытки\n",
        "If A Then ",
        "Else ",
        "While A Do ",
        "Try ",
        "Except ",
        "EndIf\n",
        "~Метка: ",
        "#Если Сервер Тогда\n",
        "#КонецЕсли\n",
        "А = 1; // комментарий\n",
    ],
)
def test_statement_start_after_keywords_labels_and_preprocessor(prefix: str) -> None:
    text = prefix + "Отказ = Истина;"
    assert statement_start(bsl_code(text), text.index("Отказ"))


@pytest.mark.parametrize(
    "prefix",
    [
        "А = ?(",
        "Ф(1, ",
        "А = ",
        "Если ",
        "ИначеЕсли ",
        "А = Б + ",
        "Возврат ",
        "Если Не ",
        "Пока ",
        "А = (",
    ],
)
def test_statement_start_is_not_an_expression(prefix: str) -> None:
    text = prefix + "Отказ = Истина"
    assert not statement_start(bsl_code(text), text.index("Отказ"))


def test_code_matches_skip_literals_and_do_not_hide_the_next_match() -> None:
    pattern = re.compile(r"Ф\([^)]*\)")
    text = 'А = "Ф(" + Б; Ф(1);'
    assert [m.group() for m in code_matches(pattern, text)] == ["Ф(1)"]
    assign = re.compile(r"(?<![\w.])Отказ\s*=\s*Истина\b")
    text = (
        'Сообщить("Отказ = Истина");\n'
        "// Отказ = Истина;\n"
        "А = ?(Отказ = Истина, 1, 2);\n"
        "Если Отказ = Истина Тогда\n"
        "Попытка Отказ = Истина; Исключение КонецПопытки;"
    )
    found = list(statement_matches(assign, text))
    assert [text.count("\n", 0, m.start()) for m in found] == [4]
    assert len(list(code_matches(assign, text))) == 3


def test_rejected_comparison_does_not_swallow_the_rest_of_the_line() -> None:
    pattern = re.compile(r"(?<![\w.])РежимЗаписи\s*=\s*([^\n]*)")
    text = 'Если РежимЗаписи = "" Тогда РежимЗаписи = "Проведение"; КонецЕсли;'
    found = list(statement_matches(pattern, text))
    assert [m.group(1) for m in found] == ['"Проведение"; КонецЕсли;']


def test_join_code_starts_every_part_with_a_statement() -> None:
    joined = join_code(["Выполнить(Алгоритмы.А) // без точки с запятой", "Отказ = Истина;"])
    assert statement_start(bsl_code(joined), joined.index("Отказ"))
    assert joined.count("\n") == 2


def test_queries_statement_assignment_uses_the_same_keywords() -> None:
    for text in ("Попытка ИмяПКО = 1;", "Исключение ИмяПКО = 1;", "Try ИмяПКО = 1;"):
        items = tokens(text)
        position = next(i for i, t in enumerate(items) if t.value == "ИмяПКО")
        assert statement_assignment(items, position)
    items = tokens("А = ?(ИмяПКО = 1, 2, 3);")
    position = next(i for i, t in enumerate(items) if t.value == "ИмяПКО")
    assert not statement_assignment(items, position)
