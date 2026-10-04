"""Контракт C: снимки, ошибки, preview, подтверждения и атомарное дополнение."""

import copy
import json
import shutil
from dataclasses import asdict
from pathlib import Path

import pytest
from lxml import etree

from kd2_rules_mcp.authoring.ed.manifest import operation_dict
from kd2_rules_mcp.authoring.ed.xml_dump import M, serialize
from kd2_rules_mcp.server import error_payload
from kd2_rules_mcp.service import Kd2Service, Settings
from kd2_rules_mcp.service import ed_authoring as module
from tests.test_ed_authoring_model import DATA, IDENTITY, OPERATION
from tests.test_service_ed_routes import _xml


def make_dump(root: Path) -> None:
    shutil.copytree(DATA / "base/descriptions", root)
    children = etree.SubElement(
        etree.parse(str(root / "Configuration.xml")).getroot()[0], f"{{{M}}}ChildObjects"
    )
    tree = children.getroottree()
    for kind, names in {
        "Language": ["Русский"],
        "CommonModule": ["Менеджер1", "Менеджер2", "Менеджер3"],
        "Catalog": ["Товары"],
        "Document": ["Заказ"],
        "ExchangePlan": ["ПланФормата"],
        "XDTOPackage": ["Формат120", "Формат121", "Общие"],
    }.items():
        for name in names:
            etree.SubElement(children, f"{{{M}}}{kind}").text = name
    (root / "Configuration.xml").write_bytes(serialize(tree.getroot()))
    for kind, name in (("Catalog", "Товары"), ("Document", "Заказ")):
        path = root / (kind + "s") / (name + ".xml")
        doc = etree.parse(str(path)).getroot()
        attrs = etree.SubElement(doc[0], f"{{{M}}}ChildObjects")
        attrs.append(
            etree.fromstring(
                f'<Attribute xmlns="{M}" uuid="00000000-0000-0000-0000-000000000099">'
                '<Properties><Name>Заметка</Name><Type xmlns:v8="http://v8.1c.ru/8.1/data/core" '
                'xmlns:xs="http://www.w3.org/2001/XMLSchema"><v8:Type>xs:string</v8:Type>'
                "<v8:StringQualifiers><v8:Length>150</v8:Length><v8:AllowedLength>Variable</v8:AllowedLength>"
                "</v8:StringQualifiers></Type></Properties></Attribute>".encode()
            )
        )
        path.write_bytes(serialize(doc))
    for version in (1, 2, 3):
        path = root / f"CommonModules/Менеджер{version}/Ext/Module.bsl"
        path.parent.mkdir(parents=True)
        shutil.copyfile(DATA / f"base/manager-v{version}.bsl", path)
    plan = root / "ExchangePlans/ПланФормата/Ext/ManagerModule.bsl"
    plan.parent.mkdir(parents=True)
    (root / "ExchangePlans/ПланФормата.xml").write_text(
        _xml("ПланФормата", "").replace("XDTOPackage", "ExchangePlan"), encoding="utf-8"
    )
    plan.write_text(
        "Процедура ПриПолученииНастроек(Настройки) Экспорт\n"
        "Настройки.ЭтоПланОбменаXDTO = Истина;\n"
        'Настройки.ФорматОбмена = "urn:fiction";\n'
        "Версии = Новый Соответствие;\n"
        'Версии.Вставить("1.20", Менеджер2);\n'
        'Версии.Вставить("1.21", Менеджер2);\n'
        "Настройки.ВерсииФорматаОбмена = Версии;\nКонецПроцедуры\n",
        encoding="utf-8",
    )
    for name, version in (("Формат120", "1.20"), ("Формат121", "1.21"), ("Общие", "common")):
        namespace = "urn:fiction/" + version if version != "common" else "urn:fiction:common"
        path = root / f"XDTOPackages/{name}/Ext/Package.bin"
        path.parent.mkdir(parents=True)
        source = DATA / ("base/common.bin" if version == "common" else f"base/format-{version}.bin")
        path.write_text(
            source.read_text("utf-8").replace("urn:fiction:" + version, namespace), encoding="utf-8"
        )
        (root / f"XDTOPackages/{name}.xml").write_text(_xml(name, namespace), encoding="utf-8")


@pytest.fixture
def setup(tmp_path):
    root = tmp_path / "fiction"
    make_dump(root)
    catalog = tmp_path / "projects.yaml"
    catalog.write_text(
        "projects:\n  Пример:\n    name: Вымышленная\n    configurations:\n"
        "      Main:\n        dump: .\n",
        encoding="utf-8",
    )
    service = Kd2Service(
        Settings(
            cache_dir=tmp_path / "cache",
            workspace=tmp_path / "workspace",
            projects_file=catalog,
            project_dirs={"Пример": root},
        )
    )
    ed = service.ed_open(str(root / "CommonModules/Менеджер2/Ext/Module.bsl"))
    schema = service.ed_schema_open(
        "1.20", project="Пример", configuration="Main", package="Формат120"
    )
    structure = service.structure_load_project("Пример", "Main", structure_id="fiction-main")
    raw = operation_dict(OPERATION)
    raw.pop("operation_id")
    raw["target"].pop("project")
    raw["target"].pop("configuration")
    raw["target"].update(
        project_id=ed["project_id"],
        schema_id=schema["schema_id"],
        structure_id=structure["structure_id"],
    )
    arguments = dict(
        project="Пример",
        configuration="Main",
        extension=asdict(IDENTITY),
        operations=[raw],
        version_scope="manager",
    )
    return service, arguments, root


def preview(setup, **changes):
    service, arguments, _ = setup
    return service.ed_authoring_build(**(arguments | changes))


def write(setup, viewed=None, **changes):
    service, arguments, _ = setup
    viewed = viewed or preview(setup, **changes)
    return service.ed_authoring_build(
        **(
            arguments
            | changes
            | {
                "mode": "write",
                "expected_preview_hash": viewed["build_hash"],
                "acknowledged_notices": viewed["required_acknowledgements"],
            }
        )
    )


def failure(call, code):
    with pytest.raises(Exception) as caught:
        call()
    result = error_payload(caught.value)
    assert result["code"] == code, result
    return result


def content(destination):
    return {
        p.relative_to(destination).as_posix(): p.read_bytes()
        for p in destination.rglob("*")
        if p.is_file()
    }


def test_preview_write_repeat_and_both_deliveries(setup):
    service, _, _ = setup
    before = content(service.workspace.root)
    viewed = preview(setup)
    assert viewed["status"] == "ready" and not viewed["written"]
    scope = viewed["scopes"][0]
    assert scope["version_scope"] == "manager" and scope["directions"] == ["send"]
    assert Path(scope["manager_path"]).as_posix().endswith("Менеджер2/Ext/Module.bsl")
    assert scope["plans"][0]["plan"] == "ПланФормата"
    assert {e["key"] for e in scope["plans"][0]["entries"]} == {"1.20", "1.21"}
    assert scope["without_node"] == {"status": "partial", "entries": []}
    assert content(service.workspace.root) == before
    destination = Path(viewed["output_dir"])
    assert not destination.exists()
    assert write(setup, viewed)["status"] == "written"
    first = content(destination)
    assert write(setup)["status"] == "unchanged"
    assert content(destination) == first
    assert write(setup, delivery="manual")["status"] == "written"
    assert not any(p.startswith("extension/") for p in content(destination))
    assert write(setup, delivery="manual")["status"] == "unchanged"


@pytest.mark.parametrize(
    "changes,code",
    [
        ({"operations": []}, "invalid_argument"),
        ({"limit": 201}, "invalid_argument"),
        ({"offset": -1}, "invalid_argument"),
        ({"limit": True}, "invalid_argument"),
        ({"delivery": "extension_with_load"}, "invalid_argument"),
        ({"mode": "write"}, "ed_authoring_stale"),
        ({"mode": "write", "expected_preview_hash": "foreign"}, "ed_authoring_stale"),
        ({"version_scope": None}, "ed_authoring_precondition"),
        ({"output_dir": "../outside"}, "ed_authoring_path"),
    ],
)
def test_argument_and_write_errors_do_not_create_files(setup, changes, code):
    service, _, _ = setup
    failure(lambda: preview(setup, **changes), code)
    assert not (service.workspace.root / "ed-authoring").exists()


def test_acknowledgements_are_specific_to_operation(setup):
    viewed = preview(setup)
    assert viewed["required_acknowledgements"]
    details = failure(
        lambda: preview(
            setup,
            mode="write",
            expected_preview_hash=viewed["build_hash"],
            acknowledged_notices=["ed.author.other_version_incompatible:foreign"],
        ),
        "ed_authoring_ack_required",
    )
    assert details["required_acknowledgements"] == sorted(viewed["required_acknowledgements"])
    assert not Path(viewed["output_dir"]).exists()


@pytest.mark.parametrize(
    "key,code",
    [
        ("project_id", "project_not_found"),
        ("schema_id", "ed_schema_not_found"),
        ("structure_id", "structure_not_found"),
    ],
)
def test_unknown_snapshot_codes(setup, key, code):
    _, args, _ = setup
    operations = copy.deepcopy(args["operations"])
    operations[0]["target"][key] = "missing"
    failure(lambda: preview(setup, operations=operations), code)


def test_core_failure_retains_source_and_identifier(setup):
    _, args, _ = setup
    operations = copy.deepcopy(args["operations"])
    operations[0]["format_property"] = "НетТакого"
    details = failure(lambda: preview(setup, operations=operations), "ed_authoring_precondition")
    assert details["failures"][0]["id"] == "ed.author.schema_property_missing"
    assert details["failures"][0]["address"] == "ПКО/Товар"
    assert details["failures"][0]["source"]["line"] > 0


def test_augmentation_preserves_old_operation(setup):
    _, args, _ = setup
    old = write(setup)
    destination = Path(old["output_dir"])
    first = json.loads((destination / "manifest.json").read_bytes())
    operations = copy.deepcopy(args["operations"])
    operations[0]["target"]["pko_address"] = "ПКО/Заказ"
    updated = write(setup, operations=operations)
    assert updated["change_counts"]["pks_added"] == 2
    second = json.loads((destination / "manifest.json").read_bytes())
    for key, value in first["identity_map"]["objects"].items():
        assert second["identity_map"]["objects"][key] == value
    assert write(setup, operations=args["operations"])["status"] == "unchanged"


@pytest.mark.parametrize(
    "tamper,code",
    [
        ("edit", "ed_authoring_precondition"),
        ("foreign", "ed_authoring_path"),
        ("manifest", "ed_authoring_precondition"),
    ],
)
def test_owned_content_and_foreign_files(setup, tamper, code):
    result = write(setup)
    destination = Path(result["output_dir"])
    if tamper == "edit":
        (destination / "instruction.md").write_bytes(b"hand edit")
    elif tamper == "foreign":
        (destination / "foreign.txt").write_bytes(b"foreign")
    else:
        (destination / "manifest.json").write_bytes(b"{")
    expected = content(destination)
    failure(lambda: preview(setup), code)
    assert content(destination) == expected


def test_stale_input_after_preview(setup):
    _, _, root = setup
    viewed = preview(setup)
    path = root / "CommonModules/Менеджер2/Ext/Module.bsl"
    path.write_text(path.read_text("utf-8") + "\n// changed\n", encoding="utf-8")
    failure(lambda: write(setup, viewed), "ed_authoring_stale")
    assert not Path(viewed["output_dir"]).exists()


def test_atomic_rename_failure_restores_previous(setup, monkeypatch):
    viewed = write(setup)
    destination = Path(viewed["output_dir"])
    before = content(destination)
    rename = module.os.replace

    def fail_install(source, target):
        if Path(source).name.startswith(".staging-") and not str(source).endswith("-previous"):
            raise OSError("simulated rename failure")
        return rename(source, target)

    monkeypatch.setattr(module.os, "replace", fail_install)
    failure(lambda: write(setup, delivery="manual"), "ed_authoring_io")
    assert content(destination) == before
    assert not list(destination.parent.glob(".staging-*"))


def test_manifest_race_during_rename_restores_changed_previous(setup, monkeypatch):
    viewed = write(setup)
    destination = Path(viewed["output_dir"])
    rename = module.os.replace

    def change_previous(source, target):
        if Path(source) == destination:
            (destination / "instruction.md").write_bytes(b"concurrent change")
        return rename(source, target)

    monkeypatch.setattr(module.os, "replace", change_previous)
    failure(lambda: write(setup, delivery="manual"), "ed_authoring_stale")
    assert (destination / "instruction.md").read_bytes() == b"concurrent change"
    assert not list(destination.parent.glob(".staging-*"))


def test_resource_limits(setup, monkeypatch):
    _, args, _ = setup
    failure(
        lambda: preview(setup, operations=args["operations"] * 101), "ed_authoring_resource_limit"
    )
    monkeypatch.setattr(module, "MAX_BYTES", 1)
    failure(lambda: preview(setup), "ed_authoring_resource_limit")


def test_candidates_both_kinds_and_build_pages(setup):
    service, args, _ = setup
    target = args["operations"][0]["target"] | {"project": "Пример", "configuration": "Main"}
    found = service.ed_authoring_candidates(
        target, "format", configuration_attribute="Заметка", limit=1
    )
    assert found["has_more"] and found["items"][0]["auto"] is False
    full = service.ed_authoring_candidates(
        target, "format", configuration_attribute="Заметка", limit=200
    )
    assert (
        found["items"]
        + service.ed_authoring_candidates(
            target, "format", configuration_attribute="Заметка", offset=1, limit=200
        )["items"]
        == full["items"]
    )
    config = service.ed_authoring_candidates(target, "configuration", format_property="Комментарий")
    assert any(i["name"] == "Заметка" and i["compatible"] for i in config["items"])
    viewed = preview(setup, limit=1)
    rest = preview(setup, offset=1, limit=200)
    whole = preview(setup, limit=200)
    assert viewed["items"] + rest["items"] == whole["items"]
    assert viewed["build_hash"] == rest["build_hash"] == whole["build_hash"]
    details = failure(
        lambda: service.ed_authoring_candidates(target | {"pko_address": "ПКО/Нет"}, "format"),
        "ed_authoring_precondition",
    )
    assert details["failures"][0]["id"] == "ed.author.pko_missing"


def test_input_cache_does_not_reload_schema_or_structure(setup, monkeypatch):
    service, _, _ = setup
    viewed = preview(setup)

    def unexpected(*args, **kwargs):
        raise AssertionError("inputs reread")

    monkeypatch.setattr(module, "_load_for_uri", unexpected)
    monkeypatch.setattr(service, "_ed_structure_snapshot", unexpected)
    assert preview(setup)["build_hash"] == viewed["build_hash"]
    assert write(setup, viewed)["status"] == "written"


def test_manifest_race_preserves_concurrent_bundle(setup, monkeypatch):
    service, _, _ = setup
    original = service._write_artifact

    def concurrent_write(destination, files, previous, entries):
        destination.mkdir(parents=True)
        for name, data in files.items():
            path = destination / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        return original(destination, files, previous, entries)

    monkeypatch.setattr(service, "_write_artifact", concurrent_write)
    viewed = preview(setup)
    failure(lambda: write(setup, viewed), "ed_authoring_stale")
    assert content(Path(viewed["output_dir"]))


def test_symlinks_and_junction_are_refused(setup, monkeypatch):
    viewed = preview(setup)
    target = Path(viewed["output_dir"])
    for method in ("is_symlink", "is_junction"):
        with monkeypatch.context() as context:
            original = getattr(Path, method)
            context.setattr(
                Path, method, lambda self, original=original: self == target or original(self)
            )
            failure(lambda: preview(setup), "ed_authoring_path")
    assert not target.exists()


def test_schema_snapshot_version_and_metadata_staleness(setup):
    _, args, root = setup
    operations = copy.deepcopy(args["operations"])
    operations[0]["target"]["format_version"] = "1.21"
    details = failure(lambda: preview(setup, operations=operations), "ed_authoring_precondition")
    assert details["failures"][0]["id"] == "ed.author.snapshot_mismatch"
    viewed = preview(setup)
    path = root / "Catalogs/Товары.xml"
    path.write_bytes(path.read_bytes() + b"\n")
    failure(lambda: write(setup, viewed), "ed_authoring_stale")


def test_selected_schema_uri_must_match_route(setup):
    service, args, _ = setup
    opened = service.ed_schema_open(
        "1.20", project="Пример", configuration="Main", package="Формат121"
    )
    operations = copy.deepcopy(args["operations"])
    operations[0]["target"]["schema_id"] = opened["schema_id"]
    calls = (
        lambda: preview(setup, operations=operations),
        lambda: service.ed_authoring_candidates(
            operations[0]["target"] | {"project": "Пример", "configuration": "Main"}, "format"
        ),
    )
    for call in calls:
        details = failure(call, "ed_authoring_precondition")
        assert details["failures"][0]["id"] == "ed.author.snapshot_mismatch"
        assert details["failures"][0]["message"] == (
            "schema_id не соответствует выбранной версии и URI маршрута"
        )
    assert not (service.workspace.root / "ed-authoring").exists()


@pytest.mark.parametrize("uuid", [None, "invalid"])
def test_configuration_uuid_failure_is_core_refusal(setup, uuid):
    service, _, root = setup
    path = root / "Configuration.xml"
    xml = etree.parse(str(path)).getroot()
    if uuid is None:
        del xml[0].attrib["uuid"]
    else:
        xml[0].attrib["uuid"] = uuid
    path.write_bytes(serialize(xml))
    service.structure_load_project("Пример", "Main", structure_id="fiction-main", force=True)
    result = failure(lambda: preview(setup), "ed_authoring_precondition")
    assert result["failures"][0]["id"] == "ed.author.metadata_profile_unsupported"
    assert result["failures"][0]["message"] == "Не задан UUID объекта"


def test_preview_hash_binds_delivery(setup):
    viewed = preview(setup)
    failure(
        lambda: preview(
            setup,
            delivery="manual",
            mode="write",
            expected_preview_hash=viewed["build_hash"],
            acknowledged_notices=viewed["required_acknowledgements"],
        ),
        "ed_authoring_stale",
    )


def test_bounded_input_cache(setup, monkeypatch):
    service, args, root = setup
    monkeypatch.setattr(module, "MAX_INPUTS", 1)
    preview(setup)
    previous = tuple(service._authoring_inputs)
    # Тот же модуль после закрытия/открытия имеет тот же id; отдельный снимок схемы
    # создаём другим путём при том же содержимом.
    schema = service.ed_schema_open(
        "1.20",
        path=str(root / "XDTOPackages/Формат120/Ext/Package.bin"),
        imports={"urn:fiction:common": str(root / "XDTOPackages/Общие/Ext/Package.bin")},
    )
    operations = copy.deepcopy(args["operations"])
    operations[0]["target"]["schema_id"] = schema["schema_id"]
    preview(setup, operations=operations)
    assert len(service._authoring_inputs) == 1
    assert tuple(service._authoring_inputs) != previous


def test_two_managers_augmentation_after_service_restart(setup):
    service, args, root = setup
    configuration = etree.parse(str(root / "Configuration.xml")).getroot()
    children = configuration[0].find(f"{{{M}}}ChildObjects")
    assert children is not None
    etree.SubElement(children, f"{{{M}}}ExchangePlan").text = "ВторойПлан"
    (root / "Configuration.xml").write_bytes(serialize(configuration))
    path = root / "ExchangePlans/ВторойПлан/Ext/ManagerModule.bsl"
    path.parent.mkdir(parents=True)
    path.write_text(
        (root / "ExchangePlans/ПланФормата/Ext/ManagerModule.bsl")
        .read_text("utf-8")
        .replace("Менеджер2", "Менеджер1"),
        encoding="utf-8",
    )
    (root / "ExchangePlans/ВторойПлан.xml").write_text(
        _xml("ВторойПлан", "").replace("XDTOPackage", "ExchangePlan"), encoding="utf-8"
    )
    service.structure_load_project("Пример", "Main", structure_id="fiction-main", force=True)
    old = write(setup)
    # Новый процесс восстанавливает прежние решения из manifest, без draft_id.
    restarted = Kd2Service(service.settings)
    ed = restarted.ed_open(str(root / "CommonModules/Менеджер1/Ext/Module.bsl"))
    schema = restarted.ed_schema_open(
        "1.20", project="Пример", configuration="Main", package="Формат120"
    )
    operations = copy.deepcopy(args["operations"])
    operations[0]["target"].update(
        plan="ВторойПлан",
        pko_address="ПКО/Заказ",
        project_id=ed["project_id"],
        schema_id=schema["schema_id"],
    )
    new_setup = restarted, args, root
    result = write(new_setup, operations=operations)
    assert result["output_dir"] == old["output_dir"]
    assert len(result["scopes"]) == 2 and result["change_counts"]["pks_added"] == 2
    files = content(Path(result["output_dir"]))
    assert len([p for p in files if p.startswith("modules/")]) == 2
    assert write(new_setup, operations=operations)["status"] == "unchanged"


def test_other_schema_read_failure_becomes_notice(setup, monkeypatch):
    from kd2_rules_mcp.validation.ed_routes import SchemaUnavailable

    monkeypatch.setattr(
        module,
        "_load_for_uri",
        lambda *args: SchemaUnavailable(
            "unreadable", "сломанная схема", "XDTOPackages/Формат121.xml"
        ),
    )
    viewed = preview(setup, limit=200)
    assert viewed["status"] == "ready"
    notice = next(i for i in viewed["items"] if i.get("id") == "ed.author.other_version_unverified")
    assert notice["version_keys"] == ("1.21",) and "сломанная схема" in notice["message"]


def test_only_explicit_foreign_extensions_are_scanned(setup):
    service, args, root = setup
    foreign = root.parent / "foreign"
    path = foreign / "CommonModules/Менеджер2/Ext/Module.bsl"
    path.parent.mkdir(parents=True)
    path.write_text(
        '&Вместо("ДобавитьПКО_Товар")\nПроцедура Чужая()\nКонецПроцедуры\n', encoding="utf-8"
    )
    (foreign / "Configuration.xml").write_bytes((root / "Configuration.xml").read_bytes())
    extension_xml = etree.parse(str(foreign / "Configuration.xml")).getroot()
    children = extension_xml[0].find(f"{{{M}}}ChildObjects")
    assert children is not None
    children.clear()
    (foreign / "Configuration.xml").write_bytes(serialize(extension_xml))
    assert preview(setup)["status"] == "ready"
    catalog = service.settings.projects_file
    catalog.write_text(
        catalog.read_text("utf-8") + "        extensions: [../foreign]\n", encoding="utf-8"
    )
    failure(lambda: preview(setup), "ed_authoring_stale")
    service.structure_load_project("Пример", "Main", structure_id="fiction-main", force=True)
    restarted = Kd2Service(service.settings)
    ed = restarted.ed_open(str(root / "CommonModules/Менеджер2/Ext/Module.bsl"))
    schema = restarted.ed_schema_open(
        "1.20", project="Пример", configuration="Main", package="Формат120"
    )
    operations = copy.deepcopy(args["operations"])
    operations[0]["target"].update(project_id=ed["project_id"], schema_id=schema["schema_id"])
    details = failure(
        lambda: preview((restarted, args, root), operations=operations), "ed_authoring_precondition"
    )
    assert any(f["id"] == "ed.author.extension_conflict" for f in details["failures"])


def test_new_foreign_extension_module_makes_preview_stale(setup):
    service, args, root = setup
    foreign = root.parent / "foreign"
    foreign.mkdir()
    xml = etree.parse(str(root / "Configuration.xml")).getroot()
    children = xml[0].find(f"{{{M}}}ChildObjects")
    assert children is not None
    children.clear()
    (foreign / "Configuration.xml").write_bytes(serialize(xml))
    catalog = service.settings.projects_file
    catalog.write_text(
        catalog.read_text("utf-8") + "        extensions: [../foreign]\n", encoding="utf-8"
    )
    service.structure_load_project("Пример", "Main", structure_id="fiction-main", force=True)
    viewed = preview(setup)
    path = foreign / "CommonModules/Менеджер2/Ext/Module.bsl"
    path.parent.mkdir(parents=True)
    path.write_text(
        '&После("ДобавитьПКО_Товар")\nПроцедура Чужая()\nКонецПроцедуры\n', encoding="utf-8"
    )
    failure(lambda: write((service, args, root), viewed), "ed_authoring_stale")
    assert not Path(viewed["output_dir"]).exists()
