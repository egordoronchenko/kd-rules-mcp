"""Строгий ответ платформы и безопасные значения кода."""

import pytest

from kd_rules_mcp.errors import LiveCodeError, LiveEmptyError, LiveParseError
from kd_rules_mcp.live.response import json_code, literal, parse_response, short_error


@pytest.mark.parametrize("text", ["OK", "OK ", "OK {", "OK []", "OK {} хвост", "успех {}"])
def test_truncated_or_wrong_response(text: str) -> None:
    with pytest.raises(LiveParseError, match="неверный ответ") as caught:
        parse_response(text)
    assert text.strip() in str(caught.value)


def test_empty_response() -> None:
    with pytest.raises(LiveEmptyError, match="код не выполнился"):
        parse_response("")
    assert parse_response("OK {}") == {}
    assert parse_response('OK {"text":"строка\\n|;\\"Символ("}') == {"text": 'строка\n|;"Символ('}


def test_platform_error_shortened() -> None:
    text = "ОШИБКА Загрузка не выполнена\n{стек}\nпо причине:\nневерный XML " + "A" * 400
    with pytest.raises(LiveCodeError) as caught:
        parse_response(text)
    assert str(caught.value) == "Загрузка не выполнена неверный XML <base64>"
    assert short_error("ОШИБКА причина\n{стек}") == "причина"


@pytest.mark.parametrize(
    "value", ['Имя"|;\r\nСимвол(10) Кириллица', "//комментарий", 'x"; Выполнить("код")']
)
def test_single_line_escaped(value: str) -> None:
    code = json_code(f'Данные = Новый Структура("value", {literal(value)});')
    assert "\r" not in code and "\n" not in code and "//" not in code
    assert code.startswith("Попытка ") and code.endswith("КонецПопытки;")
    assert "ЗаписатьJSON(Запись, Данные)" in code
    assert 'Выполнить("код")' not in code
    if "\r" in value:
        assert (
            literal(value) == '("Имя""|;" + Символ(13) + "" + Символ(10) + "Символ(10) Кириллица")'
        )
