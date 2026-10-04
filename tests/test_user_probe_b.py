"""Недостающие ответы сервера по прогону «как пользователь» (задача #64, часть Б)."""

from pathlib import Path

import pytest

from kd2_rules_mcp.authoring.correspondent import CONVERSION_NOTE, mirror_rules
from kd2_rules_mcp.authoring.edits import find_rule
from kd2_rules_mcp.authoring.registration import registration_losses, snapshot_registration
from kd2_rules_mcp.errors import AmbiguousAddressError, Kd2Error
from kd2_rules_mcp.kd2.diff import diff_rules
from kd2_rules_mcp.kd2.model import Node, RegistrationRules
from kd2_rules_mcp.kd2.rules_io import dump_rules, load_exchange_rules, load_registration_rules
from kd2_rules_mcp.service import Kd2Service, Settings
from kd2_rules_mcp.validation.address import pks_candidates, pks_segments
from tests.test_authoring_correspondent import _DOCUMENT, _rules

DATA = Path(__file__).parent / "data"
EXCHANGE = DATA / "exchange_rules.xml"
REGISTRATION = DATA / "registration_rules.xml"
DUMP = DATA / "registration" / "dump"


def _service(tmp_path: Path) -> Kd2Service:
    return Kd2Service(Settings(cache_dir=tmp_path / "cache", workspace=tmp_path / "workspace"))


def _pks(source: str, target: str = "", parameter: str = "") -> Node:
    node = Node.new("pks", "Свойство")
    if source:
        side = Node.new("pks_side", "Источник")
        side.attrs["Имя"] = source
        node.children["Источник"] = side
    if target:
        side = Node.new("pks_side", "Приемник")
        side.attrs["Имя"] = target
        node.children["Приемник"] = side
    if parameter:
        node.values["ИмяПараметраДляПередачи"] = parameter
    return node


def _container(*items: Node) -> Node:
    container = Node.new("pks_list", "Свойства")
    container.items.extend(items)
    return container


def test_registration_list_and_get(tmp_path: Path) -> None:
    service = _service(tmp_path)
    project = service.rules_open(str(REGISTRATION))["project_id"]
    listed = service.rules_list(project, "registration", None, 0, 50)
    assert listed["total"] == 1
    row = listed["items"][0]
    assert row["address"] == "ПРО «Справочник.Организации»"
    assert row["ОбъектМетаданныхИмя"] == "Справочник.Организации"
    assert row["code"] == "000000002"
    assert row["group"] == "000000001"
    assert row["disabled"] is False
    assert row["filters"] is True
    assert row["handlers"] is True
    with pytest.raises(Kd2Error, match="доступен раздел «registration»"):
        service.rules_list(project, "pko", None, 0, 50)
    got = service.rules_get(project, "pro", "Справочник.Организации", "", 20)
    assert got["address"] == row["address"]
    assert got["Валидное"] is True
    assert got["ОбъектНастройки"] == "СправочникСсылка.Организации"
    plan = got["ОтборПоСвойствамПланаОбмена"]
    assert plan["operator"] == "И"
    item = plan["items"][0]
    assert item["ВидСравнения"] == "Равно"
    assert item["СвойствоПланаОбмена"] == "[Организации].Организация"
    assert item["СвойствоОбъекта"] == "Ссылка"
    assert item["ТаблицаСвойствОбъекта"][0]["Наименование"] == "Ссылка"
    assert got["handlers"][0]["name"] == "ПослеОбработки"
    assert "Отказ" in got["handlers"][0]["text"]
    with pytest.raises(Kd2Error, match="вид pro"):
        service.rules_get(project, "pko", "Организации", "", 20)


def test_duplicate_registration_object_needs_ordinal(tmp_path: Path) -> None:
    rules = load_registration_rules(REGISTRATION)
    group = rules.section("ПравилаРегистрацииОбъектов").items[0]
    group.items.append(group.items[0])
    path = tmp_path / "reg.xml"
    path.write_bytes(dump_rules(rules))
    service = _service(tmp_path)
    project = service.rules_open(str(path))["project_id"]
    listed = service.rules_list(project, "registration", None, 0, 50)
    assert [item["address"] for item in listed["items"]] == [
        "ПРО «Справочник.Организации»#1",
        "ПРО «Справочник.Организации»#2",
    ]
    second = service.rules_get(project, "pro", "ПРО «Справочник.Организации»#2", "", 10)
    assert second["address"].endswith("#2")
    with pytest.raises(AmbiguousAddressError):
        service.rules_get(project, "pro", "Справочник.Организации", "", 10)


def test_registration_diff_matches_object_not_group() -> None:
    left = load_registration_rules(REGISTRATION)
    right = load_registration_rules(dump_rules(left))
    section = right.section("ПравилаРегистрацииОбъектов")
    group = section.items[0]
    rule = group.items.pop(0)
    section.items.append(rule)
    rule.values["Комментарий"] = "на месте"
    moved = diff_rules(left, right)
    assert [item.change for item in moved if item.field == "group"] == ["moved"]
    move = next(item for item in moved if item.change == "moved")
    assert move.address == "ПРО «Справочник.Организации»"
    assert move.old == "000000001" and move.new == ""
    assert not any(item.change in ("added", "removed") and item.field is None for item in moved)

    changed = load_registration_rules(dump_rules(left))
    node = changed.rules()[0]
    node.child("ОтборПоСвойствамПланаОбмена").items[0].values["ВидСравнения"] = "НеРавно"  # type: ignore[union-attr]
    node.values.pop("ПослеОбработки")
    changes = diff_rules(left, changed)
    by_field = {item.field: item for item in changes}
    assert by_field["ОтборПоСвойствамПланаОбмена"].change == "changed"
    assert by_field["ОтборПоСвойствамПланаОбмена"].old is not None
    assert "Равно" in by_field["ОтборПоСвойствамПланаОбмена"].old
    assert by_field["ОтборПоСвойствамПланаОбмена"].new is not None
    assert "НеРавно" in by_field["ОтборПоСвойствамПланаОбмена"].new
    assert by_field["ПослеОбработки"].change == "removed"


def test_registration_build_warns_without_changing_rules(tmp_path: Path) -> None:
    service = _service(tmp_path)
    service.structure_load_xml("registration", str(DUMP))
    service.registration_build(
        "registration",
        "Обмен",
        None,
        [
            {
                "metadata_name": "Документ.Приход",
                "plan_filters": [
                    {
                        "plan_property": "ДатаНачала",
                        "object_property": "Дата",
                        "comparison": "Равно",
                    }
                ],
            }
        ],
        project_id="reg",
    )
    document = service.workspace.get("reg").document
    assert isinstance(document, RegistrationRules)
    document.rules()[0].values["ПередОбработкой"] = "Отказ = Ложь;"
    before = document.rules()[0].get("ПередОбработкой")
    updated = service.registration_build(
        "registration",
        "Обмен",
        None,
        [{"metadata_name": "Документ.Приход", "plan_filters": []}],
        project_id="reg",
    )
    assert document.rules()[0].get("ПередОбработкой") == before
    assert document.rules()[0].child("ОтборПоСвойствамПланаОбмена") is None
    assert updated["losses"] == [{"address": "ПРО «Документ.Приход»", "filters": 1, "handlers": 0}]
    assert any("отборов" in item for item in updated["warnings"])
    snaps = snapshot_registration(document)
    document.rules()[0].values.pop("ПередОбработкой")
    assert registration_losses(snaps, document)[0]["handlers"] == 1


def test_rules_diff_brief_lists_addresses_without_body(tmp_path: Path) -> None:
    left = load_exchange_rules(EXCHANGE)
    right = load_exchange_rules(dump_rules(left))
    _pko = next(node for node in right.pko() if node.code == "Организации")
    _pko.values["Наименование"] = "Другое"
    _pko.values["Комментарий"] = "ещё"
    saved = tmp_path / "right.xml"
    saved.write_bytes(dump_rules(right))
    service = _service(tmp_path)
    full = service.rules_diff(str(EXCHANGE), str(saved), False, False, None, 0, 50, "полный")
    brief = service.rules_diff(str(EXCHANGE), str(saved), False, False, None, 0, 50, "кратко")
    assert full["detail"] == "full" and brief["detail"] == "brief"
    assert brief["summary"] == full["summary"]
    assert brief["changes"]["total"] == 1
    assert set(brief["changes"]["items"][0]) == {"section", "address", "change"}
    assert "field" in full["changes"]["items"][0]
    with pytest.raises(ValueError, match="Допустимые"):
        service.rules_diff(str(EXCHANGE), str(saved), False, False, None, 0, 50, "short")


def test_added_algorithm_shows_size_and_preview() -> None:
    left = load_exchange_rules(EXCHANGE)
    right = load_exchange_rules(dump_rules(left))
    algorithm = Node.new("algorithm", "Алгоритм")
    algorithm.attrs["Имя"] = "Новый"
    algorithm.values["Текст"] = "Первая\nВторая\nТретья"
    right.section("Алгоритмы").items.append(algorithm)
    added = next(item for item in diff_rules(left, right) if item.change == "added")
    assert added.address == "алгоритм «Новый»"
    assert added.size == len("Первая\nВторая\nТретья")
    assert added.preview is not None and added.preview.startswith("Первая")


def test_pks_search_across_rules(tmp_path: Path) -> None:
    service = _service(tmp_path)
    project = service.rules_open(str(EXCHANGE))["project_id"]
    found = service.rules_list(project, "pks", "инн", 0, 50)
    assert found["total"] == 1
    row = found["items"][0]
    assert row["code"] == "Организации"
    assert row["source"] == "ИНН" and row["target"] == "ИНН"
    assert row["search"] is True and row["disabled"] is False
    assert row["address"] == "ПКО «Организации» / ПКС ИНН"
    assert service.rules_list(project, "pks", "нет-такого", 0, 50)["total"] == 0


def test_empty_target_and_parameter_keep_old_addresses() -> None:
    source_only = _container(_pks("Склад"))
    assert pks_segments(source_only) == ["Склад"]
    assert pks_candidates(source_only, "(Склад)")[0][1] is source_only.items[0]
    parameter = _container(_pks("", parameter="Курс"))
    assert pks_segments(parameter) == ["Курс"]
    assert pks_candidates(parameter, "#1")[0][1] is parameter.items[0]
    both = _container(_pks("А", "Сумма"), _pks("Б", "Сумма"))
    assert pks_segments(both) == ["А→Сумма", "Б→Сумма"]
    assert pks_candidates(both, "Сумма#2")[0][1] is both.items[1]
    rules = load_exchange_rules(
        (
            "<ПравилаОбмена><ВерсияФормата>2.01</ВерсияФормата><Ид>1</Ид><Наименование>Т</Наименование>"
            "<Источник>А</Источник><Приемник>Б</Приемник><ПравилаКонвертацииОбъектов><Правило>"
            "<Код>Док</Код><Свойства>"
            '<Свойство><Источник Имя="Склад" Вид="Реквизит"/>'
            "<ИмяПараметраДляПередачи>Курс</ИмяПараметраДляПередачи></Свойство>"
            "</Свойства></Правило></ПравилаКонвертацииОбъектов></ПравилаОбмена>"
        ).encode()
    )
    found = find_rule(rules, "pks", "(Склад)", "Док")
    assert found.values["ИмяПараметраДляПередачи"] == "Курс"
    assert find_rule(rules, "pks", "Курс", "Док") is found


def test_match_properties_marks_covered_pks(tmp_path: Path) -> None:
    service = _service(tmp_path)
    service.structure_load_xml("registration", str(DUMP))
    project = service.rules_create("registration", "registration", "rules")["project_id"]
    service.rule_create(
        project,
        "pko",
        "Контрагенты",
        source_structure="registration",
        target_structure="registration",
        fields={
            "Источник": "СправочникСсылка.Контрагенты",
            "Приемник": "СправочникСсылка.Контрагенты",
        },
    )
    service.rule_create(
        project,
        "pks",
        "ИНН",
        owner="Контрагенты",
        fields={
            "Источник": {"Имя": "ИНН", "Вид": "Реквизит"},
            "Приемник": {"Имя": "ИНН", "Вид": "Реквизит"},
        },
    )
    plain = service.match_properties(
        "registration",
        "registration",
        "Справочник.Контрагенты",
        "Справочник.Контрагенты",
        None,
        0,
        50,
    )
    assert plain["total"] > 0
    assert "covered" not in plain["items"][0]
    marked = service.match_properties(
        "registration",
        "registration",
        "Справочник.Контрагенты",
        "Справочник.Контрагенты",
        None,
        0,
        50,
        "rules",
        "Контрагенты",
        False,
    )
    by_path = {item["path"]: item for item in marked["items"]}
    assert by_path["ИНН"]["covered"] is True
    assert by_path["ИНН"]["pks"] == ["ПКО «Контрагенты» / ПКС ИНН"]
    uncovered = service.match_properties(
        "registration",
        "registration",
        "Справочник.Контрагенты",
        "Справочник.Контрагенты",
        None,
        0,
        50,
        "rules",
        "Контрагенты",
        True,
    )
    assert all(item["covered"] is False for item in uncovered["items"])
    assert "ИНН" not in {item["path"] for item in uncovered["items"]}
    with pytest.raises(ValueError, match="rules_project_id"):
        service.match_properties(
            "registration",
            "registration",
            "Справочник.Контрагенты",
            "Справочник.Контрагенты",
            None,
            0,
            50,
            None,
            None,
            True,
        )


def test_correspondent_names_the_gap_and_lists_conversion_once() -> None:
    rules = _rules("<ПередЗагрузкойДанных>Отказ = Ложь;</ПередЗагрузкойДанных>" + _DOCUMENT)
    result = mirror_rules(rules, ["Ведомость", "Организации", "Виды"])
    conversion = [item for item in result.handlers if item.address == "Конвертация"]
    assert {item.event for item in conversion} == {"ПередВыгрузкойДанных", "ПередЗагрузкойДанных"}
    assert len(conversion) == 2
    assert {item.note for item in conversion} == {CONVERSION_NOTE}
    assert any("нет ПКО для типа" in note and "Склады" in note for note in result.notes)
