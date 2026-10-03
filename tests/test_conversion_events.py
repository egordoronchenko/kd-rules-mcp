"""События конвертации как правило вида conversion: чтение, правка, проверки, дифф."""

from pathlib import Path

import pytest
from lxml import etree

from kd2_rules_mcp.errors import Kd2Error, RuleEditError, RuleNotFoundError
from kd2_rules_mcp.kd2.diff import TEXT_LIMIT
from kd2_rules_mcp.kd2.rules_io import dump_rules, load_exchange_rules
from kd2_rules_mcp.kd2.schema import CONVERSION_EVENTS
from kd2_rules_mcp.server import error_payload
from kd2_rules_mcp.service import Kd2Service, Settings
from kd2_rules_mcp.validation.address import CONVERSION_ADDRESS

DATA = Path(__file__).parent / "data"
EXCHANGE = DATA / "exchange_rules.xml"
_EVENT = "ПередВыгрузкойДанных"
_BEFORE = "ПослеЗагрузкиПравилОбмена"
_AFTER = "ПослеЗагрузкиДанных"


def _service(tmp_path: Path) -> Kd2Service:
    return Kd2Service(Settings(cache_dir=tmp_path / "cache", workspace=tmp_path / "workspace"))


def _open(service: Kd2Service, path: Path | None = None) -> str:
    opened = service.rules_open(str(path or EXCHANGE))
    return str(opened["project_id"])


def _root(raw: bytes) -> etree._Element:
    if raw.startswith(b"\xef\xbb\xbf"):
        raw = raw[3:]
    return etree.fromstring(raw)


def test_rules_get_lists_filled_events_and_header(tmp_path: Path) -> None:
    """Пустой ключ и «Конвертация» — один адрес; в ответе только заполненные события."""
    service = _service(tmp_path)
    project_id = _open(service)
    by_empty = service.rules_get(project_id, "conversion", "", "", 10)
    by_name = service.rules_get(project_id, "conversion", CONVERSION_ADDRESS, "", 10)
    assert by_empty == by_name
    assert by_empty["kind"] == "conversion"
    assert by_empty["address"] == CONVERSION_ADDRESS
    assert [item["name"] for item in by_empty["events"]] == [_EVENT]
    event = by_empty["events"][0]
    assert event["lines"] == 3
    assert "Отказ" in event["text"]
    header = by_empty["header"]
    assert header["ВерсияФормата"] == "2.01"
    assert header["Источник"] == "БП"
    assert header["Приемник"] == "ЗУП"
    assert header["ДатаВремяСоздания"] == "2026-07-07T17:56:45"
    assert header["Ид"] == "63ae3719-2387-40fb-b337-a3a27d2c698f"
    assert service.rules_overview(project_id)["counts"]["conversion"] == 1
    page = service.rules_list(project_id, "conversion", None, 0, 50)
    assert page["total"] == 1
    assert page["items"] == [{"address": CONVERSION_ADDRESS, "events": 1, "names": [_EVENT]}]
    assert service.rules_list(project_id, "conversion", "нет-такого", 0, 50)["total"] == 0


def test_long_event_text_is_clipped_and_lines_count_the_whole(tmp_path: Path) -> None:
    service = _service(tmp_path)
    project_id = _open(service)
    text = "строка\n" * 500
    service.rule_update(project_id, "conversion", "", fields={_EVENT: text})
    event = service.rules_get(project_id, "conversion", "", "", 10)["events"][0]
    assert event["lines"] == 500
    assert len(event["text"]) > TEXT_LIMIT
    assert event["text"].endswith(" …[обрезано]")
    assert event["text"].startswith("строка")


def test_update_adds_in_writer_order_and_empty_string_removes(tmp_path: Path) -> None:
    """Событие встаёт на место писателя КД; остальной документ после снятия события прежний."""
    service = _service(tmp_path)
    project_id = _open(service)
    baseline = service.rules_save(project_id, "base.xml", False)
    # В словаре порядок обратный порядку писателя: в XML решает схема, не порядок полей.
    service.rule_update(
        project_id,
        "conversion",
        CONVERSION_ADDRESS,
        fields={_AFTER: "Б = 2;", _BEFORE: "А = 1;"},
    )
    changed = service.rules_save(project_id, "changed.xml", False)
    raw = Path(changed["path"]).read_bytes()
    tags = [element.tag for element in _root(raw)]
    assert tags.index(_BEFORE) < tags.index(_EVENT) < tags.index(_AFTER) < tags.index("Параметры")
    events = [tag for tag in tags if tag in CONVERSION_EVENTS]
    assert events == [_BEFORE, _EVENT, _AFTER]
    kept = _root(raw).find(_EVENT)
    assert kept is not None and kept.text is not None and "Отказ" in kept.text

    service.rule_update(project_id, "conversion", "", fields={_EVENT: ""})
    removed = service.rules_save(project_id, "removed.xml", False)
    gone = [element.tag for element in _root(Path(removed["path"]).read_bytes())]
    assert _EVENT not in gone
    assert gone.index(_BEFORE) < gone.index(_AFTER)
    view = service.rules_get(project_id, "conversion", "", "", 10)
    assert [item["name"] for item in view["events"]] == [_BEFORE, _AFTER]
    assert service.rules_overview(project_id)["counts"]["conversion"] == 2

    restored = load_exchange_rules(raw)
    restored.root.values.pop(_BEFORE, None)
    restored.root.values.pop(_AFTER, None)
    assert dump_rules(restored) == Path(baseline["path"]).read_bytes()

    again = _open(service, Path(changed["path"]))
    reread = service.rules_get(again, "conversion", CONVERSION_ADDRESS, "", 10)
    assert [item["name"] for item in reread["events"]] == [_BEFORE, _EVENT, _AFTER]
    assert reread["events"][0]["text"] == "А = 1;"


def test_unknown_event_and_header_and_singleton_are_rejected(tmp_path: Path) -> None:
    service = _service(tmp_path)
    project_id = _open(service)
    before = service.rules_get(project_id, "conversion", "", "", 10)

    with pytest.raises(ValueError, match="Допустимые") as unknown:
        service.rule_update(project_id, "conversion", "", fields={"НетТакого": "А = 1;"})
    payload = error_payload(unknown.value)
    assert payload["code"] == "invalid_argument"
    assert _EVENT in payload["message"]
    assert "ПослеЗагрузкиПараметров" in payload["message"]

    with pytest.raises(RuleEditError, match="заголовка") as header:
        service.rule_update(
            project_id,
            "conversion",
            "",
            fields={"ДатаВремяСоздания": "2000-01-01T00:00:00", "Источник": "Другая"},
        )
    assert error_payload(header.value)["code"] == "edit_rejected"
    assert "ДатаВремяСоздания" in str(header.value)

    with pytest.raises(RuleEditError, match="экземпляр") as created:
        service.rule_create(project_id, "conversion", "")
    assert error_payload(created.value)["code"] == "edit_rejected"
    with pytest.raises(RuleEditError, match="пустую строку"):
        service.rule_delete(project_id, "conversion", CONVERSION_ADDRESS, "", None, None)
    with pytest.raises(RuleNotFoundError):
        service.rules_get(project_id, "conversion", "другое", "", 10)

    assert service.rules_get(project_id, "conversion", "", "", 10) == before
    assert service.rules_overview(project_id)["modified"] is False


def test_registration_has_no_conversion_section(tmp_path: Path) -> None:
    service = _service(tmp_path)
    project_id = _open(service, DATA / "registration_rules.xml")
    with pytest.raises(Kd2Error, match="регистрац"):
        service.rules_list(project_id, "conversion", None, 0, 10)


def test_validate_address_opens_with_rules_get(tmp_path: Path) -> None:
    """Адрес замечания — Конвертация, и rules_get по нему читает событие."""
    source = tmp_path / "events.xml"
    source.write_text(
        "<ПравилаОбмена><ВерсияФормата>2.01</ВерсияФормата>"
        "<ПередЗагрузкойДанных>Выполнить(Алгоритмы.НетТакого);</ПередЗагрузкойДанных>"
        "</ПравилаОбмена>",
        encoding="utf-8",
    )
    service = _service(tmp_path)
    project_id = _open(service, source)
    report = service.rules_validate(project_id, None, None, None, "algorithm.", 0, 20)
    issues = report["issues"]["items"]
    assert len(issues) == 1
    assert issues[0]["check"] == "algorithm.missing"
    assert issues[0]["address"] == f"{CONVERSION_ADDRESS} / ПередЗагрузкойДанных"
    key = issues[0]["address"].split(" / ", 1)[0]
    view = service.rules_get(project_id, "conversion", key, "", 10)
    assert view["address"] == CONVERSION_ADDRESS
    assert view["events"][0]["name"] == "ПередЗагрузкойДанных"
    assert "НетТакого" in view["events"][0]["text"]


def test_handlers_export_and_locate_use_conversion_address(tmp_path: Path) -> None:
    service = _service(tmp_path)
    project_id = _open(service)
    exported = service.handlers_export(project_id, "handlers", 50)
    matches = [item for item in exported["files"] if item["event"] == _EVENT]
    assert len(matches) == 1
    assert matches[0]["address"] == CONVERSION_ADDRESS
    file_path = Path(exported["folder"]) / matches[0]["file"]
    lines = file_path.read_text(encoding="utf-8-sig").splitlines()
    assert any(line.startswith("Процедура Конвертация_ПередВыгрузкойДанных(") for line in lines)
    number = next(index for index, line in enumerate(lines, 1) if "Если А <> Б" in line)
    found = service.handlers_locate(project_id, matches[0]["file"], number)
    assert found == {
        "found": True,
        "address": CONVERSION_ADDRESS,
        "event": _EVENT,
        "handler_line": 1,
    }


def test_rules_diff_shows_event_change(tmp_path: Path) -> None:
    service = _service(tmp_path)
    project_id = _open(service)
    left = service.rules_save(project_id, "left.xml", False)
    service.rule_update(project_id, "conversion", "", fields={_EVENT: "Отказ = Истина;"})
    right = service.rules_save(project_id, "right.xml", False)
    diff = service.rules_diff(left["path"], right["path"], False, False, None, 0, 50)
    changes = [item for item in diff["changes"]["items"] if item.get("field") == _EVENT]
    assert len(changes) == 1
    assert changes[0]["section"] == "header"
    assert changes[0]["change"] == "changed"
    assert changes[0]["handler_diff"]
    assert any(line.startswith("-") and "Отказ" in line for line in changes[0]["handler_diff"])
    assert any(
        line.startswith("+") and "Отказ = Истина" in line for line in changes[0]["handler_diff"]
    )
