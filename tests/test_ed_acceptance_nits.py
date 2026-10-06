"""Мелочи живой приёмки: исполняемые подсказки, фильтры и краткая инструкция."""

import json
from dataclasses import replace
from pathlib import Path

import pytest
from lxml import etree

from kd2_rules_mcp.authoring.ed.registration_delivery import (
    read_plan_host,
    render_registration_kit,
)
from kd2_rules_mcp.authoring.registration_retarget import RetargetNotice
from kd2_rules_mcp.errors import (
    EdAuthoringPreconditionError,
    EdSchemaNotFoundError,
    StructureNotFoundError,
)
from kd2_rules_mcp.server import error_payload
from kd2_rules_mcp.service.ed_reopen import reopen_details
from kd2_rules_mcp.service.registration_retarget import _manual_files, _notices
from tests.test_ed_registration_delivery import ATTR, DATA, HINTS, result
from tests.test_ed_writer_plan_content import add_content
from tests.test_service_ed_authoring import setup as setup
from tests.test_service_ed_writer import (
    PLAN,
    apply_packet,
    build,
    manager_operations,
)
from tests.test_service_ed_writer import (
    writer_setup as writer_setup,
)


@pytest.mark.parametrize(
    "path",
    [
        "C:/Synthetic/XDTOPackages/Example/Ext/Package.bin",
        r"C:\Synthetic\XDTOPackages\Example\Ext\Package.bin",
        "/projects/example/Main/XDTOPackages/Example/ext/package.bin",
        r"C:\Synthetic\XDTOPackages\Example.xml",
    ],
)
def test_legacy_schema_package_name_from_host_paths(path):
    details = reopen_details(
        {"project": "Example", "format_version": "1.20"},
        {"schema_sources": [[path, "digest"]]},
        schema=True,
    )
    assert details["reopen_calls"][0]["arguments"]["package"] == "Example"


def test_unknown_package_names_missing_reopen_parameter():
    details = reopen_details(
        {"project": "Example", "format_version": "1.20"},
        {},
        schema=True,
    )
    assert details["reopen_calls"] == []
    assert details["missing_reopen_parameters"] == [
        {"tool": "ed_schema_open", "fields": ["package"]}
    ]


@pytest.mark.parametrize("tool", ["repeat", "rebind", "validate", "candidates", "build", "apply"])
@pytest.mark.parametrize("legacy_metadata", [False, True])
def test_schema_reopen_call_executes_unchanged(setup, monkeypatch, tool, legacy_metadata):
    service, _, _ = setup
    opened = service.ed_schema_open(
        "1.20", project="Пример", configuration="Main", package="Формат120"
    )
    created = service.ed_create(
        "acceptance",
        project="Пример",
        configuration="Main",
        plan=PLAN,
        format_version="1.20",
        schema_id=opened["schema_id"],
        structure_id="fiction-main",
    )
    if legacy_metadata:
        path = service.manager_workspace.directory / "acceptance/creation.json"
        metadata = json.loads(path.read_bytes())
        for package in metadata["schema_packages"]:
            package.pop("name", None)
        path.write_text(json.dumps(metadata), encoding="utf-8")
    service.ed_schema_close(opened["schema_id"])
    if tool == "build":
        # Сборка умеет читать файлы без открытого снимка; воспроизводим отказ после перезапуска.
        metadata = service._manager_metadata("acceptance")
        metadata["schema_packages"] = []
        monkeypatch.setattr(service, "_manager_metadata", lambda _: metadata)
    if tool == "apply":

        def missing(*a, **kw):
            raise EdSchemaNotFoundError("Схема формата не открыта")

        monkeypatch.setattr(service, "_manager_inputs", missing)
    with pytest.raises((EdSchemaNotFoundError, EdAuthoringPreconditionError)) as caught:
        if tool in ("repeat", "rebind"):
            service.ed_create(
                "acceptance",
                mode="rebind" if tool == "rebind" else "new",
                **(
                    {}
                    if tool == "rebind"
                    else {
                        "project": "Пример",
                        "configuration": "Main",
                        "plan": PLAN,
                        "format_version": "1.20",
                        "schema_id": opened["schema_id"],
                        "structure_id": "fiction-main",
                    }
                ),
            )
        elif tool == "validate":
            service.ed_validate(created["document_id"], schema_id=opened["schema_id"])
        elif tool == "candidates":
            service.ed_authoring_candidates(
                {
                    "project_id": "acceptance",
                    "schema_id": opened["schema_id"],
                    "structure_id": "fiction-main",
                    "direction": "send",
                },
                "objects",
                scope="manager",
            )
        elif tool == "build":
            build(service, created)
        else:
            service.ed_apply("acceptance", created["revision"], [])
    payload = error_payload(caught.value, service)
    call = next(c for c in payload["reopen_calls"] if c["tool"] == "ed_schema_open")
    assert call["arguments"]["package"] == "Формат120"
    assert getattr(service, call["tool"])(**call["arguments"])["schema_id"] == opened["schema_id"]


def test_rebind_closed_structure_has_executable_reopen_call(writer_setup, monkeypatch):
    service, args, _ = writer_setup
    service.ed_create(**args)
    original = service._ed_structure_snapshot

    def missing(*a, **kw):
        raise StructureNotFoundError("Структура не открыта")

    monkeypatch.setattr(service, "_ed_structure_snapshot", missing)
    with pytest.raises(StructureNotFoundError) as caught:
        service.ed_create("positions", mode="rebind")
    monkeypatch.setattr(service, "_ed_structure_snapshot", original)
    call = error_payload(caught.value, service)["reopen_calls"][0]
    assert call["tool"] == "structure_load_xml"
    assert getattr(service, call["tool"])(**call["arguments"])["structure_id"] == "host"


@pytest.mark.parametrize(
    "filters",
    [
        {"check_prefix": "ed.plan"},
        {"level": "ошибка"},
        {"address_prefix": "ПКО/Должности"},
        {"address_prefix": "ПКО/Missing"},
        {"check_prefix": "ed.plan", "level": "предупреждение", "address_prefix": "пко/должности"},
    ],
)
def test_manager_notices_apply_issue_filters(writer_setup, filters):
    service, args, root = writer_setup
    tree = etree.parse(str(root / "Catalogs/Товары.xml"))
    name = tree.find("{*}Catalog/{*}Properties/{*}Name")
    catalog = tree.find("{*}Catalog")
    assert name is not None and catalog is not None
    name.text = "Должности"
    catalog.set("uuid", "44444444-4444-4444-8444-444444444444")
    tree.write(str(root / "Catalogs/Должности.xml"), encoding="utf-8")
    add_content(service, False)
    created = service.ed_create(**args)
    _, _, applied = apply_packet(service, created, manager_operations())
    all_notices = build(service, applied, section="notices", limit=200)
    selected = build(service, applied, section="notices", limit=200, **filters)
    expected = [
        n
        for n in all_notices["items"]
        if (
            not filters.get("check_prefix")
            or n.get("check", "").startswith(filters["check_prefix"])
        )
        and (not filters.get("level") or n.get("level") == filters["level"])
        and (
            not filters.get("address_prefix")
            or n.get("address", "").casefold().startswith(filters["address_prefix"].casefold())
        )
    ]
    assert selected["items"] == expected
    assert selected["total"] == len(expected)
    assert selected["required_acknowledgements"] == all_notices["required_acknowledgements"]
    assert selected["build_hash"] == all_notices["build_hash"]


def repeated_registration_notices():
    """Вход приёмки: восемь режимов и 52 объекта; только вымышленные имена."""
    notices = tuple(
        RetargetNotice(
            "registration.deletion_mode",
            f"ПРО/Rule{i}",
            "",
            "UnloadMode",
            f"ПРО/Rule{i}: проверьте режим узла в «UnloadMode». «По условию» или пустой: "
            "пометка и её снятие передаются с отбором. «При необходимости»: "
            "пометка уходит удалением и без нашего отбора; снятие пометки не регистрируется "
            "до следующего изменения объекта. После снятия нужно перезаписать объект "
            "или зарегистрировать его к отправке. «Выгружать всегда»: отбор не проверяется. "
            "«Вручную» и «Не выгружать»: помеченный объект снимается с регистрации "
            "без отправки удаления. При начальной выгрузке помеченные объекты не отправляются, "
            "кроме узлов с режимом «Выгружать всегда».",
            requires_acknowledgement=True,
        )
        for i in range(8)
    ) + tuple(
        RetargetNotice(
            "registration.deletion_missing_rule",
            "ПланОбмена.TargetPlan",
            "",
            f"Справочник.Object{i}",
            f"Объект «Справочник.Object{i}» входит в состав плана, "
            "но действующего правила регистрации нет. "
            "Он выгружается всегда; пометка удаления этим отбором не передастся.",
            requires_acknowledgement=True,
        )
        for i in range(52)
    )
    return replace(result(), deletion_mark_filter=True, notices=notices)


def registration_files(with_extension):
    transferred = repeated_registration_notices()
    notices = _notices(transferred, {ATTR.name.casefold()})
    if with_extension:
        return render_registration_kit(
            transferred,
            read_plan_host(DATA / "dump", "TargetPlan"),
            own_attributes=(ATTR,),
            node_values=HINTS,
            extension_name="reg_Registration",
            prefix="reg_",
            notices=[n["message"] for n in notices if n["requires_acknowledgement"]],
        ).files
    return _manual_files(transferred, "TargetPlan", list(HINTS), notices)


@pytest.mark.parametrize("with_extension", [False, True])
def test_registration_instruction_groups_repeated_notices(with_extension):
    transferred = repeated_registration_notices()
    before = _notices(transferred, {ATTR.name.casefold()})
    generated = registration_files(with_extension)
    instruction = generated["ИНСТРУКЦИЯ.md"].decode("utf-8")
    assert "проверьте режим узла в «UnloadMode»" not in instruction
    assert (
        instruction.count("Он выгружается всегда; пометка удаления этим отбором не передастся.")
        == 1
    )
    for i in range(8):
        assert instruction.count(f"| ПРО/Rule{i} | UnloadMode |") == 1
    for i in range(52):
        assert instruction.count(f"| Справочник.Object{i} |") == 1
    assert _notices(transferred, {ATTR.name.casefold()}) == before
    assert sum(n["requires_acknowledgement"] for n in before) == 60


@pytest.mark.parametrize("with_extension", [False, True])
def test_registration_grouped_instruction_golden(with_extension):
    root = Path(__file__).parent / "data/ed/registration/delivery/golden/grouped-notices"
    name = "extension" if with_extension else "manual"
    generated = registration_files(with_extension)
    assert generated["ИНСТРУКЦИЯ.md"] == (root / f"{name}.md").read_bytes()
    hashes = json.loads((root / f"{name}-unchanged.json").read_bytes())
    from kd2_rules_mcp.authoring.ed.manifest import sha256

    assert {
        p: sha256(b) for p, b in generated.items() if p not in ("ИНСТРУКЦИЯ.md", "manifest.json")
    } == hashes
