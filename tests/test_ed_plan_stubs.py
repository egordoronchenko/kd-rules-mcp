"""ПОД доставки: пять объектов, две отправки, три безопасные заглушки (#73)."""

import json
import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest

from kd_rules_mcp.authoring.ed.manager_operations import parse_operation, preview
from kd_rules_mcp.authoring.ed.plan_stubs import COMMENT, add_plan_stubs
from kd_rules_mcp.ed.reader import read_manager_text
from kd_rules_mcp.ed.writer import new_manager, render
from kd_rules_mcp.ed.writer_import import import_manager
from kd_rules_mcp.service import Kd2Service, Settings
from kd_rules_mcp.service import ed_routes as routes_service
from kd_rules_mcp.structures import db
from kd_rules_mcp.validation.ed_plan import validate_plan_pods
from kd_rules_mcp.validation.ed_structure_snapshot import StructureSnapshot
from kd_rules_mcp.validation.report import Level
from tests import session_inputs
from tests.test_ed_writer_plan_content import add_content
from tests.test_service_ed_writer import PLAN, apply_packet, build, manager_operations
from tests.test_service_ed_writer import writer_setup as writer_setup

NAMES = ("Должности", "Штатный", "Договоры", "Сотрудники", "Номенклатура")


def insert_content(connection, names, *, existing=False):
    if not existing:
        connection.executescript(db.SCHEMA)
        ident = connection.execute(
            "INSERT INTO objects(kind,name,type_name) VALUES(?,?,?)",
            ("ПланОбмена", PLAN, "ПланОбменаСсылка." + PLAN),
        ).lastrowid
        group = connection.execute(
            "INSERT INTO properties(object_id,kind,name,path,is_group) VALUES(?,?,?,?,1)",
            (ident, "СоставПланаОбмена", "{Состав}", "{Состав}"),
        ).lastrowid
    else:
        ident, group = connection.execute(
            "SELECT object_id,id FROM properties WHERE kind='СоставПланаОбмена'"
        ).fetchone()
    for name in names:
        type_name = "СправочникСсылка." + name
        connection.execute(
            "INSERT INTO objects(kind,name,type_name) VALUES(?,?,?)",
            ("Справочник", name, type_name),
        )
        type_id = connection.execute(
            "INSERT INTO type_sets(types) VALUES(?)", (type_name,)
        ).lastrowid
        connection.execute(
            "INSERT INTO properties(object_id,parent_id,kind,name,path,type_set_id) "
            "VALUES(?,?,?,?,?,?)",
            (ident, group, "ЭлементСоставаПланаОбмена", name, "{Состав}." + name, type_id),
        )


def pod_operation(name, direction="send", rule_name=None):
    return {
        "client_id": "pod-" + name,
        "kind": "pod",
        "action": "create",
        "patch": {
            "name": rule_name or name,
            "directions": [direction],
            "configuration_selection": {
                "state": "reference",
                "reference_parts": ["Метаданные", "Справочники", name],
            },
        },
    }


def fixture_model():
    with sqlite3.connect(":memory:") as connection:
        insert_content(connection, NAMES)
        structure = StructureSnapshot.load(connection)
    model = new_manager()
    operations = tuple(parse_operation(pod_operation(name)) for name in NAMES[:2])
    planned = preview(model, operations, expected_revision=model.revision)
    assert not planned.failures
    return planned.model, structure


def test_three_stubs_have_names_empty_pko_no_handler_and_round_trip():
    model, structure = fixture_model()
    report, objects = validate_plan_pods(model, structure, PLAN)
    assert len(report.warnings) == 3
    assert all("всего без ПОД: 3" in i.message for i in report.issues)
    delivered, rows = add_plan_stubs(model, objects)
    assert len(model.pod) == 2
    assert [row["name"] for row in rows] == [
        "Справочник_" + name + "_Отправка_Заглушка" for name in sorted(NAMES[2:])
    ]
    rendered = render(delivered)
    assert rendered.text.count(COMMENT) == 3
    reread = import_manager(read_manager_text(rendered.text), project_id=model.project_id)[0]
    stubs = [pod for pod in reread.pod if pod.name in {row["name"] for row in rows}]
    assert len(stubs) == 3
    assert all(pod.directions == ("send",) and not pod.used_pko and not pod.events for pod in stubs)
    assert not validate_plan_pods(reread, structure, PLAN)[0].issues
    assert not validate_plan_pods(read_manager_text(rendered.text), structure, PLAN)[0].issues


def test_registered_objects_are_info_receive_pod_does_not_cover_send():
    model, structure = fixture_model()
    receive = parse_operation(pod_operation("Договоры", "receive"))
    planned = preview(model, (receive,), expected_revision=model.revision)
    assert not planned.failures
    report, objects = validate_plan_pods(
        planned.model,
        structure,
        PLAN,
        registration_objects=("Справочник.Сотрудники",),
        registered_objects=("справочник.договоры",),
    )
    assert len(objects) == 3
    assert {i.address: i.level for i in report.issues} == {
        "Справочник.Договоры": Level.INFO,
        "Справочник.Сотрудники": Level.INFO,
        "Справочник.Номенклатура": Level.WARNING,
    }
    disabled, _ = validate_plan_pods(model, structure, PLAN, plan_stubs=False)
    assert len(disabled.warnings) == 3
    assert all("комплект не добавит ПОД" in i.message for i in disabled.issues)
    assert validate_plan_pods(model, None, PLAN)[0].skipped


def test_stub_name_conflict_is_resolved_and_additions_are_checked():
    model, structure = fixture_model()
    name = "Справочник_Договоры_Отправка_Заглушка"
    op = parse_operation(pod_operation("Номенклатура", "receive", name))
    planned = preview(model, (op,), expected_revision=model.revision)
    assert not planned.failures
    _, objects = validate_plan_pods(planned.model, structure, PLAN)
    _, rows = add_plan_stubs(planned.model, objects)
    assert rows[0]["name"] == name + "_2"
    plan_key = ("планобмена", PLAN.casefold())
    plan = structure.objects[plan_key]
    empty = replace(
        plan,
        properties={
            key: tuple(p for p in rows if p.kind == "СоставПланаОбмена")
            for key, rows in plan.properties.items()
        },
    )
    reduced = replace(structure, objects={**structure.objects, plan_key: empty})
    report, missing = validate_plan_pods(
        model,
        reduced,
        PLAN,
        (("Catalog", "Договоры", "Catalogs"),),
        ("Справочник.Сотрудники",),
    )
    assert {obj.name for obj in missing} == {"Договоры", "Сотрудники"}
    assert len(report.issues) == 2


def test_opened_project_checks_known_route_and_only_sending(tmp_path, monkeypatch):
    _, structure = fixture_model()
    plan_key = ("планобмена", PLAN.casefold())
    plan = replace(structure.objects[plan_key], name="ДемоОбмен")
    structure = replace(
        structure,
        objects={**structure.objects, ("планобмена", "демообмен"): plan},
    )
    service = Kd2Service(Settings(cache_dir=tmp_path / "cache", workspace=tmp_path / "workspace"))
    monkeypatch.setattr(service, "_ed_structure_snapshot", lambda _: (structure, "synthetic"))
    monkeypatch.setattr(routes_service, "read_routes", session_inputs._orig_read_routes)
    root = Path(__file__).parent / "data/ed/layers/base"
    opened = service.ed_open(
        path=str(root / "CommonModules/МенеджерДемо/Ext/Module.bsl"),
        configuration_path=str(root),
        extensions=[str(root.parent / "b")],
    )
    routes = service.ed_routes(path=str(root), extensions=[str(root.parent / "b")])
    checked = service.ed_validate(
        opened["project_id"],
        structure_id="host",
        route_profile_id=routes["profile_id"],
        direction="send",
        check_prefix="ed.plan.pod_missing",
    )
    assert checked["issues"]["total"] == 5
    received = service.ed_validate(
        opened["project_id"],
        structure_id="host",
        route_profile_id=routes["profile_id"],
        direction="receive",
        check_prefix="ed.plan.pod_missing",
    )
    assert received["issues"]["total"] == 0


@pytest.mark.parametrize("enabled", [True, False])
def test_manager_build_stubs_only_in_delivery_and_hash(writer_setup, enabled):
    service, args, _ = writer_setup
    add_content(service, True)
    with sqlite3.connect(service.store.path("host")) as connection:
        insert_content(connection, NAMES[1:], existing=True)
    created = service.ed_create(**args)
    _, _, applied = apply_packet(
        service, created, [*manager_operations(), pod_operation("Штатный")]
    )
    initial = service.ed_list(applied["document_id"], "pod")
    checked = service.ed_validate(
        applied["document_id"], structure_id="host", check_prefix="ed.plan.pod_missing"
    )
    assert checked["issues"]["total"] == 3
    previewed = build(service, applied, plan_stubs=enabled)
    assert build(service, applied, plan_stubs=not enabled)["build_hash"] != previewed["build_hash"]
    warnings = build(
        service, applied, plan_stubs=enabled, section="notices", check_prefix="ed.plan.pod_missing"
    )
    assert warnings["total"] == 3
    written = build(
        service,
        applied,
        plan_stubs=enabled,
        mode="write",
        expected_preview_hash=previewed["build_hash"],
        acknowledged_notices=previewed["required_acknowledgements"],
    )
    root = Path(written["output_dir"])
    manifest = json.loads((root / "manifest.json").read_bytes())
    assert len(manifest["pod_stubs"]) == (3 if enabled else 0)
    path = next((root / "extension/CommonModules").glob("*/Ext/Module.bsl"))
    text = path.read_text("utf-8-sig")
    assert text.count(COMMENT) == (3 if enabled else 0)
    instruction = (root / "instruction.md").read_text("utf-8")
    assert ("Заглушки ПОД" in instruction) is enabled
    if enabled and "## Проверьте данные перед первым обменом" in instruction:
        # Д-П3: раздел проверки данных — последний, заглушки не попадают внутрь него.
        assert instruction.rfind("## Заглушки ПОД") < instruction.rfind(
            "## Проверьте данные перед первым обменом"
        )
    assert service.ed_list(applied["document_id"], "pod") == initial
    infos = build(
        service,
        applied,
        registered_objects=["Справочник.Договоры"],
        section="issues_after",
        check_prefix="ed.plan.pod_missing",
    )
    assert (
        next(i for i in infos["items"] if i["address"] == "Справочник.Договоры")["level"] == "info"
    )
