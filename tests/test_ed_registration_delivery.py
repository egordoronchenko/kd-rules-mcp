"""Комплект регистрации и сценарий копии базы: только синтетические данные."""

from __future__ import annotations

import json
import shutil
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path

import pytest
from lxml import etree

from kd_rules_mcp.authoring.ed.manifest import sha256
from kd_rules_mcp.authoring.ed.registration_delivery import (
    NodeValueHint,
    OwnNodeAttribute,
    RegistrationManifest,
    read_plan_host,
    render_registration_kit,
)
from kd_rules_mcp.authoring.ed.xml_dump import M
from kd_rules_mcp.authoring.registration_retarget import RetargetRemark, retarget_registration
from kd_rules_mcp.errors import (
    RegistrationAttributeClashError,
    RegistrationAttributePrefixError,
    RegistrationDeliveryError,
    RegistrationDeliveryProfileError,
    RegistrationExtensionClashError,
    RegistrationMissingAttributeError,
    RegistrationPlanNotFoundError,
)
from kd_rules_mcp.kd2.rules_io import dump_rules, load_registration_rules
from kd_rules_mcp.structures.queries import ObjectCard, ObjectProperty
from kdbase.ed_registration_kit_check import check_registration_kit, compare_extension, main

DATA = Path(__file__).parent / "data/ed/registration/delivery"
ATTR = OwnNodeAttribute("reg_Flag", "Булево", "Не выгружать персональные данные")
HINTS = (NodeValueHint("OldFlag", "reg_Flag", "Перенести значение старого узла"),)


def result(*, code: bool = False):
    source = load_registration_rules(DATA / "RegistrationRules.xml")
    if code:
        source.rules()[0].values["ПриОбработке"] = "Значение = Узел.OldFlag;"
    card = ObjectCard(
        "TargetPlan",
        "ПланОбменаСсылка.TargetPlan",
        "ПланОбмена",
        (ObjectProperty("DateStart", "Реквизит", False, ("Дата",), ()),),
    )
    return retarget_registration(
        source,
        plan_name="TargetPlan",
        node_properties={"OldFlag": ATTR.name, "OldDate": "DateStart"},
        target_plan=card,
    )


@pytest.mark.parametrize("usage", ["tabular", "dereference", "date", "unload"])
def test_delivery_itself_rejects_incompatible_own_header(usage: str) -> None:
    transferred = result()
    rule = transferred.document.rules()[0]
    filters = rule.child("ОтборПоСвойствамПланаОбмена")
    assert filters is not None
    leaf = filters.items[0]
    if usage == "tabular":
        leaf.values["СвойствоПланаОбмена"] = "[reg_Flag].Field"
    elif usage == "dereference":
        leaf.values["СвойствоПланаОбмена"] = "reg_Flag.Code"
    elif usage == "date":
        leaf.values.update(
            ЭтоСтрокаКонстанты=True, ТипСвойстваОбъекта="Дата", СвойствоОбъекта="2020-01-01"
        )
    else:
        rule.values["РеквизитРежимаВыгрузки"] = "reg_Flag"
    with pytest.raises(RegistrationMissingAttributeError):
        render_registration_kit(
            transferred,
            read_plan_host(DATA / "dump", "TargetPlan"),
            own_attributes=[ATTR],
            extension_name="reg_Registration",
            prefix="reg_",
        )


@pytest.mark.parametrize("usage", ["comparison", "unload", "bare_tabular"])
def test_delivery_itself_checks_existing_reference_and_type(usage: str) -> None:
    transferred = result()
    rule = transferred.document.rules()[0]
    filters = rule.child("ОтборПоСвойствамПланаОбмена")
    assert filters is not None
    if usage == "comparison":
        filters.items[1].values.update(
            ЭтоСтрокаКонстанты=True, ТипСвойстваОбъекта="Булево", СвойствоОбъекта="true"
        )
    elif usage == "unload":
        rule.values["РеквизитРежимаВыгрузки"] = "DateStart"
    else:
        filters.items[1].values["СвойствоПланаОбмена"] = "[Organizations]"
    with pytest.raises(RegistrationMissingAttributeError):
        render_registration_kit(
            transferred,
            read_plan_host(DATA / "dump", "TargetPlan"),
            own_attributes=[ATTR],
            extension_name="reg_Registration",
            prefix="reg_",
        )


def kit(
    *,
    own_attributes: Sequence[OwnNodeAttribute] = (ATTR,),
    node_values: Sequence[NodeValueHint] = HINTS,
    extension_name: str = "reg_Registration",
    prefix: str = "reg_",
    previous_manifest: RegistrationManifest | None = None,
):
    return render_registration_kit(
        result(),
        read_plan_host(DATA / "dump", "TargetPlan"),
        own_attributes=own_attributes,
        node_values=node_values,
        extension_name=extension_name,
        prefix=prefix,
        previous_manifest=previous_manifest,
    )


def write_kit(path: Path) -> Path:
    for name, content in kit().files.items():
        target = path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    return path


def test_repeatable_files_manifest_and_previous() -> None:
    first = kit()
    restored = RegistrationManifest.from_bytes(first.files["manifest.json"])
    assert kit().files == first.files
    assert kit(previous_manifest=restored).files == first.files
    assert first.manifest.input_hashes["source_rules"] == sha256(
        dump_rules(load_registration_rules(DATA / "RegistrationRules.xml"))
    )
    assert first.manifest.input_hashes["source_file"] == sha256(
        (DATA / "RegistrationRules.xml").read_bytes()
    )
    assert first.manifest.input_hashes["ExchangePlans/TargetPlan.xml"] == sha256(
        (DATA / "dump/ExchangePlans/TargetPlan.xml").read_bytes()
    )
    assert set(first.manifest.file_hashes) == first.files.keys() - {"manifest.json"}
    assert all(sha256(first.files[p]) == h for p, h in restored.file_hashes.items())
    manifest = json.loads(first.files["manifest.json"])
    assert manifest["schema_version"] == "ed-registration/1"
    assert manifest["xml_form_verified"] is True
    assert manifest["unverified"] == []
    assert manifest["runtime_verified"] is False
    assert "deletion_mark_filter" not in manifest
    assert manifest["counters"] == {
        "rules": 1,
        "renamed_leaves": 2,
        "untouched_leaves": 0,
        "code_mentions": 0,
        "remarks": 1,
    }
    assert manifest["compatibility_mode"] == "Version8_3_24"
    assert manifest["interface_compatibility_mode"] == "TaxiEnableVersion8_2"
    assert (
        load_registration_rules(first.files["registration/RegistrationRules.xml"]).exchange_plan
        == "TargetPlan"
    )


def test_review_disabled_parameter_preserves_published_kit_bytes() -> None:
    """Эталон снят кодом main (64e2a56), до параметра пометки, на входе Валидное=true."""
    root = DATA / "golden/before-deletion"
    golden = {
        p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*") if p.is_file()
    }
    assert len(golden) == 6
    assert dict(kit().files) == golden
    previous = RegistrationManifest.from_bytes(golden["manifest.json"])
    assert kit(previous_manifest=previous).files == golden


def test_minimal_extension_and_own_boolean() -> None:
    first = kit()
    paths = {p for p in first.files if p.startswith("extension/")}
    assert paths == {
        "extension/Configuration.xml",
        "extension/Languages/Russian.xml",
        "extension/ExchangePlans/TargetPlan.xml",
    }
    root = etree.fromstring(first.files["extension/ExchangePlans/TargetPlan.xml"])
    assert not root.findall(f".//{{{M}}}TabularSection")
    assert not root.findall(f".//{{{M}}}ManagerModule")
    attrs = root.findall(f".//{{{M}}}Attribute")
    assert len(attrs) == 1
    assert attrs[0].findtext(f"{{{M}}}Properties/{{{M}}}Name") == ATTR.name
    assert (
        root.findtext(f"{{{M}}}ExchangePlan/{{{M}}}Properties/{{{M}}}ExtendedConfigurationObject")
        == "b3b52d69-eb65-40fc-9bbc-2a5a356a45c5"
    )
    assert root.xpath("//*[local-name()='Type' and text()='xs:boolean']")


@pytest.mark.parametrize(
    ("attr", "error"),
    [
        (OwnNodeAttribute("DateStart", "Булево", ""), RegistrationAttributeClashError),
        (OwnNodeAttribute("reg_Flag", "Строка", ""), RegistrationDeliveryProfileError),
        (OwnNodeAttribute("OtherFlag", "Булево", ""), RegistrationAttributePrefixError),
    ],
)
def test_attribute_refusals(attr: OwnNodeAttribute, error: type[RegistrationDeliveryError]) -> None:
    prefix = "Date" if attr.name == "DateStart" else "reg_"
    with pytest.raises(error) as caught:
        kit(own_attributes=(attr,), prefix=prefix)
    assert caught.value.code.startswith("registration.")


def test_duplicate_case_insensitive_attribute() -> None:
    with pytest.raises(RegistrationAttributeClashError):
        kit(own_attributes=(ATTR, replace(ATTR, name="REG_FLAG")))


def test_missing_attribute_refused() -> None:
    with pytest.raises(RegistrationMissingAttributeError):
        kit(own_attributes=())
    bad = replace(result(), remarks=(RetargetRemark("ПРО 1", "отбор", "Missing", "Нет реквизита"),))
    with pytest.raises(RegistrationMissingAttributeError):
        render_registration_kit(
            bad,
            read_plan_host(DATA / "dump", "TargetPlan"),
            own_attributes=(ATTR,),
            extension_name="reg_Registration",
            prefix="reg_",
        )


def test_missing_plan_and_compatibility(tmp_path: Path) -> None:
    with pytest.raises(RegistrationPlanNotFoundError):
        read_plan_host(DATA / "dump", "Absent")
    dump = tmp_path / "dump"
    shutil.copytree(DATA / "dump", dump)
    path = dump / "Configuration.xml"
    path.write_text(
        path.read_text("utf-8").replace("<CompatibilityMode>Version8_3_24</CompatibilityMode>", ""),
        "utf-8",
    )
    with pytest.raises(RegistrationDeliveryProfileError):
        read_plan_host(dump, "TargetPlan")


def test_attribute_in_explicit_extension_refused(tmp_path: Path) -> None:
    ext = tmp_path / "extension"
    for name, content in kit().files.items():
        if name.startswith("extension/"):
            path = tmp_path / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
    host = read_plan_host(DATA / "dump", "TargetPlan", extensions=(ext,))
    with pytest.raises(RegistrationExtensionClashError):
        render_registration_kit(
            result(), host, own_attributes=(ATTR,), extension_name="reg_Registration", prefix="reg_"
        )
    # Неявно обнаруженные соседние расширения не участвуют.
    assert kit()


def test_instruction_required_blocks_and_code_mentions() -> None:
    incomplete = render_registration_kit(
        result(code=True),
        read_plan_host(DATA / "dump", "TargetPlan"),
        own_attributes=(ATTR,),
        node_values=HINTS,
        extension_name="reg_Registration",
        prefix="reg_",
    )
    text = incomplete.files["ИНСТРУКЦИЯ.md"].decode("utf-8")
    for part in (
        "Перенос неполон",
        "OldFlag",
        "reg_Flag",
        "резервную копию",
        "Из макета",
        "Из менеджера регистрации",
        "МенеджерТиповой",
        "МакетКонфигурации",
        "ЗагрузитьПоставляемыеПравилаРегистрацииОбъектов",
        "СброситьКэшМеханизмаРегистрацииОбъектов",
        "ОбновитьПовторноИспользуемыеЗначения",
        "обычно запишите",
        "настройки узла «что отправлять» перестают управлять регистрацией",
        "а не записью полей",
    ):
        assert part in text
    complete = replace(result(), remarks=())
    clean = render_registration_kit(
        complete,
        read_plan_host(DATA / "dump", "TargetPlan"),
        own_attributes=(ATTR,),
        extension_name="reg_Registration",
        prefix="reg_",
    )
    assert "Перенос неполон" not in clean.files["ИНСТРУКЦИЯ.md"].decode("utf-8")


def test_modified_previous_manifest_refused() -> None:
    first = kit()
    with pytest.raises(RegistrationDeliveryError):
        kit(previous_manifest=replace(first.manifest, file_hashes={}))


def fake_runner(
    kit_path: Path,
    calls: list[Sequence[str]],
    *,
    failure: str = "",
    result_code: str = "0",
    extra: bool = False,
):
    def run(argv: Sequence[str]) -> int:
        calls.append(argv)
        result_path = Path(argv[argv.index("/DumpResult") + 1])
        result_path.write_text(result_code, "utf-8")
        if failure in argv and failure:
            return 7
        if "/DumpConfigToFiles" in argv:
            target = Path(argv[argv.index("/DumpConfigToFiles") + 1])
            shutil.copytree(kit_path / "extension", target, dirs_exist_ok=True)
            (target / "ConfigDumpInfo.xml").write_text("<ignored/>", "utf-8")
            if extra:
                (target / "extra.bsl").write_text("changed", "utf-8")
        return 0

    return run


@pytest.mark.parametrize(
    ("failure", "result_code", "extra", "expected", "count"),
    [
        ("", "0", False, 0, 5),
        ("/CheckConfig", "0", False, 1, 2),
        ("", "1", False, 1, 1),
        ("", "0", True, 2, 5),
    ],
)
def test_probe_commands_and_cleanup(
    tmp_path: Path, failure: str, result_code: str, extra: bool, expected: int, count: int
) -> None:
    base = tmp_path / "source"
    base.mkdir()
    (base / "1Cv8.1CD").write_bytes(b"synthetic base")
    kit_path = write_kit(tmp_path / "kit")
    calls: list[Sequence[str]] = []
    runner = fake_runner(kit_path, calls, failure=failure, result_code=result_code, extra=extra)
    code, report = check_registration_kit(
        base, kit_path, Path("1cv8.exe"), user="TestUser", run_root=tmp_path / "run", runner=runner
    )
    assert code == expected
    assert len(calls) == count
    assert all(Path(a[a.index("/F") + 1]).is_relative_to(tmp_path / "run") for a in calls)
    assert all("-Extension" in a and "/CheckModules" not in a for a in calls)
    assert not list((tmp_path / "run").iterdir())
    assert (base / "1Cv8.1CD").read_bytes() == b"synthetic base"
    assert "TestUser" not in json.dumps(report)


def test_probe_cli_returns_failure_without_file_base(tmp_path: Path) -> None:
    assert main(["--base", str(tmp_path), "--kit", str(tmp_path), "--platform", "1cv8.exe"]) == 1


def test_probe_detects_changed_uuid(tmp_path: Path) -> None:
    expected = write_kit(tmp_path / "kit") / "extension"
    actual = tmp_path / "dump"
    shutil.copytree(expected, actual)
    path = actual / "ExchangePlans/TargetPlan.xml"
    path.write_bytes(
        path.read_bytes().replace(
            b"b3b52d69-eb65-40fc-9bbc-2a5a356a45c5", b"b3b52d69-eb65-40fc-9bbc-2a5a356a45c6"
        )
    )
    assert compare_extension(expected, actual)["equal"] is False


@pytest.mark.parametrize("missing_result", [True, False])
def test_probe_missing_result_or_launch_failure_stops_and_cleans(
    tmp_path: Path, missing_result: bool
) -> None:
    base = tmp_path / "source"
    base.mkdir()
    (base / "1Cv8.1CD").write_bytes(b"synthetic base")
    kit_path = write_kit(tmp_path / "kit")

    def runner(argv: Sequence[str]) -> int:
        if not missing_result:
            raise PermissionError("synthetic launch restriction")
        return 0

    code, report = check_registration_kit(
        base, kit_path, Path("1cv8.exe"), run_root=tmp_path / "run", runner=runner
    )
    assert code == 1
    assert not list((tmp_path / "run").iterdir())
    if not missing_result:
        assert report["error_type"] == "PermissionError"


def test_probe_tampered_manifest_does_not_copy_base(tmp_path: Path) -> None:
    base = tmp_path / "source"
    base.mkdir()
    (base / "1Cv8.1CD").write_bytes(b"synthetic base")
    kit_path = write_kit(tmp_path / "kit")
    (kit_path / "manifest.json").write_bytes(b"{}")
    code, report = check_registration_kit(
        base, kit_path, Path("1cv8.exe"), run_root=tmp_path / "run"
    )
    assert code == 1
    assert report["error_type"] == "RegistrationDeliveryError"
    assert not (tmp_path / "run").exists()
