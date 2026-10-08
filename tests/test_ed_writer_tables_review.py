"""Регрессии W3 на синтетических входах независимого ревью."""

from dataclasses import replace

import pytest

from kd_rules_mcp.authoring.ed.manager_operations import (
    ManagerOperation,
    PkoPatch,
    PropertyPatch,
    TablePartPatch,
    preview,
)
from kd_rules_mcp.ed.canonical import canonicalize
from kd_rules_mcp.ed.reader import read_manager_text
from kd_rules_mcp.ed.writer import render
from kd_rules_mcp.ed.writer_import import import_manager
from kd_rules_mcp.ed.writer_model import Reference
from kd_rules_mcp.validation.ed_writer import validate_writer
from tests.test_ed_writer_code import execute, round_trip
from tests.test_ed_writer_tables import legacy_model, table, table_model


def imported(text, model):
    return import_manager(read_manager_text(text), project_id=model.project_id)[0]


def two_groups():
    model = table(table(table_model()), "lines", "Lines", "Lines")
    for group in model.pko[0].groups:
        model = execute(
            model,
            ManagerOperation(
                "qty-" + group.logical_id,
                "property",
                "create",
                owner_id=group.logical_id,
                patch=PropertyPatch(configuration_property="Qty", format_property="Qty"),
            ),
        )
    return model


@pytest.mark.parametrize(
    "separators",
    [("", "", ""), ("\n", "\n", "\n"), ("   ", "   ", "   "), ("", " \t ", "\t")],
)
@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_whitespace_table_separators_preserve_and_edit_one_line(separators, newline):
    model = two_groups()
    text = render(model, "canonical").text
    before, between, after = separators
    # Перед первой группой, между группами, после последней ПКС.
    text = text.replace("\t\n\tСвойстваТЧ", before + "\n\tСвойстваТЧ", 1)
    text = text.replace("\t\n\tСвойстваТЧ", between + "\n\tСвойстваТЧ", 1)
    last = text.rindex('ДобавитьПКС(СвойстваТЧ, "Qty", "Qty");')
    end = text.index("\n", last)
    text = text[:end] + text[end:].replace("\n\n", "\n" + after + "\n", 1)
    text = text.replace("\n", newline)
    model = imported(text, model)
    assert render(model).text == text
    assert all(g.state == "editable" for g in model.pko[0].groups)
    assert not any(b.kind in ("pks", "pktch") for b in model.retained_blocks)
    assert canonicalize(imported(render(model).text, model)) == canonicalize(model)
    prop = model.pko[0].groups[0].properties[0]
    changed = execute(
        model,
        ManagerOperation(
            "quantity",
            "property",
            "update",
            target_id=prop.logical_id,
            patch=PropertyPatch(format_property="Quantity"),
        ),
    )
    assert render(changed).text == text.replace('"Qty", "Qty"', '"Qty", "Quantity"', 1)


@pytest.mark.parametrize("mode", ["preserve", "canonical"])
def test_table_and_column_trailing_comments_survive_edit(mode):
    model = two_groups()
    text = render(model).text
    text = text.replace('"Rows");', '"Rows"); // строки', 1)
    text = text.replace('"Qty", "Qty");', '"Qty", "Qty"); // количество', 1)
    model = imported(text, model)
    group = model.pko[0].groups[0]
    assert group.trailing_comment == "// строки"
    assert group.properties[0].trailing_comment == "// количество"
    model = execute(
        model,
        ManagerOperation(
            "qty",
            "property",
            "update",
            target_id=group.properties[0].logical_id,
            patch=PropertyPatch(format_property="Quantity"),
        ),
    )
    model = execute(
        model,
        ManagerOperation(
            "lines",
            "table_part",
            "update",
            target_id=group.logical_id,
            patch=TablePartPatch(format_property="Lines2"),
        ),
    )
    output = render(model, mode).text
    assert output.count("// строки") == output.count("// количество") == 1
    round_trip(model)


@pytest.mark.parametrize("separator", ["", "\n\n", "  \t \n", "\t\n", " \n\n\t \n"])
@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_group_update_preserves_exact_original_separator(separator, newline):
    model = two_groups()
    text = render(model, "canonical").text
    declaration = next(
        line for line in text.split("\n") if "ДобавитьПКТЧ" in line and '"Lines"' in line
    )
    text = text.replace("\t\n" + declaration, separator + declaration, 1).replace("\n", newline)
    model = imported(text, model)
    group = next(g for g in model.pko[0].groups if g.format_property == "Lines")
    changed = execute(
        model,
        ManagerOperation(
            "group-separator",
            "table_part",
            "update",
            target_id=group.logical_id,
            patch=TablePartPatch(format_property="Lines2"),
        ),
    )
    expected = text.replace(declaration, declaration.replace(', "Lines");', ', "Lines2");'), 1)
    assert render(changed, "preserve").text == expected
    assert (
        "\t" + newline + declaration.replace(', "Lines");', ', "Lines2");')
        in render(changed, "canonical").text
    )
    round_trip(changed)


@pytest.mark.parametrize("direction", ["send", "receive"])
@pytest.mark.parametrize("action", ["create", "update", "move", "direction"])
def test_case_insensitive_duplicate_tables_all_operations(direction, action):
    model = two_groups()
    rule = model.pko[0]
    # Импортированное расхождение законно сохраняется; операция добавляет новое направление.
    if action == "direction":
        model = replace(
            model, pko=(replace(rule, directions=("receive" if direction == "send" else "send",)),)
        ).with_revision()
        model = replace(
            model,
            pko=(
                replace(
                    model.pko[0],
                    groups=tuple(
                        replace(g, configuration_property="rows", format_property="rows")
                        for g in model.pko[0].groups
                    ),
                ),
            ),
        ).with_revision()
        op = ManagerOperation(
            "direction",
            "pko",
            "update",
            target_id=rule.logical_id,
            patch=PkoPatch(directions=(direction,)),
        )
    else:
        model = execute(
            model,
            ManagerOperation(
                "direction",
                "pko",
                "update",
                target_id=rule.logical_id,
                patch=PkoPatch(directions=(direction,)),
            ),
        )
        rule = model.pko[0]
        if action == "create":
            op = ManagerOperation(
                "duplicate",
                "table_part",
                "create",
                owner_id=rule.logical_id,
                patch=TablePartPatch(configuration_property="rows", format_property="rows"),
            )
        elif action == "update":
            op = ManagerOperation(
                "duplicate",
                "table_part",
                "update",
                target_id=rule.groups[1].logical_id,
                patch=TablePartPatch(configuration_property="rows", format_property="rows"),
            )
        else:
            # Перенос из другого ПКО в контейнер с одноимённой ТЧ.
            model = execute(
                model,
                ManagerOperation(
                    "other", "pko", "create", patch=PkoPatch(name="Other", directions=(direction,))
                ),
            )
            model = execute(
                model,
                ManagerOperation(
                    "other-table",
                    "table_part",
                    "create",
                    owner_id=model.pko[1].logical_id,
                    patch=TablePartPatch(configuration_property="rows", format_property="rows"),
                ),
            )
            op = ManagerOperation(
                "duplicate",
                "table_part",
                "move",
                target_id=model.pko[1].groups[0].logical_id,
                container_id=rule.logical_id,
            )
    plan = preview(model, (op,), expected_revision=model.revision)
    if direction == "send":
        assert any(f.reason == "model_invalid" and "затирают" in f.message for f in plan.failures)
    else:
        assert not plan.failures
        assert any(
            n.code == "table_part_duplicate" and n.requires_confirmation for n in plan.notices
        )


@pytest.mark.parametrize("direction", ["send", "receive"])
def test_import_case_duplicate_is_diagnosed_without_repair(direction):
    model = two_groups()
    rule = replace(model.pko[0], directions=(direction,))
    groups = tuple(
        replace(
            g,
            configuration_property="Rows" if n else "rows",
            format_property="Rows" if n else "rows",
        )
        for n, g in enumerate(rule.groups)
    )
    model = replace(model, pko=(replace(rule, groups=groups),)).with_revision()
    text = render(model).text
    model = imported(text, model)
    assert render(model).text == text
    assert (
        sum(
            i.check == "ed.writer.table_part_duplicate"
            for i in validate_writer(model, render(model).data).issues
        )
        == 1
    )


def test_legacy_compact_separator_does_not_change_string_literal():
    model = legacy_model()
    model = execute(
        model,
        ManagerOperation(
            "target", "pko", "create", patch=PkoPatch(name="Target", directions=("both",))
        ),
    )
    literal = 'a, 1, "b'
    model = execute(
        model,
        ManagerOperation(
            "literal",
            "property",
            "create",
            owner_id=model.pko[0].groups[0].logical_id,
            patch=PropertyPatch(
                configuration_property="",
                format_property=literal,
                property_kind="algorithm",
                algorithm_flag=1,
                conversion=Reference("pko", name="Target"),
            ),
        ),
    )
    text = render(model).text
    assert ', 1,"Target"' in text
    back = imported(text, model)
    assert any(
        p.format_property == literal for r in back.pko for g in r.groups for p in g.properties
    )
    round_trip(model)


@pytest.mark.parametrize("side", ["configuration_property", "format_property"])
@pytest.mark.parametrize("action", ["create", "update"])
def test_invalid_table_name_refuses_operation_and_is_retained_on_import(side, action):
    model = table(table_model())
    op = ManagerOperation(
        "bad",
        "table_part",
        action,
        owner_id=model.pko[0].logical_id,
        target_id=model.pko[0].groups[0].logical_id if action == "update" else None,
        patch=TablePartPatch(
            configuration_property="Rows 2" if side == "configuration_property" else "Rows",
            format_property="Rows 2" if side == "format_property" else "Rows",
        ),
    )
    plan = preview(model, (op,), expected_revision=model.revision)
    assert any(f.reason == "model_invalid" and "идентификатор" in f.message for f in plan.failures)
    text = render(model).text.replace(
        '"Rows", "Rows"',
        '"Rows 2", "Rows"' if side == "configuration_property" else '"Rows", "Rows 2"',
    )
    back = imported(text, model)
    assert back.pko[0].groups[0].state == "retained"
    assert any(e.reason == "invalid_table_part_name" for e in back.import_report.entries)
    assert render(back).text == text


def test_direct_column_in_algorithmic_send_group_is_preserved_by_executor():
    model = table(table_model(), configuration="", format_name="Rows")
    model = execute(
        model,
        ManagerOperation(
            "direction",
            "pko",
            "update",
            target_id=model.pko[0].logical_id,
            patch=PkoPatch(directions=("send",)),
        ),
    )
    for client, patch in (
        (
            "algorithm",
            PropertyPatch(
                configuration_property="",
                format_property="Amount",
                property_kind="algorithm",
                algorithm_flag=1,
            ),
        ),
        ("direct", PropertyPatch(configuration_property="Qty", format_property="Qty")),
    ):
        model = execute(
            model,
            ManagerOperation(
                client,
                "property",
                "create",
                owner_id=model.pko[0].groups[0].logical_id,
                patch=patch,
            ),
        )
    assert not any(
        i.check == "ed.writer.table_part_direct_empty"
        for i in validate_writer(model, render(model).data).issues
    )


@pytest.mark.parametrize("kind", ["pko", "pkpd"])
def test_missing_table_property_target_message_names_kind_name_direction_and_hint(kind):
    model = table(table_model())
    op = ManagerOperation(
        "ghost",
        "property",
        "create",
        owner_id=model.pko[0].groups[0].logical_id,
        patch=PropertyPatch(
            configuration_property="Product",
            format_property="Product",
            property_kind="pkpd" if kind == "pkpd" else "reference",
            conversion=Reference(kind, name="Ghost"),
        ),
    )
    plan = preview(model, (op,), expected_revision=model.revision)
    failure = next(f for f in plan.failures if f.reason == "dangling_reference")
    assert all(
        word in failure.message
        for word in ({"pko": "ПКО", "pkpd": "ПКПД"}[kind], "Ghost", "receive", "раньше", "пакете")
    )


def test_moving_reference_to_unavailable_direction_names_target():
    model = table(table_model())
    model = execute(
        model,
        ManagerOperation(
            "target", "pko", "create", patch=PkoPatch(name="Target", directions=("send",))
        ),
    )
    model = execute(
        model,
        ManagerOperation(
            "product",
            "property",
            "create",
            owner_id=model.pko[1].logical_id,
            patch=PropertyPatch(
                configuration_property="Product",
                format_property="Product",
                property_kind="reference",
                conversion=Reference("pko", name="Target"),
            ),
        ),
    )
    op = ManagerOperation(
        "move",
        "property",
        "move",
        target_id=model.pko[1].properties[0].logical_id,
        container_id=model.pko[0].groups[0].logical_id,
    )
    plan = preview(model, (op,), expected_revision=model.revision)
    failure = next(f for f in plan.failures if f.reason == "dangling_reference")
    assert all(word in failure.message for word in ("ПКО", "Target", "receive", "раньше", "пакете"))
