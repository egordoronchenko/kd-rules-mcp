"""Круг форм четвёртого захода переноса на вымышленных правилах."""

from dataclasses import replace

import pytest

from kd2_rules_mcp.authoring.ed.manager_operations import parse_operation
from kd2_rules_mcp.authoring.ed.manager_render import _reread
from kd2_rules_mcp.authoring.ed.model import AuthoringPreconditionError
from kd2_rules_mcp.ed.canonical import canonicalize
from kd2_rules_mcp.ed.reader import read_manager_text
from kd2_rules_mcp.ed.writer import new_manager, render
from kd2_rules_mcp.ed.writer_import import import_manager
from kd2_rules_mcp.ed.writer_model import (
    LayoutElement,
    RetainedBlock,
    dump_model,
    load_model,
    text_hash,
)
from kd2_rules_mcp.validation.ed_writer import validate_writer
from tests.test_ed_writer_code import round_trip
from tests.test_ed_writer_values import commit_batch


def operation(client, kind, patch, **fields):
    return parse_operation(
        dict(client_id=client, kind=kind, action="create", patch=patch, **fields)
    )


def packet(form):
    rows = [operation("owner", "pko", {"name": "Item", "directions": ["both"]})]
    owner = {"client_id": "owner"}
    if form in ("table", "all"):
        rows += [
            operation(
                "table",
                "table_part",
                {"configuration_property": "", "format_property": "Rows"},
                owner_id=owner,
            ),
            operation(
                "column",
                "property",
                {
                    "configuration_property": "",
                    "format_property": "Amount",
                    "property_kind": "algorithm",
                    "algorithm_flag": 1,
                },
                owner_id={"client_id": "table"},
            ),
        ]
    if form in ("shared", "all"):
        rows += [
            operation("second", "pko", {"name": "Second", "directions": ["receive"]}),
            operation("third", "pko", {"name": "Third", "directions": ["receive"]}),
            operation(
                "handler",
                "handler",
                {
                    "event": "ПередЗаписьюПолученныхДанных",
                    "body": "ПолученныеДанные = Неопределено;",
                },
                owner_id=owner,
            ),
            operation(
                "second-handler",
                "handler",
                {"event": "ПередЗаписьюПолученныхДанных", "target": {"client_id": "handler"}},
                owner_id={"client_id": "second"},
            ),
            operation(
                "third-handler",
                "handler",
                {"event": "ПередЗаписьюПолученныхДанных", "target": {"client_id": "handler"}},
                owner_id={"client_id": "third"},
            ),
        ]
    if form in ("pkpd", "all"):
        rows += [
            operation(
                "colors",
                "pkpd",
                {
                    "name": "Colors",
                    "directions": ["send", "receive"],
                    "configuration_type": {
                        "state": "reference",
                        "reference_parts": ["Метаданные", "Перечисления", "Colors"],
                    },
                    "format_type": {"state": "string", "value": "Color"},
                },
            )
        ]
        for direction in ("send", "receive"):
            rows += [
                operation(
                    "value-" + direction,
                    "value_mapping",
                    {
                        "direction": direction,
                        "configuration_value": {
                            "state": "reference",
                            "reference_parts": ["Перечисления", "Colors", "Red"],
                        },
                        "format_value": {"state": "string", "value": "Red"},
                    },
                    owner_id={"client_id": "colors"},
                )
            ]
    if form in ("key", "all"):
        rows += [
            operation(
                "key",
                "property",
                {
                    "configuration_property": "",
                    "format_property": "Owner",
                    "property_kind": "algorithm",
                    "algorithm_flag": 1,
                    "conversion": {"kind": "pko", "target_id": owner},
                },
                owner_id=owner,
            )
        ]
    if form in ("additional", "all"):
        rows += [
            operation(
                "additional",
                "handler",
                {
                    "event": "ПриОтправкеДанных",
                    "body": "ДанныеXDTO.AdditionalInfo = Новый Структура;\n"
                    'ДанныеXDTO.AdditionalInfo.Вставить("Dates", Новый ТаблицаЗначений);',
                },
                owner_id=owner,
            )
        ]
    return rows


@pytest.mark.parametrize("form", ["table", "shared", "pkpd", "key", "additional", "all"])
def test_batch4_forms_round_trip(form):
    round_trip(commit_batch(new_manager(), packet(form)))


def test_code_dependency_is_refreshed_when_pkpd_created_later():
    model = commit_batch(
        new_manager(),
        [operation("algorithm", "algorithm", {"name": "Lookup", "body": 'ИмяПравила = "Colors";'})],
    )
    model = commit_batch(model, packet("pkpd"))
    unit = next(u for u in model.code_units if u.name == "Lookup")
    assert any(ref.kind == "pkpd" and ref.name == "Colors" for ref in unit.dependencies)
    round_trip(model)


def test_namespace_refresh_keeps_dependencies_of_conditional_method_copies_separate():
    text = (
        render(new_manager()).text
        + """
#Область Алгоритмы
#Если Сервер Тогда
Процедура Business(КомпонентыОбмена)
    ОбменДаннымиXDTOСервер.ПКОПоИмени(КомпонентыОбмена, "Target");
КонецПроцедуры
#Иначе
Процедура Business(КомпонентыОбмена)
    ОбменДаннымиXDTOСервер.ПКОПоИмени(КомпонентыОбмена, "Target");
    ОбменДаннымиXDTOСервер.ПКОПоИмени(КомпонентыОбмена, "Other");
КонецПроцедуры
#КонецЕсли
#КонецОбласти
"""
    )
    model = import_manager(read_manager_text(text), project_id="collision")[0]
    model = commit_batch(
        model, [operation("target", "pko", {"name": "Target", "directions": ["both"]})]
    )
    units = [u for u in model.code_units if u.name == "Business"]
    assert all(u.state == "retained" for u in units)
    assert [tuple(ref.name for ref in u.dependencies) for u in units] == [
        ("Target",),
        ("Target", "Other"),
    ]
    round_trip(model)


def test_current_snapshot_recovers_bidirectional_pkpd_and_stale_dependency_index():
    model = commit_batch(
        new_manager(),
        [
            operation(
                "algorithm", "algorithm", {"name": "Lookup", "body": 'ИмяПравила = "Colors";'}
            ),
            *packet("pkpd"),
        ],
    )
    model = replace(
        model,
        pkpd=tuple(replace(r, directions=("send", "receive")) for r in model.pkpd),
        code_units=tuple(
            replace(u, dependencies=()) if u.name == "Lookup" else u for u in model.code_units
        ),
    ).with_revision()
    before = render(model).data
    loaded = load_model(dump_model(model))
    assert loaded.pkpd[0].directions == ("both",)
    assert render(loaded).data == before
    round_trip(loaded)


def test_current_snapshot_recovers_old_clear_data_scaffold_without_changing_text():
    model = new_manager()
    key = "old-clear-data"
    text = (
        '\tЕсли ПравилаОбработкиДанных.Колонки.Найти("ОчисткаДанных") = Неопределено Тогда\r\n'
        '\t\tПравилаОбработкиДанных.Колонки.Добавить("ОчисткаДанных");\r\n'
        "\tКонецЕсли;\r\n"
    )
    owner = next(c for c in model.layouts if c.name == "ЗаполнитьПравилаОбработкиДанных")
    block = RetainedBlock(
        logical_id=key,
        name="Текст",
        state="retained",
        kind="scaffold",
        text=text,
        sha256=text_hash(text),
        file_id="",
        source_hash="",
        owner_id=owner.logical_id,
        reason="outside_w1",
    )
    model = replace(
        model,
        header=replace(model.header, clear_data_column=False),
        layouts=tuple(
            replace(c, elements=(*c.elements, LayoutElement(key, "text", block_id=key)))
            if c.logical_id == owner.logical_id
            else c
            for c in model.layouts
        ),
        retained_blocks=(*model.retained_blocks, block),
    ).with_revision()
    before = render(model).data
    loaded = load_model(dump_model(model))
    assert loaded.header.clear_data_column
    assert render(loaded).data == before
    round_trip(loaded)


def test_current_snapshot_recovers_old_migrated_frame_ids():
    model = new_manager()
    code = {c.logical_id for c in model.layouts if c.kind in ("code", "dispatcher")}
    model = replace(
        model,
        layouts=tuple(
            replace(
                c,
                elements=tuple(
                    replace(e, logical_id="old-frame-" + e.logical_id)
                    if e.container_id in code
                    else e
                    for e in c.elements
                ),
            )
            for c in model.layouts
        ),
    ).with_revision()
    before = render(model).data
    loaded = load_model(dump_model(model))
    assert render(loaded).data == before
    round_trip(loaded)


def test_validation_and_build_report_same_entity_and_field():
    model = commit_batch(new_manager(), [*packet("key")])
    source = render(model)
    rule = model.pko[0]
    broken = commit_batch(
        model,
        [
            parse_operation(
                {
                    "client_id": "changed",
                    "kind": "property",
                    "action": "update",
                    "target_id": rule.properties[0].logical_id,
                    "patch": {"configuration_property": "Changed"},
                }
            )
        ],
    )
    issues = [i for i in validate_writer(broken, source.data).errors if i.check == "ed.writer.read"]
    assert len(issues) == 1
    assert issues[0].address == "ПКО/Item/ПКС/Owner"
    assert "configuration_property" in issues[0].message
    with pytest.raises(AuthoringPreconditionError) as error:
        _reread(broken, source)
    failure = error.value.failures[0]
    assert failure.address == issues[0].address
    assert failure.message == issues[0].message


def test_equal_readback_keeps_opaque_additional_info_body():
    model = commit_batch(new_manager(), packet("additional"))
    output = render(model)
    back = import_manager(
        read_manager_text(output.data.decode("utf-8")),
        project_id=model.project_id,
        manager_name=model.header.manager_name,
    )[0]
    assert canonicalize(model) == canonicalize(back)
