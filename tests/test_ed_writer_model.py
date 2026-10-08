"""Публичные инварианты автора ED на синтетических декларациях, без типового кода."""

from dataclasses import replace
from pathlib import Path

import pytest

from kd_rules_mcp.authoring.ed.manager_operations import (
    Action,
    IdentificationPatch,
    ManagerOperation,
    ManagerOperationError,
    ManagerPatch,
    PkoPatch,
    PodPatch,
    PropertyPatch,
    apply,
    parse_operation,
    preview,
)
from kd_rules_mcp.authoring.ed.workspace import ManagerWorkspace
from kd_rules_mcp.ed.canonical import canonical_model, canonicalize, model_addresses
from kd_rules_mcp.ed.diff import compare_models
from kd_rules_mcp.ed.forms import HELPER_PKS
from kd_rules_mcp.ed.reader import read_manager_text
from kd_rules_mcp.ed.writer import render
from kd_rules_mcp.ed.writer_import import import_manager
from kd_rules_mcp.ed.writer_model import (
    ExecutorProfile,
    Formal,
    LayoutElement,
    ManagerModel,
    Reference,
    Value,
    decode_dto,
    dump_model,
    load_model,
    logical_id,
    partition_report,
    text_hash,
    validate_model,
)
from kd_rules_mcp.errors import EdAuthoringResourceLimitError, EdAuthoringStaleError

# Формы: reference/kd3-cfg/DataProcessors/ВыгрузкаМодуля/Templates/
# ШаблоныТекстовМодулей/Ext/Template.txt:1,19–72,109,151,175–199.
SYNTHETIC = """// Менеджер обмена через универсальный формат (Тест)
Функция ВерсияФорматаМенеджераОбмена() Экспорт
    Возврат "2";
КонецФункции
Процедура ЗаполнитьПравилаКонвертацииОбъектов(НаправлениеОбмена, ПравилаКонвертации) Экспорт
    Если НаправлениеОбмена = "Отправка" Тогда
        ДобавитьПКО_Send(ПравилаКонвертации);
    КонецЕсли;
    Если НаправлениеОбмена = "Получение" Тогда
        ДобавитьПКО_Receive(ПравилаКонвертации);
    КонецЕсли;
КонецПроцедуры
Процедура ДобавитьПКО_Send(ПравилаКонвертации)
    ПравилоКонвертации = ОбменДаннымиXDTOСервер.ИнициализироватьПравилоКонвертацииОбъекта(
        ПравилаКонвертации);
    ПравилоКонвертации.ИмяПКО = "Item";
    ПравилоКонвертации.ОбъектДанных = Метаданные.Справочники.Items;
    ПравилоКонвертации.ОбъектФормата = "Item";
    СвойстваШапки = ПравилоКонвертации.Свойства;
    ДобавитьПКС(СвойстваШапки, "Code", "Code");
    ДобавитьПКС(СвойстваШапки, "Link", "Link", 0, "Other");
    ДобавитьПКС(СвойстваШапки, "Calculated", "Calculated", 1);
    СвойстваТЧ = ДобавитьПКТЧ(ПравилоКонвертации, "Rows", "Rows");
    ДобавитьПКС(СвойстваТЧ, "Value", "Value");
    СвойстваТЧ = ДобавитьПКТЧ(ПравилоКонвертации, "Extra", "Extra");
    ДобавитьПКС(СвойстваТЧ, "Value", "Value");
КонецПроцедуры
Процедура ДобавитьПКО_Receive(ПравилаКонвертации)
    ПравилоКонвертации = ОбменДаннымиXDTOСервер.ИнициализироватьПравилоКонвертацииОбъекта(
        ПравилаКонвертации);
    ПравилоКонвертации.ИмяПКО = "Item";
    ПравилоКонвертации.ОбъектФормата = "Item";
    ПравилоКонвертации.ВариантИдентификации = "ПоПолямПоиска";
    СвойстваШапки = ПравилоКонвертации.Свойства;
    ДобавитьПКС(СвойстваШапки, "Name", "Name", , , "urn:test");
    ПравилоКонвертации.ПоляПоиска.Добавить("Code,Name");
    ПравилоКонвертации.ПоляПоиска.Добавить("Name");
КонецПроцедуры
Процедура ЗаполнитьПравилаОбработкиДанных(НаправлениеОбмена, ПравилаОбработкиДанных) Экспорт
    Если НаправлениеОбмена = "Получение" Тогда
        ДобавитьПОД_Items(ПравилаОбработкиДанных);
    КонецЕсли;
КонецПроцедуры
Процедура ДобавитьПОД_Items(ПравилаОбработкиДанных)
    ПравилоОбработки = ПравилаОбработкиДанных.Добавить();
    ПравилоОбработки.Имя = "Items";
    ПравилоОбработки.ОбъектВыборкиФормат = "Item";
    ПравилоОбработки.ОчисткаДанных = Ложь;
КонецПроцедуры
Процедура ЗаполнитьПараметрыКонвертации(ПараметрыКонвертации) Экспорт
    ПараметрыКонвертации.Вставить("Option");
КонецПроцедуры
Процедура ПередКонвертацией(КомпонентыОбмена) Экспорт
    // пустое событие
КонецПроцедуры
"""


def imported(text: str = SYNTHETIC):
    return import_manager(read_manager_text(text), project_id="test", manager_name="TestManager")


def execute(model, *operations):
    # Старые сценарии W1 получают явную позицию; новые проверки раскладки ниже
    # задают её самостоятельно и проверяют обязательность через preview.
    prepared = []
    last = {c.logical_id: c.elements[-1].logical_id if c.elements else None for c in model.layouts}
    for operation in operations:
        if operation.action in ("create", "move") and operation.container_id is None:
            owner = operation.owner_id or next(
                (
                    r.logical_id
                    for r in model.pko
                    if any(p.logical_id == operation.target_id for p in r.properties)
                ),
                None,
            )
            container = owner or model.root_layouts[0]
            operation = replace(
                operation,
                container_id=container,
                after_id=operation.after_id
                or (last.get(container) if operation.action == "create" else None),
            )
            if operation.action == "create":
                last[container] = logical_id(model.project_id, operation.client_id)
        prepared.append(operation)
    operations = tuple(prepared)
    plan = preview(model, operations, expected_revision=model.revision)
    assert not plan.failures
    return apply(
        model, operations, expected_revision=model.revision, expected_preview_hash=plan.preview_hash
    )


def test_import_counts_states_and_exact_blocks():
    document = read_manager_text(SYNTHETIC)
    model, report = imported()
    assert model.counts == {key: document.counts[key] for key in model.counts}
    assert report.counts["pks"] == {"editable": 4, "retained": 2, "blocked": 0}
    assert report.counts["pko"]["editable"] == 2
    assert model.pko[0].directions == ("send",)
    assert model.pko[1].directions == ("receive",)
    assert model.pko[0].properties[0].configuration_property == "Code"
    assert model.pko[1].properties[0].configuration_property == "Name"
    assert model.pko[1].properties[0].argument_presence == (True, True, True, False, False, True)
    assert model.pod[0].clear_data == Value("boolean", False)
    assert model.parameters[0].default == Value()
    assert model.conversion_events[0].state == "editable"
    assert all(text_hash(block.text) == block.sha256 for block in model.retained_blocks)
    assert all(block.text in document.files[0].text for block in model.retained_blocks)
    assert canonicalize(load_model(dump_model(model))) == canonicalize(model)
    assert dump_model(model) == dump_model(load_model(dump_model(model)))


@pytest.mark.parametrize(
    "value",
    [
        Value(),
        Value("string", ""),
        Value("boolean", False),
        Value("number", 0),
        Value("undefined"),
        Value("unknown", raw="X()"),
    ],
)
def test_values_do_not_collapse(value):
    model, _ = imported()
    parameter = replace(model.parameters[0], default=value)
    updated = replace(model, parameters=(parameter,)).with_revision()
    assert load_model(dump_model(updated)).parameters[0].default == value
    other = replace(
        model, parameters=(replace(parameter, default=Value("undefined")),)
    ).with_revision()
    assert compare_models(updated, other).equal == (value.state == "undefined")


def test_canonical_excludes_coordinates_and_maps_logical_ids():
    model, _ = imported()
    maps = tuple(
        replace(
            entry,
            reader_id="moved:" + entry.reader_id,
            char_start=entry.char_start + 10,
            char_end=entry.char_end + 10,
            line_start=entry.line_start + 1,
            line_end=entry.line_end + 1,
        )
        for entry in model.source_map
    )
    moved = replace(model, source_map=maps).with_revision()
    assert canonical_model(model).value == canonical_model(moved).value
    assert compare_models(model, moved).equal
    other, _ = import_manager(read_manager_text(SYNTHETIC, file_id="other"), project_id="other")
    other = replace(other, header=model.header).with_revision()
    assert canonicalize(model) == canonicalize(other)
    diff = compare_models(model, other)
    assert diff.equal and len(diff.id_map) == len(model.members())


def test_order_search_groups_signatures_and_direct_edit_diff():
    model, _ = imported()
    receiving = model.pko[1]
    identification = replace(
        receiving.identification, search_sets=receiving.identification.search_sets[::-1]
    )
    changed = replace(
        model, pko=(model.pko[0], replace(receiving, identification=identification))
    ).with_revision()
    c = next(c for c in model.layouts if c.logical_id == receiving.logical_id)
    positions = [
        n
        for n, e in enumerate(c.elements)
        if e.entity_id in {s.logical_id for s in receiving.identification.search_sets}
    ]
    rows = list(c.elements)
    rows[positions[0]], rows[positions[1]] = rows[positions[1]], rows[positions[0]]
    changed = replace(
        changed,
        layouts=tuple(
            replace(row, elements=tuple(rows)) if row is c else row for row in model.layouts
        ),
    ).with_revision()
    assert canonicalize(model) != canonicalize(changed)
    assert not compare_models(model, changed).equal
    sending = model.pko[0]
    layout = next(c for c in model.layouts if c.logical_id == sending.logical_id)
    reordered = replace(
        model,
        layouts=tuple(
            replace(c, elements=c.elements[:-2] + c.elements[-2:][::-1]) if c is layout else c
            for c in model.layouts
        ),
    )
    assert canonicalize(model) != canonicalize(reordered)
    unit = model.code_units[0]
    signature = replace(unit.signature, parameters=(Formal("КомпонентыОбмена", True),))
    assert canonicalize(model) != canonicalize(
        replace(model, code_units=(replace(unit, signature=signature),))
    )
    op = ManagerOperation(
        "edit",
        "property",
        "update",
        target_id=sending.properties[0].logical_id,
        patch=PropertyPatch(configuration_property="NewCode"),
    )
    updated = execute(model, op)
    diff = compare_models(model, updated)
    assert len(diff.changes) == 1
    assert diff.changes[0].address.endswith("/ПКС/Code")
    assert updated.retained_blocks == model.retained_blocks


@pytest.mark.parametrize(
    "addition",
    [
        "Unexpected();",
        'ПравилоКонвертации.ОбъектФормата = "Again";',
        "Если CustomCondition Тогда Unexpected(); КонецЕсли;",
    ],
)
def test_unknown_and_repeated_assignment_are_retained_and_not_editable(addition):
    model, report = imported(
        SYNTHETIC.replace(
            'ДобавитьПКС(СвойстваШапки, "Code", "Code");',
            'ДобавитьПКС(СвойстваШапки, "Code", "Code");\n' + addition,
        )
    )
    assert report.counts["pko"]["retained"] >= 1
    assert any(addition in b.text for b in model.retained_blocks)
    assert canonicalize(model) == canonicalize(load_model(dump_model(model)))
    op = ManagerOperation(
        "unsafe",
        "property",
        "update",
        target_id=model.pko[0].properties[0].logical_id,
        patch=PropertyPatch(configuration_property="Changed"),
    )
    plan = preview(model, (op,), expected_revision=model.revision)
    assert plan.failures[0].reason == "opaque_context_changed"
    assert plan.model == model and not plan.changes


def test_bom_crlf_and_multiline_code_are_exact():
    text = SYNTHETIC.replace(
        "// пустое событие",
        'Текст = "Item\n|Процедура // бизнес-текст";\n'
        "#Если Сервер Тогда\nТекст = Текст;\n#КонецЕсли",
    )
    text = "\ufeff" + text.replace("\n", "\r\n")
    model, _ = imported(text)
    loaded = load_model(dump_model(model))
    assert loaded.source_files[0].bytes() == text.encode("utf-8")
    assert loaded.header.text_style.bom and loaded.header.text_style.newline == "\r\n"
    assert loaded.code_units[0].body == model.code_units[0].body
    assert loaded.conversion_events[0].state == "editable"


def test_changed_helper_and_missing_dispatch_case_are_visible():
    helper = HELPER_PKS.format(parameter="", check="")
    a, _ = imported(SYNTHETIC + helper)
    b, report = imported(SYNTHETIC + helper.replace("НоваяСтрока =", "ДругаяСтрока ="))
    assert any(code == "helper_semantics_unverified" for code, _ in report.diagnostics)
    assert not compare_models(a, b).equal
    dispatcher = """\nПроцедура ВыполнитьПроцедуруМодуляМенеджера(ИмяПроцедуры, Параметры) Экспорт
Если ИмяПроцедуры = "Before" Тогда ПередКонвертацией(Параметры.КомпонентыОбмена); КонецЕсли;
КонецПроцедуры"""
    a, _ = imported(SYNTHETIC + dispatcher)
    b, _ = imported(
        SYNTHETIC + dispatcher.replace("ПередКонвертацией(Параметры.КомпонентыОбмена);", "")
    )
    assert len(a.dispatcher_cases) == 1 and not b.dispatcher_cases
    assert not compare_models(a, b).equal


def test_preview_atomic_stale_conflict_and_idempotence():
    model, _ = imported()
    op = ManagerOperation(
        "edit",
        "property",
        "update",
        target_id=model.pko[0].properties[0].logical_id,
        patch=PropertyPatch(configuration_property="NewCode"),
    )
    plan = preview(model, (op,), expected_revision=model.revision)
    assert model.pko[0].properties[0].configuration_property == "Code"
    assert plan.preview_hash == preview(model, (op,), expected_revision=model.revision).preview_hash
    with pytest.raises(EdAuthoringStaleError):
        apply(model, (op,), expected_revision=model.revision, expected_preview_hash="wrong")
    updated = execute(model, op)
    assert (
        apply(
            updated,
            (op,),
            expected_revision=model.revision,
            expected_preview_hash=plan.preview_hash,
        )
        is updated
    )
    conflict = replace(op, patch=PropertyPatch(configuration_property="OtherCode"))
    assert preview(updated, (conflict,), expected_revision=updated.revision).failures
    with pytest.raises(EdAuthoringStaleError):
        preview(updated, (), expected_revision=model.revision)
    bad = ManagerOperation("unsupported", "table_part", "create")
    refused = preview(model, (op, bad), expected_revision=model.revision)
    assert refused.model == model and not refused.changes
    assert "владельца" in refused.failures[0].message
    with pytest.raises(ManagerOperationError):
        apply(
            model,
            (op, bad),
            expected_revision=model.revision,
            expected_preview_hash=refused.preview_hash,
        )


def test_new_ids_moves_clears_and_non_cascading_delete():
    model = ManagerModel("new").with_revision()
    create = ManagerOperation(
        "pko", "pko", "create", patch=PkoPatch(name="Item", directions=("receive",))
    )
    model = execute(model, create)
    key = logical_id("new", "pko")
    assert model.pko[0].logical_id == key
    add = ManagerOperation(
        "property",
        "property",
        "create",
        owner_id=key,
        patch=PropertyPatch(configuration_property="Code", format_property="Code"),
    )
    model = execute(model, add)
    pod = ManagerOperation(
        "pod",
        "pod",
        "create",
        patch=PodPatch(
            name="Items",
            directions=("receive",),
            used_pko=(Reference("pko", key, "Item", "resolved"),),
        ),
    )
    model = execute(model, pod)
    delete = ManagerOperation("delete", "pko", "delete", target_id=key)
    plan = preview(model, (delete,), expected_revision=model.revision)
    assert plan.failures
    model = execute(
        model,
        ManagerOperation(
            "property2",
            "property",
            "create",
            owner_id=key,
            patch=PropertyPatch(configuration_property="Name", format_property="Name"),
        ),
    )
    moved = execute(
        model,
        ManagerOperation(
            "move",
            "property",
            "move",
            target_id=logical_id("new", "property2"),
            container_id=key,
            after_id=None,
        ),
    )
    assert moved.ordered_entity_ids(key)[0] == logical_id("new", "property2")
    cleared = execute(
        moved, ManagerOperation("clear", "pko", "update", target_id=key, clear=("format_object",))
    )
    assert cleared.pko[0].format_object.state == "unset"
    operations = (
        ManagerOperation("delprop", "property", "delete", target_id=logical_id("new", "property")),
        ManagerOperation(
            "delprop2", "property", "delete", target_id=logical_id("new", "property2")
        ),
        delete,
    )
    plan = preview(cleared, operations, expected_revision=cleared.revision)
    assert plan.failures[-1].reason == "dangling_reference"
    assert any("ПОД/Items" in address for address in plan.failures[-1].references)
    fixed = execute(
        cleared,
        *operations,
        ManagerOperation(
            "unlink",
            "pod",
            "update",
            target_id=model.pod[0].logical_id,
            patch=PodPatch(used_pko=()),
        ),
    )
    assert not fixed.pko and not fixed.pod[0].used_pko


def test_identification_manager_and_strict_schema():
    model, _ = imported()
    rule = model.pko[1]
    updated = execute(
        model,
        ManagerOperation(
            "search",
            "identification",
            "update",
            target_id=rule.identification.logical_id,
            patch=IdentificationPatch(search_sets=(("Name",), ("Code", "Name"))),
        ),
    )
    assert updated.pko[1].identification.search_sets[0].fields == ("Name",)
    title = ManagerOperation(
        "title", "manager", "update", patch=ManagerPatch(title=Value("string", "Title"))
    )
    assert execute(updated, title).header.title.value == "Title"
    with pytest.raises(ValueError):
        parse_operation(
            {"client_id": "x", "kind": "property", "action": "update", "patch": {"surprise": True}}
        )
    with pytest.raises(ValueError):
        parse_operation(
            {
                "client_id": "x",
                "kind": "pko",
                "action": "create",
                "patch": {"directions": ["sideways"]},
            }
        )
    known = parse_operation(
        {"client_id": "x", "kind": "algorithm", "action": "create", "patch": {"body": "Whatever"}}
    )
    assert preview(model, (known,), expected_revision=model.revision).failures
    with pytest.raises(ValueError, match="DTO"):
        decode_dto(ExecutorProfile, {"runtime_verified": True})


def test_workspace_recovery_concurrency_and_owned_bodies(tmp_path: Path):
    model, _ = imported()
    first = ManagerWorkspace(tmp_path)
    first.create(model)
    second = ManagerWorkspace(tmp_path)
    assert second.get("test").model == model
    op = ManagerOperation(
        "title", "manager", "update", patch=ManagerPatch(title=Value("string", "Changed"))
    )
    plan = first.preview("test", (op,), expected_revision=model.revision)
    changed = first.apply(
        "test", (op,), expected_revision=model.revision, expected_preview_hash=plan.preview_hash
    )
    with pytest.raises(EdAuthoringStaleError):
        second.apply(
            "test", (op,), expected_revision=model.revision, expected_preview_hash=plan.preview_hash
        )
    restored = ManagerWorkspace(tmp_path)
    assert restored.get("test").model == changed.model
    body = next((tmp_path / ".ed-projects/test/bodies").glob("*.bsl"))
    body.write_bytes(b"changed")
    next_op = replace(op, client_id="title2", patch=ManagerPatch(title=Value("string", "Again")))
    next_plan = restored.preview("test", (next_op,), expected_revision=changed.model.revision)
    with pytest.raises(EdAuthoringStaleError):
        restored.apply(
            "test",
            (next_op,),
            expected_revision=changed.model.revision,
            expected_preview_hash=next_plan.preview_hash,
        )
    with pytest.raises(ValueError, match="Повреждённый"):
        ManagerWorkspace(tmp_path).get("test")


def test_workspace_close_and_path_boundary(tmp_path: Path):
    workspace = ManagerWorkspace(tmp_path)
    workspace.create(ManagerModel("new").with_revision())
    published = tmp_path / "published.bsl"
    published.write_bytes(b"result")
    assert workspace.close("new")
    assert published.read_bytes() == b"result" and not workspace.ids()
    with pytest.raises(ValueError):
        workspace.create(ManagerModel("../outside").with_revision())


def test_blocked_span_is_explicit_and_header_versions_are_editable():
    document = read_manager_text(SYNTHETIC)
    corrupt = replace(
        document, pko=(replace(document.pko[0], raw_text="unproven"), document.pko[1])
    )
    model, report = import_manager(corrupt, project_id="blocked")
    assert report.counts["pko"]["blocked"] == 1
    assert model.pko[0].state == "blocked"
    assert any(block.state == "blocked" for block in model.retained_blocks)
    model, report = imported()
    assert report.counts["conversion"]["editable"] == 1
    for version in (1, 3):
        changed = execute(
            model,
            ManagerOperation(
                f"v{version}", "manager", "update", patch=ManagerPatch(interface_version=version)
            ),
        )
        assert changed.header.interface_version == version
        assert not hasattr(changed.executor_profile, "runtime_verified")


def test_namespace_update_and_invalid_argument_positions():
    model, _ = imported()
    prop = model.pko[0].properties[0]
    updated = execute(
        model,
        ManagerOperation(
            "namespace",
            "property",
            "update",
            target_id=prop.logical_id,
            patch=PropertyPatch(namespace="urn:new"),
        ),
    )
    prop = updated.pko[0].properties[0]
    assert prop.argument_presence == (True, True, True, False, False, True)
    assert prop.argument_values[4] == Value("string", "urn:new")
    assert len(compare_models(model, updated).changes) == 1
    bad = ManagerOperation(
        "bad-positions",
        "property",
        "update",
        target_id=prop.logical_id,
        patch=PropertyPatch(argument_presence=(False,)),
    )
    assert preview(updated, (bad,), expected_revision=updated.revision).failures


def test_known_and_computed_code_references_require_confirmation():
    code = """\n#Область Алгоритмы
Процедура Business(КомпонентыОбмена)
    ОбменДаннымиXDTOСервер.ПКОПоИмени(КомпонентыОбмена, "Item");
КонецПроцедуры
#КонецОбласти
"""
    for text in (code, code.replace('"Item"', "RuleName")):
        model, _ = imported(SYNTHETIC + text)
        op = ManagerOperation(
            "rename",
            "pko",
            "update",
            target_id=model.pko[0].logical_id,
            patch=PkoPatch(name="Renamed"),
        )
        plan = preview(model, (op,), expected_revision=model.revision)
        assert not plan.failures and plan.notices[0].references
        with pytest.raises(ManagerOperationError):
            apply(
                model,
                (op,),
                expected_revision=model.revision,
                expected_preview_hash=plan.preview_hash,
            )
        confirmed = apply(
            model,
            (op,),
            expected_revision=model.revision,
            expected_preview_hash=plan.preview_hash,
            confirmations=tuple((n.code, n.notice_hash) for n in plan.notices),
        )
        assert confirmed.pko[0].name == "Renamed"
        # Литерал тела остаётся прежним; индекс следует новому пространству имён.
        assert (
            tuple(
                replace(unit, dependencies=old.dependencies)
                for unit, old in zip(confirmed.code_units, model.code_units, strict=True)
            )
            == model.code_units
        )
        if '"Item"' in text:
            business = next(unit for unit in confirmed.code_units if unit.name == "Business")
            previous = next(unit for unit in model.code_units if unit.name == "Business")
            assert business.dependencies != previous.dependencies
        back, _ = imported(render(confirmed).data.decode("utf-8"))
        assert canonicalize(confirmed)["code_units"] == canonicalize(back)["code_units"]
    model, _ = imported(
        SYNTHETIC
        + code.replace(
            'ОбменДаннымиXDTOСервер.ПКОПоИмени(КомпонентыОбмена, "Item");',
            'Сообщить("Item is only business text");',
        )
    )
    op = ManagerOperation(
        "rename", "pko", "update", target_id=model.pko[1].logical_id, patch=PkoPatch(name="Renamed")
    )
    assert execute(model, op).pko[1].name == "Renamed"
    renamed = execute(model, op)
    assert any(use.rule.name == "ДобавитьПКО_Renamed" for use in renamed.rule_uses)


def test_operation_limits_and_same_name_collision():
    model = ManagerModel("new").with_revision()
    a = ManagerOperation("a", "pko", "create", patch=PkoPatch(name="Item", directions=("send",)))
    b = replace(a, client_id="b")
    assert preview(model, (a, b), expected_revision=model.revision).failures
    assert preview(
        model,
        (a, replace(b, patch=PkoPatch(name="Item", directions=("receive",)))),
        expected_revision=model.revision,
    ).failures
    with pytest.raises(EdAuthoringResourceLimitError):
        preview(
            model,
            tuple(replace(a, client_id=str(n)) for n in range(101)),
            expected_revision=model.revision,
        )
    with pytest.raises(ValueError):
        parse_operation(
            {
                "client_id": "bad",
                "kind": "pko",
                "action": "create",
                "patch": {"group_flag": {"state": "boolean", "value": 0}},
            }
        )


def test_snapshot_failure_rolls_back_memory_and_manifest(tmp_path: Path, monkeypatch):
    import kd_rules_mcp.authoring.ed.workspace as storage

    model, _ = imported()
    workspace = ManagerWorkspace(tmp_path)
    workspace.create(model)
    operation = ManagerOperation(
        "title", "manager", "update", patch=ManagerPatch(title=Value("string", "Changed"))
    )
    plan = workspace.preview("test", (operation,), expected_revision=model.revision)

    def failure(*args):
        raise OSError("Синтетический обрыв записи")

    monkeypatch.setattr(storage.os, "replace", failure)
    with pytest.raises(OSError):
        workspace.apply(
            "test",
            (operation,),
            expected_revision=model.revision,
            expected_preview_hash=plan.preview_hash,
        )
    assert workspace.get("test").model == model
    assert ManagerWorkspace(tmp_path).get("test").model == model


def test_report_addresses_and_rule_moves_are_executable():
    model, report = imported()
    entry = next(
        entry for entry in report.entries if entry.kind == "pks" and entry.state == "editable"
    )
    changed = execute(
        model,
        ManagerOperation(
            "address",
            "property",
            "update",
            address=entry.address,
            patch=PropertyPatch(configuration_property="NewCode"),
        ),
    )
    assert changed.pko[0].properties[0].configuration_property == "NewCode"
    model = ManagerModel("moves").with_revision()
    for kind in ("pko", "pod"):
        for name in ("First", "Second"):
            model = execute(
                model,
                parse_operation(
                    {
                        "client_id": kind + name,
                        "kind": kind,
                        "action": "create",
                        "patch": {"name": name, "directions": ["send"]},
                    }
                ),
            )
        target = getattr(model, kind)[1].logical_id
        model = execute(
            model,
            parse_operation(
                {
                    "client_id": kind + "move",
                    "kind": kind,
                    "action": "move",
                    "target_id": target,
                    "container_id": model.root_layouts[0],
                    "after_id": None,
                }
            ),
        )
        assert model.ordered_entity_ids(model.root_layouts[0])[0] == target


def probe_module(body: str, uses: str | None = None, extra: str = "") -> str:
    """Минимальные входы независимого ревью, без типового модуля."""
    source = SYNTHETIC[: SYNTHETIC.index("Процедура ДобавитьПКО_Send")]
    if uses is not None:
        left = source.index('    Если НаправлениеОбмена = "Отправка"')
        source = source[:left] + uses + "\nКонецПроцедуры\n"
    else:
        source = source.replace("        ДобавитьПКО_Receive(ПравилаКонвертации);", "")
    return (
        source
        + """Процедура ДобавитьПКО_Send(ПравилаКонвертации)
    ПравилоКонвертации = ОбменДаннымиXDTOСервер.ИнициализироватьПравилоКонвертацииОбъекта(
        ПравилаКонвертации);
    ПравилоКонвертации.ИмяПКО = "Item";
    ПравилоКонвертации.ОбъектФормата = "Item";
    СвойстваШапки = ПравилоКонвертации.Свойства;
"""
        + body
        + "\nКонецПроцедуры\n"
        + extra
    )


@pytest.mark.parametrize(
    "body",
    [
        "Если ВычислитьУсловие() Тогда\n"
        'ПравилоКонвертации.ВариантИдентификации = "ПоПолямПоиска"; КонецЕсли;',
        "Если ВычислитьУсловие() Тогда\n"
        "ПравилоКонвертации.ПравилоДляГруппыСправочника = Истина; КонецЕсли;",
        'Если ВычислитьУсловие() Тогда ПравилоКонвертации.ПоляПоиска.Добавить("Code"); КонецЕсли;',
        'ДобавитьПКС(СвойстваШапки, "A", "A"); Возврат; ДобавитьПКС(СвойстваШапки, "B", "B");',
        'Если НаправлениеОбмена = "Отправка" И Ложь Тогда\n'
        'ДобавитьПКС(СвойстваШапки, "A", "A"); КонецЕсли;',
        '#Область Шапка\nДобавитьПКС(СвойстваШапки, "A", "A");\n#КонецОбласти',
        '#Удаление\nДобавитьПКС(СвойстваШапки, "A", "A");\n#КонецУдаления',
        'ДругойМодуль.ИнициализироватьРасширениеПравилаКонвертацииОбъекта(ЧтоУгодно, "Ext");',
        "Если ВычислитьУсловие() Тогда\n"
        "ОбменДаннымиXDTOСервер.ИнициализироватьРасширениеПравилаКонвертацииОбъекта("
        'ПравилоКонвертации, "Ext"); КонецЕсли;',
    ],
)
def test_review_non_exact_rule_forms_are_locked(body):
    model, _ = imported(probe_module(body))
    rule = model.pko[0]
    assert rule.state == "retained"
    assert any(
        b.logical_id == rule.inside_leaf_id and b.locks_context and body in b.text
        for b in model.retained_blocks
    )
    op = ManagerOperation(
        "ident",
        "identification",
        "update",
        target_id=rule.identification.logical_id,
        patch=IdentificationPatch(mode=Value("string", "ПоУникальномуИдентификатору")),
    )
    plan = preview(model, (op,), expected_revision=model.revision)
    assert plan.failures[0].reason == "opaque_context_changed" and plan.model is model


def test_review_assigned_property_and_conditional_pod_are_retained():
    model, _ = imported(probe_module('Строка = ДобавитьПКС(СвойстваШапки, "A", "A");'))
    assert model.pko[0].properties[0].state == "retained"
    assert any("Строка = ДобавитьПКС" in b.text for b in model.retained_blocks)
    text = SYNTHETIC.replace(
        "ПравилоОбработки.ОчисткаДанных = Ложь;",
        "Если ВычислитьУсловие() Тогда\n"
        'ПравилоОбработки.ИспользуемыеПКО.Добавить("Item"); КонецЕсли;',
    )
    model, _ = imported(text)
    assert model.pod[0].state == "retained"
    assert any(
        b.logical_id == model.pod[0].inside_leaf_id and b.locks_context
        for b in model.retained_blocks
    )


@pytest.mark.parametrize(
    "uses",
    [
        'Если ВерсияФормата() = "1.8" Тогда ДобавитьПКО_Send(ПравилаКонвертации); КонецЕсли;',
        'Если НаправлениеОбмена = "Отправка" И ВерсияФормата() = "1.8" Тогда\n'
        "ДобавитьПКО_Send(ПравилаКонвертации); КонецЕсли;",
        'Если НаправлениеОбмена = "Отправка" Тогда\n'
        "ДобавитьПКО_Send(ПравилаКонвертации, Лишний); КонецЕсли;",
    ],
)
def test_review_opaque_uses_and_retained_calls_block_rule_changes(uses):
    model, _ = imported(probe_module("", uses))
    cases: tuple[tuple[Action, PkoPatch | None], ...] = (
        ("update", PkoPatch(name="Other")),
        ("update", PkoPatch(directions=("receive",))),
        ("delete", None),
    )
    for action, patch in cases:
        op = ManagerOperation(
            action + str(patch), "pko", action, target_id=model.pko[0].logical_id, patch=patch
        )
        plan = preview(model, (op,), expected_revision=model.revision)
        assert plan.failures and plan.model is model
        assert plan.failures[0].reason in ("opaque_context_changed", "dangling_reference")
    assert any(b.dependencies for b in model.retained_blocks)


def test_review_profile_selection_is_semantic_and_report_is_not_revision():
    model, report = imported()
    left = replace(
        model, executor_profile=ExecutorProfile(profile_id="p", receive_mode="ordinary")
    ).with_revision()
    right = replace(
        left, executor_profile=replace(left.executor_profile, receive_mode="object")
    ).with_revision()
    assert canonicalize(left) != canonicalize(right)
    assert compare_models(left, right).changes[0].address == "Конвертация/executor_profile"
    assert (
        replace(left, import_report=replace(report, diagnostics=(("new", "address"),)))
        .with_revision()
        .revision
        == left.revision
    )


def test_review_helper_signature_and_presence_are_enforced():
    helper = (
        HELPER_PKS.format(parameter="", check="")
        .replace(', ПространствоИмен = ""', "")
        .replace("НоваяСтрока.ПространствоИмен = ПространствоИмен;", "")
    )
    model, report = imported(probe_module('ДобавитьПКС(СвойстваШапки, "A", "A");', extra=helper))
    prop = model.pko[0].properties[0]
    assert prop.state == "editable" and model.header.helper_variant == "legacy-v2"
    assert not report.diagnostics
    op = ManagerOperation(
        "ns",
        "property",
        "update",
        target_id=prop.logical_id,
        patch=PropertyPatch(namespace="urn:x"),
    )
    assert preview(model, (op,), expected_revision=model.revision).failures
    model, _ = imported(probe_module('ДобавитьПКС(СвойстваШапки, "A", "A", , , "urn:x");'))
    prop = model.pko[0].properties[0]
    cut = ManagerOperation(
        "cut",
        "property",
        "update",
        target_id=prop.logical_id,
        patch=PropertyPatch(argument_presence=(True, True, True)),
    )
    assert preview(model, (cut,), expected_revision=model.revision).failures


def test_review_missing_reference_address_retry_and_event_text():
    model, _ = imported()
    pod = model.pod[0]
    op = ManagerOperation(
        "bogus",
        "pod",
        "update",
        target_id=pod.logical_id,
        patch=PodPatch(used_pko=(Reference("pko", None, "Missing", "resolved"),)),
    )
    assert (
        preview(model, (op,), expected_revision=model.revision).failures[0].reason
        == "dangling_reference"
    )
    prop = model.pko[0].properties[0]
    op = ManagerOperation(
        "retry",
        "property",
        "update",
        target_id=prop.logical_id,
        patch=PropertyPatch(format_property="New"),
    )
    updated = execute(model, op)
    by_address = replace(op, target_id=None, address=model_addresses(model)[prop.logical_id])
    assert preview(updated, (by_address,), expected_revision=updated.revision).skipped == ("retry",)
    text = SYNTHETIC.replace(
        "    СвойстваШапки = ПравилоКонвертации.Свойства;",
        '    ПравилоКонвертации.ПриОтправкеДанных = "Handler";\n'
        "    СвойстваШапки = ПравилоКонвертации.Свойства;",
        1,
    )
    model, _ = imported(text)
    event = model.pko[0].events[0]
    assert event.state == "editable" and event.target.resolution == "missing"
    from kd_rules_mcp.ed.writer import render

    assert 'ПриОтправкеДанных = "Handler"' in render(model).text


@pytest.mark.parametrize(
    "project_id", ["trail.", "trail ", "CON", "con.txt", "nul", "COM1", "LPT9"]
)
def test_review_windows_project_identifiers_are_rejected(tmp_path, project_id):
    with pytest.raises(ValueError):
        ManagerWorkspace(tmp_path).create(ManagerModel(project_id).with_revision())


def test_review_workspace_isolates_damage_case_and_dead_locks(tmp_path):
    import json
    import os
    import time

    workspace = ManagerWorkspace(tmp_path)
    workspace.create(ManagerModel("Good").with_revision())
    workspace.create(ManagerModel("Broken").with_revision())
    with pytest.raises(ValueError):
        workspace.create(ManagerModel("good").with_revision())
    path = tmp_path / ".ed-projects/Broken/manager.ed.json"
    path.write_bytes(b"broken")
    loaded = ManagerWorkspace(tmp_path)
    assert loaded.get("Good").id == "Good"
    with pytest.raises(ValueError, match="Повреждённый"):
        loaded.get("Broken")
    lock = tmp_path / ".ed-projects/Good/.lock"
    lock.write_text(json.dumps({"pid": 99999999, "created": time.time()}), encoding="utf-8")
    model = loaded.get("Good").model
    op = ManagerOperation(
        "title", "manager", "update", patch=ManagerPatch(title=Value("string", "Changed"))
    )
    plan = loaded.preview("Good", (op,), expected_revision=model.revision)
    loaded.apply(
        "Good", (op,), expected_revision=model.revision, expected_preview_hash=plan.preview_hash
    )
    assert not lock.exists()
    lock.write_text(json.dumps({"pid": os.getpid(), "created": time.time()}), encoding="utf-8")
    with pytest.raises(EdAuthoringStaleError):
        loaded.close("Good")


def test_review_cache_eviction_does_not_rehash_bodies_or_mask_revision(monkeypatch):
    import kd_rules_mcp.ed.writer_model as dto

    model, _ = imported()
    dump_model(model)
    dto._HASHES.clear()
    original = dto.digest

    def checked(value):
        assert value != model.source_files[0].text
        assert value != model.code_units[0].body
        return original(value)

    monkeypatch.setattr(dto, "digest", checked)
    op = ManagerOperation(
        "title", "manager", "update", patch=ManagerPatch(title=Value("string", "New"))
    )
    updated = execute(model, op)
    assert updated.revision != model.revision
    with pytest.raises(EdAuthoringStaleError):
        preview(
            replace(model, header=replace(model.header, manager_name="Changed")),
            (),
            expected_revision=model.revision,
        )


def test_review_source_alias_cannot_restore_deleted_rule():
    model, _ = imported(probe_module(""))
    key = model.pko[0].logical_id
    changed = execute(model, ManagerOperation("delete", "pko", "delete", target_id=key))
    assert key not in model_addresses(changed)
    validate_model(changed)


def test_review_unverified_helper_creation_is_refused_and_unused_bodies_cleaned(tmp_path):
    from kd_rules_mcp.ed.writer_model import CodeUnit, Signature

    helper = HELPER_PKS.format(parameter="", check="").replace("НоваяСтрока =", "ДругаяСтрока =")
    model, _ = imported(probe_module("", extra=helper))
    op = ManagerOperation(
        "new",
        "property",
        "create",
        owner_id=model.pko[0].logical_id,
        patch=PropertyPatch(configuration_property="A", format_property="A"),
    )
    assert preview(model, (op,), expected_revision=model.revision).failures
    unit = CodeUnit(
        logical_id="unit",
        name="Body",
        signature=Signature(),
        body="// тело\r\n",
        sha256=text_hash("// тело\r\n"),
        origin="authored",
    )
    model = ManagerModel("bodies", code_units=(unit,)).with_revision()
    workspace = ManagerWorkspace(tmp_path)
    workspace.create(model)
    assert ManagerWorkspace(tmp_path).get("bodies").model.code_units[0].body == unit.body
    unused = tmp_path / ".ed-projects/bodies/bodies" / ("0" * 64 + ".bsl")
    unused.write_bytes(b"unused")
    plan = workspace.preview(
        "bodies",
        (
            ManagerOperation(
                "title", "manager", "update", patch=ManagerPatch(title=Value("string", "Title"))
            ),
        ),
        expected_revision=model.revision,
    )
    workspace.apply(
        "bodies",
        tuple(item.operation for item in plan.operations),
        expected_revision=model.revision,
        expected_preview_hash=plan.preview_hash,
    )
    assert not unused.exists()


def test_review_corrupt_compressed_project_does_not_stop_other_projects(tmp_path):
    import json

    workspace = ManagerWorkspace(tmp_path)
    for name in ("A", "B"):
        workspace.create(ManagerModel(name).with_revision())
    path = tmp_path / ".ed-projects/A/manager.ed.json"
    value = json.loads(path.read_bytes())
    value["model"]["data"] = "AAAA"
    path.write_text(json.dumps(value), encoding="utf-8")
    restored = ManagerWorkspace(tmp_path)
    assert restored.get("B").id == "B"
    with pytest.raises(ValueError, match="Повреждённый"):
        restored.get("A")


def test_review_create_property_uses_generator_optional_arguments():
    model = execute(
        ManagerModel("presence").with_revision(),
        ManagerOperation("rule", "pko", "create", patch=PkoPatch(name="A", directions=("send",))),
    )
    patch = PropertyPatch(
        configuration_property="A",
        format_property="A",
        argument_presence=(True, True, True, True, False),
    )
    op = ManagerOperation(
        "property", "property", "create", owner_id=model.pko[0].logical_id, patch=patch
    )
    updated = execute(model, op)
    prop = updated.pko[0].properties[0]
    assert prop.argument_presence == (True, True, True)
    assert len(prop.argument_values) == 2
    bad = replace(op, client_id="bad", patch=replace(patch, namespace="urn:x"))
    assert (
        preview(model, (bad,), expected_revision=model.revision).failures[0].reason
        == "model_invalid"
    )


LAYOUT_BODY = """ПравилоКонвертации.ВариантИдентификации = "ПоПолямПоиска";
ПравилоКонвертации.ПоляПоиска.Добавить("Code");
// C0 перед A
ДобавитьПКС(СвойстваШапки, "A", "A"); // хвост A
// C1 между A и R

ДобавитьПКС(СвойстваШапки, "R", "R", 0, "Other");
// C2 между R и B
ДобавитьПКС(СвойстваШапки, "B", "B");
Если НаправлениеОбмена = "Получение" Тогда
    ДобавитьПКС(СвойстваШапки, "G1", "G1");
    // GC между G1 и G2
    ДобавитьПКС(СвойстваШапки, "G2", "G2");
КонецЕсли;
// C3 конец тела"""


def layout_input():
    uses = """// E0 перед группой
Если НаправлениеОбмена = "Отправка" Тогда
    ДобавитьПКО_Send(ПравилаКонвертации); // хвост вызова
ИначеЕсли НаправлениеОбмена = "Получение" Тогда
    // E1 перед вызовом
    ДобавитьПКО_Send(ПравилаКонвертации);
КонецЕсли;"""
    return probe_module(LAYOUT_BODY, uses)


def layout_labels(model, container_id):
    members = {m.logical_id: m for m in model.members()}
    blocks = {b.logical_id: b for b in model.retained_blocks}
    containers = {c.logical_id: c for c in model.layouts}
    result = []
    for element in containers[container_id].elements:
        if element.container_id:
            result.append("group:" + str(containers[element.container_id].direction))
        elif element.field:
            result.append("field:" + element.field)
        elif element.entity_id:
            member = members[element.entity_id]
            result.append(
                "search:" + ",".join(member.fields) if hasattr(member, "fields") else member.name
            )
        else:
            text = blocks[element.block_id].text.strip()
            result.append(text if not text or text.startswith("//") else "scaffold")
    return result


BASE_LAYOUT = [
    "field:name",
    "field:format_object",
    "field:properties_start",
    "field:mode",
    "search:Code",
    "// C0 перед A",
    "A",
    "// C1 между A и R",
    "",
    "R",
    "// C2 между R и B",
    "B",
    "group:receive",
    "// C3 конец тела",
]


@pytest.mark.parametrize("crlf,bom", [(False, False), (True, False), (True, True)])
def test_layout_partition_and_views_are_exact(crlf, bom):
    text = "// начало\n\n" + layout_input() + "\n// конец\n"
    if crlf:
        text = text.replace("\n", "\r\n")
    if bom:
        text = "\ufeff" + text
    model, report = imported(text)
    assert all(covered == total and exact for _, covered, total, exact in report.source_partition)
    assert partition_report(load_model(dump_model(model))) == report.source_partition
    rule = model.pko[0]
    assert layout_labels(model, rule.logical_id) == BASE_LAYOUT
    assert rule.properties[0].trailing_comment == "// хвост A"
    group = next(
        c for c in model.layouts if c.owner_id == rule.logical_id and c.kind == "conditional"
    )
    assert layout_labels(model, group.logical_id) == ["G1", "// GC между G1 и G2", "G2"]
    other, _ = imported()
    parameter = other.parameters[0]
    assert parameter.inside_leaf_id is None and parameter.state == "editable"
    assert parameter.logical_id in {e.entity_id for c in other.layouts for e in c.elements}
    assert other.pko[0].groups[0].properties[0].inside_leaf_id is not None
    assert all(
        not hasattr(b, "before_id") and not hasattr(b, "after_id") for b in model.retained_blocks
    )
    assert all(not hasattr(m, "ordinal") for m in model.members())
    assert not hasattr(rule, "declaration_order")


def test_layout_operations_have_explicit_sequences_and_preserve_text():
    model, _ = imported(layout_input())
    rule = model.pko[0]
    props = {p.name: p for p in rule.properties}
    group = next(
        c for c in model.layouts if c.owner_id == rule.logical_id and c.kind == "conditional"
    )
    text_positions = {
        e.logical_id: c.logical_id for c in model.layouts for e in c.elements if e.kind == "text"
    }

    def check(changed, expected, group_expected=None):
        assert layout_labels(changed, rule.logical_id) == expected
        assert layout_labels(changed, group.logical_id) == (
            group_expected or ["G1", "// GC между G1 и G2", "G2"]
        )
        assert {
            e.logical_id: c.logical_id
            for c in changed.layouts
            for e in c.elements
            if e.kind == "text"
        } == text_positions
        assert changed.retained_blocks == model.retained_blocks
        validate_model(changed)

    changed = execute(
        model,
        ManagerOperation(
            "update",
            "property",
            "update",
            target_id=props["A"].logical_id,
            patch=PropertyPatch(configuration_property="NewA"),
        ),
    )
    check(changed, BASE_LAYOUT)
    for client, container, neighbor, expected, group_expected in (
        ("start", rule.logical_id, None, [*BASE_LAYOUT[:3], "X", *BASE_LAYOUT[3:]], None),
        (
            "afterA",
            rule.logical_id,
            props["A"].logical_id,
            [*BASE_LAYOUT[:7], "X", *BASE_LAYOUT[7:]],
            None,
        ),
        (
            "afterR",
            rule.logical_id,
            props["R"].logical_id,
            [*BASE_LAYOUT[:10], "X", *BASE_LAYOUT[10:]],
            None,
        ),
        (
            "inside",
            group.logical_id,
            props["G1"].logical_id,
            BASE_LAYOUT,
            ["G1", "X", "// GC между G1 и G2", "G2"],
        ),
    ):
        changed = execute(
            model,
            ManagerOperation(
                client,
                "property",
                "create",
                owner_id=rule.logical_id,
                container_id=container,
                after_id=neighbor,
                patch=PropertyPatch(configuration_property="X", format_property="X"),
            ),
        )
        check(changed, expected, group_expected)
    changed = execute(
        model,
        ManagerOperation(
            "move",
            "property",
            "move",
            target_id=props["A"].logical_id,
            container_id=rule.logical_id,
            after_id=props["B"].logical_id,
        ),
    )
    expected = [s for s in BASE_LAYOUT if s != "A"]
    expected.insert(expected.index("B") + 1, "A")
    check(changed, expected)
    assert changed.pko[0].properties[0].trailing_comment == "// хвост A"
    changed = execute(
        model,
        ManagerOperation(
            "into",
            "property",
            "move",
            target_id=props["B"].logical_id,
            container_id=group.logical_id,
            after_id=props["G1"].logical_id,
        ),
    )
    check(changed, [s for s in BASE_LAYOUT if s != "B"], ["G1", "B", "// GC между G1 и G2", "G2"])
    changed = execute(
        model,
        ManagerOperation(
            "out",
            "property",
            "move",
            target_id=props["G1"].logical_id,
            container_id=rule.logical_id,
            after_id=props["R"].logical_id,
        ),
    )
    check(changed, [*BASE_LAYOUT[:10], "G1", *BASE_LAYOUT[10:]], ["// GC между G1 и G2", "G2"])
    changed = execute(
        model,
        ManagerOperation(
            "ident",
            "identification",
            "update",
            target_id=rule.identification.logical_id,
            patch=IdentificationPatch(search_sets=(("Name",), ("Code", "Name"))),
        ),
    )
    check(changed, [*BASE_LAYOUT[:4], "search:Name", "search:Code,Name", *BASE_LAYOUT[5:]])
    deleted = execute(
        model, ManagerOperation("deleteA", "property", "delete", target_id=props["A"].logical_id)
    )
    expected = [s for s in BASE_LAYOUT if s != "A"]
    check(deleted, expected)
    original_container = next(c for c in model.layouts if c.logical_id == rule.logical_id)
    predecessor = original_container.elements[5].logical_id
    inserted = execute(
        deleted,
        ManagerOperation(
            "insertB",
            "property",
            "create",
            owner_id=rule.logical_id,
            container_id=rule.logical_id,
            after_id=predecessor,
            patch=PropertyPatch(configuration_property="X", format_property="X"),
        ),
    )
    removed = execute(
        inserted,
        ManagerOperation(
            "deleteB", "property", "delete", target_id=logical_id(model.project_id, "insertB")
        ),
    )
    check(removed, expected)
    assert canonicalize(removed) == canonicalize(deleted)
    assert compare_models(deleted, removed).equal
    repeat = ManagerOperation("deleteA", "property", "delete", target_id=props["A"].logical_id)
    assert preview(deleted, (repeat,), expected_revision=deleted.revision).skipped == ("deleteA",)


def test_layout_refusals_and_computed_dependency_scope():
    from kd_rules_mcp.ed.writer_model import CodeUnit, Signature

    model, _ = imported(layout_input().replace('0, "Other");', '0, "Other", "urn:w4");'))
    rule = model.pko[0]
    op = ManagerOperation(
        "directions",
        "pko",
        "update",
        target_id=rule.logical_id,
        patch=PkoPatch(directions=("send",)),
    )
    plan = preview(model, (op,), expected_revision=model.revision)
    assert plan.failures[0].reason == "unreachable_group"
    assert "Условие/" in plan.failures[0].address
    missing = ManagerOperation(
        "missing-container",
        "property",
        "create",
        owner_id=rule.logical_id,
        patch=PropertyPatch(configuration_property="N", format_property="N"),
    )
    assert not preview(model, (missing,), expected_revision=model.revision).failures
    retained_leaf = next(
        e.block_id
        for c in model.layouts
        for e in c.elements
        if e.entity_id == rule.properties[1].logical_id
    )
    assert (
        preview(
            model, (replace(missing, container_id=retained_leaf),), expected_revision=model.revision
        )
        .failures[0]
        .reason
        == "opaque_context_changed"
    )
    unit = CodeUnit(
        logical_id="computed",
        name="Computed",
        signature=Signature(),
        body="",
        sha256=text_hash(""),
        dependencies=(Reference("pko_lookup", resolution="computed"),),
    )
    model = replace(model, code_units=(*model.code_units, unit)).with_revision()
    delete = ManagerOperation(
        "delprop", "property", "delete", target_id=rule.properties[0].logical_id
    )
    assert not preview(model, (delete,), expected_revision=model.revision).failures
    rename = ManagerOperation(
        "rename", "pko", "update", target_id=rule.logical_id, patch=PkoPatch(name="New")
    )
    plan = preview(model, (rename,), expected_revision=model.revision)
    assert not plan.failures and plan.notices[0].references == ("Код/Computed",)


def test_layout_positional_addresses_are_refused_and_id_delete_replays():
    model, _ = imported(
        probe_module('ДобавитьПКС(СвойстваШапки, "A", "X");\nДобавитьПКС(СвойстваШапки, "B", "X");')
    )
    first, second = model.pko[0].properties
    address = model_addresses(model)[second.logical_id]
    assert "~2" in address
    op = ManagerOperation("address", "property", "delete", address=address)
    assert (
        preview(model, (op,), expected_revision=model.revision).failures[0].reason
        == "unstable_address"
    )
    delete = ManagerOperation("id", "property", "delete", target_id=first.logical_id)
    changed = execute(model, delete)
    assert preview(changed, (delete,), expected_revision=changed.revision).skipped == ("id",)
    assert (
        apply(changed, (delete,), expected_revision=model.revision, expected_preview_hash="old")
        is changed
    )


@pytest.mark.parametrize("damage", ["array", "index", "missing_source", "stop_iteration"])
def test_layout_workspace_isolates_all_load_exceptions(tmp_path, monkeypatch, damage):
    import json

    import kd_rules_mcp.authoring.ed.workspace as workspace_module
    from kd_rules_mcp.ed.writer_model import json_bytes, pack_json, unpack_json

    workspace = ManagerWorkspace(tmp_path)
    for project in ("good", "bad"):
        model, _ = import_manager(read_manager_text(layout_input()), project_id=project)
        workspace.create(model)
    path = tmp_path / ".ed-projects/bad/manager.ed.json"
    envelope = json.loads(path.read_bytes())
    value = unpack_json(envelope["model"])
    if damage == "array":
        path.write_bytes(b"[]")
    elif damage in ("index", "missing_source"):
        if damage == "index":
            value["source_map"][0][3] = 999
        else:
            value["retained_blocks"][0]["file_id"] = "absent"
        envelope["model"] = pack_json(value)
        path.write_bytes(json_bytes(envelope))
    else:
        original = workspace_module.load_model

        def raising(data, **kwargs):
            if unpack_json(json.loads(data)["model"])["project_id"] == "bad":
                raise StopIteration("испорчен проект")
            return original(data, **kwargs)

        monkeypatch.setattr(workspace_module, "load_model", raising)
    restored = ManagerWorkspace(tmp_path)
    assert restored.ids() == ("good",)
    with pytest.raises(ValueError, match="Повреждённый"):
        restored.get("bad")


def test_layout_bom_only_and_header_slots():
    from kd_rules_mcp.ed.errors import EdFormatError

    with pytest.raises(EdFormatError):
        imported("\ufeff")
    text = (
        layout_input()
        + """
Процедура ЗаполнитьПравилаОбработкиДанных(НаправлениеОбмена, ПравилаОбработкиДанных) Экспорт
    // пустой заполнитель

КонецПроцедуры
"""
    )
    model, report = imported("\ufeff" + text.replace("\n", "\r\n"))
    assert all(exact for *_, exact in report.source_partition)
    empty = next(c for c in model.layouts if c.name == "ЗаполнитьПравилаОбработкиДанных")
    assert layout_labels(model, empty.logical_id) == ["// пустой заполнитель", ""]
    model, _ = imported()
    fields = {e.field for c in model.layouts for e in c.elements}
    assert {"header.title", "header.interface_version"} <= fields
    model, _ = imported(
        SYNTHETIC.replace('    Возврат "2";', '    Сообщить("extra");\n    Возврат "2";')
    )
    op = ManagerOperation("version", "manager", "update", patch=ManagerPatch(interface_version=3))
    assert (
        preview(model, (op,), expected_revision=model.revision).failures[0].reason
        == "opaque_context_changed"
    )


def test_third_review_single_rule_regions_and_retained_event_refusals():
    from kd_rules_mcp.ed.writer import render

    text = SYNTHETIC.replace(
        "Процедура ДобавитьПКО_Send", "#область Single\nПроцедура ДобавитьПКО_Send"
    ).replace("Процедура ДобавитьПКО_Receive", "#конецобласти\nПроцедура ДобавитьПКО_Receive")
    model, _ = imported(text)
    changed = execute(
        model,
        ManagerOperation(
            "after-region",
            "pko",
            "create",
            container_id=model.root_layouts[0],
            after_id=model.pko[0].logical_id,
            patch=PkoPatch(name="Inserted", directions=("send",)),
        ),
    )
    output = render(changed)
    assert output.text.index("#конецобласти") < output.text.index("Процедура ДобавитьПКО_Inserted")
    back, _ = imported(output.data.decode("utf-8"))
    assert canonicalize(back) == canonicalize(changed)
    event_text = layout_input().replace(
        "    СвойстваШапки = ПравилоКонвертации.Свойства;",
        '    ПравилоКонвертации.ПриОтправкеДанных = "H";\n'
        "    СвойстваШапки = ПравилоКонвертации.Свойства;",
    )
    model, report = imported(event_text)
    rule = model.pko[0]
    assert rule.events and all(e.reason for e in report.entries if e.state == "retained")
    op = ManagerOperation("delete-event-owner", "pko", "delete", target_id=rule.logical_id)
    plan = preview(model, (op,), expected_revision=model.revision)
    assert not plan.failures
    assert all(r.logical_id != rule.logical_id for r in plan.model.pko)
    assert all(e.logical_id != rule.events[0].logical_id for r in plan.model.pko for e in r.events)
    event_leaf = next(
        e for c in model.layouts for e in c.elements if e.entity_id == rule.events[0].logical_id
    )
    op = ManagerOperation(
        "before-alias",
        "property",
        "create",
        owner_id=rule.logical_id,
        container_id=rule.logical_id,
        after_id=event_leaf.logical_id,
        patch=PropertyPatch(configuration_property="Bad", format_property="Bad"),
    )
    assert (
        preview(model, (op,), expected_revision=model.revision).failures[0].reason
        == "model_invalid"
    )


def test_layout_module_and_calls_order_and_unique_names():
    model = ManagerModel("order").with_revision()
    module = model.root_layouts[0]
    for name, neighbor in (
        ("A", None),
        ("B", logical_id("order", "A")),
        ("C", logical_id("order", "B")),
    ):
        model = execute(
            model,
            ManagerOperation(
                name,
                "pko",
                "create",
                container_id=module,
                after_id=neighbor,
                patch=PkoPatch(name=name, directions=("send",)),
            ),
        )
    a, b, c = (logical_id("order", name) for name in ("A", "B", "C"))
    call_group = next(g for g in model.layouts if g.kind == "conditional")
    assert model.ordered_entity_ids(module)[:3] == (a, b, c)
    assert layout_labels(model, call_group.logical_id) == [
        "ДобавитьПКО_A",
        "ДобавитьПКО_B",
        "ДобавитьПКО_C",
    ]
    moved = execute(
        model, ManagerOperation("move", "pko", "move", target_id=c, container_id=module, after_id=a)
    )
    assert moved.ordered_entity_ids(module)[:3] == (a, c, b)
    assert layout_labels(moved, call_group.logical_id) == [
        "ДобавитьПКО_A",
        "ДобавитьПКО_C",
        "ДобавитьПКО_B",
    ]
    op = ManagerOperation(
        "same",
        "pko",
        "create",
        container_id=module,
        after_id=a,
        patch=PkoPatch(name="A", directions=("receive",)),
    )
    failure = preview(model, (op,), expected_revision=model.revision).failures
    assert any("Имя процедуры" in f.message for f in failure)
    assert preview(
        model,
        (replace(op, patch=PkoPatch(name="New", directions=("both", "send"))),),
        expected_revision=model.revision,
    ).failures
    both = execute(
        model, replace(op, client_id="both", patch=PkoPatch(name="Both", directions=("both",)))
    )
    use = next(u for u in both.rule_uses if u.direction == "both")
    parent = next(g for g in both.layouts if any(e.entity_id == use.logical_id for e in g.elements))
    assert parent.kind == "entrypoint" and not use.guards
    order_changes = [
        change for change in compare_models(model, moved).changes if change.action == "order"
    ]
    assert len(order_changes) == 2 and all(change.fields == ("order",) for change in order_changes)


def test_layout_corrupt_leaf_and_read_only_view_are_rejected():
    model, _ = imported()
    container = next(c for c in model.layouts if c.logical_id == model.pko[0].logical_id)
    first, second = [
        e
        for e in container.elements
        if e.entity_id in {p.logical_id for p in model.pko[0].properties}
    ][:2]
    bad = replace(
        model,
        layouts=tuple(
            replace(
                c,
                elements=tuple(
                    replace(e, source=first.source) if e is second else e for e in c.elements
                ),
            )
            if c is container
            else c
            for c in model.layouts
        ),
    )
    with pytest.raises(ValueError, match="перекрываются"):
        validate_model(bad)
    parameter = model.pko[0].groups[0].properties[0]
    root = next(c for c in model.layouts if c.logical_id == model.root_layouts[0])
    bad = replace(
        model,
        layouts=tuple(
            replace(
                c,
                elements=(
                    *c.elements,
                    LayoutElement("view", "entity", entity_id=parameter.logical_id),
                ),
            )
            if c is root
            else c
            for c in model.layouts
        ),
    )
    with pytest.raises(ValueError, match="Представление"):
        validate_model(bad)


def test_layout_address_replay_prefers_receipt_after_name_reuse():
    model, _ = imported(probe_module('ДобавитьПКС(СвойстваШапки, "A", "A");'))
    prop = model.pko[0].properties[0]
    delete = ManagerOperation(
        "delete", "property", "delete", address=model_addresses(model)[prop.logical_id]
    )
    deleted = execute(model, delete)
    recreated = execute(
        deleted,
        ManagerOperation(
            "recreate",
            "property",
            "create",
            owner_id=model.pko[0].logical_id,
            container_id=model.pko[0].logical_id,
            patch=PropertyPatch(configuration_property="A", format_property="A"),
        ),
    )
    assert preview(recreated, (delete,), expected_revision=recreated.revision).skipped == (
        "delete",
    )


def test_layout_exact_pod_column_is_model_field():
    text = SYNTHETIC.replace(
        '    Если НаправлениеОбмена = "Получение" Тогда\n        ДобавитьПОД_Items',
        '    Если НаправлениеОбмена = "Получение" Тогда\n'
        '        Если ПравилаОбработкиДанных.Колонки.Найти("ОчисткаДанных") = Неопределено Тогда\n'
        '            ПравилаОбработкиДанных.Колонки.Добавить("ОчисткаДанных");\n'
        "        КонецЕсли;\n        ДобавитьПОД_Items",
    )
    model, report = imported(text)
    assert all(exact for *_, exact in report.source_partition)
    use = next(u for u in model.rule_uses if u.rule.target_id == model.pod[0].logical_id)
    assert use.state == "editable" and use.inside_leaf_id is None
    assert model.header.clear_data_column
    assert not any('Колонки.Найти("ОчисткаДанных")' in b.text for b in model.retained_blocks)
    group = next(g for g in model.layouts if any(e.entity_id == use.logical_id for e in g.elements))
    assert any(e.field == "header.clear_data_column" for e in group.elements)


def test_layout_chain_and_nested_conditions_keep_their_context():
    uses = """Если НаправлениеОбмена = "Отправка" Тогда
    ДобавитьПКО_Send(ПравилаКонвертации);
ИначеЕсли НаправлениеОбмена = "Получение" Тогда
    ДобавитьПКО_Send(ПравилаКонвертации);
КонецЕсли;"""
    body = """Если НаправлениеОбмена = "Получение" Тогда
    Если НаправлениеОбмена = "Получение" Тогда
        ДобавитьПКС(СвойстваШапки, "A", "A"); // хвост A
    КонецЕсли;
ИначеЕсли НаправлениеОбмена = "Отправка" Тогда
    ДобавитьПКС(СвойстваШапки, "B", "B");
КонецЕсли;
ДобавитьПКС(СвойстваШапки, "C", "C");"""
    model, report = imported(probe_module(body, uses))
    assert all(exact for *_, exact in report.source_partition)
    rule = model.pko[0]
    containers = {c.logical_id: c for c in model.layouts}
    chain = next(c for c in model.layouts if c.branch == "chain" and c.owner_id == rule.logical_id)
    branch_ids = [e.container_id for e in chain.elements]
    assert all(isinstance(key, str) for key in branch_ids)
    assert [containers[key].branch for key in branch_ids if key is not None] == ["if", "elseif"]
    inner = next(
        c
        for c in model.layouts
        if any(e.entity_id == rule.properties[0].logical_id for e in c.elements)
    )
    assert inner.owner_id is not None
    outer = containers[inner.owner_id]
    assert outer.owner_id == chain.logical_id
    op = ManagerOperation(
        "into-inner",
        "property",
        "move",
        target_id=rule.properties[2].logical_id,
        container_id=inner.logical_id,
        after_id=rule.properties[0].logical_id,
    )
    changed = execute(model, op)
    assert changed.pko[0].properties[2].guards == (outer.logical_id, inner.logical_id)
    assert layout_labels(changed, inner.logical_id) == ["A", "C"]
    assert preview(
        model,
        (replace(op, container_id=chain.logical_id, after_id=None),),
        expected_revision=model.revision,
    ).failures
    unreachable = ManagerOperation(
        "unreachable-nested",
        "pko",
        "update",
        target_id=rule.logical_id,
        patch=PkoPatch(directions=("send",)),
    )
    assert (
        preview(model, (unreachable,), expected_revision=model.revision).failures[0].reason
        == "unreachable_group"
    )


def test_layout_field_operators_own_comments_and_new_fields_have_slots():
    text = (
        probe_module('ДобавитьПКС(СвойстваШапки, "A", "A");')
        .replace('ИмяПКО = "Item";', 'ИмяПКО = "Item"; // имя')
        .replace('ОбъектФормата = "Item";', 'ОбъектФормата = "Item"; // формат')
    )
    model, _ = imported(text)
    rule = model.pko[0]
    container = next(c for c in model.layouts if c.logical_id == rule.logical_id)
    assert {e.field: e.trailing_comment for e in container.elements if e.field} == {
        "name": "// имя",
        "format_object": "// формат",
        "properties_start": "",
    }
    changed = execute(
        model,
        ManagerOperation(
            "new-config",
            "pko",
            "update",
            target_id=rule.logical_id,
            patch=PkoPatch(
                configuration_object=Value(
                    "reference", reference_parts=("Метаданные", "Справочники", "Items")
                )
            ),
        ),
    )
    slots = next(c for c in changed.layouts if c.logical_id == rule.logical_id).elements
    assert [(e.field, e.trailing_comment) for e in slots if e.field] == [
        ("name", "// имя"),
        ("format_object", "// формат"),
        ("configuration_object", ""),
        ("properties_start", ""),
    ]
    cleared = execute(
        changed,
        ManagerOperation(
            "clear-config",
            "pko",
            "update",
            target_id=rule.logical_id,
            clear=("configuration_object",),
        ),
    )
    assert layout_labels(cleared, rule.logical_id) == layout_labels(model, rule.logical_id)
    opaque, _ = imported(
        probe_module('ДобавитьПКС(СвойстваШапки, "R", "R", 0, "Other", "urn:w4");')
    )
    refusal = ManagerOperation(
        "clear-format",
        "pko",
        "update",
        target_id=opaque.pko[0].logical_id,
        clear=("format_object",),
    )
    assert (
        preview(opaque, (refusal,), expected_revision=opaque.revision).failures[0].reason
        == "opaque_context_changed"
    )
    authored = ManagerModel("fields").with_revision()
    authored = execute(
        authored,
        ManagerOperation(
            "rule",
            "pko",
            "create",
            container_id=authored.root_layouts[0],
            patch=PkoPatch(name="New", directions=("send",), format_object=Value("string", "Item")),
        ),
    )
    assert layout_labels(authored, authored.pko[0].logical_id) == [
        "field:name",
        "field:format_object",
        "field:properties_start",
    ]


def test_layout_unknown_formal_default_is_retained():
    text = probe_module('ДобавитьПКС(СвойстваШапки, "A", "A");').replace(
        "Процедура ДобавитьПКО_Send(ПравилаКонвертации)",
        "Процедура ДобавитьПКО_Send(ПравилаКонвертации = ПолучитьТаблицу())",
    )
    model, report = imported(text)
    assert model.pko[0].state == "retained"
    assert model.pko[0].inside_leaf_id
    assert all(exact for *_, exact in report.source_partition)


def test_layout_delete_rule_removes_only_its_own_conditional_containers():
    model, _ = imported(
        probe_module('Если НаправлениеОбмена = "Отправка" Тогда\n// тело\nКонецЕсли;')
    )
    rule = model.pko[0]
    group = next(c for c in model.layouts if c.owner_id == rule.logical_id)
    outside = {
        b.logical_id: b.owner_id
        for b in model.retained_blocks
        if b.owner_id not in (rule.logical_id, group.logical_id)
    }
    changed = execute(
        model, ManagerOperation("delete-rule", "pko", "delete", target_id=rule.logical_id)
    )
    assert group.logical_id not in {c.logical_id for c in changed.layouts}
    assert group.logical_id not in {g.logical_id for g in changed.guards}
    assert all(
        any(b.logical_id == key and b.owner_id == owner for b in changed.retained_blocks)
        for key, owner in outside.items()
    )
    validate_model(changed)


def test_layout_field_comment_is_visible_in_semantic_diff():
    text = probe_module('ДобавитьПКС(СвойстваШапки, "A", "A");').replace(
        'ИмяПКО = "Item";', 'ИмяПКО = "Item"; // комментарий поля'
    )
    before, _ = imported(text)
    after, _ = imported(text.replace("// комментарий поля", "// новый комментарий поля"))
    assert canonicalize(before) != canonicalize(after)
    diff = compare_models(before, after)
    assert not diff.equal
    assert len(diff.changes) == 1
    assert diff.changes[0].fields == ("trailing_comment",)
