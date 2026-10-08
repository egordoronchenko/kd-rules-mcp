"""Представления ответов сервиса: страницы, строки правил, итоги проверки и правки."""

from pathlib import Path

import pytest

from kd_rules_mcp.authoring.edits import EditResult
from kd_rules_mcp.errors import Kd2Error
from kd_rules_mcp.kd2.model import Node
from kd_rules_mcp.kd2.rules_io import load_exchange_rules
from kd_rules_mcp.service.views import (
    TEXT_LIMIT,
    counts,
    edit_view,
    group_paths,
    node_view,
    overview_groups,
    page_view,
    report_summary,
    rule_row,
    slice_rows,
)
from kd_rules_mcp.structures.queries import MAX_LIMIT, Page
from kd_rules_mcp.validation.report import ValidationReport

DATA = Path(__file__).parent / "data"


def test_slice_rows_middle_page_reports_has_more() -> None:
    page = slice_rows(list("abcde"), 1, 2)
    assert page == {
        "items": ["b", "c"],
        "total": 5,
        "offset": 1,
        "limit": 2,
        "has_more": True,
    }


def test_slice_rows_tail_and_offset_past_end() -> None:
    last = slice_rows([1, 2, 3, 4], 3, 10)
    assert last["items"] == [4]
    assert last["offset"] == 3
    assert last["limit"] == 10
    assert last["has_more"] is False
    empty = slice_rows([1, 2, 3, 4], 4, 2)
    assert empty["items"] == []
    assert empty["total"] == 4
    assert empty["has_more"] is False


def test_slice_rows_rejects_negative_offset_and_nonpositive_limit() -> None:
    with pytest.raises(Kd2Error, match="отрицательным"):
        slice_rows([1], -1, 1)
    with pytest.raises(Kd2Error, match="положительным"):
        slice_rows([1], 0, 0)


def test_slice_rows_clamps_limit_to_max() -> None:
    rows = list(range(MAX_LIMIT + 5))
    page = slice_rows(rows, 0, MAX_LIMIT + 50)
    assert page["limit"] == MAX_LIMIT
    assert page["items"] == rows[:MAX_LIMIT]
    assert page["total"] == MAX_LIMIT + 5
    assert page["has_more"] is True


def test_page_view_copies_window_including_has_more() -> None:
    first = Page(items=[{"name": "А"}, {"name": "Б"}], total=3, offset=0, limit=2)
    assert page_view(first) == {
        "items": [{"name": "А"}, {"name": "Б"}],
        "total": 3,
        "offset": 0,
        "limit": 2,
        "has_more": True,
    }
    last = Page(items=[{"name": "В"}], total=3, offset=2, limit=2)
    view = page_view(last)
    assert view["items"] == [{"name": "В"}]
    assert view["has_more"] is False


def test_rule_row_from_sample_exchange() -> None:
    rules = load_exchange_rules(DATA / "exchange_rules.xml")
    pko = rule_row(rules.pko()[0])
    assert pko["address"] == "ПКО «Организации»"
    assert pko["code"] == "Организации"
    assert pko["Наименование"] == "Справочник: Организации"
    assert pko["Источник"] == "СправочникСсылка.Организации"
    assert pko["Приемник"] == "СправочникСсылка.Организации"
    assert pko["pks_count"] == 1
    pvd = rule_row(rules.pvd()[0])
    assert pvd["address"] == "ПВД «Организации»"
    assert pvd["КодПравилаКонвертации"] == "Организации"
    assert pvd["ОбъектВыборки"] == "СправочникСсылка.Организации"
    assert "Отключить" not in pvd


def test_node_view_clips_long_field_and_keeps_short_one() -> None:
    rules = load_exchange_rules(DATA / "exchange_rules.xml")
    node = rules.pko()[0]
    node.values["ПередВыгрузкой"] = "Я" * (TEXT_LIMIT + 1)
    node.values["ПослеЗагрузки"] = "Б" * TEXT_LIMIT
    view = node_view(node, limit=10)
    clipped = view["fields"]["ПередВыгрузкой"]
    assert clipped == "Я" * TEXT_LIMIT + " …[обрезано]"
    assert view["fields"]["ПослеЗагрузки"] == "Б" * TEXT_LIMIT
    assert view["fields"]["Код"] == "Организации"
    assert view["properties"]["total"] == 1
    assert view["properties"]["items"][0]["path"] == "ИНН"
    assert view["properties"]["items"][0]["search"] is True


def test_counts_sections_of_sample_exchange() -> None:
    rules = load_exchange_rules(DATA / "exchange_rules.xml")
    assert counts(rules) == {
        "pko": 2,
        "pvd": 1,
        "pod": 0,
        "algorithms": 1,
        "queries": 0,
        "parameters": 1,
        "conversion": 1,
    }


def test_report_summary_counts_error_warning_and_skipped() -> None:
    report = ValidationReport()
    report.error("format.duplicate_code", "ПКО «Виды»", "Код повторяется")
    report.warning("structure.pkz_coverage", "ПКО «Виды»", "Непокрытые значения: А")
    report.skip("structure.target", "структура приёмника не загружена")
    summary = report_summary(report)
    assert summary["errors"] == 1
    assert summary["warnings"] == 1
    assert summary["skipped"] == 1
    assert summary["by_check"] == {
        "format.duplicate_code": 1,
        "structure.pkz_coverage": 1,
    }
    assert summary["text"] == (
        "Ошибок: 1, предупреждений: 1; не выполнены проверки: structure.target — результат неполный"
    )


def test_edit_view_omits_empty_lists() -> None:
    assert edit_view(EditResult(address="ПКО «Организации»")) == {"address": "ПКО «Организации»"}


def test_group_path_on_nested_pko_row_view_and_overview() -> None:
    rules = load_exchange_rules(DATA / "exchange_rules.xml")
    section = rules.root.children["ПравилаКонвертацииОбъектов"]
    catalogs = section.items[0]
    assert catalogs.is_group and catalogs.code == "Справочники"
    inner = Node.new("pko_group", "Группа")
    inner.values["Код"] = "Основные"
    nested = Node.new("pko", "Правило")
    nested.values["Код"] = "Банки"
    inner.items.append(nested)
    catalogs.items.append(inner)
    top = Node.new("pko", "Правило")
    top.values["Код"] = "Контрагенты"
    section.items.append(top)

    paths = group_paths(section)
    nested_row = rule_row(nested, paths[id(nested)])
    assert nested_row["group"] == "Справочники/Основные"
    assert "group" not in rule_row(top, paths[id(top)])
    assert rule_row(inner, paths[id(inner)])["group"] == "Справочники"
    assert "group" not in rule_row(catalogs, paths[id(catalogs)])

    view = node_view(nested, 10, paths[id(nested)])
    assert view["group"] == "Справочники/Основные"
    assert "group" not in node_view(top, 10, paths[id(top)])

    assert overview_groups(rules)["pko"] == [
        {"path": "Справочники", "count": 2},
        {"path": "Справочники/Основные", "count": 1},
    ]
    assert "pvd" not in overview_groups(rules)


def test_node_view_pko_without_properties_has_address_and_empty_list() -> None:
    node = Node.new("pko", "Правило")
    node.values["Код"] = "Новый"
    view = node_view(node, 10)
    assert view["address"] == "ПКО «Новый»"
    assert view["properties"] == {"total": 0, "items": []}
    assert "values" not in view


def test_rule_row_strips_padded_rule_code_and_keeps_model_value() -> None:
    rules = load_exchange_rules(DATA / "exchange_rules.xml")
    pvd = rules.pvd()[0]
    pvd.values["КодПравилаКонвертации"] = "Организации   "
    row = rule_row(pvd)
    assert row["КодПравилаКонвертации"] == "Организации"
    assert pvd.values["КодПравилаКонвертации"] == "Организации   "

    view = node_view(pvd, 10)
    assert view["fields"]["КодПравилаКонвертации"] == "Организации"
    assert pvd.values["КодПравилаКонвертации"] == "Организации   "

    pko = rules.pko()[0]
    properties = pko.child("Свойства")
    assert properties is not None
    pks = next(item for item in properties.items if not item.is_group)
    pks.values["КодПравилаКонвертации"] = "ВидыОпераций  "
    items = node_view(pko, 10)["properties"]["items"]
    assert items[0]["conversion"] == "ВидыОпераций"
    assert pks.values["КодПравилаКонвертации"] == "ВидыОпераций  "

    parameter = Node.new("parameter", "Параметр")
    parameter.attrs["ПравилоКонвертации"] = "Организации   "
    assert node_view(parameter, 10)["attrs"]["ПравилоКонвертации"] == "Организации"
    assert parameter.attrs["ПравилоКонвертации"] == "Организации   "


def test_edit_view_truncates_lists_past_max_limit() -> None:
    warnings = [f"w{index}" for index in range(MAX_LIMIT + 3)]
    view = edit_view(EditResult(address="ПКО «А»", warnings=warnings, disabled=["выкл"]))
    assert view["address"] == "ПКО «А»"
    assert view["warnings"] == warnings[:MAX_LIMIT]
    assert view["warnings_total"] == MAX_LIMIT + 3
    assert view["disabled"] == ["выкл"]
    assert "disabled_total" not in view
    assert "skipped" not in view
