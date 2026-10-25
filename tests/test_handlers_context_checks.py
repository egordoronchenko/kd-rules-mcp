"""Переменные, которых нет у исполнителя БСП: `Правило` в ПВД обмена через план и
`ОбщиеПроцедурыФункции` (БСП:18170–18173, БСП:14647–14668; в УОД есть — УОД:48, 11247)."""

from pathlib import Path

import pytest

from kd_rules_mcp.kd2.model import ExchangeRules
from kd_rules_mcp.kd2.rules_io import load_exchange_rules, load_rules
from kd_rules_mcp.validation.exchange_plan import (
    COMMON_PROCEDURES,
    PVD_RULE_VARIABLE,
    check_exchange_plan,
)
from kd_rules_mcp.validation.report import Issue, Level, ValidationReport
from tests.corpus import EXCHANGE_KINDS, CorpusFile, corpus_params

ROOT = Path(__file__).resolve().parents[1]


def rules(
    pvd_events: str = "", *, pvd_attrs: str = "", pko_events: str = "", algorithms: str = ""
) -> bytes:
    return f"""<ПравилаОбмена>
<ВерсияФормата>2.01</ВерсияФормата>
<ПравилаКонвертацииОбъектов>
<Правило><Код>Спр</Код><Источник>СправочникСсылка.А</Источник>
<Приемник>СправочникСсылка.А</Приемник>{pko_events}</Правило>
</ПравилаКонвертацииОбъектов>
<ПравилаВыгрузкиДанных>
<Правило{pvd_attrs}><Код>В</Код><ОбъектВыборки>СправочникСсылка.А</ОбъектВыборки>
<КодПравилаКонвертации>Спр</КодПравилаКонвертации>{pvd_events}</Правило>
</ПравилаВыгрузкиДанных>
<ПравилаОчисткиДанных>
<Правило><Код>О</Код><ПередОбработкойПравила>А = Правило.Имя;</ПередОбработкойПравила></Правило>
</ПравилаОчисткиДанных>
<Алгоритмы>{algorithms}</Алгоритмы>
</ПравилаОбмена>""".encode()


def run(xml: bytes) -> ValidationReport:
    return check_exchange_plan(load_exchange_rules(xml), None, None)


def hits(report: ValidationReport, check: str) -> list[Issue]:
    return [issue for issue in report.issues if issue.check == check]


@pytest.mark.parametrize("event", ["ПередОбработкойПравила", "ПослеОбработкиПравила"])
def test_pvd_rule_in_plan_events_is_an_error(event: str) -> None:
    found = hits(run(rules(f"<{event}>Имя = Правило.Имя;</{event}>")), PVD_RULE_VARIABLE)
    assert len(found) == 1
    issue = found[0]
    assert issue.level == Level.ERROR
    assert issue.address == "ПВД «В»"
    assert event in issue.message
    assert "ПравилоВыгрузкиДанных" in issue.message
    assert "обмен через план" in issue.message
    assert "универсальной обработкой" in issue.message


def test_pvd_rule_once_per_handler() -> None:
    text = "А = Правило.Имя;\nБ = Правило.Код;"
    events = f"<ПередОбработкойПравила>{text}</ПередОбработкойПравила>"
    assert len(hits(run(rules(events)), PVD_RULE_VARIABLE)) == 1


@pytest.mark.parametrize(
    ("events", "attrs"),
    [
        ("<ПередВыгрузкойОбъекта>А = Правило.Имя;</ПередВыгрузкойОбъекта>", ""),
        ("<ПередОбработкойПравила>// А = Правило.Имя;</ПередОбработкойПравила>", ""),
        ('<ПередОбработкойПравила>Сообщить("Правило");</ПередОбработкойПравила>', ""),
        ("<ПередОбработкойПравила>А = Структура.Правило;</ПередОбработкойПравила>", ""),
        ("<ПередОбработкойПравила>А = ПравилоВыгрузки;</ПередОбработкойПравила>", ""),
        ("<ПередОбработкойПравила>А = Правило.Имя;</ПередОбработкойПравила>", ' Отключить="true"'),
        (
            "<ПередОбработкойПравила>Правило = Новый Структура;\n"
            "А = Правило;</ПередОбработкойПравила>",
            "",
        ),
        (
            "<ПередОбработкойПравила>Для Каждого Правило Из Набор Цикл А = Правило; КонецЦикла;"
            "</ПередОбработкойПравила>",
            "",
        ),
        ("<ПослеОбработкиПравила>For Each Правило In Набор Do EndDo;</ПослеОбработкиПравила>", ""),
    ],
)
def test_pvd_rule_clean_cases(events: str, attrs: str) -> None:
    assert hits(run(rules(events, pvd_attrs=attrs)), PVD_RULE_VARIABLE) == []


def test_pod_handler_with_the_same_tag_is_not_pvd() -> None:
    # ПОД из `rules()` читает `Правило` в ПередОбработкойПравила — ПОД тут ни при чём.
    assert hits(run(rules()), PVD_RULE_VARIABLE) == []


def test_common_procedures_in_handler_and_algorithm() -> None:
    call = "<ПередВыгрузкой>ОбщиеПроцедурыФункции.ВыгрузитьПоПравилу(Источник);</ПередВыгрузкой>"
    algorithm = (
        '<Алгоритм Имя="Общий"><Текст>ОбщиеПроцедурыФункции.ЗаписатьОбъектВИБ(Объект, Тип);'
        "</Текст></Алгоритм>"
    )
    found = hits(run(rules(pko_events=call, algorithms=algorithm)), COMMON_PROCEDURES)
    assert len(found) == 2
    assert found[0].address == "ПКО «Спр»"
    assert "Общий" in found[1].address
    assert all(issue.level == Level.ERROR for issue in found)
    assert "без префикса" in found[0].message
    assert "универсальной обработкой" in found[0].message


@pytest.mark.parametrize(
    ("pko_events", "algorithms"),
    [
        ("<ПередВыгрузкой>// ОбщиеПроцедурыФункции.Х();</ПередВыгрузкой>", ""),
        ('<ПередВыгрузкой>Т = "ОбщиеПроцедурыФункции.Х";</ПередВыгрузкой>', ""),
        ("<ПередВыгрузкой>ВыгрузитьПоПравилу(Источник);</ПередВыгрузкой>", ""),
        # Присваивание в любом тексте правил снимает все замечания.
        (
            "<ПередВыгрузкой>ОбщиеПроцедурыФункции.Х();</ПередВыгрузкой>",
            '<Алгоритм Имя="Инит"><Текст>ОбщиеПроцедурыФункции = Обработка;</Текст></Алгоритм>',
        ),
    ],
)
def test_common_procedures_clean_cases(pko_events: str, algorithms: str) -> None:
    report = run(rules(pko_events=pko_events, algorithms=algorithms))
    assert hits(report, COMMON_PROCEDURES) == []


@pytest.mark.parametrize("check", [PVD_RULE_VARIABLE, COMMON_PROCEDURES])
def test_checks_are_listed_in_doc(check: str) -> None:
    doc = (ROOT / "docs" / "checks.md").read_text(encoding="utf-8")
    assert f"| `{check}` | E |" in doc


@pytest.mark.corpus
@pytest.mark.parametrize("item", corpus_params(EXCHANGE_KINDS))
def test_corpus_has_no_missing_variables(item: CorpusFile) -> None:
    document = load_rules(item.path)
    assert isinstance(document, ExchangeRules)
    report = check_exchange_plan(document, None, None)
    assert hits(report, PVD_RULE_VARIABLE) == []
    assert hits(report, COMMON_PROCEDURES) == []
