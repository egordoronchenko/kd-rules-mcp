"""Неизвестный перехват и отброшенный файл видны у действующих правил."""

from dataclasses import replace
from shutil import copytree

import pytest

from kd_rules_mcp.ed.reader import read_manager_text
from kd_rules_mcp.service import Kd2Service, Settings, ed_layers
from tests.test_ed_layers import ROOT
from tests.test_service_ed_authoring import setup as authoring_setup
from tests.test_service_ed_writer import writer_setup as manager_setup

MODULE = "CommonModules/МенеджерДемо/Ext/Module.bsl"
configured_authoring = authoring_setup
writer_setup = manager_setup


@pytest.fixture
def setup(tmp_path):
    base = tmp_path / "base"
    extension = tmp_path / "extension"
    copytree(ROOT / "base", base)
    copytree(ROOT / "change-control", extension)
    path = base / MODULE
    text = path.read_text("utf-8")
    text = text.replace(
        'ПравилоКонвертации.ВариантИдентификации = "ПоУникальномуИдентификатору";',
        'ПравилоКонвертации.ВариантИдентификации = "ПоУникальномуИдентификатору";\n'
        '    ПравилоКонвертации.ПоляПоиска.Добавить("Наименование");',
    )
    rule = read_manager_text(text).pko[0]
    text = text.replace("Товар", "X")
    text += "\n" + rule.raw_text.replace("Товар", "Y") + "\n"
    text = text.replace(
        "ДобавитьПКО_X(ПравилаКонвертации);",
        "ДобавитьПКО_X(ПравилаКонвертации);\n    ДобавитьПКО_Y(ПравилаКонвертации);",
    )
    path.write_text(text, encoding="utf-8")
    service = Kd2Service(Settings(cache_dir=tmp_path / "cache", workspace=tmp_path / "workspace"))
    return service, base, extension


def open_layers(setup):
    service, base, extension = setup
    return service.ed_open(
        path=str(base / MODULE), configuration_path=str(base), extensions=[str(extension)]
    )


def test_change_control_only_taints_built_rule(setup):
    service, _, _ = setup
    opened = open_layers(setup)
    ident = opened["project_id"]
    unknown = service.ed_list(ident, "layer_unknown")["items"]
    assert len(unknown) == 1 and unknown[0]["name"] == "unmodeled_hook"
    x = service.ed_get(ident, "Действующее/ПКО/X", direction="receive")
    y = service.ed_get(ident, "Действующее/ПКО/Y", direction="receive")
    assert x["certainty"] == "unknown"
    assert x["uncertain_by"] == {"items": [unknown[0]["address"]], "has_more": False}
    assert y["certainty"] == "known" and "uncertain_by" not in y
    for rule in (x, y):
        searches = [row for row in rule["children"]["items"] if row["kind"] == "search"]
        assert len(searches) == 1
        row = searches[0]
        search = service.ed_get(ident, row["address"], direction="receive")
        assert search["certainty"] == row["certainty"] == rule["certainty"]
        assert search.get("uncertain_by") == row.get("uncertain_by") == rule.get("uncertain_by")
    rows = service.ed_list(ident, "pko", direction="receive")["items"]
    by_name = {row["name"]: row for row in rows}
    assert by_name["X"]["certainty"] == x["certainty"]
    assert by_name["X"]["uncertain_by"] == x["uncertain_by"]
    assert by_name["Y"]["certainty"] == "known" and "uncertain_by" not in by_name["Y"]


@pytest.mark.parametrize("named_handler", [True, False], ids=["handler", "base_routine"])
def test_unmodeled_handler_only_taints_its_owner(setup, named_handler):
    service, base, extension = setup
    name = "ПКО_X_ПриОтправкеДанных" if named_handler else "ОбработатьX"
    path = base / MODULE
    path.write_text(
        path.read_text("utf-8").replace("ПКО_X_ПриОтправкеДанных", name), encoding="utf-8"
    )
    if not named_handler:
        text = path.read_text("utf-8")
        routine = next(r for r in read_manager_text(text).routines if r.name == name)
        path.write_text(
            text.replace(
                routine.raw_text, "#Область Алгоритмы\n" + routine.raw_text + "\n#КонецОбласти"
            ),
            encoding="utf-8",
        )
    # Известный обработчик с верной сигнатурой — изменение тела, правило известно
    # (test_ed_layers_handler_promise); в неразобранные он попадает при неверной сигнатуре.
    parameters = (
        "ДанныеИБ, ДанныеXDTO"
        if named_handler
        else "ДанныеИБ, ДанныеXDTO, КомпонентыОбмена, СтекВыгрузки"
    )
    (extension / MODULE).write_text(
        f'&ИзменениеИКонтроль("{name}")\n'
        f"Процедура доп_Обработать({parameters})\n"
        '#Вставка\n    ДанныеXDTO.Вставить("Code", ДанныеИБ.Код);\n#КонецВставки\n'
        "КонецПроцедуры\n",
        encoding="utf-8",
    )
    ident = open_layers(setup)["project_id"]
    unknown = service.ed_list(ident, "layer_unknown")["items"]
    assert len(unknown) == 1 and unknown[0]["name"] == "unmodeled_hook"
    assert ("класс handler" if named_handler else "класс base_routine") in unknown[0]["detail"]
    x = service.ed_get(ident, "Действующее/ПКО/X", direction="send")
    y = service.ed_get(ident, "Действующее/ПКО/Y", direction="send")
    assert x["certainty"] == "unknown"
    assert x["uncertain_by"]["items"] == [unknown[0]["address"]]
    assert y["certainty"] == "known" and "uncertain_by" not in y


def test_unmodeled_handler_alias_uses_resolved_procedure_id(setup):
    service, base, extension = setup
    path = base / MODULE
    text = path.read_text("utf-8")
    # Привязка хранит литерал ветки, а перехватывается вызываемая ею процедура.
    text = text.replace(
        'ПравилоКонвертации.ПриОтправкеДанных = "ПКО_X_ПриОтправкеДанных";',
        'ПравилоКонвертации.ПриОтправкеДанных = "ОтправитьX";',
    ).replace('ИмяПроцедуры = "ПКО_X_ПриОтправкеДанных"', 'ИмяПроцедуры = "ОтправитьX"')
    path.write_text(text, encoding="utf-8")
    (extension / MODULE).write_text(
        '&ИзменениеИКонтроль("ПКО_X_ПриОтправкеДанных")\n'
        "Процедура доп_Отправить(ДанныеИБ, ДанныеXDTO)\n"
        '#Вставка\nСообщить("Дополнение");\n#КонецВставки\nКонецПроцедуры\n',
        encoding="utf-8",
    )
    ident = open_layers(setup)["project_id"]
    x = service.ed_get(ident, "Действующее/ПКО/X", direction="send")
    assert x["certainty"] == "unknown" and len(x["uncertain_by"]["items"]) == 1
    assert service.ed_get(ident, "Действующее/ПКО/Y", direction="send")["certainty"] == "known"


def test_unrelated_base_routine_does_not_taint_rules(setup):
    service, base, extension = setup
    path = base / MODULE
    path.write_text(
        path.read_text("utf-8") + "\nПроцедура Служебная()\nКонецПроцедуры\n", encoding="utf-8"
    )
    (extension / MODULE).write_text(
        '&ИзменениеИКонтроль("Служебная")\nПроцедура доп_Служебная()\nКонецПроцедуры\n',
        encoding="utf-8",
    )
    ident = open_layers(setup)["project_id"]
    assert len(service.ed_list(ident, "layer_unknown")["items"]) == 1
    for name in ("X", "Y"):
        rule = service.ed_get(ident, "Действующее/ПКО/" + name, direction="receive")
        assert rule["certainty"] == "known" and "uncertain_by" not in rule


@pytest.mark.parametrize("methods", [68, 512, 513])
def test_method_limit_reports_actual_and_preserves_accepted_layers(setup, methods):
    service, _, extension = setup
    (extension / MODULE).write_text(
        "".join(f"Процедура Метод{n}()\nКонецПроцедуры\n" for n in range(methods)),
        encoding="utf-8",
    )
    opened = open_layers(setup)
    dropped = opened["dropped_layers"]
    assert service.ed_overview(opened["project_id"])["composition"]["dropped_layers"] == dropped
    assert dropped["total"] == (1 if methods > 512 else 0)
    assert not dropped["has_more"]
    assert (
        opened["diagnostics_summary"]
        == service.ed_open(str(setup[1] / MODULE), extensions=[])["diagnostics_summary"]
    )
    x = service.ed_get(opened["project_id"], "Действующее/ПКО/X", direction="receive")
    if methods <= 512:
        assert dropped["items"] == [] and x["certainty"] == "known"
        assert "uncertain_by" not in x
    else:
        row = dropped["items"][0]
        assert row["reason"] == "resource_limit" and row["detail"] == "методов 513 > 512"
        unknown = service.ed_list(opened["project_id"], "layer_unknown")["items"][0]
        assert row["address"] == unknown["address"]
        assert row["file_id"] == unknown["file_id"]
        assert row["layer_id"] == unknown["layer_id"]
        assert x["certainty"] == "unknown" and x["uncertain_by"]["items"] == [row["address"]]


def test_whole_syntax_failure_is_reported_but_local_unknown_is_not_dropped(setup):
    service, _, extension = setup
    opened = open_layers(setup)
    assert opened["dropped_layers"] == {"items": [], "total": 0, "has_more": False}
    service.ed_close(opened["project_id"])
    (extension / MODULE).write_text('Процедура Сломано()\nА = "без конца;\n', encoding="utf-8")
    opened = open_layers(setup)
    dropped = opened["dropped_layers"]["items"]
    assert len(dropped) == 1 and dropped[0]["reason"] == "bsl_syntax"
    assert dropped[0]["detail"]
    unknown = service.ed_get(opened["project_id"], dropped[0]["address"])
    assert unknown["name"] == "bsl_syntax"


def test_recursion_drops_whole_file_and_is_listed(setup):
    """Рекурсия обрывает чтение файла так же, как предел: файл виден в dropped_layers."""
    service, _, extension = setup
    (extension / MODULE).write_text(
        "Процедура Первая(ПравилаКонвертации)\n    Вторая(ПравилаКонвертации);\nКонецПроцедуры\n"
        "Процедура Вторая(ПравилаКонвертации)\n    Первая(ПравилаКонвертации);\nКонецПроцедуры\n"
        '&После("ЗаполнитьПравилаКонвертацииОбъектов")\n'
        "Процедура Доп(НаправлениеОбмена, ПравилаКонвертации)\n"
        "    Первая(ПравилаКонвертации);\nКонецПроцедуры\n",
        encoding="utf-8",
    )
    opened = open_layers(setup)
    dropped = opened["dropped_layers"]["items"]
    assert len(dropped) == 1 and dropped[0]["reason"] == "recursion"
    x = service.ed_get(opened["project_id"], "Действующее/ПКО/X", direction="receive")
    assert x["certainty"] == "unknown" and x["uncertain_by"]["items"] == [dropped[0]["address"]]


def test_reason_pages_are_bounded_and_use_global_skip_numbering(setup):
    from kd_rules_mcp.ed.layers import compose_manager

    service, _, _ = setup
    ident = open_layers(setup)["project_id"]
    project = service._ed_project(ident)
    manager = project.layered.manager
    reading = manager.readings[0]
    skip = reading.skips[0]
    reading = replace(reading, skips=(skip,) * 10)
    manager = compose_manager(manager.base, readings=[reading], layers=manager.layers)
    snap = ed_layers.snapshot(ed_layers.portable_files(manager), ())
    result = ed_layers.get_view(
        snap, "Действующее/ПКО/X", "receive", False, None, 0, 50, False, 0, 2000
    )
    rows = ed_layers.list_rows(snap, "layer_unknown", "receive", False, None, None)
    assert result["uncertain_by"] == {
        "items": [row["address"] for row in rows[:8]],
        "has_more": True,
    }
    assert len(rows) == 10
    # Несколько отброшенных файлов внутри одного слоя: предел относится к файлам.
    sources = [replace(reading.source, file_id=f"file:{n}") for n in range(18)]
    skips = tuple(
        replace(skip, reason="resource_limit", origin=replace(skip.origin, file_id=s.file_id))
        for s in sources
    )
    dropped = replace(
        manager,
        skipped=(skip, *skips),
        readings=tuple(
            replace(reading, source=s, skips=(k,), dropped=True)
            for s, k in zip(sources, skips, strict=True)
        ),
    )
    page = ed_layers.composition(replace(snap, manager=dropped))["dropped_layers"]
    assert page["total"] == 18 and len(page["items"]) == 16 and page["has_more"]
    assert page["items"][0]["address"].endswith("/2")
    assert page["items"][-1]["address"].endswith("/17")


def test_byte_limit_detail_and_clean_boundary(monkeypatch):
    from kd_rules_mcp.ed import layer_reader
    from kd_rules_mcp.ed.layer_model import LayerDescriptor

    text = "Процедура Метод()\nКонецПроцедуры\n"
    size = len(text.encode("utf-8"))
    layer = LayerDescriptor("L01-Демо", 1, "Демо", "mem", None, "")
    monkeypatch.setattr(layer_reader, "MAX_FILE_BYTES", size)
    clean = layer_reader.read_extension_text(text, layer=layer, helpers=frozenset(), targets={})
    assert not clean.dropped and not clean.skips
    dropped = layer_reader.read_extension_text(
        text + " ", layer=layer, helpers=frozenset(), targets={}
    )
    assert dropped.dropped
    assert dropped.skips[0].reason == "resource_limit"
    assert dropped.skips[0].raw == f"байт {size + 1} > {size}"


def test_layer_count_limit_reports_actual_without_changing_limit(setup):
    from kd_rules_mcp.ed.layers import read_layers

    _, base, extension = setup
    limited = read_layers(base, [extension] * 17)
    assert limited.skipped[0].reason == "resource_limit"
    assert limited.skipped[0].raw == "слоёв 17 > 16"
    assert read_layers(base).status == "complete"


@pytest.mark.parametrize("methods", [512, 513])
def test_authoring_requires_ack_for_dropped_configured_layer(configured_authoring, methods):
    from pathlib import Path

    from kd_rules_mcp.errors import EdAuthoringAckRequiredError
    from tests.test_service_ed_authoring import write

    service, args, root = configured_authoring
    foreign = root.parent / "foreign"
    copytree(ROOT / "change-control", foreign)
    module = foreign / "CommonModules/Менеджер2/Ext/Module.bsl"
    module.parent.mkdir(parents=True)
    module.write_text(
        "".join(f"Процедура Метод{n}()\nКонецПроцедуры\n" for n in range(methods)), encoding="utf-8"
    )
    catalog = service.settings.projects_file
    catalog.write_text(
        catalog.read_text("utf-8") + "        extensions: [../foreign]\n", encoding="utf-8"
    )
    service.structure_load_project("Пример", "Main", structure_id="fiction-main", force=True)
    viewed = service.ed_authoring_build(**args)
    notices = [n for n in viewed["acknowledgement_notices"] if n["id"] == "layer_uncertain_rule"]
    if methods > 512:
        assert len(notices) == 1
        notice = notices[0]
        assert notice["address"] == args["operations"][0]["target"]["pko_address"]
        assert notice["uncertain_by"] == {
            "items": ["Слой/L01-Контроль/Неизвестное/1"],
            "has_more": False,
        }
        assert notice["notice_id"] in viewed["required_acknowledgements"]
        with pytest.raises(EdAuthoringAckRequiredError) as caught:
            service.ed_authoring_build(
                **args,
                mode="write",
                expected_preview_hash=viewed["build_hash"],
                acknowledged_notices=[
                    n for n in viewed["required_acknowledgements"] if n != notice["notice_id"]
                ],
            )
        assert notice["notice_id"] in caught.value.details["required_acknowledgements"]
        assert not Path(viewed["output_dir"]).exists()
    else:
        assert notices == []
    result = write(configured_authoring, viewed)
    assert result["written"]


def test_authoring_keeps_explicit_layered_snapshot_refusal(setup):
    service, _, _ = setup
    ident = open_layers(setup)["project_id"]
    with pytest.raises(ValueError, match=r"extensions=\[\]"):
        service._inputs_for("demo", "full", (ident, "schema", "structure"))


def test_import_refuses_layers_and_rebind_preserves_imported_rules(writer_setup, tmp_path):
    from kd_rules_mcp.errors import EdAuthoringPreconditionError

    service, args, root = writer_setup
    extension = tmp_path / "extension"
    copytree(ROOT / "change-control", extension)
    for path in sorted(extension.rglob("*"), reverse=True):
        if path.is_file():
            path.write_text(
                path.read_text("utf-8").replace("МенеджерДемо", "Менеджер2"), encoding="utf-8"
            )
    (extension / "CommonModules/МенеджерДемо").rename(extension / "CommonModules/Менеджер2")
    (extension / "CommonModules/МенеджерДемо.xml").rename(extension / "CommonModules/Менеджер2.xml")
    source = service.ed_open(
        str(root / "CommonModules/Менеджер2/Ext/Module.bsl"),
        configuration_path=str(root),
        extensions=[str(extension)],
    )
    with pytest.raises(EdAuthoringPreconditionError) as caught:
        service.ed_create(**args, mode="import", document_id=source["project_id"])
    assert caught.value.details["failures"][0]["id"] == "unsupported_form"
    assert not service.manager_workspace.ids()
    plain = service.ed_open(str(root / "CommonModules/Менеджер2/Ext/Module.bsl"))
    created = service.ed_create(**args, mode="import", document_id=plain["project_id"])
    model = service.manager_workspace.get(created["project_id"]).model
    before = (model.pko, model.pod, model.source_files)
    rebound = service.ed_create(**args, mode="rebind")
    after = service.manager_workspace.get(rebound["project_id"]).model
    assert (after.pko, after.pod, after.source_files) == before
