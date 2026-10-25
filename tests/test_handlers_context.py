"""Справка по контексту обработчиков: `handlers_context` и параметры в `handlers_read`."""

from pathlib import Path

import pytest

from kd_rules_mcp.errors import Kd2Error
from kd_rules_mcp.service import Kd2Service, Settings
from kd_rules_mcp.validation.handlers import (
    EVENT_AREAS,
    HANDLER_GLOBALS,
    HANDLER_NOTES,
    HANDLER_PARAMS,
    HANDLER_TEMPLATE_LINES,
)

RULES = """<ПравилаОбмена><ВерсияФормата>2.01</ВерсияФормата>
<ПравилаКонвертацииОбъектов>
 <Правило><Код>Спр</Код><Источник>СправочникСсылка.А</Источник>
  <Приемник>СправочникСсылка.А</Приемник>
  <ПослеЗагрузки>А = ПараметрыОбъекта["Вид"];</ПослеЗагрузки></Правило>
</ПравилаКонвертацииОбъектов>
<Алгоритмы><Алгоритм Имя="Разбор"><Текст>А = 1;</Текст>
<Параметры>Ссылка, Значение</Параметры></Алгоритм></Алгоритмы>
</ПравилаОбмена>"""

REGISTRATION = """<ПравилаРегистрации><ВерсияФормата>2.01</ВерсияФормата>
<ПравилаРегистрацииОбъектов><Правило Валидное="true">
<Код>ПРО1</Код><ОбъектМетаданныхИмя>Документ.Пример</ОбъектМетаданныхИмя>
<ПередОбработкой>Маркер = 1;</ПередОбработкой>
</Правило></ПравилаРегистрацииОбъектов></ПравилаРегистрации>"""


@pytest.fixture
def service(tmp_path: Path) -> Kd2Service:
    return Kd2Service(Settings(cache_dir=tmp_path / "cache", workspace=tmp_path / "work"))


def test_data_every_area_has_template_lines_and_notes_cite_bsp() -> None:
    areas = set(EVENT_AREAS.values())
    assert areas == set(HANDLER_PARAMS) == set(HANDLER_TEMPLATE_LINES)
    assert set(HANDLER_NOTES) <= areas
    for notes in HANDLER_NOTES.values():
        assert notes and all("БСП:" in note for note in notes)
    assert {item.name for item in HANDLER_GLOBALS} >= {
        "Параметры",
        "Алгоритмы",
        "Запросы",
        "УзелДляОбмена",
        "ОбщиеПроцедурыФункции",
    }
    assert all(item.source for item in HANDLER_GLOBALS)


def test_context_one_event(service: Kd2Service) -> None:
    found = service.handlers_context("pko", "ПослеЗагрузки")
    assert found["area"] == "ПКО_ПослеЗагрузкиОбъекта"
    assert found["template_lines"] == "98–100"
    assert found["params"] == [
        "ФайлОбмена",
        "Отказ",
        "Ссылка",
        "Объект",
        "ПараметрыОбъекта",
        "ОбъектМодифицирован",
        "ИмяТипаОбъекта",
        "ОбъектНайден",
    ]
    names = [item["name"] for item in found["globals"]]
    assert "Алгоритмы" in names and "ОбщиеПроцедурыФункции" in names
    assert any("ПрочитатьНаборЗаписейРегистра" in note for note in found["notes"])


def test_context_notes_for_pvd_plan_export(service: Kd2Service) -> None:
    found = service.handlers_context("pvd", "ПередОбработкойПравила")
    assert found["params"][2] == "Правило"
    assert any("ПравилоВыгрузкиДанных" in note for note in found["notes"])
    assert service.handlers_context("pks", "ПриВыгрузке")["notes"] == []


def test_context_list_all_and_by_kind(service: Kd2Service) -> None:
    listed = service.handlers_context()
    assert listed["total"] == len(EVENT_AREAS) == len(listed["items"])
    row = next(item for item in listed["items"] if item["area"] == "ПКО_ПослеЗагрузкиОбъекта")
    assert row == {
        "kind": "pko",
        "handler": "ПослеЗагрузки",
        "area": "ПКО_ПослеЗагрузкиОбъекта",
        "params": 8,
    }
    pks = service.handlers_context("pks")
    assert [item["handler"] for item in pks["items"]] == [
        "ПередВыгрузкой",
        "ПриВыгрузке",
        "ПослеВыгрузки",
    ]


def test_context_area_name_and_unique_tag_without_kind(service: Kd2Service) -> None:
    by_area = service.handlers_context(handler="ПКО_ПоследовательностьПолейПоиска")
    assert (by_area["kind"], by_area["handler"]) == ("pko", "ПоследовательностьПолейПоиска")
    assert service.handlers_context(handler="пко_послезагрузкиобъекта")["kind"] == "pko"
    unique = service.handlers_context(handler="ПослеЗагрузкиПараметра")
    assert unique["kind"] == "parameter"
    assert service.handlers_context(handler="послезагрузкипараметра")["kind"] == "parameter"
    assert service.handlers_context("pko", "послезагрузки")["handler"] == "ПослеЗагрузки"
    by_conversion = service.handlers_context("exchange_rules", "ПослеЗагрузкиОбъекта")
    assert by_conversion["area"] == "Конвертация_ПослеЗагрузкиОбъекта"


@pytest.mark.parametrize(
    ("kind", "handler", "expected"),
    [
        ("pkz", None, "pko"),
        ("pko", "ПриВыгрузкеСвойства", "ПослеЗагрузки"),
        (None, "ПередВыгрузкой", "без вида"),
        (None, "Нет", "без вида"),
        # Событие конвертации и суффикс области ПКО: без вида неоднозначно.
        (None, "ПослеЗагрузкиОбъекта", "без вида"),
        (None, "ПередЗагрузкойОбъекта", "без вида"),
    ],
)
def test_context_unknown_kind_or_event(
    service: Kd2Service, kind: str | None, handler: str | None, expected: str
) -> None:
    with pytest.raises(Kd2Error) as error:
        service.handlers_context(kind, handler)
    assert expected in str(error.value)


def test_read_returns_params(service: Kd2Service, tmp_path: Path) -> None:
    source = tmp_path / "ExchangeRules.xml"
    source.write_text(RULES, encoding="utf-8")
    pid = service.rules_open(str(source))["project_id"]
    read = service.handlers_read(pid, "ПКО «Спр»", "ПослеЗагрузки")
    assert read["params"] == list(HANDLER_PARAMS["ПКО_ПослеЗагрузкиОбъекта"])
    algorithm = service.handlers_search(pid, "А = 1")["items"][0]
    read = service.handlers_read(pid, algorithm["address"], algorithm["handler"])
    assert read["params"] == ["Ссылка", "Значение"]
    registration = tmp_path / "RegistrationRules.xml"
    registration.write_text(REGISTRATION, encoding="utf-8")
    reg_id = service.rules_open(str(registration))["project_id"]
    hit = service.handlers_search(reg_id, "Маркер")["items"][0]
    assert service.handlers_read(reg_id, hit["address"], hit["handler"])["params"] == []
