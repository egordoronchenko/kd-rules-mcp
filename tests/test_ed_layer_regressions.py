"""Регрессии чтения чужих форм: только синтетические модули и метаданные."""

from collections import Counter
from dataclasses import replace
from shutil import copytree
from time import perf_counter

import pytest

from kd2_rules_mcp.ed.address import build_addresses
from kd2_rules_mcp.ed.layers import compose_manager, read_layers
from kd2_rules_mcp.ed.model import ObjectRule
from kd2_rules_mcp.ed.reader import read_manager_text
from kd2_rules_mcp.ed.refs import build_references
from kd2_rules_mcp.ed.route_model import FormatExtension
from kd2_rules_mcp.service import Kd2Service, Settings, ed_layers
from kd2_rules_mcp.validation.ed_layers import validate_effective_links, validate_layers
from kd2_rules_mcp.validation.ed_links import validate_links
from tests import session_inputs
from tests.test_ed_layers import ROOT, _overlay, _send
from tests.test_validation_ed_links import _pko_procedure, _pod_handler, analyze, module_text, put

MODULE = "CommonModules/МенеджерДемо/Ext/Module.bsl"


@pytest.fixture
def service(tmp_path):
    return Kd2Service(Settings(cache_dir=tmp_path / "cache", workspace=tmp_path / "workspace"))


def test_unselected_own_manager_preserves_base_file_and_entities(service, tmp_path):
    root = tmp_path / "kits"
    copytree(ROOT, root)
    extension = root / "b"
    config = extension / "Configuration.xml"
    config.write_text(
        config.read_text("utf-8").replace(
            "</ChildObjects>", "<CommonModule>СвойМенеджер</CommonModule></ChildObjects>"
        ),
        encoding="utf-8",
    )
    (extension / "CommonModules/СвойМенеджер.xml").write_text(
        "<MetaDataObject><CommonModule><Properties><Name>СвойМенеджер</Name>"
        "</Properties></CommonModule></MetaDataObject>",
        encoding="utf-8",
    )
    own = extension / "CommonModules/СвойМенеджер/Ext/Module.bsl"
    own.parent.mkdir(parents=True)
    own.write_bytes((root / "base" / MODULE).read_bytes())
    plain = service.ed_open(
        path=str(root / "base" / MODULE),
        configuration_path=str(root / "base"),
        extensions=[str(extension)],
    )
    files = plain["source_files"]
    assert files[0]["file_id"] == "module"
    assert len({f["file_id"] for f in files}) == len(files)
    assert plain["changes_summary"]["added"] < 20
    ident = plain["project_id"]
    assert service.ed_locate(ident, 1)["file_id"] == "module"
    base = service.ed_get(ident, "Слой/base/ПКО/Товар")
    assert base["span"]["file_id"] == "module" and base["state"] == "base"
    unused = service.ed_overview(ident)["composition"]["unselected_managers"]
    assert len(unused) == 1 and unused[0]["name"] == "СвойМенеджер"
    assert "маршрутом не выбран" in unused[0]["reason"]
    assert not any(
        r["file_id"] == unused[0]["file_id"] for r in service.ed_list(ident, "handler")["items"]
    )


def test_change_page_thousands_is_linear_and_builds_only_page(service, monkeypatch):
    base = read_layers(ROOT / "base")
    context = _send(base)
    sample = context.entities[0]
    assert sample.payload is not None
    revisions = tuple(
        replace(
            sample,
            logical_id=f"id:{i}",
            revision_id=f"rev:{i}",
            payload=replace(sample.payload, entity_id=f"id:{i}", name=f"Товар{i}"),
            state="added",
            layer_id="base",
        )
        for i in range(3000)
    )
    manager = replace(base, revisions=revisions, contexts=(replace(context, entities=revisions),))
    snap = ed_layers.snapshot(manager, ())

    def fail(*args, **kwargs):
        pytest.fail("Индекс адресов повторно строится для страницы")

    monkeypatch.setattr(ed_layers, "build_layer_addresses", fail)
    original_context_row = ed_layers.context_row
    counted_rows = []

    def count_row(context):
        counted_rows.append(context)
        return original_context_row(context)

    monkeypatch.setattr(ed_layers, "context_row", count_row)
    ident = service.ed_open(path=str(ROOT / "base" / MODULE))["project_id"]
    service._ed_projects[ident] = replace(service._ed_project(ident), layered=snap)
    start = perf_counter()
    result = service.ed_list(ident, "change", direction="send", limit=1)
    assert perf_counter() - start < 2.0
    assert result["total"] == 3000 and len(result["items"]) == 1
    assert result["items"][0]["contexts"] == [{"direction": "send", "headers_only": False}]
    assert len(counted_rows) == 1


def test_base_external_call_is_not_layer_unknown_and_preserves_links():
    text = (
        (ROOT / "base" / MODULE)
        .read_text("utf-8")
        .replace(
            "ДобавитьПКО_Товар(ПравилаКонвертации);",
            "ДобавитьПКО_Товар(ПравилаКонвертации);\n"
            "ВнешнийМодуль.ЗаполнитьПравилаКонвертацииОбъектов("
            "НаправлениеОбмена, ПравилаКонвертации);",
        )
        .replace('"Наименование", "Description"', '"Наименование", "Description", 0, "НетПравила"')
    )
    document = read_manager_text(text)
    assert any(u.reason == "external_rule_call" for u in document.unknown)
    layered = _overlay(
        document,
        """&После("ЗаполнитьПравилаКонвертацииОбъектов")
Процедура Доп_ПКО(НаправлениеОбмена, ПравилаКонвертации)
КонецПроцедуры""",
    )
    assert not any(s.reason == "unknown_call" for s in layered.skipped)
    assert all(v.certainty == "known" for c in layered.contexts for v in c.entities)
    before = validate_links(document, build_addresses(document), build_references(document))
    after = {i for c in layered.contexts for i in validate_effective_links(layered, c).issues}
    assert after == set(before.issues)


def test_six_argument_property_defaults_and_namespace(service):
    document = read_manager_text((ROOT / "base" / MODULE).read_text("utf-8"))
    text = """&После("ЗаполнитьПравилаКонвертацииОбъектов")
Процедура Доп_ПКО(НаправлениеОбмена, ПравилаКонвертации)
 Правило = ПравилаКонвертации.Найти("Товар", "ИмяПКО");
 Если Правило <> Неопределено Тогда
  ДобавитьПКС(Правило.Свойства, "ДопКод", "ExtraCode", , , "urn:demo:extra");
 КонецЕсли;
КонецПроцедуры"""
    layered = _overlay(document, text)
    assert not layered.skipped
    payload = _send(layered).entities[0].payload
    assert isinstance(payload, ObjectRule)
    prop = payload.properties[-1]
    assert (prop.algorithm_flag, prop.conversion_rule, prop.namespace) == (0, "", "urn:demo:extra")
    snap = ed_layers.snapshot(replace(layered, layers=()), ())
    listed = ed_layers.list_rows(snap, "pks", "send", False, None, None)
    row = next(r for r in listed if r["name"] == "ExtraCode")
    assert row["namespace"] == "urn:demo:extra"
    assert (
        ed_layers.get_view(snap, row["address"], "send", False, None, 0, 50, False, 0, 100)[
            "fields"
        ]["namespace"]
        == "urn:demo:extra"
    )
    unknown = _overlay(
        document, text.replace(', , , "urn:demo:extra"', ', ВычислитьФлаг(), , "urn:demo:extra"')
    )
    assert Counter(s.reason for s in unknown.skipped)["nonliteral_property"] == 1


def test_base_reader_already_supports_six_argument_omissions():
    text = (
        (ROOT / "base" / MODULE)
        .read_text("utf-8")
        .replace(
            '"Наименование", "Description");',
            '"Наименование", "Description", , , "urn:demo:extra");',
        )
    )
    document = read_manager_text(text)
    prop = document.pko[0].properties[0]
    assert (prop.algorithm_flag, prop.conversion_rule, prop.namespace) == (0, "", "urn:demo:extra")
    assert prop.argument_presence == (True, True, True, False, False, True)


def test_opaque_base_property_does_not_hide_untouched_reference():
    text = (
        (ROOT / "base" / MODULE)
        .read_text("utf-8")
        .replace(
            'ДобавитьПКС(СвойстваШапки, "Наименование", "Description");',
            "Если Настройка Тогда\n"
            'ДобавитьПКС(СвойстваШапки, "Наименование", "Description");\nКонецЕсли;\n'
            'ДобавитьПКС(СвойстваШапки, "Код", "Code", 0, "НетПравила");',
        )
    )
    document = read_manager_text(text)
    layered = _overlay(
        document,
        '&После("ЗаполнитьПравилаКонвертацииОбъектов")\n'
        "Процедура Доп_ПКО(НаправлениеОбмена, ПравилаКонвертации)\nКонецПроцедуры",
    )
    assert not layered.skipped
    before = validate_links(document, build_addresses(document), build_references(document))
    assert before.issues
    after = {i for c in layered.contexts for i in validate_effective_links(layered, c).issues}
    assert after == set(before.issues)


def test_unread_layer_property_prevents_unused_extension_claim():
    document = read_manager_text((ROOT / "base" / MODULE).read_text("utf-8"))
    layered = _overlay(
        document,
        """&После("ЗаполнитьПравилаКонвертацииОбъектов")
Процедура Доп_ПКО(НаправлениеОбмена, ПравилаКонвертации)
 Правило = ПравилаКонвертации.Найти("Товар", "ИмяПКО");
 Если Правило <> Неопределено Тогда
  ОбменДаннымиXDTOСервер.ИнициализироватьРасширениеПравилаКонвертацииОбъекта(
   Правило, "urn:demo:extra");
  ДобавитьПКС(Правило.Свойства, "ДопКод", "ExtraCode", , , ПолучитьURI());
 КонецЕсли;
КонецПроцедуры""",
    )
    report = validate_effective_links(layered, _send(layered))
    assert not any(i.check == "ed.extension.unused" for i in report.issues)
    assert any(
        s.check == "ed.extension.unused" and "nonliteral_property" in s.reason
        for s in report.skipped
    )


def test_seven_argument_omission_remains_unrecognized():
    document = read_manager_text((ROOT / "base" / MODULE).read_text("utf-8"))
    layered = _overlay(
        document,
        """&После("ЗаполнитьПравилаКонвертацииОбъектов")
Процедура Доп_ПКО(НаправлениеОбмена, ПравилаКонвертации)
 Правило = ПравилаКонвертации.Найти("Товар", "ИмяПКО");
 Если Правило <> Неопределено Тогда
  ДобавитьПКС(Правило.Свойства, "ДопКод", "ExtraCode", , , "urn:demo:extra", "Условие");
 КонецЕсли;
КонецПроцедуры""",
    )
    assert layered.skipped


def route_profile(root, layered):
    assert session_inputs._orig_read_routes is not None
    return session_inputs._orig_read_routes(root, layers=layered)


def test_route_format_extensions_have_summary_and_section_without_hooks():
    from kd2_rules_mcp.service.ed_routes_views import route_rows, route_summary

    assert session_inputs._orig_read_routes is not None
    profile = session_inputs._orig_read_routes(ROOT / "base")
    source = profile.plans[0].entries[0].source
    profile = replace(
        profile,
        format_extensions=(FormatExtension("urn:demo:global", "1.20", source),),
        plans=(
            replace(
                profile.plans[0],
                declared_plan_extensions=(FormatExtension("urn:demo:plan", "1.21", source),),
            ),
        ),
    )
    assert route_summary(profile, {}, reused=False, stale=False)["format_extensions"]["total"] == 2
    rows = route_rows(profile, "format_extensions", None)
    assert {r["uri"] for r in rows} == {"urn:demo:global", "urn:demo:plan"}
    assert {r["context"] for r in rows} == {"plan", "without_node"}


@pytest.mark.parametrize("local_map", [False, True])
def test_settings_format_map_keeps_versions_and_is_visible(service, tmp_path, local_map):
    root = tmp_path / "kits"
    copytree(ROOT, root)
    hook = root / "a/ExchangePlans/ДемоОбмен/Ext/ManagerModule.bsl"
    hook.write_text(
        """&После("ПриПолученииНастроек")
Процедура Доп_Настройки(Настройки)
 Настройки.РасширенияФорматаОбмена = Новый Соответствие;
 Настройки.РасширенияФорматаОбмена.Вставить("urn:demo:extra", "1.20");
КонецПроцедуры""",
        encoding="utf-8",
    )
    if local_map:
        hook.write_text(
            hook.read_text("utf-8")
            .replace(
                "Настройки.РасширенияФорматаОбмена = Новый Соответствие;",
                "Карта = Новый Соответствие;",
            )
            .replace(
                'Настройки.РасширенияФорматаОбмена.Вставить("urn:demo:extra", "1.20");',
                'Карта.Вставить("urn:demo:extra", "1.20");\n'
                "Настройки.РасширенияФорматаОбмена = Карта;",
            ),
            encoding="utf-8",
        )
    # Только перехват плана: глобальная карта не подменяет менеджер.
    global_hook = root / "a/CommonModules/ОбменДаннымиПереопределяемый/Ext/Module.bsl"
    global_hook.write_text(
        """&После("ПриПолученииДоступныхРасширенийФормата")
Процедура Доп_Расширения(РасширенияФормата)
 РасширенияФормата.Вставить("urn:demo:extra", "1.20");
КонецПроцедуры""",
        encoding="utf-8",
    )
    layered = read_layers(root / "base", [root / "a"], manager="МенеджерДемо")
    assert not layered.skipped
    profile = route_profile(root / "base", layered)
    assert profile.plans[0].effective_map() == {"1.20": "МенеджерДемо", "1.21": "МенеджерДемо"}
    assert profile.status == "complete"
    assert len(profile.plans[0].declared_plan_extensions) == 1
    from kd2_rules_mcp.service.ed_routes_views import route_rows, route_summary

    rows = route_rows(profile, "format_extensions", None)
    assert {r["context"] for r in rows} == {"plan", "without_node"}
    assert all(r["uri"] == "urn:demo:extra" and r["state"] == "effective" for r in rows)
    assert route_summary(profile, {}, reused=False, stale=False)["format_extensions"]["total"] == 2
    hook.write_text(
        hook.read_text("utf-8").replace(
            '.Вставить("urn:demo:extra", "1.20");',
            '.Вставить("urn:demo:extra", "1.19");\n'
            + ("Карта" if local_map else "Настройки.РасширенияФорматаОбмена")
            + '.Вставить("urn:demo:extra", "1.20");',
        ),
        encoding="utf-8",
    )
    repeated = read_layers(root / "base", [root / "a"], manager="МенеджерДемо")
    declarations = route_profile(root / "base", repeated).plans[0].declared_plan_extensions
    assert [(e.version, e.state) for e in declarations] == [
        ("1.19", "overwritten"),
        ("1.20", "effective"),
    ]
    hook.write_text(
        hook.read_text("utf-8").replace(
            "КонецПроцедуры", "НеизвестныйКод(Настройки);\nКонецПроцедуры"
        ),
        encoding="utf-8",
    )
    unknown = read_layers(root / "base", [root / "a"], manager="МенеджерДемо")
    assert route_profile(root / "base", unknown).plans[0].effective_map() == {}


def test_guarded_single_version_selects_own_manager_and_preserves_other_key(service, tmp_path):
    root = tmp_path / "kits"
    copytree(ROOT, root)
    (root / "a/ExchangePlans/ДемоОбмен/Ext/ManagerModule.bsl").write_text(
        """
&После("ПриПолученииНастроек")
Процедура Доп_Настройки(Настройки)
 Если ТипЗнч(Настройки.ВерсииФорматаОбмена) = Тип("Соответствие") Тогда
  Настройки.ВерсииФорматаОбмена.Вставить("1.20", ДопМенеджер);
 КонецЕсли;
КонецПроцедуры""",
        encoding="utf-8",
    )
    (root / "a/CommonModules/ОбменДаннымиПереопределяемый/Ext/Module.bsl").write_text(
        "// Глобальная карта не изменяется", encoding="utf-8"
    )
    layered = read_layers(root / "base", [root / "a"], version_key="1.20")
    assert layered.contexts[0].manager_name == "ДопМенеджер"
    assert len({s.file_id for s in layered.source_files}) == len(layered.source_files)
    profile = route_profile(root / "base", layered)
    assert profile.plans[0].effective_map() == {"1.20": "ДопМенеджер", "1.21": "МенеджерДемо"}
    path = root / "a/CommonModules/ДопМенеджер/Ext/Module.bsl"
    ident = service.ed_open(
        path=str(path), configuration_path=str(root / "base"), extensions=[str(root / "a")]
    )["project_id"]
    assert service.ed_get(ident, "ПКО/Товар")["span"]["file_id"] != "module"
    hook = root / "a/ExchangePlans/ДемоОбмен/Ext/ManagerModule.bsl"
    hook.write_text(
        hook.read_text("utf-8").replace('Тип("Соответствие")', 'Тип("Структура")'), encoding="utf-8"
    )
    unknown = read_layers(root / "base", [root / "a"], manager="МенеджерДемо")
    assert route_profile(root / "base", unknown).plans[0].effective_map() == {}


def test_unknown_target_in_other_direction_is_skipped_in_both_passes():
    text = module_text().replace(
        "ДобавитьПКО_Товар(ПравилаКонвертации);",
        'Если НаправлениеОбмена = "Получение" Тогда\n'
        "ДобавитьПКО_Товар(ПравилаКонвертации);\nКонецЕсли;",
    )
    text = put(
        text, "event", 'ПКО = КомпонентыОбмена.ПравилаКонвертацииОбъектов.Найти("Товар", "ИмяПКО");'
    )
    document = read_manager_text(text)
    layered = compose_manager(document)
    layered = replace(
        layered,
        contexts=tuple(
            replace(
                c,
                entities=tuple(
                    replace(v, certainty="unknown") if isinstance(v.payload, ObjectRule) else v
                    for v in c.entities
                    if c.direction != "send" or not isinstance(v.payload, ObjectRule)
                ),
            )
            for c in layered.contexts
        ),
    )
    for context in layered.contexts:
        report = validate_effective_links(layered, context)
        assert not any(i.check == "ed.reference.code_rule_missing" for i in report.issues)
        assert any(s.check == "ed.reference.code_rule_missing" for s in report.skipped)


def test_pod_insert_checks_rule_name_and_assignment_checks_original_key():
    text = _pod_handler('ИспользованиеПКО.Вставить("Виды", Истина);') + _pko_procedure("Виды")
    _, references, result = analyze(text)
    assert not result.issues
    assert any(r.kind == "pod_use" and r.form == "insert" for r in references.entries)
    missing = analyze(_pod_handler('ИспользованиеПКО.Вставить("НетПравила", Истина);'))[2]
    assert [i.check for i in missing.issues] == ["ed.reference.code_rule_missing"]
    assignment = analyze(_pod_handler("ИспользованиеПКО.Виды = Ложь;") + _pko_procedure("Виды"))[2]
    assert [i.check for i in assignment.issues] == ["ed.reference.pod_usage_key_missing"]


def test_unknown_base_dispatcher_keeps_untouched_pod_assignment_warning():
    text = _pod_handler("ИспользованиеПКО.Товар = Ложь;").replace(
        'ПравилоОбработки.ИспользуемыеПКО.Добавить("Товар");', ""
    )
    text = text.replace(
        "ОбработатьПОД(Параметры);\n    КонецЕсли;",
        "ОбработатьПОД(Параметры);\n    КонецЕсли;\n"
        "ВнешнийМодуль.Дополнить(ИмяПроцедуры, Параметры);",
        1,
    )
    document = read_manager_text(text)
    assert any(u.reason == "unsupported_dispatcher_statement" for u in document.unknown)
    layered = _overlay(
        document,
        '&После("ЗаполнитьПравилаКонвертацииОбъектов")\n'
        "Процедура Доп_ПКО(НаправлениеОбмена, ПравилаКонвертации)\nКонецПроцедуры",
    )
    before = validate_links(document, build_addresses(document), build_references(document))
    assert [i.check for i in before.issues] == ["ed.reference.pod_usage_key_missing"]
    after = {i for c in layered.contexts for i in validate_effective_links(layered, c).issues}
    assert after == set(before.issues)


@pytest.mark.parametrize(
    "name",
    [
        "ОбменДаннымиXDTOСервер",
        "ОбменДаннымиСервер",
        "ОбменДаннымиСобытия",
        "ОбменДаннымиПовтИсп",
    ],
)
def test_executor_hooks_are_reported_without_reading_bodies(service, tmp_path, name):
    root = tmp_path / "kits"
    copytree(ROOT, root)
    for side in ("base", "b"):
        config = root / side / "Configuration.xml"
        config.write_text(
            config.read_text("utf-8").replace(
                "</ChildObjects>", f"<CommonModule>{name}</CommonModule></ChildObjects>"
            ),
            encoding="utf-8",
        )
        extra = (
            ""
            if side == "base"
            else (
                "<ObjectBelonging>Adopted</ObjectBelonging>"
                "<ExtendedConfigurationObject>00000000-0000-0000-0000-000000000008</ExtendedConfigurationObject>"
            )
        )
        (root / side / f"CommonModules/{name}.xml").write_text(
            '<MetaDataObject><CommonModule uuid="00000000-0000-0000-0000-000000000008">'
            f"<Properties><Name>{name}</Name>{extra}</Properties></CommonModule></MetaDataObject>",
            encoding="utf-8",
        )
        path = root / side / f"CommonModules/{name}/Ext/Module.bsl"
        path.parent.mkdir(parents=True)
        path.write_text(
            ""
            if side == "base"
            else """&ИзменениеИКонтроль("ПрочитатьСообщениеОбмена")
Процедура Доп_Прочитать()
 ПроизвольныйКод();
КонецПроцедуры
&Вместо("ПКОПоИмени")
Функция Доп_ПКО()
 Возврат ПроизвольнаяФункция();
КонецФункции""",
            encoding="utf-8",
        )
    opened = service.ed_open(
        path=str(root / "base" / MODULE),
        configuration_path=str(root / "base"),
        extensions=[str(root / "b")],
    )
    ident = opened["project_id"]
    overview = service.ed_overview(ident)["composition"]
    overrides = overview["executor_overrides"]
    assert "не гарантированы" in overview["executor_note"]
    assert "не гарантированы" in opened["executor_note"]
    assert {r["kind"] for r in overrides} == {"change_control", "around"}
    report = validate_layers(service._ed_project(ident).layered.manager)
    assert len([s for s in report.skipped if s.check == "ed.layer.executor.overridden"]) == 2
    assert not any(
        s.reason == "unknown_call" for s in service._ed_project(ident).layered.manager.skipped
    )


def test_schema_open_names_missing_imports(service):
    path = ROOT.parent / "schema/base.bin"
    opened = service.ed_schema_open("1.2", path=str(path))
    assert opened["status"] == "partial"
    assert opened["missing_imports"] == ["urn:test:message"]
    assert "imports" in opened["imports_hint"]
    complete = service.ed_schema_open(
        "1.2", path=str(path), imports={"urn:test:message": str(path.parent / "message.bin")}
    )
    assert complete["status"] == "complete" and "imports_hint" not in complete


def test_schema_open_unresolved_type_without_import_declaration_explains_imports(service, tmp_path):
    path = tmp_path / "package.bin"
    path.write_text(
        '<package xmlns="http://v8.1c.ru/8.1/xdto" xmlns:m="urn:demo:missing" '
        'targetNamespace="urn:demo:package">'
        '<objectType name="Item" base="m:Object"/></package>',
        encoding="utf-8",
    )
    opened = service.ed_schema_open("1.2", path=str(path))
    assert opened["status"] == "partial" and "imports" in opened["imports_hint"]
