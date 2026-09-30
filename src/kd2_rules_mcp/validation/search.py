"""Проверка параметров объекта, которые читает обработчик `ПоследовательностьПолейПоиска`.

Читатель — `DataProcessors/КонвертацияОбъектовИнформационныхБаз/Ext/ObjectModule.bsl` (БСП).
Объект ищется сразу по узлу `<Ссылка>`: загрузка встречает его первым и вызывает
`НайтиОбъектПоСсылке` с текущими `ПараметрыОбъекта` (БСП:10735–10757), а обработчик поиска
получает их же (БСП:8947). В узел `<Ссылка>` выгрузка пишет только свойства поиска ПКО
(БСП:790–826); ПКС с `ИмяПараметраДляПередачи` пишется узлом `<ЗначениеПараметра>`
(БСП:12691), и из узла ссылки такой параметр попадает в `ПараметрыОбъекта` до поиска
(БСП:8228–8291). Параметр ПКС без флага «Поиск» идёт в свойствах объекта после ссылки и читается
уже после поиска (БСП:10705): в обработчике поиска его нет, `ПараметрыОбъекта` — `Неопределено`,
если других параметров в ссылке нет.

- Обработчик поиска читает параметр, который передаёт ПКС без «Поиск», — предупреждение
  (ветка обработчика по этому параметру не выполнится — типичная причина дублей).
- Параметр не передаёт ни одно включённое ПКС верхнего уровня ПКО — предупреждение.

Ссылка на параметр — `ПараметрыОбъекта["Имя"]` или `ПараметрыОбъекта.Получить("Имя")` в тексте
без комментариев `//`.
"""

import re

from kd2_rules_mcp.kd2.model import ExchangeRules, Node
from kd2_rules_mcp.validation.address import rule_address
from kd2_rules_mcp.validation.report import ValidationReport

PARAM_NOT_IN_SEARCH = "search.param_not_in_search"
PARAM_NOT_PASSED = "search.param_not_passed"

_PARAMETER = re.compile(
    r'ПараметрыОбъекта\s*(?:\[\s*"([^"\n]+)"\s*\]|\.\s*Получить\s*\(\s*"([^"\n]+)"\s*\))',
    re.IGNORECASE,
)


def check_search_params(rules: ExchangeRules) -> ValidationReport:
    """Параметры, которые обработчик поиска ПКО читает, против ПКС, которые их передают."""
    report = ValidationReport()
    for pko in rules.pko():
        text = pko.get("ПоследовательностьПолейПоиска")
        if not isinstance(text, str) or not text.strip():
            continue
        passed = _passed_parameters(pko)
        for name in sorted(_read_parameters(text), key=str.casefold):
            search = passed.get(name.casefold())
            address = rule_address(pko)
            if search is None:
                report.warning(
                    PARAM_NOT_PASSED,
                    address,
                    f"ПоследовательностьПолейПоиска читает параметр «{name}», но ни одно "
                    "включённое ПКС верхнего уровня его не передаёт (ИмяПараметраДляПередачи): "
                    "при поиске его нет",
                )
            elif not search:
                report.warning(
                    PARAM_NOT_IN_SEARCH,
                    address,
                    f"ПоследовательностьПолейПоиска читает параметр «{name}», а ПКС передаёт его "
                    "без флага «Поиск»: параметр приходит в свойствах объекта уже после поиска, "
                    "в обработчике поиска его нет и ветка по нему не выполнится — включите «Поиск» "
                    "у ПКС параметра",
                )
    return report


def _read_parameters(text: str) -> set[str]:
    names: set[str] = set()
    for line in text.splitlines():
        for match in _PARAMETER.finditer(_without_comment(line)):
            names.add(match.group(1) or match.group(2))
    return names


def _without_comment(line: str) -> str:
    """Строка до комментария `//` вне строкового литерала."""
    quoted = False
    for index, char in enumerate(line):
        if char == '"':
            quoted = not quoted
        elif not quoted and line.startswith("//", index):
            return line[:index]
    return line


def _passed_parameters(pko: Node) -> dict[str, bool]:
    """Параметры, которые передают включённые ПКС верхнего уровня: имя → есть ли флаг «Поиск»."""
    passed: dict[str, bool] = {}
    properties = pko.child("Свойства")
    if properties is None:
        return passed
    for item in properties.items:
        if item.is_group or item.attrs.get("Отключить") is True:
            continue
        name = str(item.get("ИмяПараметраДляПередачи")).strip()
        if name:
            key = name.casefold()
            passed[key] = passed.get(key, False) or item.attrs.get("Поиск") is True
    return passed
