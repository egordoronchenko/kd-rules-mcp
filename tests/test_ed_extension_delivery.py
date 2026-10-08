"""Доставка в существующую выгрузку: слияние, владение и атомарный откат."""

import json
import shutil
import sqlite3
from pathlib import Path

import pytest
import yaml
from lxml import etree

from kd_rules_mcp.authoring.ed.extension_merge import merge_extension, xml
from kd_rules_mcp.authoring.ed.manager_render import ManagerRoute, render_manager_route
from kd_rules_mcp.authoring.ed.xml_dump import XR, M
from kd_rules_mcp.errors import EdAuthoringStaleError, RegistrationToolError
from kd_rules_mcp.projects import LocalSettings, load_catalog
from kd_rules_mcp.service import Kd2Service, Settings
from kd_rules_mcp.service import extension_delivery as delivery_module
from kd_rules_mcp.service.extension_delivery import atomic_files, prepare_user_delivery
from scripts.setup_local import compose_override
from tests.test_service_ed_writer import (
    apply_packet,
    build,
    files_at,
    manager_operations,
    reopen_writer_schema,
)
from tests.test_service_ed_writer import writer_setup as writer_setup
from tests.test_service_registration_retarget import setup as registration_setup

DATA = Path(__file__).parent / "data/xmldump/user-extension"
PLAN = "ПланФормата"
CONTENT = f"ExchangePlans/{PLAN}/Ext/Content.xml"
ROUTE = f"ExchangePlans/{PLAN}/Ext/ManagerModule.bsl"


def object_xml(kind, name, uuid, extra="", children=""):
    return (
        f'<MetaDataObject xmlns="{M}" xmlns:xr="{XR}" '
        'xmlns:v8="http://v8.1c.ru/8.1/data/core" '
        'xmlns:cfg="http://v8.1c.ru/8.1/data/enterprise/current-config" version="2.20">'
        f'<{kind} uuid="{uuid}"><InternalInfo>{extra}</InternalInfo><Properties>'
        f"<ObjectBelonging>Adopted</ObjectBelonging><Name>{name}</Name>"
        f"<ExtendedConfigurationObject>{uuid}</ExtendedConfigurationObject>"
        f"</Properties><ChildObjects>{children}</ChildObjects></{kind}></MetaDataObject>\n"
    ).encode()


@pytest.fixture
def merge_setup(tmp_path):
    extension = tmp_path / "user"
    shutil.copytree(DATA, extension)
    base = tmp_path / "base"
    base.mkdir()
    generated = {p: b for p, b in files_at(DATA).items() if p.endswith(".xml")}
    for name, content in list(generated.items()):
        root = xml(content)
        if name != "Configuration.xml":
            root[0].set(
                "uuid",
                root.findtext(
                    f"*/{{{M}}}Properties/{{{M}}}ExtendedConfigurationObject",
                    "99999999-9999-4999-8999-999999999999",
                ),
            )
            belonging = root.find(f"*/{{{M}}}Properties/{{{M}}}ObjectBelonging")
            if belonging is not None:
                belonging.text = "Native"
        path = base / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(etree.tostring(root))
    generated.pop("CommonModules/UserModule.xml")
    generated["CommonModules/OwnRules.xml"] = object_xml(
        "CommonModule", "OwnRules", "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    )
    generated["CommonModules/OwnRules/Ext/Module.bsl"] = (
        "Процедура Правила() Экспорт\nКонецПроцедуры\n".encode()
    )
    cfg = xml(generated["Configuration.xml"])
    children = cfg.find(f"*/{{{M}}}ChildObjects")
    assert children is not None
    etree.SubElement(children, f"{{{M}}}CommonModule").text = "OwnRules"
    generated["Configuration.xml"] = etree.tostring(cfg)
    plan = xml(generated[f"ExchangePlans/{PLAN}.xml"])
    info = plan.find(f"*/{{{M}}}InternalInfo")
    assert info is not None
    state = etree.SubElement(info, f"{{{XR}}}PropertyState")
    etree.SubElement(state, f"{{{XR}}}Property").text = "ManagerModule"
    etree.SubElement(state, f"{{{XR}}}State").text = "Extended"
    generated[f"ExchangePlans/{PLAN}.xml"] = etree.tostring(plan)
    generated[ROUTE] = render_manager_route(
        ManagerRoute(PLAN, "1.20"), module_name="OwnRules", prefix="own_"
    ).encode()
    generated[CONTENT] = generated[CONTENT].replace(b"Catalog.Foreign", b"Catalog.New")
    sub = xml(generated["EventSubscriptions/RegisterObjects.xml"])
    source = sub.find(f"*/{{{M}}}Properties/{{{M}}}Source")
    assert source is not None
    etree.SubElement(source, "{http://v8.1c.ru/8.1/data/core}Type").text = "cfg:CatalogObject.New"
    etree.SubElement(source, "{http://v8.1c.ru/8.1/data/core}Type").text = "cfg:CatalogObject.New"
    generated["EventSubscriptions/RegisterObjects.xml"] = etree.tostring(sub)
    return extension, base, generated


def test_merge_preserves_foreign_entries_and_repeats(merge_setup):
    extension, base, generated = merge_setup
    original = files_at(extension)
    merged = merge_extension(extension, base, generated)
    assert files_at(extension) == original
    assert merged.summaries["Configuration.xml"] == ["CommonModule: OwnRules"]
    atomic_files({extension / p: b for p, b in merged.files.items()}, lambda: None)
    written = files_at(extension)
    assert (
        written["CommonModules/UserModule/Ext/Module.bsl"]
        == original["CommonModules/UserModule/Ext/Module.bsl"]
    )
    assert written["Catalogs/Existing.xml"] == original["Catalogs/Existing.xml"]
    assert b"Catalog.Foreign" in written[CONTENT] and b"Catalog.New" in written[CONTENT]
    source = xml(written["EventSubscriptions/RegisterObjects.xml"]).find(
        f"*/{{{M}}}Properties/{{{M}}}Source"
    )
    assert source is not None
    assert [n.text for n in source] == [
        "cfg:CatalogObject.Foreign",
        "cfg:CatalogObject.Existing",
        "cfg:CatalogObject.New",
    ]
    repeated = merge_extension(extension, base, generated, merged.files)
    assert all(written[p] == b for p, b in repeated.files.items())
    assert (
        xml(written["Configuration.xml"])
        .findall(f"*/{{{M}}}ChildObjects/{{{M}}}CommonModule")[-1]
        .text
        == "OwnRules"
    )


def test_update_owned_route_preserves_shared_code(merge_setup):
    extension, base, generated = merge_setup
    foreign = "Процедура Чужая(Настройки)\nКонецПроцедуры\n".encode()
    (extension / ROUTE).write_bytes(foreign)
    first = merge_extension(extension, base, generated)
    atomic_files({extension / p: b for p, b in first.files.items()}, lambda: None)
    generated[ROUTE] += "// Обновлённый маршрут\n".encode()
    updated = merge_extension(extension, base, generated, first.files)
    assert updated.files[ROUTE].startswith(foreign)
    assert updated.files[ROUTE].count(b"kd-rules-mcp:route:") == 1
    assert "Обновлённый маршрут".encode() in updated.files[ROUTE]


@pytest.mark.parametrize("bom,crlf", [(False, False), (True, False), (False, True), (True, True)])
def test_style_is_copied(merge_setup, bom, crlf):
    extension, base, generated = merge_setup
    for path in extension.rglob("*"):
        if path.is_file():
            raw = path.read_bytes().replace(b"\r\n", b"\n")
            path.write_bytes(
                (b"\xef\xbb\xbf" if bom else b"") + (raw.replace(b"\n", b"\r\n") if crlf else raw)
            )
    merged = merge_extension(extension, base, generated)
    for content in merged.files.values():
        assert content.startswith(b"\xef\xbb\xbf") == bom
        assert (b"\r\n" in content) == crlf
        if crlf:
            assert b"\n" not in content.replace(b"\r\n", b"")


@pytest.mark.parametrize("conflict", ["module", "route", "missing", "compatibility"])
def test_refusals_leave_dump_untouched(merge_setup, conflict):
    extension, base, generated = merge_setup
    code = "delivery_conflict"
    if conflict == "module":
        (extension / "CommonModules/OwnRules.xml").write_bytes(
            generated["CommonModules/OwnRules.xml"]
        )
    elif conflict == "route":
        (extension / ROUTE).write_bytes(
            (
                "Процедура Чужая(Настройки)\n"
                'Настройки.ВерсииФорматаОбмена.Вставить("1.20", ЧужойМодуль);\n'
                "КонецПроцедуры"
            ).encode()
        )
    elif conflict == "missing":
        (base / "Catalogs/Existing.xml").unlink()
        code = "delivery_missing_object"
    else:
        path = extension / "Configuration.xml"
        path.write_bytes(path.read_bytes().replace(b"Version8_3_24", b"Version8_3_12"))
        code = "delivery_compatibility"
    before = files_at(extension)
    with pytest.raises(RegistrationToolError) as caught:
        merge_extension(extension, base, generated)
    assert caught.value.code == code
    assert files_at(extension) == before


@pytest.mark.parametrize("step", ["stage", "replace"])
def test_atomic_failure_rolls_back(tmp_path, monkeypatch, step):
    paths = [tmp_path / "one", tmp_path / "two"]
    for p in paths:
        p.write_bytes(b"old")
    original = delivery_module._stage if step == "stage" else delivery_module.os.replace
    calls = 0

    def fail(*args):
        nonlocal calls
        calls += 1
        if calls == (3 if step == "stage" else 2):
            raise OSError("Тестовая ошибка второго файла")
        return original(*args)

    monkeypatch.setattr(
        delivery_module if step == "stage" else delivery_module.os,
        "_stage" if step == "stage" else "replace",
        fail,
    )
    with pytest.raises(OSError):
        atomic_files(dict.fromkeys(paths, b"new"), lambda: None)
    assert [p.read_bytes() for p in paths] == [b"old", b"old"]
    assert set(tmp_path.iterdir()) == set(paths)


def configure(service, base, extension):
    catalog = base.parent / "projects.yaml"
    catalog.write_text(
        yaml.safe_dump(
            {
                "projects": {
                    "sample": {
                        "configurations": {
                            "full": {
                                "dump": base.name,
                                "extensions": [extension.name],
                                "writable_extensions": [extension.name],
                            }
                        }
                    }
                }
            }
        ),
        "utf-8",
    )
    service.settings.projects_file = catalog
    service.settings.project_dirs = {"sample": base.parent}


@pytest.mark.parametrize("included_extensions", [False, True])
def test_manager_service_user_delivery(writer_setup, included_extensions):
    service, args, base = writer_setup
    extension = base.parent / "user"
    shutil.copytree(DATA, extension)
    config = extension / "Configuration.xml"
    config.write_bytes(config.read_bytes().replace(b"Version8_3_24", b"DontUse"))
    (extension / "ExchangePlans" / (PLAN + ".xml")).write_bytes(
        (extension / "ExchangePlans" / (PLAN + ".xml"))
        .read_bytes()
        .replace(
            b"33333333-3333-4333-8333-333333333333",
            str(xml((base / f"ExchangePlans/{PLAN}.xml").read_bytes())[0].get("uuid")).encode(),
        )
    )
    configure(service, base, extension)
    if included_extensions:
        with sqlite3.connect(service.store.path("host")) as connection:
            connection.execute(
                "UPDATE meta SET value=? WHERE key='extensions'", ('["UserExtension"]',)
            )
        args["extensions"] = [str(extension)]
    created = service.ed_create(**args)
    _, _, applied = apply_packet(service, created, manager_operations())
    options = {"mode": "user_extension", "extension": "UserExtension"}
    before = files_at(extension)
    preview = build(service, applied, delivery=options)
    assert files_at(extension) == before
    assert any(r["action"] == "create" for r in preview["delivery"]["files"])
    with pytest.raises(Exception) as caught:
        build(service, applied, delivery=options, mode="write", expected_preview_hash="bad")
    assert "устарел" in str(caught.value)
    written = build(
        service,
        applied,
        delivery=options,
        mode="write",
        expected_preview_hash=preview["build_hash"],
        acknowledged_notices=preview["required_acknowledgements"],
    )
    assert written["written"]
    delivered = files_at(extension)
    preview = build(service, applied, delivery=options)
    build(
        service,
        applied,
        delivery=options,
        mode="write",
        expected_preview_hash=preview["build_hash"],
        acknowledged_notices=preview["required_acknowledgements"],
    )
    assert files_at(extension) == delivered
    service = Kd2Service(service.settings)
    reopen_writer_schema(service, base)
    preview = build(service, applied, delivery=options)
    build(
        service,
        applied,
        delivery=options,
        mode="write",
        expected_preview_hash=preview["build_hash"],
        acknowledged_notices=preview["required_acknowledgements"],
    )
    assert files_at(extension) == delivered
    slot = Path(written["output_dir"])
    manifest = json.loads((slot / "manifest.json").read_bytes())
    assert manifest["delivery"]["extension"] == "UserExtension"
    assert "Установите расширение" not in (slot / "instruction.md").read_text("utf-8")
    assert "Отключите расширение" not in (slot / "instruction.md").read_text("utf-8")
    assert 'Новый Структура("Имя", "UserExtension")' in (slot / "instruction.md").read_text("utf-8")
    configuration = base / "Configuration.xml"
    configuration.write_bytes(configuration.read_bytes() + b"\n")
    with pytest.raises(EdAuthoringStaleError):
        build(service, applied, delivery=options)


@pytest.mark.parametrize("included_extensions", [False, True])
def test_registration_service_user_delivery(tmp_path, included_extensions):
    service, args = registration_setup(tmp_path)
    base = tmp_path / "dump"
    extension = tmp_path / "user"
    shutil.copytree(DATA, extension)
    plan = extension / "ExchangePlans/TargetPlan.xml"
    plan.write_bytes(
        object_xml(
            "ExchangePlan",
            "TargetPlan",
            xml((base / "ExchangePlans/TargetPlan.xml").read_bytes())[0].get("uuid"),
        )
    )
    configure(service, base, extension)
    if included_extensions:
        config = xml((extension / "Configuration.xml").read_bytes())
        children = config.find(f"*/{{{M}}}ChildObjects")
        assert children is not None
        children.clear()
        etree.SubElement(children, f"{{{M}}}CommonModule").text = "UserModule"
        etree.SubElement(children, f"{{{M}}}ExchangePlan").text = "TargetPlan"
        (extension / "Configuration.xml").write_bytes(etree.tostring(config))
        args["source"]["extensions"] = [str(extension)]
        service.structure_load_xml(
            "target", str(base), extension_paths=[str(extension)], force=True
        )
    args["delivery"] = {"mode": "user_extension", "extension": "UserExtension"}
    before = files_at(extension)
    preview = service.registration_retarget(**args)
    assert files_at(extension) == before
    written = service.registration_retarget(
        **args,
        mode="write",
        expected_preview_hash=preview["preview_hash"],
        acknowledged_notices=preview["required_acknowledgements"],
    )
    delivered = files_at(extension)
    assert b"reg_Flag" in delivered["ExchangePlans/TargetPlan.xml"]
    preview = service.registration_retarget(**args)
    service.registration_retarget(
        **args,
        mode="write",
        expected_preview_hash=preview["preview_hash"],
        acknowledged_notices=preview["required_acknowledgements"],
    )
    assert files_at(extension) == delivered
    assert "Установите расширение" not in (
        Path(written["output_path"]) / "instruction.md"
    ).read_text("utf-8")
    assert "Расширение отключайте" not in (
        Path(written["output_path"]) / "instruction.md"
    ).read_text("utf-8")
    assert "Расширение: `UserExtension`" in (
        Path(written["output_path"]) / "instruction.md"
    ).read_text("utf-8")


def test_catalog_rejects_unlisted_writable_extension(tmp_path):
    path = tmp_path / "projects.yaml"
    path.write_text(
        "projects:\n  x:\n    configurations:\n      full:\n"
        "        dump: base\n        writable_extensions: [other]\n",
        "utf-8",
    )
    with pytest.raises(Exception, match="writable_extensions"):
        load_catalog(path)


@pytest.mark.parametrize("reason", ["unknown", "readonly"])
def test_delivery_requires_configured_writable_target(merge_setup, reason):
    extension, base, generated = merge_setup
    service = Kd2Service(
        Settings(workspace=base.parent / "workspace", cache_dir=base.parent / "cache")
    )
    configure(service, base, extension)
    name = "Missing" if reason == "unknown" else "UserExtension"
    if reason == "readonly":
        path = service.settings.projects_file
        data = yaml.safe_load(path.read_text("utf-8"))
        data["projects"]["sample"]["configurations"]["full"]["writable_extensions"] = []
        path.write_text(yaml.safe_dump(data), "utf-8")
    before = files_at(extension)
    with pytest.raises(RegistrationToolError) as caught:
        prepare_user_delivery(
            service,
            {"mode": "user_extension", "extension": name},
            base,
            {"extension/" + p: b for p, b in generated.items()},
            service.workspace.root / "ed-authoring/test",
            "test",
        )
    assert caught.value.code == (
        "delivery_unknown_extension" if reason == "unknown" else "delivery_not_writable"
    )
    assert files_at(extension) == before
    assert not (service.workspace.root / "ed-authoring/test").exists()


def test_setup_mounts_only_declared_extension(merge_setup):
    extension, base, _ = merge_setup
    service = Kd2Service(
        Settings(workspace=base.parent / "workspace", cache_dir=base.parent / "cache")
    )
    configure(service, base, extension)
    rendered = yaml.safe_load(
        compose_override(
            load_catalog(service.settings.projects_file),
            LocalSettings(project_dirs={"sample": base.parent}),
        )
    )
    settings = rendered["services"]["kd-rules-mcp"]
    mount = next(v for v in settings["volumes"] if ":/extensions/" in v)
    assert mount.endswith(":/extensions/sample.full.0")
    environment = Settings.from_env(settings["environment"])
    assert environment.extension_dirs == {"sample.full.0": Path("/extensions/sample.full.0")}
    assert environment.path_map.to_local(str(extension / "Configuration.xml")) == Path(
        "/extensions/sample.full.0/Configuration.xml"
    )
