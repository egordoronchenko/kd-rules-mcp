"""Синтетическое чтение маршрутов EnterpriseData: грамматика §2 и негативные границы."""

import shutil
from pathlib import Path

import pytest

from kd2_rules_mcp.ed.errors import EdFormatError
from kd2_rules_mcp.ed.routes import compare_versions, read_routes

DATA = Path(__file__).parent / "data" / "ed" / "routes"
NODE = (
    "Версия конкретного узла неизвестна; при пустом значении "
    "исполнитель выбирает минимальную версию карты (XDTO:3618)."
)


def load(name: str):
    return read_routes(DATA / name)


def plan_named(profile, name="ПланФормата"):
    return next(item for item in profile.plans if item.plan_name == name)


def reading_skips(profile):
    return [item for item in profile.skipped if item.code == "ed.route.reading"]


def test_inventory_and_packages():
    profile = load("grammar")
    assert profile.configuration_name == "ВымышленнаяКонфигурация"
    assert profile.project is None and profile.configuration is None
    assert profile.reader_version == "1"
    assert "ПланФормата" in {item.plan_name for item in profile.plans}
    assert "ПланБезМодуля" in {item.plan_name for item in profile.plans}
    assert "СиротаНаДиске" not in profile.declared_modules
    assert "Приложения" in profile.declared_subsystems
    names = {item.metadata_name: item for item in profile.packages}
    assert names["ПакетФормата"].namespace == "urn:example:format/1.8"
    assert names["ПакетФормата"].package_path is not None
    assert names["ПакетЛишний"].namespace == "urn:example:extra"
    assert names["СообщениеОбмена"].namespace == "urn:example:message"
    ambiguous = [item for item in profile.skipped if item.code == "ed.route.schema_ambiguous"]
    assert len(ambiguous) == 1
    assert "urn:example:shared" in ambiguous[0].reason
    bare = plan_named(profile, "ПланБезМодуля")
    assert bare.is_ed is False and bare.status == "complete" and bare.entries == ()
    assert profile.status == "complete"


def test_grammar_maps_provenance_and_variants():
    profile = load("grammar")
    plan = plan_named(profile)
    assert plan.is_ed is True
    assert plan.base_namespace == "urn:example:base"
    assert plan.effective_map() == {
        "1.2": "МенеджерОбменаПример",
        "1.4": "ДругойМенеджер",
        "1.5": "МенеджерОбменаПример",
        "2.0": "МенеджерОбменаПример",
    }
    assert "0.8" not in {item.key for item in plan.entries}
    assert "8.8" not in {item.key for item in plan.entries}
    spaced = next(
        item
        for item in plan.entries
        if item.state == "overwritten"
        and item.manager_name == "МенеджерОбменаПример"
        and item.key == "1.4"
    )
    assert spaced.key_raw == " 1.4 "
    replaced = next(item for item in plan.entries if item.key == "0.9")
    assert replaced.state == "overwritten"
    reached = next(item for item in plan.entries if item.key == "2.0")
    assert reached.source.procedure == "Заполнить"
    assert reached.source.relative_file.endswith("ПоставщикВерсий/Ext/Module.bsl")
    assert reached.source.sha256
    assert reached.source.call_chain
    case_entry = next(item for item in profile.without_node_entries if item.key == "1.6")
    assert case_entry.manager_name == "менеджеробменапример"
    assert case_entry.state == "effective"
    assert profile.effective_without_node()["1.8"] == "МенеджерОбменаПример"
    files = {span.file_id for span in case_entry.source.call_chain}
    assert any("ОбёрткаМаршрута" in item for item in files)
    assert any("ОбменДаннымиПереопределяемый" in item for item in files)
    assert any(
        item.kind == "static_metadata" and item.value == "true" for item in case_entry.conditions
    )
    assert profile.reading.static_subsystem_checks == (
        ("Приложения.ОбменФорматом", "true"),
        ("Приложения.ОбменФорматомБО", "false"),
    )
    assert profile.reading.module_aliases_reached == 1
    assert profile.reading.direct_calls_plan == 1
    assert profile.reading.calls_without_node == 2
    assert plan.registration.mode == "xml"
    template = plan.registration.template_body_path
    assert template is not None
    assert template.endswith("ПравилаРегистрации/Ext/Template.txt")
    assert [item.uri for item in profile.format_extensions] == ["urn:example:ext"]
    assert [item.uri for item in plan.declared_plan_extensions] == ["urn:example:plan"]
    by_id = {item.id: item for item in plan.variants}
    assert set(by_id) == {"Скрытый", "Особый", "Универсальный"}
    assert by_id["Скрытый"].conditions[0].value == "false"
    assert by_id["Особый"].correspondent is None
    assert by_id["Особый"].correspondent_raw == "ЧужаяБаза"
    managers = {item.name: item for item in profile.managers}
    assert managers["МенеджерОбменаПример"].interface_version == 2
    assert managers["МенеджерОбменаПример"].directions == ("send", "receive")
    assert plan.empty_node_fallback == "1.2"
    assert not hasattr(profile, "actual_node_version")
    assert any(
        item.code == "ed.route.node_state" and item.reason == NODE for item in profile.skipped
    )
    assert any(item.code == "ed.route.extensions" for item in profile.skipped)
    assert reading_skips(profile) == []


def test_false_branch_has_no_reading_skip():
    profile = load("false-branch")
    plan = plan_named(profile)
    assert plan.effective_map() == {"1.1": "МенеджерОбменаПример"}
    assert any(item.key == "9.9" and item.state == "unreachable" for item in plan.entries)
    assert reading_skips(profile) == []
    assert profile.status == "complete"


def test_negative_boundaries_record_file_and_line():
    cases = {
        "computed": "не литеральны",
        "cycle": "Цикл вызовов",
        "data-guard": "Условие по данным",
        "depth5": "глубина",
        "by-value": "Знач",
        "unknown-arg": "Неизвестный аргумент",
        "loop": "Цикл или исключение",
    }
    for name, reason in cases.items():
        profile = load(name)
        assert profile.status == "partial"
        assert plan_named(profile).effective_map() == {}
        skips = reading_skips(profile)
        assert skips
        assert all(item.relative_file and item.line for item in skips)
        assert any(reason in item.reason for item in skips)
    guarded = load("data-guard")
    assert any(item.state == "conditional" for item in plan_named(guarded).entries)


def test_depth_four_reaches_insert():
    profile = load("depth4")
    assert profile.status == "complete"
    assert plan_named(profile).effective_map() == {"1.1": "МенеджерОбменаПример"}
    assert profile.reading.direct_calls_plan == 4


def test_empty_map_and_dump_without_ed():
    empty = load("empty")
    assert empty.status == "complete"
    assert plan_named(empty).is_ed is True
    assert plan_named(empty).entries == ()
    assert empty.effective_without_node() == {}
    assert plan_named(empty).empty_node_fallback is None
    plain = load("no-ed")
    assert plain.status == "complete"
    assert all(item.is_ed is False for item in plain.plans)
    assert plain.reading.effective_plan == 0
    assert plain.reading.ed_assignments == 0


def test_root_without_configuration():
    with pytest.raises(EdFormatError, match=r"Configuration\.xml"):
        read_routes(DATA / "not-a-dump")
    with pytest.raises(EdFormatError, match=r"Configuration\.xml"):
        read_routes(DATA / "not-a-dump" / "readme.txt")


def test_managers_interfaces_and_missing_body():
    profile = load("managers")
    assert profile.status == "partial"
    found = {item.name: item for item in profile.managers}
    assert found["МенеджерОдин"].interface_origin == "fallback"
    assert found["МенеджерОдин"].interface_version == 1
    assert found["МенеджерДва"].interface_version == 2
    assert found["МенеджерДва"].directions == ("send", "receive")
    assert found["МенеджерТри"].interface_version == 3
    assert found["МенеджерТри"].interface_origin == "declared"
    assert found["МенеджерСтранный"].interface_origin == "unknown"
    assert found["МенеджерСтранный"].interface_version is None
    assert found["МенеджерБезТела"].metadata_exists is True
    assert found["МенеджерБезТела"].source_exists is False
    assert found["МенеджерНигде"].metadata_exists is False


def test_two_sources_do_not_repair_each_other():
    differ = load("maps-differ")
    assert plan_named(differ).effective_map() == {"1.1": "МенеджерОбменаПример"}
    assert differ.effective_without_node() == {"2.2": "МенеджерОбменаПример"}
    same = load("maps-equal")
    full = plan_named(same, "ПланПолный")
    blank = plan_named(same, "ПланПустой")
    assert full.effective_map() == same.effective_without_node() == {"1.5": "МенеджерОбменаПример"}
    assert blank.effective_map() == {}
    assert blank.is_ed is True


def test_subsystems():
    hidden = load("subsystem-ui")
    assert hidden.status == "complete"
    assert hidden.reading.static_subsystem_checks == (("Родитель.Потомок", "false"),)
    assert any(item.state == "unreachable" for item in plan_named(hidden).entries)
    assert reading_skips(hidden) == []
    missing = load("subsystem-missing")
    assert missing.status == "partial"
    assert missing.reading.static_subsystem_checks == (("Родитель.Потомок", "unknown"),)
    assert any("Нет XML подсистемы" in item.reason for item in reading_skips(missing))
    callback = load("subsystem-callback")
    assert callback.status == "partial"
    assert callback.reading.static_subsystem_checks == (("Родитель", "unknown"),)
    assert any("не доказан пустым" in item.reason for item in reading_skips(callback))


def test_version_comparator_and_tied_minimum():
    older = compare_versions("1.8", "1.20")
    same = compare_versions("1.20.1", "1.20.2")
    beta = compare_versions("1.8.beta", "1.8")
    release = compare_versions("1.8", "1.8.beta")
    assert older is not None and older < 0
    assert same == 0
    assert beta is not None and beta < 0
    assert release is not None and release > 0
    assert compare_versions("abc", "1.2") is None
    assert compare_versions("1.²", "1.3") is None
    profile = load("tied")
    plan = plan_named(profile)
    assert set(plan.effective_map()) == {"1.20", "1.20.2"}
    assert plan.empty_node_fallback is None
    assert plan.empty_node_tied_minima == ("1.20", "1.20.2")
    assert any(
        item.code == "ed.route.node_state" and item.reason == NODE for item in profile.skipped
    )


def test_unsupported_key_does_not_confirm_minimum(tmp_path: Path):
    profile = read_routes(
        assemble(
            tmp_path / "mix",
            "Процедура ПриПолученииНастроек(Настройки) Экспорт\n"
            "    Настройки.ЭтоПланОбменаXDTO = Истина;\n"
            "    ВерсииФормата = Новый Соответствие;\n"
            '    ВерсииФормата.Вставить("1.3", М);\n'
            '    ВерсииФормата.Вставить("1.2.x", М);\n'
            "    Настройки.ВерсииФорматаОбмена = ВерсииФормата;\n"
            "КонецПроцедуры\n",
        )
    )
    plan = _plan_of(profile)
    assert plan.status == "complete"
    assert plan.effective_map() == {"1.3": "М", "1.2.x": "М"}
    assert plan.empty_node_fallback is None
    assert plan.empty_node_tied_minima == ()


def test_non_ascii_digit_key_is_unsupported_form(tmp_path: Path):
    profile = read_routes(
        assemble(
            tmp_path / "digit",
            "Процедура ПриПолученииНастроек(Настройки) Экспорт\n"
            "    Настройки.ЭтоПланОбменаXDTO = Истина;\n"
            "    ВерсииФормата = Новый Соответствие;\n"
            '    ВерсииФормата.Вставить("1.²", М);\n'
            '    ВерсииФормата.Вставить("1.3", М);\n'
            "    Настройки.ВерсииФорматаОбмена = ВерсииФормата;\n"
            "КонецПроцедуры\n",
        )
    )
    plan = _plan_of(profile)
    assert plan.status == "complete"
    assert "1.²" in plan.effective_map()
    assert plan.empty_node_fallback is None


def _xml_object(kind: str, name: str, body: str = "") -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<MetaDataObject xmlns="http://v8.1c.ru/8.3/MDClasses">\n'
        f'  <{kind} uuid="00000000-0000-0000-0000-0000000000aa">\n'
        f"    <Properties><Name>{name}</Name></Properties>\n"
        f"{body}"
        f"  </{kind}>\n"
        "</MetaDataObject>\n"
    )


def assemble(
    root: Path,
    plan: str,
    *,
    modules: dict[str, str] | None = None,
    global_text: str | None = None,
    subsystems: dict[str, str] | None = None,
) -> Path:
    """Минимальная выгрузка с вымышленными именами."""
    modules = dict(modules or {})
    global_body = global_text or (
        "Процедура ПриПолученииДоступныхВерсийФормата(ВерсииФормата) Экспорт\nКонецПроцедуры\n"
    )
    modules["ОбменДаннымиПереопределяемый"] = global_body
    subsystem_names = tuple(subsystems or ())
    children = ["      <ExchangePlan>План</ExchangePlan>"]
    children.extend(f"      <CommonModule>{name}</CommonModule>" for name in modules)
    children.extend(f"      <Subsystem>{name}</Subsystem>" for name in subsystem_names)
    configuration = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<MetaDataObject xmlns="http://v8.1c.ru/8.3/MDClasses">\n'
        '  <Configuration uuid="00000000-0000-0000-0000-000000000001">\n'
        "    <Properties><Name>ВымышленнаяКонфигурация</Name></Properties>\n"
        "    <ChildObjects>\n" + "\n".join(children) + "\n    </ChildObjects>\n"
        "  </Configuration>\n"
        "</MetaDataObject>\n"
    )
    root.mkdir(parents=True, exist_ok=True)
    (root / "Configuration.xml").write_text(configuration, encoding="utf-8", newline="\n")
    plan_dir = root / "ExchangePlans" / "План" / "Ext"
    plan_dir.mkdir(parents=True)
    (root / "ExchangePlans" / "План.xml").write_text(
        _xml_object("ExchangePlan", "План"), encoding="utf-8", newline="\n"
    )
    (plan_dir / "ManagerModule.bsl").write_text(plan, encoding="utf-8", newline="\n")
    for name, text in modules.items():
        folder = root / "CommonModules" / name / "Ext"
        folder.mkdir(parents=True)
        (folder / "Module.bsl").write_text(text, encoding="utf-8", newline="\n")
    for name, text in (subsystems or {}).items():
        folder = root / "Subsystems"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / f"{name}.xml").write_text(text, encoding="utf-8", newline="\n")
    return root


def _plan_of(profile):
    return profile.plans[0]


def _reading(profile, reason: str):
    found = [item for item in reading_skips(profile) if reason in item.reason]
    assert found, [item.reason for item in reading_skips(profile)]
    assert all(item.relative_file and item.line for item in found)
    return found


def test_settings_follow_alias_and_parameter_name(tmp_path: Path):
    helper = (
        "Процедура ЗаполнитьНастройки(Настройки) Экспорт\n"
        "    Настройки.ЭтоПланОбменаXDTO = Истина;\n"
        "    В = Новый Соответствие;\n"
        '    В.Вставить("1.1", М);\n'
        "    Настройки.ВерсииФорматаОбмена = В;\n"
        "КонецПроцедуры\n"
    )
    passed = assemble(
        tmp_path / "passed",
        "Процедура ПриПолученииНастроек(Настройки) Экспорт\n"
        "    Помощник.ЗаполнитьНастройки(Настройки);\n"
        "КонецПроцедуры\n",
        modules={"Помощник": helper},
    )
    profile = read_routes(passed)
    plan = _plan_of(profile)
    assert plan.is_ed is True and plan.status == "complete" and profile.status == "complete"
    assert plan.effective_map() == {"1.1": "М"}
    assert reading_skips(profile) == []

    alias = assemble(
        tmp_path / "alias",
        "Процедура ПриПолученииНастроек(Настройки) Экспорт\n"
        "    Н = Настройки;\n"
        "    Н.ЭтоПланОбменаXDTO = Истина;\n"
        "    ВерсииФормата = Новый Соответствие;\n"
        '    ВерсииФормата.Вставить("1.1", М);\n'
        "    Н.ВерсииФорматаОбмена = ВерсииФормата;\n"
        "КонецПроцедуры\n",
    )
    profile = read_routes(alias)
    plan = _plan_of(profile)
    assert plan.is_ed is True and plan.status == "complete" and profile.status == "complete"
    assert plan.effective_map() == {"1.1": "М"}
    assert reading_skips(profile) == []

    renamed = assemble(
        tmp_path / "renamed",
        "Процедура ПриПолученииНастроек(НастройкиПлана) Экспорт\n"
        "    НастройкиПлана.ЭтоПланОбменаXDTO = Истина;\n"
        "    ВерсииФормата = Новый Соответствие;\n"
        '    ВерсииФормата.Вставить("1.1", М);\n'
        "    НастройкиПлана.ВерсииФорматаОбмена = ВерсииФормата;\n"
        "КонецПроцедуры\n",
    )
    profile = read_routes(renamed)
    plan = _plan_of(profile)
    assert plan.is_ed is True and plan.status == "complete" and profile.status == "complete"
    assert plan.effective_map() == {"1.1": "М"}
    assert reading_skips(profile) == []


def test_unread_setting_fields_are_unknown(tmp_path: Path):
    computed_flag = assemble(
        tmp_path / "flag",
        "Процедура ПриПолученииНастроек(Настройки) Экспорт\n"
        '    Настройки.ЭтоПланОбменаXDTO = ПолучитьФункциональнуюОпцию("Х");\n'
        "    ВерсииФормата = Новый Соответствие;\n"
        '    ВерсииФормата.Вставить("1.1", М);\n'
        "    Настройки.ВерсииФорматаОбмена = ВерсииФормата;\n"
        "КонецПроцедуры\n",
    )
    profile = read_routes(computed_flag)
    plan = _plan_of(profile)
    assert plan.is_ed is None and plan.status == "partial" and profile.status == "partial"
    assert plan.effective_map() == {"1.1": "М"}
    assert plan.registration.mode == "none"
    skip = _reading(profile, "не прочитано")[0]
    assert skip.relative_file.endswith("ManagerModule.bsl") and skip.line == 2

    inserted = assemble(
        tmp_path / "insert",
        "Процедура ПриПолученииНастроек(Настройки) Экспорт\n"
        '    Настройки.Вставить("ЭтоПланОбменаXDTO", Истина);\n'
        "    ВерсииФормата = Новый Соответствие;\n"
        '    ВерсииФормата.Вставить("1.1", М);\n'
        "    Настройки.ВерсииФорматаОбмена = ВерсииФормата;\n"
        "КонецПроцедуры\n",
    )
    profile = read_routes(inserted)
    plan = _plan_of(profile)
    assert plan.is_ed is None and plan.status == "partial" and profile.status == "partial"
    assert plan.effective_map() == {"1.1": "М"}
    assert _reading(profile, "не прочитано")[0].line == 2

    fields = assemble(
        tmp_path / "fields",
        "Процедура ПриПолученииНастроек(Настройки) Экспорт\n"
        "    Настройки.ЭтоПланОбменаXDTO = Истина;\n"
        '    Настройки.ФорматОбмена = ПолучитьФункциональнуюОпцию("Х");\n'
        "    ВерсииФормата = Новый Соответствие;\n"
        '    ВерсииФормата.Вставить("1.1", М);\n'
        "    Настройки.ВерсииФорматаОбмена = Помощник.Версии();\n"
        "КонецПроцедуры\n",
        modules={"Помощник": "Функция Версии() Экспорт\n    Возврат 1;\nКонецФункции\n"},
    )
    profile = read_routes(fields)
    plan = _plan_of(profile)
    assert plan.is_ed is True and plan.base_namespace is None
    assert plan.status == "partial" and profile.status == "partial"
    assert plan.effective_map() == {} and plan.entries == ()
    lines = {item.line for item in _reading(profile, "не прочитано")}
    assert lines == {3, 6}


def test_not_ed_plan_is_partial_when_settings_run_is(tmp_path: Path):
    root = assemble(
        tmp_path / "plain",
        "Процедура ПриПолученииНастроек(Настройки) Экспорт\n"
        "    Настройки.ЭтоПланОбменаXDTO = Ложь;\n"
        '    Настройки.ФорматОбмена = ПолучитьФункциональнуюОпцию("Х");\n'
        "КонецПроцедуры\n",
    )
    profile = read_routes(root)
    plan = _plan_of(profile)
    assert plan.is_ed is False and plan.base_namespace is None
    assert plan.status == "partial" and profile.status == "partial"
    assert plan.entries == ()
    assert _reading(profile, "не прочитано")[0].line == 3


def test_unknown_branch_with_exit_makes_later_entries_conditional(tmp_path: Path):
    early = (
        "Процедура ПриПолученииДоступныхВерсийФормата(ВерсииФормата) Экспорт\n"
        '    Если Не ПолучитьФункциональнуюОпцию("Х") Тогда\n'
        "        Возврат;\n"
        "    КонецЕсли;\n"
        '    ВерсииФормата.Вставить("1.8", М);\n'
        "КонецПроцедуры\n"
    )
    raised = early.replace("        Возврат;", '        ВызватьИсключение "нет";')
    plan = (
        "Процедура ПриПолученииНастроек(Настройки) Экспорт\n"
        "    Настройки.ЭтоПланОбменаXDTO = Истина;\n"
        "    ВерсииФормата = Новый Соответствие;\n"
        '    ВерсииФормата.Вставить("1.1", М);\n'
        "    Настройки.ВерсииФорматаОбмена = ВерсииФормата;\n"
        "КонецПроцедуры\n"
    )
    for folder, text in (("return", early), ("raise", raised)):
        profile = read_routes(assemble(tmp_path / folder, plan, global_text=text))
        assert _plan_of(profile).is_ed is True and _plan_of(profile).status == "complete"
        assert _plan_of(profile).effective_map() == {"1.1": "М"}
        assert profile.without_node_status == "partial" and profile.status == "partial"
        assert [(item.key, item.state) for item in profile.without_node_entries] == [
            ("1.8", "conditional")
        ]
        assert profile.effective_without_node() == {}
        skip = _reading(profile, "Условие по данным")[0]
        assert skip.line == 2 and "ОбменДаннымиПереопределяемый" in skip.relative_file


def test_return_inside_unknown_branch_keeps_both_entries(tmp_path: Path):
    text = (
        "Процедура ПриПолученииДоступныхВерсийФормата(ВерсииФормата) Экспорт\n"
        '    Если ПолучитьФункциональнуюОпцию("Х") Тогда\n'
        '        ВерсииФормата.Вставить("1.1", М);\n'
        "        Возврат;\n"
        "    КонецЕсли;\n"
        '    ВерсииФормата.Вставить("1.2", М);\n'
        "КонецПроцедуры\n"
    )
    plan = (
        "Процедура ПриПолученииНастроек(Настройки) Экспорт\n"
        "    Настройки.ЭтоПланОбменаXDTO = Истина;\n"
        "КонецПроцедуры\n"
    )
    profile = read_routes(assemble(tmp_path / "fork", plan, global_text=text))
    assert profile.without_node_status == "partial" and profile.status == "partial"
    assert [(item.key, item.manager_name, item.state) for item in profile.without_node_entries] == [
        ("1.1", "М", "conditional"),
        ("1.2", "М", "conditional"),
    ]
    assert profile.effective_without_node() == {}
    assert _reading(profile, "Условие по данным")[0].line == 2


def test_map_in_assignment_call_is_skipped(tmp_path: Path):
    text = (
        "Процедура ПриПолученииДоступныхВерсийФормата(ВерсииФормата) Экспорт\n"
        '    ВерсииФормата.Вставить("1.8", М);\n'
        "    Результат = Помощник.Дополнить(ВерсииФормата);\n"
        "КонецПроцедуры\n"
    )
    plan = (
        "Процедура ПриПолученииНастроек(Настройки) Экспорт\n"
        "    Настройки.ЭтоПланОбменаXDTO = Истина;\n"
        "    ВерсииФормата = Новый Соответствие;\n"
        '    ВерсииФормата.Вставить("1.1", М);\n'
        "    Настройки.ВерсииФорматаОбмена = ВерсииФормата;\n"
        "КонецПроцедуры\n"
    )
    profile = read_routes(
        assemble(
            tmp_path / "rhs",
            plan,
            global_text=text,
            modules={
                "Помощник": "Функция Дополнить(Карта) Экспорт\n    Возврат Карта;\nКонецФункции\n"
            },
        )
    )
    assert _plan_of(profile).status == "complete" and _plan_of(profile).effective_map() == {
        "1.1": "М"
    }
    assert profile.without_node_status == "partial" and profile.status == "partial"
    assert profile.effective_without_node() == {"1.8": "М"}
    skip = _reading(profile, "неподдержанный вызов")[0]
    assert skip.line == 3 and "ОбменДаннымиПереопределяемый" in skip.relative_file


def test_conditional_insert_stays_on_its_map(tmp_path: Path):
    text = (
        "Процедура ПриПолученииДоступныхВерсийФормата(ВерсииФормата) Экспорт\n"
        '    ВерсииФормата.Вставить("1.8", М);\n'
        "    Прочее = Новый Соответствие;\n"
        '    Если ПолучитьФункциональнуюОпцию("Х") Тогда\n'
        '        Прочее.Вставить("Ключ", Значение);\n'
        "    КонецЕсли;\n"
        "КонецПроцедуры\n"
    )
    plan = (
        "Процедура ПриПолученииНастроек(Настройки) Экспорт\n"
        "    Настройки.ЭтоПланОбменаXDTO = Истина;\n"
        "    ВерсииФормата = Новый Соответствие;\n"
        '    ВерсииФормата.Вставить("1.1", М);\n'
        "    Настройки.ВерсииФорматаОбмена = ВерсииФормата;\n"
        "КонецПроцедуры\n"
    )
    profile = read_routes(assemble(tmp_path / "other", plan, global_text=text))
    assert profile.effective_without_node() == {"1.8": "М"}
    assert [item.key for item in profile.without_node_entries] == ["1.8"]
    assert profile.without_node_status == "partial" and profile.status == "partial"
    assert _reading(profile, "Условие по данным")[0].line == 4


def test_try_and_unclosed_structure_are_skipped(tmp_path: Path):
    tried = assemble(
        tmp_path / "try",
        "Процедура ПриПолученииНастроек(Настройки) Экспорт\n"
        "    Попытка\n"
        "        Настройки.ЭтоПланОбменаXDTO = Истина;\n"
        "    Исключение\n"
        "    КонецПопытки;\n"
        "    ВерсииФормата = Новый Соответствие;\n"
        '    ВерсииФормата.Вставить("1.1", М);\n'
        "    Настройки.ВерсииФорматаОбмена = ВерсииФормата;\n"
        "КонецПроцедуры\n",
    )
    profile = read_routes(tried)
    plan = _plan_of(profile)
    assert plan.is_ed is None and plan.status == "partial" and profile.status == "partial"
    assert plan.effective_map() == {"1.1": "М"}
    assert _reading(profile, "Цикл или исключение")[0].line == 2

    text = (
        "Процедура ПриПолученииДоступныхВерсийФормата(ВерсииФормата) Экспорт\n"
        '    Если ПолучитьФункциональнуюОпцию("Х") Тогда\n'
        '        ВерсииФормата.Вставить("1.8", М);\n'
        "КонецПроцедуры\n"
    )
    plan_text = (
        "Процедура ПриПолученииНастроек(Настройки) Экспорт\n"
        "    Настройки.ЭтоПланОбменаXDTO = Истина;\n"
        "    ВерсииФормата = Новый Соответствие;\n"
        '    ВерсииФормата.Вставить("1.1", М);\n'
        "    Настройки.ВерсииФорматаОбмена = ВерсииФормата;\n"
        "КонецПроцедуры\n"
    )
    profile = read_routes(assemble(tmp_path / "open", plan_text, global_text=text))
    assert _plan_of(profile).status == "complete" and _plan_of(profile).effective_map() == {
        "1.1": "М"
    }
    assert profile.without_node_entries == ()
    assert profile.without_node_status == "partial" and profile.status == "partial"
    skip = _reading(profile, "Незакрытый")[0]
    assert skip.line == 1 and skip.relative_file.endswith("Module.bsl")


def test_damaged_plan_subsystem_and_module_do_not_drop_profile(tmp_path: Path):
    broken_plan = assemble(
        tmp_path / "plan",
        "Процедура ПриПолученииНастроек(Настройки) Экспорт\nКонецПроцедуры\n",
    )
    (broken_plan / "ExchangePlans" / "План.xml").write_bytes(b"<not-xml")
    profile = read_routes(broken_plan)
    plan = _plan_of(profile)
    assert plan.is_ed is None and plan.status == "partial" and profile.status == "partial"
    skip = _reading(profile, "Повреждённый XML плана")[0]
    assert skip.relative_file == "ExchangePlans/План.xml" and skip.line == 1

    callback = (
        "Процедура ПриОпределенииОтключенныхПодсистем(ОтключенныеПодсистемы) Экспорт\n"
        "КонецПроцедуры\n"
    )
    global_text = (
        "Процедура ПриПолученииДоступныхВерсийФормата(ВерсииФормата) Экспорт\n"
        '    Если ОбщегоНазначения.ПодсистемаСуществует("Родитель") Тогда\n'
        '        ВерсииФормата.Вставить("1.8", М);\n'
        "    КонецЕсли;\n"
        "КонецПроцедуры\n"
    )
    broken_subsystem = assemble(
        tmp_path / "subsystem",
        "Процедура ПриПолученииНастроек(Настройки) Экспорт\n"
        "    Настройки.ЭтоПланОбменаXDTO = Истина;\n"
        "КонецПроцедуры\n",
        modules={"ОбщегоНазначенияПереопределяемый": callback},
        global_text=global_text,
        subsystems={"Родитель": "<not-xml"},
    )
    profile = read_routes(broken_subsystem)
    assert profile.status == "partial" and profile.without_node_status == "partial"
    skip = _reading(profile, "Повреждённый XML подсистемы")[0]
    assert skip.relative_file == "Subsystems/Родитель.xml" and skip.line == 1

    encoded = assemble(
        tmp_path / "encoding",
        "Процедура ПриПолученииНастроек(Настройки) Экспорт\n"
        "    Настройки.ЭтоПланОбменаXDTO = Истина;\n"
        "КонецПроцедуры\n",
    )
    target = encoded / "ExchangePlans" / "План" / "Ext" / "ManagerModule.bsl"
    target.write_bytes("Настройки.ЭтоПланОбменаXDTO = Истина;\n".encode("cp1251"))
    profile = read_routes(encoded)
    plan = _plan_of(profile)
    assert plan.is_ed is None and plan.status == "partial" and profile.status == "partial"
    skip = _reading(profile, "UTF-8")[0]
    assert skip.relative_file.endswith("ManagerModule.bsl") and skip.line == 1

    bare = tmp_path / "configuration"
    bare.mkdir()
    (bare / "Configuration.xml").write_bytes(b"<not-xml")
    with pytest.raises(EdFormatError):
        read_routes(bare)


def test_preprocessor_branches_are_conditional(tmp_path: Path):
    text = (
        "Процедура ПриПолученииДоступныхВерсийФормата(ВерсииФормата) Экспорт\n"
        "#Если Сервер Тогда\n"
        '    ВерсииФормата.Вставить("1.8", М);\n'
        "#Иначе\n"
        '    ВерсииФормата.Вставить("1.8", Помощник);\n'
        "#КонецЕсли\n"
        "КонецПроцедуры\n"
    )
    plan = (
        "Процедура ПриПолученииНастроек(Настройки) Экспорт\n"
        "    Настройки.ЭтоПланОбменаXDTO = Истина;\n"
        "    ВерсииФормата = Новый Соответствие;\n"
        '    ВерсииФормата.Вставить("1.1", М);\n'
        "    Настройки.ВерсииФорматаОбмена = ВерсииФормата;\n"
        "КонецПроцедуры\n"
    )
    profile = read_routes(assemble(tmp_path / "pre", plan, global_text=text))
    assert _plan_of(profile).status == "complete" and _plan_of(profile).effective_map() == {
        "1.1": "М"
    }
    assert profile.without_node_status == "partial" and profile.status == "partial"
    assert profile.effective_without_node() == {}
    assert [(item.key, item.manager_name, item.state) for item in profile.without_node_entries] == [
        ("1.8", "М", "conditional"),
        ("1.8", "Помощник", "conditional"),
    ]
    assert _reading(profile, "препроцессора")[0].line == 2


def test_call_without_map_or_settings_keeps_profile(tmp_path: Path):
    noise = assemble(
        tmp_path / "noise",
        "Процедура ПриПолученииНастроек(Настройки) Экспорт\n"
        "    Настройки.ЭтоПланОбменаXDTO = Истина;\n"
        "    ВерсииФормата = Новый Соответствие;\n"
        '    ВерсииФормата.Вставить("1.1", М);\n'
        "    Настройки.ВерсииФорматаОбмена = ВерсииФормата;\n"
        "    Помощник.Сообщить(НСтр(\"ru = 'x'\"));\n"
        "    НетМодуля.Метод();\n"
        "КонецПроцедуры\n",
        modules={"Помощник": "Процедура Сообщить(Текст) Экспорт\nКонецПроцедуры\n"},
    )
    profile = read_routes(noise)
    plan = _plan_of(profile)
    assert plan.is_ed is True and plan.status == "complete" and profile.status == "complete"
    assert plan.effective_map() == {"1.1": "М"}
    assert reading_skips(profile) == []

    deep = {f"Шаг{index}": "" for index in range(1, 5)}
    deep["Шаг1"] = (
        "Процедура Шаг(ВерсииФормата) Экспорт\n    Шаг2.Шаг(ВерсииФормата);\nКонецПроцедуры\n"
    )
    deep["Шаг2"] = (
        "Процедура Шаг(ВерсииФормата) Экспорт\n    Шаг3.Шаг(ВерсииФормата);\nКонецПроцедуры\n"
    )
    deep["Шаг3"] = (
        "Процедура Шаг(ВерсииФормата) Экспорт\n    Шаг4.Шаг(ВерсииФормата);\nКонецПроцедуры\n"
    )
    deep["Шаг4"] = (
        "Процедура Шаг(ВерсииФормата) Экспорт\n"
        "    Помощник.Сообщить(НСтр(\"ru = 'x'\"));\n"
        "    НетМодуля.Метод();\n"
        '    ВерсииФормата.Вставить("1.1", М);\n'
        "КонецПроцедуры\n"
    )
    deep["Помощник"] = "Процедура Сообщить(Текст) Экспорт\nКонецПроцедуры\n"
    rooted = assemble(
        tmp_path / "deep-noise",
        "Процедура ПриПолученииНастроек(Настройки) Экспорт\n"
        "    Настройки.ЭтоПланОбменаXDTO = Истина;\n"
        "    ВерсииФормата = Новый Соответствие;\n"
        "    Шаг1.Шаг(ВерсииФормата);\n"
        "    Настройки.ВерсииФорматаОбмена = ВерсииФормата;\n"
        "КонецПроцедуры\n",
        modules=deep,
    )
    profile = read_routes(rooted)
    assert profile.status == "complete"
    assert _plan_of(profile).effective_map() == {"1.1": "М"}
    assert reading_skips(profile) == []


def test_registration_under_data_guard_is_unknown(tmp_path: Path):
    root = assemble(
        tmp_path / "registration",
        "Процедура ПриПолученииНастроек(Настройки) Экспорт\n"
        "    Настройки.ЭтоПланОбменаXDTO = Истина;\n"
        "    ВерсииФормата = Новый Соответствие;\n"
        '    ВерсииФормата.Вставить("1.1", М);\n'
        "    Настройки.ВерсииФорматаОбмена = ВерсииФормата;\n"
        '    Если ПолучитьФункциональнуюОпцию("Х") Тогда\n'
        "        Настройки.ПравилаРегистрацииВМенеджере = Истина;\n"
        '        Настройки.ИмяМенеджераРегистрации = "Помощник";\n'
        "    КонецЕсли;\n"
        "КонецПроцедуры\n",
    )
    profile = read_routes(root)
    plan = _plan_of(profile)
    assert plan.is_ed is True and plan.status == "partial" and profile.status == "partial"
    assert plan.effective_map() == {"1.1": "М"}
    assert plan.registration.mode == "unknown"
    assert _reading(profile, "Условие по данным")[0].line == 6


def test_visited_pairs_stop_after_64(tmp_path: Path):
    def modules_for(count: int) -> dict[str, str]:
        found = {}
        for index in range(1, count + 1):
            found[f"Шаг{index}"] = (
                "Процедура Шаг(ВерсииФормата) Экспорт\n"
                f'    ВерсииФормата.Вставить("{index}", М);\n'
                "КонецПроцедуры\n"
            )
        return found

    def plan_for(count: int) -> str:
        calls = "\n".join(f"    Шаг{index}.Шаг(ВерсииФормата);" for index in range(1, count + 1))
        return (
            "Процедура ПриПолученииНастроек(Настройки) Экспорт\n"
            "    Настройки.ЭтоПланОбменаXDTO = Истина;\n"
            "    ВерсииФормата = Новый Соответствие;\n"
            f"{calls}\n"
            "    Настройки.ВерсииФорматаОбмена = ВерсииФормата;\n"
            "КонецПроцедуры\n"
        )

    inside = read_routes(assemble(tmp_path / "64", plan_for(63), modules=modules_for(63)))
    assert inside.status == "complete"
    assert len(_plan_of(inside).effective_map()) == 63
    outside = read_routes(assemble(tmp_path / "65", plan_for(64), modules=modules_for(64)))
    assert outside.status == "partial"
    assert _reading(outside, "посещённых")[0].line == 1


def test_condition_depth_stops_after_32(tmp_path: Path):
    def plan_for(depth: int) -> str:
        opening = "\n".join("    Если Истина Тогда" for _ in range(depth))
        closing = "\n".join("    КонецЕсли;" for _ in range(depth))
        return (
            "Процедура ПриПолученииНастроек(Настройки) Экспорт\n"
            "    Настройки.ЭтоПланОбменаXDTO = Истина;\n"
            "    ВерсииФормата = Новый Соответствие;\n"
            f"{opening}\n"
            '        ВерсииФормата.Вставить("1.1", М);\n'
            f"{closing}\n"
            "    Настройки.ВерсииФорматаОбмена = ВерсииФормата;\n"
            "КонецПроцедуры\n"
        )

    inside = read_routes(assemble(tmp_path / "32", plan_for(32)))
    assert inside.status == "complete" and _plan_of(inside).effective_map() == {"1.1": "М"}
    outside = read_routes(assemble(tmp_path / "33", plan_for(33)))
    assert outside.status == "partial" and _plan_of(outside).effective_map() == {}
    assert _reading(outside, "глубина условий")


def test_calls_stop_after_256(tmp_path: Path):
    helper = "Процедура Шаг(ВерсииФормата) Экспорт\nКонецПроцедуры\n"

    def plan_for(count: int) -> str:
        calls = "\n".join("    Помощник.Шаг(ВерсииФормата);" for _ in range(count))
        return (
            "Процедура ПриПолученииНастроек(Настройки) Экспорт\n"
            "    Настройки.ЭтоПланОбменаXDTO = Истина;\n"
            "    ВерсииФормата = Новый Соответствие;\n"
            f"{calls}\n"
            '    ВерсииФормата.Вставить("1.1", М);\n'
            "    Настройки.ВерсииФорматаОбмена = ВерсииФормата;\n"
            "КонецПроцедуры\n"
        )

    inside = read_routes(assemble(tmp_path / "256", plan_for(256), modules={"Помощник": helper}))
    assert inside.status == "complete" and _plan_of(inside).effective_map() == {"1.1": "М"}
    outside = read_routes(assemble(tmp_path / "257", plan_for(257), modules={"Помощник": helper}))
    assert outside.status == "partial"
    assert _reading(outside, "предел вызовов")


def test_map_entries_stop_after_4096(tmp_path: Path):
    def plan_for(count: int) -> str:
        inserts = "\n".join(
            f'    ВерсииФормата.Вставить("{index}", М);' for index in range(1, count + 1)
        )
        return (
            "Процедура ПриПолученииНастроек(Настройки) Экспорт\n"
            "    Настройки.ЭтоПланОбменаXDTO = Истина;\n"
            "    ВерсииФормата = Новый Соответствие;\n"
            "    Настройки.ВерсииФорматаОбмена = ВерсииФормата;\n"
            f"{inserts}\n"
            "КонецПроцедуры\n"
        )

    inside = read_routes(assemble(tmp_path / "4096", plan_for(4096)))
    assert inside.status == "complete" and len(_plan_of(inside).effective_map()) == 4096
    outside = read_routes(assemble(tmp_path / "4097", plan_for(4097)))
    assert outside.status == "partial" and len(_plan_of(outside).effective_map()) == 4096
    assert _reading(outside, "предел записей")


def test_two_reads_match_and_trivia_changes_fingerprint(tmp_path: Path):
    first = load("grammar")
    second = load("grammar")
    assert first == second
    shutil.copytree(DATA / "grammar", tmp_path / "grammar")
    target = tmp_path / "grammar" / "CommonModules" / "ПоставщикВерсий" / "Ext" / "Module.bsl"
    target.write_text(
        target.read_text(encoding="utf-8") + "\n// trivia\n", encoding="utf-8", newline="\n"
    )
    changed = read_routes(tmp_path / "grammar")
    assert changed.plans[0].effective_map() == first.plans[0].effective_map()
    assert changed.effective_without_node() == first.effective_without_node()
    assert changed.sources_fingerprint != first.sources_fingerprint
    original = next(item.source.sha256 for item in first.without_node_entries if item.key == "1.8")
    updated = next(item.source.sha256 for item in changed.without_node_entries if item.key == "1.8")
    assert original != updated
