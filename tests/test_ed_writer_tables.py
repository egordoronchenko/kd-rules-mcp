"""W3: группы, свойства строки, размещение и точный круг без исполнения BSL."""

from collections import Counter
from dataclasses import replace

import pytest

from kd_rules_mcp.authoring.ed.manager_operations import (
    ManagerOperation,
    ManagerOperationError,
    PkoPatch,
    PropertyPatch,
    TablePartPatch,
    apply,
    parse_operation,
    preview,
)
from kd_rules_mcp.ed.canonical import model_addresses
from kd_rules_mcp.ed.executor_profile import BSP_3_1_12_XDTO
from kd_rules_mcp.ed.forms import helper_forms
from kd_rules_mcp.ed.reader import read_manager_text
from kd_rules_mcp.ed.writer import new_manager, render
from kd_rules_mcp.ed.writer_import import import_manager
from kd_rules_mcp.ed.writer_model import Reference, TextStyle
from kd_rules_mcp.validation.ed_writer import _declarative_checks, validate_writer
from kd_rules_mcp.validation.report import ValidationReport
from tests.test_ed_writer_code import execute, round_trip


def table_model():
    return execute(
        new_manager(style=TextStyle(newline="\n", bom=False)),
        ManagerOperation(
            "rule", "pko", "create", patch=PkoPatch(name="Item", directions=("receive",))
        ),
    )


def table(model, client="table", configuration="Rows", format_name="Rows"):
    return execute(
        model,
        parse_operation(
            {
                "client_id": client,
                "kind": "table_part",
                "action": "create",
                "owner_id": model.pko[0].logical_id,
                "patch": {"configuration_property": configuration, "format_property": format_name},
            }
        ),
    )


@pytest.mark.parametrize("sides", [("Rows", "Rows"), ("Rows", ""), ("", "Rows")])
def test_table_part_and_property_round_trip(sides):
    model = table(table_model(), configuration=sides[0], format_name=sides[1])
    group = model.pko[0].groups[0]
    model = execute(
        model,
        parse_operation(
            {
                "client_id": "column",
                "kind": "property",
                "action": "create",
                "owner_id": group.logical_id,
                "patch": {"configuration_property": "Amount", "format_property": "Amount"},
            }
        ),
    )
    assert "\t\n\tСвойстваТЧ = ДобавитьПКТЧ(ПравилоКонвертации," in render(model).text
    assert 'ДобавитьПКС(СвойстваТЧ, "Amount", "Amount");\n\n' in render(model).text
    assert model.pko[0].groups[0].properties[0].state == "editable"
    round_trip(model)


def test_group_create_notice_requires_confirmation_and_names_group():
    model = table_model()
    operation = ManagerOperation(
        "table",
        "table_part",
        "create",
        owner_id=model.pko[0].logical_id,
        patch=TablePartPatch(configuration_property="Rows", format_property="Rows"),
    )
    plan = preview(model, (operation,), expected_revision=model.revision)
    assert not plan.failures
    assert [(n.code, n.requires_confirmation) for n in plan.notices] == [
        ("table_part_replace", True)
    ]
    assert plan.notices[0].address == "ПКО/Item/ПКТЧ/Rows"
    with pytest.raises(ManagerOperationError) as error:
        apply(
            model,
            (operation,),
            expected_revision=model.revision,
            expected_preview_hash=plan.preview_hash,
        )
    assert error.value.failures[0].reason == "confirmation_required"
    assert (
        apply(
            model,
            (operation,),
            expected_revision=model.revision,
            expected_preview_hash=plan.preview_hash,
            confirmations=tuple((n.code, n.notice_hash) for n in plan.notices),
        )
        == plan.model
    )


def test_two_group_create_notices_have_distinct_addresses_and_both_need_ack():
    model = table_model()
    operations = tuple(
        ManagerOperation(
            name,
            "table_part",
            "create",
            owner_id=model.pko[0].logical_id,
            patch=TablePartPatch(configuration_property=name, format_property=name),
        )
        for name in ("Rows", "Lines")
    )
    plan = preview(model, operations, expected_revision=model.revision)
    assert not plan.failures
    notices = [n for n in plan.notices if n.code == "table_part_replace"]
    assert [n.address for n in notices] == ["ПКО/Item/ПКТЧ/Rows", "ПКО/Item/ПКТЧ/Lines"]
    assert all(n.requires_confirmation for n in notices)
    with pytest.raises(ManagerOperationError):
        apply(
            model,
            operations,
            expected_revision=model.revision,
            expected_preview_hash=plan.preview_hash,
            confirmations=((notices[0].code, notices[0].notice_hash),),
        )
    assert (
        apply(
            model,
            operations,
            expected_revision=model.revision,
            expected_preview_hash=plan.preview_hash,
            confirmations=tuple((n.code, n.notice_hash) for n in notices),
        )
        == plan.model
    )


def test_table_create_delete_restores_source_and_other_rows_stay_exact():
    model = table_model()
    text = render(model).text
    model = import_manager(read_manager_text(text), project_id=model.project_id)[0]
    created = table(model)
    changed = execute(
        created,
        ManagerOperation(
            "rename",
            "table_part",
            "update",
            target_id=created.pko[0].groups[0].logical_id,
            patch=TablePartPatch(configuration_property="LongerRows"),
        ),
    )
    assert render(changed).text.replace("LongerRows", "Rows") == render(created).text
    deleted = execute(
        created,
        ManagerOperation(
            "delete", "table_part", "delete", target_id=created.pko[0].groups[0].logical_id
        ),
    )
    assert render(deleted).text == text
    round_trip(changed)


def test_properties_move_between_header_and_groups():
    model = table(
        table(table_model()), client="second", configuration="OtherRows", format_name="OtherRows"
    )
    model = execute(
        model,
        ManagerOperation(
            "column",
            "property",
            "create",
            owner_id=model.pko[0].logical_id,
            patch=PropertyPatch(configuration_property="Amount", format_property="Amount"),
        ),
    )
    key = model.pko[0].properties[0].logical_id
    for container in (
        model.pko[0].groups[0].logical_id,
        model.pko[0].groups[1].logical_id,
        model.pko[0].logical_id,
    ):
        model = execute(
            model,
            ManagerOperation(
                "move-" + container,
                "property",
                "move",
                target_id=key,
                container_id=container,
                after_id=None,
            ),
        )
        round_trip(model)
    assert model.pko[0].properties[0].logical_id == key


def test_preserve_does_not_realign_neighbouring_properties():
    model = table(table_model())
    group = model.pko[0].groups[0]
    for client, name in (("a", "A"), ("b", "B")):
        model = execute(
            model,
            ManagerOperation(
                client,
                "property",
                "create",
                owner_id=group.logical_id,
                patch=PropertyPatch(configuration_property=name, format_property=name),
            ),
        )
    text = render(model, "canonical").text
    imported = import_manager(read_manager_text(text), project_id=model.project_id)[0]
    prop = imported.pko[0].groups[0].properties[0]
    changed = execute(
        imported,
        ManagerOperation(
            "long",
            "property",
            "update",
            target_id=prop.logical_id,
            patch=PropertyPatch(configuration_property="LongestColumn"),
        ),
    )
    assert next(line for line in render(changed).text.splitlines() if '"B"' in line) == next(
        line for line in text.splitlines() if '"B"' in line
    )
    assert render(imported).text == text


def test_group_argument_extensions_refuse():
    model = table_model()
    plan = preview(
        model,
        (
            ManagerOperation(
                "bad",
                "table_part",
                "create",
                owner_id=model.pko[0].logical_id,
                patch=TablePartPatch(
                    configuration_property="Rows",
                    format_property="Rows",
                    argument_presence=(True, True, True, True),
                ),
            ),
        ),
        expected_revision=model.revision,
    )
    assert any(f.reason == "unsupported_form" for f in plan.failures)


def legacy_model():
    model = table(table_model())
    model = execute(
        model,
        ManagerOperation(
            "column",
            "property",
            "create",
            owner_id=model.pko[0].groups[0].logical_id,
            patch=PropertyPatch(configuration_property="Amount", format_property="Amount"),
        ),
    )
    text = render(model).text
    document = read_manager_text(text)
    for routine in document.routines:
        if routine.name not in ("ДобавитьПКС", "ДобавитьПКТЧ"):
            continue
        replacement = helper_forms(routine.name, 2)[0].replace(', ПространствоИмен = ""', "")
        replacement = "\n".join(
            line for line in replacement.split("\n") if ".ПространствоИмен =" not in line
        )
        text = text.replace(routine.raw_text, replacement)
    return import_manager(read_manager_text(text), project_id=model.project_id)[0]


def test_legacy_helpers_and_frames_are_editable_and_exact():
    model = legacy_model()
    assert model.header.helper_variant == "legacy-v2"
    assert not model.import_report.diagnostics
    helpers = [u for u in model.code_units if u.name in ("ДобавитьПКС", "ДобавитьПКТЧ")]
    assert len(helpers) == 2 and all(u.helper_verified for u in helpers)
    assert model.pko[0].groups[0].properties[0].state == "editable"
    assert not any(b.kind in ("pks", "pktch") for b in model.retained_blocks)
    before = {u.name: u.body for u in helpers}
    changed = execute(
        model,
        ManagerOperation(
            "rename-column",
            "property",
            "update",
            target_id=model.pko[0].groups[0].properties[0].logical_id,
            patch=PropertyPatch(configuration_property="Total"),
        ),
    )
    assert before == {u.name: u.body for u in changed.code_units if u.name in before}
    round_trip(changed)
    report = validate_writer(changed, render(changed).data, profile=BSP_3_1_12_XDTO)
    assert not any(
        i.check == "ed.writer.incomplete" and i.address == "Код/ДобавитьПКС" for i in report.issues
    )


def test_missing_table_helper_is_reported_and_create_refuses():
    model = table(table_model())
    text = render(model).text
    helper = next(r for r in read_manager_text(text).routines if r.name == "ДобавитьПКТЧ")
    model = import_manager(
        read_manager_text(text.replace(helper.raw_text, "")), project_id=model.project_id
    )[0]
    report = validate_writer(model, render(model).data, profile=BSP_3_1_12_XDTO)
    assert any(
        i.check == "ed.writer.incomplete" and i.address == "Код/ДобавитьПКТЧ" for i in report.issues
    )
    plan = preview(
        model,
        (
            ManagerOperation(
                "new-table",
                "table_part",
                "create",
                owner_id=model.pko[0].logical_id,
                patch=TablePartPatch(configuration_property="Other", format_property="Other"),
            ),
        ),
        expected_revision=model.revision,
    )
    assert any("не подтверждено" in f.message for f in plan.failures)


@pytest.mark.parametrize("change", ["mixed", "unverified", "duplicate"])
def test_unverified_or_mixed_helpers_keep_table_declarations(change):
    model = legacy_model()
    text = render(model).text
    document = read_manager_text(text)
    routine = next(r for r in document.routines if r.name == "ДобавитьПКТЧ")
    if change == "mixed":
        text = text.replace(routine.raw_text, helper_forms(routine.name, 2)[0])
    elif change == "unverified":
        text = text.replace("Возврат КонвертацияТабличнойЧасти.Свойства;", "Возврат Неопределено;")
    else:
        text += "\n" + routine.raw_text
    imported = import_manager(read_manager_text(text), project_id=model.project_id)[0]
    assert imported.pko[0].groups[0].state == "retained"
    assert render(imported).text == text
    round_trip(imported)


def test_remaining_header_declarations_are_fields():
    model = table_model()
    text = render(model).text.replace(
        "Процедура ЗаполнитьПравилаОбработкиДанных(",
        "Функция Подключаемый_ИдентификаторМодуля() Экспорт\n"
        '\tВозврат "synthetic-module-id";\nКонецФункции\n\n'
        "Процедура ЗаполнитьПравилаОбработкиДанных(",
    )
    text = text.replace(
        "НаправлениеОбмена, ПравилаОбработкиДанных) Экспорт\n",
        "НаправлениеОбмена, ПравилаОбработкиДанных) Экспорт\n"
        '\tЕсли ПравилаОбработкиДанных.Колонки.Найти("ОчисткаДанных") = Неопределено Тогда\n'
        '\t\tПравилаОбработкиДанных.Колонки.Добавить("ОчисткаДанных");\n\tКонецЕсли;\n',
    )
    model = import_manager(read_manager_text(text), project_id=model.project_id)[0]
    assert model.header.clear_data_column
    assert model.header.module_identifier.value == "synthetic-module-id"
    assert not any("synthetic-module-id" in b.text for b in model.retained_blocks)
    changed = replace(
        model,
        header=replace(
            model.header,
            module_identifier=replace(model.header.module_identifier, value="changed-module-id"),
        ),
    ).with_revision()
    assert 'Возврат "changed-module-id";' in render(changed).text
    round_trip(changed)


@pytest.mark.parametrize("direction", ["send", "receive"])
def test_duplicate_tables_refuse_send_and_require_confirmation_receive(direction):
    model = table_model()
    model = execute(
        model,
        ManagerOperation(
            "direction",
            "pko",
            "update",
            target_id=model.pko[0].logical_id,
            patch=PkoPatch(directions=(direction,)),
        ),
    )
    model = table(model)
    plan = preview(
        model,
        (
            ManagerOperation(
                "duplicate",
                "table_part",
                "create",
                owner_id=model.pko[0].logical_id,
                patch=TablePartPatch(configuration_property="Rows", format_property="Rows"),
            ),
        ),
        expected_revision=model.revision,
    )
    if direction == "send":
        assert any("затирают" in f.message for f in plan.failures)
    else:
        assert not plan.failures
        assert any(
            n.code == "table_part_duplicate" and n.requires_confirmation for n in plan.notices
        )


def test_table_checks_are_local_and_existing_duplicates_do_not_block_unrelated_edit():
    model = table(table_model(), configuration="", format_name="Rows")
    model = execute(
        model,
        ManagerOperation(
            "direct",
            "property",
            "create",
            owner_id=model.pko[0].groups[0].logical_id,
            patch=PropertyPatch(configuration_property="Value", format_property="Value"),
        ),
    )
    report = ValidationReport()
    _declarative_checks(model, report, model_addresses(model))
    assert Counter(i.check for i in report.issues)["ed.writer.table_part_direct_empty"] == 1
    model = table(table_model())
    report = ValidationReport()
    _declarative_checks(model, report, model_addresses(model))
    assert Counter(i.check for i in report.issues)["ed.writer.table_part_empty"] == 1


def test_table_references_recheck_target_direction_and_property_moves():
    model = table(table_model())
    model = execute(
        model,
        ManagerOperation(
            "target", "pko", "create", patch=PkoPatch(name="Target", directions=("both",))
        ),
    )
    group = model.pko[0].groups[0]
    target = model.pko[1]
    model = execute(
        model,
        ManagerOperation(
            "reference",
            "property",
            "create",
            owner_id=group.logical_id,
            patch=PropertyPatch(
                configuration_property="Ref",
                format_property="Ref",
                property_kind="reference",
                conversion=Reference("pko", target.logical_id, target.name, "resolved"),
            ),
        ),
    )
    plan = preview(
        model,
        (
            ManagerOperation(
                "direction",
                "pko",
                "update",
                target_id=target.logical_id,
                patch=PkoPatch(directions=("send",)),
            ),
        ),
        expected_revision=model.revision,
    )
    assert plan.failures and any("ПКТЧ" in place for f in plan.failures for place in f.references)
    plan = preview(
        model,
        (ManagerOperation("delete", "pko", "delete", target_id=target.logical_id),),
        expected_revision=model.revision,
    )
    assert any(f.reason == "dangling_reference" for f in plan.failures)
    round_trip(model)


def test_table_move_direction_rechecks_references_and_children_guards():
    model = table(table_model())
    model = execute(
        model,
        ManagerOperation(
            "target", "pko", "create", patch=PkoPatch(name="Target", directions=("send",))
        ),
    )
    text = render(model).text.replace(
        '\tСвойстваТЧ = ДобавитьПКТЧ(ПравилоКонвертации, "Rows", "Rows");',
        '\tЕсли НаправлениеОбмена = "Отправка" Тогда\n'
        '\t\tСвойстваТЧ = ДобавитьПКТЧ(ПравилоКонвертации, "Rows", "Rows");\n'
        '\t\tДобавитьПКС(СвойстваТЧ, "Ref", "Ref", , "Target");\n'
        "\tКонецЕсли;\n"
        '\tЕсли НаправлениеОбмена = "Получение" Тогда\n'
        '\t\tДобавитьПКС(СвойстваШапки, "Dummy", "Dummy");\n\tКонецЕсли;',
    )
    model = import_manager(read_manager_text(text), project_id=model.project_id)[0]
    group = model.pko[0].groups[0]
    receive = next(
        c
        for c in model.layouts
        if c.kind == "conditional"
        and c.owner_id == model.pko[0].logical_id
        and c.direction == "receive"
    )
    move = ManagerOperation(
        "move-group",
        "table_part",
        "move",
        target_id=group.logical_id,
        container_id=receive.logical_id,
        after_id=None,
    )
    plan = preview(model, (move,), expected_revision=model.revision)
    assert any(f.reason == "dangling_reference" for f in plan.failures)
    target = model.pko[1]
    model = execute(
        model,
        ManagerOperation(
            "both",
            "pko",
            "update",
            target_id=target.logical_id,
            patch=PkoPatch(directions=("both",)),
        ),
    )
    moved = execute(model, move)
    assert moved.pko[0].groups[0].guards == (receive.logical_id,)
    assert moved.pko[0].groups[0].properties[0].guards == (receive.logical_id,)
    round_trip(moved)


def test_move_with_client_container_and_no_anchor_appends():
    model = table_model()
    packet = tuple(
        parse_operation(o)
        for o in (
            {
                "client_id": "group",
                "kind": "table_part",
                "action": "create",
                "owner_id": model.pko[0].logical_id,
                "patch": {"configuration_property": "Rows", "format_property": "Rows"},
            },
            {
                "client_id": "A",
                "kind": "property",
                "action": "create",
                "owner_id": {"client_id": "group"},
                "patch": {"configuration_property": "A", "format_property": "A"},
            },
            {
                "client_id": "B",
                "kind": "property",
                "action": "create",
                "owner_id": {"client_id": "group"},
                "patch": {"configuration_property": "B", "format_property": "B"},
            },
            {
                "client_id": "move",
                "kind": "property",
                "action": "move",
                "target_id": {"client_id": "A"},
                "container_id": {"client_id": "group"},
            },
        )
    )
    plan = preview(model, packet, expected_revision=model.revision)
    assert not plan.failures
    model = apply(
        model,
        packet,
        expected_revision=model.revision,
        expected_preview_hash=plan.preview_hash,
        confirmations=tuple((n.code, n.notice_hash) for n in plan.notices),
    )
    names = [e.address.rsplit("/", 1)[-1] for e in render(model).report.entries if e.kind == "pks"]
    assert names == ["B", "A"]
    round_trip(model)


def test_first_group_reuses_header_blank_and_delete_restores_it():
    model = table_model()
    model = execute(
        model,
        ManagerOperation(
            "header",
            "property",
            "create",
            owner_id=model.pko[0].logical_id,
            patch=PropertyPatch(configuration_property="Code", format_property="Code"),
        ),
    )
    text = render(model).text.replace(
        'ДобавитьПКС(СвойстваШапки, "Code", "Code");\n',
        'ДобавитьПКС(СвойстваШапки, "Code", "Code");\n\n',
    )
    model = import_manager(read_manager_text(text), project_id=model.project_id)[0]
    created = table(model)
    assert 'ДобавитьПКС(СвойстваШапки, "Code", "Code");\n\t\n' in render(created).text
    assert '"Rows", "Rows");\n\n' in render(created).text
    round_trip(created)
    removed = execute(
        created,
        ManagerOperation(
            "delete", "table_part", "delete", target_id=created.pko[0].groups[0].logical_id
        ),
    )
    assert render(removed).text == text


def test_group_trailing_comment_survives_both_modes():
    model = table(table_model())
    text = render(model).text.replace(
        'ДобавитьПКТЧ(ПравилоКонвертации, "Rows", "Rows");',
        'ДобавитьПКТЧ(ПравилоКонвертации, "Rows", "Rows"); // комментарий группы',
    )
    model = import_manager(read_manager_text(text), project_id=model.project_id)[0]
    model = execute(
        model,
        ManagerOperation(
            "update",
            "table_part",
            "update",
            target_id=model.pko[0].groups[0].logical_id,
            patch=TablePartPatch(format_property="OtherRows"),
        ),
    )
    for mode in ("preserve", "canonical"):
        assert '"OtherRows"); // комментарий группы' in render(model, mode=mode).text
    round_trip(model)


def test_move_by_address_with_container_only_does_not_anchor_to_itself():
    model = table(table_model())
    group = model.pko[0].groups[0]
    model = execute(
        model,
        ManagerOperation(
            "move",
            "table_part",
            "move",
            address=model_addresses(model)[group.logical_id],
            container_id=model.pko[0].logical_id,
        ),
    )
    round_trip(model)


def test_legacy_last_group_delete_keeps_blank_after_empty_header():
    model = legacy_model()
    model = execute(
        model,
        ManagerOperation(
            "delete-table", "table_part", "delete", target_id=model.pko[0].groups[0].logical_id
        ),
    )
    lines = render(model).text.split("\n")
    assert any(
        line.strip().endswith("= ПравилоКонвертации.Свойства;") and lines[n + 1] == ""
        for n, line in enumerate(lines[:-1])
    )
    round_trip(model)
    model = execute(
        model,
        ManagerOperation(
            "header-column",
            "property",
            "create",
            owner_id=model.pko[0].logical_id,
            patch=PropertyPatch(configuration_property="Code", format_property="Code"),
        ),
    )
    model = execute(
        model,
        ManagerOperation(
            "delete-header", "property", "delete", target_id=model.pko[0].properties[0].logical_id
        ),
    )
    round_trip(model)
