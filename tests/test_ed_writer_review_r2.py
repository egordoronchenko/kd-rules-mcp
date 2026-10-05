"""Повторное ревью D1 и пакеты пользователя: только синтетические данные."""

import re
from dataclasses import replace

import pytest

from kd2_rules_mcp.authoring.ed.manager_operations import (
    HandlerPatch,
    ManagerOperation,
    ManagerOperationError,
    ParameterPatch,
    PkoPatch,
    PkpdPatch,
    PodPatch,
    apply,
    parse_operation,
    preview,
)
from kd2_rules_mcp.ed.canonical import canonicalize, model_addresses
from kd2_rules_mcp.ed.writer import new_manager, render
from kd2_rules_mcp.ed.writer_import import parameter_accesses
from kd2_rules_mcp.ed.writer_model import logical_id
from tests.test_ed_writer_review import checks, imported, plan
from tests.test_ed_writer_values import commit_batch, value_model
from tests.test_service_ed_writer import writer_setup as writer_setup


def code_reference_model(name="Target", event="ПриОтправкеДанных"):
    model = value_model()
    model = commit_batch(
        model,
        [
            ManagerOperation(
                "handler",
                "handler",
                "create",
                owner_id=model.pko[0].logical_id,
                patch=HandlerPatch(
                    event=event,
                    body='ДанныеXDTO.Вставить("Calc", Новый Структура('
                    f'"ИмяПКО, Значение", "{name}", 1));',
                ),
            )
        ],
    )
    return imported(render(model).text)


@pytest.mark.parametrize("kind,name", [("pko", "Target"), ("pkpd", "Colors")])
def test_r1_code_instruction_checks_direction(kind, name):
    model = code_reference_model(name)
    target = next(r for r in getattr(model, kind) if r.name == name)
    patch = (
        PkoPatch(directions=("receive",)) if kind == "pko" else PkpdPatch(directions=("receive",))
    )
    result = plan(
        model, ManagerOperation("narrow", kind, "update", target_id=target.logical_id, patch=patch)
    )
    assert result.failures and any(
        re.search(r":\d+$", r) for f in result.failures for r in f.references
    )
    bad = replace(
        model,
        **{
            kind: tuple(
                replace(r, directions=("receive",)) if r.logical_id == target.logical_id else r
                for r in getattr(model, kind)
            )
        },
    ).with_revision()
    assert any(
        i.check == "ed.writer.reference_direction" and "Код/" in i.address for i in checks(bad)
    )


def test_r1_event_direction_is_not_both_owner_directions():
    model = code_reference_model()
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
    assert not result.failures


def test_r1_preexisting_code_case_mismatch_remains_warning():
    model = code_reference_model("target")
    assert any(i.check == "ed.writer.reference_case_mismatch" for i in checks(model))
    target = next(r for r in model.pko if r.name == "Target")
    operation = ManagerOperation(
        "narrow",
        "pko",
        "update",
        target_id=target.logical_id,
        patch=PkoPatch(directions=("receive",)),
    )
    changed = commit_batch(model, [operation])
    assert next(u.body for u in changed.code_units if "handler" in u.roles) == next(
        u.body for u in model.code_units if "handler" in u.roles
    )


def test_r1_unknown_event_direction_requires_manual_confirmation():
    model = code_reference_model()
    owner = model.pko[0]
    model = replace(
        model,
        pko=tuple(
            replace(r, events=tuple(replace(e, event="РасширенноеСобытие") for e in r.events))
            if r.logical_id == owner.logical_id
            else r
            for r in model.pko
        ),
    ).with_revision()
    target = next(r for r in model.pko if r.name == "Target")
    result = plan(
        model,
        ManagerOperation(
            "narrow",
            "pko",
            "update",
            target_id=target.logical_id,
            patch=PkoPatch(directions=("receive",)),
        ),
    )
    assert not result.failures
    assert any(n.code == "computed_dependencies" and n.references for n in result.notices)


def test_p2_optional_null_reference_patch_keeps_previous_contract():
    operation = parse_operation(
        {
            "client_id": "nullable",
            "kind": "pod",
            "action": "create",
            "patch": {"name": "Load", "directions": ["send"], "used_pko": None},
        }
    )
    assert isinstance(operation.patch, PodPatch) and operation.patch.used_pko is None


@pytest.mark.parametrize("deleted_direction,refused", [("send", True), ("receive", False)])
def test_r1_code_instruction_delete_one_directional_name(deleted_direction, refused):
    model = code_reference_model()
    target = next(r for r in model.pko if r.name == "Target")
    model = commit_batch(
        model,
        [
            ManagerOperation(
                "narrow",
                "pko",
                "update",
                target_id=target.logical_id,
                patch=PkoPatch(directions=("send",)),
            ),
            ManagerOperation(
                "incoming",
                "pko",
                "create",
                patch=PkoPatch(name="Incoming", directions=("receive",)),
            ),
        ],
    )
    text = render(model).text
    text = text.replace(' = "Incoming";', ' = "Target";')
    model = imported(text)
    target = next(r for r in model.pko if deleted_direction in r.directions and r.name == "Target")
    result = plan(model, ManagerOperation("delete", "pko", "delete", target_id=target.logical_id))
    assert bool(result.failures) == refused
    if refused:
        assert any("Код/" in ref for f in result.failures for ref in f.references)


def test_r1_pod_handler_direction_change_validates_code():
    model = value_model("send")
    model = commit_batch(
        model,
        [
            ManagerOperation(
                "pod", "pod", "create", patch=PodPatch(name="Process", directions=("send",))
            )
        ],
    )
    model = commit_batch(
        model,
        [
            ManagerOperation(
                "handler",
                "handler",
                "create",
                owner_id=model.pod[0].logical_id,
                patch=HandlerPatch(
                    event="ПриОбработке",
                    body='X = Новый Структура("ИмяПКО, Значение", "Target", 1);',
                ),
            )
        ],
    )
    result = plan(
        model,
        ManagerOperation(
            "move",
            "pod",
            "update",
            target_id=model.pod[0].logical_id,
            patch=PodPatch(directions=("receive",)),
        ),
    )
    assert result.failures and any("Код/" in ref for f in result.failures for ref in f.references)


@pytest.mark.parametrize("branch", ["if", "else", "elseif", "nested"])
def test_r1_direction_guard_propagates_to_helper_and_reference(branch):
    model = value_model("receive")
    call = "Helper(КомпонентыОбмена);"
    if branch == "if":
        body = f'Если КомпонентыОбмена.НаправлениеОбмена = "Получение" Тогда\n{call}\nКонецЕсли;'
    elif branch == "else":
        body = (
            'Если КомпонентыОбмена.НаправлениеОбмена = "Отправка" Тогда\n'
            f"X = 1;\nИначе\n{call}\nКонецЕсли;"
        )
    elif branch == "elseif":
        body = (
            "Если ДругоеУсловие Тогда\nX = 1;\n"
            'ИначеЕсли КомпонентыОбмена.НаправлениеОбмена = "Получение" Тогда\n'
            f"{call}\nКонецЕсли;"
        )
    else:
        body = (
            'Если КомпонентыОбмена.НаправлениеОбмена = "Получение" Тогда\n'
            f"Если ДругоеУсловие Тогда\n{call}\nКонецЕсли;\nКонецЕсли;"
        )
    text = render(model).text.replace(
        "Процедура ПередКонвертацией(КомпонентыОбмена) Экспорт",
        "Процедура ПередКонвертацией(КомпонентыОбмена) Экспорт\n" + body,
    )
    text += (
        "\nПроцедура Helper(КомпонентыОбмена)\n"
        'X = ОбменДаннымиXDTOСервер.ПКОПоИмени(КомпонентыОбмена, "Target");\n'
        "КонецПроцедуры\n"
    )
    model = imported(text)
    assert not any(i.check == "ed.writer.reference_direction" for i in checks(model))
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
    assert result.failures


@pytest.mark.parametrize(
    "body",
    [
        "П = КомпонентыОбмена.ПараметрыКонвертации;\nЗначение = П.Alpha;",
        "Вызвать(ПараметрыКонвертации);",
        "Для Каждого П Из КомпонентыОбмена.ПараметрыКонвертации Цикл\nКонецЦикла;",
        "ПараметрыКонвертации",
    ],
)
@pytest.mark.parametrize("action", ["delete", "update"])
def test_r2_parameter_structure_escape_requires_confirmation(body, action):
    model = value_model()
    model = commit_batch(
        model,
        [ManagerOperation("alpha", "parameter", "create", patch=ParameterPatch(name="Alpha"))],
    )
    model = imported(render(model).text + "\nПроцедура Business()\n" + body + "\nКонецПроцедуры\n")
    parameter = model.parameters[0]
    operation = ManagerOperation(
        "change",
        "parameter",
        action,
        target_id=parameter.logical_id,
        patch=ParameterPatch(name="Omega") if action == "update" else None,
    )
    result = plan(model, operation)
    assert not result.failures
    assert any(
        n.code == "computed_dependencies" and any(re.search(r":\d+$", r) for r in n.references)
        for n in result.notices
    )
    with pytest.raises(ManagerOperationError, match="вычисляемые"):
        apply(
            model,
            (operation,),
            expected_revision=model.revision,
            expected_preview_hash=result.preview_hash,
        )


def test_r2_direct_access_and_comments_are_not_structure_escape():
    assert parameter_accesses("X = ПараметрыКонвертации.Alpha; // ПараметрыКонвертации") == (
        ("Alpha", 1),
    )


@pytest.mark.parametrize("mode", ["preserve", "canonical"])
def test_mr1_predefined_header_and_mapping_open_comments(mode):
    model = value_model()
    text = render(model).text.replace("\r\n", "\n")
    text = text.replace(' = "Colors";\n', ' = "Colors"; // имя\n\t// строка в шапке\n', 1)
    text = text.replace(
        "ЗначенияДляПолучения = Новый Соответствие;",
        "ЗначенияДляПолучения = Новый Соответствие; // карта получения",
    )
    model = imported(text)
    target = model.pkpd[0]
    changed = commit_batch(
        model,
        [
            ManagerOperation(
                "rename",
                "pkpd",
                "update",
                target_id=target.logical_id,
                patch=PkpdPatch(name="Paints"),
            )
        ],
    )
    for current in (model, changed):
        output = render(current, mode)
        for marker in ("// имя", "// строка в шапке", "// карта получения"):
            assert marker in output.text
        back = imported(output.text)
        assert canonicalize(back) == canonicalize(current)
        assert render(back, mode).data == output.data


def pko_create(client="owner", name="Owner", **extra):
    return {
        "client_id": client,
        "kind": "pko",
        "action": "create",
        "patch": {"name": name, "directions": ["both"], **extra},
    }


@pytest.mark.parametrize("bad", [False, True])
def test_p1_preview_apply_resolve_incremental_addresses(bad):
    model = new_manager(project_id="protocol")
    operations = tuple(
        parse_operation(p)
        for p in [
            pko_create(),
            {
                "client_id": "identify",
                "kind": "identification",
                "action": "update",
                "address": "ПКО/Owner/Идентификация" if not bad else "ПКО/Missing/Идентификация",
                "patch": {
                    "mode": {"state": "string", "value": "ПоПолямПоиска"},
                    "search_sets": [["Code"]],
                },
            },
        ]
    )
    planned = preview(model, operations, expected_revision=model.revision)
    if bad:
        with pytest.raises(ManagerOperationError) as error:
            apply(
                model,
                operations,
                expected_revision=model.revision,
                expected_preview_hash=planned.preview_hash,
            )
        assert error.value.failures == planned.failures
    else:
        result = apply(
            model,
            operations,
            expected_revision=model.revision,
            expected_preview_hash=planned.preview_hash,
        )
        assert result == planned.model
        assert result.pko[0].identification.search_sets[0].fields == ("Code",)


def test_p2_all_packet_client_references_and_replay():
    model = new_manager(project_id="protocol")
    payloads = [
        pko_create(),
        pko_create("target", "Target"),
        {
            "client_id": "a",
            "kind": "property",
            "action": "create",
            "owner_id": {"client_id": "owner"},
            "container_id": {"client_id": "owner"},
            "patch": {
                "configuration_property": "A",
                "format_property": "A",
                "property_kind": "reference",
                "conversion": {"kind": "pko", "target_id": {"client_id": "target"}},
            },
        },
        {
            "client_id": "b",
            "kind": "property",
            "action": "create",
            "owner_id": {"client_id": "owner"},
            "after_id": {"client_id": "a"},
            "patch": {"configuration_property": "B", "format_property": "B"},
        },
        {
            "client_id": "update",
            "kind": "property",
            "action": "update",
            "target_id": {"client_id": "b"},
            "patch": {"format_property": "C"},
        },
        {
            "client_id": "pod",
            "kind": "pod",
            "action": "create",
            "patch": {
                "name": "Load",
                "directions": ["both"],
                "used_pko": [{"target_id": {"client_id": "owner"}}],
            },
        },
    ]
    operations = tuple(map(parse_operation, payloads))
    result = commit_batch(model, operations)
    assert len(result.decisions) == len(payloads)
    owner = next(r for r in result.pko if r.name == "Owner")
    assert [p.format_property for p in owner.properties] == ["A", "C"]
    assert owner.properties[0].conversion.name == "Target"
    assert result.pod[0].used_pko[0].name == "Owner"
    planned = preview(model, operations, expected_revision=model.revision)
    assert (
        apply(
            result,
            operations,
            expected_revision=model.revision,
            expected_preview_hash=planned.preview_hash,
        )
        == result
    )


@pytest.mark.parametrize(
    "field", ["owner_id", "container_id", "after_id", "target_id", "conversion", "used_pko"]
)
def test_p2_unknown_and_forward_client_reference_identifies_operation(field):
    model = new_manager(project_id="protocol")
    payload = {
        "client_id": "broken",
        "kind": "property",
        "action": "create",
        "owner_id": logical_id(model.project_id, "owner"),
        "patch": {"configuration_property": "A", "format_property": "A"},
    }
    reference = {"client_id": "later"}
    if field == "conversion":
        payload["patch"][field] = {"target_id": reference}
    elif field == "used_pko":
        payload = {
            "client_id": "broken",
            "kind": "pod",
            "action": "create",
            "patch": {
                "name": "Load",
                "directions": ["send"],
                "used_pko": [{"target_id": reference}],
            },
        }
    else:
        payload[field] = reference
        if field == "target_id":
            payload["action"] = "update"
    operations = tuple(map(parse_operation, [pko_create(), payload, pko_create("later", "Later")]))
    result = preview(model, operations, expected_revision=model.revision)
    assert result.failures and any(
        "broken" in f.address + f.message and "later" in f.message for f in result.failures
    )
    assert len(result.operations) == len(operations)
    assert result.model == model


def test_p2_wrong_owner_does_not_disappear_from_count():
    model = new_manager(project_id="protocol")
    operations = tuple(
        map(
            parse_operation,
            [
                pko_create(),
                {
                    "client_id": "broken",
                    "kind": "property",
                    "action": "create",
                    "owner_id": "wrong",
                    "patch": {"configuration_property": "A", "format_property": "A"},
                },
            ],
        )
    )
    result = preview(model, operations, expected_revision=model.revision)
    assert result.failures and "broken" in result.failures[0].address + result.failures[0].message
    assert len(result.operations) == 2 and not result.changes and result.model == model


def test_p3_omitted_after_appends_explicit_null_prepends():
    model = new_manager(project_id="protocol")
    model = commit_batch(
        model, tuple(map(parse_operation, [pko_create("z", "Zulu"), pko_create("a", "Alpha")]))
    )
    text = render(model).text
    assert text.index("Процедура ДобавитьПКО_Zulu") < text.index("Процедура ДобавитьПКО_Alpha")
    owner = model.pko[0]
    payloads = [
        {
            "client_id": name,
            "kind": "property",
            "action": "create",
            "owner_id": owner.logical_id,
            "patch": {"configuration_property": name, "format_property": name},
        }
        for name in ("Z", "A", "First")
    ]
    payloads[-1]["after_id"] = None
    model = commit_batch(model, tuple(map(parse_operation, payloads)))
    text = render(model).text
    positions = []
    for name in ("First", "Z", "A"):
        match = re.search(f'"{name}"\\s*,\\s*"{name}"', text)
        assert match
        positions.append(match.start())
    assert positions == sorted(positions)


def test_p4_identification_on_create_matches_separate_update():
    model = new_manager(project_id="protocol")
    identification = {
        "mode": {"state": "string", "value": "ПоПолямПоиска"},
        "search_sets": [["Code"], ["Name", "Parent"]],
    }
    together = commit_batch(model, [parse_operation(pko_create(identification=identification))])
    separate = commit_batch(model, [parse_operation(pko_create())])
    separate = commit_batch(
        separate,
        [
            parse_operation(
                {
                    "client_id": "identify",
                    "kind": "identification",
                    "action": "update",
                    "address": "ПКО/Owner/Идентификация",
                    "patch": identification,
                }
            )
        ],
    )
    assert canonicalize(together) == canonicalize(separate)
    assert (
        model_addresses(together)[together.pko[0].identification.logical_id]
        == "ПКО/Owner/Идентификация"
    )
    assert render(imported(render(together).text)).text == render(together).text


def test_p2_reference_follows_later_rename_and_replays():
    model = new_manager(project_id="protocol")
    operations = tuple(
        map(
            parse_operation,
            [
                pko_create(),
                pko_create("target", "Target"),
                {
                    "client_id": "link",
                    "kind": "property",
                    "action": "create",
                    "owner_id": {"client_id": "owner"},
                    "patch": {
                        "configuration_property": "Link",
                        "format_property": "Link",
                        "property_kind": "reference",
                        "conversion": {"client_id": "target"},
                    },
                },
                {
                    "client_id": "pod",
                    "kind": "pod",
                    "action": "create",
                    "patch": {
                        "name": "Load",
                        "directions": ["both"],
                        "used_pko": [{"client_id": "target"}],
                    },
                },
                {
                    "client_id": "rename",
                    "kind": "pko",
                    "action": "update",
                    "target_id": {"client_id": "target"},
                    "patch": {"name": "Renamed"},
                },
            ],
        )
    )
    planned = preview(model, operations, expected_revision=model.revision)
    assert not planned.failures
    result = apply(
        model,
        operations,
        expected_revision=model.revision,
        expected_preview_hash=planned.preview_hash,
    )
    assert result.pko[0].properties[0].conversion.name == "Renamed"
    assert result.pod[0].used_pko[0].name == "Renamed"
    assert (
        apply(
            result,
            operations,
            expected_revision=model.revision,
            expected_preview_hash=planned.preview_hash,
        )
        == result
    )


def test_p3_algorithm_default_append_and_explicit_beginning():
    model = new_manager(project_id="protocol")
    payloads = [
        {
            "client_id": name,
            "kind": "algorithm",
            "action": "create",
            "patch": {"name": name, "body": "X = 1;"},
        }
        for name in ("Zulu", "Alpha", "First")
    ]
    payloads[-1]["after_id"] = None
    model = commit_batch(model, tuple(map(parse_operation, payloads)))
    text = render(model).text
    positions = [text.index("Процедура " + name + "(") for name in ("First", "Zulu", "Alpha")]
    assert positions == sorted(positions)


@pytest.mark.parametrize(
    "case",
    [
        "property_address",
        "missing_owner",
        "missing_target",
        "forward",
        "direction",
        "create_delete",
    ],
)
def test_p1_preview_apply_packet_property(case):
    model = new_manager(project_id="protocol")
    property_payload = {
        "client_id": "link",
        "kind": "property",
        "action": "create",
        "owner_id": {"client_id": "owner"},
        "patch": {"configuration_property": "Link", "format_property": "Link"},
    }
    payloads = [pko_create(), property_payload]
    if case == "property_address":
        payloads.append(
            {
                "client_id": "edit",
                "kind": "property",
                "action": "update",
                "address": "ПКО/Owner/ПКС/Link",
                "patch": {"format_property": "Changed"},
            }
        )
    elif case == "missing_owner":
        property_payload["owner_id"] = "wrong"
    elif case == "missing_target":
        payloads.append(
            {"client_id": "edit", "kind": "property", "action": "delete", "target_id": "wrong"}
        )
    elif case == "forward":
        property_payload["owner_id"] = {"client_id": "later"}
        payloads.append(pko_create("later", "Later"))
    elif case == "direction":
        payloads.insert(1, pko_create("target", "Target"))
        payloads[1]["patch"]["directions"] = ["receive"]
        property_payload["patch"].update(
            property_kind="reference", conversion={"client_id": "target"}
        )
    else:
        payloads.extend(
            [
                {
                    "client_id": "delete_link",
                    "kind": "property",
                    "action": "delete",
                    "target_id": {"client_id": "link"},
                },
                {
                    "client_id": "delete_owner",
                    "kind": "pko",
                    "action": "delete",
                    "target_id": {"client_id": "owner"},
                },
            ]
        )
    operations = tuple(map(parse_operation, payloads))
    planned = preview(model, operations, expected_revision=model.revision)
    if planned.failures:
        with pytest.raises(ManagerOperationError) as error:
            apply(
                model,
                operations,
                expected_revision=model.revision,
                expected_preview_hash=planned.preview_hash,
            )
        assert error.value.failures == planned.failures
        assert planned.model == model and not planned.changes
    else:
        assert (
            apply(
                model,
                operations,
                expected_revision=model.revision,
                expected_preview_hash=planned.preview_hash,
            )
            == planned.model
        )
    assert len(planned.operations) == len(operations)


def test_p1_p2_p4_service_packet_uses_current_model(writer_setup):
    service, args, _ = writer_setup
    created = service.ed_create(**args)
    operations = [
        pko_create(),
        pko_create("target", "Target"),
        {
            "client_id": "link",
            "kind": "property",
            "action": "create",
            "owner_id": {"client_id": "owner"},
            "patch": {
                "configuration_property": "Link",
                "format_property": "Link",
                "property_kind": "reference",
                "conversion": {"client_id": "target"},
            },
        },
        {
            "client_id": "identify",
            "kind": "identification",
            "action": "update",
            "address": "ПКО/Owner/Идентификация",
            "patch": {
                "mode": {"state": "string", "value": "ПоПолямПоиска"},
                "search_sets": [["Link"]],
            },
        },
        pko_create(
            "inline",
            "Inline",
            identification={
                "mode": {"state": "string", "value": "ПоПолямПоиска"},
                "search_sets": [["Code"]],
            },
        ),
    ]
    call = {
        "project_id": created["project_id"],
        "expected_revision": created["revision"],
        "operations": operations,
    }
    planned = service.ed_apply(**call)
    assert planned["failures"]["total"] == 0 and planned["operation_count"] == len(operations)
    applied = service.ed_apply(**call, mode="apply", expected_preview_hash=planned["preview_hash"])
    assert applied["revision"] == planned["future_revision"]
    assert service.ed_apply(**call, mode="apply", expected_preview_hash=planned["preview_hash"])[
        "replayed"
    ]
    wrong = [
        {
            "client_id": "broken",
            "kind": "property",
            "action": "create",
            "owner_id": "wrong",
            "patch": {"configuration_property": "A", "format_property": "A"},
        }
    ]
    refused = service.ed_apply(created["project_id"], applied["revision"], wrong)
    assert refused["failures"]["total"] == refused["operation_count"] == 1
