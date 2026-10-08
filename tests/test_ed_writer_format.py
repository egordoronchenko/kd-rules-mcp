"""URI в формах G:2326–2361, 2435–2444 и init ПКО, без исполнения BSL."""

from dataclasses import replace

import pytest

from kd_rules_mcp.authoring.ed.manager_operations import (
    ManagerOperation,
    PkoPatch,
    PropertyPatch,
    TablePartPatch,
    parse_operation,
    preview,
)
from kd_rules_mcp.ed import build_references
from kd_rules_mcp.ed.address import build_addresses
from kd_rules_mcp.ed.reader import read_manager_text
from kd_rules_mcp.ed.writer import new_manager, render
from kd_rules_mcp.ed.writer_import import import_manager
from kd_rules_mcp.ed.writer_model import Reference, content_hash
from kd_rules_mcp.validation.ed_links import validate_links
from tests.test_ed_writer import execute
from tests.test_ed_writer_code import round_trip
from tests.test_ed_writer_tables import table_model

URI = "urn:fiction:extension"


@pytest.mark.parametrize("kind", ["header", "table", "row"])
def test_uri_forms_are_editable_and_round_trip_bytes(kind):
    model = table_model()
    model = execute(
        model,
        ManagerOperation(
            "init",
            "pko",
            "update",
            target_id=model.pko[0].logical_id,
            patch=PkoPatch(extensions=(URI,)),
        ),
    )
    owner_id = model.pko[0].logical_id
    if kind in ("table", "row"):
        model = execute(
            model,
            ManagerOperation(
                "table",
                "table_part",
                "create",
                owner_id=owner_id,
                patch=TablePartPatch(
                    configuration_property="Rows",
                    format_property="Rows",
                    namespace=URI if kind == "table" else "",
                ),
            ),
        )
        owner_id = model.pko[0].groups[0].logical_id
    model = execute(
        model,
        ManagerOperation(
            "column",
            "property",
            "create",
            owner_id=owner_id,
            patch=PropertyPatch(
                configuration_property="Amount",
                format_property="Amount",
                namespace=URI if kind != "table" else "",
            ),
        ),
    )
    output = render(model)
    if kind == "table":
        assert f'ДобавитьПКТЧ(ПравилоКонвертации, "Rows", "Rows", "{URI}");' in output.text
    else:
        assert f'"Amount", "Amount", , , "{URI}");' in output.text
    back = import_manager(read_manager_text(output.text), project_id=model.project_id)[0]
    assert back.pko[0].state == "editable"
    assert all(g.state == "editable" for g in back.pko[0].groups)
    assert all(
        p.state == "editable"
        for p in (*back.pko[0].properties, *(p for g in back.pko[0].groups for p in g.properties))
    )
    for mode in ("preserve", "canonical"):
        assert render(back, mode).data == output.data
    round_trip(model)


def test_init_without_properties_create_update_clear_and_delete():
    model = execute(
        table_model(),
        parse_operation(
            {
                "client_id": "init",
                "kind": "pko",
                "action": "update",
                "target_id": table_model().pko[0].logical_id,
                "patch": {"extensions": [URI, "urn:second"]},
            }
        ),
    )
    assert model.pko[0].extensions == (URI, "urn:second")
    assert render(model).text.count("ИнициализироватьРасширениеПравилаКонвертацииОбъекта(") == 2
    round_trip(model)
    model = execute(
        model,
        ManagerOperation(
            "replace",
            "pko",
            "update",
            target_id=model.pko[0].logical_id,
            patch=PkoPatch(extensions=("urn:changed",)),
        ),
    )
    assert URI not in render(model).text
    round_trip(model)
    cleared = execute(
        model,
        ManagerOperation(
            "clear", "pko", "update", target_id=model.pko[0].logical_id, clear=("extensions",)
        ),
    )
    assert not cleared.pko[0].extensions
    assert "ИнициализироватьРасширениеПравилаКонвертацииОбъекта(" not in render(cleared).text
    round_trip(cleared)


@pytest.mark.parametrize("direction", ["send", "receive", "both"])
@pytest.mark.parametrize("mode", ["preserve", "canonical"])
@pytest.mark.parametrize("use_source_style", [True, False])
def test_new_extension_init_suffix_matches_generator(direction, mode, use_source_style):
    # ШаблоныТекстовМодулей/Ext/Template.txt:55; подстановка
    # ВыгрузкаМодуля/Ext/ObjectModule.bsl:2541–2550: табуляция остаётся у последнего init.
    model = table_model()
    model = execute(
        model,
        ManagerOperation(
            "init",
            "pko",
            "update",
            target_id=model.pko[0].logical_id,
            patch=PkoPatch(extensions=(URI, "urn:second"), directions=(direction,)),
        ),
    )
    lines = [
        line
        for line in render(model, mode, use_source_style=use_source_style).text.splitlines()
        if "ИнициализироватьРасширениеПравилаКонвертацииОбъекта(" in line
    ]
    assert len(lines) == 2
    assert lines[0] == lines[0].rstrip(" \t")
    suffix = "\t" if direction in ("receive", "both") else ""
    assert lines[-1] == lines[-1].rstrip(" \t") + suffix
    round_trip(model)


def test_create_pko_with_init_and_no_properties():
    model = execute(
        new_manager(),
        ManagerOperation(
            "rule",
            "pko",
            "create",
            patch=PkoPatch(name="Item", directions=("send",), extensions=(URI,)),
        ),
    )
    assert model.pko[0].extensions == (URI,)
    assert not model.pko[0].properties
    round_trip(model)


@pytest.mark.parametrize("direction", ["receive", "both"])
@pytest.mark.parametrize("mode", ["preserve", "canonical"])
@pytest.mark.parametrize("use_source_style", [True, False])
@pytest.mark.parametrize("style_suffix", ["\t", "\t\t"])
def test_new_extension_init_does_not_duplicate_imported_trailing_tab(
    direction, mode, use_source_style, style_suffix
):
    # ШаблоныТекстовМодулей/Ext/Template.txt:55 и
    # ВыгрузкаМодуля/Ext/ObjectModule.bsl:2541–2550: одна табуляция у последнего init.
    model = table_model()
    model = execute(
        model,
        ManagerOperation(
            "init",
            "pko",
            "update",
            target_id=model.pko[0].logical_id,
            patch=PkoPatch(extensions=("urn:original",), directions=("receive",)),
        ),
    )
    source = render(model).text
    assert '"urn:original");\t\n' in source
    model = import_manager(read_manager_text(source), project_id=model.project_id)[0]
    assert render(model).text == source
    assert dict(model.module_styles[0].line_suffixes)["extensions"] == "\t"
    # Даже прежний снимок с выученным удвоением не должен портить новые строки.
    model = replace(
        model,
        module_styles=tuple(
            replace(
                style,
                line_suffixes=tuple(
                    (name, style_suffix if name == "extensions" else suffix)
                    for name, suffix in style.line_suffixes
                ),
            )
            for style in model.module_styles
        ),
    )
    model = replace(model, revision=content_hash(model))
    changed = execute(
        model,
        ManagerOperation(
            "new",
            "pko",
            "create",
            patch=PkoPatch(name="Another", directions=(direction,), extensions=(URI, "urn:second")),
        ),
    )
    output = render(changed, mode, use_source_style=use_source_style)
    lines = [line for line in output.text.splitlines() if URI in line or "urn:second" in line]
    assert len(lines) == 2
    assert lines[0] == lines[0].rstrip(" \t")
    assert lines[-1] == lines[-1].rstrip(" \t") + "\t"
    assert '"urn:original");\t\n' in output.text
    back = import_manager(read_manager_text(output.text), project_id=model.project_id)[0]
    assert render(back, mode, use_source_style=use_source_style).data == output.data
    round_trip(changed)


def test_init_is_before_properties_when_added_to_imported_rule():
    model = execute(
        table_model(),
        ManagerOperation(
            "column",
            "property",
            "create",
            owner_id=table_model().pko[0].logical_id,
            patch=PropertyPatch(configuration_property="A", format_property="A"),
        ),
    )
    model = import_manager(read_manager_text(render(model).text), project_id=model.project_id)[0]
    model = execute(
        model,
        ManagerOperation(
            "init",
            "pko",
            "update",
            target_id=model.pko[0].logical_id,
            patch=PkoPatch(extensions=(URI,)),
        ),
    )
    text = render(model).text
    assert text.index("ИнициализироватьРасширениеПравилаКонвертацииОбъекта(") < text.index(
        "СвойстваШапки ="
    )
    round_trip(model)


def test_property_uri_without_init_warns():
    model = execute(
        table_model(),
        ManagerOperation(
            "column",
            "property",
            "create",
            owner_id=table_model().pko[0].logical_id,
            patch=PropertyPatch(configuration_property="A", format_property="A", namespace=URI),
        ),
    )
    document = read_manager_text(render(model).text)
    report = validate_links(document, build_addresses(document), build_references(document))
    assert any(i.check == "ed.extension.uninitialized" for i in report.issues)


def test_table_namespace_update_and_remove():
    model = execute(
        table_model(),
        ManagerOperation(
            "table",
            "table_part",
            "create",
            owner_id=table_model().pko[0].logical_id,
            patch=TablePartPatch(configuration_property="Rows", format_property="Rows"),
        ),
    )
    for index, uri in enumerate((URI, "urn:changed", "")):
        model = execute(
            model,
            ManagerOperation(
                f"uri-{index}",
                "table_part",
                "update",
                target_id=model.pko[0].groups[0].logical_id,
                patch=TablePartPatch(namespace=uri),
            ),
        )
        assert model.pko[0].groups[0].namespace == uri
        round_trip(model)


def test_reference_property_uri_preserves_empty_algorithm_position():
    model = table_model()
    target = model.pko[0]
    model = execute(
        model,
        ManagerOperation(
            "table",
            "table_part",
            "create",
            owner_id=target.logical_id,
            patch=TablePartPatch(configuration_property="Rows", format_property="Rows"),
        ),
    )
    model = execute(
        model,
        ManagerOperation(
            "ref",
            "property",
            "create",
            owner_id=model.pko[0].groups[0].logical_id,
            patch=PropertyPatch(
                configuration_property="Parent",
                format_property="Parent",
                property_kind="reference",
                conversion=Reference("pko", target.logical_id, target.name, "resolved"),
                namespace=URI,
            ),
        ),
    )
    assert f'"Parent", "Parent", , "Item", "{URI}");' in render(model).text
    round_trip(model)


@pytest.mark.parametrize("uris", [("",), (URI, URI), ("urn:\ninvalid",)])
def test_invalid_init_uris_refuse(uris):
    model = table_model()
    operation = ManagerOperation(
        "bad", "pko", "update", target_id=model.pko[0].logical_id, patch=PkoPatch(extensions=uris)
    )
    assert preview(model, (operation,), expected_revision=model.revision).failures


def test_unknown_schema_namespace_warns_and_known_namespace_does_not():
    from kd_rules_mcp.ed.address import build_addresses
    from kd_rules_mcp.ed.schema.profile import ValidationProfile
    from kd_rules_mcp.validation.ed_schema import validate_schema
    from tests.data.ed.format_package.model import sample_model

    model = execute(
        table_model(),
        ManagerOperation(
            "column",
            "property",
            "create",
            owner_id=table_model().pko[0].logical_id,
            patch=PropertyPatch(configuration_property="A", format_property="A", namespace=URI),
        ),
    )
    document = read_manager_text(render(model).text)
    schema = sample_model().base_schema
    report = validate_schema(
        document, schema, build_addresses(document), ValidationProfile.build(schema, "1.20")
    )
    issue = next(i for i in report.issues if i.check == "ed.schema.namespace_unknown")
    assert "переданном пакете" not in issue.message
    assert "ed_schema_open" in issue.message and "extensions" in issue.message
    assert "format_package" in issue.message
    known = replace(schema, extension_namespaces=(URI,))
    report = validate_schema(
        document, known, build_addresses(document), ValidationProfile.build(known, "1.20")
    )
    assert not any(i.check == "ed.schema.namespace_unknown" for i in report.issues)
