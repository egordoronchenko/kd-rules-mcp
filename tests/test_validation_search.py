"""Проверка параметров объекта в обработчике `ПоследовательностьПолейПоиска`."""

import pytest

from kd2_rules_mcp.kd2.model import ExchangeRules
from kd2_rules_mcp.kd2.rules_io import load_exchange_rules
from kd2_rules_mcp.validation.report import Level
from kd2_rules_mcp.validation.search import (
    PARAM_NOT_IN_SEARCH,
    PARAM_NOT_PASSED,
    check_search_params,
)
from tests.corpus import EXCHANGE_KINDS, CorpusFile, corpus_params

# Юрлицо ищется по ИНН и КПП, если параметр «ТехПараметр1» (вид лица) дошёл до поиска.
_SEARCH = """
Если ПараметрыОбъекта = Неопределено Тогда
	СтрокаИменСвойствПоиска = "ИНН, КПП, Наименование, ЭтоГруппа";
ИначеЕсли ПараметрыОбъекта["ТехПараметр1"] = "Юридическое лицо" Тогда
	СтрокаИменСвойствПоиска = "ИНН, КПП, ЭтоГруппа";
КонецЕсли;
// ПараметрыОбъекта["ВКомментарии"]
"""


def _rules(search: str, properties: str) -> ExchangeRules:
    xml = (
        "<ПравилаОбмена><ВерсияФормата>2.01</ВерсияФормата>"
        "<ПравилаКонвертацииОбъектов><Правило><Код>Контрагенты</Код>"
        f"<ПоследовательностьПолейПоиска>{search}</ПоследовательностьПолейПоиска>"
        "<Источник>СправочникСсылка.Контрагенты</Источник>"
        "<Приемник>СправочникСсылка.Контрагенты</Приемник>"
        f"<Свойства>{properties}</Свойства>"
        "</Правило></ПравилаКонвертацииОбъектов></ПравилаОбмена>"
    )
    return load_exchange_rules(xml.encode())


def _parameter(name: str, *, search: bool = False, disabled: bool = False) -> str:
    attrs = (' Поиск="true"' if search else "") + (' Отключить="true"' if disabled else "")
    return (
        f'<Свойство{attrs}><Код>1</Код><Источник Имя="" Вид=""/><Приемник Имя="" Вид=""/>'
        f"<ИмяПараметраДляПередачи>{name}</ИмяПараметраДляПередачи></Свойство>"
    )


def _checks(rules: ExchangeRules) -> list[tuple[str, Level]]:
    return [(issue.check, issue.level) for issue in check_search_params(rules).issues]


def test_parameter_without_search_flag_is_warning() -> None:
    issues = check_search_params(_rules(_SEARCH, _parameter("ТехПараметр1"))).issues
    assert [(issue.check, issue.level) for issue in issues] == [
        (PARAM_NOT_IN_SEARCH, Level.WARNING)
    ]
    assert issues[0].address == "ПКО «Контрагенты»"
    assert "ТехПараметр1" in issues[0].message and "Поиск" in issues[0].message


def test_search_parameter_is_accepted() -> None:
    assert _checks(_rules(_SEARCH, _parameter("ТехПараметр1", search=True))) == []


@pytest.mark.parametrize(
    "properties", ["", _parameter("Другой", search=True), _parameter("ТехПараметр1", disabled=True)]
)
def test_parameter_not_passed(properties: str) -> None:
    assert _checks(_rules(_SEARCH, properties)) == [(PARAM_NOT_PASSED, Level.WARNING)]


def test_get_method_and_no_handler() -> None:
    search = 'Вид = ПараметрыОбъекта.Получить("ТехПараметр1");'
    assert _checks(_rules(search, _parameter("ТехПараметр1"))) == [
        (PARAM_NOT_IN_SEARCH, Level.WARNING)
    ]
    assert _checks(_rules("", _parameter("ТехПараметр1"))) == []


@pytest.mark.parametrize("corpus_file", corpus_params(EXCHANGE_KINDS))
def test_corpus_runs(corpus_file: CorpusFile) -> None:
    """На корпусе проверка отрабатывает без исключений."""
    check_search_params(load_exchange_rules(corpus_file.path.read_bytes()))
