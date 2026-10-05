"""Каркас проверок: отчёт и адреса правил (спецификация `rules-validation`, «Отчёт проверки»)."""

from pathlib import Path

from kd2_rules_mcp.kd2.rules_io import load_exchange_rules
from kd2_rules_mcp.validation.address import pks_address, pkz_address, rule_address, walk_pks
from kd2_rules_mcp.validation.report import Level, ValidationReport

DATA = Path(__file__).parent / "data"


def test_report_levels_and_summary() -> None:
    report = ValidationReport()
    assert report.summary() == "Ошибок и предупреждений нет"
    report.warning("structure.pkz_coverage", "ПКО «Виды»", "Непокрытые значения: А")
    assert not report.has_errors
    assert report.summary() == "Ошибок нет, предупреждений: 1"
    report.error("format.duplicate_code", "ПКО «Виды»", "Код повторяется")
    assert report.has_errors
    assert [issue.level for issue in report.issues] == [Level.WARNING, Level.ERROR]
    assert report.counts() == {"structure.pkz_coverage": 1, "format.duplicate_code": 1}
    assert report.errors[0].to_dict() == {
        "level": "ошибка",
        "check": "format.duplicate_code",
        "address": "ПКО «Виды»",
        "message": "Код повторяется",
    }


def test_skipped_checks_make_result_incomplete() -> None:
    """Сценарий «Структура приёмника не загружена»: итог не сообщает «ошибок нет» без оговорки."""
    report = ValidationReport()
    report.skip("structure.target", "структура приёмника не загружена")
    other = ValidationReport()
    other.skip("structure.target", "структура приёмника не загружена")
    report.extend(other)
    assert not report.has_errors
    assert report.summary() == (
        "Ошибок и предупреждений нет; не выполнены проверки: structure.target — результат неполный"
    )


def test_information_does_not_count_as_warning_or_error() -> None:
    from kd2_rules_mcp.service.views import report_summary

    report = ValidationReport()
    report.info("ed.schema.value_range", "ПКО/Тест/ПКС/Код", "Проверьте длину значений")
    assert not report.has_errors and not report.warnings
    assert report.issues[0].to_dict()["level"] == "info"
    assert report_summary(report) == {
        "errors": 0,
        "warnings": 0,
        "info": 1,
        "skipped": 0,
        "by_check": {"ed.schema.value_range": 1},
        "text": "Ошибок и предупреждений нет; сведений: 1",
    }


def test_rule_addresses() -> None:
    rules = load_exchange_rules(DATA / "exchange_rules.xml")
    pko = rules.pko()
    assert [rule_address(node) for node in pko] == ["ПКО «Организации»", "ПКО «ВидыОпераций»"]
    assert rule_address(rules.algorithms()[0]) == "алгоритм «Общий»"
    assert rule_address(rules.pvd()[0]) == "ПВД «Организации»"
    properties = pko[0].child("Свойства")
    assert properties is not None
    paths = [path for path, _ in walk_pks(properties)]
    assert paths == ["ИНН"]
    assert pks_address("Организации", paths[0]) == "ПКО «Организации» / ПКС ИНН"
    assert pkz_address("ВидыОпераций", "Начисление") == "ПКО «ВидыОпераций» / ПКЗ Начисление"
