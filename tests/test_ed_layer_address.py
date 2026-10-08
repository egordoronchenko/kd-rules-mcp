"""Адреса действующего слоя и покрытие исходников расширения."""

from itertools import pairwise
from pathlib import Path

import pytest

from kd_rules_mcp.ed.layer_address import (
    AmbiguousLayerAddress,
    LayerAddressNotFound,
    build_layer_addresses,
)
from kd_rules_mcp.ed.layer_model import EntityState, LayerDescriptor
from kd_rules_mcp.ed.layer_reader import read_extension_text
from kd_rules_mcp.ed.layers import compose_manager, read_layers
from kd_rules_mcp.ed.model import Classification, ObjectRule, PropertyRule

ROOT = Path(__file__).resolve().parent / "data" / "ed" / "layers"


@pytest.fixture(scope="module")
def kit_b():
    return read_layers(ROOT / "base", [ROOT / "b"])


def test_kit_b_addresses(kit_b):
    index = build_layer_addresses(kit_b)
    current = index.find("ПКО/Товар").version
    assert current is not None
    assert current.state == EntityState.CHANGED
    code = index.find("Действующее/ПКО/Товар/ПКС/Code")
    assert isinstance(code.entity, PropertyRule)
    assert code.entity.format_property == "Code"
    base_version = index.find("Слой/base/ПКО/Товар").version
    assert base_version is not None
    assert base_version.state == EntityState.BASE
    changed = index.find("Слой/L01-ДемоB/ПКО/Товар")
    assert changed.version is not None
    assert changed.version.state == EntityState.CHANGED
    assert changed.version.changes
    handler = index.find("Слой/L01-ДемоB/Обработчик/Доп_Заказ_Отправка").routine
    assert handler is not None
    assert handler.name == "Доп_Заказ_Отправка"
    hook = index.find("Слой/L01-ДемоB/Перехват/Доп_Диспетчер").hook
    assert hook is not None
    assert hook.routine.name == "Доп_Диспетчер"
    assert any(address.startswith("Слой/L01-ДемоB/Операция/") for address in index.by_address)
    send = next(
        item for item in kit_b.contexts if item.direction == "send" and not item.headers_only
    )
    assert "ПКО/Товар" in send.addresses
    assert "Действующее/ПКО/Товар/ПКС/Code" in send.addresses


@pytest.mark.parametrize("same_name", [False, True])
def test_direction_payloads_keep_separate_layer_addresses(same_name: bool):
    base = read_layers(ROOT / "base")
    layer = LayerDescriptor("L01-ДемоB", 1, "ДемоB", "mem", None, "")
    source = """\
&После("ЗаполнитьПравилаКонвертацииОбъектов")
Процедура Доп(НаправлениеОбмена, ПравилаКонвертации)
    Правило = ПравилаКонвертации.Найти("Товар", "ИмяПКО");
    Если НаправлениеОбмена = "Отправка" Тогда
        ДобавитьПКС(Правило.Свойства, "Отправка", "SendOnly");
    Иначе
        ДобавитьПКС(Правило.Свойства, "Получение", "ReceiveOnly");
    КонецЕсли;
КонецПроцедуры
"""
    if same_name:
        source = source.replace('"SendOnly"', '"Same"').replace('"ReceiveOnly"', '"Same"')
    reading = read_extension_text(
        source,
        layer=layer,
        version=2,
        helpers=frozenset({"добавитьпкс"}),
        targets={item.name.casefold(): item for item in base.base.routines},
    )
    layered = compose_manager(base.base, readings=[reading], layers=(base.layers[0], layer))
    index = build_layer_addresses(layered)
    send = next(item for item in layered.contexts if item.direction == "send")
    receive = next(item for item in layered.contexts if item.direction == "receive")
    send_layer = [
        item
        for item in send.addresses
        if item.startswith("Слой/L01-ДемоB/ПКО/Товар") and "/ПКС/" not in item
    ]
    receive_layer = [
        item
        for item in receive.addresses
        if item.startswith("Слой/L01-ДемоB/ПКО/Товар") and "/ПКС/" not in item
    ]
    assert len(send_layer) == 1
    assert len(receive_layer) == 1
    assert send_layer[0] != receive_layer[0]
    send_rule = index.find(send_layer[0]).version
    receive_rule = index.find(receive_layer[0]).version
    assert send_rule is not None and receive_rule is not None
    assert isinstance(send_rule.payload, ObjectRule)
    assert isinstance(receive_rule.payload, ObjectRule)
    if same_name:
        assert send_rule.payload.properties[-1].configuration_property == "Отправка"
        assert receive_rule.payload.properties[-1].configuration_property == "Получение"
    else:
        assert {prop.format_property for prop in send_rule.payload.properties} >= {"SendOnly"}
        assert "SendOnly" not in {prop.format_property for prop in receive_rule.payload.properties}
        assert {prop.format_property for prop in receive_rule.payload.properties} >= {"ReceiveOnly"}


def test_escape_duplicate_and_deleted_addresses():
    base = read_layers(ROOT / "base")
    layer = LayerDescriptor("L01-ДемоB", 1, "ДемоB", "mem", None, "")
    reading = read_extension_text(
        """\
&После("ЗаполнитьПравилаКонвертацииОбъектов")
Процедура Доп(НаправлениеОбмена, ПравилаКонвертации)
    Первый = ОбменДаннымиXDTOСервер.ИнициализироватьПравилоКонвертацииОбъекта(ПравилаКонвертации);
    Первый.ИмяПКО = "A/B";
    Второй = ОбменДаннымиXDTOСервер.ИнициализироватьПравилоКонвертацииОбъекта(ПравилаКонвертации);
    Второй.ИмяПКО = "Повтор";
    Третий = ОбменДаннымиXDTOСервер.ИнициализироватьПравилоКонвертацииОбъекта(ПравилаКонвертации);
    Третий.ИмяПКО = "Повтор";
    Правило = ПравилаКонвертации.Найти("Товар", "ИмяПКО");
    Если Правило <> Неопределено Тогда
        ПравилаКонвертации.Удалить(Правило);
    КонецЕсли;
КонецПроцедуры
""",
        layer=layer,
        version=2,
        helpers=frozenset({"добавитьпкс"}),
        targets={item.name.casefold(): item for item in base.base.routines},
    )
    layered = compose_manager(base.base, readings=[reading], layers=(base.layers[0], layer))
    index = build_layer_addresses(layered)
    escaped = index.find("ПКО/A%2FB")
    assert isinstance(escaped.entity, ObjectRule)
    assert escaped.entity.name == "A/B"
    with pytest.raises(AmbiguousLayerAddress) as error:
        index.find("ПКО/Повтор")
    assert len(error.value.candidates) == 2
    assert error.value.candidates[0].endswith("#1")
    assert error.value.candidates[1].endswith("#2")
    with pytest.raises(LayerAddressNotFound):
        index.find("ПКО/Товар")
    historical = index.find("Слой/base/ПКО/Товар")
    assert historical.version is not None
    assert historical.version.state == EntityState.DELETED or any(
        item.state == EntityState.DELETED
        and item.payload is not None
        and item.payload.name == "Товар"
        for item in layered.revisions
    )
    deleted = index.find("Слой/L01-ДемоB/ПКО/Товар")
    assert deleted.version is not None
    assert deleted.version.state == EntityState.DELETED


def test_spans_belong_to_their_files_and_coverage_has_no_gaps(kit_b):
    send = next(
        item for item in kit_b.contexts if item.direction == "send" and not item.headers_only
    )
    tip = next(
        item
        for item in send.entities
        if isinstance(item.payload, ObjectRule) and item.payload.name == "Товар"
    )
    assert tip.payload is not None
    assert tip.payload.span == kit_b.base.pko[0].span
    assert tip.payload.raw_text == kit_b.base.pko[0].raw_text
    added = next(prop for prop in tip.payload.properties if prop.format_property == "Code")
    source = next(item for item in kit_b.source_files if item.file_id == added.span.file_id)
    assert source.text[added.span.char_start : added.span.char_end] == added.raw_text
    folder = str(Path("layers") / "b" / "CommonModules")
    assert folder in source.path
    base_source = kit_b.base.files[0]
    old = next(prop for prop in tip.payload.properties if prop.format_property == "Description")
    assert base_source.text[old.span.char_start : old.span.char_end] == old.raw_text
    coverage = next(item for file_id, item in kit_b.coverage if file_id == source.file_id)
    segments = coverage.segments
    assert segments[0].span.char_start == 0
    assert segments[-1].span.char_end == len(source.text)
    for left, right in pairwise(segments):
        assert left.span.char_end == right.span.char_start
    assert any(segment.classification == Classification.UNKNOWN for segment in segments) or any(
        segment.classification == Classification.DECLARATIVE for segment in segments
    )
    assert (
        added.span.char_end <= tip.payload.span.char_end
        or added.span.file_id != tip.payload.span.file_id
    )
