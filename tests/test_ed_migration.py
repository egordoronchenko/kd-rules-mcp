"""Сценарии черновика: опись, явный выбор, реальные пакеты писателя и read-инструмент."""

import json
import sqlite3
from copy import deepcopy
from dataclasses import asdict, replace
from hashlib import sha256
from pathlib import Path
from typing import Any

import anyio
import pytest
from lxml import etree
from mcp import Client

from kd_rules_mcp.authoring.ed.manager_operations import apply, parse_operation, preview
from kd_rules_mcp.authoring.ed.migration import build_migration_draft
from kd_rules_mcp.authoring.ed.migration_inventory import snapshot
from kd_rules_mcp.ed.model import Expr, Field
from kd_rules_mcp.ed.reader import read_manager_text
from kd_rules_mcp.ed.schema import load_schema
from kd_rules_mcp.ed.writer import new_manager, render
from kd_rules_mcp.kd2.model import Node
from kd_rules_mcp.kd2.rules_io import load_exchange_rules
from kd_rules_mcp.server import TOOL_EFFECTS, create_server
from kd_rules_mcp.validation.address import pks_address, rule_address
from tests.test_ed_writer_candidates import add_prop, structure
from tests.test_service_ed_writer import files_at
from tests.test_service_ed_writer import writer_setup as writer_setup

DATA = Path(__file__).parent / "data"
RULES = DATA / "migration/ExchangeRules.xml"


def extend_structure(connection):
    connection.execute("UPDATE properties SET string_length=10 WHERE name='Наименование'")
    connection.execute(
        "INSERT INTO objects(kind,name,type_name) "
        "VALUES ('Перечисление','Статусы','ПеречислениеСсылка.Статусы')"
    )
    enum_id = connection.execute("SELECT id FROM objects WHERE name='Статусы'").fetchone()[0]
    connection.executemany(
        "INSERT INTO object_values(object_id,name) VALUES (?,?)",
        [(enum_id, v) for v in ("Активен", "Закрыт")],
    )
    add_prop(
        connection, "Справочник", "Должности", "Реквизит", "Статус", ("ПеречислениеСсылка.Статусы",)
    )
    add_prop(
        connection, "Документ", "Заказ", "Реквизит", "Должность", ("СправочникСсылка.Должности",)
    )
    table = add_prop(connection, "Документ", "Заказ", "ТабличнаяЧасть", "Товары", ())
    add_prop(connection, "Документ", "Заказ", "Реквизит", "Номенклатура", ("Строка",), parent=table)
    connection.commit()


def extend_schema(path):
    text = path.read_text("utf-8")
    text = text.replace(
        '<objectType name="Справочник.Должности">',
        '<objectType name="Справочник.Должности"><property name="Статус" type="t:Статусы"/>',
    )
    text = text.replace(
        '<objectType name="Документ.Заказ">',
        '<objectType name="Документ.Заказ">'
        '<property name="Должность" type="t:КлючевыеСвойстваДолжности"/>'
        '<property name="Товары" type="t:Товары"/>',
    )
    text = text.replace(
        "</package>",
        '<valueType name="Статусы" base="xs:string">'
        "<enumeration>Активен</enumeration><enumeration>Закрыт</enumeration>"
        "</valueType></package>",
    )
    path.write_text(text, encoding="utf-8", newline="\n")


@pytest.fixture
def synthetic(tmp_path):
    connection = structure()
    extend_structure(connection)
    path = tmp_path / "format.bin"
    path.write_bytes((DATA / "ed/writer/format.bin").read_bytes())
    extend_schema(path)
    schema = load_schema(path, locate_import=lambda _: DATA / "ed/writer/message.bin")
    yield connection, schema, load_exchange_rules(RULES)
    connection.close()


def draft(synthetic, *, role="send", manager=None, rules=None, **kwargs):
    connection, schema, original = synthetic
    return build_migration_draft(
        rules or [(role, original)], connection, schema, manager or new_manager(), **kwargs
    )


def entity(result, code, kind="pko", role="send"):
    if kind == "pko":
        origin = f"{role}:ПКО «{code}»"
    elif kind == "pvd":
        origin = f"{role}:ПВД «{code}»"
    else:
        origin = f"{role}:ПКО «{code[0]}» / ПКС {code[1]}"
    return next(e for e in result.coverage if e.origin == origin)


def operations(result, kind=None):
    return [o for p in result.packets for o in p.operations if kind is None or o["kind"] == kind]


def apply_draft(manager, result, *, notice_codes=("table_part_replace",)):
    for packet in result.packets:
        ops = tuple(parse_operation(o) for o in packet.operations)
        planned = preview(manager, ops, expected_revision=manager.revision)
        assert not planned.failures, planned.failures
        assert all(n.code in notice_codes for n in planned.notices)
        manager = apply(
            manager,
            ops,
            expected_revision=manager.revision,
            expected_preview_hash=planned.preview_hash,
            confirmations=tuple((n.code, n.notice_hash) for n in planned.notices),
        )
    return manager


def test_inventory_complete(synthetic):
    rules = synthetic[2]
    rules.root.values["ПередВыгрузкойДанных"] = "Начать();\n"
    rules.pko()[0].values["ПриВыгрузке"] = "Изменить();"
    for section, kind, tag in (
        ("Алгоритмы", "algorithm", "Алгоритм"),
        ("Запросы", "query", "Запрос"),
    ):
        node = Node.new(kind, tag)
        node.attrs["Имя"] = kind
        node.values["Текст"] = "СекретноеТело();"
        rules.section(section).items.append(node)
    result = draft(synthetic, accept_single=True)
    counts = result.summary()["kinds"]
    assert {
        k: counts[k]
        for k in (
            "pko",
            "pks",
            "pks_group",
            "pkz",
            "pvd",
            "parameter",
            "algorithm",
            "query",
            "handler",
        )
    } == {
        "pko": 3,
        "pks": 6,
        "pks_group": 1,
        "pkz": 2,
        "pvd": 2,
        "parameter": 1,
        "algorithm": 1,
        "query": 1,
        "handler": 2,
    }
    assert len({e.origin for e in result.coverage}) == len(result.coverage)
    assert sum(result.summary()["states"].values()) == len(result.coverage)
    assert "СекретноеТело" not in json.dumps(asdict(result), ensure_ascii=False)


def test_inventory_pko_handler_manual_task(synthetic):
    body = "\r\n  // без нормализации\r\nХ = 1;\n"
    synthetic[2].pko()[0].values["ПриВыгрузке"] = body
    result = draft(synthetic, accept_single=True)
    assert entity(result, "Должности").state == "ready"
    task = next(t for t in result.manual if t.event == "ПриВыгрузке")
    assert task.proposed_location == "ПриОтправкеДанных"
    assert task.body_sha256 == sha256(body.encode()).hexdigest()
    assert task.line_count == body.count("\n") + 1
    assert body not in json.dumps(asdict(result), ensure_ascii=False)


@pytest.mark.parametrize("unknown", [False, True])
def test_inventory_unknown_tag_is_never_ready(synthetic, unknown):
    node = synthetic[2].pko()[0]
    if unknown:
        node.unknown.append(etree.fromstring(b"<Future>Hidden();</Future>"))
    result = draft(synthetic, accept_single=True)
    row = entity(result, "Должности")
    assert (row.state, row.reason) == (
        ("manual", "unsupported") if unknown else ("ready", "resolved")
    )
    if unknown:
        assert (
            next(t for t in result.manual if t.event == "Future").body_sha256
            == sha256(b"Hidden();").hexdigest()
        )
        assert "Hidden();" not in json.dumps(asdict(result))


@pytest.mark.parametrize("referenced", [False, True])
def test_unreachable_reference_only_and_unused(synthetic, referenced):
    rules = synthetic[2]
    rules.section("ПравилаВыгрузкиДанных").items.pop(0)
    if not referenced:
        rules.pko()[1].child("Свойства").items.pop(1)
    result = draft(synthetic, accept_single=True)
    row = entity(result, "Должности")
    assert (row.state, row.reason) == (
        ("ready", "resolved") if referenced else ("excluded", "unreachable")
    )


def test_candidates_single_without_flag(synthetic):
    result = draft(synthetic)
    row = entity(result, "Должности")
    assert (row.state, row.reason) == ("needs_decision", "single_candidate")
    assert all(c["auto"] is False for c in row.candidates)
    prop = entity(result, ("Должности", "Код"), "pks")
    assert prop.state == "blocked" and prop.blocked_by == [row.origin]
    assert not operations(result, "pko")


def test_decisions_agent_selects_type(synthetic):
    result = draft(
        synthetic,
        decisions=[{"origin": "send:ПКО «Должности»", "format_type": "Справочник.Должности"}],
    )
    assert entity(result, "Должности").state == "ready"
    assert operations(result, "pko")[0]["patch"]["format_object"] == {
        "state": "string",
        "value": "Справочник.Должности",
    }


def test_decisions_invalid_schema_type(synthetic):
    result = draft(
        synthetic,
        accept_single=True,
        decisions=[{"origin": "send:ПКО «Должности»", "format_type": "Несуществующий"}],
    )
    assert entity(result, "Должности").reason == "decision_invalid"
    assert all(o["patch"]["name"] != "Должности" for o in operations(result, "pko"))


def test_decisions_unknown_origin_refused(synthetic):
    with pytest.raises(ValueError, match="origin: отсутствует"):
        draft(synthetic, decisions=[{"origin": "отсутствует", "exclude": True}])


def test_decisions_invalid_property_and_value(synthetic):
    result = draft(
        synthetic,
        accept_single=True,
        decisions=[
            {"origin": "send:ПКО «Должности» / ПКС Код", "format_property": "Номер"},
            {"origin": "send:ПКО «Статусы»/Значение/Активен", "format_value": "НеЛитерал"},
        ],
    )
    assert entity(result, ("Должности", "Код"), "pks").reason == "decision_invalid"
    assert (
        next(e for e in result.coverage if e.origin.endswith("/Значение/Активен")).reason
        == "decision_invalid"
    )


@pytest.mark.parametrize("code", ["Статусы", "стАТУсы", "НетТакого"])
def test_operations_conversion_code_case_or_missing(synthetic, code):
    synthetic[2].pko()[0].child("Свойства").items[2].values["КодПравилаКонвертации"] = code
    result = draft(synthetic, accept_single=True)
    row = entity(result, ("Должности", "Статус"), "pks")
    assert row.state == ("manual" if code == "НетТакого" else "ready")
    if code == "НетТакого":
        assert row.reason == "unsupported"


def test_decisions_excluded_target_blocks_property(synthetic):
    result = draft(
        synthetic,
        accept_single=True,
        decisions=[{"origin": "send:ПКО «Статусы»", "exclude": True, "reason": "Не переносим"}],
    )
    row = entity(result, ("Должности", "Статус"), "pks")
    assert row.state == "blocked" and row.blocked_by == ["send:ПКО «Статусы»"]


@pytest.mark.parametrize("missing", [None, "Источник", "Приемник"])
def test_inventory_empty_property_side(synthetic, missing):
    node = synthetic[2].pko()[0].child("Свойства").items[0]
    if missing:
        node.children.pop(missing)
    result = draft(synthetic, accept_single=True)
    row = next(e for e in result.coverage if e.kind == "pks" and e.parent == "send:ПКО «Должности»")
    assert row.state == ("manual" if missing else "ready")
    if missing:
        assert row.reason == "unsupported"


@pytest.mark.parametrize("missing", [False, True])
def test_inventory_empty_group_side(synthetic, missing):
    if missing:
        synthetic[2].pko()[1].child("Свойства").items[-1].children.pop("Приемник")
    result = draft(synthetic, accept_single=True)
    group = next(e for e in result.coverage if e.kind == "pks_group")
    assert group.state == ("manual" if missing else "ready")
    child = next(e for e in result.coverage if e.origin.endswith("Товары/Номенклатура"))
    assert child.state == ("blocked" if missing else "ready")


@pytest.mark.parametrize("bad", [False, True])
def test_inventory_unsupported_property_kind(synthetic, bad):
    if bad:
        synthetic[2].pko()[0].child("Свойства").items[0].child("Источник").attrs["Вид"] = (
            "ПроизвольноеВыражение"
        )
    row = entity(draft(synthetic, accept_single=True), ("Должности", "Код"), "pks")
    assert row.state == ("manual" if bad else "ready")


@pytest.mark.parametrize("missing", [False, True])
def test_inventory_host_object_missing(synthetic, missing):
    if missing:
        synthetic[2].pko()[0].values["Источник"] = "СправочникСсылка.Несуществующий"
    row = entity(draft(synthetic, accept_single=True), "Должности")
    assert row.state == ("excluded" if missing else "ready")
    if missing:
        assert row.reason == "host_object_missing"


@pytest.mark.parametrize("disabled", [False, True])
def test_inventory_disabled_property_and_pvd(synthetic, disabled):
    if disabled:
        synthetic[2].pko()[0].child("Свойства").items[0].attrs["Отключить"] = True
        synthetic[2].pvd()[0].attrs["Отключить"] = True
    result = draft(synthetic, accept_single=True)
    for row in (entity(result, ("Должности", "Код"), "pks"), entity(result, "Должности", "pvd")):
        assert row.state == ("excluded" if disabled else "ready")
        if disabled:
            assert row.reason == "disabled"


@pytest.mark.parametrize("custom", [False, True])
def test_inventory_nonstandard_pvd_selection(synthetic, custom):
    if custom:
        synthetic[2].pvd()[0].values["СпособОтбораДанных"] = "ПроизвольныйАлгоритм"
    row = entity(draft(synthetic, accept_single=True), "Должности", "pvd")
    assert row.state == ("manual" if custom else "ready")


def test_operations_pvd_handler_is_manual(synthetic):
    synthetic[2].pvd()[0].values["ПередВыгрузкойОбъекта"] = "Отказ = Истина;"
    result = draft(synthetic, accept_single=True)
    assert entity(result, "Должности", "pvd").state == "manual"
    assert all(o["patch"]["name"] != "Должности_Отправка" for o in operations(result, "pod"))
    assert (
        next(t for t in result.manual if t.event == "ПередВыгрузкойОбъекта").proposed_location
        == "ПриОбработке"
    )


def test_operations_receive_identification_configuration_names(synthetic):
    result = draft(synthetic, role="receive", accept_single=True)
    pko = next(o for o in operations(result, "pko") if o["patch"]["name"] == "Должности")
    assert pko["patch"]["identification"] == {
        "mode": {
            "state": "string",
            "value": "СначалаПоУникальномуИдентификаторуПотомПоПолямПоиска",
        },
        "search_sets": [["Код"]],
    }


def test_operations_reference_property(synthetic):
    result = draft(synthetic, accept_single=True)
    prop = next(
        o
        for o in operations(result, "property")
        if o["patch"]["configuration_property"] == "Должность"
    )
    assert prop["patch"]["property_kind"] == "reference"
    assert "conversion" in prop["patch"]
    apply_draft(new_manager(), result)


def test_operations_receive_always_has_pod(synthetic):
    result = draft(synthetic, role="receive", accept_single=True)
    assert {o["patch"]["format_selection"]["value"] for o in operations(result, "pod")} == {
        "Справочник.Должности",
        "Документ.Заказ",
    }
    assert all(o["patch"]["used_pko"] for o in operations(result, "pod"))


@pytest.mark.parametrize("folded", [False, True])
def test_candidates_case_of_object_property_and_enum_values(synthetic, folded):
    if folded:
        node = synthetic[2].pko()[0]
        node.values["Источник"] = str(node.get("Источник")).swapcase()
        node.values["Код"] = "доЛЖности"
        node.child("Свойства").items[0].child("Источник").attrs["Имя"] = "кОД"
        for value in synthetic[2].pko()[2].child("Значения").items:
            value.values["Источник"] = str(value.get("Источник")).swapcase()
    result = draft(synthetic, accept_single=True)
    assert len(operations(result, "pko")) == 2
    assert any(
        o["patch"]["configuration_property"] == "Код" for o in operations(result, "property")
    )
    assert {o["patch"]["format_value"]["value"] for o in operations(result, "value_mapping")} == {
        "Активен",
        "Закрыт",
    }


def test_enum_values_require_decision_and_accept_literal(synthetic):
    node = synthetic[2].pko()[2].child("Значения").items[0]
    node.values["Источник"] = "Иное"
    node.values["Приемник"] = "Другое"
    connection = synthetic[0]
    connection.execute("UPDATE object_values SET name='Иное' WHERE name='Активен'")
    connection.commit()
    result = draft(synthetic, accept_single=True)
    row = next(e for e in result.coverage if e.origin.endswith("/Значение/Иное"))
    assert row.state == "needs_decision" and row.reason == "no_candidate"
    selected = draft(
        synthetic, accept_single=True, decisions=[{"origin": row.origin, "format_value": "Активен"}]
    )
    assert next(e for e in selected.coverage if e.origin == row.origin).state == "ready"


def test_packets_same_name_two_roles_get_suffix(synthetic):
    result = draft(
        synthetic,
        rules=[("receive", deepcopy(synthetic[2])), ("send", synthetic[2])],
        accept_single=True,
    )
    assert {o["patch"]["name"] for o in operations(result, "pko")} == {
        "Должности_Отправка",
        "Должности_Получение",
        "Заказ_Отправка",
        "Заказ_Получение",
    }
    apply_draft(new_manager(), result)


def test_packets_determinism_decision_order(synthetic):
    decisions = [
        {"origin": "send:ПКО «Должности»", "format_type": "Справочник.Должности"},
        {"origin": "send:ПКО «Заказ»", "format_type": "Документ.Заказ"},
    ]
    a = draft(synthetic, decisions=decisions, accept_single=True)
    b = draft(synthetic, decisions=list(reversed(decisions)), accept_single=True)
    assert asdict(a) == asdict(b)
    c = draft(
        synthetic,
        decisions=[{**decisions[0], "format_type": "Справочник.Классификатор"}, decisions[1]],
        accept_single=True,
    )
    assert a.decision_hash != c.decision_hash
    assert operations(a, "pko")[0]["client_id"] != operations(c, "pko")[0]["client_id"]


def test_packets_repeat_after_apply(synthetic):
    model = new_manager()
    first = draft(synthetic, manager=model, accept_single=True)
    applied = apply_draft(model, first)
    repeated = draft(synthetic, manager=applied, accept_single=True)
    assert not operations(repeated), [(e.origin, e.state, e.reason) for e in repeated.coverage]
    assert entity(repeated, "Должности").reason == "already_in_manager"


def test_packets_name_conflict(synthetic):
    model = apply_draft(new_manager(), draft(synthetic, accept_single=True))
    result = draft(
        synthetic,
        manager=model,
        accept_single=True,
        decisions=[{"origin": "send:ПКО «Должности»", "format_type": "Справочник.Классификатор"}],
    )
    assert entity(result, "Должности").reason == "name_conflict"


@pytest.mark.parametrize("count", [99, 100, 101, 201])
def test_packets_operation_boundary(synthetic, count):
    rules = synthetic[2]
    rules.root.children.pop("ПравилаКонвертацииОбъектов")
    rules.root.children.pop("ПравилаВыгрузкиДанных")
    parameters = rules.section("Параметры")
    parameters.items.clear()
    for index in range(count):
        node = Node.new("parameter", "Параметр")
        node.attrs["Имя"] = f"Параметр{index}"
        parameters.items.append(node)
    result = draft(synthetic, accept_single=True)
    assert [len(p.operations) for p in result.packets] == [100] * (count // 100) + (
        [count % 100] if count % 100 else []
    )
    assert result.packets[0].expected_revision == new_manager().revision
    assert all(p.expected_revision is None for p in result.packets[1:])
    apply_draft(new_manager(), result)


@pytest.fixture
def migration_service(writer_setup, tmp_path):
    service, args, root = writer_setup
    connection = sqlite3.connect(service.store.path("host"))
    extend_structure(connection)
    connection.close()
    service.ed_schema_close(args["schema_id"])
    extend_schema(root / "Schemas/format.bin")
    opened = service.ed_schema_open(
        "1.20",
        path=str(root / "Schemas/format.bin"),
        imports={"urn:test:writer-message": str(DATA / "ed/writer/message.bin")},
    )
    args["schema_id"] = opened["schema_id"]
    created = service.ed_create(**args)
    inputs = []
    for role in ("send", "receive"):
        path = tmp_path / (role + ".xml")
        path.write_bytes(RULES.read_bytes())
        opened_rules = service.rules_open(str(path))
        inputs.append({"rules_id": opened_rules["project_id"], "role": role})
    arguments = {k: args[k] for k in ("project_id", "schema_id", "structure_id")}
    arguments["rules"] = inputs
    return service, arguments, created


def test_end_to_end_nothing_written(migration_service):
    service, arguments, created = migration_service
    before = files_at(service.workspace.root)
    rules_before = [snapshot(service._exchange(r["rules_id"]).root) for r in arguments["rules"]]
    result = service.ed_migration_draft(
        **arguments, accept_single=True, reference_document_id="auto"
    )
    assert result["revision"] == created["revision"]
    assert files_at(service.workspace.root) == before
    assert [
        snapshot(service._exchange(r["rules_id"]).root) for r in arguments["rules"]
    ] == rules_before
    assert service._manager_project(arguments["project_id"]).model.revision == created["revision"]


def test_end_to_end_explicit_operations_and_writer(migration_service):
    service, arguments, created = migration_service
    arguments = {**arguments, "rules": arguments["rules"][:1]}
    page = service.ed_migration_draft(
        **arguments, accept_single=True, section="operations", limit=200
    )
    actual = [
        (
            o["kind"],
            o["patch"].get("name")
            or o["patch"].get("configuration_property")
            or o["patch"].get("format_value", {}).get("value"),
        )
        for p in page["items"]
        for o in p["operations"]
    ]
    assert actual == [
        ("pkpd", "Статусы"),
        ("value_mapping", "Активен"),
        ("value_mapping", "Закрыт"),
        ("pko", "Должности"),
        ("pko", "Заказ"),
        ("table_part", "Товары"),
        ("property", "Код"),
        ("property", "Наименование"),
        ("property", "Статус"),
        ("property", "Номер"),
        ("property", "Должность"),
        ("property", "Номенклатура"),
        ("pod", "Должности_Отправка"),
        ("pod", "Заказ_Отправка"),
        ("parameter", "Период"),
    ]
    apply_service_packets(service, created, page["items"])
    repeated = service.ed_migration_draft(**arguments, accept_single=True)
    assert repeated["operation_count"] == 0


def apply_service_packets(service, created, packet_rows):
    for packet in packet_rows:
        arguments = {
            "project_id": created["project_id"],
            "expected_revision": created["revision"],
            "operations": packet["operations"],
        }
        planned = service.ed_apply(**arguments)
        assert planned["failures"]["total"] == 0, planned
        notices = service.ed_apply(**arguments, section="notices", limit=200)["items"]
        confirmations = [
            {"code": n["code"], "notice_hash": n["notice_hash"]}
            for n in notices
            if n["code"] == "table_part_replace"
        ]
        created = service.ed_apply(
            **arguments,
            mode="apply",
            expected_preview_hash=planned["preview_hash"],
            confirmations=confirmations,
        )
    return created


@pytest.mark.parametrize("case", ["duplicate_file", "duplicate_role"])
def test_tool_wrong_role_refused(migration_service, case):
    service, args, _ = migration_service
    args = deepcopy(args)
    args["rules"][1]["rules_id" if case == "duplicate_file" else "role"] = args["rules"][0][
        "rules_id" if case == "duplicate_file" else "role"
    ]
    with pytest.raises(ValueError):
        service.ed_migration_draft(**args)


def test_tool_two_files_same_configuration_both_roles(migration_service):
    service, args, created = migration_service
    page = service.ed_migration_draft(**args, accept_single=True, section="operations", limit=200)
    applied = apply_service_packets(service, created, page["items"])
    assert applied["revision"] != created["revision"]
    assert service.ed_migration_draft(**args, accept_single=True)["operation_count"] == 0


def test_tool_pages_effect_and_errors(migration_service):
    service, args, _ = migration_service

    async def run():
        async with Client(create_server(service)) as client:
            result = await client.call_tool(
                "ed_migration_draft", {**args, "section": "coverage", "limit": 1}
            )
            assert not result.is_error
            page = result.structured_content
            assert page["has_more"] is True and page["next_offset"] == 1
            result = await client.call_tool(
                "ed_migration_draft",
                {**args, "decisions": [{"origin": "несуществующий", "exclude": True}]},
            )
            assert result.is_error
            assert "invalid_argument" in str(result.content) and "несуществующий" in str(
                result.content
            )
            result = await client.call_tool("ed_migration_draft", {**args, "limit": 201})
            assert result.is_error

    anyio.run(run)
    assert TOOL_EFFECTS["ed_migration_draft"][0] == "read"


def test_inventory_navigation_addresses_match_rules_get(migration_service):
    service, args, _ = migration_service
    rules_id = args["rules"][0]["rules_id"]
    rows = service.rules_list(rules_id, "pko", None, 0, 200)["items"]
    coverage = service.ed_migration_draft(**args, section="coverage", limit=200)["items"]
    for row in rows:
        assert any(e["origin"] == "send:" + row["address"] for e in coverage)
    assert pks_address("Должности", "Код") == "ПКО «Должности» / ПКС Код"
    assert rule_address(service._exchange(rules_id).pko()[0]) == rows[0]["address"]


@pytest.mark.parametrize(
    "uid,fallback,mode",
    [
        (True, False, "ПоУникальномуИдентификатору"),
        (False, False, "ПоПолямПоиска"),
        (True, True, "СначалаПоУникальномуИдентификаторуПотомПоПолямПоиска"),
    ],
)
def test_operations_identification_modes_and_different_format_property(
    synthetic, uid, fallback, mode
):
    node = synthetic[2].pko()[0]
    node.values["СинхронизироватьПоИдентификатору"] = uid
    node.values["ПродолжитьПоискПоПолямПоискаЕслиПоИдентификаторуНеНашли"] = fallback
    result = draft(
        synthetic,
        role="receive",
        accept_single=True,
        decisions=[
            {
                "origin": "receive:ПКО «Должности» / ПКС Код",
                "format_property": "НаименованиеКраткое",
            }
        ],
    )
    pko = next(o for o in operations(result, "pko") if o["patch"]["name"] == "Должности")
    assert pko["patch"]["identification"] == {
        "mode": {"state": "string", "value": mode},
        "search_sets": [] if uid and not fallback else [["Код"]],
    }
    assert (
        next(
            o
            for o in operations(result, "property")
            if o["patch"]["configuration_property"] == "Код"
        )["patch"]["format_property"]
        == "НаименованиеКраткое"
    )
    apply_draft(new_manager(), result)


def test_inventory_search_order_code_is_manual(synthetic):
    synthetic[2].pko()[0].values["ПоследовательностьПолейПоиска"] = 'Строка = "Код";'
    result = draft(synthetic, role="receive", accept_single=True)
    assert entity(result, "Должности", role="receive").state == "ready"
    assert any(t.proposed_location == "АлгоритмПоиска" for t in result.manual)


def test_inventory_unknown_property_container(synthetic):
    synthetic[2].pko()[0].child("Свойства").unknown.append(etree.fromstring(b"<FutureProperty/>"))
    result = draft(synthetic, accept_single=True)
    assert any(
        e.kind == "pks_list" and e.state == "manual" and e.reason == "unsupported"
        for e in result.coverage
    )


@pytest.mark.parametrize("disabled", [False, True])
def test_inventory_disabled_parent_groups(synthetic, disabled):
    rules = synthetic[2]
    group = Node.new("pvd_group", "Группа")
    group.values["Код"] = "Отключенные"
    group.attrs["Отключить"] = disabled
    group.items.append(rules.section("ПравилаВыгрузкиДанных").items.pop(0))
    rules.section("ПравилаВыгрузкиДанных").items.append(group)
    rules.pko()[1].child("Свойства").items[-1].attrs["Отключить"] = disabled
    result = draft(synthetic, accept_single=True)
    assert entity(result, "Должности", "pvd").state == "ready"
    assert entity(result, "Должности").state == "ready"
    assert next(e for e in result.coverage if e.origin.endswith("Товары/Номенклатура")).reason == (
        "parent_excluded" if disabled else "resolved"
    )


def test_decisions_invalid_name_and_repair(synthetic):
    unneeded = draft(
        synthetic,
        accept_single=True,
        decisions=[{"origin": "send:ПКО «Должности»", "name": "ДругоеИмя"}],
    )
    assert entity(unneeded, "Должности").reason == "decision_invalid"
    rules = synthetic[2]
    rules.pko()[0].values["Код"] = "Некорректный-код"
    rules.pvd()[0].values["КодПравилаКонвертации"] = "Некорректный-код"
    rules.pko()[1].child("Свойства").items[1].values["КодПравилаКонвертации"] = "Некорректный-код"
    result = draft(synthetic, accept_single=True)
    row = entity(result, "Некорректный-код")
    assert row.reason == "invalid_name"
    fixed = draft(
        synthetic, accept_single=True, decisions=[{"origin": row.origin, "name": "Корректный"}]
    )
    assert entity(fixed, "Некорректный-код").state == "ready"
    apply_draft(new_manager(), fixed)


def test_candidates_reference_manager_and_enum_values(synthetic):
    original = draft(synthetic, accept_single=True)
    model = apply_draft(new_manager(), original)
    typical = read_manager_text(render(model).text)
    connection, schema, rules = synthetic
    connection.execute(
        "UPDATE objects SET name='Работы',type_name='СправочникСсылка.Работы',"
        "synonym='Другое' WHERE name='Должности'"
    )
    connection.commit()
    for side in ("Источник", "Приемник"):
        rules.pko()[0].values[side] = "СправочникСсылка.Работы"
    # Изменяем только декларацию типового документа, тела остаются непрозрачными.
    ref_rule = typical.pko[0]
    expr = Expr(
        "Метаданные.Справочники.Работы",
        ref_rule.span,
        reference_parts=("Метаданные", "Справочники", "Работы"),
    )
    typical = replace(
        typical,
        pko=(replace(ref_rule, configuration_object=Field("present", expr)), *typical.pko[1:]),
    )
    result = build_migration_draft(
        [("send", rules)],
        connection,
        schema,
        new_manager(),
        reference_document=typical,
        accept_single=True,
    )
    row = entity(result, "Должности")
    assert row.state == "ready" and any(c["confidence"] == "reference" for c in row.candidates)
    enum_row = next(e for e in result.coverage if e.origin.endswith("/Значение/Активен"))
    assert enum_row.state == "ready"


@pytest.mark.parametrize("count", [1, 4])
def test_candidates_more_than_three_are_not_truncated_into_single(synthetic, monkeypatch, count):
    from kd_rules_mcp.authoring.ed.migration_candidates import CandidateContext

    def rows(self, host, role, enumeration):
        return [
            {"format_type": "Справочник.Должности" + (str(i) if i else ""), "auto": False}
            for i in range(count)
        ]

    monkeypatch.setattr(CandidateContext, "object_rows", rows)
    result = draft(synthetic, accept_single=True)
    row = entity(result, "Должности")
    assert row.state == ("ready" if count == 1 else "needs_decision")
    assert len(row.candidates) == min(3, count)


def test_packets_keep_owner_with_properties_and_cross_packet_addresses():
    from kd_rules_mcp.authoring.ed.migration_packets import operation, packets

    entries = []
    for name in ("First", "Second"):
        address = "ПКО/" + name
        entries.append(
            operation(
                "send:" + name, "pko", {"name": name, "directions": ["send"]}, address, group=name
            )
        )
        for index in range(60):
            patch: dict[str, Any] = {
                "configuration_property": f"Field{index}",
                "format_property": f"Field{index}",
            }
            if name == "Second" and index == 0:
                patch.update(property_kind="reference", conversion={"address": "ПКО/First"})
            entries.append(
                operation(
                    f"send:{name}/{index}",
                    "property",
                    patch,
                    address + f"/ПКС/Field{index}",
                    owner_id={"address": address},
                    group=name,
                )
            )
    result = packets(entries, new_manager().revision)
    assert [len(p.operations) for p in result] == [61, 61]
    assert result[1].operations[1]["patch"]["conversion"] == {"address": "ПКО/First"}
    for packet in result:
        for op in packet.operations:
            from kd_rules_mcp.ed.writer_model import digest

            assert op["client_id"].endswith(
                "/" + digest({k: v for k, v in op.items() if k != "client_id"})[:8]
            )
    model = new_manager()
    for packet in result:
        parsed = tuple(parse_operation(o) for o in packet.operations)
        planned = preview(model, parsed, expected_revision=model.revision)
        assert not planned.failures
        model = apply(
            model,
            parsed,
            expected_revision=model.revision,
            expected_preview_hash=planned.preview_hash,
        )


def test_packets_repeat_after_partial_apply(synthetic):
    from kd_rules_mcp.authoring.ed.migration_packets import packets

    model = new_manager()
    result = draft(synthetic, accept_single=True)
    # Отдельный первый пакет с созданными каркасами: тот же сценарий, что после
    # первой страницы большого черновика, без искусственного изменения лимита.
    rows = [o for o in operations(result) if o["kind"] in ("pko", "pkpd", "value_mapping")]
    parsed = tuple(parse_operation(o) for o in rows)
    planned = preview(model, parsed, expected_revision=model.revision)
    assert not planned.failures
    model = apply(
        model, parsed, expected_revision=model.revision, expected_preview_hash=planned.preview_hash
    )
    rest = draft(synthetic, manager=model, accept_single=True)
    assert entity(rest, "Должности").reason == "already_in_manager"
    assert not operations(rest, "pko") and not operations(rest, "pkpd")
    assert all(
        "address" in o["owner_id"]
        for o in operations(rest, "property")
        if o["patch"]["configuration_property"] != "Номенклатура"
    )
    apply_draft(model, rest)
    assert packets([], model.revision) == []


def test_tool_structure_schema_binding_and_reference_notice(migration_service, monkeypatch):
    from kd_rules_mcp.errors import EdAuthoringPreconditionError

    service, args, _ = migration_service
    monkeypatch.setattr(
        service, "_manager_reference", lambda *a, **kw: (None, {"id": "route_missing"})
    )
    result = service.ed_migration_draft(**args, reference_document_id="auto")
    assert any(n["id"] == "reference_manager_unavailable" for n in result["notices"])
    assert any(n["id"] == "side_mismatch" for n in result["notices"])
    opened = service._ed_schemas[args["schema_id"]]
    service._ed_schemas[args["schema_id"]] = replace(opened, format_version="1.21")
    with pytest.raises(EdAuthoringPreconditionError):
        service.ed_migration_draft(**args)
    service._ed_schemas[args["schema_id"]] = opened
    connection = sqlite3.connect(service.store.path("host"))
    connection.execute("UPDATE meta SET value='[\"Other\"]' WHERE key='extensions'")
    connection.commit()
    connection.close()
    with pytest.raises(EdAuthoringPreconditionError):
        service.ed_migration_draft(**args)


def test_enum_values_reference_pair_without_equal_name(synthetic):
    connection, schema, rules = synthetic
    connection.execute("UPDATE object_values SET name='Иное' WHERE name='Активен'")
    connection.commit()
    rules.pko()[2].child("Значения").items[0].values["Источник"] = "Иное"
    rules.pko()[2].child("Значения").items[0].values["Приемник"] = "Другое"
    without = draft(synthetic, accept_single=True)
    assert (
        next(e for e in without.coverage if e.origin.endswith("/Значение/Иное")).reason
        == "no_candidate"
    )
    packet = [
        {
            "client_id": "enum",
            "kind": "pkpd",
            "action": "create",
            "patch": {
                "name": "ТиповыеСтатусы",
                "directions": ["send"],
                "configuration_type": {
                    "state": "reference",
                    "reference_parts": ["Метаданные", "Перечисления", "Статусы"],
                },
                "format_type": {"state": "string", "value": "Статусы"},
            },
        },
        {
            "client_id": "value",
            "kind": "value_mapping",
            "action": "create",
            "owner_id": {"client_id": "enum"},
            "patch": {
                "direction": "send",
                "configuration_value": {
                    "state": "reference",
                    "reference_parts": ["Перечисления", "Статусы", "Иное"],
                },
                "format_value": {"state": "string", "value": "Активен"},
            },
        },
    ]
    model = new_manager()
    parsed = tuple(parse_operation(o) for o in packet)
    planned = preview(model, parsed, expected_revision=model.revision)
    assert not planned.failures
    model = apply(
        model, parsed, expected_revision=model.revision, expected_preview_hash=planned.preview_hash
    )
    typical = read_manager_text(render(model).text)
    result = build_migration_draft(
        [("send", rules)],
        connection,
        schema,
        new_manager(),
        reference_document=typical,
        accept_single=True,
    )
    row = next(e for e in result.coverage if e.origin.endswith("/Значение/Иное"))
    assert row.state == "ready" and row.candidates[0]["reason"] == "так в типовом ПКПД"


@pytest.mark.parametrize("reverse", [False, True])
def test_candidates_table_and_row_reference_aliases(synthetic, reverse):
    connection, schema, rules = synthetic
    model = apply_draft(
        new_manager(), draft(synthetic, role="receive" if reverse else "send", accept_single=True)
    )
    typical = read_manager_text(render(model).text)
    doc = next(r for r in typical.pko if r.name == "Заказ")
    group = doc.groups[0]
    typical = replace(
        typical,
        pko=tuple(
            replace(
                r,
                groups=(
                    replace(
                        group,
                        configuration_property="Состав",
                        properties=tuple(
                            replace(p, configuration_property="Артикул") for p in group.properties
                        ),
                    ),
                ),
            )
            if r == doc
            else r
            for r in typical.pko
        ),
    )
    ident = connection.execute(
        "SELECT id FROM objects WHERE kind='Документ' AND name='Заказ'"
    ).fetchone()[0]
    connection.execute(
        "UPDATE properties SET name='Состав',path='Состав' "
        "WHERE object_id=? AND kind='ТабличнаяЧасть'",
        (ident,),
    )
    connection.execute(
        "UPDATE properties SET name='Артикул',path='Состав.Артикул' "
        "WHERE object_id=? AND path='Товары.Номенклатура'",
        (ident,),
    )
    connection.commit()
    node = rules.pko()[1].child("Свойства").items[-1]
    for side in ("Источник", "Приемник"):
        node.child(side).attrs["Имя"] = "Состав"
        node.items[0].child(side).attrs["Имя"] = "Артикул"
    result = build_migration_draft(
        [("send", rules)],
        connection,
        schema,
        new_manager(),
        reference_document={"send": typical},
        accept_single=True,
    )
    assert next(e for e in result.coverage if e.kind == "pks_group").state == "ready"
    column = next(e for e in result.coverage if e.origin.endswith("Состав/Артикул"))
    assert column.state == "ready"
    assert ("другого направления" in column.candidates[0]["reason"]) == reverse
    apply_draft(new_manager(), result)


def test_operations_parameter_name_can_equal_rule_name(synthetic):
    synthetic[2].section("Параметры").items[0].attrs["Имя"] = "Должности"
    result = draft(synthetic, accept_single=True)
    assert operations(result, "parameter")[0]["patch"]["name"] == "Должности"
    apply_draft(new_manager(), result)


def test_decisions_receive_type_conflict_and_exclusion(synthetic):
    rules = synthetic[2]
    duplicate = deepcopy(rules.pko()[0])
    duplicate.values["Код"] = "Должности2"
    rules.section("ПравилаКонвертацииОбъектов").items.append(duplicate)
    pvd = deepcopy(rules.pvd()[0])
    pvd.values["Код"] = "Должности2"
    pvd.values["КодПравилаКонвертации"] = "Должности2"
    rules.section("ПравилаВыгрузкиДанных").items.append(pvd)
    result = draft(synthetic, role="receive", accept_single=True)
    assert entity(result, "Должности", role="receive").state == "ready"
    assert entity(result, "Должности2", role="receive").state == "ready"
    pod = next(e for e in result.coverage if e.origin == "receive:ПОД «Справочник.Должности»")
    assert (pod.kind, pod.state) == ("pod_receive", "manual")
    assert (
        next(t for t in result.manual if t.origin == pod.origin).proposed_location == "ПриОбработке"
    )
    assert not any(
        o["patch"].get("format_selection", {}).get("value") == "Справочник.Должности"
        for o in operations(result, "pod")
    )
    apply_draft(new_manager(), result)
    fixed = draft(
        synthetic,
        role="receive",
        accept_single=True,
        decisions=[{"origin": "receive:ПКО «Должности2»", "exclude": True}],
    )
    assert entity(fixed, "Должности", role="receive").state == "ready"
    apply_draft(new_manager(), fixed)


def test_packets_receive_pod_name_conflict(synthetic):
    model = new_manager()
    parsed = (
        parse_operation(
            {
                "client_id": "occupied",
                "kind": "pod",
                "action": "create",
                "patch": {
                    "name": "Должности_Получение",
                    "directions": ["receive"],
                    "format_selection": {"state": "string", "value": "ДругойТип"},
                },
            }
        ),
    )
    planned = preview(model, parsed, expected_revision=model.revision)
    assert not planned.failures
    model = apply(
        model, parsed, expected_revision=model.revision, expected_preview_hash=planned.preview_hash
    )
    result = draft(synthetic, manager=model, role="receive", accept_single=True)
    pko = entity(result, "Должности", role="receive")
    assert (pko.state, pko.reason) == ("blocked", "dependency")
    assert pko.blocked_by == ["receive:ПОД «Справочник.Должности»"]
    assert not any(o["patch"]["name"] == "Должности" for o in operations(result, "pko"))


@pytest.mark.parametrize("first_both", [False, True])
def test_packets_repeat_with_changed_file_set(synthetic, first_both):
    rules = synthetic[2]
    both = [("send", rules), ("receive", deepcopy(rules))]
    first = draft(synthetic, rules=both if first_both else [("send", rules)], accept_single=True)
    model = apply_draft(new_manager(), first)
    repeated = draft(
        synthetic,
        rules=[("send", rules)] if first_both else both,
        manager=model,
        accept_single=True,
    )
    assert all(
        e.reason == "already_in_manager"
        for e in repeated.coverage
        if e.kind == "pko" and e.role == "send"
    )
    assert not any(
        o["patch"]["directions"] == ["send"]
        for o in operations(repeated, "pko") + operations(repeated, "pod")
    )
    assert entity(repeated, "Должности", "pvd").reason == "already_in_manager"
    applied = apply_draft(model, repeated)
    assert sum("send" in r.directions for r in applied.pko) == 2


@pytest.mark.parametrize("role", ["send", "receive"])
def test_packets_existing_arbitrary_names_and_reference_addresses(synthetic, role):
    model = apply_draft(
        new_manager(),
        draft(
            synthetic,
            role=role,
            accept_single=True,
            decisions=[{"origin": role + ":ПКО «Заказ»", "exclude": True}],
        ),
    )
    source = synthetic[2]
    source.pko()[0].values["Код"] = "ДругоеИмя"
    source.pvd()[0].values["КодПравилаКонвертации"] = "ДругоеИмя"
    source.pko()[1].child("Свойства").items[1].values["КодПравилаКонвертации"] = "ДругоеИмя"
    repeated = draft(synthetic, role=role, manager=model, accept_single=True)
    assert entity(repeated, "ДругоеИмя", role=role).reason == "already_in_manager"
    prop = next(
        o
        for o in operations(repeated, "property")
        if o["patch"]["configuration_property"] == "Должность"
    )
    assert prop["patch"]["conversion"] == {"address": "ПКО/Должности"}
    assert not any("Должности" in o["patch"]["name"] for o in operations(repeated, "pod"))
    apply_draft(model, repeated)


@pytest.mark.parametrize("problem", ["clean", "excluded", "candidate", "handler", "blocked"])
def test_operations_receive_search_field_not_transferred(synthetic, problem):
    prop = synthetic[2].pko()[0].child("Свойства").items[0]
    decisions = []
    if problem == "excluded":
        decisions = [{"origin": "receive:ПКО «Должности» / ПКС Код", "exclude": True}]
    elif problem == "candidate":
        prop.child("Приемник").attrs["Имя"] = "Заметка"
    elif problem == "handler":
        # Событие без установленной стороны нельзя автоматически отбросить.
        prop.unknown.append(etree.fromstring(b"<UnknownHandler/>"))
    elif problem == "blocked":
        prop.values["КодПравилаКонвертации"] = "Статусы"
        decisions = [{"origin": "receive:ПКО «Статусы»", "exclude": True}]
    result = draft(synthetic, role="receive", accept_single=True, decisions=decisions)
    pko = entity(result, "Должности", role="receive")
    if problem == "clean":
        assert pko.state == "ready"
    elif problem == "excluded":
        assert pko.state == "manual"
        assert "Поиск без полей" in pko.message
    else:
        assert pko.state == "blocked"
        assert pko.blocked_by == [
            "receive:ПКО «Должности» / ПКС " + ("Заметка" if problem == "candidate" else "Код")
        ]
        assert not any(o["patch"].get("name") == "Должности" for o in operations(result, "pko"))
        assert entity(result, ("Должности", "Наименование"), "pks", "receive").state == "blocked"
        assert next(
            e for e in result.coverage if e.origin == "receive:ПОД «Справочник.Должности»"
        ).blocked_by == [pko.origin]
    apply_draft(new_manager(), result)


@pytest.mark.parametrize("kind", ["Свойство", "Реквизит", "СтандартныйРеквизит", "НеизвестныйВид"])
def test_operations_search_reference_without_uid_is_manual(synthetic, kind):
    # Без синхронизации по УИД поле «Ссылка» ищется общим запросом (БСП:3934), не по УИД.
    node = synthetic[2].pko()[0]
    node.values["СинхронизироватьПоИдентификатору"] = False
    prop = node.child("Свойства").items[0]
    for side in ("Источник", "Приемник"):
        prop.child(side).attrs.update(Имя="Ссылка", Вид=kind)
    result = draft(synthetic, role="receive", accept_single=True)
    assert not any(o["patch"].get("name") == "Должности" for o in operations(result, "pko"))
    row = entity(result, ("Должности", "Ссылка"), "pks", "receive")
    assert row.state == "manual"
    assert any(t.origin == row.origin for t in result.manual)
    pko = entity(result, "Должности", "pko", "receive")
    assert (pko.state, pko.blocked_by) == ("blocked", [row.origin])
    apply_draft(new_manager(), result)


@pytest.mark.parametrize("kind", ["Свойство", "Реквизит", "СтандартныйРеквизит", "НеизвестныйВид"])
def test_operations_search_reference_is_uid(synthetic, kind):
    node = synthetic[2].pko()[0]
    node.values["СинхронизироватьПоИдентификатору"] = True
    node.values.pop("ПродолжитьПоискПоПолямПоискаЕслиПоИдентификаторуНеНашли", None)
    prop = node.child("Свойства").items[0]
    for side in ("Источник", "Приемник"):
        prop.child(side).attrs.update(Имя="Ссылка", Вид=kind)
    result = draft(synthetic, role="receive", accept_single=True)
    patch = next(o["patch"] for o in operations(result, "pko") if o["patch"]["name"] == "Должности")
    assert patch["identification"] == {
        "mode": {"state": "string", "value": "ПоУникальномуИдентификатору"},
        "search_sets": [],
    }
    row = entity(result, ("Должности", "Ссылка"), "pks", "receive")
    assert (row.state, row.reason) == ("excluded", "identification")
    assert not any(t.origin == row.origin for t in result.manual)
    apply_draft(new_manager(), result)


@pytest.mark.parametrize("required", [False, True])
def test_operations_required_property_is_search_field(synthetic, required):
    prop = synthetic[2].pko()[0].child("Свойства").items[1]
    prop.attrs["Обязательное"] = required
    result = draft(synthetic, role="receive", accept_single=True)
    patch = next(o["patch"] for o in operations(result, "pko") if o["patch"]["name"] == "Должности")
    assert patch["identification"]["search_sets"] == [
        ["Код", "Наименование"] if required else ["Код"]
    ]
    apply_draft(new_manager(), result)


@pytest.mark.parametrize(
    "kind,field,attribute,value",
    [
        ("pko", "ПриоритетОбъектовОбмена", False, "Ниже"),
        ("pko", "НеЗамещать", False, True),
        ("pko", "НоваяПолитика", True, "Особая"),
        ("pvd", "НеВыгружатьОбъектыСозданныеВБазеПриемнике", False, True),
        ("pvd", "ВыбиратьДанныеДляВыгрузкиОднимЗапросом", False, True),
        ("pvd", "НоваяПолитика", True, "Особая"),
        ("parameter", "ПередаватьПараметрПриВыгрузке", True, True),
        ("parameter", "ПравилоКонвертации", True, "Должности"),
        ("parameter", "ТипЗначения", True, "Строка"),
        ("parameter", "НоваяПолитика", True, "Особая"),
    ],
)
@pytest.mark.parametrize("present", [False, True])
def test_inventory_untransferred_policies_keep_skeleton(
    synthetic, kind, field, attribute, value, present
):
    rules = synthetic[2]
    node = (
        rules.pko()[0]
        if kind == "pko"
        else rules.pvd()[0]
        if kind == "pvd"
        else rules.root.child("Параметры").items[0]
    )
    if present:
        (node.attrs if attribute else node.values)[field] = value
    result = draft(synthetic, accept_single=True)
    row = next(
        e
        for e in result.coverage
        if e.kind == kind and (e.origin.endswith("«Должности»") or kind == "parameter")
    )
    assert row.state == "ready"
    policies = [
        e
        for e in result.coverage
        if e.kind == "policy" and e.origin == row.origin + "/Политика/" + field
    ]
    assert len(policies) == int(present)
    if present:
        opposite = field == "НеЗамещать"
        assert policies[0].state == ("excluded" if opposite else "manual")
        assert (
            any(t.origin == policies[0].origin and t.event == field for t in result.manual)
            != opposite
        )
    apply_draft(new_manager(), result)


@pytest.mark.parametrize("present", [False, True])
def test_inventory_search_variants_policy(synthetic, present):
    if present:
        variants = Node.new("search_variants", "НастройкаВариантовПоискаОбъектов")
        variant = Node.new("search_variant", "ВариантПоиска")
        variant.values["ИмяНастройкиДляАлгоритма"] = "ПоКоду"
        variants.items.append(variant)
        synthetic[2].pko()[0].children[variants.tag] = variants
    result = draft(synthetic, role="receive", accept_single=True)
    assert entity(result, "Должности", role="receive").state == "ready"
    assert sum(
        e.kind == "policy" and e.origin.endswith("/НастройкаВариантовПоискаОбъектов")
        for e in result.coverage
    ) == int(present)


@pytest.mark.parametrize("explicit", [False, True])
def test_unreachable_explicit_type_asserts_code_usage(synthetic, explicit):
    rules = synthetic[2]
    rules.section("ПравилаВыгрузкиДанных").items.pop(0)
    rules.pko()[1].child("Свойства").items.pop(1)
    result = draft(
        synthetic,
        accept_single=True,
        decisions=[{"origin": "send:ПКО «Должности»", "format_type": "Справочник.Должности"}]
        if explicit
        else [],
    )
    assert entity(result, "Должности").state == ("ready" if explicit else "excluded")
    assert any(o["patch"]["name"] == "Должности" for o in operations(result, "pko")) == explicit


@pytest.mark.parametrize("role", ["send", "receive"])
@pytest.mark.parametrize("different", [False, True])
def test_enum_values_candidates_from_both_sides(synthetic, role, different):
    if different:
        synthetic[2].pko()[2].child("Значения").items[0].values["Приемник"] = "Закрыт"
    result = draft(synthetic, role=role, accept_single=True)
    row = next(e for e in result.coverage if e.kind == "pkz")
    assert row.state == ("needs_decision" if different else "ready")
    assert {r["format_value"] for r in row.candidates} == (
        {"Активен", "Закрыт"} if different else {"Активен"}
    )
    if different:
        assert row.reason == "several_candidates"


@pytest.mark.parametrize("present", [False, True])
def test_inventory_predefined_values_keep_object_skeleton(synthetic, present):
    if present:
        values = Node.new("pkz_list", "Значения")
        value = Node.new("pkz", "Значение")
        value.values.update(Источник="Директор", Приемник="Директор")
        values.items.append(value)
        synthetic[2].pko()[0].children["Значения"] = values
    result = draft(synthetic, accept_single=True)
    assert entity(result, "Должности").state == "ready"
    assert sum(e.kind == "pkz" and e.state == "manual" for e in result.coverage) == int(present)
    apply_draft(new_manager(), result)


@pytest.mark.parametrize("wrong", [False, True])
def test_operations_pvd_selection_matches_pko_object(synthetic, wrong):
    if wrong:
        synthetic[2].pvd()[0].values["ОбъектВыборки"] = "ДокументСсылка.Заказ"
    result = draft(synthetic, accept_single=True)
    assert entity(result, "Должности", "pvd").state == ("manual" if wrong else "ready")
    assert (
        any(o["patch"]["name"] == "Должности_Отправка" for o in operations(result, "pod")) != wrong
    )


@pytest.mark.parametrize("kind", ["ТабличнаяЧасть", "Реквизит", ""])
def test_operations_group_requires_table_part_kind(synthetic, kind):
    group = synthetic[2].pko()[1].child("Свойства").items[-1]
    for side in ("Источник", "Приемник"):
        group.child(side).attrs["Вид"] = kind
    result = draft(synthetic, accept_single=True)
    assert entity(result, ("Заказ", "Товары"), "pks_group").state == (
        "ready" if kind == "ТабличнаяЧасть" else "manual"
    )
    assert len(operations(result, "table_part")) == int(kind == "ТабличнаяЧасть")


@pytest.mark.parametrize(
    "origin,wrong_field",
    [
        ("send:ПКО «Должности»/ПриВыгрузке", "format_type"),
        ("send:ПКО «Должности»/Политика/НеЗамещать", "format_property"),
        ("send:ПВД «Должности»", "format_type"),
        ("send:параметр «Период»", "format_value"),
        ("send:ПКО «Должности» / ПКС Код", "format_type"),
        ("send:ПКО «Статусы»/Значение/Активен", "format_property"),
    ],
)
@pytest.mark.parametrize("invalid", [False, True])
def test_decisions_wrong_entity_kind(synthetic, origin, wrong_field, invalid):
    synthetic[2].pko()[0].values.update(ПриВыгрузке="Тело();", НеЗамещать=True)
    result = draft(
        synthetic,
        accept_single=True,
        decisions=[{"origin": origin, wrong_field: "Неверно"}] if invalid else [],
    )
    row = next(e for e in result.coverage if e.origin == origin)
    assert (row.reason == "decision_invalid") == invalid
    if invalid:
        assert not any(t.origin == origin for t in result.manual)


@pytest.mark.parametrize(
    "event,location",
    [
        ("ПередОбработкойПравила", "ПриОбработке"),
        ("ПередВыгрузкойОбъекта", "ПриОбработке"),
        ("ПослеВыгрузкиОбъекта", "нет прямого соответствия"),
        ("ПослеОбработкиПравила", "нет прямого соответствия"),
        ("СпособОтбораДанных", "нет прямого соответствия"),
    ],
)
def test_inventory_pvd_manual_actual_event(synthetic, event, location):
    synthetic[2].pvd()[0].values[event] = (
        "ПроизвольныйКод" if event != "СпособОтбораДанных" else "ПроизвольныйАлгоритм"
    )
    result = draft(synthetic, accept_single=True)
    task = next(t for t in result.manual if t.origin == "send:ПВД «Должности»")
    assert (task.event, task.proposed_location) == (event, location)


def test_inventory_duplicate_addresses_are_local_and_stable(synthetic):
    rules = synthetic[2]
    values = rules.pko()[2].child("Значения")
    values.items.append(deepcopy(values.items[0]))
    group = rules.pko()[1].child("Свойства").items[-1]
    rules.pko()[1].child("Свойства").items.append(deepcopy(group))
    first = draft(synthetic, accept_single=True)
    added = deepcopy(rules.pko()[0])
    added.values["Код"] = "Постороннее"
    rules.section("ПравилаКонвертацииОбъектов").items.insert(0, added)
    second = draft(synthetic, accept_single=True)
    duplicates = {e.origin for e in first.coverage if "/ #" in e.origin}
    assert duplicates == {e.origin for e in second.coverage if "/ #" in e.origin}
    assert "send:ПКО «Статусы»/Значение/Активен / #2" in duplicates
    groups = [e.origin for e in first.coverage if e.kind == "pks_group"]
    assert len(groups) == len(set(groups)) == 2
    assert set(groups) <= {e.origin for e in second.coverage}


@pytest.mark.parametrize("excluded", [False, True])
def test_inventory_enum_properties_and_excluded_children(synthetic, excluded):
    enum = synthetic[2].pko()[2]
    enum.children["Свойства"] = deepcopy(synthetic[2].pko()[1].child("Свойства"))
    result = draft(
        synthetic,
        accept_single=True,
        decisions=[{"origin": "send:ПКО «Статусы»", "exclude": True}] if excluded else [],
    )
    children = [
        e
        for e in result.coverage
        if e.parent == "send:ПКО «Статусы»" and e.kind in ("pks", "pks_group", "pkz")
    ]
    assert all(
        e.state == "excluded" and e.reason == ("parent_excluded" if excluded else "not_applicable")
        for e in children
        if e.kind != "pkz" or excluded
    )
    assert all(e.state == "ready" for e in children if e.kind == "pkz" and not excluded)


@pytest.mark.parametrize("role", ["send", "receive"])
@pytest.mark.parametrize(
    "event,side",
    [
        ("ПриВыгрузке", "send"),
        ("ПриЗагрузке", "receive"),
        ("ПоследовательностьПолейПоиска", "receive"),
    ],
)
def test_inventory_handler_sides_and_search_order(synthetic, role, event, side):
    synthetic[2].pko()[0].values[event] = "Обработчик();"
    result = draft(synthetic, role=role, accept_single=True)
    row = next(e for e in result.coverage if e.kind == "handler")
    assert (row.state, row.reason) == (
        ("manual", "handler") if role == side else ("excluded", "opposite_role")
    )
    assert any(t.origin == row.origin for t in result.manual) == (role == side)
    assert entity(result, "Должности", role=role).state == "ready"
    apply_draft(new_manager(), result)


def test_inventory_unknown_handler_side_is_manual(synthetic):
    synthetic[2].root.values["ПослеЗагрузкиПравилОбмена"] = "Обработчик();"
    for role in ("send", "receive"):
        result = draft(synthetic, role=role, accept_single=True)
        assert next(e for e in result.coverage if e.kind == "handler").state == "manual"


@pytest.mark.parametrize("role", ["send", "receive"])
def test_operations_property_handler_only_blocks_its_side(synthetic, role):
    synthetic[2].pko()[0].child("Свойства").items[1].values["ПриВыгрузке"] = "Изменить();"
    result = draft(synthetic, role=role, accept_single=True)
    assert entity(result, ("Должности", "Наименование"), "pks", role).state == (
        "manual" if role == "send" else "ready"
    )
    apply_draft(new_manager(), result)


@pytest.mark.parametrize("blocked", [False, True])
def test_operations_states_agree_with_emitted_operations(synthetic, monkeypatch, blocked):
    from kd_rules_mcp.authoring.ed import migration

    original = migration.operation
    origins = set()

    def record(origin, *args, **kwargs):
        origins.add(origin)
        return original(origin, *args, **kwargs)

    monkeypatch.setattr(migration, "operation", record)
    result = draft(
        synthetic,
        role="receive",
        accept_single=True,
        decisions=[{"origin": "receive:ПКО «Должности» / ПКС Код", "exclude": True}]
        if blocked
        else [],
    )
    assert origins == {e.origin for e in result.coverage if e.state == "ready"}
    assert sum(result.summary()["states"].values()) == len(result.coverage)
    apply_draft(new_manager(), result)


def manager_with_pod(name, role, **selection):
    model = new_manager()
    rows = (
        parse_operation(
            {
                "client_id": "occupied",
                "kind": "pod",
                "action": "create",
                "patch": {"name": name, "directions": [role], **selection},
            }
        ),
    )
    planned = preview(model, rows, expected_revision=model.revision)
    assert not planned.failures
    return apply(
        model, rows, expected_revision=model.revision, expected_preview_hash=planned.preview_hash
    )


@pytest.mark.parametrize("conflict", [False, True])
def test_packets_pvd_name_decision_requires_conflict(synthetic, conflict):
    model = (
        manager_with_pod(
            "Должности_Отправка",
            "send",
            configuration_selection={
                "state": "reference",
                "reference_parts": ["Метаданные", "Документы", "Заказ"],
            },
        )
        if conflict
        else new_manager()
    )
    result = draft(
        synthetic,
        manager=model,
        accept_single=True,
        decisions=[{"origin": "send:ПВД «Должности»", "name": "СвободноеИмя"}],
    )
    row = entity(result, "Должности", "pvd")
    assert (row.state, row.reason) == (
        ("ready", "resolved") if conflict else ("needs_decision", "decision_invalid")
    )
    apply_draft(model, result)


@pytest.mark.parametrize("direction", ["send", "receive"])
def test_packets_pod_name_conflict_any_direction(synthetic, direction):
    # Процедура «ДобавитьПОД_<Наименование>» одна на модуль при любом направлении
    # (reference/kd3-cfg/DataProcessors/ВыгрузкаМодуля/Ext/ObjectModule.bsl:1587).
    model = manager_with_pod(
        "Должности_Отправка",
        direction,
        format_selection={"state": "string", "value": "ДругойТип"},
    )
    result = draft(synthetic, manager=model, accept_single=True)
    assert entity(result, "Должности", "pvd").reason == "name_conflict"
    apply_draft(model, result)
    chosen = draft(
        synthetic,
        manager=model,
        accept_single=True,
        decisions=[{"origin": "send:ПВД «Должности»", "name": "Должности_ОтправкаДругая"}],
    )
    assert entity(chosen, "Должности", "pvd").state == "ready"
    apply_draft(model, chosen)


def test_packets_pvd_unneeded_name_cannot_break_receive_pko(synthetic):
    result = draft(
        synthetic,
        rules=[("send", synthetic[2]), ("receive", deepcopy(synthetic[2]))],
        accept_single=True,
        decisions=[{"origin": "send:ПВД «Должности»", "name": "Должности_Получение_Получение"}],
    )
    assert entity(result, "Должности", "pvd").reason == "decision_invalid"
    assert entity(result, "Должности", role="receive").state == "ready"
    assert any(
        o["patch"]["name"] == "Должности_Получение_Получение"
        and o["patch"]["directions"] == ["receive"]
        for o in operations(result, "pod")
    )
    apply_draft(new_manager(), result)


def test_packets_existing_receive_pod_matches_type_only(synthetic):
    model = manager_with_pod(
        "ПроизвольныйПОД",
        "receive",
        format_selection={"state": "string", "value": "Справочник.Должности"},
    )
    result = draft(synthetic, manager=model, role="receive", accept_single=True)
    row = next(e for e in result.coverage if e.origin == "receive:ПОД «Справочник.Должности»")
    assert (row.state, row.reason) == ("manual", "unsupported")
    task = next(t for t in result.manual if t.origin == row.origin)
    assert task.proposed_location == "ИспользуемыеПКО"
    assert "добавить ПКО в ПОД ПОД/ПроизвольныйПОД" in task.message
    assert not any(
        o["patch"].get("format_selection", {}).get("value") == "Справочник.Должности"
        for o in operations(result, "pod")
    )
    assert entity(result, "Должности", role="receive").state == "ready"
    apply_draft(model, result)


def test_packets_receive_pod_conflict_name_decision(synthetic):
    model = manager_with_pod(
        "Должности_Получение", "receive", format_selection={"state": "string", "value": "ДругойТип"}
    )
    result = draft(
        synthetic,
        manager=model,
        role="receive",
        accept_single=True,
        decisions=[{"origin": "receive:ПОД «Справочник.Должности»", "name": "СвободныйПОД"}],
    )
    assert entity(result, "Должности", role="receive").state == "ready"
    assert any(o["patch"]["name"] == "СвободныйПОД" for o in operations(result, "pod"))
    apply_draft(model, result)


@pytest.mark.parametrize("role", ["send", "receive"])
@pytest.mark.parametrize("limit", [3, 4, 100])
def test_packets_table_address_uses_format_property(synthetic, tmp_path, monkeypatch, role, limit):
    from kd_rules_mcp.authoring.ed import migration_packets

    path = tmp_path / "renamed.bin"
    path.write_bytes((DATA / "ed/writer/format.bin").read_bytes())
    extend_schema(path)
    path.write_text(
        path.read_text("utf-8").replace(
            '<property name="Товары" type="t:Товары"/>',
            '<property name="ТоварыФ" type="t:Товары"/>',
        ),
        encoding="utf-8",
    )
    schema = load_schema(path, locate_import=lambda _: DATA / "ed/writer/message.bin")
    monkeypatch.setattr(migration_packets, "MAX_OPERATIONS", limit)
    result = draft(
        (synthetic[0], schema, synthetic[2]),
        role=role,
        accept_single=True,
        decisions=[{"origin": f"{role}:ПКО «Заказ» / ПКС Товары", "format_property": "ТоварыФ"}],
    )
    if limit < 100:
        assert len(result.packets) > 1
    model = apply_draft(new_manager(), result)
    group = next(p for p in model.pko if p.name == "Заказ").groups[0]
    assert (group.name, group.configuration_property, group.format_property) == (
        "ТоварыФ",
        "Товары",
        "ТоварыФ",
    )
    assert [(p.configuration_property, p.format_property) for p in group.properties] == [
        ("Номенклатура", "Номенклатура")
    ]
    repeated = draft(
        (synthetic[0], schema, synthetic[2]),
        manager=model,
        role=role,
        accept_single=True,
        decisions=[{"origin": f"{role}:ПКО «Заказ» / ПКС Товары", "format_property": "ТоварыФ"}],
    )
    assert not operations(repeated)


@pytest.mark.parametrize("duplicate", [False, True])
@pytest.mark.parametrize("role", ["send", "receive"])
def test_packets_two_tables_keep_own_columns(synthetic, tmp_path, duplicate, role):
    from kd_rules_mcp.validation.address import walk_pks

    connection, _, rules = synthetic
    table = connection.execute("SELECT id FROM properties WHERE path='Товары'").fetchone()[0]
    add_prop(connection, "Документ", "Заказ", "Реквизит", "Цена", ("Строка",), parent=table)
    connection.commit()
    path = tmp_path / "two-tables.bin"
    path.write_bytes((DATA / "ed/writer/format.bin").read_bytes())
    extend_schema(path)
    text = (
        path.read_text("utf-8")
        .replace(
            '<objectType name="СтрокаТоваров">',
            '<objectType name="СтрокаТоваров"><property name="Цена" type="xs:string"/>',
        )
        .replace(
            '<property name="Товары" type="t:Товары"/>',
            '<property name="Товары" type="t:Товары"/><property name="ДопТовары" type="t:Товары"/>',
        )
    )
    path.write_text(text, encoding="utf-8")
    schema = load_schema(path, locate_import=lambda _: DATA / "ed/writer/message.bin")
    props = rules.pko()[1].child("Свойства")
    if duplicate:
        second = deepcopy(props.items[-1])
        second.child("Приемник" if role == "send" else "Источник").attrs["Имя"] = "ДопТовары"
        for side in ("Источник", "Приемник"):
            second.items[0].child(side).attrs["Имя"] = "Цена"
        props.items.append(second)
    decisions = []
    for path, prop in walk_pks(props):
        if not prop.is_group:
            continue
        side = prop.child("Приемник" if role == "send" else "Источник")
        assert side is not None
        decisions.append(
            {
                "origin": role + ":" + pks_address("Заказ", path),
                "format_property": side.attrs["Имя"],
            }
        )
    result = draft((connection, schema, rules), role=role, accept_single=True, decisions=decisions)
    notices = preview(
        new_manager(),
        tuple(parse_operation(o) for o in operations(result)),
        expected_revision=new_manager().revision,
    ).notices
    assert any(n.code == "table_part_duplicate" for n in notices) == (
        duplicate and role == "receive"
    )
    model = apply_draft(
        new_manager(), result, notice_codes=("table_part_replace", "table_part_duplicate")
    )
    groups = next(p for p in model.pko if p.name == "Заказ").groups
    actual = {g.name: [p.configuration_property for p in g.properties] for g in groups}
    expected = {"Товары": ["Номенклатура"]}
    if duplicate:
        expected["ДопТовары"] = ["Цена"]
    assert actual == expected


@pytest.mark.parametrize("flag", ["Поиск", "Обязательное"])
@pytest.mark.parametrize("collision", [False, True])
@pytest.mark.parametrize("marked", [False, True])
def test_operations_search_ignores_table_columns(synthetic, flag, collision, marked):
    connection, _, rules = synthetic
    node = rules.pko()[1]
    node.values["СинхронизироватьПоИдентификатору"] = False
    props = node.child("Свойства")
    props.items[0].attrs["Поиск"] = True
    column = props.items[-1].items[0]
    column.attrs[flag] = marked
    if collision:
        for side in ("Источник", "Приемник"):
            column.child(side).attrs["Имя"] = "Номер"
        table = connection.execute("SELECT id FROM properties WHERE path='Товары'").fetchone()[0]
        add_prop(connection, "Документ", "Заказ", "Реквизит", "Номер", ("Строка",), parent=table)
        connection.commit()
    result = draft(synthetic, role="receive", accept_single=True)
    assert entity(result, "Заказ", role="receive").state == "ready"
    patch = next(o["patch"] for o in operations(result, "pko") if o["patch"]["name"] == "Заказ")
    assert patch["identification"]["search_sets"] == [["Номер"]]
    apply_draft(new_manager(), result)


@pytest.mark.parametrize("membership", ["empty", "other", "logical_id", "address"])
def test_packets_existing_receive_pod_requires_used_pko(synthetic, membership):
    from kd_rules_mcp.ed.writer_model import Reference, model_addresses

    model = apply_draft(new_manager(), draft(synthetic, role="receive", accept_single=True))
    pko = next(p for p in model.pko if p.name == "Должности")
    other = next(p for p in model.pko if p.name == "Заказ")
    refs = {
        "empty": (),
        "other": (Reference("pko", other.logical_id, other.name, "resolved"),),
        "logical_id": (Reference("pko", pko.logical_id, pko.name, "resolved"),),
        "address": (Reference("pko", name=model_addresses(model)[pko.logical_id]),),
    }[membership]
    model = replace(
        model,
        pod=tuple(
            replace(p, used_pko=refs) if p.format_selection.value == "Справочник.Должности" else p
            for p in model.pod
        ),
    )
    result = draft(synthetic, manager=model, role="receive", accept_single=True)
    row = next(e for e in result.coverage if e.origin == "receive:ПОД «Справочник.Должности»")
    assert (row.state, row.reason) == (
        ("manual", "unsupported")
        if membership in ("empty", "other")
        else ("excluded", "already_in_manager")
    )
    assert not operations(result, "pod")
    if row.state == "manual":
        task = next(t for t in result.manual if t.origin == row.origin)
        assert task.proposed_location == "ИспользуемыеПКО"
        assert "добавить ПКО в ПОД ПОД/Должности_Получение" in task.message


@pytest.mark.parametrize("role", ["send", "receive"])
@pytest.mark.parametrize(
    "flag,side",
    [
        ("НеЗамещать", "receive"),
        ("ПоискПоДатеНаРавенство", "receive"),
        ("ПолучитьИзВходящихДанных", "send"),
        ("ПриводитьКДлине", "send"),
    ],
)
def test_inventory_property_flag_sides(synthetic, role, flag, side):
    prop = synthetic[2].pko()[0].child("Свойства").items[1]
    prop.values[flag] = True
    result = draft(synthetic, role=role, accept_single=True)
    row = entity(result, ("Должности", "Наименование"), "pks", role)
    policy = next(e for e in result.coverage if e.origin == row.origin + "/Политика/" + flag)
    opposite = side is not None and side != role
    assert row.state == ("ready" if opposite else "manual")
    assert (policy.state, policy.reason) == (
        ("excluded", "opposite_role") if opposite else ("manual", "unsupported")
    )
    assert any(t.origin == policy.origin for t in result.manual) != opposite
    apply_draft(new_manager(), result)


@pytest.mark.parametrize("role", ["send", "receive"])
@pytest.mark.parametrize("blank", [False, True])
def test_inventory_parameter_property_opposite_role(synthetic, role, blank):
    prop = synthetic[2].pko()[0].child("Свойства").items[1]
    prop.values["ИмяПараметраДляПередачи"] = "Имя"
    if blank:
        prop.children.pop("Приемник")
    result = draft(synthetic, role=role, accept_single=True)
    policy = next(
        e
        for e in result.coverage
        if e.kind == "policy" and e.origin.endswith("/Политика/ИмяПараметраДляПередачи")
    )
    row = next(e for e in result.coverage if e.origin == policy.parent)
    assert (row.state, row.reason) == (
        ("excluded", "opposite_role") if role == "receive" else ("manual", "unsupported")
    )
    if role == "receive":
        assert not any(t.origin == row.origin for t in result.manual)
    apply_draft(new_manager(), result)


@pytest.mark.parametrize("role", ["send", "receive"])
@pytest.mark.parametrize(
    "field", ["НеСоздаватьЕслиНеНайден", "НеЗапоминатьВыгруженные", "НовыйФлаг"]
)
def test_inventory_pko_flag_sides(synthetic, role, field):
    synthetic[2].pko()[0].attrs[field] = True
    result = draft(synthetic, role=role, accept_single=True)
    row = entity(result, "Должности", role=role)
    policy = next(e for e in result.coverage if e.origin == row.origin + "/Политика/" + field)
    opposite = (field == "НеСоздаватьЕслиНеНайден" and role == "send") or (
        field == "НеЗапоминатьВыгруженные" and role == "receive"
    )
    assert row.state == "ready"
    assert policy.state == ("excluded" if opposite else "manual")
    assert any(t.origin == policy.origin for t in result.manual) != opposite


@pytest.mark.parametrize("role", ["send", "receive"])
@pytest.mark.parametrize("field", ["НеВыгружатьОбъектыСозданныеВБазеПриемнике", "НовыйФлаг"])
def test_inventory_pvd_policy_sides(synthetic, role, field):
    synthetic[2].pvd()[0].attrs[field] = True
    result = draft(synthetic, role=role, accept_single=True)
    parent = entity(result, "Должности", "pvd", role)
    row = next(e for e in result.coverage if e.origin == parent.origin + "/Политика/" + field)
    assert (row.state, row.reason) == (
        ("manual", "unsupported") if role == "send" else ("excluded", "opposite_role")
    )


@pytest.mark.parametrize("uid_only", [False, True])
@pytest.mark.parametrize("problem", ["clean", "excluded", "manual"])
def test_operations_uid_only_ignores_untransferred_search(synthetic, uid_only, problem):
    node = synthetic[2].pko()[0]
    node.values["ПродолжитьПоискПоПолямПоискаЕслиПоИдентификаторуНеНашли"] = not uid_only
    decisions = []
    if problem == "excluded":
        decisions = [{"origin": "receive:ПКО «Должности» / ПКС Код", "exclude": True}]
    elif problem == "manual":
        node.child("Свойства").items[0].values["НеЗамещать"] = True
    result = draft(synthetic, role="receive", accept_single=True, decisions=decisions)
    row = entity(result, "Должности", role="receive")
    assert row.state == (
        "ready"
        if uid_only or problem == "clean"
        else "manual"
        if problem == "excluded"
        else "blocked"
    )
    if row.state == "ready":
        patch = next(
            o["patch"] for o in operations(result, "pko") if o["patch"]["name"] == "Должности"
        )
        assert patch["identification"]["search_sets"] == ([] if uid_only else [["Код"]])
    apply_draft(new_manager(), result)


@pytest.mark.parametrize("kind", ["pko", "pks", "pks_group", "pvd"])
@pytest.mark.parametrize("excluded", [False, True])
def test_inventory_excluded_rule_children(synthetic, kind, excluded):
    rules = synthetic[2]
    node = (
        rules.pko()[0]
        if kind == "pko"
        else rules.pvd()[0]
        if kind == "pvd"
        else (
            rules.pko()[1].child("Свойства").items[-1]
            if kind == "pks_group"
            else rules.pko()[0].child("Свойства").items[1]
        )
    )
    node.values["ПередОбработкойПравила" if kind == "pvd" else "ПриВыгрузке"] = "Код();"
    node.attrs["НоваяПолитика"] = "Особая" if kind in ("pko", "pvd") else True
    decisions = []
    if excluded:
        if kind == "pko":
            decisions = [{"origin": "send:ПКО «Должности»", "exclude": True}]
        else:
            node.attrs["Отключить"] = True
    result = draft(synthetic, accept_single=True, decisions=decisions)
    parent = next(
        e
        for e in result.coverage
        if e.kind == kind
        and e.origin
        in {
            "send:ПКО «Должности»",
            "send:ПВД «Должности»",
            "send:ПКО «Должности» / ПКС Наименование",
            "send:ПКО «Заказ» / ПКС Товары",
        }
    )
    children = [
        e for e in result.coverage if e.parent == parent.origin and e.kind in ("handler", "policy")
    ]
    assert {e.kind for e in children} == {"handler", "policy"}
    for child in children:
        if excluded:
            assert (child.state, child.reason) == (
                "excluded",
                "parent_excluded" if kind == "pko" else "disabled",
            )
            assert not any(t.origin == child.origin for t in result.manual)
        else:
            assert child.state == "manual"


@pytest.mark.parametrize("explicit", [False, True])
def test_unreachable_decision_reaches_reference_targets(synthetic, explicit):
    rules = synthetic[2]
    rules.root.child("ПравилаВыгрузкиДанных").items.clear()
    decisions = (
        [{"origin": "send:ПКО «Заказ»", "format_type": "Документ.Заказ"}] if explicit else []
    )
    result = draft(synthetic, accept_single=True, decisions=decisions)
    for code in ("Заказ", "Должности", "Статусы"):
        row = entity(result, code)
        assert (row.state, row.reason) == (
            ("ready", "resolved") if explicit else ("excluded", "unreachable")
        )
    apply_draft(new_manager(), result)


@pytest.mark.parametrize("both", [False, True])
def test_packets_parameter_shared_by_roles(synthetic, both):
    result = draft(
        synthetic,
        rules=[("send", synthetic[2]), ("receive", deepcopy(synthetic[2]))] if both else None,
        accept_single=True,
    )
    rows = [e for e in result.coverage if e.kind == "parameter"]
    assert len(rows) == (2 if both else 1)
    assert sum(e.state == "ready" for e in rows) == 1
    assert sum(e.reason == "already_in_manager" for e in rows) == int(both)
    assert len(operations(result, "parameter")) == 1
    model = apply_draft(new_manager(), result)
    assert len(model.parameters) == 1


@pytest.mark.parametrize("prefix", [False, True])
def test_inventory_group_prefix_matches_rules_get(synthetic, prefix):
    from kd_rules_mcp.validation.address import walk_pks

    pko = synthetic[2].pko()[1]
    props = pko.child("Свойства")
    if prefix:
        group = deepcopy(props.items[-1])
        for side in ("Источник", "Приемник"):
            group.child(side).attrs["Имя"] = "Товары2"
        props.items.append(group)
    result = draft(synthetic, accept_single=True)
    expected = {"send:" + pks_address(pko.code, path) for path, _ in walk_pks(props)}
    actual = {
        e.origin
        for e in result.coverage
        if e.parent == "send:ПКО «Заказ»" and e.kind in ("pks", "pks_group")
    }
    assert actual == expected
    assert not any("Товары/2" in address for address in actual)


@pytest.mark.parametrize("role", ["send", "receive"])
@pytest.mark.parametrize("present", [False, True])
def test_inventory_rerun_keeps_manual_tasks(synthetic, role, present):
    rules = synthetic[2]
    if present:
        rules.pko()[0].values.update(
            ПриВыгрузке="Выгрузка();", ПриЗагрузке="Загрузка();", НеЗамещать=True
        )
        rules.pvd()[0].values["НеВыгружатьОбъектыСозданныеВБазеПриемнике"] = True
    first = draft(synthetic, role=role, accept_single=True)
    model = apply_draft(new_manager(), first)
    second = draft(synthetic, role=role, manager=model, accept_single=True)
    assert {(t.origin, t.event, t.body_sha256, t.line_count) for t in first.manual} == {
        (t.origin, t.event, t.body_sha256, t.line_count) for t in second.manual
    }
    opposite = {e.origin for e in first.coverage if e.reason == "opposite_role"}
    assert opposite <= {e.origin for e in second.coverage if e.reason == "opposite_role"}
    if present:
        task = next(
            t
            for t in second.manual
            if t.origin.endswith("/ПриВыгрузке" if role == "send" else "/ПриЗагрузке")
        )
        assert "Правило уже в проекте — проверить, перенесено ли" in task.message


@pytest.mark.parametrize("change", ["clean", "mode", "fields", "case"])
def test_operations_existing_identification_is_compared(synthetic, change):
    model = apply_draft(new_manager(), draft(synthetic, role="receive", accept_single=True))
    pko = next(p for p in model.pko if p.name == "Должности")
    ident = pko.identification
    if change == "mode":
        ident = replace(ident, mode=replace(ident.mode, value="ПоПолямПоиска"))
    elif change in ("fields", "case"):
        ident = replace(
            ident,
            search_sets=(
                replace(
                    ident.search_sets[0], fields=("Наименование" if change == "fields" else "кОД",)
                ),
            ),
        )
    model = replace(
        model, pko=tuple(replace(p, identification=ident) if p == pko else p for p in model.pko)
    )
    result = draft(synthetic, manager=model, role="receive", accept_single=True)
    row = entity(result, "Должности", role="receive")
    if change in ("mode", "fields"):
        assert row.state == "manual"
        assert "Идентификация отличается: КД 2" in row.message and " / проект " in row.message
        assert any(t.origin == row.origin for t in result.manual)
    else:
        assert row.reason == "already_in_manager"
    assert not operations(result)


@pytest.mark.parametrize("excluded", ["none", "agent", "disabled", "opposite"])
@pytest.mark.parametrize("remaining", [False, True])
def test_operations_excluded_search_fields_drop_from_set(synthetic, excluded, remaining):
    node = synthetic[2].pko()[0]
    node.values["СинхронизироватьПоИдентификатору"] = False
    props = node.child("Свойства")
    props.items[0].values["НеЗамещать"] = True
    if remaining:
        props.items[1].attrs["Поиск"] = True
    decisions = []
    if excluded == "agent":
        decisions = [{"origin": "receive:ПКО «Должности» / ПКС Код", "exclude": True}]
    elif excluded == "disabled":
        props.items[0].attrs["Отключить"] = True
    elif excluded == "opposite":
        props.items[0].values["ИмяПараметраДляПередачи"] = "Параметр"
    result = draft(synthetic, role="receive", accept_single=True, decisions=decisions)
    row = entity(result, "Должности", role="receive")
    assert row.state == ("blocked" if excluded == "none" else "ready" if remaining else "manual")
    if row.state == "blocked":
        assert row.blocked_by == ["receive:ПКО «Должности» / ПКС Код"]
    elif row.state == "manual":
        assert "Поиск без полей" in row.message
    else:
        patch = next(
            o["patch"] for o in operations(result, "pko") if o["patch"]["name"] == "Должности"
        )
        assert patch["identification"]["search_sets"] == [["Наименование"]]
    apply_draft(new_manager(), result)


@pytest.mark.parametrize("manual_reason", [False, True])
def test_operations_reference_search_message_has_priority(synthetic, manual_reason):
    node = synthetic[2].pko()[0]
    node.values["СинхронизироватьПоИдентификатору"] = False
    prop = node.child("Свойства").items[0]
    for side in ("Источник", "Приемник"):
        prop.child(side).attrs["Имя"] = "Ссылка"
    if manual_reason:
        prop.values["НеЗамещать"] = True
    result = draft(synthetic, role="receive", accept_single=True)
    row = entity(result, ("Должности", "Ссылка"), "pks", "receive")
    assert row.state == "manual" and "Ссылка" in row.message
    assert "Ссылка" in next(t.message for t in result.manual if t.origin == row.origin)


@pytest.mark.parametrize("role", ["send", "receive"])
@pytest.mark.parametrize("empty", ["none", "source", "target"])
@pytest.mark.parametrize("group", [False, True])
def test_operations_empty_property_side_depends_on_role(synthetic, role, empty, group):
    prop = (
        synthetic[2].pko()[1].child("Свойства").items[-1]
        if group
        else synthetic[2].pko()[0].child("Свойства").items[0]
    )
    if empty != "none":
        prop.children.pop("Источник" if empty == "source" else "Приемник")
    result = draft(synthetic, role=role, accept_single=True)
    row = next(
        e
        for e in result.coverage
        if e.kind == ("pks_group" if group else "pks")
        and e.origin.startswith(f"{role}:ПКО «{'Заказ' if group else 'Должности'}» / ПКС ")
        and e.origin.endswith("Товары" if group else "Код")
    )
    assert row.state == (
        "ready" if empty == "none" or (role == "receive" and empty == "source") else "manual"
    )
    apply_draft(new_manager(), result)


@pytest.mark.parametrize("role", ["send", "receive"])
@pytest.mark.parametrize(
    "variant", ["unique", "explicit", "absent", "several", "multi_type", "excluded"]
)
def test_operations_implicit_reference_and_reachability(synthetic, role, variant):
    connection, _, rules = synthetic
    prop = rules.pko()[1].child("Свойства").items[1]
    if variant != "explicit":
        prop.values.pop("КодПравилаКонвертации")
    rules.root.child("ПравилаВыгрузкиДанных").items.pop(0)
    if variant == "absent":
        rules.root.child("ПравилаКонвертацииОбъектов").items.pop(0)
    elif variant == "several":
        other = deepcopy(rules.pko()[0])
        other.values["Код"] = "Должности2"
        rules.root.child("ПравилаКонвертацииОбъектов").items.append(other)
    elif variant == "multi_type":
        ident = connection.execute(
            "INSERT INTO type_sets(types) VALUES (?)", ("СправочникСсылка.Должности\nСтрока",)
        ).lastrowid
        connection.execute("UPDATE properties SET type_set_id=? WHERE path='Должность'", (ident,))
        connection.commit()
    result = draft(
        synthetic,
        role=role,
        accept_single=True,
        decisions=[{"origin": f"{role}:ПКО «Должности»", "exclude": True}]
        if variant == "excluded"
        else [],
    )
    row = entity(result, ("Заказ", "Должность"), "pks", role)
    assert row.state == (
        "ready"
        if variant in ("unique", "explicit", "several")
        else "blocked"
        if variant == "excluded"
        else "manual"
    )
    if row.state == "ready":
        patch = next(
            o["patch"]
            for o in operations(result, "property")
            if o["patch"]["configuration_property"] == "Должность"
        )
        assert patch["property_kind"] == "reference"
        assert (
            entity(result, "Должности2" if variant == "several" else "Должности", role=role).state
            == "ready"
        )
    apply_draft(new_manager(), result)


@pytest.mark.parametrize("different_namespace", [False, True])
def test_candidates_same_path_namespace_is_one(synthetic, different_namespace):
    from kd_rules_mcp.authoring.ed.migration_candidates import unique

    ns = synthetic[1].base_namespace
    rows = [
        {"format_path": "Товары", "reason": "совпадает имя"},
        {
            "format_path": "Товары",
            "namespace": "urn:other" if different_namespace else ns,
            "confidence": "reference",
        },
    ]
    found = unique(rows, "format_path", default_namespace=ns)
    assert len(found) == (2 if different_namespace else 1)


@pytest.mark.parametrize("reachable", [False, True])
def test_candidates_unreachable_still_has_candidates(synthetic, reachable):
    if not reachable:
        synthetic[2].root.child("ПравилаВыгрузкиДанных").items.clear()
    result = draft(synthetic, accept_single=True)
    row = entity(result, "Должности")
    assert row.candidates_total == 1
    assert row.candidates[0]["format_type"] == "Справочник.Должности"
    assert row.state == ("ready" if reachable else "excluded")


@pytest.mark.parametrize("count", [1, 2, 3])
@pytest.mark.parametrize("accept", [False, True])
def test_candidates_element_and_group_are_single(synthetic, monkeypatch, count, accept):
    from kd_rules_mcp.authoring.ed.migration_candidates import CandidateContext

    original = CandidateContext.object_rows

    def rows(self, host, role, enumeration):
        if host.name == "Должности":
            return [
                {"format_type": name, "auto": False}
                for name in (
                    "Справочник.Должности",
                    "Справочник.ДолжностиГруппа",
                    "Справочник.Другое",
                )[:count]
            ]
        return original(self, host, role, enumeration)

    monkeypatch.setattr(CandidateContext, "object_rows", rows)
    result = draft(synthetic, accept_single=accept)
    row = entity(result, "Должности")
    assert row.candidates_total == (1 if count < 3 else 3)
    assert row.state == ("ready" if accept and count < 3 else "needs_decision")
    if row.state == "needs_decision":
        assert row.reason == ("single_candidate" if count < 3 else "several_candidates")
    assert any("Группы справочника" in t.message for t in result.manual) == (count == 2)
    apply_draft(new_manager(), result)


@pytest.mark.parametrize("kind", ["handler", "algorithm"])
@pytest.mark.parametrize(
    "body,called",
    [
        ('ИмяПКО = "Заказ";', True),
        ('ИмяПКО = "заКаз";', True),
        ('// "Заказ"\nЗаказ();', False),
        ('ИмяПКО = "ДругойЗаказ";', False),
        ('Текст = """Заказ""";', False),
    ],
)
def test_unreachable_bsl_literal_calls(synthetic, kind, body, called):
    rules = synthetic[2]
    rules.root.child("ПравилаВыгрузкиДанных").items.clear()
    if kind == "handler":
        rules.root.values["ПередВыгрузкойДанных"] = body
        origin = "send:Конвертация/ПередВыгрузкойДанных"
    else:
        node = Node.new("algorithm", "Алгоритм")
        node.attrs["Имя"] = "Вызов"
        node.values["Текст"] = body
        rules.section("Алгоритмы").items.append(node)
        rules.root.values["ПередВыгрузкойДанных"] = "Выполнить(Алгоритмы.Вызов);"
        origin = "send:Конвертация/ПередВыгрузкойДанных"
    result = draft(synthetic, accept_single=True)
    row = entity(result, "Заказ")
    assert row.state == ("ready" if called else "excluded")
    if called:
        assert origin in row.message
        assert entity(result, "Должности").state == "ready"
    apply_draft(new_manager(), result)


@pytest.mark.parametrize("role", ["send", "receive"])
@pytest.mark.parametrize("reverse", [False, True])
def test_candidates_reference_opposite_direction(synthetic, role, reverse):
    connection, schema, rules = synthetic
    reference_role = ("receive" if role == "send" else "send") if reverse else role
    model = apply_draft(new_manager(), draft(synthetic, role=reference_role, accept_single=True))
    typical = read_manager_text(render(model).text)
    ident = connection.execute("SELECT id FROM objects WHERE name='Должности'").fetchone()[0]
    connection.execute(
        "UPDATE properties SET name='Шифр',path='Шифр' WHERE object_id=? AND path='Код'", (ident,)
    )
    connection.execute("UPDATE objects SET name='Профессии' WHERE id=?", (ident,))
    connection.commit()
    typical = replace(
        typical,
        pko=tuple(
            replace(
                r,
                configuration_object=replace(
                    r.configuration_object,
                    value=Expr(
                        "Метаданные.Справочники.Профессии",
                        r.span,
                        reference_parts=("Метаданные", "Справочники", "Профессии"),
                    ),
                ),
                properties=tuple(
                    replace(p, configuration_property="Шифр")
                    if p.configuration_property == "Код"
                    else p
                    for p in r.properties
                ),
            )
            if r.name == "Должности"
            else r
            for r in typical.pko
        ),
    )
    for side in ("Источник", "Приемник"):
        rules.pko()[0].values[side] = "СправочникСсылка.Профессии"
        rules.pko()[0].child("Свойства").items[0].child(side).attrs["Имя"] = "Шифр"
    rules.pvd()[0].values["ОбъектВыборки"] = "СправочникСсылка.Профессии"
    result = build_migration_draft(
        [(role, rules)],
        connection,
        schema,
        new_manager(),
        reference_document=typical,
        accept_single=True,
    )
    root = entity(result, "Должности", role=role)
    prop = entity(result, ("Должности", "Шифр"), "pks", role=role)
    assert root.state == prop.state == "ready"
    assert root.candidates[0]["confidence"] == ("reference_reverse" if reverse else "reference")
    assert "типовом" in prop.candidates[0]["reason"]
    assert ("другого направления" in prop.candidates[0]["reason"]) == reverse
    apply_draft(new_manager(), result)


@pytest.mark.parametrize("count", [2, 5])
def test_candidates_rank_and_total_in_tool_page(synthetic, monkeypatch, count):
    from kd_rules_mcp.authoring.ed.migration_candidates import CandidateContext, unique

    original = CandidateContext.object_rows
    rows = [
        {"format_type": "Справочник.Подразделения", "reason": "прочее"},
        {"format_type": "Справочник.Должности", "reason": "совпадает имя"},
        {"format_type": "Справочник.Классификатор", "reason": "так в типовом модуле"},
        {"format_type": "Справочник.Склады", "reason": "совпадает синоним"},
        {"format_type": "Документ.Заказ", "reason": "прочее"},
    ][:count]
    monkeypatch.setattr(
        CandidateContext,
        "object_rows",
        lambda self, host, role, enumeration: (
            unique(rows, "format_type")
            if host.name == "Должности"
            else original(self, host, role, enumeration)
        ),
    )
    result = draft(synthetic)
    row = entity(result, "Должности")
    assert row.candidates_total == count
    assert len(row.candidates) == min(count, 3)
    assert [r["format_type"] for r in row.candidates] == (
        ["Справочник.Должности", "Справочник.Классификатор", "Справочник.Склады"]
        if count > 3
        else ["Справочник.Должности", "Справочник.Подразделения"]
    )
    payload = next(r for r in result.page("coverage", 0, 200)["items"] if r["origin"] == row.origin)
    assert payload["candidates_total"] == count and payload["candidates"] == row.candidates


@pytest.mark.parametrize("role", ["send", "receive"])
@pytest.mark.parametrize("missing", [False, True])
def test_operations_missing_key_properties_task(synthetic, tmp_path, role, missing):
    connection, _, rules = synthetic
    path = tmp_path / "required.bin"
    text = (DATA / "ed/writer/format.bin").read_text("utf-8")
    text = text.replace(
        'name="КлючевыеСвойства" type="t:КлючевыеСвойстваДолжности"',
        'name="КлючевыеСвойства" type="t:КлючевыеСвойстваДолжности" lowerBound="0"',
    )
    text = text.replace(
        '<property name="Код" type="xs:string"/>',
        '<property name="Код" type="xs:string" lowerBound="1"/>',
    )
    text = text.replace(
        '<property name="Наименование" type="t:Наименование10"/>',
        '<property name="Наименование" type="t:Наименование10" lowerBound="1"/>',
    )
    path.write_text(text, encoding="utf-8")
    extend_schema(path)
    schema = load_schema(path, locate_import=lambda _: DATA / "ed/writer/message.bin")
    decisions = []
    if missing:
        decisions.append({"origin": f"{role}:ПКО «Должности» / ПКС Наименование", "exclude": True})
    result = build_migration_draft(
        [(role, rules)], connection, schema, new_manager(), accept_single=True, decisions=decisions
    )
    assert entity(result, "Должности", role=role).state == "ready"
    tasks = [t for t in result.manual if "Ключевые свойства" in t.message]
    assert bool(tasks) == (missing and role == "send")
    if tasks:
        assert "КлючевыеСвойства.Наименование" in tasks[0].message
        assert "КлючевыеСвойства.Код" not in tasks[0].message
    apply_draft(new_manager(), result)


@pytest.mark.parametrize("role", ["send", "receive"])
@pytest.mark.parametrize("excluded", [False, True])
def test_inventory_excluded_group_excludes_columns(synthetic, role, excluded):
    result = draft(
        synthetic,
        role=role,
        accept_single=True,
        decisions=[{"origin": f"{role}:ПКО «Заказ» / ПКС Товары", "exclude": True}]
        if excluded
        else [],
    )
    column = next(e for e in result.coverage if e.origin.endswith("Товары/Номенклатура"))
    assert (column.state, column.reason) == (
        ("excluded", "parent_excluded") if excluded else ("ready", "resolved")
    )
    apply_draft(new_manager(), result)


@pytest.mark.parametrize("rename", [False, True])
def test_decisions_parameter_second_role_cannot_rename(synthetic, rename):
    result = draft(
        synthetic,
        rules=[("send", synthetic[2]), ("receive", synthetic[2])],
        accept_single=True,
        decisions=[{"origin": "send:параметр «Период»", "name": "Период2"}] if rename else [],
    )
    parameters = [e for e in result.coverage if e.kind == "parameter"]
    second = next(e for e in parameters if e.role == "send")
    assert second.reason == ("decision_invalid" if rename else "already_in_manager")
    assert len(operations(result, "parameter")) == 1
    model = apply_draft(new_manager(), result)
    assert [p.name for p in model.parameters] == ["Период"]


@pytest.mark.parametrize("collision", [False, True])
def test_decisions_name_cannot_steal_default(synthetic, collision):
    model = manager_with_pod(
        "Должности_Получение",
        "send",
        configuration_selection={
            "state": "reference",
            "reference_parts": ["Метаданные", "Документы", "Заказ"],
        },
    )
    result = draft(
        synthetic,
        role="receive",
        manager=model,
        accept_single=True,
        decisions=[
            {
                "origin": "receive:ПОД «Справочник.Должности»",
                "name": "Заказ_Получение" if collision else "ДолжностиНовые",
            }
        ],
    )
    chosen = next(e for e in result.coverage if e.origin == "receive:ПОД «Справочник.Должности»")
    assert chosen.reason == ("name_conflict" if collision else "resolved")
    assert (
        next(e for e in result.coverage if e.origin == "receive:ПОД «Документ.Заказ»").state
        == "ready"
    )
    apply_draft(model, result)


@pytest.mark.parametrize("register", [False, True])
def test_inventory_register_manual_task_has_actual_reason(synthetic, register):
    connection, _, rules = synthetic
    if register:
        connection.execute("UPDATE objects SET kind='РегистрСведений' WHERE name='Должности'")
        connection.commit()
        for side in ("Источник", "Приемник"):
            rules.pko()[0].values[side] = "РегистрСведенийЗапись.Должности"
        rules.pko()[0].values["СинхронизироватьПоИдентификатору"] = False
        rules.pko()[0].child("Свойства").items[0].attrs.pop("Поиск")
    result = draft(
        synthetic,
        role="receive",
        accept_single=True,
        decisions=[{"origin": "receive:ПКО «Должности»", "format_type": "Справочник.Должности"}],
    )
    row = entity(result, "Должности", role="receive")
    assert row.state == ("manual" if register else "ready")
    tasks = [t for t in result.manual if t.origin == row.origin]
    if register:
        assert tasks and "измерениям" in tasks[0].message
        assert tasks[0].event == "Идентификация"


@pytest.mark.parametrize("flag", ["ПриводитьКДлине", "ВыгружатьГруппуЧерезФайл"])
@pytest.mark.parametrize("role", ["send", "receive"])
@pytest.mark.parametrize("enabled", [False, True])
def test_inventory_length_and_group_flags_side(synthetic, flag, role, enabled):
    prop = (
        synthetic[2].pko()[1].child("Свойства").items[-1]
        if flag == "ВыгружатьГруппуЧерезФайл"
        else synthetic[2].pko()[0].child("Свойства").items[1]
    )
    if enabled:
        prop.attrs[flag] = True
    result = draft(synthetic, role=role, accept_single=True)
    policies = [e for e in result.coverage if e.kind == "policy" and e.origin.endswith("/" + flag)]
    assert bool(policies) == enabled
    if enabled:
        assert (policies[0].state, policies[0].reason) == (
            ("manual", "unsupported") if role == "send" else ("excluded", "opposite_role")
        )
    apply_draft(new_manager(), result)


@pytest.mark.parametrize("duplicate", [False, True])
def test_inventory_duplicate_pko_origins_do_not_mix(synthetic, duplicate):
    rules = synthetic[2]
    if duplicate:
        other = deepcopy(rules.pko()[0])
        other.child("Свойства").items = [other.child("Свойства").items[1]]
        other.values["ПриВыгрузке"] = "Второй();"
        rules.section("ПравилаКонвертацииОбъектов").items.append(other)
    result = draft(synthetic, accept_single=True)
    assert len({e.origin for e in result.coverage}) == len(result.coverage)
    first = [e for e in result.coverage if e.parent == "send:ПКО «Должности»" and e.kind == "pks"]
    assert len(first) == 3
    second = [e for e in result.coverage if e.parent == "send:ПКО «Должности» / #2"]
    assert bool(second) == duplicate
    if duplicate:
        assert len([e for e in second if e.kind == "pks"]) == 1
        assert any(e.origin.endswith("/ #2 / ПКС Наименование") for e in second)
        assert any(e.origin.endswith("/ #2/ПриВыгрузке") for e in second)


@pytest.mark.parametrize("role", ["send", "receive"])
@pytest.mark.parametrize("owner", ["pko", "pks", "pks_group", "pvd", "global"])
@pytest.mark.parametrize("rooted", [False, True])
def test_unreachable_code_literal_is_owner_edge(synthetic, role, owner, rooted):
    rules = synthetic[2]
    target = deepcopy(rules.pko()[0])
    target.values["Код"] = "Цель"
    target.child("Свойства").items.pop()
    rules.section("ПравилаКонвертацииОбъектов").items.append(target)
    if not rooted and owner != "pvd":
        rules.section("ПравилаВыгрузкиДанных").items.clear()
    if owner == "global":
        node, event = rules.root, "ПередВыгрузкойДанных"
    elif owner == "pvd":
        node, event = rules.pvd()[0], "ПередВыгрузкойОбъекта"
        node.values.pop("КодПравилаКонвертации")
        if not rooted:
            node.attrs["Отключить"] = True
    else:
        node = rules.pko()[1] if owner == "pks_group" else rules.pko()[0]
        if owner != "pko":
            node = node.child("Свойства").items[-1 if owner == "pks_group" else 1]
        event = "ПередВыгрузкой"
    node.values[event] = 'ИмяПКО = "Цель";'
    result = draft(synthetic, role=role, accept_single=True)
    row = entity(result, "Цель", role=role)
    expected = rooted or owner == "global"
    assert row.state == ("ready" if expected else "excluded")
    if expected:
        assert "Вызывается из кода:" in row.message and event in row.message
    else:
        assert row.reason == "unreachable"
    apply_draft(new_manager(), result)


@pytest.mark.parametrize("role", ["send", "receive"])
@pytest.mark.parametrize("owner", ["pko", "global", "pod"])
@pytest.mark.parametrize("export", [False, True])
def test_unreachable_load_and_cleanup_literals_are_not_roots(synthetic, role, owner, export):
    rules = synthetic[2]
    rules.section("ПравилаВыгрузкиДанных").items.pop(1)
    if owner == "pko":
        node = rules.pko()[0]
        event = "ПередВыгрузкой" if export else "ПослеЗагрузки"
    elif owner == "global":
        node = rules.root
        event = "ПередВыгрузкойДанных" if export else "ПослеЗагрузкиДанных"
    else:
        node = Node.new("pod", "Правило")
        node.values["Код"] = "Очистка"
        rules.section("ПравилаОчисткиДанных").items.append(node)
        event = "ПередОбработкойПравила"
    node.values[event] = 'Сообщить("Заказ");'
    result = draft(synthetic, role=role, accept_single=True)
    row = entity(result, "Заказ", role=role)
    assert row.state == ("ready" if export and owner != "pod" else "excluded")
    apply_draft(new_manager(), result)


@pytest.mark.parametrize("role", ["send", "receive"])
@pytest.mark.parametrize("rooted", [False, True])
@pytest.mark.parametrize("disabled", [False, True])
def test_unreachable_self_literal_cycle_needs_root(synthetic, role, rooted, disabled):
    rules = synthetic[2]
    if not rooted:
        rules.section("ПравилаВыгрузкиДанных").items.clear()
    # Заказ → Должности уже есть в обычной ПКС; обратное ребро — из её обработчика.
    prop = rules.pko()[0].child("Свойства").items[1]
    prop.values["ПередВыгрузкой"] = 'ИмяПКО = "Заказ";'
    if disabled:
        prop.attrs["Отключить"] = True
    if rooted:
        rules.section("ПравилаВыгрузкиДанных").items.pop(1)
    result = draft(synthetic, role=role, accept_single=True)
    row = entity(result, "Заказ", role=role)
    assert row.state == ("ready" if rooted and not disabled else "excluded")
    if not rooted:
        assert all(e.reason == "unreachable" for e in result.coverage if e.kind == "pko")
    apply_draft(new_manager(), result)


@pytest.mark.parametrize("role", ["send", "receive"])
@pytest.mark.parametrize(
    "call,identifier",
    [
        ("", False),
        ("Алгоритмы.Вызов;", True),
        ("Алгоритмы.выЗов;", True),
        ('"Вызов";', False),
        ("// Вызов", False),
        ("ВызовДругой;", False),
    ],
)
@pytest.mark.parametrize("scope", ["global", "load", "reachable_pko", "unreachable_pko"])
def test_unreachable_algorithm_requires_export_identifier(synthetic, role, call, identifier, scope):
    rules = synthetic[2]
    rules.section("ПравилаВыгрузкиДанных").items.pop(1)
    algorithm = Node.new("algorithm", "Алгоритм")
    algorithm.attrs["Имя"] = "Вызов"
    algorithm.values["Текст"] = 'ИмяПКО = "Заказ";'
    rules.section("Алгоритмы").items.append(algorithm)
    if scope in ("global", "load"):
        event = "ПередВыгрузкойДанных" if scope == "global" else "ПослеЗагрузкиДанных"
        rules.root.values[event] = call
        origin = f"{role}:Конвертация/{event}"
    else:
        if scope == "unreachable_pko":
            rules.section("ПравилаВыгрузкиДанных").items.clear()
        rules.pko()[0].values["ПередВыгрузкой"] = call
        origin = f"{role}:ПКО «Должности»/ПередВыгрузкой"
    result = draft(synthetic, role=role, accept_single=True)
    row = entity(result, "Заказ", role=role)
    expected = identifier and scope in ("global", "reachable_pko")
    assert row.state == ("ready" if expected else "excluded")
    if expected:
        assert row.message.endswith("Вызывается из кода: " + origin)
    apply_draft(new_manager(), result)


@pytest.mark.parametrize("role", ["send", "receive"])
@pytest.mark.parametrize("nearer", [False, True])
@pytest.mark.parametrize("normal_root", [False, True])
def test_unreachable_transitive_code_provenance(synthetic, role, nearer, normal_root):
    rules = synthetic[2]
    rules.section("ПравилаВыгрузкиДанных").items = rules.pvd()[:1] if normal_root else []
    root = f"{role}:Конвертация/ПередВыгрузкойДанных"
    rules.root.values["ПередВыгрузкойДанных"] = 'ИмяПКО = "Заказ";'
    if nearer:
        rules.pko()[1].child("Свойства").items.pop(1)
        rules.pko()[1].values["ПередВыгрузкой"] = 'ИмяПКО = "Должности";'
    result = draft(synthetic, role=role, accept_single=True)
    assert entity(result, "Заказ", role=role).message.endswith("Вызывается из кода: " + root)
    for code in ("Должности", "Статусы"):
        row = entity(result, code, role=role)
        assert row.state == "ready"
        if normal_root:
            assert "Вызывается из кода" not in row.message
        else:
            handler = f"{role}:ПКО «Заказ»/ПередВыгрузкой" if nearer else root
            assert row.message.endswith("Вызывается из кода: " + handler)
    assert asdict(result) == asdict(draft(synthetic, role=role, accept_single=True))
    apply_draft(new_manager(), result)


@pytest.mark.parametrize("reverse", [False, True])
def test_unreachable_code_path_choice_is_deterministic(synthetic, reverse):
    rules = synthetic[2]
    rules.section("ПравилаВыгрузкиДанных").items.clear()
    events = ["ПередВыгрузкойДанных", "ПослеВыгрузкиДанных"]
    for event in reversed(events) if reverse else events:
        rules.root.values[event] = 'ИмяПКО = "Заказ";'
    result = draft(synthetic, accept_single=True)
    selected = min("send:Конвертация/" + event for event in events)
    assert all(
        entity(result, code).message.endswith("Вызывается из кода: " + selected)
        for code in ("Заказ", "Должности", "Статусы")
    )


@pytest.mark.parametrize("role", ["send", "receive"])
@pytest.mark.parametrize("copies", [1, 2, 3])
@pytest.mark.parametrize("source", ["pvd", "property", "literal"])
def test_inventory_last_duplicate_is_effective(synthetic, role, copies, source):
    rules = synthetic[2]
    first, document, enumeration = rules.pko()
    pvd = rules.pvd()[0 if source == "pvd" else 1]
    rules.section("ПравилаВыгрузкиДанных").items = [pvd] if source != "literal" else []
    if source == "literal":
        rules.root.values["ПередВыгрузкойДанных"] = 'ИмяПКО = "Должности";'
    for _ in range(copies - 1):
        last = deepcopy(first)
        last.child("Свойства").items = [last.child("Свойства").items[1]]
        last.child("Свойства").items[0].attrs["Поиск"] = True
        last.values["СинхронизироватьПоИдентификатору"] = False
        rules.section("ПравилаКонвертацииОбъектов").items.append(last)
    if copies > 1:
        first.values["ПриВыгрузке"] = "Первый();"
        first.values["ПослеЗагрузки"] = "Первый();"
        first.attrs["ПриоритетОбъектовОбмена"] = "Первый"
        first.child("Свойства").items.append(deepcopy(document.child("Свойства").items[-1]))
        first.children["Значения"] = deepcopy(enumeration.child("Значения"))
    result = draft(synthetic, role=role, accept_single=True)
    roots = [
        e
        for e in result.coverage
        if e.kind == "pko" and e.origin.startswith(f"{role}:ПКО «Должности»")
    ]
    active = roots[-1]
    assert active.state == "ready"
    by_origin = {e.origin: e for e in result.coverage}
    for old in roots[:-1]:
        assert (old.state, old.reason) == ("excluded", "duplicate_code")
        assert active.origin in old.message
        for row in result.coverage:
            parent = by_origin.get(row.parent)
            while parent is not None and parent.origin != old.origin:
                parent = by_origin.get(parent.parent)
            if parent is not None:
                assert (row.state, row.reason) == ("excluded", "parent_excluded"), row
    model = apply_draft(new_manager(), result)
    rule = next(r for r in model.pko if r.name == "Должности")
    assert {p.configuration_property for p in rule.properties} == (
        {"Наименование"} if copies > 1 else {"Код", "Наименование", "Статус"}
    )
    if source == "pvd" and role == "send":
        assert entity(result, "Должности", "pvd").state == "ready"
    if source == "property":
        prop = next(
            p
            for r in model.pko
            if r.name == "Заказ"
            for p in r.properties
            if p.configuration_property == "Должность"
        )
        assert prop.conversion.target_id == rule.logical_id
    if copies > 1:
        assert entity(result, "Статусы", role=role).reason == "unreachable"
    assert sum(result.summary()["states"].values()) == len(result.coverage)


@pytest.mark.parametrize("role", ["send", "receive"])
@pytest.mark.parametrize("active_edge", [False, True])
def test_unreachable_replaced_duplicate_has_no_edges(synthetic, role, active_edge):
    rules = synthetic[2]
    first = rules.pko()[0]
    last = deepcopy(first)
    last.child("Свойства").items = [last.child("Свойства").items[0]]
    rules.section("ПравилаКонвертацииОбъектов").items.append(last)
    rules.section("ПравилаВыгрузкиДанных").items.pop(1)
    (last if active_edge else first).values["ПередВыгрузкой"] = 'ИмяПКО = "Заказ";'
    result = draft(synthetic, role=role, accept_single=True)
    row = entity(result, "Заказ", role=role)
    assert row.state == ("ready" if active_edge else "excluded")
    if active_edge:
        assert f"{role}:ПКО «Должности» / #2/ПередВыгрузкой" in row.message
    assert entity(result, "Статусы", role=role).reason == "unreachable"
    apply_draft(new_manager(), result)


@pytest.mark.parametrize("role", ["send", "receive"])
@pytest.mark.parametrize("kind", ["reference", "pkpd"])
@pytest.mark.parametrize("shadowed", [False, True])
def test_operations_implicit_last_effective_rule(synthetic, role, kind, shadowed):
    rules = synthetic[2]
    index = 0 if kind == "reference" else 2
    owner = rules.pko()[1] if kind == "reference" else rules.pko()[0]
    prop = owner.child("Свойства").items[1 if kind == "reference" else 2]
    prop.values.pop("КодПравилаКонвертации")
    original = rules.pko()[index]
    other = deepcopy(original)
    other.values["Код"] = original.code + "Позднее"
    rules.section("ПравилаКонвертацииОбъектов").items.append(other)
    if shadowed:
        replacement = deepcopy(rules.pko()[1])
        replacement.values["Код"] = other.code
        rules.section("ПравилаКонвертацииОбъектов").items.append(replacement)
    result = draft(synthetic, role=role, accept_single=True)
    selected = original.code if shadowed else other.code
    row = entity(
        result, (owner.code, "Должность" if kind == "reference" else "Статус"), "pks", role
    )
    assert row.state == "ready"
    assert f"ПКО выбрано неявно по типу: {role}:ПКО «{selected}»" in row.message
    packet = next(
        o
        for o in operations(result, "property")
        if o["patch"]["configuration_property"]
        == ("Должность" if kind == "reference" else "Статус")
    )
    created = {
        o["client_id"]: o["patch"]["name"]
        for o in operations(result, "pko" if kind == "reference" else "pkpd")
    }
    assert created[packet["patch"]["conversion"]["client_id"]] == selected
    assert packet["patch"]["property_kind"] == kind
    assert not any(t.origin == row.origin for t in result.manual)
    apply_draft(new_manager(), result)
