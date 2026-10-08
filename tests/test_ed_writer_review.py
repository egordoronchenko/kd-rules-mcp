"""Регрессии независимого ревью D1: только вымышленные модули-пробы."""

from itertools import permutations

import pytest

from kd_rules_mcp.authoring.ed.manager_operations import (
    ManagerOperation,
    ManagerOperationError,
    ParameterPatch,
    PkoPatch,
    PkpdPatch,
    PropertyPatch,
    ValueMappingPatch,
    apply,
    preview,
)
from kd_rules_mcp.authoring.ed.manager_render import _reread
from kd_rules_mcp.ed.canonical import canonicalize, model_addresses
from kd_rules_mcp.ed.reader import read_manager_text
from kd_rules_mcp.ed.writer import new_manager, render
from kd_rules_mcp.ed.writer_import import import_manager
from kd_rules_mcp.ed.writer_model import Reference, Value, dump_model, load_model
from kd_rules_mcp.validation.ed_writer import validate_writer
from tests.test_ed_writer_values import commit_batch, value_model


def imported(text):
    return import_manager(read_manager_text(text), project_id="review")[0]


def base_text():
    model = value_model()
    model = commit_batch(
        model,
        [
            ManagerOperation(
                "link",
                "property",
                "create",
                owner_id=model.pko[0].logical_id,
                patch=PropertyPatch(
                    configuration_property="Link",
                    format_property="Link",
                    property_kind="reference",
                    conversion=Reference("pko", name="Target"),
                ),
            ),
            ManagerOperation("alpha", "parameter", "create", patch=ParameterPatch(name="Alpha")),
        ],
    )
    return render(model).text.replace("\r\n", "\n")


def property_line(text):
    return next(
        line
        for line in text.splitlines(keepends=True)
        if "ДобавитьПКС(" in line and '"Link"' in line
    )


def guarded_target(text):
    call = "\tДобавитьПКО_Target(ПравилаКонвертации);\n"
    return text.replace(
        call, '\tЕсли НаправлениеОбмена = "Получение" Тогда\n\t' + call + "\tКонецЕсли;\n"
    )


def plan(model, *ops):
    return preview(model, tuple(ops), expected_revision=model.revision)


def checks(model):
    return validate_writer(model, render(model, "preserve").data).issues


def writer_circle(model, mode):
    output = render(model, mode)
    back = imported(output.data.decode("utf-8"))
    assert canonicalize(back) == canonicalize(model)
    assert render(back, mode).data == output.data


def test_b1_move_guard_and_finished_model_direction():
    text = guarded_target(base_text())
    line = property_line(text)
    text = text.replace(
        line, '\tЕсли НаправлениеОбмена = "Получение" Тогда\n\t' + line + "\tКонецЕсли;\n"
    )
    model = imported(text)
    prop = model.pko[0].properties[0]
    result = plan(
        model,
        ManagerOperation(
            "move",
            "property",
            "move",
            target_id=prop.logical_id,
            container_id=model.pko[0].logical_id,
        ),
    )
    assert result.failures and result.failures[0].references
    bad = imported(guarded_target(base_text()))
    assert any(i.check == "ed.writer.reference_direction" for i in checks(bad))


def test_b1_any_property_edit_checks_target_direction():
    model = imported(guarded_target(base_text()))
    result = plan(
        model,
        ManagerOperation(
            "algorithm",
            "property",
            "update",
            target_id=model.pko[0].properties[0].logical_id,
            patch=PropertyPatch(
                property_kind="algorithm",
                algorithm_flag=1,
                conversion=model.pko[0].properties[0].conversion,
            ),
        ),
    )
    assert result.failures and result.failures[0].references


@pytest.mark.parametrize("kind", ["pko", "pkpd"])
def test_b1_retained_tablepart_incoming_directions(kind):
    text = base_text()
    line = property_line(text)
    name = "Target" if kind == "pko" else "Colors"
    text = text.replace(
        line,
        '\tСвойстваТЧ = ДобавитьПКТЧ(ПравилоКонвертации, "Items", "Items");\n'
        + f'\tДобавитьПКС(СвойстваТЧ, "Item", "Item", , "{name}");\n',
    )
    model = imported(text)
    target = next(r for r in getattr(model, kind) if r.name == name)
    patch = PkoPatch(directions=("send",)) if kind == "pko" else PkpdPatch(directions=("send",))
    result = plan(
        model, ManagerOperation("narrow", kind, "update", target_id=target.logical_id, patch=patch)
    )
    assert result.failures and result.failures[0].references


def same_name_model():
    model = imported(base_text())
    model = commit_batch(
        model,
        [
            ManagerOperation(
                "in", "pko", "create", patch=PkoPatch(name="TargetIn", directions=("receive",))
            )
        ],
    )
    text = render(model).text.replace("\r\n", "\n")
    line = next(line for line in text.splitlines() if "ИмяПКО" in line and '"TargetIn"' in line)
    text = text.replace(line, line.replace('"TargetIn"', '"Target"'))
    call = "\tДобавитьПКО_Target(ПравилаКонвертации);\n"
    return imported(
        text.replace(
            call, '\tЕсли НаправлениеОбмена = "Отправка" Тогда\n\t' + call + "\tКонецЕсли;\n"
        )
    )


def test_b2_one_rename_refused_pair_rename_in_either_order():
    model = same_name_model()
    text = render(model).text.replace("\r\n", "\n")
    line = property_line(text)
    model = imported(text.replace(line, line + line.replace('"Link"', '"Other"')))
    targets = [r for r in model.pko if r.name == "Target"]
    ops = [
        ManagerOperation(
            "rename" + str(n),
            "pko",
            "update",
            target_id=r.logical_id,
            patch=PkoPatch(name="Renamed"),
        )
        for n, r in enumerate(targets)
    ]
    assert len(plan(model, ops[0]).failures[0].references) == 2
    assert len(plan(model, ops[1]).failures[0].references) == 2
    for order in permutations(ops):
        changed = commit_batch(model, order)
        assert changed.pko[0].properties[0].conversion.name == "Renamed"
        _reread(changed, render(changed, "canonical"))


def test_b2_unreferenced_same_name_target_does_not_rewrite_property():
    model = same_name_model()
    text = render(model).text.replace("\r\n", "\n")
    line = property_line(text)
    exact = '\tДобавитьПКС(СвойстваШапки,  "Link", "Link", 0, "Target"); // точно\n'
    text = text.replace(
        line, '\tЕсли НаправлениеОбмена = "Отправка" Тогда\n\t' + exact + "\tКонецЕсли;\n"
    )
    model = imported(text)
    target = next(r for r in model.pko if r.name == "Target" and r.directions == ("receive",))
    changed = commit_batch(
        model,
        [
            ManagerOperation(
                "rename-unused",
                "pko",
                "update",
                target_id=target.logical_id,
                patch=PkoPatch(name="Incoming"),
            )
        ],
    )
    assert property_line(render(changed).text) == "\t" + exact
    writer_circle(changed, "preserve")


@pytest.mark.parametrize(
    "kind,action", [("pkpd", "create"), ("pkpd", "update"), ("pko", "create"), ("pko", "update")]
)
def test_v1_shared_namespace(kind, action):
    text = base_text()
    text = text.replace(
        property_line(text),
        '\tСвойстваТЧ = ДобавитьПКТЧ(ПравилоКонвертации, "Items", "Items");\n'
        '\tДобавитьПКС(СвойстваТЧ, "Item", "Item", , "Target");\n',
    )
    model = imported(text)
    name = "Target" if kind == "pkpd" else "Colors"
    patch = (
        PkpdPatch(
            name=name,
            directions=("both",),
            configuration_type=Value(
                "reference", reference_parts=("Метаданные", "Перечисления", "Other")
            ),
            format_type=Value("string", "Other"),
        )
        if kind == "pkpd"
        else PkoPatch(name=name, directions=("both",))
    )
    op = ManagerOperation(
        "collision",
        kind,
        action,
        target_id=getattr(model, kind)[0].logical_id if action == "update" else None,
        patch=patch,
    )
    assert plan(model, op).failures


def test_v1_imported_collision_is_diagnostic_and_does_not_block_unrelated_packet():
    model = imported(base_text().replace('"Target"', '"Colors"'))
    assert any(code == "rule_namespace_collision" for code, _ in model.import_report.diagnostics)
    assert any(i.check == "ed.writer.rule_namespace_collision" for i in checks(model))
    assert not plan(
        model, ManagerOperation("other", "parameter", "create", patch=ParameterPatch(name="Other"))
    ).failures


def test_b1_reports_all_header_and_retained_incoming_references():
    text = base_text()
    line = property_line(text)
    text = text.replace(
        line,
        line + '\tСвойстваТЧ = ДобавитьПКТЧ(ПравилоКонвертации, "Items", "Items");\n'
        '\tДобавитьПКС(СвойстваТЧ, "Item", "Item", , "Target");\n',
    )
    model = imported(text)
    target = next(r for r in model.pko if r.name == "Target")
    result = plan(
        model,
        ManagerOperation(
            "narrow",
            "pko",
            "update",
            target_id=target.logical_id,
            patch=PkoPatch(directions=("send",)),
        ),
    )
    assert len(result.failures[0].references) == 2


def test_b2_delete_create_same_name_does_not_rebind_existing_reference():
    model = imported(base_text())
    target = next(r for r in model.pko if r.name == "Target")
    delete = ManagerOperation("delete", "pko", "delete", target_id=target.logical_id)
    create = ManagerOperation(
        "replace", "pko", "create", patch=PkoPatch(name="Target", directions=("both",))
    )
    for ops in permutations((delete, create)):
        result = plan(model, *ops)
        assert result.failures and result.failures[0].references


def test_m5_non_identifier_parameter_is_diagnostic():
    model = imported(base_text().replace('"Alpha"', '"Имя с пробелом"'))
    assert any(code == "parameter_name" for code, _ in model.import_report.diagnostics)
    assert any(i.check == "ed.writer.parameter_name" for i in checks(model))


def test_m4_direction_move_removes_generated_guard_and_aligns_mapping():
    model = new_manager(project_id="review")
    model = commit_batch(
        model,
        [
            ManagerOperation(
                "send",
                "pkpd",
                "create",
                patch=PkpdPatch(
                    name="Colors",
                    directions=("send",),
                    configuration_type=Value(
                        "reference", reference_parts=("Метаданные", "Перечисления", "Colors")
                    ),
                    format_type=Value("string", "Color"),
                ),
            )
        ],
    )
    model = commit_batch(
        model,
        [
            ManagerOperation(
                "red",
                "value_mapping",
                "create",
                owner_id=model.pkpd[0].logical_id,
                patch=ValueMappingPatch(
                    direction="send",
                    configuration_value=Value(
                        "reference", reference_parts=("Перечисления", "Colors", "Red")
                    ),
                    format_value=Value("string", "Red"),
                ),
            )
        ],
    )
    changed = commit_batch(
        model,
        [
            ManagerOperation(
                "both",
                "pkpd",
                "update",
                target_id=model.pkpd[0].logical_id,
                patch=PkpdPatch(directions=("both",)),
            )
        ],
    )
    assert 'Если НаправлениеОбмена = "Отправка"' not in render(changed).text
    writer_circle(changed, "preserve")
    # После импорта перемещение меняет исходную глубину карты вместе с рамкой.
    original = imported(render(model).text)
    moved = commit_batch(
        original,
        [
            ManagerOperation(
                "move",
                "pkpd",
                "update",
                target_id=original.pkpd[0].logical_id,
                patch=PkpdPatch(directions=("both",)),
            )
        ],
    )
    line = next(
        line for line in render(moved).text.splitlines() if "ЗначенияДляОтправки.Вставить" in line
    )
    assert line.startswith("\tЗначения")


@pytest.mark.parametrize("target", ["target", "COLORS"])
def test_v2_case_preserved_for_unrelated_operation(target):
    text = base_text()
    line = property_line(text)
    text = text.replace(line, line.replace('"Target"', f'"{target}"'))
    model = imported(text)
    changed = commit_batch(
        model,
        [
            ManagerOperation(
                "unrelated", "parameter", "create", patch=ParameterPatch(name="Unrelated")
            )
        ],
    )
    assert property_line(render(changed).text) == property_line(text)
    assert any(i.check == "ed.writer.reference_case_mismatch" for i in checks(model))
    assert any(code == "reference_case_mismatch" for code, _ in model.import_report.diagnostics)


@pytest.mark.parametrize(
    "access", ["ПараметрыКонвертации.Alpha", "КомпонентыОбмена.ПараметрыКонвертации.Alpha"]
)
@pytest.mark.parametrize("action", ["delete", "update"])
def test_v3_parameter_direct_code_use_refused(access, action):
    model = imported(
        base_text() + f"\nПроцедура Business()\n\tЗначение = {access};\nКонецПроцедуры\n"
    )
    result = plan(
        model,
        ManagerOperation(
            "parameter",
            "parameter",
            action,
            target_id=model.parameters[0].logical_id,
            patch=ParameterPatch(name="Omega") if action == "update" else None,
        ),
    )
    assert result.failures and result.failures[0].references
    assert any(r.kind == "parameter" for u in model.code_units for r in u.dependencies)


def test_v3_parameter_created_then_deleted_keeps_usage_protection():
    model = imported(
        base_text()
        + "\nПроцедура Business()\n\tЗначение = ПараметрыКонвертации.New;\nКонецПроцедуры\n"
    )
    created = commit_batch(
        model, [ManagerOperation("new", "parameter", "create", patch=ParameterPatch(name="New"))]
    )
    key = next(p.logical_id for p in created.parameters if p.name == "New")
    unit = next(u for u in created.code_units if u.name == "Business")
    assert any(r.kind == "parameter" and r.target_id == key for r in unit.dependencies)
    writer_circle(created, "preserve")
    result = plan(
        model,
        ManagerOperation("new", "parameter", "create", patch=ParameterPatch(name="New")),
        ManagerOperation("remove", "parameter", "delete", target_id=key),
    )
    assert result.failures and result.failures[0].references


def test_v3_intermediate_parameter_name_in_batch_is_protected():
    model = imported(
        base_text()
        + "\nПроцедура Business()\n\tЗначение = ПараметрыКонвертации.Beta;\nКонецПроцедуры\n"
    )
    key = model.parameters[0].logical_id
    result = plan(
        model,
        ManagerOperation(
            "first", "parameter", "update", target_id=key, patch=ParameterPatch(name="Beta")
        ),
        ManagerOperation(
            "second", "parameter", "update", target_id=key, patch=ParameterPatch(name="Gamma")
        ),
    )
    assert result.failures and result.failures[0].references


@pytest.mark.parametrize(
    "access",
    [
        'ПараметрыКонвертации["Alpha"]',
        'КомпонентыОбмена["ПараметрыКонвертации"]["Alpha"]',
        'ПараметрыКонвертации.Свойство("Alpha")',
        'Вычислить("ПараметрыКонвертации.Alpha")',
    ],
)
def test_v3_computed_parameter_use_requires_confirmation(access):
    model = imported(
        base_text() + f"\nПроцедура Business()\n\tЗначение = {access};\nКонецПроцедуры\n"
    )
    operation = ManagerOperation(
        "rename",
        "parameter",
        "update",
        target_id=model.parameters[0].logical_id,
        patch=ParameterPatch(name="Omega"),
    )
    result = plan(model, operation)
    assert not result.failures
    assert any(n.code == "computed_dependencies" and n.references for n in result.notices)
    with pytest.raises(ManagerOperationError) as error:
        apply(
            model,
            (operation,),
            expected_revision=model.revision,
            expected_preview_hash=result.preview_hash,
        )
    assert error.value.failures[0].reason == "confirmation_required"
    assert commit_batch(model, [operation]).parameters[0].name == "Omega"


@pytest.mark.parametrize("order", [("both", "send"), ("send", "both")])
def test_v4_values_addresses_and_kit_order(order):
    model = new_manager(project_id="review")
    ops = {
        direction: ManagerOperation(
            direction,
            "pkpd",
            "create",
            patch=PkpdPatch(
                name="Colors" if direction == "both" else "Sizes",
                directions=(direction,),
                configuration_type=Value(
                    "reference", reference_parts=("Метаданные", "Перечисления", "Colors")
                ),
                format_type=Value("string", "Color"),
            ),
        )
        for direction in order
    }
    model = commit_batch(model, [ops[d] for d in order])
    addresses = model_addresses(model)
    values = [addresses[c.logical_id] for c in model.layouts if c.kind == "values"]
    assert len(set(values)) == len(values)
    assert all("Colors" in a or "Sizes" in a for a in values)
    _reread(model, render(model, "canonical"))


@pytest.mark.parametrize(
    "configuration,format_name,expected",
    [("Link", "Link", 0), ("", "Link", 1), ("Link", "", 1), ("", "", 0)],
)
def test_v5_algorithm_handler_empty_side(configuration, format_name, expected):
    text = base_text()
    text = text.replace(
        property_line(text),
        f'\tДобавитьПКС(СвойстваШапки, "{configuration}", "{format_name}", 1);\n',
    )
    model = imported(text)
    assert sum(i.check == "ed.writer.algorithm_handler" for i in checks(model)) == expected


def test_v6_mapping_and_pkpd_field_comments_survive():
    model = imported(base_text())
    model = commit_batch(
        model,
        [
            ManagerOperation(
                "red",
                "value_mapping",
                "create",
                owner_id=model.pkpd[0].logical_id,
                patch=ValueMappingPatch(
                    direction="send",
                    configuration_value=Value(
                        "reference", reference_parts=("Перечисления", "Colors", "Red")
                    ),
                    format_value=Value("string", "Red"),
                ),
            )
        ],
    )
    text = render(model).text.replace("\r\n", "\n")
    pair = next(line for line in text.splitlines() if "ЗначенияДляОтправки.Вставить" in line)
    heading = next(line for line in text.splitlines() if "ТипXDTO" in line and '"Color"' in line)
    text = text.replace(pair, pair + " // красный").replace(heading, heading + " // тип формата")
    model = imported(text)
    assert model.pkpd[0].mappings[0].trailing_comment == "// красный"
    changed = commit_batch(
        model,
        [
            ManagerOperation(
                "pair",
                "value_mapping",
                "update",
                target_id=model.pkpd[0].mappings[0].logical_id,
                patch=ValueMappingPatch(format_value=Value("string", "Scarlet")),
            ),
            ManagerOperation(
                "rule",
                "pkpd",
                "update",
                target_id=model.pkpd[0].logical_id,
                patch=PkpdPatch(name="Paints"),
            ),
        ],
    )
    for mode in ("preserve", "canonical"):
        output = render(changed, mode)
        assert "// красный" in output.text and "// тип формата" in output.text
        _reread(changed, output)


@pytest.mark.parametrize("kind", ["parameter", "value_mapping"])
@pytest.mark.parametrize("value", ["a\rb", "a\r\nb"])
def test_m1_carriage_return_refused(kind, value):
    model = imported(base_text())
    patch = (
        ParameterPatch(name="CR", default=Value("string", value))
        if kind == "parameter"
        else ValueMappingPatch(
            direction="send",
            configuration_value=Value(
                "reference", reference_parts=("Перечисления", "Colors", "Red")
            ),
            format_value=Value("string", value),
        )
    )
    op = ManagerOperation(
        "cr",
        kind,
        "create",
        owner_id=model.pkpd[0].logical_id if kind == "value_mapping" else None,
        patch=patch,
    )
    assert plan(model, op).failures


def test_m2_decimal_parameter_exact_roundtrip_and_rename():
    text = base_text().replace(
        'ПараметрыКонвертации.Вставить("Alpha");',
        'ПараметрыКонвертации.Вставить("Alpha", 12345678901234567.89);',
    )
    model = imported(text)
    changed = commit_batch(
        model,
        [
            ManagerOperation(
                "decimal",
                "parameter",
                "update",
                target_id=model.parameters[0].logical_id,
                patch=ParameterPatch(name="Omega"),
            )
        ],
    )
    assert canonicalize(load_model(dump_model(changed))) == canonicalize(changed)
    for mode in ("preserve", "canonical"):
        assert "12345678901234567.89" in render(changed, mode).text
        writer_circle(changed, mode)


def test_m3_date_with_separators_retained_or_generated():
    text = base_text().replace(
        'ПараметрыКонвертации.Вставить("Alpha");',
        "ПараметрыКонвертации.Вставить(\"Alpha\", '2020.01.31');",
    )
    model = imported(text)
    output = render(model, "canonical")
    assert "2020.01.31" in output.text
    writer_circle(model, "canonical")


def test_m4_create_delete_pkpd_is_byte_reversible():
    model = imported(base_text())
    original = render(model).data
    created = commit_batch(
        model,
        [
            ManagerOperation(
                "new",
                "pkpd",
                "create",
                patch=PkpdPatch(
                    name="Incoming",
                    directions=("receive",),
                    configuration_type=Value(
                        "reference", reference_parts=("Метаданные", "Перечисления", "Colors")
                    ),
                    format_type=Value("string", "Color"),
                ),
            )
        ],
    )
    key = next(r.logical_id for r in created.pkpd if r.name == "Incoming")
    deleted = commit_batch(created, [ManagerOperation("delete", "pkpd", "delete", target_id=key)])
    assert render(deleted).data == original


def test_m5_parameter_case_duplicate_import_diagnostic():
    text = base_text().replace(
        'ПараметрыКонвертации.Вставить("Alpha");',
        'ПараметрыКонвертации.Вставить("Alpha");\n\tПараметрыКонвертации.Вставить("alpha", 1);',
    )
    model = imported(text)
    assert any(code == "parameter_duplicate" for code, _ in model.import_report.diagnostics)
    assert any(i.check == "ed.writer.parameter_duplicate" for i in checks(model))


@pytest.mark.parametrize(
    "direction,configuration,format_name", [("send", "", "Link"), ("receive", "Link", "")]
)
def test_m6_legal_one_sided_reference(direction, configuration, format_name):
    model = value_model()
    model = commit_batch(
        model,
        [
            ManagerOperation(
                "target-dir",
                "pko",
                "update",
                target_id=model.pko[1].logical_id,
                patch=PkoPatch(directions=(direction,)),
            )
        ],
    )
    model = commit_batch(
        model,
        [
            ManagerOperation(
                "one-sided",
                "property",
                "create",
                owner_id=model.pko[0].logical_id,
                patch=PropertyPatch(
                    configuration_property=configuration,
                    format_property=format_name,
                    property_kind="reference",
                    conversion=Reference("pko", name="Target"),
                ),
            )
        ],
    )
    assert not any(i.check == "ed.writer.reference_direction" for i in checks(model))
