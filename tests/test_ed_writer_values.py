"""Декларативные операции W2/D1 на собственных данных, без типовых модулей."""

from collections import Counter
from dataclasses import replace
from typing import Literal, cast

import pytest

from kd2_rules_mcp.authoring.ed.manager_operations import (
    ManagerOperation,
    ManagerOperationError,
    ParameterPatch,
    PkoPatch,
    PkpdPatch,
    PropertyPatch,
    ValueMappingPatch,
    apply,
    parse_operation,
    preview,
)
from kd2_rules_mcp.ed.canonical import canonicalize
from kd2_rules_mcp.ed.reader import read_manager_text
from kd2_rules_mcp.ed.writer import new_manager, render
from kd2_rules_mcp.ed.writer_import import import_manager
from kd2_rules_mcp.ed.writer_model import (
    Direction,
    Event,
    Reference,
    Value,
    dump_model,
    load_model,
    logical_id,
)
from kd2_rules_mcp.validation.ed_writer import validate_writer
from tests.test_ed_writer import execute


def commit_batch(model, operations):
    plan = preview(model, tuple(operations), expected_revision=model.revision)
    assert not plan.failures, plan.failures
    return apply(
        model,
        tuple(operations),
        expected_revision=model.revision,
        expected_preview_hash=plan.preview_hash,
        confirmations=tuple((n.code, n.notice_hash) for n in plan.notices),
    )


def value_model(direction: Direction = "both"):
    model = new_manager(project_id="values")
    return commit_batch(
        model,
        [
            ManagerOperation(
                "owner", "pko", "create", patch=PkoPatch(name="Owner", directions=(direction,))
            ),
            ManagerOperation(
                "target", "pko", "create", patch=PkoPatch(name="Target", directions=(direction,))
            ),
            ManagerOperation(
                "enum",
                "pkpd",
                "create",
                patch=PkpdPatch(
                    name="Colors",
                    directions=(direction,),
                    configuration_type=Value(
                        "reference", reference_parts=("Метаданные", "Перечисления", "Colors")
                    ),
                    format_type=Value("string", "Color"),
                ),
            ),
        ],
    )


def check_circle(model):
    assert canonicalize(load_model(dump_model(model))) == canonicalize(model)
    for mode in ("preserve", "canonical"):
        output = render(model, mode)
        back = import_manager(
            read_manager_text(output.data.decode("utf-8")), project_id=model.project_id
        )[0]
        assert canonicalize(model) == canonicalize(back)
        assert Counter(b.sha256 for b in model.retained_blocks) == Counter(
            b.sha256 for b in back.retained_blocks
        )
        assert render(back, mode).data == output.data


@pytest.mark.parametrize(
    "kind,flag,target",
    [
        ("direct", 0, None),
        ("reference", 0, "target"),
        ("pkpd", 0, "enum"),
        ("algorithm", 1, None),
        ("algorithm", 1, "target"),
        ("algorithm", 1, "enum"),
    ],
)
def test_property_all_kinds_crud_and_move(kind, flag, target):
    model = value_model()
    owner = model.pko[0]
    reference = (
        Reference(
            "pkpd" if target == "enum" else "pko",
            logical_id(model.project_id, target),
            "Colors" if target == "enum" else "Target",
            "resolved",
        )
        if target
        else Reference("conversion")
    )
    model = execute(
        model,
        ManagerOperation(
            "property",
            "property",
            "create",
            owner_id=owner.logical_id,
            patch=PropertyPatch(
                configuration_property="Color",
                format_property='Color"Text',
                property_kind=kind,
                algorithm_flag=flag,
                conversion=reference,
            ),
        ),
    )
    prop = model.pko[0].properties[0]
    assert len(prop.argument_presence) == (5 if target else 4 if flag else 3)
    model = execute(
        model,
        ManagerOperation(
            "edit",
            "property",
            "update",
            target_id=prop.logical_id,
            patch=PropertyPatch(format_property="Color"),
        ),
    )
    model = execute(
        model,
        ManagerOperation(
            "move", "property", "move", target_id=prop.logical_id, container_id=owner.logical_id
        ),
    )
    check_circle(model)
    model = execute(
        model, ManagerOperation("delete", "property", "delete", target_id=prop.logical_id)
    )
    assert not model.pko[0].properties


def test_kind_change_is_one_explicit_operation_and_references_resolve_after_batch():
    model = value_model()
    owner = model.pko[0]
    model = execute(
        model,
        ManagerOperation(
            "property",
            "property",
            "create",
            owner_id=owner.logical_id,
            patch=PropertyPatch(configuration_property="Color", format_property="Color"),
        ),
    )
    key = model.pko[0].properties[0].logical_id
    incomplete = ManagerOperation(
        "bad", "property", "update", target_id=key, patch=PropertyPatch(property_kind="algorithm")
    )
    assert preview(model, (incomplete,), expected_revision=model.revision).failures
    for n, (kind, flag, reference) in enumerate(
        [
            ("reference", 0, Reference("pko", name="Target")),
            ("pkpd", 0, Reference("pkpd", name="Colors")),
            ("algorithm", 1, Reference("conversion")),
            ("direct", 0, Reference("conversion")),
        ]
    ):
        model = execute(
            model,
            ManagerOperation(
                str(n),
                "property",
                "update",
                target_id=key,
                patch=PropertyPatch(
                    property_kind=cast(Literal["direct", "reference", "pkpd", "algorithm"], kind),
                    algorithm_flag=flag,
                    conversion=reference,
                ),
            ),
        )
        check_circle(model)
    future_id = logical_id(model.project_id, "future")
    operations = [
        ManagerOperation(
            "future-link",
            "property",
            "create",
            owner_id=owner.logical_id,
            patch=PropertyPatch(
                configuration_property="Future",
                format_property="Future",
                property_kind="reference",
                conversion=Reference("pko", future_id, "Future"),
            ),
        ),
        ManagerOperation(
            "future", "pko", "create", patch=PkoPatch(name="Future", directions=("both",))
        ),
    ]
    assert (
        preview(model, tuple(operations[:1]), expected_revision=model.revision).failures[0].reason
        == "dangling_reference"
    )
    check_circle(commit_batch(model, operations))


@pytest.mark.parametrize("direction", ["send", "receive"])
def test_wrong_reference_direction_refused(direction):
    model = value_model(direction)
    model = execute(
        model,
        ManagerOperation(
            "opposite",
            "pko",
            "create",
            patch=PkoPatch(
                name="Opposite", directions=("receive" if direction == "send" else "send",)
            ),
        ),
    )
    op = ManagerOperation(
        "link",
        "property",
        "create",
        owner_id=model.pko[0].logical_id,
        patch=PropertyPatch(
            configuration_property="Link",
            format_property="Link",
            property_kind="reference",
            conversion=Reference("pko", name="Opposite"),
        ),
    )
    plan = preview(model, (op,), expected_revision=model.revision)
    assert plan.failures[0].reason == "dangling_reference" and plan.model is model


@pytest.mark.parametrize("target", ["pko", "pkpd"])
def test_rename_updates_properties_and_delete_requires_same_batch(target):
    model = value_model()
    rule = model.pko[1] if target == "pko" else model.pkpd[0]
    model = execute(
        model,
        ManagerOperation(
            "link",
            "property",
            "create",
            owner_id=model.pko[0].logical_id,
            patch=PropertyPatch(
                configuration_property="Link",
                format_property="Link",
                property_kind="reference" if target == "pko" else "pkpd",
                conversion=Reference(target, rule.logical_id, rule.name, "resolved"),
            ),
        ),
    )
    model = execute(
        model,
        ManagerOperation(
            "rename",
            target,
            "update",
            target_id=rule.logical_id,
            patch=PkoPatch(name="NewName") if target == "pko" else PkpdPatch(name="NewName"),
        ),
    )
    assert model.pko[0].properties[0].conversion.name == "NewName"
    check_circle(model)
    delete = ManagerOperation("delete", target, "delete", target_id=rule.logical_id)
    plan = preview(model, (delete,), expected_revision=model.revision)
    assert plan.failures[0].reason == "dangling_reference" and plan.failures[0].references
    check_circle(
        commit_batch(
            model,
            [
                delete,
                ManagerOperation(
                    "delete-link",
                    "property",
                    "delete",
                    target_id=model.pko[0].properties[0].logical_id,
                ),
            ],
        )
    )


def mapping(model, client, value, side: Literal["send", "receive"] = "send", **position):
    return ManagerOperation(
        client,
        "value_mapping",
        "create",
        owner_id=model.pkpd[0].logical_id,
        patch=ValueMappingPatch(
            direction=side,
            configuration_value=Value(
                "reference", reference_parts=("Перечисления", "Colors", value)
            ),
            format_value=Value("string", value),
        ),
        **position,
    )


def test_mapping_crud_moves_and_duplicate_keys():
    model = value_model()
    model = commit_batch(
        model,
        [
            mapping(model, "red", "Red"),
            mapping(model, "blue", "Blue"),
            mapping(model, "receive", "Red", "receive"),
        ],
    )
    assert preview(
        model, (mapping(model, "duplicate-case", "red"),), expected_revision=model.revision
    ).failures
    red, blue, receive = model.pkpd[0].mappings
    assert preview(
        model, (mapping(model, "duplicate", "Red"),), expected_revision=model.revision
    ).failures
    model = execute(
        model,
        ManagerOperation(
            "move",
            "value_mapping",
            "move",
            target_id=blue.logical_id,
            container_id=next(
                c.logical_id for c in model.layouts if c.kind == "values" and c.direction == "send"
            ),
        ),
    )
    assert [m.configuration_value.reference_parts[-1] for m in model.pkpd[0].mappings] == [
        "Blue",
        "Red",
        "Red",
    ]
    model = execute(
        model,
        ManagerOperation(
            "edit",
            "value_mapping",
            "update",
            target_id=receive.logical_id,
            patch=ValueMappingPatch(format_value=Value("string", 'Text"Value')),
        ),
    )
    check_circle(model)
    model = execute(
        model, ManagerOperation("delete", "value_mapping", "delete", target_id=red.logical_id)
    )
    check_circle(model)
    op = ManagerOperation(
        "bad-value",
        "value_mapping",
        "update",
        target_id=blue.logical_id,
        patch=ValueMappingPatch(
            configuration_value=Value(
                "reference", reference_parts=("Перечисления", "Other", "Blue")
            )
        ),
    )
    assert preview(model, (op,), expected_revision=model.revision).failures


@pytest.mark.parametrize(
    "default",
    [
        Value(),
        Value("undefined"),
        Value("string", 'a"b\nc'),
        Value("number", -12.5),
        Value("number", 1e20),
        Value("number", 1e-9),
        Value("boolean", False),
        Value("date", "20261005000000"),
        Value("reference", reference_parts=("Перечисления", "Colors", "Red")),
    ],
)
def test_parameter_typed_defaults_and_crud(default):
    model = execute(
        new_manager(),
        ManagerOperation(
            "p", "parameter", "create", patch=ParameterPatch(name="Option", default=default)
        ),
    )
    assert model.parameters[0].default_source == (
        "implicit" if default.state == "unset" else "explicit"
    )
    check_circle(model)
    model = execute(
        model,
        ManagerOperation(
            "rename",
            "parameter",
            "update",
            target_id=model.parameters[0].logical_id,
            patch=ParameterPatch(name="Renamed"),
        ),
    )
    model = execute(
        model,
        ManagerOperation(
            "clear",
            "parameter",
            "update",
            target_id=model.parameters[0].logical_id,
            clear=("default",),
        ),
    )
    assert model.parameters[0].default_source == "implicit"
    model = execute(
        model,
        ManagerOperation("delete", "parameter", "delete", target_id=model.parameters[0].logical_id),
    )
    assert not model.parameters
    check_circle(model)


def test_closed_dtos_and_property_insertion_does_not_sort_existing_rows():
    with pytest.raises(ValueError):
        parse_operation(
            {"client_id": "x", "kind": "pkpd", "action": "create", "patch": {"unknown": True}}
        )
    model = value_model()
    owner = model.pko[0]
    for name in ("Z", "A"):
        model = execute(
            model,
            ManagerOperation(
                name,
                "property",
                "create",
                owner_id=owner.logical_id,
                container_id=owner.logical_id,
                after_id=model.pko[0].properties[-1].logical_id
                if model.pko[0].properties
                else None,
                patch=PropertyPatch(configuration_property=name, format_property=name),
            ),
        )
    model = execute(
        model,
        ManagerOperation(
            "middle",
            "property",
            "create",
            owner_id=owner.logical_id,
            patch=PropertyPatch(configuration_property="M", format_property="M"),
        ),
    )
    ids = model.ordered_entity_ids(owner.logical_id)
    names = {p.logical_id: p.name for p in model.pko[0].properties}
    assert [names[k] for k in ids if k in names] == ["Z", "A", "M"]


def test_pkpd_literal_in_code_requires_confirmation_and_is_not_replaced():
    model = value_model()
    text = render(model).text + '\nПроцедура Business()\n\tСообщить("Colors");\nКонецПроцедуры\n'
    model = import_manager(read_manager_text(text), project_id=model.project_id)[0]
    op = ManagerOperation(
        "rename",
        "pkpd",
        "update",
        target_id=model.pkpd[0].logical_id,
        patch=PkpdPatch(name="Renamed"),
    )
    plan = preview(model, (op,), expected_revision=model.revision)
    assert not plan.failures and plan.notices
    with pytest.raises(ManagerOperationError):
        apply(
            model, (op,), expected_revision=model.revision, expected_preview_hash=plan.preview_hash
        )
    changed = commit_batch(model, [op])
    assert changed.code_units == model.code_units
    assert 'Сообщить("Colors")' in render(changed).text


def test_predefined_import_excludes_direction_closing_statement():
    text = render(value_model("send")).text
    document = read_manager_text(text)
    assert "КонецЕсли" in document.pkpd[0].raw_text
    model = import_manager(document, project_id="boundary")[0]
    rule = model.pkpd[0]
    container = next(c for c in model.layouts if c.logical_id == rule.logical_id)
    assert container.opening
    source = model.source_files[0].text
    assert "КонецЕсли" not in source[container.opening.char_start : container.opening.char_end]
    assert render(model, "preserve").text == text
    check_circle(model)


@pytest.mark.parametrize("direction", ["send", "receive"])
def test_declarative_warnings_are_per_direction_and_property(direction):
    model = value_model(direction)
    model = execute(
        model,
        ManagerOperation(
            "algorithm",
            "property",
            "create",
            owner_id=model.pko[0].logical_id,
            patch=PropertyPatch(
                configuration_property="" if direction == "send" else "Color",
                format_property="Color" if direction == "send" else "",
                property_kind="algorithm",
                algorithm_flag=1,
                conversion=Reference("pkpd", name="Colors"),
            ),
        ),
    )
    report = validate_writer(model, render(model).data)
    issues = [
        i
        for i in report.issues
        if i.check in ("ed.writer.algorithm_handler", "ed.writer.pkpd_direction")
    ]
    assert len(issues) == 2
    assert all(i.address.endswith("/ПКС/Color") and direction in i.message for i in issues)
    assert all("XDTO:" in i.message for i in issues)
    event = "ПриОтправкеДанных" if direction == "send" else "ПриКонвертацииДанныхXDTO"
    owner = replace(
        model.pko[0],
        events=(
            Event(
                logical_id="event",
                name=event,
                event=event,
                target=Reference("code", name="Handler"),
            ),
        ),
    )
    model = replace(model, pko=(owner, *model.pko[1:])).with_revision()
    assert not any(
        i.check == "ed.writer.algorithm_handler"
        for i in validate_writer(model, render(model).data).issues
    )


def test_pkpd_type_update_checks_existing_pairs_after_entire_batch():
    model = value_model("send")
    model = execute(
        model,
        ManagerOperation(
            "mapping",
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
        ),
    )
    operation = ManagerOperation(
        "type",
        "pkpd",
        "update",
        target_id=model.pkpd[0].logical_id,
        patch=PkpdPatch(
            configuration_type=Value(
                "reference", reference_parts=("Метаданные", "Перечисления", "NewColors")
            )
        ),
    )
    assert preview(model, (operation,), expected_revision=model.revision).failures
    updated = commit_batch(
        model,
        [
            operation,
            ManagerOperation(
                "value",
                "value_mapping",
                "update",
                target_id=model.pkpd[0].mappings[0].logical_id,
                patch=ValueMappingPatch(
                    configuration_value=Value(
                        "reference", reference_parts=("Перечисления", "NewColors", "Red")
                    )
                ),
            ),
        ],
    )
    check_circle(updated)


def test_default_property_position_does_not_enter_direction_branch():
    model = value_model()
    model = execute(
        model,
        ManagerOperation(
            "guarded",
            "property",
            "create",
            owner_id=model.pko[0].logical_id,
            patch=PropertyPatch(configuration_property="Z", format_property="Z"),
        ),
    )
    text = render(model).text.replace("\r\n", "\n")
    line = next(line for line in text.splitlines() if line.startswith("\tДобавитьПКС("))
    text = text.replace(
        line + "\n",
        '\tЕсли НаправлениеОбмена = "Отправка" Тогда\n\t' + line + "\n\tКонецЕсли;\n",
        1,
    )
    model = import_manager(read_manager_text(text), project_id=model.project_id)[0]
    owner = next(r for r in model.pko if r.name == "Owner")
    assert owner.properties[0].guards
    changed = execute(
        model,
        ManagerOperation(
            "common",
            "property",
            "create",
            owner_id=owner.logical_id,
            patch=PropertyPatch(configuration_property="M", format_property="M"),
        ),
    )
    owner = next(r for r in changed.pko if r.name == "Owner")
    assert not next(p for p in owner.properties if p.name == "M").guards
    check_circle(changed)


def test_default_property_create_in_retained_rule_returns_refusal():
    model = value_model()
    text = render(model).text.replace("\r\n", "\n")
    line = next(
        line for line in text.splitlines() if line.startswith("Процедура ДобавитьПКО_Owner(")
    )
    text = text.replace(line + "\n", line + '\n\tСообщить("opaque");\n', 1)
    model = import_manager(read_manager_text(text), project_id=model.project_id)[0]
    owner = next(r for r in model.pko if r.name == "Owner")
    assert owner.state == "retained"
    operation = ManagerOperation(
        "refused",
        "property",
        "create",
        owner_id=owner.logical_id,
        patch=PropertyPatch(configuration_property="X", format_property="X"),
    )
    plan = preview(model, (operation,), expected_revision=model.revision)
    assert plan.model is model and not plan.changes
    assert plan.failures[0].reason == "opaque_context_changed"


def test_unverified_helper_does_not_prove_algorithm_warning():
    model = execute(
        value_model("send"),
        ManagerOperation(
            "algorithm",
            "property",
            "create",
            owner_id=value_model("send").pko[0].logical_id,
            patch=PropertyPatch(
                configuration_property="Color",
                format_property="Color",
                property_kind="algorithm",
                algorithm_flag=1,
            ),
        ),
    )
    text = render(model).text.replace("\r\n", "\n")
    start = text.index("Процедура ДобавитьПКС(")
    end = text.index("\nКонецПроцедуры", start) + len("\nКонецПроцедуры")
    text = (
        text[:start]
        + (
            'Процедура ДобавитьПКС(ТаблицаПКС, Имя1 = "", Имя2 = "", '
            'Алгоритм = 0, Правило = "")\nКонецПроцедуры'
        )
        + text[end:]
    )
    model = import_manager(read_manager_text(text), project_id=model.project_id)[0]
    assert model.pko[0].properties[0].state == "retained"
    report = validate_writer(model, render(model).data)
    assert any(i.check == "ed.writer.incomplete" for i in report.issues)
    assert not any(i.check == "ed.writer.algorithm_handler" for i in report.issues)


@pytest.mark.parametrize("incomplete", [True, False])
def test_incomplete_or_custom_predefined_body_is_retained(incomplete):
    text = render(value_model()).text.replace("\r\n", "\n")
    if incomplete:
        text = "\n".join(line for line in text.split("\n") if ".КонвертацииЗначенийПри" not in line)
    else:
        line = next(line for line in text.splitlines() if ".ИмяПКПД" in line)
        text = text.replace(line + "\n", line + '\n\tСообщить("custom");\n', 1)
    model, report = import_manager(read_manager_text(text), project_id="partial")
    assert model.pkpd[0].state == "retained"
    assert (
        next(e for e in report.entries if e.logical_id == model.pkpd[0].logical_id).reason
        == "unsupported_predefined_form"
    )
    assert render(model, "preserve").text == text
    check_circle(model)


@pytest.mark.parametrize("kind", ["pko", "pkpd"])
def test_name_reference_created_and_target_renamed_in_one_batch(kind):
    model = value_model()
    target = model.pko[1] if kind == "pko" else model.pkpd[0]
    changed = commit_batch(
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
                    property_kind="reference" if kind == "pko" else "pkpd",
                    conversion=Reference(kind, name=target.name),
                ),
            ),
            ManagerOperation(
                "rename",
                kind,
                "update",
                target_id=target.logical_id,
                patch=PkoPatch(name="Renamed") if kind == "pko" else PkpdPatch(name="Renamed"),
            ),
        ],
    )
    assert changed.pko[0].properties[0].conversion.name == "Renamed"
    assert changed.pko[0].properties[0].conversion.target_id == target.logical_id
    check_circle(changed)


@pytest.mark.parametrize("use_id", [False, True])
def test_ambiguous_rule_name_cannot_be_disambiguated_by_dto_kind(use_id):
    model = value_model()
    text = render(model).text.replace('"Target"', '"Colors"')
    model = import_manager(read_manager_text(text), project_id=model.project_id)[0]
    operation = ManagerOperation(
        "link",
        "property",
        "create",
        owner_id=model.pko[0].logical_id,
        patch=PropertyPatch(
            configuration_property="Color",
            format_property="Color",
            property_kind="pkpd",
            conversion=Reference(
                "pkpd", model.pkpd[0].logical_id if use_id else None, name="Colors"
            ),
        ),
    )
    plan = preview(model, (operation,), expected_revision=model.revision)
    assert plan.model is model and plan.failures[0].reason == "dangling_reference"
