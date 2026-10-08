"""Неизвестные имена: параметры, локальные значения и глобальная область BSL."""

import pytest

from kd_rules_mcp.validation.ed_names import unknown_names


def names(body, parameters=(), **kwargs):
    return unknown_names(
        body, parameters, common_modules=("ОбщийМодуль",), module_methods=("МойМетод",), **kwargs
    )


def test_received_processing_object_is_not_the_formal_parameter():
    result = names('ОбъектОбработки.Свойство("X", Значение);', ("ДанныеXDTO",))
    assert [(r.name, r.line) for r in result] == [("ОбъектОбработки", 1)]
    assert not names('ОбъектОбработки.Свойство("X", Значение);', ("ОбъектОбработки",))


@pytest.mark.parametrize(
    "body",
    [
        "Перем Локальная; Локальная = 1; Сообщить(Локальная);",
        "Локальная = 1; Сообщить(Локальная);",
        "Для Каждого Элемент Из ДанныеXDTO Цикл Сообщить(Элемент); КонецЦикла;",
        "Для Индекс = 1 По 10 Цикл Сообщить(Индекс); КонецЦикла;",
        'МойМетод(); ОбщийМодуль.Метод(); Справочники.Тест.НайтиПоКоду("1");',
        "Значение = УровеньЖурналаРегистрации.Ошибка; Строка = Символы.ПС;",
        "Значение = ВидСравнения.Равно; Тип = ТипЗнч(ДанныеXDTO);",
        "Значение = Новый Структура; Значение = Неопределено; ЭтотОбъект.Метод();",
        '// ОбъектОбработки.Метод();\nСообщить("ОбъектОбработки.Метод()");',
    ],
)
def test_known_names_do_not_fail(body):
    assert not names(body, ("ДанныеXDTO",))


def test_read_before_assignment_and_self_assignment():
    assert names("Сообщить(Позже); Позже = 1;")[0].name == "Позже"
    assert names("Позже = Позже + 1;")[0].name == "Позже"
    assert names("Попытка Позже.Метод(); Исключение КонецПопытки;")[0].name == "Позже"
    assert names("Для Индекс = Индекс + 1 По 10 Цикл КонецЦикла;")[0].name == "Индекс"
    assert names("Для Каждого Элемент Из Элемент Цикл КонецЦикла;")[0].name == "Элемент"


def test_unknown_bare_argument_and_call_are_reported():
    assert [r.name for r in names("Сообщить(НетИмени); НетМетода();")] == ["НетИмени", "НетМетода"]
