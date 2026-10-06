"""Перечисление формата как строка: прямое чтение, выходной аргумент и локальный алгоритм."""

from dataclasses import replace

import pytest

from kd2_rules_mcp.authoring.ed.manager_operations import parse_operation
from kd2_rules_mcp.ed.reader import read_manager_text
from kd2_rules_mcp.ed.schema import load_schema
from kd2_rules_mcp.ed.schema.profile import ValidationProfile
from kd2_rules_mcp.ed.writer import new_manager, render
from kd2_rules_mcp.validation.ed_handler_enum import enum_uses, validate_enum_handlers
from tests.test_ed_profile import DATA
from tests.test_ed_writer import execute


@pytest.mark.parametrize(
    "body",
    [
        'Если ДанныеXDTO.Перечисление = "A" Тогда КонецЕсли;',
        'Если "A" = ДанныеXDTO["Перечисление"] Тогда КонецЕсли;',
        'Вид = Неопределено; ДанныеXDTO.Свойство("Перечисление", Вид); '
        'Если Вид = "A" Тогда КонецЕсли;',
        'Вид = ДанныеXDTO.Перечисление; СтрНайти(Вид, "A");',
        'ВРег(ДанныеXDTO["Перечисление"]);',
    ],
)
def test_live_format_enum_is_a_structure(body):
    warnings, _ = enum_uses(body, lambda p: p if p == "Перечисление" else None)
    assert warnings and warnings[0].property == "Перечисление"


@pytest.mark.parametrize(
    "body",
    [
        'Если ДанныеXDTO.Перечисление.Значение = "A" Тогда КонецЕсли;',
        'Вид = ДанныеXDTO.Перечисление; Вид = Вид.Значение; Если Вид = "A" Тогда КонецЕсли;',
        'Вид = ДанныеXDTO.Перечисление; Если ТипЗнч(Вид) = Тип("Структура") '
        'И Вид.Свойство("Значение") Тогда Вид = Вид.Значение; КонецЕсли; '
        'Если Вид = "A" Тогда КонецЕсли;',
        '// ДанныеXDTO.Перечисление = "A"\nСообщить("ДанныеXDTO.Перечисление");',
        'Если ДанныеXDTO.Код = "A" Тогда КонецЕсли;',
        'Вид = ДанныеXDTO.Перечисление; Вид = "A"; Если Вид = "A" Тогда КонецЕсли;',
        'ДанныеXDTO.Свойство("Перечисление", Вид); ДанныеXDTO.Свойство("Код", Вид); '
        'Если Вид = "A" Тогда КонецЕсли;',
    ],
)
def test_value_extraction_scalar_property_and_comments_are_clean(body):
    assert not enum_uses(body, lambda p: p if p == "Перечисление" else None)[0]


def enum_model(*, helper=False, direction="receive"):
    model = new_manager()
    model = execute(
        model,
        parse_operation(
            {
                "client_id": "pod",
                "kind": "pod",
                "action": "create",
                "patch": {
                    "name": "Получение",
                    "directions": [direction],
                    "format_selection": {"state": "string", "value": "Справочник.Тест"},
                },
            }
        ),
    )
    if helper:
        model = execute(
            model,
            parse_operation(
                {
                    "client_id": "helper",
                    "kind": "algorithm",
                    "action": "create",
                    "patch": {
                        "name": "ВыбратьПКО",
                        "routine_kind": "procedure",
                        "parameters": "ИспользованиеПКО, Вид",
                        "body": 'ИспользованиеПКО.Вставить("Тест", Вид = "A");',
                    },
                }
            ),
        )
    body = 'Вид = Неопределено; ДанныеXDTO.Свойство("Перечисление", Вид); '
    body += "ВыбратьПКО(ИспользованиеПКО, Вид);" if helper else 'Если Вид = "A" Тогда КонецЕсли;'
    return execute(
        model,
        parse_operation(
            {
                "client_id": "handler",
                "kind": "handler",
                "action": "create",
                "owner_id": model.pod[0].logical_id,
                "patch": {"event": "ПриОбработке", "body": body},
            }
        ),
    )


@pytest.mark.parametrize("helper", [False, True])
def test_receiving_pod_and_its_helper_report_code_address_and_line(helper):
    model = enum_model(helper=helper)
    schema = load_schema(DATA / "validation.bin")
    report = validate_enum_handlers(
        model, read_manager_text(render(model).text), ValidationProfile.build(schema, "1.2")
    )
    assert len(report.issues) == 1
    issue = report.issues[0]
    assert issue.level.value == "предупреждение" and issue.address.startswith("Код/")
    assert ".Значение" in issue.message and "грабля 22" in issue.message
    assert "строка тела 1" in issue.message


def test_no_schema_skips_and_sending_preserved_bodies_are_exempt():
    model = enum_model()
    doc = read_manager_text(render(model).text)
    no_schema = validate_enum_handlers(model, doc, ValidationProfile.build(None, "1.2"))
    assert not no_schema.issues and any(
        s.reason.startswith("schema_required") for s in no_schema.skipped
    )
    schema = load_schema(DATA / "validation.bin")
    profile = ValidationProfile.build(schema, "1.2")
    preserved = replace(
        model, code_units=tuple(replace(u, origin="imported_opaque") for u in model.code_units)
    )
    assert not validate_enum_handlers(preserved, doc, profile).issues
    assert validate_enum_handlers(preserved, doc, profile, include_preserved=True).issues
    sending = enum_model(direction="send")
    assert not validate_enum_handlers(
        sending, read_manager_text(render(sending).text), profile
    ).issues
