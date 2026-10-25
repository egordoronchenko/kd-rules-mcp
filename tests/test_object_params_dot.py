"""Проверка `handlers.object_params_dot`: обращение к `ПараметрыОбъекта` через точку.

У исполнителя БСП `ПараметрыОбъекта` — `Неопределено` или соответствие (БСП:10131, БСП:9786),
полей у соответствия нет.
"""

from pathlib import Path

import pytest

from kd_rules_mcp.kd2.model import ExchangeRules
from kd_rules_mcp.kd2.rules_io import load_exchange_rules, load_rules
from kd_rules_mcp.validation.exchange_plan import OBJECT_PARAMS_DOT, check_exchange_plan
from kd_rules_mcp.validation.report import Issue, Level, ValidationReport
from tests.corpus import EXCHANGE_KINDS, CorpusFile, corpus_params

ROOT = Path(__file__).resolve().parents[1]


def rules(
    after_load: str = "", *, attrs: str = "", algorithms: str = "", on_load: str = ""
) -> bytes:
    return f"""<ПравилаОбмена>
<ВерсияФормата>2.01</ВерсияФормата>
<ПравилаКонвертацииОбъектов>
<Правило{attrs}><Код>Спр</Код><Источник>СправочникСсылка.А</Источник>
<Приемник>СправочникСсылка.А</Приемник><ПриЗагрузке>{on_load}</ПриЗагрузке>
<ПослеЗагрузки>{after_load}</ПослеЗагрузки></Правило>
</ПравилаКонвертацииОбъектов>
<Алгоритмы>{algorithms}</Алгоритмы>
</ПравилаОбмена>""".encode()


def run(xml: bytes) -> ValidationReport:
    return check_exchange_plan(load_exchange_rules(xml), None, None)


def hits(report: ValidationReport) -> list[Issue]:
    return [issue for issue in report.issues if issue.check == OBJECT_PARAMS_DOT]


def skips(report: ValidationReport) -> list[str]:
    return [item.reason for item in report.skipped if item.check == OBJECT_PARAMS_DOT]


def test_dot_access_is_an_error_with_the_right_form() -> None:
    found = hits(run(rules("Значение = ПараметрыОбъекта.Контрагент;")))
    assert len(found) == 1
    issue = found[0]
    assert issue.level == Level.ERROR
    assert issue.address == "ПКО «Спр»"
    assert "ПослеЗагрузки" in issue.message
    assert 'ПараметрыОбъекта["Контрагент"]' in issue.message
    assert 'ПараметрыОбъекта.Получить("Контрагент")' in issue.message


def test_dot_access_one_issue_per_name_and_case_insensitive() -> None:
    text = (
        "А = параметрыобъекта.Контрагент;\n"
        "Б = ПараметрыОбъекта . контрагент;\n"
        "ПараметрыОбъекта.Договор = 1;\n"
        'Если ПараметрыОбъекта.Свойство("Вид") Тогда КонецЕсли;'
    )
    names = sorted(
        issue.message.split("«ПараметрыОбъекта.")[1].split("»")[0]
        for issue in hits(run(rules(text)))
    )
    assert names == ["Договор", "Контрагент", "Свойство"]


@pytest.mark.parametrize(
    "text",
    [
        'А = ПараметрыОбъекта["Контрагент"];',
        'А = ПараметрыОбъекта.Получить("Контрагент");',
        "Если ПараметрыОбъекта.Количество() > 0 Тогда КонецЕсли;",
        'ПараметрыОбъекта.Вставить("А", 1); ПараметрыОбъекта.Удалить("А");',
        "ПараметрыОбъекта.Очистить();",
        'А = ПараметрыОбъекта.Get("А"); ПараметрыОбъекта.Insert("Б", 2);',
        "Н = ПараметрыОбъекта.Count();",
        "Если ПараметрыОбъекта = Неопределено Тогда Возврат; КонецЕсли;",
        "А = МоиПараметрыОбъекта.Имя; Б = Структура.ПараметрыОбъекта.Имя;",
    ],
)
def test_mapping_methods_and_lookalikes_are_clean(text: str) -> None:
    report = run(rules(text))
    assert hits(report) == []
    assert skips(report) == []


@pytest.mark.parametrize(
    "text",
    [
        "// А = ПараметрыОбъекта.Контрагент;",
        "А = 1; // ПараметрыОбъекта.Контрагент",
        'Сообщить("ПараметрыОбъекта.Контрагент");',
        'Текст = "ВЫБРАТЬ\n|ПараметрыОбъекта.Контрагент\n|ИЗ Т";',
    ],
)
def test_comment_and_string_are_not_code(text: str) -> None:
    assert hits(run(rules(text))) == []


def test_disabled_rule_is_not_checked() -> None:
    xml = rules("А = ПараметрыОбъекта.Контрагент;", attrs=' Отключить="true"')
    assert hits(run(xml)) == []


def test_algorithm_text_is_checked() -> None:
    algorithm = (
        '<Алгоритм Имя="Разбор"><Текст>'
        "А = ПараметрыОбъекта.Контрагент;</Текст><Параметры>Ссылка</Параметры></Алгоритм>"
    )
    found = hits(run(rules(algorithms=algorithm)))
    assert len(found) == 1
    assert "Разбор" in found[0].address


def test_own_assignment_skips_the_handler() -> None:
    text = 'ПараметрыОбъекта = Новый Структура("Контрагент", 1);\nА = ПараметрыОбъекта.Контрагент;'
    report = run(rules(text))
    assert hits(report) == []
    reasons = skips(report)
    assert len(reasons) == 1
    assert "ПКО «Спр»" in reasons[0]
    assert "присваивает" in reasons[0]


def test_comparison_is_not_an_assignment() -> None:
    text = "Если ПараметрыОбъекта = Неопределено Тогда КонецЕсли;\nА = ПараметрыОбъекта.Контрагент;"
    report = run(rules(text))
    assert len(hits(report)) == 1
    assert skips(report) == []


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # Комментарий после многострочного литерала — не код.
        ('Т = "строка\n|продолжение"; // ПараметрыОбъекта.Имя', []),
        # `//` внутри литерала не обрывает его и не прячет следующий код.
        ('Т = "А\n|http://х.ру";\nСообщить(ПараметрыОбъекта.Имя);\nБ = "к";', ["Имя"]),
        # Строка после многострочного литерала остаётся строкой.
        ('Т = "А\n|Б";\nВ = "текст ПараметрыОбъекта.Имя";', []),
        # Комментарий между строками литерала в литерал не входит.
        ('Т = "А\n// ПараметрыОбъекта.Тихо "\n|Б";\nВ = ПараметрыОбъекта.Имя;', ["Имя"]),
        ('Т = "кавычка "" ПараметрыОбъекта.Имя";', []),
    ],
)
def test_multiline_literals_and_comments(text: str, expected: list[str]) -> None:
    found = hits(run(rules(text)))
    assert [issue.message.split("«ПараметрыОбъекта.")[1].split("»")[0] for issue in found] == (
        expected
    )


@pytest.mark.parametrize(
    "text",
    [
        "А = ?(ПараметрыОбъекта = Неопределено, Неопределено, ПараметрыОбъекта.Вид);",
        "Если (ПараметрыОбъекта = Неопределено) Тогда КонецЕсли; А = ПараметрыОбъекта.Вид;",
        "Ф(1, ПараметрыОбъекта = Неопределено); А = ПараметрыОбъекта.Вид;",
        "Ф = ПараметрыОбъекта = Неопределено; А = ПараметрыОбъекта.Вид;",
    ],
)
def test_comparisons_do_not_switch_the_check_off(text: str) -> None:
    report = run(rules(text))
    assert len(hits(report)) == 1
    assert skips(report) == []


@pytest.mark.parametrize(
    "prefix",
    [
        "Попытка\n",
        "Попытка А = 1; Исключение\n",
        "Если Истина Тогда\n",
        "Если Истина Тогда А = 1; Иначе\n",
        "Для Каждого Э Из М Цикл\n",
        "If True Then\n",
        "Try\n",
        "#Если Сервер Тогда\n",
        "~Метка:\n",
        "",
    ],
)
def test_assignment_after_statement_keywords(prefix: str) -> None:
    text = f'{prefix}ПараметрыОбъекта = Новый Структура("А", 1);\nБ = ПараметрыОбъекта.А;'
    report = run(rules(text))
    assert hits(report) == []
    assert len(skips(report)) == 1


def test_assignment_in_another_handler_or_algorithm_skips_everywhere() -> None:
    report = run(
        rules(
            "Если ПараметрыОбъекта.Флаг Тогда КонецЕсли;",
            on_load='ПараметрыОбъекта = Новый Структура("Флаг", Истина);',
        )
    )
    assert hits(report) == []
    reasons = skips(report)
    assert len(reasons) == 1
    assert "«ПослеЗагрузки»" in reasons[0] and "«ПриЗагрузке»" in reasons[0]
    algorithm = (
        '<Алгоритм Имя="Инит"><Текст>ПараметрыОбъекта = Новый Структура;</Текст>'
        "<ИспользуетсяПриЗагрузке>true</ИспользуетсяПриЗагрузке></Алгоритм>"
    )
    report = run(rules("А = ПараметрыОбъекта.Флаг;", algorithms=algorithm))
    assert hits(report) == []
    assert len(skips(report)) == 1


@pytest.mark.parametrize(
    "text",
    ["А = Структура . ПараметрыОбъекта.Имя;", "А = Структура.\nПараметрыОбъекта.Имя;"],
)
def test_member_of_another_object_is_not_ours(text: str) -> None:
    assert hits(run(rules(text))) == []


def test_check_is_listed_in_docs() -> None:
    doc = (ROOT / "docs" / "checks.md").read_text(encoding="utf-8")
    assert f"| `{OBJECT_PARAMS_DOT}` | E |" in doc


@pytest.mark.corpus
@pytest.mark.parametrize("item", corpus_params(EXCHANGE_KINDS))
def test_corpus_has_no_dot_access(item: CorpusFile) -> None:
    document = load_rules(item.path)
    assert isinstance(document, ExchangeRules)
    report = check_exchange_plan(document, None, None)
    assert hits(report) == []
