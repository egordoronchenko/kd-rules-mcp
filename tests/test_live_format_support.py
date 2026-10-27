"""Живое чтение объектов формата корреспондента: подставная платформа проверяет код."""

import json
import re

import pytest

from kd_rules_mcp.errors import LiveBaseRefusedError, LiveParseError
from kd_rules_mcp.live.codegen.format_support import format_support_code
from tests.test_live_base_check import setup  # noqa: F401 — фикстура сервиса с песочницей


class Platform:
    """Отвечает как сервер данных и проверяет, что код только читает."""

    def __init__(self, payload):
        self.payload = payload
        self.codes: list[str] = []

    def execute(self, code: str) -> str:
        assert not any(marker in code for marker in ("\r", "\n", "//"))
        assert not re.search(
            r"\b(?:Записать|Удалить|НачатьТранзакцию|УстановитьПривилегированныйРежим)\b", code
        )
        for expression in (
            "ОбменДаннымиПовтИсп.ЭтоПланОбменаXDTO(Имя)",
            "Есть = Метаданные.ПланыОбмена.Найти(Имя) <> Неопределено;",
            "ОбменДаннымиXDTOСервер.ПоддерживаемыеОбъектыФорматаКорреспондента(",
            '"ОтправкаПолучение"',
            "РегистрыСведений.НастройкиОбменаДаннымиXDTO.ЗначениеНастройкиКорреспондента(",
            '"ПоддерживаемыеОбъекты"',
            "Выборка.ВерсияФорматаОбмена",
            "Если Узел = Этот Тогда Продолжить; КонецЕсли;",
        ):
            assert expression in code
        self.codes.append(code)
        return "OK " + json.dumps(self.payload, ensure_ascii=False)


PAYLOAD = {
    "exists": True,
    "xdto": True,
    "nodes": [
        {
            "code": "ZUP",
            "name": "Зарплата",
            "format_version": "1.20",
            "settings_received": True,
            "setup_complete": True,
            "objects": [
                {"version": "1.20", "object": "Справочник.А", "send": True, "receive": False},
                {"version": "1.20", "object": "Справочник.Б", "send": False, "receive": True},
                {"version": "1.8", "object": "Справочник.А", "send": True, "receive": True},
            ],
        }
    ],
}


def test_rows_filters_and_versions(setup):  # noqa: F811
    service, _ = setup
    platform = Platform(PAYLOAD)
    service.live_access_factory = lambda access: platform
    result = service.live_format_support("bp.dev", "План", format_object="Справочник.А")
    node = result["nodes"][0]
    assert result["status"] == "OK" and result["xdto"] is True
    assert node["total"] == 3 and node["versions"] == ["1.20", "1.8"]
    assert [row["version"] for row in node["objects"]["items"]] == ["1.20", "1.8"]
    assert node["node_version_rows"] == 1
    assert "зеркально" in result["note"]
    only = service.live_format_support("bp.dev", "План", version="1.8")
    assert only["nodes"][0]["objects"]["total"] == 1


def test_node_filter_is_in_code():
    code = format_support_code("План", "ZUP")
    assert 'Если СокрЛП(Выборка.Код) <> ("ZUP") Тогда Продолжить; КонецЕсли;' in code
    assert "СокрЛП(Выборка.Код) <>" not in format_support_code("План")


def test_broken_row_is_parse_error(setup):  # noqa: F811
    service, _ = setup
    broken = json.loads(json.dumps(PAYLOAD))
    broken["nodes"][0]["objects"][0]["send"] = "да"
    service.live_access_factory = lambda access: Platform(broken)
    with pytest.raises(LiveParseError):
        service.live_format_support("bp.dev", "План")


def test_production_base_refused(setup):  # noqa: F811
    service, _ = setup
    created = []
    service.live_access_factory = lambda access: created.append(access)
    with pytest.raises(LiveBaseRefusedError, match="не песочница"):
        service.live_format_support("bp.prod", "План")
    assert created == []


def test_plan_required(setup):  # noqa: F811
    service, _ = setup
    with pytest.raises(ValueError, match="плана"):
        service.live_format_support("bp.dev", " ")


def test_missing_plan_is_answer_not_error(setup):  # noqa: F811
    service, _ = setup
    service.live_access_factory = lambda access: Platform(
        {"exists": False, "xdto": False, "nodes": []}
    )
    result = service.live_format_support("bp.dev", "НетТакого")
    assert result["status"] == "OK" and result["exists"] is False and result["nodes"] == []


def test_missing_node_is_reported(setup):  # noqa: F811
    service, _ = setup
    service.live_access_factory = lambda access: Platform(
        {"exists": True, "xdto": True, "nodes": []}
    )
    result = service.live_format_support("bp.dev", "План", node="НЕТ")
    assert result["node_found"] is False
    assert "node_found" not in service.live_format_support("bp.dev", "План")
