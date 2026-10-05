"""Порождение W1 на собственных данных; пробы третьего ревью и золотые файлы."""

from dataclasses import replace
from itertools import product
from pathlib import Path
from typing import Any, cast

import pytest

from kd2_rules_mcp.authoring.ed.manager_operations import (
    IdentificationPatch,
    ManagerOperation,
    ManagerOperationError,
    ManagerPatch,
    PkoPatch,
    PodPatch,
    PropertyPatch,
    apply,
    preview,
)
from kd2_rules_mcp.ed.canonical import canonicalize
from kd2_rules_mcp.ed.diff import compare_models
from kd2_rules_mcp.ed.reader import read_manager_text
from kd2_rules_mcp.ed.writer import new_manager, render
from kd2_rules_mcp.ed.writer_import import import_manager
from kd2_rules_mcp.ed.writer_model import Reference, Value, dump_model, load_model, partition_report
from tests.test_ed_writer_model import SYNTHETIC, imported, layout_input


def execute(model, operation):
    planned = preview(model, (operation,), expected_revision=model.revision)
    assert not planned.failures, planned.failures
    return apply(
        model,
        (operation,),
        expected_revision=model.revision,
        expected_preview_hash=planned.preview_hash,
        confirmations=tuple((n.code, n.notice_hash) for n in planned.notices),
    )


def reread(model, output):
    document = read_manager_text(output.data.decode("utf-8"))
    return document, import_manager(
        document, project_id=model.project_id, manager_name=model.header.manager_name
    )[0]


def pilot_model(interface_version=2, directions=("send", "receive")):
    """Собственная модель пилота: один ПКО, два ПОД, две ПКС и поиск по наименованию."""
    model = new_manager(
        project_id="pilot", manager_name="PilotManager", interface_version=interface_version
    )
    model = execute(
        model,
        ManagerOperation(
            "rule",
            "pko",
            "create",
            patch=PkoPatch(
                name="Должности",
                directions=directions,
                configuration_object=Value(
                    "reference", reference_parts=("Метаданные", "Справочники", "Должности")
                ),
                format_object=Value("string", "Справочник.Должности"),
            ),
        ),
    )
    rule = model.pko[0]
    for name in ("Наименование", "НаименованиеКраткое"):
        model = execute(
            model,
            ManagerOperation(
                "property-" + name,
                "property",
                "create",
                owner_id=rule.logical_id,
                container_id=rule.logical_id,
                after_id=model.pko[0].properties[-1].logical_id
                if model.pko[0].properties
                else None,
                patch=PropertyPatch(configuration_property=name, format_property=name),
            ),
        )
    model = execute(
        model,
        ManagerOperation(
            "identify",
            "identification",
            "update",
            target_id=rule.identification.logical_id,
            patch=IdentificationPatch(
                mode=Value("string", "СначалаПоУникальномуИдентификаторуПотомПоПолямПоиска"),
                search_sets=(("Наименование",),),
            ),
        ),
    )
    for direction in ("send", "receive"):
        fields: dict[str, Any] = (
            {
                "configuration_selection": Value(
                    "reference", reference_parts=("Метаданные", "Справочники", "Должности")
                ),
                "clear_data": Value("boolean", False),
            }
            if direction == "send"
            else {"format_selection": Value("string", "Справочник.Должности")}
        )
        model = execute(
            model,
            ManagerOperation(
                "pod-" + direction,
                "pod",
                "create",
                patch=PodPatch(
                    name="Должности_" + ("Отправка" if direction == "send" else "Получение"),
                    directions=(direction,),
                    used_pko=(Reference("pko", rule.logical_id, rule.name, "resolved"),),
                    **fields,
                ),
            ),
        )
    return model


@pytest.mark.parametrize(
    "factory,name", [(new_manager, "empty"), (pilot_model, "positions"), (pilot_model, "pilot")]
)
def test_goldens_are_generated_complete_and_stable(factory, name):
    model = factory()
    output = render(model, "canonical")
    assert output.data == (Path(__file__).parent / "data/ed/writer" / (name + ".bsl")).read_bytes()
    document, back = reread(model, output)
    assert document.parse_status == "complete"
    assert not document.unknown
    assert canonicalize(model) == canonicalize(back)
    assert render(back, "canonical").data == output.data
    assert render(model).data == output.data
    assert canonicalize(load_model(dump_model(model))) == canonicalize(model)


@pytest.mark.parametrize(
    "text", [SYNTHETIC, layout_input(), "\ufeff" + layout_input().replace("\n", "\r\n")]
)
@pytest.mark.parametrize("mode", ["preserve", "canonical"])
def test_synthetic_roundtrip_and_exact_preserve(text, mode):
    model, _ = imported(text)
    output = render(model, mode)
    _, back = reread(model, output)
    assert canonicalize(model) == canonicalize(back)
    assert sorted(b.sha256 for b in model.retained_blocks) == sorted(
        b.sha256 for b in back.retained_blocks
    )
    assert render(back, mode).data == output.data
    if mode == "preserve":
        assert output.data == text.encode("utf-8")
    assert all(ok for _, _, _, ok in partition_report(back))
    assert output.report.entries[-1].byte_end == len(output.data)


def test_preserve_regenerates_only_modified_leaf():
    model, _ = imported()
    prop = model.pko[0].properties[0]
    changed = execute(
        model,
        ManagerOperation(
            "edit",
            "property",
            "update",
            target_id=prop.logical_id,
            patch=PropertyPatch(configuration_property="NewCode"),
        ),
    )
    result = render(changed)
    assert [e.leaf_id for e in result.report.entries if e.state == "regenerated"] == [
        prop.logical_id
    ]
    _, back = reread(changed, result)
    assert compare_models(changed, back).equal
    assert len(compare_models(model, back).changes) == 1


def test_third_review_boundaries_fingerprints_and_shared_line():
    text = layout_input().replace(
        'ДобавитьПКС(СвойстваШапки, "A", "A");',
        'ДобавитьПКС(СвойстваШапки, "A", "A"); ДобавитьПКС(СвойстваШапки, "Second", "Second");',
    )
    model, _ = imported(text)
    rule = model.pko[0]
    shared = [p for p in rule.properties if p.name in ("A", "Second")]
    assert len(shared) == 2 and shared[0].inside_leaf_id == shared[1].inside_leaf_id
    assert all(p.state == "retained" for p in shared)
    for prop in shared:
        plan = preview(
            model,
            (
                ManagerOperation(
                    "del-" + prop.name, "property", "delete", target_id=prop.logical_id
                ),
            ),
            expected_revision=model.revision,
        )
        assert plan.failures[0].reason == "opaque_context_changed"
    changed = execute(
        model,
        ManagerOperation(
            "first",
            "property",
            "create",
            owner_id=rule.logical_id,
            container_id=rule.logical_id,
            after_id=None,
            patch=PropertyPatch(configuration_property="First", format_property="First"),
        ),
    )
    container = next(c for c in changed.layouts if c.logical_id == rule.logical_id)
    marker = next(i for i, e in enumerate(container.elements) if e.field == "properties_start")
    assert container.elements[marker + 1].entity_id == changed.pko[0].properties[-1].logical_id
    before = next(e for e in container.elements if e.field == "name")
    plan = preview(
        model,
        (
            ManagerOperation(
                "bad",
                "property",
                "create",
                owner_id=rule.logical_id,
                container_id=rule.logical_id,
                after_id=before.logical_id,
                patch=PropertyPatch(configuration_property="Bad", format_property="Bad"),
            ),
        ),
        expected_revision=model.revision,
    )
    assert plan.failures[0].reason == "model_invalid"
    _, back = reread(changed, render(changed))
    assert canonicalize(changed) == canonicalize(back)


@pytest.mark.parametrize("action", ["create", "move", "delete", "identify", "rename"])
def test_third_review_operations_render_without_losing_context(action):
    model, _ = imported(layout_input())
    rule = model.pko[0]
    prop = next(p for p in rule.properties if p.name == "B")
    group = next(
        c for c in model.layouts if c.kind == "conditional" and c.owner_id == rule.logical_id
    )
    operations = {
        "create": ManagerOperation(
            "new",
            "property",
            "create",
            owner_id=rule.logical_id,
            container_id=group.logical_id,
            patch=PropertyPatch(configuration_property="New", format_property="New"),
        ),
        "move": ManagerOperation(
            "move", "property", "move", target_id=prop.logical_id, container_id=rule.logical_id
        ),
        "delete": ManagerOperation("delete", "property", "delete", target_id=prop.logical_id),
        "identify": ManagerOperation(
            "search",
            "identification",
            "update",
            target_id=rule.identification.logical_id,
            patch=IdentificationPatch(search_sets=(("New",), ("Code", "Name"))),
        ),
        "rename": ManagerOperation(
            "rename", "pko", "update", target_id=rule.logical_id, patch=PkoPatch(name="Renamed")
        ),
    }
    changed = execute(model, operations[action])
    for mode in ("preserve", "canonical"):
        output = render(changed, mode)
        _, back = reread(changed, output)
        assert canonicalize(changed) == canonicalize(back)
        assert render(back, mode).data == output.data


@pytest.mark.parametrize("version", [1, 2, 3])
def test_interface_signatures_and_runtime_mark(version):
    model = new_manager(interface_version=version)
    output = render(model, "canonical")
    document, back = reread(model, output)
    assert not document.unknown
    assert document.manager_version == version
    assert canonicalize(model) == canonicalize(back)
    assert output.report.exchange_verified == (version == 2)


@pytest.mark.parametrize("version", [1, 3])
def test_nonempty_interface_signature_matrix(version):
    model = pilot_model(version)
    output = render(model, "canonical")
    document, back = reread(model, output)
    assert document.manager_version == version and not document.unknown
    assert canonicalize(model) == canonicalize(back)
    assert render(back, "canonical").data == output.data


def test_invalid_mode_refused():
    with pytest.raises(ValueError, match="Режим"):
        render(new_manager(), cast(Any, "wrong"))


def test_retained_inline_comment_is_stored_once_and_positional_address_refused():
    # Пространство имён ссылочной ПКС остаётся сохранённой формой W4.
    model, _ = imported(layout_input().replace('0, "Other");', '0, "Other", "urn:w4"); // хвост R'))
    retained = next(p for p in model.pko[0].properties if p.state == "retained")
    element = next(
        e for c in model.layouts for e in c.elements if e.entity_id == retained.logical_id
    )
    assert not retained.trailing_comment and not element.trailing_comment
    assert sum("хвост R" in b.text for b in model.retained_blocks) == 1
    from kd2_rules_mcp.ed.canonical import model_addresses

    address = model_addresses(model)[model.pko[0].logical_id] + "#2"
    plan = preview(
        model,
        (
            ManagerOperation(
                "address", "pko", "update", address=address, patch=PkoPatch(name="New")
            ),
        ),
        expected_revision=model.revision,
    )
    assert plan.failures[0].reason == "unstable_address"
    assert "logical_id" in plan.failures[0].message


def test_all_direct_property_argument_presence_forms_roundtrip():
    model = pilot_model()
    rule = model.pko[0]
    for count in range(3, 7):
        for flags in product((False, True), repeat=count - 3):
            name = "P" + str(count) + "".join(str(int(f)) for f in flags)
            model = execute(
                model,
                ManagerOperation(
                    name,
                    "property",
                    "create",
                    owner_id=rule.logical_id,
                    container_id=rule.logical_id,
                    patch=PropertyPatch(
                        configuration_property=name,
                        format_property=name,
                        argument_presence=(True, True, True, *flags),
                    ),
                ),
            )
    output = render(model, "canonical")
    document, back = reread(model, output)
    assert not document.unknown
    assert canonicalize(model) == canonicalize(back)
    assert render(back, "canonical").data == output.data


def test_dispatcher_chain_is_generator_form_without_else_or_exception():
    from kd2_rules_mcp.ed.writer_forms import dispatcher, empty_module

    for function in (False, True):
        name = (
            "ВыполнитьФункциюМодуляМенеджера" if function else "ВыполнитьПроцедуруМодуляМенеджера"
        )
        text = dispatcher(name, function, (("A", ("Параметры",)), ("B", ("Параметры",))))
        assert text.count("КонецЕсли;") == 1
        assert "ИначеЕсли" in text and "Иначе\n" not in text
        assert "ВызватьИсключение" not in text
        assert text.count("Возврат ") == (2 if function else 0)
        module = empty_module().replace(dispatcher(name, function), text)
        assert not read_manager_text(module).unknown


def test_insert_after_retained_and_delete_reinsert_series_render_roundtrip():
    model, _ = imported(layout_input().replace('0, "Other");', '0, "Other", "urn:w4");'))
    rule = model.pko[0]
    retained = next(p for p in rule.properties if p.state == "retained")
    initial_texts = {b.logical_id: b.text for b in model.retained_blocks}
    previous = retained.logical_id
    for step, action in enumerate(("create", "delete", "create", "move", "delete")):
        if action == "create":
            op = ManagerOperation(
                str(step),
                "property",
                action,
                owner_id=rule.logical_id,
                container_id=rule.logical_id,
                after_id=previous,
                patch=PropertyPatch(configuration_property="X", format_property="X"),
            )
        else:
            target = next(p for p in model.pko[0].properties if p.name == "X")
            op = ManagerOperation(
                str(step),
                "property",
                action,
                target_id=target.logical_id,
                container_id=rule.logical_id if action == "move" else None,
            )
        model = execute(model, op)
        assert {b.logical_id: b.text for b in model.retained_blocks} == initial_texts
        for mode in ("preserve", "canonical"):
            _, back = reread(model, render(model, mode))
            assert canonicalize(model) == canonicalize(back)


def test_duplicate_property_addresses_follow_layout_after_move():
    text = layout_input().replace('"B", "B"', '"B", "A"')
    model, _ = imported(text)
    rule = model.pko[0]
    prop = next(p for p in rule.properties if p.configuration_property == "B")
    changed = execute(
        model,
        ManagerOperation(
            "move-duplicate",
            "property",
            "move",
            target_id=prop.logical_id,
            container_id=rule.logical_id,
        ),
    )
    for mode in ("preserve", "canonical"):
        _, back = reread(changed, render(changed, mode))
        assert canonicalize(changed) == canonicalize(back)


def test_new_rule_at_module_start_follows_header_and_bom_is_unique():
    model = new_manager()
    model = execute(
        model,
        ManagerOperation(
            "first",
            "pko",
            "create",
            container_id=model.root_layouts[0],
            patch=PkoPatch(name="First", directions=("send",)),
        ),
    )
    output = render(model)
    assert output.data.startswith(b"\xef\xbb\xbf")
    assert output.data.count(b"\xef\xbb\xbf") == 1
    assert output.text.index("КонецФункции") < output.text.index("Процедура ДобавитьПКО_First")
    _, back = reread(model, output)
    assert canonicalize(model) == canonicalize(back)


@pytest.mark.parametrize("title", [Value(), Value("string", ""), Value("string", "NewTitle")])
def test_manager_header_update_remove_and_add_roundtrip(title):
    model = new_manager()
    for n, value in enumerate((title, Value("string", "Restored"))):
        model = execute(
            model, ManagerOperation(str(n), "manager", "update", patch=ManagerPatch(title=value))
        )
        for mode in ("preserve", "canonical"):
            _, back = reread(model, render(model, mode))
            assert canonicalize(model) == canonicalize(back)


@pytest.mark.parametrize("join_at", ["name", "initializer", "closing"])
def test_shared_line_with_rule_frame_or_field_is_one_retained_leaf(join_at):
    text = layout_input()
    changes = {
        "name": (";\n    ПравилоКонвертации.ОбъектФормата", "; ПравилоКонвертации.ОбъектФормата"),
        "initializer": (";\n    ПравилоКонвертации.ИмяПКО", "; ПравилоКонвертации.ИмяПКО"),
    }
    if join_at == "closing":
        text = text.replace("// C3 конец тела\n", "").replace(
            "КонецЕсли;\nКонецПроцедуры", "КонецЕсли; КонецПроцедуры"
        )
    else:
        text = text.replace(*changes[join_at])
    model, _ = imported(text)
    assert model.pko[0].state == "retained" and model.pko[0].inside_leaf_id
    assert render(model).data == text.encode("utf-8")
    _, back = reread(model, render(model, "canonical"))
    assert canonicalize(model) == canonicalize(back)


def test_bom_style_update_and_opaque_line_endings_refusal():
    from dataclasses import replace

    model = new_manager()
    style = replace(model.header.text_style, bom=False)
    changed = execute(
        model, ManagerOperation("bom", "manager", "update", patch=ManagerPatch(text_style=style))
    )
    output = render(changed)
    assert not output.data.startswith(b"\xef\xbb\xbf")
    _, back = reread(changed, output)
    assert canonicalize(changed) == canonicalize(back)
    op = ManagerOperation(
        "line-endings",
        "manager",
        "update",
        patch=ManagerPatch(text_style=replace(style, newline="\n")),
    )
    assert (
        preview(model, (op,), expected_revision=model.revision).failures[0].reason
        == "opaque_context_changed"
    )


def test_module_start_is_after_version_even_when_version_follows_other_routines():
    from kd2_rules_mcp.ed.writer_forms import version

    source = render(new_manager()).text
    version_text = version(2).replace("\n", "\r\n")
    dispatcher = "Процедура ВыполнитьПроцедуруМодуляМенеджера"
    source = source.replace(version_text, "").replace(dispatcher, version_text + dispatcher)
    model, _ = imported(source)
    changed = execute(
        model,
        ManagerOperation(
            "begin-late-header",
            "pko",
            "create",
            container_id=model.root_layouts[0],
            patch=PkoPatch(name="First", directions=("send",)),
        ),
    )
    output = render(changed)
    assert output.text.index("КонецФункции") < output.text.index("Процедура ДобавитьПКО_First")
    _, back = reread(changed, output)
    assert canonicalize(changed) == canonicalize(back)


@pytest.mark.parametrize("directions", [("both",), ("receive", "send")])
def test_both_and_reversed_direction_forms_roundtrip(directions):
    model = pilot_model(directions=directions)
    for mode in ("preserve", "canonical"):
        _, back = reread(model, render(model, mode))
        assert canonicalize(model) == canonicalize(back)


@pytest.mark.parametrize(
    "following",
    ["", 'ДобавитьПКС(СвойстваШапки, "C", "C");', "// следующий комментарий", "#КонецОбласти"],
)
@pytest.mark.parametrize("newline", ["\n", "\r\n"])
@pytest.mark.parametrize("count", [2, 3])
def test_fourth_review_shared_line_does_not_consume_next_line(following, newline, count):
    from kd2_rules_mcp.ed.writer_forms import HELPER

    text = """Функция ВерсияФорматаМенеджераОбмена() Экспорт
    Возврат "2";
КонецФункции
Процедура ЗаполнитьПравилаКонвертацииОбъектов(НаправлениеОбмена, ПравилаКонвертации) Экспорт
    ДобавитьПКО_A(ПравилаКонвертации);
КонецПроцедуры
Процедура ДобавитьПКО_A(ПравилаКонвертации)
    ПравилоКонвертации = ОбменДаннымиXDTOСервер.ИнициализироватьПравилоКонвертацииОбъекта(
        ПравилаКонвертации);
    ПравилоКонвертации.ИмяПКО = "A";
    СвойстваШапки = ПравилоКонвертации.Свойства;
    ДобавитьПКС(СвойстваШапки, "A", "A"); ДобавитьПКС(СвойстваШапки, "B", "B");
"""
    if count == 3:
        text = text.rstrip("\n") + ' ДобавитьПКС(СвойстваШапки, "D", "D");\n'
    if following == "#КонецОбласти":
        text = text.replace("Процедура ДобавитьПКО_A", "#Область A\nПроцедура ДобавитьПКО_A")
        text += "КонецПроцедуры\n" + following + "\n"
    else:
        text += ("    " + following + "\n" if following else "") + "КонецПроцедуры\n"
    text = "\ufeff" + (text + HELPER).replace("\n", newline)
    model, report = imported(text)
    assert all(exact for _, _, _, exact in report.source_partition)
    shared = next(b for b in model.retained_blocks if b.kind == "shared_line")
    assert shared.text.count("ДобавитьПКС") == count
    assert 'C"' not in shared.text and "следующий" not in shared.text
    assert render(model).data == text.encode("utf-8")
    if following.startswith("ДобавитьПКС"):
        assert model.pko[0].properties[-1].state == "editable"


@pytest.mark.parametrize("mode", ["preserve", "canonical"])
@pytest.mark.parametrize("into_group", [True, False])
def test_fourth_review_moved_property_uses_destination_indent(mode, into_group):
    model, _ = imported(layout_input())
    rule = model.pko[0]
    group = next(
        c for c in model.layouts if c.kind == "conditional" and c.owner_id == rule.logical_id
    )
    prop = next(p for p in rule.properties if p.name == ("B" if into_group else "G1"))
    changed = execute(
        model,
        ManagerOperation(
            "depth",
            "property",
            "move",
            target_id=prop.logical_id,
            container_id=group.logical_id if into_group else rule.logical_id,
        ),
    )
    output = render(changed, mode)
    row = next(e for e in output.report.entries if e.leaf_id == prop.logical_id)
    line = output.data[row.byte_start : row.byte_end].decode("utf-8")
    assert line.startswith(
        next(s.indent for s in changed.module_styles if s.kind == "pko") * (2 if into_group else 1)
        + "ДобавитьПКС"
    )
    _, back = reread(changed, output)
    assert compare_models(changed, back).equal


@pytest.mark.parametrize("mode", ["preserve", "canonical"])
def test_fourth_review_rename_updates_editable_pod_leaves(mode):
    model = pilot_model()
    # Повторное чтение даёт настоящие исходные отрезки всех ссылок ПОД.
    _, model = reread(model, render(model))
    changed = execute(
        model,
        ManagerOperation(
            "rename",
            "pko",
            "update",
            target_id=model.pko[0].logical_id,
            patch=PkoPatch(name="Positions"),
        ),
    )
    assert all(ref.name == "Positions" for pod in changed.pod for ref in pod.used_pko)
    output = render(changed, mode)
    leaves = {e.logical_id for c in changed.layouts for e in c.elements if e.field == "used_pko"}
    assert all(e.state == "regenerated" for e in output.report.entries if e.leaf_id in leaves)
    _, back = reread(changed, output)
    assert compare_models(changed, back).equal


def test_fourth_review_new_manager_generator_sections_and_default_positions():
    model = pilot_model()
    output = render(model)
    for kind, area in (("pod", "ПОД"), ("pko", "ПКО")):
        start = output.text.index("#Область " + area + "\r\n")
        end = output.text.index("#КонецОбласти", start)
        assert all(
            start < output.text.index("Процедура " + r.procedure_name) < end
            for r in getattr(model, kind)
        )
    assert output.text.index("ПоляПоиска.Добавить") > output.text.index("ДобавитьПКС(СвойстваШапки")
    assert "КонецПроцедуры\r\nПроцедура Добавить" not in output.text
    assert compare_models(model, reread(model, output)[1]).equal


def test_fourth_review_default_rule_exits_singleton_but_stays_in_category():
    model = new_manager()
    model = execute(
        model,
        ManagerOperation("first", "pko", "create", patch=PkoPatch(name="A", directions=("send",))),
    )
    text = render(model).text.replace(
        "Процедура ДобавитьПКО_A", "#Область Single\r\nПроцедура ДобавитьПКО_A"
    )
    start = text.index("#Область Single")
    end = text.index("КонецПроцедуры", start) + len("КонецПроцедуры")
    text = text[:end] + "\r\n#КонецОбласти" + text[end:]
    model, _ = imported(text)
    changed = execute(
        model,
        ManagerOperation("second", "pko", "create", patch=PkoPatch(name="B", directions=("send",))),
    )
    output = render(changed)
    start = output.text.index("#Область Single")
    singleton_end = output.text.index("#КонецОбласти", start)
    category_end = output.text.index("#КонецОбласти", singleton_end + 1)
    assert singleton_end < output.text.index("Процедура ДобавитьПКО_B") < category_end
    assert compare_models(changed, reread(changed, output)[1]).equal


def test_fourth_review_interface_three_header_pass_returns_before_properties():
    from kd2_rules_mcp.ed.writer_forms import HEADERS_GUARD

    model = pilot_model(3)
    output = render(model)
    body = output.text[output.text.index("Процедура " + model.pko[0].procedure_name) :]
    guard = HEADERS_GUARD.replace("\n", "\r\n\t")
    assert (
        body.index(guard) < body.index("СвойстваШапки =") < body.index("ДобавитьПКС(СвойстваШапки")
    )
    # Порядок каркаса доказывает: ветка ТолькоЗаголовки достигает Возврат до любого вызова ПКС.
    assert body[: body.index("Возврат;")].count("ДобавитьПКС") == 0
    assert compare_models(model, reread(model, output)[1]).equal


def test_fourth_review_inconsistent_property_fields_are_rejected():
    from kd2_rules_mcp.ed.writer_model import validate_model

    model = pilot_model()
    rule = model.pko[0]
    prop = replace(rule.properties[0], configuration_property="Inconsistent")
    broken = replace(model, pko=(replace(rule, properties=(prop, *rule.properties[1:])),))
    with pytest.raises(ValueError, match="Аргументы ПКС"):
        validate_model(broken)


def test_fourth_review_compare_ignores_origin_and_direction_permutation():
    model = pilot_model(directions=("receive", "send"))
    assert model.pko[0].directions == ("send", "receive")
    assert PkoPatch(directions=("receive", "send")).directions == ("send", "receive")
    _, back = reread(model, render(model))
    assert compare_models(model, back).equal


def test_fourth_review_style_hints_are_optional_and_offsets_include_bom():
    from kd2_rules_mcp.ed.writer_forms import assignment_width

    model, _ = imported(layout_input())
    hinted = render(model, "canonical")
    default = render(model, "canonical", use_source_style=False)
    assert hinted.data != default.data
    assert not default.report.use_source_style
    assert assignment_width(("receive",), "name") == 53
    assert assignment_width(("send",), "name") == 36
    model = pilot_model()
    output = render(model)
    assert output.report.entries[0].byte_start == 3
    assert output.report.entries[-1].byte_end == len(output.data)


def test_fourth_review_consecutive_creates_after_rule_keep_creation_order():
    model = pilot_model()
    rule = model.pko[0]
    for name in ("First", "Second"):
        model = execute(
            model,
            ManagerOperation(
                name,
                "pod",
                "create",
                container_id=model.root_layouts[0],
                after_id=rule.logical_id,
                patch=PodPatch(name=name, directions=("send",)),
            ),
        )
    output = render(model)
    assert output.text.index("Процедура ДобавитьПОД_First") < output.text.index(
        "Процедура ДобавитьПОД_Second"
    )
    assert compare_models(model, reread(model, output)[1]).equal


def test_fourth_review_new_direction_extends_imported_chain():
    model = new_manager()
    model = execute(
        model,
        ManagerOperation("rule", "pko", "create", patch=PkoPatch(name="A", directions=("send",))),
    )
    _, model = reread(model, render(model))
    model = execute(
        model,
        ManagerOperation(
            "receive",
            "pko",
            "update",
            target_id=model.pko[0].logical_id,
            patch=PkoPatch(directions=("receive", "send")),
        ),
    )
    for mode in ("preserve", "canonical"):
        output = render(model, mode)
        assert 'ИначеЕсли НаправлениеОбмена = "Получение" Тогда' in output.text
        assert compare_models(model, reread(model, output)[1]).equal


def test_fourth_review_rename_refuses_retained_pod_reference():
    model = pilot_model()
    text = render(model).text.replace(
        "ПравилоОбработки.ИспользуемыеПКО.Добавить(",
        "НеизвестныйОператор();\r\n\tПравилоОбработки.ИспользуемыеПКО.Добавить(",
        1,
    )
    model, _ = imported(text)
    assert any(p.state == "retained" for p in model.pod)
    op = ManagerOperation(
        "rename", "pko", "update", target_id=model.pko[0].logical_id, patch=PkoPatch(name="Renamed")
    )
    plan = preview(model, (op,), expected_revision=model.revision)
    assert plan.failures[0].reason == "opaque_context_changed"
    assert any(address.startswith("ПОД/") for address in plan.failures[0].references)


def test_fourth_review_computed_notice_does_not_include_usage_keys():
    from kd2_rules_mcp.ed.writer_model import CodeUnit, Signature, text_hash

    model = pilot_model()
    units = tuple(
        CodeUnit(
            logical_id=name,
            name=name,
            signature=Signature(),
            body="",
            sha256=text_hash(""),
            dependencies=(Reference(kind, resolution="computed"),),
        )
        for name, kind in (("Lookup", "pko_lookup"), ("UsageKey", "pod_use"))
    )
    model = replace(model, code_units=(*model.code_units, *units)).with_revision()
    op = ManagerOperation(
        "rename", "pko", "update", target_id=model.pko[0].logical_id, patch=PkoPatch(name="Renamed")
    )
    plan = preview(model, (op,), expected_revision=model.revision)
    assert not plan.failures
    assert plan.notices[0].references == ("Код/Lookup",)


def test_fourth_review_default_extension_tail_is_generator_tab():
    model = pilot_model()
    text = render(model).text.replace(
        "\tСвойстваШапки =",
        "\tОбменДаннымиXDTOСервер.ИнициализироватьРасширениеПравилаКонвертацииОбъекта("
        'ПравилоКонвертации, "Ext");\t\r\n\tСвойстваШапки =',
    )
    model, _ = imported(text)
    assert model.pko[0].extensions == ("Ext",)
    for hints in (True, False):
        output = render(model, "canonical", use_source_style=hints)
        assert 'ПравилоКонвертации, "Ext");\t\r\n' in output.text
        assert compare_models(model, reread(model, output)[1]).equal


def test_fourth_review_ambiguous_editable_pod_reference_blocks_rename():
    model = pilot_model()
    pod = replace(
        model.pod[0], used_pko=(Reference("pko", name=model.pko[0].name, resolution="ambiguous"),)
    )
    model = replace(model, pod=(pod, *model.pod[1:])).with_revision()
    op = ManagerOperation(
        "rename", "pko", "update", target_id=model.pko[0].logical_id, patch=PkoPatch(name="Renamed")
    )
    plan = preview(model, (op,), expected_revision=model.revision)
    assert plan.failures[0].reason == "opaque_context_changed"
    assert plan.failures[0].references == ("ПОД/" + pod.name,)


def test_fourth_review_large_computed_notice_is_counted_and_requires_confirmation():
    from kd2_rules_mcp.ed.writer_model import CodeUnit, Signature, text_hash

    model = pilot_model()
    units = tuple(
        CodeUnit(
            logical_id=f"u{n}",
            name=f"Handler{n}",
            signature=Signature(),
            body="",
            sha256=text_hash(""),
            dependencies=(Reference("instruction_rule", resolution="computed"),),
        )
        for n in range(11)
    )
    model = replace(model, code_units=(*model.code_units, *units)).with_revision()
    op = ManagerOperation(
        "rename", "pko", "update", target_id=model.pko[0].logical_id, patch=PkoPatch(name="Renamed")
    )
    plan = preview(model, (op,), expected_revision=model.revision)
    assert not plan.failures and not plan.notices[0].references
    assert "11 обработчиков" in plan.notices[0].message
    with pytest.raises(ManagerOperationError, match="Проверьте обработчики"):
        apply(
            model, (op,), expected_revision=model.revision, expected_preview_hash=plan.preview_hash
        )


def test_fourth_review_previous_snapshot_hashes_and_replay_remain_valid():
    from kd2_rules_mcp.ed.writer_model import (
        Decision,
        SourceSlice,
        content_hash,
        digest,
        json_value,
    )

    # Отпечатки этих DTO получены из принятого B1 до появления новых полей.
    assert content_hash(SourceSlice("module", 0, 10)) == (
        "a01b3378b99365d9e5dc4aa137b54662f808033ee1f46459a7c017d4d48bf4d3"
    )
    assert content_hash(Decision("client", "a" * 64)) == (
        "31961853c5e5500e43dc8755c9bd19885fc8712bca416f3ddc30a76feed617cf"
    )
    model = pilot_model()
    op = ManagerOperation(
        "old-update", "pko", "update", target_id=model.pko[0].logical_id, patch=PkoPatch(name="A")
    )
    changed = execute(model, op)
    legacy = json_value(op)
    legacy.pop("position_mode")
    changed = replace(
        changed,
        decisions=tuple(
            replace(d, operation_hash=digest(legacy)) if d.client_id == op.client_id else d
            for d in changed.decisions
        ),
    ).with_revision()
    changed = load_model(dump_model(changed))
    plan = preview(changed, (op,), expected_revision=changed.revision)
    assert not plan.failures and plan.skipped == (op.client_id,)
    assert (
        apply(
            changed,
            (op,),
            expected_revision=changed.revision,
            expected_preview_hash=plan.preview_hash,
        )
        is changed
    )
    conflicting = replace(op, patch=PkoPatch(name="Different"))
    with pytest.raises(ManagerOperationError) as failure:
        apply(
            changed,
            (conflicting,),
            expected_revision=changed.revision,
            expected_preview_hash=plan.preview_hash,
        )
    assert failure.value.failures[0].address == "ПКО/A"


@pytest.mark.parametrize("mode", ["preserve", "canonical"])
def test_fourth_review_move_from_previous_snapshot_recovers_original_container(mode):
    model, _ = imported(layout_input())
    model = replace(
        model,
        layouts=tuple(
            replace(
                c,
                elements=tuple(
                    replace(e, source=replace(e.source, container_id="")) if e.source else e
                    for e in c.elements
                ),
            )
            for c in model.layouts
        ),
    ).with_revision()
    model = load_model(dump_model(model))
    prop = next(p for p in model.pko[0].properties if p.name == "B")
    group = next(
        c
        for c in model.layouts
        if c.kind == "conditional" and c.owner_id == model.pko[0].logical_id
    )
    changed = execute(
        model,
        ManagerOperation(
            "move-old", "property", "move", target_id=prop.logical_id, container_id=group.logical_id
        ),
    )
    output = render(changed, mode)
    row = next(e for e in output.report.entries if e.leaf_id == prop.logical_id)
    indent = next(s.indent for s in changed.module_styles if s.kind == "pko")
    assert (
        output.data[row.byte_start : row.byte_end]
        .decode("utf-8")
        .startswith(indent * 2 + "ДобавитьПКС")
    )
    assert compare_models(changed, reread(changed, output)[1]).equal
