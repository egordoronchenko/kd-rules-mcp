"""Перенос регистрации сервисом: синтетические входы, хеши и владение файлами."""

import json
import os
import shutil
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

from kd2_rules_mcp.authoring.ed.manifest import json_bytes, sha256
from kd2_rules_mcp.errors import Kd2Error
from kd2_rules_mcp.kd2.model import Node, RegistrationRules
from kd2_rules_mcp.kd2.rules_io import dump_rules, load_registration_rules
from kd2_rules_mcp.server import error_payload
from kd2_rules_mcp.service import Kd2Service, Settings
from kd2_rules_mcp.service import registration_retarget as module

DATA = Path(__file__).parent / "data/ed/registration/delivery"
ATTRIBUTES = [{"name": "reg_Flag", "type": "Булево", "synonym": "Не выгружать данные"}]
EXTENSION = {"name": "reg_Registration", "prefix": "reg_"}


def setup(tmp_path: Path) -> tuple[Kd2Service, dict[str, Any]]:
    dump = tmp_path / "dump"
    shutil.copytree(DATA / "dump", dump)
    source = tmp_path / "RegistrationRules.xml"
    shutil.copyfile(DATA / "RegistrationRules.xml", source)
    service = Kd2Service(Settings(cache_dir=tmp_path / "cache", workspace=tmp_path / "workspace"))
    project = service.rules_open(str(source))
    service.structure_load_xml("target", str(dump))
    return service, {
        "project_id": project["project_id"],
        "exchange_plan": "TargetPlan",
        "node_properties": {"OldFlag": "reg_Flag", "OldDate": "DateStart"},
        "source": {"configuration_path": str(dump)},
        "structure_id": "target",
        "own_attributes": ATTRIBUTES,
        "extension": EXTENSION,
    }


def snapshot(root: Path) -> dict[str, str]:
    return {
        p.relative_to(root).as_posix(): sha256(p.read_bytes())
        for p in root.rglob("*")
        if p.is_file()
    }


def failure(service: Kd2Service, arguments: dict[str, Any], code: str) -> dict:
    with pytest.raises(Kd2Error) as caught:
        service.registration_retarget(**arguments)
    payload = error_payload(caught.value, service)
    assert payload["code"] == code
    return payload


def write(service: Kd2Service, arguments: dict[str, Any], preview: dict) -> dict:
    return service.registration_retarget(
        **(
            arguments
            | {
                "mode": "write",
                "expected_preview_hash": preview["preview_hash"],
                "acknowledged_notices": preview["required_acknowledgements"],
            }
        )
    )


def test_preview_write_repeat_and_original_unchanged(tmp_path: Path) -> None:
    service, args = setup(tmp_path)
    document = service.workspace.get(args["project_id"]).document
    assert document is not None
    original = sha256(dump_rules(document))
    before = snapshot(tmp_path)
    preview = service.registration_retarget(**args)
    assert snapshot(tmp_path) == before
    assert not Path(preview["output_path"]).exists()
    assert preview["counts"] == {
        "rules": 1,
        "renamed_leaves": 2,
        "untouched_leaves": 0,
        "unused_keys": 0,
        "code_mentions": 0,
    }
    assert preview["status"] == "ready"
    assert preview["notices"]["items"][0]["blocking"] is False
    assert preview["notices"]["items"][0]["address"]
    assert preview["required_acknowledgements"] == []
    assert len(preview["files"]) == 6
    written = write(service, args, preview)
    assert written["status"] == "written"
    assert written["message"] == "Комплект записан."
    target = Path(written["output_path"])
    assert snapshot(target) == {f["path"]: f["sha256"] for f in preview["files"]}
    restored = load_registration_rules(target / "registration/RegistrationRules.xml")
    assert sha256((target / "registration/RegistrationRules.xml").read_bytes()) == (
        "fcb4374e0135debaeccbc6abb1ff7b2dde6235d181fd63fee81010d77e35e1df"
    )
    assert restored.exchange_plan == "TargetPlan"
    filters = restored.rules()[0].child("ОтборПоСвойствамПланаОбмена")
    assert filters is not None
    assert [item.get("СвойствоПланаОбмена") for item in filters.items] == ["reg_Flag", "DateStart"]
    times = {p: p.stat().st_mtime_ns for p in target.rglob("*") if p.is_file()}
    repeated = write(service, args, preview)
    assert repeated["status"] == "unchanged"
    assert repeated["message"] == "Без изменений."
    assert {p: p.stat().st_mtime_ns for p in times} == times
    assert repeated["preview_hash"] == preview["preview_hash"]
    assert sha256(dump_rules(document)) == original
    assert service.workspace.get(args["project_id"]).modified is False
    assert (
        sha256((tmp_path / "RegistrationRules.xml").read_bytes()) == before["RegistrationRules.xml"]
    )


def test_write_requires_current_hash(tmp_path: Path) -> None:
    service, args = setup(tmp_path)
    preview = service.registration_retarget(**args)
    for digest in (None, "old"):
        payload = failure(
            service, args | {"mode": "write", "expected_preview_hash": digest}, "registration.stale"
        )
        assert payload["preview_hash"] == preview["preview_hash"]
    changed = args | {
        "node_values": [{"source": "OldFlag", "target": "reg_Flag", "instruction": "$value"}]
    }
    failure(
        service,
        changed | {"mode": "write", "expected_preview_hash": preview["preview_hash"]},
        "registration.stale",
    )
    assert not Path(preview["output_path"]).exists()


def test_code_mentions_need_acknowledgement_and_pages_are_complete(tmp_path: Path) -> None:
    service, args = setup(tmp_path)
    document = service.workspace.get(args["project_id"]).document
    assert isinstance(document, RegistrationRules)
    document.rules()[0].values["ПриОбработке"] = (
        "Значение = Узел.OldFlag;\nЗначение = Узел.OldDate;"
    )
    preview = service.registration_retarget(**args)
    assert preview["counts"]["code_mentions"] == 2
    rows = []
    offset = 0
    while True:
        paged: dict[str, Any] = args | {"offset": offset, "limit": 1}
        page = service.registration_retarget(**paged)
        assert page["preview_hash"] == preview["preview_hash"]
        rows.extend(page["notices"]["items"])
        if not page["notices"]["has_more"]:
            break
        offset += len(page["notices"]["items"])
    assert rows == preview["notices"]["items"]
    assert len({row["id"] for row in rows}) == len(rows) == 4
    mentions = [n for n in rows if n["check"] == "registration.code_mention"]
    assert [n["line"] for n in mentions] == [1, 2]
    assert all(n["address"] and n["event"] == "ПриОбработке" for n in mentions)
    writing = args | {"mode": "write", "expected_preview_hash": preview["preview_hash"]}
    payload = failure(service, writing, "registration.ack_required")
    assert set(payload["required_acknowledgements"]) == {
        n["id"] for n in rows if n["requires_acknowledgement"]
    }
    failure(
        service,
        writing | {"acknowledged_notices": [mentions[0]["id"]]},
        "registration.ack_required",
    )
    assert write(service, args, preview)["written"] is True


def test_missing_attribute_blocks_without_extension(tmp_path: Path) -> None:
    service, args = setup(tmp_path)
    args.update({"own_attributes": None, "extension": None})
    preview = service.registration_retarget(**args)
    assert preview["status"] == "blocked"
    assert preview["blocking_notices"] == 1
    assert {f["path"] for f in preview["files"]} == {
        "registration/RegistrationRules.xml",
        "ИНСТРУКЦИЯ.md",
    }
    payload = failure(
        service,
        args
        | {
            "mode": "write",
            "expected_preview_hash": preview["preview_hash"],
            "acknowledged_notices": [n["id"] for n in preview["notices"]["items"]],
        },
        "registration.missing_attribute",
    )
    assert payload["failures"][0]["property_name"] == "reg_Flag"
    assert payload["failures"][0]["address"]
    assert not Path(preview["output_path"]).exists()


def test_dump_blocks_missing_attribute_without_structure(tmp_path: Path) -> None:
    service, args = setup(tmp_path)
    args = args | {
        "own_attributes": None,
        "extension": None,
        "node_properties": {"OldFlag": "DateStart", "OldDate": "OtherDate"},
        "structure_id": None,
        "node_values": [{"source": "OldFlag", "target": "DateStart", "instruction": "$value"}],
    }
    preview = service.registration_retarget(**args)
    assert len(preview["files"]) == 2
    assert all(
        n["check"] != "registration.structure_unchecked" for n in preview["notices"]["items"]
    )
    assert preview["blocking_notices"] == 2
    assert {n["check"] for n in preview["notices"]["items"] if n["blocking"]} == {
        "registration.missing_attribute",
        "registration.attribute_type",
    }
    failure(
        service,
        args | {"mode": "write", "expected_preview_hash": preview["preview_hash"]},
        "registration.missing_attribute",
    )
    assert not Path(preview["output_path"]).exists()


@pytest.mark.parametrize(
    "change", ["foreign", "rules", "manifest", "empty_dir", "owner", "owner_hash"]
)
def test_foreign_or_edited_kit_is_preserved(tmp_path: Path, change: str) -> None:
    service, args = setup(tmp_path)
    preview = service.registration_retarget(**args)
    target = Path(preview["output_path"])
    if change == "foreign":
        target.mkdir(parents=True)
        (target / "foreign.txt").write_text("чужой файл", "utf-8")
    else:
        write(service, args, preview)
        if change == "empty_dir":
            (target / "foreign").mkdir()
        elif change == "owner_hash":
            path = target.parent / "ownership.json"
            receipt = json.loads(path.read_bytes())
            receipt["preview_hash"] = "edited"
            path.write_bytes(json_bytes(receipt))
        else:
            path = {
                "rules": target / "registration/RegistrationRules.xml",
                "manifest": target / "manifest.json",
                "owner": target.parent / "ownership.json",
            }[change]
            path.write_bytes(b"changed")
    before = snapshot(tmp_path)
    failure(
        service,
        args | {"mode": "write", "expected_preview_hash": preview["preview_hash"]},
        "registration.owned_content_changed",
    )
    assert snapshot(tmp_path) == before


def test_manual_kit_with_verified_plan_fields(tmp_path: Path) -> None:
    service, args = setup(tmp_path)
    document = service.workspace.get(args["project_id"]).document
    assert isinstance(document, RegistrationRules)
    filters = document.rules()[0].child("ОтборПоСвойствамПланаОбмена")
    assert filters is not None
    filters.items[:] = [
        item for item in filters.items if item.get("СвойствоПланаОбмена") == "OldDate"
    ]
    args.update({"own_attributes": None, "extension": None})
    before = sha256(dump_rules(document))
    preview = service.registration_retarget(**args)
    assert [n["check"] for n in preview["notices"]["items"]] == ["registration.unsaved_changes"]
    assert preview["status"] == "ready"
    assert preview["counts"]["unused_keys"] == 1
    written = write(service, args, preview)
    assert len(snapshot(Path(written["output_path"]))) == 2
    assert write(service, args, preview)["status"] == "unchanged"
    assert sha256(dump_rules(document)) == before


def test_exchange_project_is_rejected(tmp_path: Path) -> None:
    service, args = setup(tmp_path)
    exchange = service.rules_open(str(Path(__file__).parent / "data/exchange_rules.xml"))
    failure(service, args | {"project_id": exchange["project_id"]}, "registration.not_registration")


def test_foreign_structure_is_rejected(tmp_path: Path) -> None:
    service, args = setup(tmp_path)
    other = tmp_path / "other"
    shutil.copytree(tmp_path / "dump", other)
    service.structure_load_xml("foreign", str(other))
    payload = failure(service, args | {"structure_id": "foreign"}, "registration.snapshot_mismatch")
    assert payload["failures"][0]["address"] == "ПланОбмена.TargetPlan"


def test_changed_dump_and_source_file_are_rejected(tmp_path: Path) -> None:
    service, args = setup(tmp_path)
    preview = service.registration_retarget(**args)
    path = tmp_path / "dump/ExchangePlans/TargetPlan.xml"
    path.write_bytes(path.read_bytes() + b"\n")
    failure(service, args, "registration.snapshot_mismatch")
    service.structure_load_xml("target", str(tmp_path / "dump"), force=True)
    failure(
        service,
        args | {"mode": "write", "expected_preview_hash": preview["preview_hash"]},
        "registration.stale",
    )
    source = tmp_path / "RegistrationRules.xml"
    source.write_bytes(source.read_bytes() + b"\n")
    failure(service, args, "registration.stale")


def test_owned_kit_survives_restart_and_changed_decisions(tmp_path: Path) -> None:
    service, args = setup(tmp_path)
    preview = service.registration_retarget(**args)
    write(service, args, preview)
    restarted = Kd2Service(service.settings)
    assert write(restarted, args, preview)["status"] == "unchanged"
    changed: dict[str, Any] = args | {
        "node_values": [{"source": "OldFlag", "target": "reg_Flag", "instruction": "Перенести"}]
    }
    updated = restarted.registration_retarget(**changed)
    assert updated["output_path"] == preview["output_path"]
    assert updated["preview_hash"] != preview["preview_hash"]
    assert write(restarted, changed, updated)["status"] == "written"


@pytest.mark.parametrize(
    ("changes", "code"),
    [
        ({"exchange_plan": "Missing"}, "registration.plan_not_found"),
        ({"exchange_plan": "../Plan"}, "registration.invalid_name"),
        (
            {"node_properties": {"OldFlag": "DateStart", "OldDate": "DateStart"}},
            "registration.duplicate_target",
        ),
        (
            {"own_attributes": [{"name": "reg_Flag", "type": "Строка", "synonym": "Флаг"}]},
            "registration.delivery_profile",
        ),
        (
            {
                "own_attributes": [{"name": "reg_Date", "type": "Булево", "synonym": "Дата"}],
                "extension": {"name": "reg_Ext", "prefix": "wrong_"},
            },
            "registration.attribute_prefix",
        ),
    ],
)
def test_lower_errors_have_codes_and_addresses(tmp_path: Path, changes: dict, code: str) -> None:
    service, args = setup(tmp_path)
    payload = failure(service, args | changes, code)
    assert payload["failures"][0]["address"]
    assert payload["message"]


def test_failed_publication_restores_previous_kit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, args = setup(tmp_path)
    preview = service.registration_retarget(**args)
    write(service, args, preview)
    target = Path(preview["output_path"]).parent
    before = snapshot(target)
    changed: dict[str, Any] = args | {"node_values": [{"source": "OldFlag", "target": "reg_Flag"}]}
    updated = service.registration_retarget(**changed)
    original = module.os.replace

    def fail_publish(source, destination):
        if (
            Path(source).name.startswith(".staging-")
            and Path(destination) == target
            and not Path(source).name.endswith("-previous")
        ):
            raise OSError("synthetic write failure")
        return original(source, destination)

    monkeypatch.setattr(module.os, "replace", fail_publish)
    failure(
        service,
        changed | {"mode": "write", "expected_preview_hash": updated["preview_hash"]},
        "registration.io",
    )
    assert snapshot(target) == before


def test_project_source_and_explicit_extension_selection(tmp_path: Path) -> None:
    service, args = setup(tmp_path)
    initial = service.registration_retarget(**args)
    written = write(service, args, initial)
    shutil.copytree(Path(written["output_path"]) / "extension", tmp_path / "selected")
    projects = tmp_path / "projects.yaml"
    projects.write_text(
        "projects:\n  demo:\n    name: Test\n    configurations:\n"
        "      full:\n        dump: dump\n        extensions: [selected]\n",
        "utf-8",
    )
    service = Kd2Service(
        Settings(
            cache_dir=tmp_path / "cache",
            workspace=tmp_path / "workspace",
            projects_file=projects,
            project_dirs={"demo": tmp_path},
        )
    )
    arguments: dict[str, Any] = args | {
        "source": {"project": "demo", "configuration": "full"},
        "structure_id": None,
        "own_attributes": None,
        "extension": None,
    }
    result = service.registration_retarget(**arguments)
    assert result["counts"]["renamed_leaves"] == 2
    assert result["blocking_notices"] == 0
    first = write(service, arguments, result)
    before = snapshot(Path(first["output_path"]))
    explicit: dict[str, Any] = arguments | {"source": {"project": "demo", "extensions": []}}
    omitted = service.registration_retarget(**explicit)
    assert omitted["blocking_notices"] == 1
    assert omitted["output_path"] != result["output_path"]
    failure(
        service,
        explicit | {"mode": "write", "expected_preview_hash": omitted["preview_hash"]},
        "registration.missing_attribute",
    )
    assert snapshot(Path(first["output_path"])) == before
    failure(service, arguments | {"structure_id": "target"}, "registration.snapshot_mismatch")


def change_source(service: Kd2Service, args: dict, source: Path, old: str, new: str) -> None:
    service.rules_close(args["project_id"])
    source.write_text(source.read_text("utf-8").replace(old, new), "utf-8")
    args["project_id"] = service.rules_open(str(source))["project_id"]


@pytest.mark.parametrize(
    "reference,mapping,own",
    [
        ("[OldRows].Field", {"[OldRows]": "reg_Rows"}, "reg_Rows"),
        ("[OldRows]", {"[OldRows]": "reg_Rows"}, "reg_Rows"),
        (
            "[OldRows].Field",
            {"[OldRows]": "Organizations", "[OldRows].Field": "reg_Field"},
            "reg_Field",
        ),
        ("OldFlag.Code", {"OldFlag": "reg_Flag"}, "reg_Flag"),
    ],
)
def test_own_header_never_covers_tabular_or_dereference(
    tmp_path: Path, reference: str, mapping: dict, own: str
) -> None:
    service, args = setup(tmp_path)
    change_source(service, args, tmp_path / "RegistrationRules.xml", ">OldFlag<", f">{reference}<")
    args.update(
        node_properties=mapping | {"OldDate": "DateStart"},
        own_attributes=[{"name": own, "type": "Булево", "synonym": "Тест"}],
    )
    preview = service.registration_retarget(**args)
    assert preview["status"] == "blocked"
    assert preview["blocking_notices"] >= 1
    assert any(
        n["reference"].startswith("[") or "." in n["reference"]
        for n in preview["notices"]["items"]
        if n["blocking"]
    )
    failure(
        service,
        args
        | {
            "mode": "write",
            "expected_preview_hash": preview["preview_hash"],
            "acknowledged_notices": [n["id"] for n in preview["notices"]["items"]],
        },
        "registration.missing_attribute",
    )
    assert not Path(preview["output_path"]).exists()


@pytest.mark.parametrize(
    "value", ["2020-01-01T00:00:00", "1", '"строка"', "СправочникСсылка.TestObjects"]
)
def test_own_boolean_rejects_other_comparison_types(tmp_path: Path, value: str) -> None:
    service, args = setup(tmp_path)
    change_source(service, args, tmp_path / "RegistrationRules.xml", ">false<", f">{value}<")
    preview = service.registration_retarget(**args)
    assert preview["status"] == "blocked"
    assert (
        "булев" in next(n["message"] for n in preview["notices"]["items"] if n["blocking"]).lower()
    )
    failure(
        service,
        args | {"mode": "write", "expected_preview_hash": preview["preview_hash"]},
        "registration.missing_attribute",
    )


def test_algorithmic_comparison_is_not_assumed_boolean(tmp_path: Path) -> None:
    service, args = setup(tmp_path)
    change_source(
        service,
        args,
        tmp_path / "RegistrationRules.xml",
        "<Вид>Константа</Вид><ЗначениеКонстанты>false</ЗначениеКонстанты>",
        "<Вид>АлгоритмЗначения</Вид><ТипСвойстваОбъекта>Булево</ТипСвойстваОбъекта>"
        "<СвойствоОбъекта>Flag</СвойствоОбъекта>"
        "<ЗначениеКонстанты>Значение = 1;</ЗначениеКонстанты>",
    )
    preview = service.registration_retarget(**args)
    assert preview["status"] == "blocked"
    failure(
        service,
        args | {"mode": "write", "expected_preview_hash": preview["preview_hash"]},
        "registration.missing_attribute",
    )


def test_own_boolean_cannot_be_unload_mode(tmp_path: Path) -> None:
    service, args = setup(tmp_path)
    change_source(
        service,
        args,
        tmp_path / "RegistrationRules.xml",
        "<Код>001</Код>",
        "<Код>001</Код><РеквизитРежимаВыгрузки>OldMode</РеквизитРежимаВыгрузки>",
    )
    args["node_properties"] = args["node_properties"] | {"OldMode": "reg_Mode"}
    args["own_attributes"] = [
        *ATTRIBUTES,
        {"name": "reg_Mode", "type": "Булево", "synonym": "Режим"},
    ]
    preview = service.registration_retarget(**args)
    assert preview["status"] == "blocked"
    assert any(
        n["leaf"] == "РеквизитРежимаВыгрузки" and n["blocking"] for n in preview["notices"]["items"]
    )
    failure(
        service,
        args | {"mode": "write", "expected_preview_hash": preview["preview_hash"]},
        "registration.missing_attribute",
    )


@pytest.mark.parametrize(
    "case,code",
    [
        ("empty", "registration.empty_rules"),
        ("same", "registration.already_targeted"),
        ("no_source", "registration.source_required"),
    ],
)
def test_unsafe_project_preconditions(tmp_path: Path, case: str, code: str) -> None:
    service, args = setup(tmp_path)
    if case == "empty":
        document = service.workspace.get(args["project_id"]).document
        assert document is not None
        document.section("ПравилаРегистрацииОбъектов").items.clear()
        source = tmp_path / "RegistrationRules.xml"
        source.write_bytes(dump_rules(document))
        service.rules_close(args["project_id"])
        args["project_id"] = service.rules_open(str(source))["project_id"]
    elif case == "same":
        change_source(service, args, tmp_path / "RegistrationRules.xml", "OldPlan", "TargetPlan")
        change_source(service, args, tmp_path / "RegistrationRules.xml", "OldFlag", "reg_Flag")
        change_source(service, args, tmp_path / "RegistrationRules.xml", "OldDate", "DateStart")
        args["node_properties"] = {}
    else:
        built = service.registration_build("target", "TargetPlan", None, None, project_id="fresh")
        args["project_id"] = built["project_id"]
    before = snapshot(tmp_path)
    payload = failure(service, args, code)
    assert payload["message"]
    assert snapshot(tmp_path) == before


@pytest.mark.parametrize("plan", [None, ""])
def test_invalid_plan_address_has_no_empty_component(tmp_path: Path, plan: Any) -> None:
    service, args = setup(tmp_path)
    payload = failure(service, args | {"exchange_plan": plan}, "registration.invalid_name")
    assert payload["failures"][0]["address"] == "ПланОбмена"


@pytest.mark.parametrize("disk_changed", [False, True])
def test_manifest_hashes_memory_and_disk_separately(tmp_path: Path, disk_changed: bool) -> None:
    service, args = setup(tmp_path)
    document = service.workspace.get(args["project_id"]).document
    assert isinstance(document, RegistrationRules)
    source = tmp_path / "RegistrationRules.xml"
    if disk_changed:
        stat = source.stat()
        source.write_bytes(source.read_bytes().replace(b"OldFlag", b"OldFlab"))
        os.utime(source, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    else:
        document.rules()[0].values["Наименование"] = "Правка в памяти"
    model_hash = sha256(dump_rules(document))
    file_hash = sha256(source.read_bytes())
    preview = service.registration_retarget(**args)
    notice = next(
        n for n in preview["notices"]["items"] if n["check"] == "registration.unsaved_changes"
    )
    assert notice["requires_acknowledgement"]
    failure(
        service,
        args | {"mode": "write", "expected_preview_hash": preview["preview_hash"]},
        "registration.ack_required",
    )
    written = write(service, args, preview)
    kit = Path(written["output_path"])
    hashes = json.loads((kit / "manifest.json").read_bytes())["input_hashes"]
    assert hashes["source_rules"] == model_hash
    assert hashes["source_file"] == file_hash
    assert hashes["source_rules"] != hashes["source_file"]
    instruction = (kit / "ИНСТРУКЦИЯ.md").read_text("utf-8")
    assert instruction.index("несохранённые правки") < instruction.index("## 1.")
    assert sha256(dump_rules(document)) == model_hash


def test_directory_distinguishes_reused_id_with_another_source(tmp_path: Path) -> None:
    service, args = setup(tmp_path)
    preview = service.registration_retarget(**args)
    write(service, args, preview)
    original = snapshot(Path(preview["output_path"]))
    # Проверяем принадлежность даже при повторном использовании ID рабочего проекта.
    other = tmp_path / "other.xml"
    shutil.copy2(tmp_path / "RegistrationRules.xml", other)
    service.workspace.get(args["project_id"]).source_path = other
    updated = service.registration_retarget(**args)
    assert updated["output_path"] != preview["output_path"]
    write(service, args, updated)
    assert snapshot(Path(preview["output_path"])) == original


def test_delivery_receives_unfiltered_missing_references(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, args = setup(tmp_path)
    args["node_properties"] = {"OldFlag": "reg_Flag", "OldDate": "MissingDate"}
    original = module.render_registration_kit
    seen = []

    def capture(result, *positional, **keywords):
        seen.extend(r.reference for r in result.remarks)
        return original(result, *positional, **keywords)

    monkeypatch.setattr(module, "render_registration_kit", capture)
    preview = service.registration_retarget(**args)
    assert seen == ["reg_Flag", "MissingDate"]
    assert preview["status"] == "blocked" and preview["files"] == []


def test_two_valid_extension_selections_keep_separate_kits(tmp_path: Path) -> None:
    service, args = setup(tmp_path)
    seeded = write(service, args, service.registration_retarget(**args))
    extension = tmp_path / "selected"
    shutil.copytree(Path(seeded["output_path"]) / "extension", extension)
    document = service.workspace.get(args["project_id"]).document
    assert isinstance(document, RegistrationRules)
    filters = document.rules()[0].child("ОтборПоСвойствамПланаОбмена")
    assert filters is not None
    filters.items[:] = [
        item for item in filters.items if item.get("СвойствоПланаОбмена") == "OldDate"
    ]
    args.update(structure_id=None, own_attributes=None, extension=None)
    first = service.registration_retarget(**args)
    write(service, args, first)
    before = snapshot(Path(first["output_path"]))
    other: dict[str, Any] = args | {
        "source": {"configuration_path": str(tmp_path / "dump"), "extensions": [str(extension)]}
    }
    second = service.registration_retarget(**other)
    assert second["status"] == first["status"] == "ready"
    assert second["output_path"] != first["output_path"]
    write(service, other, second)
    assert snapshot(Path(first["output_path"])) == before


@pytest.mark.parametrize(
    "value,type_name,constant,blocked",
    [
        ("false", "Булево", True, False),
        ("Flag", "Булево", False, False),
        ("false", "Строка", True, True),
        ("2020-01-01", "Дата", True, True),
        ("1", "Число", True, True),
        ("Item", "СправочникСсылка.TestObjects", False, True),
        ("Unknown", "", False, True),
    ],
)
def test_own_boolean_comparison_in_kd2_leaf(
    tmp_path: Path, value: str, type_name: str, constant: bool, blocked: bool
) -> None:
    service, args = setup(tmp_path)
    old = "<Вид>Константа</Вид><ЗначениеКонстанты>false</ЗначениеКонстанты>"
    new = (
        f"<ЭтоСтрокаКонстанты>{str(constant).lower()}</ЭтоСтрокаКонстанты>"
        f"<ТипСвойстваОбъекта>{type_name}</ТипСвойстваОбъекта>"
        f"<СвойствоОбъекта>{value}</СвойствоОбъекта>"
    )
    change_source(service, args, tmp_path / "RegistrationRules.xml", old, new)
    preview = service.registration_retarget(**args)
    assert (preview["status"] == "blocked") == blocked
    if blocked:
        failure(
            service,
            args | {"mode": "write", "expected_preview_hash": preview["preview_hash"]},
            "registration.missing_attribute",
        )
    else:
        assert write(service, args, preview)["status"] == "written"


def test_dump_is_checked_without_structure(tmp_path: Path) -> None:
    service, args = setup(tmp_path)
    result = module.retarget_registration(
        service._document(args["project_id"]),
        plan_name="TargetPlan",
        node_properties=args["node_properties"],
    )
    assert result.document.exchange_plan == "TargetPlan"
    arguments: dict[str, Any] = args | {"structure_id": None}
    preview = service.registration_retarget(**arguments)
    assert preview["status"] == "ready"
    assert not any(
        n["check"] == "registration.structure_unchecked" for n in preview["notices"]["items"]
    )


def single_leaf(service: Kd2Service, args: dict[str, Any], reference: str, type_name: str) -> None:
    """Один типизированный лист; правки памяти одинаковы для обеих веток сверки."""
    document = service.workspace.get(args["project_id"]).document
    assert isinstance(document, RegistrationRules)
    filters = document.rules()[0].child("ОтборПоСвойствамПланаОбмена")
    assert filters is not None
    filters.items[:] = filters.items[:1]
    leaf = filters.items[0]
    leaf.unknown.clear()
    leaf.values.update(
        СвойствоПланаОбмена=reference,
        ЭтоСтрокаКонстанты=True,
        ТипСвойстваОбъекта=type_name,
        СвойствоОбъекта="true" if type_name == "Булево" else "Значение",
    )
    args.update(node_properties={}, own_attributes=None, extension=None)


def both_previews(service: Kd2Service, args: dict[str, Any]) -> tuple[dict, dict]:
    """Карточка структуры и карточка выгрузки должны давать одинаковые замечания."""
    structured = service.registration_retarget(**args)
    arguments: dict[str, Any] = args | {"structure_id": None}
    dumped = service.registration_retarget(**arguments)
    for field in ("status", "counts", "blocking_notices", "notices", "required_acknowledgements"):
        assert structured[field] == dumped[field]
    return structured, dumped


@pytest.mark.parametrize(
    "reference,type_name,blocked",
    [
        ("[Organizations]", "Булево", True),
        ("[Organizations].Organization", "Строка", False),
        ("[Organizations].Absent", "Строка", True),
        ("DateStart", "Дата", False),
        ("Absent", "Дата", True),
        ("DateStart.Field", "Дата", True),
    ],
)
def test_reference_kinds_have_identical_checks(
    tmp_path: Path, reference: str, type_name: str, blocked: bool
) -> None:
    service, args = setup(tmp_path)
    single_leaf(service, args, reference, type_name)
    previews = both_previews(service, args)
    assert (previews[0]["status"] == "blocked") == blocked
    if blocked:
        for structure_id, preview in zip(("target", None), previews, strict=True):
            failure(
                service,
                args
                | {
                    "structure_id": structure_id,
                    "mode": "write",
                    "expected_preview_hash": preview["preview_hash"],
                },
                "registration.missing_attribute",
            )


@pytest.mark.parametrize("type_name", ["Булево", "Строка", "Число", "СправочникСсылка.Items"])
def test_existing_attribute_type_mismatch_blocks_both_sources(
    tmp_path: Path, type_name: str
) -> None:
    service, args = setup(tmp_path)
    single_leaf(service, args, "DateStart", type_name)
    preview, _ = both_previews(service, args)
    assert preview["status"] == "blocked"
    notice = next(n for n in preview["notices"]["items"] if n["blocking"])
    assert notice["check"] == "registration.attribute_type"
    assert "Дата" in notice["message"] and type_name in notice["message"]


def test_defined_node_type_matches_structure_and_participates_in_preview_hash(
    tmp_path: Path,
) -> None:
    service, args = setup(tmp_path)
    dump = tmp_path / "dump"
    plan = dump / "ExchangePlans/TargetPlan.xml"
    plan.write_text(
        plan.read_text("utf-8").replace(
            "<v8:Type>xs:dateTime</v8:Type>", "<v8:TypeSet>cfg:DefinedType.StartValue</v8:TypeSet>"
        ),
        "utf-8",
    )
    config = dump / "Configuration.xml"
    config.write_text(
        config.read_text("utf-8").replace(
            "</ChildObjects>", "<DefinedType>StartValue</DefinedType></ChildObjects>"
        ),
        "utf-8",
    )
    (dump / "DefinedTypes").mkdir()
    defined = dump / "DefinedTypes/StartValue.xml"
    defined.write_text(
        '<MetaDataObject xmlns="http://v8.1c.ru/8.3/MDClasses" '
        'xmlns:v8="http://v8.1c.ru/8.1/data/core"><DefinedType><Properties>'
        "<Name>StartValue</Name><Type><v8:Type>xs:dateTime</v8:Type></Type>"
        "</Properties></DefinedType></MetaDataObject>",
        "utf-8",
    )
    service.structure_load_xml("target", str(dump), force=True)
    single_leaf(service, args, "DateStart", "Дата")
    structured, dumped = both_previews(service, args)
    assert structured["status"] == "ready"
    defined.write_text(defined.read_text("utf-8").replace("xs:dateTime", "xs:boolean"), "utf-8")
    failure(
        service,
        args
        | {"structure_id": None, "mode": "write", "expected_preview_hash": dumped["preview_hash"]},
        "registration.stale",
    )


@pytest.mark.parametrize(
    "xml_type,comparison,blocked",
    [
        ("xs:boolean", "Булево", False),
        ("xs:dateTime", "Дата", False),
        ("xs:decimal", "Число", False),
        ("xs:string", "Строка", False),
        ("cfg:CatalogRef.Items", "СправочникСсылка.Items", False),
        ("cfg:CatalogRef.Items", "СправочникСсылка.OtherItems", True),
        ("cfg:EnumRef.OtherMode", "ПеречислениеСсылка.РежимыВыгрузкиОбъектовОбмена", True),
    ],
)
def test_existing_comparison_uses_primitive_or_named_reference_type(
    tmp_path: Path, xml_type: str, comparison: str, blocked: bool
) -> None:
    service, args = setup(tmp_path)
    dump = tmp_path / "dump"
    plan = dump / "ExchangePlans/TargetPlan.xml"
    plan.write_text(plan.read_text("utf-8").replace("xs:dateTime", xml_type), "utf-8")
    if "Ref." in xml_type:
        tag, _, name = xml_type.removeprefix("cfg:").partition("Ref.")
        directory = "Catalogs" if tag == "Catalog" else "Enums"
        (dump / directory).mkdir()
        (dump / directory / f"{name}.xml").write_text(
            f'<MetaDataObject xmlns="http://v8.1c.ru/8.3/MDClasses"><{tag}>'
            f"<Properties><Name>{name}</Name></Properties></{tag}></MetaDataObject>",
            "utf-8",
        )
        config = dump / "Configuration.xml"
        config.write_text(
            config.read_text("utf-8").replace(
                "</ChildObjects>", f"<{tag}>{name}</{tag}></ChildObjects>"
            ),
            "utf-8",
        )
    service.structure_load_xml("target", str(dump), force=True)
    single_leaf(service, args, "DateStart", comparison)
    preview, _ = both_previews(service, args)
    assert (preview["status"] == "blocked") == blocked
    assert not any(n["check"] == "registration.type_unchecked" for n in preview["notices"]["items"])
    if not blocked:
        assert write(service, args, preview)["status"] == "written"


@pytest.mark.parametrize(
    "target_type,blocked",
    [("xs:dateTime", True), ("cfg:EnumRef.РежимыВыгрузкиОбъектовОбмена", False)],
)
def test_existing_unload_mode_requires_specific_enumeration(
    tmp_path: Path, target_type: str, blocked: bool
) -> None:
    service, args = setup(tmp_path)
    plan = tmp_path / "dump/ExchangePlans/TargetPlan.xml"
    plan.write_text(plan.read_text("utf-8").replace("xs:dateTime", target_type), "utf-8")
    service.structure_load_xml("target", str(tmp_path / "dump"), force=True)
    single_leaf(service, args, "[Organizations].Organization", "Строка")
    document = service.workspace.get(args["project_id"]).document
    assert isinstance(document, RegistrationRules)
    document.rules()[0].values["РеквизитРежимаВыгрузки"] = "DateStart"
    preview, _ = both_previews(service, args)
    assert (preview["status"] == "blocked") == blocked
    if blocked:
        assert any(
            n["leaf"] == "РеквизитРежимаВыгрузки" and n["blocking"]
            for n in preview["notices"]["items"]
        )


@pytest.mark.parametrize("unknown", ["node", "comparison"])
@pytest.mark.parametrize("extended", [False, True])
def test_unknown_existing_type_requires_ack_in_instruction(
    tmp_path: Path, unknown: str, extended: bool
) -> None:
    service, args = setup(tmp_path)
    if unknown == "node":
        plan = tmp_path / "dump/ExchangePlans/TargetPlan.xml"
        plan.write_text(
            plan.read_text("utf-8").replace("<v8:Type>xs:dateTime</v8:Type>", ""), "utf-8"
        )
        service.structure_load_xml("target", str(tmp_path / "dump"), force=True)
    single_leaf(service, args, "DateStart", "" if unknown == "comparison" else "Дата")
    if unknown == "comparison":
        document = service.workspace.get(args["project_id"]).document
        assert isinstance(document, RegistrationRules)
        filters = document.rules()[0].child("ОтборПоСвойствамПланаОбмена")
        assert filters is not None
        filters.items[0].values["ЭтоСтрокаКонстанты"] = False
    if extended:
        args.update(own_attributes=ATTRIBUTES, extension=EXTENSION)
    preview, _ = both_previews(service, args)
    notice = next(
        n for n in preview["notices"]["items"] if n["check"] == "registration.type_unchecked"
    )
    assert notice["requires_acknowledgement"] and not notice["blocking"]
    failure(
        service,
        args | {"mode": "write", "expected_preview_hash": preview["preview_hash"]},
        "registration.ack_required",
    )
    written = write(service, args, preview)
    instruction = (Path(written["output_path"]) / "ИНСТРУКЦИЯ.md").read_text("utf-8")
    assert (
        instruction.index("Перенос неполон")
        < instruction.index(notice["message"])
        < instruction.index("## 1.")
    )


def test_own_boolean_constant_without_declared_type_is_blocked(tmp_path: Path) -> None:
    service, args = setup(tmp_path)
    single_leaf(service, args, "OldFlag", "")
    document = service.workspace.get(args["project_id"]).document
    assert isinstance(document, RegistrationRules)
    filters = document.rules()[0].child("ОтборПоСвойствамПланаОбмена")
    assert filters is not None
    filters.items[0].values["СвойствоОбъекта"] = "true"
    args.update(
        node_properties={"OldFlag": "reg_Flag"}, own_attributes=ATTRIBUTES, extension=EXTENSION
    )
    preview, _ = both_previews(service, args)
    assert preview["status"] == "blocked"
    assert any(
        n["blocking"] and "булев" in n["message"].lower() for n in preview["notices"]["items"]
    )


def test_constant_without_type_cannot_use_existing_attribute(tmp_path: Path) -> None:
    service, args = setup(tmp_path)
    single_leaf(service, args, "DateStart", "")
    preview, _ = both_previews(service, args)
    assert preview["status"] == "blocked"
    notice = next(n for n in preview["notices"]["items"] if n["blocking"])
    assert "константы" in notice["message"] and "не сможет загрузить" in notice["message"]


@pytest.mark.parametrize("targeted", [False, True])
def test_all_disabled_rules_are_rejected(tmp_path: Path, targeted: bool) -> None:
    service, args = setup(tmp_path)
    document = service.workspace.get(args["project_id"]).document
    assert isinstance(document, RegistrationRules)
    for rule in document.rules():
        rule.attrs["Отключить"] = True
    if targeted:
        plan = document.root.child("ПланОбмена")
        assert plan is not None
        plan.attrs["Имя"] = "TargetPlan"
        args["node_properties"] = {}
    payload = failure(service, args, "registration.empty_rules")
    assert "отключ" in payload["message"].lower()


def test_same_plan_with_applicable_mapping_retargets_properties(tmp_path: Path) -> None:
    service, args = setup(tmp_path)
    change_source(service, args, tmp_path / "RegistrationRules.xml", "OldPlan", "TargetPlan")
    preview, _ = both_previews(service, args)
    assert preview["status"] == "ready"
    assert preview["counts"]["renamed_leaves"] == 2
    written = write(service, args, preview)
    restored = load_registration_rules(
        Path(written["output_path"]) / "registration/RegistrationRules.xml"
    )
    assert restored.exchange_plan == "TargetPlan"
    filters = restored.rules()[0].child("ОтборПоСвойствамПланаОбмена")
    assert filters is not None
    assert [item.get("СвойствоПланаОбмена") for item in filters.items] == ["reg_Flag", "DateStart"]
    second = service.rules_open(
        str(Path(written["output_path"]) / "registration/RegistrationRules.xml")
    )
    failure(
        service,
        args | {"project_id": second["project_id"], "node_properties": {}},
        "registration.already_targeted",
    )


@pytest.mark.parametrize(
    "reference,canonical",
    [("datestart", "DateStart"), ("[organizations].organization", "[Organizations].Organization")],
)
def test_metadata_case_is_canonicalized_in_written_rules(
    tmp_path: Path, reference: str, canonical: str
) -> None:
    service, args = setup(tmp_path)
    single_leaf(service, args, reference, "Дата" if canonical == "DateStart" else "Строка")
    preview, _ = both_previews(service, args)
    assert preview["status"] == "ready"
    notice = next(
        n for n in preview["notices"]["items"] if n["check"] == "registration.property_case"
    )
    assert reference in notice["message"] and canonical in notice["message"]
    assert not notice["blocking"] and not notice["requires_acknowledgement"]
    written = write(service, args, preview)
    restored = load_registration_rules(
        Path(written["output_path"]) / "registration/RegistrationRules.xml"
    )
    filters = restored.rules()[0].child("ОтборПоСвойствамПланаОбмена")
    assert filters is not None
    assert filters.items[0].get("СвойствоПланаОбмена") == canonical


def add_boolean_field(service: Kd2Service, tmp_path: Path) -> None:
    plan = tmp_path / "dump/ExchangePlans/TargetPlan.xml"
    attribute = (
        '<Attribute uuid="7bc6d79a-14ca-4793-aed7-8a0947c06087"><Properties>'
        "<Name>FlagB</Name><Type><v8:Type>xs:boolean</v8:Type></Type>"
        "</Properties></Attribute>"
    )
    plan.write_text(
        plan.read_text("utf-8").replace("<ChildObjects>", "<ChildObjects>" + attribute, 1), "utf-8"
    )
    service.structure_load_xml("target", str(tmp_path / "dump"), force=True)


@pytest.mark.parametrize(
    "second,mapping,code",
    [
        ("FlagB", {"OldFlag": "FlagB"}, "registration.property_clash"),
        ("flagb", {"OldFlag": "FlagB"}, "registration.property_clash"),
        ("FlagB", {"OldFlag": "flagb"}, "registration.property_clash"),
        ("flagb", {"OLDFLAG": "FLAGB"}, "registration.property_clash"),
        ("OldB", {"OldFlag": "FlagB", "OldB": "FlagB"}, "registration.duplicate_target"),
        ("OldB", {"OldFlag": "FlagB", "OldB": "flagb"}, "registration.duplicate_target"),
        ("oldb", {"oldflag": "FLAGB", "OLDB": "FlagB"}, "registration.duplicate_target"),
    ],
)
def test_case_variants_cannot_merge_node_fields(
    tmp_path: Path, second: str, mapping: dict[str, str], code: str
) -> None:
    service, args = setup(tmp_path)
    add_boolean_field(service, tmp_path)
    single_leaf(service, args, "OldFlag", "Булево")
    document = service.workspace.get(args["project_id"]).document
    assert isinstance(document, RegistrationRules)
    filters = document.rules()[0].child("ОтборПоСвойствамПланаОбмена")
    assert filters is not None
    leaf = deepcopy(filters.items[0])
    leaf.values["СвойствоПланаОбмена"] = second
    filters.items.append(leaf)
    args["node_properties"] = mapping
    before = snapshot(tmp_path)
    for structure_id in ("target", None):
        failure(service, args | {"structure_id": structure_id}, code)
    assert snapshot(tmp_path) == before


@pytest.mark.parametrize("mixed", [False, True])
@pytest.mark.parametrize("absent_target", ["DateStart", "flagb"])
def test_absent_keys_are_unused_on_already_targeted_plan(
    tmp_path: Path, mixed: bool, absent_target: str
) -> None:
    service, args = setup(tmp_path)
    add_boolean_field(service, tmp_path)
    change_source(service, args, tmp_path / "RegistrationRules.xml", "OldPlan", "TargetPlan")
    document = service.workspace.get(args["project_id"]).document
    assert isinstance(document, RegistrationRules)
    filters = document.rules()[0].child("ОтборПоСвойствамПланаОбмена")
    assert filters is not None
    filters.items[0].values["СвойствоПланаОбмена"] = "OldFlag" if mixed else "FlagB"
    filters.items[1].values["СвойствоПланаОбмена"] = "DateStart"
    args.update(
        own_attributes=None,
        extension=None,
        node_properties=({"OldFlag": "FlagB"} if mixed else {}) | {"OldDate": absent_target},
    )
    preview, _ = both_previews(service, args)
    assert preview["status"] == "ready"
    assert preview["unused_keys"] == ["OldDate"]
    assert preview["counts"]["renamed_leaves"] == int(mixed)
    written = write(service, args, preview)
    assert written["status"] == "written"
    restored = load_registration_rules(
        Path(written["output_path"]) / "registration/RegistrationRules.xml"
    )
    assert restored.exchange_plan == "TargetPlan"
    filters = restored.rules()[0].child("ОтборПоСвойствамПланаОбмена")
    assert filters is not None
    assert [item.get("СвойствоПланаОбмена") for item in filters.items] == ["FlagB", "DateStart"]


@pytest.mark.parametrize("types,blocked", [("Булево, Дата", False), ("Дата, Строка", True)])
def test_composite_object_property_type_matches_by_intersection_in_both_sources(
    tmp_path: Path, types: str, blocked: bool
) -> None:
    service, args = setup(tmp_path)
    add_boolean_field(service, tmp_path)
    single_leaf(service, args, "FlagB", types)
    document = service.workspace.get(args["project_id"]).document
    assert isinstance(document, RegistrationRules)
    filters = document.rules()[0].child("ОтборПоСвойствамПланаОбмена")
    assert filters is not None
    filters.items[0].values.update(ЭтоСтрокаКонстанты=False, СвойствоОбъекта="SourceValue")
    previews = both_previews(service, args)
    assert (previews[0]["status"] == "blocked") == blocked
    for structure_id, preview in zip(("target", None), previews, strict=True):
        if blocked:
            notice = next(n for n in preview["notices"]["items"] if n["blocking"])
            assert notice["check"] == "registration.attribute_type"
            failure(
                service,
                args
                | {
                    "structure_id": structure_id,
                    "mode": "write",
                    "expected_preview_hash": preview["preview_hash"],
                },
                "registration.missing_attribute",
            )
        else:
            written = write(service, args | {"structure_id": structure_id}, preview)
            restored = load_registration_rules(
                Path(written["output_path"]) / "registration/RegistrationRules.xml"
            )
            leaf = restored.rules()[0].child("ОтборПоСвойствамПланаОбмена")
            assert leaf is not None
            assert leaf.items[0].get("ТипСвойстваОбъекта") == types


def deletion_setup(tmp_path: Path) -> tuple[Kd2Service, dict[str, Any]]:
    """Выгрузка с пометкой, обычными регистрами и объектами без действующих ПРО."""
    service, args = setup(tmp_path)
    dump = tmp_path / "dump"
    objects = [
        ("Catalog", "Catalogs", "Marked"),
        ("Document", "Documents", "Invoice"),
        ("InformationRegister", "InformationRegisters", "State"),
        ("Catalog", "Catalogs", "Missing"),
        ("Catalog", "Catalogs", "Disabled"),
    ]
    config = dump / "Configuration.xml"
    config.write_text(
        config.read_text("utf-8").replace(
            "</ChildObjects>",
            "".join(f"<{tag}>{name}</{tag}>" for tag, _, name in objects) + "</ChildObjects>",
        ),
        "utf-8",
    )
    for index, (tag, directory, name) in enumerate(objects):
        folder = dump / directory
        folder.mkdir(exist_ok=True)
        props = "<BasedOn/>" if tag != "InformationRegister" else ""
        props += "<DescriptionLength>50</DescriptionLength>" if tag == "Catalog" else ""
        (folder / f"{name}.xml").write_text(
            '<MetaDataObject xmlns="http://v8.1c.ru/8.3/MDClasses">'
            f'<{tag} uuid="00000000-0000-4000-8000-{index + 1:012d}"><Properties>'
            f"<Name>{name}</Name>{props}</Properties><ChildObjects/></{tag}></MetaDataObject>",
            "utf-8",
        )
    content = dump / "ExchangePlans/TargetPlan/Ext/Content.xml"
    content.parent.mkdir(parents=True)
    content.write_text(
        '<Content xmlns="http://v8.1c.ru/8.3/xcf/extrnprops">'
        + "".join(
            f"<Item><Metadata>{tag}.{name}</Metadata><AutoRecord>Deny</AutoRecord></Item>"
            for tag, _, name in objects
        )
        + "</Content>",
        "utf-8",
    )
    service.structure_load_xml("target", str(dump), force=True)
    specs = [
        {
            "metadata_name": "Справочник.Marked",
            "object_filters": [
                {
                    "operator": "ИЛИ",
                    "items": [
                        {
                            "object_property": "Наименование",
                            "property_type": "Строка",
                            "comparison": "Равно",
                            "constant_value": "Example",
                        }
                    ],
                }
            ],
        },
        {
            "metadata_name": "Справочник.Marked",
            "object_filters": [
                {
                    "object_property": "ПометкаУдаления",
                    "property_type": "Булево",
                    "comparison": "Равно",
                    "constant_value": "true",
                }
            ],
        },
        {"metadata_name": "Документ.Invoice"},
        {"metadata_name": "РегистрСведений.State"},
        {"metadata_name": "Справочник.Disabled"},
    ]
    built = service.registration_build("target", "TargetPlan", None, [specs[0], *specs[2:]])
    document = service.workspace.get(built["project_id"]).document
    assert isinstance(document, RegistrationRules)
    existing = deepcopy(document.rules()[0])
    existing.values["Код"] = "000000006"
    tree = Node.new("object_filter", "ОтборПоСвойствамОбъекта")
    leaf = Node.new("object_filter_item", "ЭлементОтбора")
    leaf.values.update(
        СвойствоОбъекта="ПометкаУдаления",
        ТипСвойстваОбъекта="Булево",
        ВидСравнения="Равно",
        Вид="ЗначениеКонстанты",
        ЗначениеКонстанты="true",
    )
    tree.items.append(leaf)
    existing.children[tree.tag] = tree
    document.section("ПравилаРегистрацииОбъектов").items.insert(1, existing)
    document.rules()[-1].attrs["Отключить"] = True
    document.rules()[0].values["ПередОбработкой"] = "Отказ = Ложь;"
    plan = document.root.child("ПланОбмена")
    assert plan is not None
    plan.attrs["Имя"] = "OldPlan"
    plan.text = "OldPlan"
    source = tmp_path / "DeletionRules.xml"
    source.write_bytes(dump_rules(document))
    service.rules_close(built["project_id"])
    opened = service.rules_open(str(source))
    args.update(
        project_id=opened["project_id"],
        node_properties={},
        own_attributes=None,
        extension=None,
        deletion_mark_filter=True,
    )
    return service, args


@pytest.mark.parametrize("with_extension", [False, True])
def test_deletion_filter_preview_write_validation_and_repeat(
    tmp_path: Path, with_extension: bool
) -> None:
    service, args = deletion_setup(tmp_path)
    if with_extension:
        args.update(own_attributes=ATTRIBUTES, extension=EXTENSION)
    before = snapshot(tmp_path)
    document = service._document(args["project_id"])
    source = sha256(dump_rules(document))
    preview, dumped = both_previews(service, args)
    assert (
        preview["deletion_mark_filter"]
        == dumped["deletion_mark_filter"]
        == {
            "enabled": True,
            "added": 2,
            "skipped": 3,
            "skipped_by_reason": {"existing_filter": 1, "no_deletion_mark": 1, "disabled": 1},
            "mode_rules": 0,
        }
    )
    assert snapshot(tmp_path) == before
    checks = {n["check"] for n in preview["notices"]["items"]}
    assert {
        "registration.deletion_missing_rule",
        "registration.deletion_handler",
        "registration.deletion_partial",
        "registration.deletion_existing",
    } <= checks
    assert (
        sum(n["check"] == "registration.deletion_missing_rule" for n in preview["notices"]["items"])
        == 2
    )
    assert all(n["address"] for n in preview["notices"]["items"])
    plain_args: dict[str, Any] = args | {"deletion_mark_filter": False}
    plain = service.registration_retarget(**plain_args)
    assert plain["preview_hash"] != preview["preview_hash"]
    failure(
        service,
        args | {"mode": "write", "expected_preview_hash": plain["preview_hash"]},
        "registration.stale",
    )
    failure(
        service,
        args | {"mode": "write", "expected_preview_hash": preview["preview_hash"]},
        "registration.ack_required",
    )
    written = write(service, args, preview)
    root = Path(written["output_path"])
    manifest = json.loads((root / "manifest.json").read_bytes())
    assert manifest["deletion_mark_filter"] is True
    instruction = (root / "ИНСТРУКЦИЯ.md").read_text("utf-8")
    for text in (
        "Как передаётся пометка удаления",
        "начальной выгрузке",
        "закрытом периоде",
        "отменяет проведение",
        "снятия пометки",
        "Перенос неполон",
    ):
        assert text in instruction
    assert "${" not in instruction and "<!-- deletion_mark:" not in instruction
    reopened = service.rules_open(str(root / "registration/RegistrationRules.xml"))
    report = service.rules_validate(reopened["project_id"], "target", None, None, None, 0, 200)
    assert report["summary"]["errors"] == 0
    repeated = service.registration_retarget(**(args | {"project_id": reopened["project_id"]}))
    assert repeated["deletion_mark_filter"]["added"] == 0
    repeated_write = write(service, args | {"project_id": reopened["project_id"]}, repeated)
    assert (
        Path(repeated_write["output_path"]) / "registration/RegistrationRules.xml"
    ).read_bytes() == (root / "registration/RegistrationRules.xml").read_bytes()
    assert write(service, args, preview)["status"] == "unchanged"
    assert sha256(dump_rules(document)) == source


@pytest.mark.parametrize("value", [None, 1, "true"])
def test_deletion_filter_flag_is_boolean(tmp_path: Path, value: Any) -> None:
    service, args = setup(tmp_path)
    with pytest.raises(ValueError, match="deletion_mark_filter"):
        service.registration_retarget(**(args | {"deletion_mark_filter": value}))


def review_object(
    service: Kd2Service,
    tmp_path: Path,
    tag: str,
    directory: str,
    name: str,
    *,
    in_plan: bool = False,
) -> None:
    """Объекты Invalid, Const и OtherPlan из синтетической пробы r4_synth."""
    dump = tmp_path / "dump"
    config = dump / "Configuration.xml"
    config.write_text(
        config.read_text("utf-8").replace(
            "</ChildObjects>", f"<{tag}>{name}</{tag}></ChildObjects>"
        ),
        "utf-8",
    )
    folder = dump / directory
    folder.mkdir(exist_ok=True)
    props = "" if tag == "Constant" else "<BasedOn/>"
    (folder / f"{name}.xml").write_text(
        '<MetaDataObject xmlns="http://v8.1c.ru/8.3/MDClasses">'
        f'<{tag} uuid="10000000-0000-4000-8000-000000000001"><Properties>'
        f"<Name>{name}</Name>{props}</Properties><ChildObjects/></{tag}></MetaDataObject>",
        "utf-8",
    )
    if in_plan:
        content = dump / "ExchangePlans/TargetPlan/Ext/Content.xml"
        content.write_text(
            content.read_text("utf-8").replace(
                "</Content>",
                f"<Item><Metadata>{tag}.{name}</Metadata><AutoRecord>Deny</AutoRecord></Item></Content>",
            ),
            "utf-8",
        )
    service.structure_load_xml("target", str(dump), force=True)


@pytest.mark.parametrize("missing_valid", [False, True])
def test_review_invalid_rule_is_not_active(tmp_path: Path, missing_valid: bool) -> None:
    service, args = deletion_setup(tmp_path)
    review_object(service, tmp_path, "Catalog", "Catalogs", "Invalid", in_plan=True)
    original = service._document(args["project_id"])
    assert isinstance(original, RegistrationRules)
    rule = original.rules()[0]
    rule.values["ОбъектМетаданныхИмя"] = "Справочник.Invalid"
    if missing_valid:
        rule.attrs.pop("Валидное", None)
    else:
        rule.attrs["Валидное"] = False
    preview, _ = both_previews(service, args)
    assert preview["deletion_mark_filter"]["skipped_by_reason"]["invalid"] == 1
    assert any(
        n["check"] == "registration.deletion_missing_rule"
        and n["reference"] == "Справочник.Invalid"
        for n in preview["notices"]["items"]
    )
    written = write(service, args, preview)
    doc = load_registration_rules(
        Path(written["output_path"]) / "registration/RegistrationRules.xml"
    )
    tree = doc.rules()[0].child("ОтборПоСвойствамОбъекта")
    assert tree is not None and len(tree.items) == 1
    for rule in original.rules():
        if missing_valid:
            rule.attrs.pop("Валидное", None)
        else:
            rule.attrs["Валидное"] = False
    for enabled in (False, True):
        failure(service, args | {"deletion_mark_filter": enabled}, "registration.empty_rules")


def test_review_constant_has_identical_checks(tmp_path: Path) -> None:
    service, args = deletion_setup(tmp_path)
    review_object(service, tmp_path, "Constant", "Constants", "Const")
    doc = service._document(args["project_id"])
    rule = Node.new("pro", "Правило")
    rule.attrs["Валидное"] = True
    rule.values.update(Код="000000021", ОбъектМетаданныхИмя="Константа.Const")
    doc.section("ПравилаРегистрацииОбъектов").items.append(rule)
    preview, _ = both_previews(service, args)
    assert preview["deletion_mark_filter"]["skipped_by_reason"]["no_deletion_mark"] == 2
    assert not any(
        n["check"] == "registration.deletion_unchecked" and n["reference"] == "Константа.Const"
        for n in preview["notices"]["items"]
    )


def test_review_other_plan_is_not_filtered_at_unload(tmp_path: Path) -> None:
    service, args = deletion_setup(tmp_path)
    review_object(service, tmp_path, "ExchangePlan", "ExchangePlans", "OtherPlan")
    doc = service._document(args["project_id"])
    rule = Node.new("pro", "Правило")
    rule.attrs["Валидное"] = True
    rule.values.update(Код="000000018", ОбъектМетаданныхИмя="ПланОбмена.OtherPlan")
    doc.section("ПравилаРегистрацииОбъектов").items.append(rule)
    preview, _ = both_previews(service, args)
    assert preview["deletion_mark_filter"]["added"] == 2
    assert preview["deletion_mark_filter"]["skipped_by_reason"]["unsupported_kind"] == 1
    written = write(service, args, preview)
    restored = load_registration_rules(
        Path(written["output_path"]) / "registration/RegistrationRules.xml"
    )
    tree = restored.rules()[-1].child("ОтборПоСвойствамОбъекта")
    assert tree is None or not tree.items


def test_review_all_active_rules_protected_no_partial_notice(tmp_path: Path) -> None:
    service, args = deletion_setup(tmp_path)
    doc = service._document(args["project_id"])
    assert isinstance(doc, RegistrationRules)
    tree = doc.rules()[1].child("ОтборПоСвойствамОбъекта")
    assert tree is not None
    tree.items[0].values["ЗначениеКонстанты"] = "false"
    preview, _ = both_previews(service, args)
    assert not any(
        n["check"] == "registration.deletion_partial" for n in preview["notices"]["items"]
    )


def test_review_manual_disabled_parameter_preserves_all_notices(tmp_path: Path) -> None:
    from dataclasses import asdict

    from kd2_rules_mcp.authoring.ed.registration_delivery import read_plan_host

    service, args = setup(tmp_path)
    doc = service._document(args["project_id"])
    assert isinstance(doc, RegistrationRules)
    filters = doc.rules()[0].child("ОтборПоСвойствамПланаОбмена")
    assert filters is not None
    filters.items.pop(0)
    transferred = module.retarget_registration(
        doc,
        plan_name="TargetPlan",
        node_properties={"OldDate": "datestart"},
        target_plan=read_plan_host(tmp_path / "dump", "TargetPlan").card,
    )
    notices = [asdict(n) for n in transferred.notices]
    files = module._manual_files(transferred, "TargetPlan", [], notices)
    root = DATA / "golden/manual-before-deletion"
    assert files == {
        p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*") if p.is_file()
    }


def test_review_published_kit_rewrite_is_unchanged(tmp_path: Path) -> None:
    from kd2_rules_mcp.authoring.ed.registration_delivery import read_plan_host

    service, args = setup(tmp_path)
    args.update(
        own_attributes=[
            {"name": "reg_Flag", "type": "Булево", "synonym": "Не выгружать персональные данные"}
        ],
        node_values=[
            {
                "source": "OldFlag",
                "target": "reg_Flag",
                "instruction": "Перенести значение старого узла",
            }
        ],
    )
    preview = service.registration_retarget(**args)
    golden_root = DATA / "golden/before-deletion"
    golden = {
        p.relative_to(golden_root).as_posix(): p.read_bytes()
        for p in golden_root.rglob("*")
        if p.is_file()
    }
    root = tmp_path / "dump"
    source = tmp_path / "RegistrationRules.xml"
    doc = service._document(args["project_id"])
    host = read_plan_host(root, "TargetPlan")
    card, structure_hash = service._registration_card("target", root, (), "TargetPlan")
    result = module.retarget_registration(
        doc, plan_name="TargetPlan", node_properties=args["node_properties"], target_plan=card
    )
    owner = sha256(
        json_bytes(
            {
                "project_id": args["project_id"],
                "root": str(root),
                "source_file": str(source.resolve()),
                "extensions": [],
                "plan": "TargetPlan",
                "extension": EXTENSION,
            }
        )
    )
    inputs = {
        "document": sha256(dump_rules(doc)),
        "source_file": sha256(source.read_bytes()),
        "host": dict(host.input_hashes),
        "structure": structure_hash,
        "source": {"root": str(root), "extensions": []},
        "decisions": {
            "mapping": args["node_properties"],
            "attributes": args["own_attributes"],
            "hints": args["node_values"],
            "extension": args["extension"],
        },
        "notices": module._notices(result, {"reg_flag"}),
    }
    legacy_hash = sha256(
        json_bytes(
            {
                "schema": module._SCHEMA,
                "inputs": inputs,
                "files": {p: sha256(b) for p, b in sorted(golden.items())},
                "owner": owner,
            }
        )
    )
    receipt = {
        "schema": module._SCHEMA,
        "owner": owner,
        "preview_hash": legacy_hash,
        "file_hashes": {"kit/" + p: sha256(b) for p, b in golden.items()},
    }
    kit = Path(preview["output_path"])
    for name, content in golden.items():
        path = kit / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    (kit.parent / module._OWNER).write_bytes(
        json_bytes(receipt | {"receipt_hash": sha256(json_bytes(receipt))})
    )
    assert preview["preview_hash"] == legacy_hash
    before = {p: p.stat().st_mtime_ns for p in kit.rglob("*") if p.is_file()}
    assert write(service, args, preview)["status"] == "unchanged"
    assert {p: p.stat().st_mtime_ns for p in before} == before
