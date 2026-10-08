"""Отправитель набора вне состава: грабля 25 и границы лексической эвристики."""

from dataclasses import replace

import pytest

from kd_rules_mcp.authoring.ed.manager_operations import parse_operation
from kd_rules_mcp.ed.reader import read_manager_text
from kd_rules_mcp.ed.schema.profile import ValidationProfile
from kd_rules_mcp.ed.writer import new_manager, render
from kd_rules_mcp.validation.ed_plan import validate_plan_content
from kd_rules_mcp.validation.ed_sender import CHECK, sender_uses, validate_sender_handlers
from kd_rules_mcp.validation.ed_structure_snapshot import (
    StructureObject,
    StructureProperty,
    StructureSnapshot,
)
from tests.test_ed_writer import execute

PLAN = "Универсальный"
FACTORY = "Набор = РегистрыСведений.История.СоздатьНаборЗаписей();\n"
SENDER = "Набор.ОбменДанными.Отправитель = КомпонентыОбмена.УзелКорреспондента;"
GUARD = "Если Узел.Метаданные().Состав.Содержит(Набор.Метаданные()) Тогда\n"


def snapshot(*, included=False, kind="РегистрСведений", name="История"):
    obj = StructureObject(1, kind, name, kind + "Ссылка." + name, {}, frozenset())
    content = StructureProperty(1, "{Состав}", "СоставПланаОбмена", "", (), (), {})
    member = StructureProperty(
        2,
        "{Состав}." + name,
        "ЭлементСоставаПланаОбмена",
        "СоставПланаОбмена",
        (obj.type_name,),
        (),
        {},
    )
    plan = StructureObject(
        2,
        "ПланОбмена",
        PLAN,
        "ПланОбменаСсылка." + PLAN,
        {
            ("{состав}", ""): (content,),
            ("{состав}." + name.casefold(), ""): (member,) if included else (),
        },
        frozenset(),
    )
    objects = {(o.kind.casefold(), o.name.casefold()): o for o in (obj, plan)}
    return StructureSnapshot(objects, {o.type_name.casefold(): o for o in objects.values()})


def sender_model(
    body=FACTORY + SENDER,
    *,
    event="ПередЗаписьюПолученныхДанных",
    helper=None,
    owner_kind="pko",
    direction="receive",
):
    model = new_manager()
    patch = {"name": "Получение", "directions": [direction]}
    if owner_kind == "pko":
        patch.update(
            {
                "configuration_object": {
                    "state": "reference",
                    "reference_parts": ["Метаданные", "Справочники", "Должности"],
                },
                "format_object": {"state": "string", "value": "Справочник.Должности"},
            }
        )
    else:
        patch["format_selection"] = {"state": "string", "value": "Справочник.Должности"}
    model = execute(
        model,
        parse_operation(
            {
                "client_id": "owner",
                "kind": owner_kind,
                "action": "create",
                "patch": patch,
            }
        ),
    )
    if helper is not None:
        model = execute(
            model,
            parse_operation(
                {
                    "client_id": "helper",
                    "kind": "algorithm",
                    "action": "create",
                    "patch": {
                        "name": "ЗаписатьИсторию",
                        "routine_kind": "procedure",
                        "parameters": "Набор, КомпонентыОбмена",
                        "body": helper,
                    },
                }
            ),
        )
    owner = model.pko[0] if owner_kind == "pko" else model.pod[0]
    return execute(
        model,
        parse_operation(
            {
                "client_id": "handler",
                "kind": "handler",
                "action": "create",
                "owner_id": owner.logical_id,
                "patch": {"event": event, "body": body},
            }
        ),
    )


def validate(model, structure=None, **kwargs):
    return validate_sender_handlers(
        model,
        read_manager_text(render(model).text),
        ValidationProfile.build(None, "1.20"),
        structure,
        PLAN,
        **kwargs,
    )


@pytest.mark.parametrize("helper", [False, True])
def test_live_history_record_set_outside_plan_warns_with_address_line_and_hint(helper):
    model = sender_model(
        FACTORY + "ЗаписатьИсторию(Набор, КомпонентыОбмена);" if helper else FACTORY + SENDER,
        helper=SENDER if helper else None,
    )
    report = validate(model, snapshot())
    assert not report.skipped and len(report.issues) == 1
    issue = report.issues[0]
    assert issue.check == CHECK and issue.level.value == "предупреждение"
    assert issue.address.startswith("Код/")
    assert f"строка тела {1 if helper else 2}" in issue.message
    assert "РегистрСведений.История" in issue.message and PLAN in issue.message
    assert "Состав.Содержит(Объект.Метаданные())" in issue.message
    assert "registration_objects" in issue.message and "ОДСер:3909–3910" in issue.message
    assert ("вызов из обработчика" in issue.message) is helper


@pytest.mark.parametrize("helper", [False, True])
def test_live_guarded_history_record_set_is_clean(helper):
    guarded = GUARD + "Если Истина Тогда\n" + SENDER + "\nКонецЕсли;\nКонецЕсли;"
    model = sender_model(
        FACTORY + "ЗаписатьИсторию(Набор, КомпонентыОбмена);" if helper else FACTORY + guarded,
        helper=guarded if helper else None,
    )
    assert not validate(model, snapshot()).issues


@pytest.mark.parametrize("delivery", ["included", "addition", "registration"])
def test_history_record_set_in_plan_or_delivered_by_kit_is_clean(delivery):
    assert not validate(
        sender_model(),
        snapshot(included=delivery == "included"),
        additions=(("InformationRegister", "История", "InformationRegisters"),)
        if delivery == "addition"
        else (),
        registration_objects=("РегистрСведений.История",) if delivery == "registration" else (),
    ).issues


@pytest.mark.parametrize(
    "collection,method,kind",
    [
        ("РегистрыСведений", "СоздатьНаборЗаписей", "РегистрСведений"),
        ("РегистрыНакопления", "СоздатьНаборЗаписей", "РегистрНакопления"),
        ("РегистрыБухгалтерии", "СоздатьНаборЗаписей", "РегистрБухгалтерии"),
        ("РегистрыРасчета", "СоздатьНаборЗаписей", "РегистрРасчета"),
        ("Справочники", "СоздатьЭлемент", "Справочник"),
        ("Документы", "СоздатьДокумент", "Документ"),
    ],
)
def test_all_supported_factories_resolve_metadata_type(collection, method, kind):
    body = f"Набор = {collection}.История.{method}();\n" + SENDER
    report = validate(sender_model(body), snapshot(kind=kind))
    assert len(report.issues) == 1 and kind + ".История" in report.issues[0].message


@pytest.mark.parametrize(
    "event,parameter",
    [
        ("ПередЗаписьюПолученныхДанных", "ПолученныеДанные"),
        ("ПередЗаписьюПолученныхДанных", "ДанныеИБ"),
        ("ПриКонвертацииДанныхXDTO", "ПолученныеДанные"),
    ],
)
def test_pko_parameter_type_and_automatic_plan_content_additions(event, parameter):
    model = sender_model(SENDER.replace("Набор", parameter), event=event)
    structure = snapshot(kind="Справочник", name="Должности")
    assert len(validate(model, structure).issues) == 1
    plan_report, additions = validate_plan_content(model, structure, PLAN)
    assert any(issue.check == "ed.plan.content_missing" for issue in plan_report.issues)
    assert not validate(model, structure, additions=additions).issues


def test_received_pod_local_creation_and_helper_without_typed_arguments():
    model = sender_model(
        "ЗаписатьИсторию();", event="ПриОбработке", owner_kind="pod", helper=FACTORY + SENDER
    )
    assert len(validate(model, snapshot()).issues) == 1


def test_alias_direct_factory_argument_and_reassignment():
    body = FACTORY + "Копия = Набор; ЗаписатьИсторию(Копия, КомпонентыОбмена);"
    assert len(validate(sender_model(body, helper=SENDER), snapshot()).issues) == 1
    body = "ЗаписатьИсторию(РегистрыСведений.История.СоздатьНаборЗаписей(), КомпонентыОбмена);"
    assert len(validate(sender_model(body, helper=SENDER), snapshot()).issues) == 1
    assert not validate(
        sender_model(FACTORY + "Набор = Неопределено;\n" + SENDER), snapshot()
    ).issues


def test_guard_does_not_cover_else_or_later_assignment_and_strings_are_ignored():
    body = FACTORY + GUARD + SENDER + "\nИначе\n" + SENDER + "\nКонецЕсли;\n" + SENDER
    assert [use.line for use in sender_uses(body)[0]] == [5, 7]
    assert not sender_uses(FACTORY + "// " + SENDER + '\nСообщить("' + SENDER + '");')[0]
    assert sender_uses(FACTORY + "Если Истина Тогда\n" + SENDER + "\nКонецЕсли;")[0]


def test_unknown_types_are_silent_and_missing_structure_or_plan_is_skipped():
    assert not validate(sender_model(SENDER), snapshot()).issues
    no_structure = validate(sender_model())
    assert not no_structure.issues and no_structure.skipped[0].reason.startswith(
        "structure_required"
    )
    no_plan = validate(sender_model(), StructureSnapshot({}, {}))
    assert not no_plan.issues and no_plan.skipped[0].reason.startswith("plan_content_unavailable")


def test_sending_and_preserved_bodies_are_exempt():
    model = sender_model()
    preserved = replace(
        model, code_units=tuple(replace(u, origin="imported_opaque") for u in model.code_units)
    )
    assert not validate(preserved, snapshot()).issues
    assert validate(preserved, snapshot(), include_preserved=True).issues
    assert not validate(
        sender_model(event="ПриОбработке", owner_kind="pod", direction="send"), snapshot()
    ).issues
