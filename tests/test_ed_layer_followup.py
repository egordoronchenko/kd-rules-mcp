"""Независимая перепроверка слоёв: все входы синтетические."""

from dataclasses import replace
from shutil import copytree

import pytest

from kd2_rules_mcp.ed.address import build_addresses
from kd2_rules_mcp.ed.reader import read_manager_text
from kd2_rules_mcp.ed.refs import build_references
from kd2_rules_mcp.service import Kd2Service, Settings, ed_layers
from kd2_rules_mcp.validation.ed_layers import validate_effective_links
from kd2_rules_mcp.validation.ed_links import validate_links
from tests.test_ed_layers import ROOT, _overlay
from tests.test_validation_ed_links import _pod_handler

MODULE = "CommonModules/МенеджерДемо/Ext/Module.bsl"
ADDED_RULE = """&После("ЗаполнитьПравилаКонвертацииОбъектов")
Процедура Доп_Правила(НаправлениеОбмена, ПравилаКонвертации)
 Правило = ОбменДаннымиXDTOСервер.ИнициализироватьПравилоКонвертацииОбъекта(ПравилаКонвертации);
 Правило.ИмяПКО = "ДопТовар";
 Правило.ОбъектДанных = Метаданные.Справочники.ДопТовары;
 Правило.ОбъектФормата = "Справочник.ДопТовар";
 ДобавитьПКС(Правило.Свойства, "Код", "Code");
 ДобавитьПКС(Правило.Свойства, "Наименование", "Description");
 ДобавитьПКС(Правило.Свойства, "ДопКод", "ExtraCode", , , "urn:demo:extra");
 ДобавитьПКС(Правило.Свойства, "ДопИмя", "ExtraName", , , "urn:demo:extra");
КонецПроцедуры
"""


@pytest.fixture
def service(tmp_path):
    return Kd2Service(Settings(cache_dir=tmp_path / "cache", workspace=tmp_path / "workspace"))


def open_layer(service, tmp_path, text, base_text=None):
    root = tmp_path / "kits"
    copytree(ROOT, root)
    (root / "b" / MODULE).write_text(text, encoding="utf-8")
    if base_text is not None:
        (root / "base" / MODULE).write_text(base_text, encoding="utf-8")
    return service.ed_open(
        path=str(root / "base" / MODULE),
        configuration_path=str(root / "base"),
        extensions=[str(root / "b")],
    )


def test_added_rule_property_owners_and_object_filters(service, tmp_path):
    opened = open_layer(service, tmp_path, ADDED_RULE)
    ident = opened["project_id"]
    snap = service._ed_project(ident).layered
    assert snap is not None
    for view in snap.contexts:
        rule = next(r for r in view.document.pko if r.name == "ДопТовар")
        assert len(rule.properties) == 4
        assert all(p.owner_id == rule.entity_id for p in rule.properties)
    metadata = service.ed_list(ident, "pks", metadata_object="Справочник.ДопТовары")
    format_rows = service.ed_list(ident, "pks", format_object="Справочник.ДопТовар")
    assert metadata["total"] == format_rows["total"] == 4
    assert {r["entity_id"] for r in metadata["items"]} == {
        r["entity_id"] for r in format_rows["items"]
    }
    assert sum(r.get("namespace") == "urn:demo:extra" for r in metadata["items"]) == 2
    parent = service.ed_get(ident, "ПКО/ДопТовар", children_kind="pks")
    assert parent["children"]["total"] == 4
    assert {r["name"] for r in parent["children"]["items"]} == {
        r["name"] for r in metadata["items"]
    }


@pytest.mark.parametrize("uncertain_owner", [False, True])
def test_receive_body_hook_is_listed_and_gettable(service, tmp_path, uncertain_owner):
    base = (
        (ROOT / "base" / MODULE)
        .read_text("utf-8")
        .replace("ПриОтправкеДанных", "ПередЗаписьюПолученныхДанных")
    )
    base = base.replace(
        "ДанныеИБ, ДанныеXDTO, КомпонентыОбмена, СтекВыгрузки",
        "ПолученныеДанные, ДанныеИБ, КонвертацияСвойств, КомпонентыОбмена",
    ).replace(
        "Параметры.ДанныеИБ, Параметры.ДанныеXDTO, "
        "Параметры.КомпонентыОбмена, Параметры.СтекВыгрузки",
        "Параметры.ПолученныеДанные, Параметры.ДанныеИБ, "
        "Параметры.КонвертацияСвойств, Параметры.КомпонентыОбмена",
    )
    text = """&После("ПКО_Товар_ПередЗаписьюПолученныхДанных")
Процедура Доп_Получение(ПолученныеДанные, ДанныеИБ, КонвертацияСвойств, КомпонентыОбмена)
 Сообщить("Демо");
КонецПроцедуры
"""
    if uncertain_owner:
        text += """&После("ДобавитьПКО_Товар")
Процедура Доп_Построитель(ПравилаКонвертации)
 НеизвестныйКод(ПравилаКонвертации);
КонецПроцедуры
"""
    opened = open_layer(service, tmp_path, text, base)
    ident = opened["project_id"]
    snap = service._ed_project(ident).layered
    assert snap is not None
    layer_id = snap.manager.layers[1].id
    receive = next(
        c for c in snap.manager.contexts if c.direction == "receive" and not c.headers_only
    )
    assert {
        v.certainty for v in receive.entities if getattr(v.payload, "name", None) == "Товар"
    } == {"unknown" if uncertain_owner else "known"}
    changes = service.ed_list(ident, "change")["items"]
    assert any(r["name"] == "Доп_Получение" for r in changes)
    listed = service.ed_list(ident, "handler", layer=layer_id, direction="receive")
    assert [r["name"] for r in listed["items"]] == ["Доп_Получение"]
    assert [r["name"] for r in service.ed_list(ident, "handler", layer=layer_id)["items"]] == [
        "Доп_Получение"
    ]
    handler = service.ed_get(ident, "Обработчик/Доп_Получение", direction="receive")
    assert handler["layer_id"] == layer_id and handler["state"] == "added"
    assert service.ed_get(ident, "Обработчик/Доп_Получение")["layer_id"] == layer_id
    assert handler["span"]["file_id"].startswith(layer_id + ":")
    historical = service.ed_get(ident, listed["items"][0]["address"], direction="receive")
    assert historical["fields"] == handler["fields"]
    assert not service.ed_list(ident, "handler", layer=layer_id, direction="send")["items"]


@pytest.mark.parametrize("missing_branch", [False, True])
def test_external_base_dispatcher_preserves_untouched_issues_and_skips(missing_branch):
    text = _pod_handler("ИспользованиеПКО.Товар = Ложь;").replace(
        'ПравилоОбработки.ИспользуемыеПКО.Добавить("Товар");', ""
    )
    text = text.replace(
        "ОбработатьПОД(Параметры);\n    КонецЕсли;",
        "ОбработатьПОД(Параметры);\n    КонецЕсли;\n"
        "ВнешнийМодуль.Дополнить(ИмяПроцедуры, Параметры);",
        1,
    )
    if missing_branch:
        text = text.replace(
            'ПравилоОбработки.ПриОбработке = "ОбработатьПОД";',
            'ПравилоОбработки.ПриОбработке = "БезВетки";',
        )
    document = read_manager_text(text)
    assert any(u.reason == "unsupported_dispatcher_statement" for u in document.unknown)
    before = validate_links(document, build_addresses(document), build_references(document))
    layered = _overlay(
        document,
        '&После("ЗаполнитьПравилаКонвертацииОбъектов")\n'
        "Процедура Доп_ПКО(НаправлениеОбмена, ПравилаКонвертации)\nКонецПроцедуры",
    )
    reports = [validate_effective_links(layered, c) for c in layered.contexts]
    assert {i for report in reports for i in report.issues} == set(before.issues)
    assert {s for report in reports for s in report.skipped} == set(before.skipped)


def test_unknown_layer_builder_still_skips_only_affected_rule():
    text = _pod_handler("").replace(
        "ОбработатьПОД(Параметры);\n    КонецЕсли;",
        "ОбработатьПОД(Параметры);\n    КонецЕсли;\n"
        "ВнешнийМодуль.Дополнить(ИмяПроцедуры, Параметры);",
        1,
    )
    layered = _overlay(
        read_manager_text(text),
        '&После("ДобавитьПКО_Товар")\n'
        "Процедура Доп_Построитель(ПравилаКонвертации)\n"
        " НеизвестныйКод(ПравилаКонвертации);\nКонецПроцедуры",
    )
    reports = [validate_effective_links(layered, c) for c in layered.contexts]
    handler_skips = {
        s for report in reports for s in report.skipped if s.check == "ed.handler.missing"
    }
    assert handler_skips and all("ПКО/Товар" in s.reason for s in handler_skips)
    assert not any(
        s.check in {"ed.dispatcher.target_missing", "ed.deferred.argument"}
        for report in reports
        for s in report.skipped
    )


def test_opaque_layer_dispatcher_still_makes_its_paths_unknown():
    document = read_manager_text(_pod_handler(""))
    layered = _overlay(
        document,
        '&После("ВыполнитьПроцедуруМодуляМенеджера")\n'
        "Процедура Доп_Диспетчер(ИмяПроцедуры, Параметры)\n"
        " НеизвестныйКод(ИмяПроцедуры, Параметры);\nКонецПроцедуры",
    )
    chains = [chain for c in layered.contexts for chain in c.dispatch_chains]
    assert chains and all(chain.resolution == "unknown" for chain in chains)
    reports = [validate_effective_links(layered, c) for c in layered.contexts]
    assert any(
        s.check == "ed.handler.missing" and "Определённость unknown" in s.reason
        for report in reports
        for s in report.skipped
    )
    assert any(
        s.check == "ed.dispatcher.target_missing" and "Цепочка диспетчера неизвестна" in s.reason
        for report in reports
        for s in report.skipped
    )


def test_change_revision_prefix_is_added_once_and_portable_ids_are_stable(service, tmp_path):
    opened = open_layer(service, tmp_path, ADDED_RULE)
    ident = opened["project_id"]
    snap = service._ed_project(ident).layered
    assert snap is not None
    rows = service.ed_list(ident, "change", direction="send")["items"]
    assert rows
    layer_id = snap.manager.layers[1].id
    assert all(r["revision_id"].count(layer_id + ":") == 1 for r in rows)
    assert ed_layers.portable_files(snap.manager) == snap.manager
    assert len({v.revision_id for v in snap.manager.contexts[0].entities}) == len(
        snap.manager.contexts[0].entities
    )
    parent = next(r for r in rows if r["name"] == "ДопТовар")
    version = next(v for v in snap.manager.revisions if v.revision_id == parent["revision_id"])
    assert version.payload is not None and parent["entity_id"] == version.payload.entity_id
    rebuilt = replace(snap, changes=ed_layers.change_index(snap.manager, list(snap.contexts)))
    assert rebuilt.changes == snap.changes
