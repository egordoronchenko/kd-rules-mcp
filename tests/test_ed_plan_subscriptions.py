"""Подписки структуры, дополнения XML и объекты только для регистрации."""

import json
import shutil
import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest
from lxml import etree

from kd2_rules_mcp.authoring.ed.model import AuthoringPreconditionError
from kd2_rules_mcp.authoring.ed.xml_dump import (
    SubscriptionAddition,
    check_subscription_sources,
    read_description,
)
from kd2_rules_mcp.authoring.registration_retarget import registration_plan_notices
from kd2_rules_mcp.errors import EdAuthoringAckRequiredError, EdAuthoringPreconditionError
from kd2_rules_mcp.kd2.rules_io import load_registration_rules
from kd2_rules_mcp.structures import db
from kd2_rules_mcp.structures.queries import Page, describe_object, exchange_plan_content
from kd2_rules_mcp.structures.xmlbuild import Metadata
from kd2_rules_mcp.structures.xmlbuild import build as build_structure
from kd2_rules_mcp.structures.xmldump import plan_subscriptions, read_dump, read_subscription
from kd2_rules_mcp.validation.ed_plan import (
    subscription_source_objects,
    validate_plan_registration,
)
from kd2_rules_mcp.validation.ed_structure_snapshot import StructureSnapshot
from tests.test_ed_writer_plan_content import add_content
from tests.test_service_ed_writer import PLAN, apply_packet, build, manager_operations
from tests.test_service_ed_writer import writer_setup as writer_setup

DATA = Path(__file__).parent / "data/xmldump/subscriptions"


def snapshot(main=DATA / "main", extensions=()):
    connection = sqlite3.connect(":memory:")
    connection.executescript(db.SCHEMA)
    build_structure(Metadata(read_dump(main), [read_dump(path) for path in extensions]), connection)
    return connection, StructureSnapshot.load(connection)


def test_structure_subscriptions_overlay_and_sqlite_round_trip():
    connection, base = snapshot()
    assert base.subscriptions is not None
    assert len(plan_subscriptions(base.subscriptions, PLAN)) == 4
    described = describe_object(connection, "ПланОбмена." + PLAN)
    assert isinstance(described, dict)
    assert len(described["registration_subscriptions"]) == 4
    page = exchange_plan_content(connection, PLAN)
    assert isinstance(page, Page)
    assert page.items[0]["registration_subscriptions"] == {
        "BeforeWrite": ["Регистрация"],
        "BeforeDelete": ["РегистрацияУдаления"],
    }
    clone = sqlite3.connect(":memory:")
    clone.deserialize(connection.serialize())
    assert StructureSnapshot.load(clone).subscriptions == base.subscriptions
    clone.close()
    connection.close()
    connection, extended = snapshot(extensions=(DATA / "ext",))
    assert extended.subscriptions is not None
    subscription = next(s for s in extended.subscriptions if s.name == "Регистрация")
    assert subscription.uuid == "22222222-2222-4222-8222-000000000001"
    assert subscription.sources == ("CatalogObject.Штатный", "CatalogObject.Должности")
    assert subscription.handler.endswith("ПередЗаписью")
    assert read_subscription(DATA / "ext/EventSubscriptions/Регистрация.xml").handler == ""
    connection.close()


def test_registration_events_and_delivery_cover_all_kinds():
    connection, structure = snapshot()
    assert structure.subscriptions is not None
    additions = (("Catalog", "Должности", "Catalogs"), ("Document", "Приход", "Documents"))
    report, subscriptions = validate_plan_registration(
        structure, PLAN, additions, ("РегистрСведений.История",)
    )
    assert len(report.issues) == 5
    sources = {s.name: values for s, values in subscriptions}
    assert sources == {
        "Регистрация": ("CatalogObject.Должности",),
        "РегистрацияДокумента": ("DocumentObject.Приход",),
        "РегистрацияНабора": ("InformationRegisterRecordSet.История",),
        "РегистрацияУдаления": ("CatalogObject.Должности", "DocumentObject.Приход"),
    }
    # Источники вне добавлений состава расширение заимствует отдельно.
    assert subscription_source_objects(subscriptions, additions) == (
        ("InformationRegister", "История", "InformationRegisters"),
    )
    assert len(subscription_source_objects(subscriptions)) == 3
    with pytest.raises(ValueError):
        subscription_source_objects(((subscriptions[0][0], ("EnumObject.Нет",)),))
    addition = SubscriptionAddition(
        read_description(
            "EventSubscriptions/Регистрация.xml",
            (DATA / "main/EventSubscriptions/Регистрация.xml").read_text("utf-8"),
            "EventSubscription",
        ),
        ("CatalogObject.Должности",),
    )
    with pytest.raises(AuthoringPreconditionError, match="не заимствован"):
        check_subscription_sources((), (addition,), ())
    patched = replace(
        structure,
        subscriptions=tuple(
            replace(s, sources=(*s.sources, *sources.get(s.name, ())))
            for s in structure.subscriptions
        ),
    )
    assert not validate_plan_registration(patched, PLAN, additions, ("РегистрСведений.История",))[
        0
    ].issues
    # Имя подписки не определяет план; похожий чужой обработчик не считается.
    assert not plan_subscriptions(
        (
            replace(
                structure.subscriptions[0], handler="CommonModule.Другой.ПланФорматаПередЗаписью"
            ),
        ),
        PLAN,
    )
    connection.close()


def prepare_service(service, host, included):
    add_content(service, included)
    for folder in ("EventSubscriptions", "Catalogs", "InformationRegisters"):
        shutil.copytree(DATA / "main" / folder, host / folder, dirs_exist_ok=True)
    with sqlite3.connect(service.store.path("host")) as connection:
        connection.execute(
            "INSERT INTO objects(kind,name,type_name) VALUES(?,?,?)",
            ("РегистрСведений", "История", "РегистрСведенийЗапись.История"),
        )
        connection.execute("INSERT OR REPLACE INTO meta VALUES ('subscriptions_known', 'true')")
        for path in (DATA / "main/EventSubscriptions").glob("*.xml"):
            s = read_subscription(path)
            connection.execute(
                "INSERT INTO event_subscriptions VALUES (?,?,?,?,?)",
                (s.name, s.uuid, s.event, s.handler, json.dumps(s.sources)),
            )


@pytest.mark.parametrize("included", [False, True])
def test_manager_subscription_kit_and_registration_only(writer_setup, included):
    service, args, host = writer_setup
    prepare_service(service, host, included)
    created = service.ed_create(**args)
    _, _, applied = apply_packet(service, created, manager_operations())
    options = {"registration_objects": ["РегистрСведений.История"]}
    result = build(service, applied, **options)
    notices = build(service, applied, section="notices", **options)["items"]
    assert len([n for n in notices if n.get("check") == "ed.plan.registration_unsubscribed"]) == 3
    with pytest.raises(EdAuthoringAckRequiredError):
        build(service, applied, mode="write", expected_preview_hash=result["build_hash"], **options)
    written = build(
        service,
        applied,
        mode="write",
        expected_preview_hash=result["build_hash"],
        acknowledged_notices=result["required_acknowledgements"],
        **options,
    )
    root = Path(written["output_dir"])
    manifest = json.loads((root / "manifest.json").read_bytes())
    assert manifest["registration_objects"] == options["registration_objects"]
    assert "InformationRegister.История" in manifest["plan_content_additions"]
    assert ("Catalog.Должности" in manifest["plan_content_additions"]) is not included
    rows = manifest["registration_subscription_additions"]
    assert len(rows) == 3
    config = etree.parse(str(root / "extension/Configuration.xml"))
    # Объект штатного состава без подписки заимствуется ради Source, но в состав не входит
    # (эталон PSR: EventSubscriptions/...ПередЗаписьюДокумента.xml:10-12, Configuration.xml:501).
    adopted = ["Catalog.Должности"] if included else []
    assert manifest.get("subscription_adopted_objects", []) == adopted
    catalogs = [n.text for n in config.findall("{*}Configuration/{*}ChildObjects/{*}Catalog")]
    assert catalogs == ["Должности"]
    catalog = etree.parse(str(root / "extension/Catalogs/Должности.xml")).getroot()[0]
    assert catalog.findtext("{*}Properties/{*}ObjectBelonging") == "Adopted"
    content = etree.parse(str(root / f"extension/ExchangePlans/{PLAN}/Ext/Content.xml"))
    assert ("Catalog.Должности" in [n.text for n in content.iter("{*}Metadata")]) is not included
    instruction = (root / "instruction.md").read_text("utf-8")
    assert ("заимствованы объекты штатного состава" in instruction) is included
    assert set(
        config.findall("{*}Configuration/{*}ChildObjects/{*}EventSubscription")[i].text
        for i in range(3)
    ) == {r["name"] for r in rows}
    for row in rows:
        path = root / "extension/EventSubscriptions" / (row["name"] + ".xml")
        xml = etree.parse(str(path))
        obj = xml.getroot()[0]
        assert obj.findtext("{*}Properties/{*}ObjectBelonging") == "Adopted"
        assert obj.findtext("{*}Properties/{*}ExtendedConfigurationObject") == row["uuid"]
        assert [n.text for n in obj.findall("{*}Properties/{*}Source/{*}Type")] == [
            "cfg:" + s for s in row["sources"]
        ]
        assert obj.find("{*}Properties/{*}Handler") is None
        assert obj.find("{*}Properties/{*}Event") is None
        assert obj.find("{*}ChildObjects") is None
        assert not path.with_suffix("").exists()
    # Форма заимствования совпадает с синтетическим образцом PSR целиком.
    actual = etree.parse(str(root / "extension/EventSubscriptions/Регистрация.xml")).getroot()
    expected = etree.parse(str(DATA / "ext/EventSubscriptions/Регистрация.xml")).getroot()
    for tree in (actual, expected):
        for node in tree.iter():
            node.tail = None
            if node.text is not None and not node.text.strip():
                node.text = None
        tree[0].set("uuid", "own")
        etree.cleanup_namespaces(tree)
    assert etree.tostring(actual[0]) == etree.tostring(expected[0])
    instruction = (root / "instruction.md").read_text("utf-8")
    assert "Источники подписок регистрации дополнены" in instruction
    assert "Объекты только для регистрации" in instruction
    assert build(service, applied, **options)["build_hash"]


@pytest.mark.parametrize(
    "objects", [["Справочник.Должности"], ["Справочник.Нет"], "РегистрСведений.История"]
)
def test_invalid_registration_objects_refuse(writer_setup, objects):
    service, args, host = writer_setup
    prepare_service(service, host, False)
    created = service.ed_create(**args)
    _, _, applied = apply_packet(service, created, manager_operations())
    with pytest.raises((EdAuthoringPreconditionError, ValueError)):
        build(service, applied, registration_objects=objects)


def test_retarget_warns_for_membership_and_missing_sources():
    rules = load_registration_rules(Path(__file__).parent / "data/registration/retarget.xml")
    connection, structure = snapshot()
    names = frozenset(str(rule.get("ОбъектМетаданныхИмя")) for rule in rules.rules())
    notices = registration_plan_notices(rules, PLAN, names, structure.subscriptions)
    assert notices and all(
        n.check == "registration.plan_registration_unsubscribed" for n in notices
    )
    assert all(n.requires_acknowledgement and "registration_objects" in n.message for n in notices)
    outside = registration_plan_notices(rules, PLAN, frozenset(), structure.subscriptions)
    assert all(
        n.check == "registration.plan_membership" and "registration_objects" in n.message
        for n in outside
    )
    connection.close()
