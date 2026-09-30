"""Проверка ссылок обработчиков на алгоритмы (`Алгоритмы.Имя`)."""

import re

import pytest

from kd2_rules_mcp.kd2.canonical import parse_xml
from kd2_rules_mcp.kd2.model import ExchangeRules
from kd2_rules_mcp.kd2.rules_io import load_exchange_rules
from kd2_rules_mcp.validation.algorithms import (
    MISSING_ALGORITHM,
    WRONG_MODE,
    check_algorithm_refs,
    references,
)
from kd2_rules_mcp.validation.report import Level
from tests.corpus import EXCHANGE_KINDS, CorpusFile, corpus_params

_HEAD = "<ПравилаОбмена><ВерсияФормата>2.01</ВерсияФормата>"
_ISSUE_NAME = re.compile(r"«Алгоритмы\.(\w+)»")


def _rules(body: str, *, head: str = "") -> ExchangeRules:
    xml = f"{_HEAD}{head}{body}</ПравилаОбмена>"
    return load_exchange_rules(xml.encode())


def _algorithm(name: str, *, on_import: bool = False, text: str = "Возврат;") -> str:
    flag = ' ИспользуетсяПриЗагрузке="true"' if on_import else ""
    return f'<Алгоритм Имя="{name}"{flag}><Текст>{text}</Текст></Алгоритм>'


def _pko(event: str, text: str) -> str:
    return (
        "<ПравилаКонвертацииОбъектов><Правило><Код>Док</Код>"
        "<Источник>ДокументСсылка.А</Источник><Приемник>ДокументСсылка.Б</Приемник>"
        f"<{event}>{text}</{event}>"
        "</Правило></ПравилаКонвертацииОбъектов>"
    )


def _checks(rules: ExchangeRules) -> list[str]:
    return [issue.check for issue in check_algorithm_refs(rules).issues]


def test_missing_algorithm_is_error_with_handler_address() -> None:
    rules = _rules(_pko("ПослеЗагрузки", "Выполнить(Алгоритмы.Нет);"))
    issues = check_algorithm_refs(rules).issues
    assert [(issue.check, issue.level) for issue in issues] == [(MISSING_ALGORITHM, Level.ERROR)]
    assert "Док" in issues[0].address and "ПослеЗагрузки" in issues[0].address
    assert "Нет" in issues[0].message


def test_import_handler_needs_import_algorithm() -> None:
    body = _pko("ПриЗагрузке", "Выполнить(Алгоритмы.Общий);")
    wrong = _rules(body + f"<Алгоритмы>{_algorithm('Общий')}</Алгоритмы>")
    right = _rules(body + f"<Алгоритмы>{_algorithm('Общий', on_import=True)}</Алгоритмы>")
    assert _checks(wrong) == [WRONG_MODE]
    assert _checks(right) == []


def test_export_handler_needs_export_algorithm() -> None:
    body = _pko("ПриВыгрузке", "Выполнить(Алгоритмы.Общий);")
    wrong = _rules(body + f"<Алгоритмы>{_algorithm('Общий', on_import=True)}</Алгоритмы>")
    right = _rules(body + f"<Алгоритмы>{_algorithm('Общий')}</Алгоритмы>")
    assert _checks(wrong) == [WRONG_MODE]
    assert _checks(right) == []


def test_search_fields_sequence_runs_on_import() -> None:
    body = _pko("ПоследовательностьПолейПоиска", "Выполнить(Алгоритмы.Поиск);")
    rules = _rules(body + f"<Алгоритмы>{_algorithm('Поиск', on_import=True)}</Алгоритмы>")
    assert _checks(rules) == []


def test_algorithm_calls_algorithm_of_its_own_mode() -> None:
    caller = _algorithm("Вызов", on_import=True, text="Выполнить(Алгоритмы.Цель);")
    wrong = _rules(f"<Алгоритмы>{caller}{_algorithm('Цель')}</Алгоритмы>")
    right = _rules(f"<Алгоритмы>{caller}{_algorithm('Цель', on_import=True)}</Алгоритмы>")
    assert _checks(wrong) == [WRONG_MODE]
    assert _checks(right) == []


def test_after_rules_load_checks_only_presence() -> None:
    head = "<ПослеЗагрузкиПравилОбмена>Выполнить(Алгоритмы.Любой);</ПослеЗагрузкиПравилОбмена>"
    present = _rules(f"<Алгоритмы>{_algorithm('Любой', on_import=True)}</Алгоритмы>", head=head)
    absent = _rules("", head=head)
    assert _checks(present) == []
    assert _checks(absent) == [MISSING_ALGORITHM]


def test_name_is_case_insensitive_and_trimmed() -> None:
    body = _pko("ПриВыгрузке", "Выполнить(алгоритмы.общий);")
    rules = _rules(body + f"<Алгоритмы>{_algorithm('Общий  ')}</Алгоритмы>")
    assert _checks(rules) == []


def test_references_skip_comments_literals_and_methods() -> None:
    text = (
        "// Выполнить(Алгоритмы.ВКомментарии);\n"
        'Текст = "Алгоритмы.ВСтроке";\n'
        'Если Алгоритмы.Свойство("Метод") Тогда\n'
        "    Выполнить(Алгоритмы.Настоящий); // Алгоритмы.ПослеКода\n"
        "КонецЕсли;\n"
        "Выполнить(Алгоритмы . Настоящий);"
    )
    assert references(text) == ["Настоящий"]


@pytest.mark.corpus
@pytest.mark.parametrize("item", corpus_params(EXCHANGE_KINDS))
def test_corpus_algorithm_issues_match_xml(item: CorpusFile) -> None:
    """Замечания проверки сверяются со списком алгоритмов, прочитанным прямо из XML.

    «Нет алгоритма» — имени нет среди `Алгоритмы//Алгоритм/@Имя`; «не тот режим» — имя есть.
    """
    raw = item.path.read_bytes()
    declared = {
        str(element.get("Имя", "")).rstrip().casefold()
        for element in parse_xml(raw).iterfind("Алгоритмы//Алгоритм")
    }
    for issue in check_algorithm_refs(load_exchange_rules(raw)).issues:
        assert issue.level is Level.ERROR, item.id
        assert issue.address, item.id
        match = _ISSUE_NAME.search(issue.message)
        assert match is not None, issue.message
        name = match.group(1).casefold()
        if issue.check == MISSING_ALGORITHM:
            assert name not in declared, (item.id, issue.message)
        else:
            assert issue.check == WRONG_MODE, (item.id, issue.check)
            assert name in declared, (item.id, issue.message)
