"""Контракт C: снимки, ошибки, preview, подтверждения и атомарное дополнение."""

import copy
import io
import json
import logging
import shutil
from dataclasses import asdict, replace
from pathlib import Path

import pytest
from lxml import etree

from kd2_rules_mcp.authoring.ed.handlers import operation_from_input
from kd2_rules_mcp.authoring.ed.manifest import operation_dict
from kd2_rules_mcp.authoring.ed.xml_dump import M, serialize
from kd2_rules_mcp.server import error_payload
from kd2_rules_mcp.service import Kd2Service, Settings
from kd2_rules_mcp.service import ed_authoring as module
from tests.test_ed_authoring_handlers import HANDLERS, handler_inputs
from tests.test_ed_authoring_model import DATA, IDENTITY, OPERATION
from tests.test_service_ed_routes import _xml


def all_items(call, **arguments):
    """Обходит страницу по фактически возвращённому продолжению, без потери записей."""
    result = call(**arguments)
    rows = list(result["items"])
    while result["has_more"]:
        next_offset = result["next_offset"]
        assert next_offset > result["offset"]
        following = call(**(arguments | {"offset": next_offset}))
        assert following["total"] == result["total"]
        rows.extend(following["items"])
        result = following
    assert len(rows) == result["total"]
    return rows


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


def handler_setup(setup, fixture, interface=2):
    """DTO проходят настоящие файловые снимки сервиса, без подмены подготовки/сборки."""
    old, arguments, root = setup
    incoming = fixture.get("previous_input", fixture["input"])
    previous = next((op for op in incoming if op.get("expected_previous")), None)
    text = (
        handler_inputs(
            interface,
            event=previous["event"] if previous else None,
            previous=previous["expected_previous"] if previous else "",
        )
        .document.files[0]
        .text
    )
    (root / "CommonModules/Менеджер2/Ext/Module.bsl").write_text(text, encoding="utf-8")
    if fixture.get("context") == "reference":
        package = root / "XDTOPackages/Формат120/Ext/Package.bin"
        package.write_text(
            (HANDLERS / "reference/format-1.20.bin")
            .read_text("utf-8")
            .replace("urn:fiction:1.20", "urn:fiction/1.20"),
            encoding="utf-8",
        )
        path = root / "Catalogs/Товары.xml"
        doc = etree.parse(str(path)).getroot()
        node = doc.find(".//{*}Attribute/{*}Properties/{*}Type")
        assert node is not None
        node[:] = [
            etree.fromstring(
                b'<v8:Type xmlns:v8="http://v8.1c.ru/8.1/data/core" '
                b'xmlns:cfg="http://v8.1c.ru/8.1/data/enterprise/current-config">cfg:CatalogRef.'
                + "Товары".encode()
                + b"</v8:Type>"
            )
        ]
        path.write_bytes(serialize(doc))
    service = Kd2Service(old.settings)
    refs = {
        "project_id": service.ed_open(str(root / "CommonModules/Менеджер2/Ext/Module.bsl"))[
            "project_id"
        ],
        "schema_id": service.ed_schema_open(
            "1.20", project="Пример", configuration="Main", package="Формат120"
        )["schema_id"],
        "structure_id": service.structure_load_project(
            "Пример", "Main", structure_id="fiction-main", force=True
        )["structure_id"],
    }
    arguments = arguments | {"operations": transport_operations(fixture["input"], refs)}
    return (service, arguments, root), refs


def transport_operations(operations, refs):
    rows = copy.deepcopy(operations)
    for row in rows:
        row["target"].pop("project", None)
        row["target"].pop("configuration", None)
        row["target"].update(refs)
    return rows


def test_explicit_legacy_kind_preserves_bytes_and_preview_hash(setup):
    first = preview(setup)
    raw = copy.deepcopy(setup[1]["operations"])
    raw[0]["kind"] = "add_header_property"
    explicit = preview(setup, operations=raw)
    assert first == explicit
    written = write(setup, first)
    before = content(Path(written["output_dir"]))
    assert write(setup, explicit, operations=raw)["status"] == "unchanged"
    assert content(Path(written["output_dir"])) == before
    assert json.loads(before["manifest.json"])["schema_version"] == 1


@pytest.mark.parametrize("path", sorted((HANDLERS / "dto").glob("*.json")), ids=lambda p: p.stem)
@pytest.mark.parametrize("interface", [1, 2, 3])
def test_handler_dto_service_lifecycle(setup, path, interface):
    fixture = json.loads(path.read_text("utf-8"))
    if interface not in fixture["interfaces"]:
        pytest.skip("DTO не задаёт этот интерфейс")
    current, refs = handler_setup(setup, fixture, interface)
    if "previous_input" in fixture:
        write(current, operations=transport_operations(fixture["previous_input"], refs))
    options = {"drop_operations": fixture.get("drop_operations", [])}
    if fixture.get("expected_failure"):
        refused = failure(lambda: preview(current, **options), "ed_authoring_precondition")
        assert fixture["expected_failure"] in {f["id"] for f in refused["failures"]}
        return
    viewed = preview(current, **options)
    assert viewed["summary"]["layer"] == {
        "certain": True,
        "unknown_lines": 0,
        "unresolved_dispatch": 0,
        "new_issues": 0,
    }
    assert not viewed["summary"]["runtime_verified"]
    assert '"body":' not in json.dumps(viewed, ensure_ascii=False)
    service, arguments, _ = current
    rows = all_items(
        service.ed_authoring_build, **arguments, **options, section="operations", limit=1
    )
    assert [r["operation_id"] for r in rows] == fixture["operation_ids"]
    for row in rows:
        if "body" in row:
            assert row["body_sha256"] == module.sha256(row["body"].encode())
            assert row["body_origin"] in ("agent", "preset")
    refused = failure(
        lambda: service.ed_authoring_build(
            **arguments, **options, mode="write", expected_preview_hash=viewed["build_hash"]
        ),
        "ed_authoring_ack_required",
    )
    assert refused["required_acknowledgements"]
    written = write(current, viewed, **options)
    destination = Path(written["output_dir"])
    before = content(destination)
    assert json.loads(before["manifest.json"])["schema_version"] == 2
    golden = (
        HANDLERS / "golden" / path.stem / ("module.bsl" if interface in (1, 2) else "module-v3.bsl")
    )
    assert (
        next(b for n, b in before.items() if n.startswith("modules/") and n.endswith(".bsl"))
        == golden.read_bytes()
    )
    assert write(current)["status"] == "unchanged"
    assert content(destination) == before
    extra = copy.deepcopy(setup[1]["operations"][0])
    extra["target"] = extra["target"] | refs | {"pko_address": "ПКО/Заказ", "direction": "send"}
    added = preview(current, operations=[extra])
    assert added["build_hash"] != viewed["build_hash"]
    failure(lambda: write(current, viewed, operations=[extra]), "ed_authoring_stale")
    write(current, added, operations=[extra])
    extra_id = service._operation(extra, "Пример", "Main")[0].operation_id
    dropped = preview(current, drop_operations=[extra_id])
    write(current, dropped, drop_operations=[extra_id])
    assert content(destination) == before
    for name in before:
        if name.endswith("Module.bsl"):
            (destination / name).write_bytes(before[name] + b"\n// changed body\n")
            break
    refused = failure(lambda: preview(current), "ed_authoring_precondition")
    assert refused["failures"][0]["id"] == "ed.author.owned_content_changed"


def test_v1_migration_and_dropping_last_handler_keeps_v2(setup):
    prop = copy.deepcopy(setup[1]["operations"][0])
    fixture = json.loads((HANDLERS / "dto/set-send.json").read_text("utf-8"))
    current, _ = handler_setup(setup, fixture)
    prop["target"] = current[1]["operations"][0]["target"]
    legacy = write(current, operations=[prop])
    old = json.loads((Path(legacy["output_dir"]) / "manifest.json").read_text("utf-8"))
    viewed = preview(current)
    assert viewed["summary"]["migration"] == {"from": 1, "to": 2}
    assert "версии 1" in viewed["changes"][0]
    failure(lambda: write(current, legacy), "ed_authoring_stale")
    written = write(current, viewed)
    new = json.loads((Path(written["output_dir"]) / "manifest.json").read_text("utf-8"))
    assert old["identity_map"] == new["identity_map"]
    handler_id = next(
        r["operation_id"] for r in new["operations"] if r.get("kind") == "set_object_handler"
    )
    dropped = preview(current, operations=[prop], drop_operations=[handler_id])
    assert dropped["summary"]["schema_version"] == 2 and dropped["summary"]["handlers"] == 0
    write(current, dropped, operations=[prop], drop_operations=[handler_id])
    assert write(current, operations=[prop])["status"] == "unchanged"


def test_handler_body_replacement_requires_drop_and_changes_preview(setup):
    fixture = json.loads((HANDLERS / "dto/set-send.json").read_text("utf-8"))
    current, _ = handler_setup(setup, fixture)
    first = write(current)
    old_id = operation_from_input(fixture["input"][0]).operation_id
    incoming = copy.deepcopy(current[1]["operations"])
    incoming[0]["body"] = "Возврат;\n// Новое тело\n"
    refused = failure(lambda: preview(current, operations=incoming), "ed_authoring_precondition")
    assert refused["failures"][0]["id"] == "ed.author.handler_slot_conflict"
    viewed = preview(current, operations=incoming, drop_operations=[old_id])
    assert viewed["build_hash"] != first["build_hash"]
    failure(
        lambda: write(current, first, operations=incoming, drop_operations=[old_id]),
        "ed_authoring_stale",
    )
    write(current, viewed, operations=incoming, drop_operations=[old_id])


def test_v2_concurrent_write_and_folder_change(setup, monkeypatch):
    fixture = json.loads((HANDLERS / "dto/set-send.json").read_text("utf-8"))
    current, _ = handler_setup(setup, fixture)
    viewed = preview(current)
    assert write(current, viewed)["status"] == "written"
    assert write(current, viewed)["status"] == "unchanged"
    service = current[0]
    original = service._write_artifact

    def concurrent(destination, files, previous, entries):
        path = next(p for p in destination.rglob("Module.bsl"))
        path.write_bytes(path.read_bytes() + b"\n// concurrent\n")
        return original(destination, files, previous, entries)

    monkeypatch.setattr(service, "_write_artifact", concurrent)
    failure(lambda: write(current, viewed), "ed_authoring_stale")
    assert any(b"// concurrent" in b for b in content(Path(viewed["output_dir"])).values())


@pytest.mark.parametrize("body", ["// " + "x" * 65536, "Возврат;\n" * 2001], ids=["bytes", "lines"])
def test_handler_body_limits_and_summary_without_body(setup, body):
    fixture = json.loads((HANDLERS / "dto/set-send.json").read_text("utf-8"))
    fixture["input"][0]["body"] = body
    current, _ = handler_setup(setup, fixture)
    failure(lambda: preview(current), "ed_authoring_resource_limit")


def test_large_body_only_on_operations_page_and_stale_inputs(setup, caplog):
    fixture = json.loads((HANDLERS / "dto/set-send.json").read_text("utf-8"))
    fixture["input"][0]["body"] = "// BODY_SENTINEL_" + "x" * 5000 + "\nВозврат;\n"
    current, _ = handler_setup(setup, fixture)
    caplog.set_level(logging.INFO, logger=module.__name__)
    viewed = preview(current)
    assert "BODY_SENTINEL" not in json.dumps(viewed) and "BODY_SENTINEL" not in caplog.text
    page = preview(current, section="operations")
    assert "BODY_SENTINEL" in page["items"][0]["body"]
    assert not page["has_more"] and page["next_offset"] == 1
    first = write(current, viewed)
    path = current[2] / "CommonModules/Менеджер2/Ext/Module.bsl"
    path.write_text(path.read_text("utf-8") + "\n// Новый снимок\n", encoding="utf-8")
    fresh = reopen_authoring(current)
    new = preview(fresh)
    assert new["build_hash"] != first["build_hash"]
    failure(lambda: write(fresh, first), "ed_authoring_stale")
    write(fresh, new)


def test_handlers_layer_rejects_new_issue_in_generated_module(setup, monkeypatch):
    fixture = json.loads((HANDLERS / "dto/set-send.json").read_text("utf-8"))
    current, _ = handler_setup(setup, fixture)
    original = module.render_handlers_authoring

    def tampered(*args, **kwargs):
        bundle = original(*args, **kwargs)
        files = {
            p: b.replace(b'"' + "Товар".encode() + b'"', b'"' + "Заказ".encode() + b'"')
            if p.endswith("Module.bsl")
            else b
            for p, b in bundle.files.items()
        }
        return replace(bundle, files=files)

    monkeypatch.setattr(module, "render_handlers_authoring", tampered)
    refused = failure(lambda: preview(current), "ed_authoring_precondition")
    assert "ed.author.new_issues" in {f["id"] for f in refused["failures"]}


def test_body_failure_does_not_expose_body_text(setup):
    fixture = json.loads((HANDLERS / "dto/set-send.json").read_text("utf-8"))
    fixture["input"][0]["body"] = 'ДанныеXDTO.Вставить(Имя + "BODY_SENTINEL", 1);'
    current, _ = handler_setup(setup, fixture)
    refused = failure(lambda: preview(current), "ed_authoring_precondition")
    assert "BODY_SENTINEL" not in json.dumps(refused)
    assert "ed.author.body_dynamic_reference" in {f["id"] for f in refused["failures"]}


@pytest.mark.parametrize("scenario", ["set-send", "preserve-receive"])
def test_v2_two_managers_survive_service_restart(setup, scenario, monkeypatch):
    fixture = json.loads((HANDLERS / f"dto/{scenario}.json").read_text("utf-8"))
    current, refs = handler_setup(setup, fixture)
    service, args, root = current
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
    (root / "CommonModules/Менеджер1/Ext/Module.bsl").write_text(
        handler_inputs(1).document.files[0].text, encoding="utf-8"
    )
    service.structure_load_project("Пример", "Main", structure_id=refs["structure_id"], force=True)
    first = write(current)
    second_fixture = json.loads((HANDLERS / "dto/set-send.json").read_text("utf-8"))
    second = transport_operations(second_fixture["input"], refs)[0]
    second["target"].update(
        plan="ВторойПлан",
        pko_address="ПКО/Заказ",
        project_id=service.ed_open(str(root / "CommonModules/Менеджер1/Ext/Module.bsl"))[
            "project_id"
        ],
    )
    second["body"] = "Возврат;\n"
    rows = all_items(
        lambda **options: preview(current, operations=[second], **options), section="operations"
    )
    bodies = {row["handler_name"]: row["body"] for row in rows if "body" in row}
    body_bytes = sum(len(body.encode("utf-8")) for body in bodies.values())
    assert module.MAX_HANDLER_BODY_BYTES == 1024 * 1024
    # Уменьшенная граница проверяет сумму двух менеджеров, включая порождённый preset.
    monkeypatch.setattr(module, "MAX_HANDLER_BODY_BYTES", body_bytes - 1)
    failure(lambda: preview(current, operations=[second]), "ed_authoring_resource_limit")
    monkeypatch.setattr(module, "MAX_HANDLER_BODY_BYTES", body_bytes)
    written = write(current, operations=[second])
    destination = Path(written["output_dir"])
    files = content(destination)
    manifest = module.previous_artifact(files)
    assert manifest is not None and len(manifest.handler_operations) == 2
    assert len(manifest.handler_bindings) == 2 and len(manifest.procedures) == 4
    assert all("module" in record for record in manifest.procedures)
    assert len(written["scopes"]) == 2 and written["summary"]["handlers"] == 2
    restarted = Kd2Service(service.settings)
    second["target"]["project_id"] = restarted.ed_open(
        str(root / "CommonModules/Менеджер1/Ext/Module.bsl")
    )["project_id"]
    second["target"]["schema_id"] = restarted.ed_schema_open(
        "1.20", project="Пример", configuration="Main", package="Формат120"
    )["schema_id"]
    fresh = restarted, args | {"operations": [second]}, root
    assert write(fresh)["status"] == "unchanged"
    assert content(destination) == files and first["output_dir"] == written["output_dir"]


def reopen_authoring(setup, *, root=None):
    """Новые снимки после обновления или переноса той же выгрузки."""
    old, arguments, old_root = setup
    root = root or old_root
    service = Kd2Service(replace(old.settings, project_dirs={"Пример": root}))
    arguments = copy.deepcopy(arguments)
    target = arguments["operations"][0]["target"]
    target["project_id"] = service.ed_open(str(root / "CommonModules/Менеджер2/Ext/Module.bsl"))[
        "project_id"
    ]
    target["schema_id"] = service.ed_schema_open(
        "1.20", project="Пример", configuration="Main", package="Формат120"
    )["schema_id"]
    target["structure_id"] = service.structure_load_project(
        "Пример", "Main", structure_id="fiction-main", force=True
    )["structure_id"]
    return service, arguments, root


def test_rebuild_changed_manager_and_stale_current_preview(setup):
    first = write(setup)
    destination = Path(first["output_dir"])
    before = content(destination)
    old = json.loads(before["manifest.json"])
    path = setup[2] / "CommonModules/Менеджер2/Ext/Module.bsl"
    path.write_text(path.read_text("utf-8") + "\n// Обновление типового модуля\n", encoding="utf-8")
    current = reopen_authoring(setup)
    viewed = preview(current)
    assert viewed["rebuild"] and "files" in viewed["changed_input_groups"]
    rows = all_items(current[0].ed_authoring_build, **current[1])
    assert {r["name"] for r in rows if r["kind"] == "input_changed"} >= {
        "CommonModules/Менеджер2/Ext/Module.bsl"
    }
    assert all("sha256" not in r for r in rows if r["kind"] == "input_changed")
    assert content(destination) == before
    stale = failure(lambda: write(current, first), "ed_authoring_stale")
    assert "files" in stale["changed_inputs"]
    assert content(destination) == before
    written = write(current, viewed)
    assert written["status"] == "written"
    new = json.loads(content(destination)["manifest.json"])
    assert old["identity_map"] == new["identity_map"]
    assert old["operations"] == new["operations"]
    assert not preview(current)["rebuild"]
    assert write(current)["status"] == "unchanged"


@pytest.mark.parametrize(
    "change, expected",
    [
        ("occupied", "ed.author.format_property_occupied"),
        ("rename", "ed.author.pko_missing"),
        ("signature", "ed.author.manager_signature"),
    ],
)
def test_rebuild_invalid_previous_operation_is_identified_and_can_be_dropped(
    setup, change, expected
):
    first = write(setup)
    destination = Path(first["output_dir"])
    before = content(destination)
    previous = json.loads(before["manifest.json"])
    old_id = previous["operations"][0]["operation_id"]
    path = setup[2] / "CommonModules/Менеджер2/Ext/Module.bsl"
    text = path.read_text("utf-8")
    text = {
        "occupied": text.replace('"Код", "Код"', '"Код", "Комментарий"', 1),
        "rename": text.replace('ИмяПКО = "Товар"', 'ИмяПКО = "ТоварНовый"'),
        "signature": text.replace("НаправлениеОбмена, ПравилаКонвертации)", "ПравилаКонвертации)"),
    }[change]
    path.write_text(text, encoding="utf-8")
    current = reopen_authoring(setup)
    new = copy.deepcopy(current[1]["operations"][0])
    new["target"]["pko_address"] = "ПКО/Заказ"
    refused = failure(lambda: preview(current, operations=[new]), "ed_authoring_precondition")
    failures = refused["failures"]
    assert any(
        f["id"] == expected and f["operation_id"] == old_id and f["from_previous"] for f in failures
    )
    assert content(destination) == before
    if change == "signature":
        # Сигнатура мешает и новым операциям этого менеджера: исключение не обходит проверку.
        failure(
            lambda: preview(current, operations=[new], drop_operations=[old_id]),
            "ed_authoring_precondition",
        )
        assert content(destination) == before
        return
    viewed = preview(current, operations=[new], drop_operations=[old_id])
    assert viewed["rebuild"] and viewed["change_counts"]["pks_added"] == 1
    write(current, viewed, operations=[new], drop_operations=[old_id])
    manifest = json.loads(content(destination)["manifest.json"])
    assert all(op["operation_id"] != old_id for op in manifest["operations"])
    for key in (
        manifest["identity_map"]["objects"].keys() & previous["identity_map"]["objects"].keys()
    ):
        assert manifest["identity_map"]["objects"][key] == previous["identity_map"]["objects"][key]


def test_rebuild_portable_source_paths_and_project_root(setup, tmp_path):
    first = write(setup)
    destination = Path(first["output_dir"])
    old = json.loads((destination / "manifest.json").read_bytes())
    moved = tmp_path / "mounted-dump"
    shutil.copytree(setup[2], moved)
    current = reopen_authoring(setup, root=moved)
    viewed = preview(current)
    write(current, viewed)
    new = json.loads((destination / "manifest.json").read_bytes())
    assert old["source_hashes"] == new["source_hashes"]
    assert old["identity_map"] == new["identity_map"]
    assert any(
        p.startswith("XDTOPackages/") and p.endswith("Package.bin") for p in new["source_hashes"]
    )
    assert all(
        not Path(p).is_absolute() and "\\" not in p and not p.startswith("schemas/")
        for p in new["source_hashes"]
    )


def test_rebuild_preserves_previously_assigned_uuid(setup):
    from kd2_rules_mcp.authoring.ed.artifacts import with_source_hashes
    from kd2_rules_mcp.authoring.ed.identity import IdentityMap
    from kd2_rules_mcp.authoring.ed.render import render_authoring

    first = write(setup)
    service = setup[0]
    entry = next(iter(service._authoring_inputs.values()))
    assert entry.prepared is not None
    destination = Path(first["output_dir"])
    old = module.ArtifactManifest.from_bytes((destination / "manifest.json").read_bytes())
    ids = IdentityMap(
        old.identity_map.artifact_uuid,
        {**old.identity_map.objects, "configuration": "00000000-0000-0000-0000-000000000800"},
        old.identity_map.borrowed,
    )
    # Прежний комплект B с внешней картой: его manifest соответствует всем XML-байтам.
    previous = with_source_hashes(
        render_authoring(entry.prepared, entry.descriptions, identity_map=ids), old.source_hashes
    )
    for name, payload in previous.files.items():
        (destination / name).write_bytes(payload)
    path = setup[2] / "CommonModules/Менеджер2/Ext/Module.bsl"
    path.write_text(path.read_text("utf-8") + "\n// Новая редакция\n", encoding="utf-8")
    current = reopen_authoring(setup)
    write(current)
    new = module.ArtifactManifest.from_bytes((destination / "manifest.json").read_bytes())
    assert new.identity_map == ids


def test_refreshed_snapshots_in_same_service_rebuild_then_enforce_preview_hash(setup):
    first = write(setup)
    service, arguments, root = setup
    path = root / "CommonModules/Менеджер2/Ext/Module.bsl"
    path.write_text(path.read_text("utf-8") + "\n// Новая редакция\n", encoding="utf-8")
    target = arguments["operations"][0]["target"]
    service.ed_close(target["project_id"])
    target["project_id"] = service.ed_open(str(path))["project_id"]
    service.ed_routes(project="Пример", configuration="Main", force=True)
    viewed = preview(setup)
    assert viewed["rebuild"]
    stale = failure(lambda: write(setup, first), "ed_authoring_stale")
    assert stale["changed_inputs"]["files"] == ("CommonModules/Менеджер2/Ext/Module.bsl",)
    assert not stale["decisions_changed"]
    write(setup, viewed)


@pytest.mark.parametrize("drops", [["unknown"], ["duplicate", "duplicate"], "not-a-list", [None]])
def test_drop_operations_arguments(setup, drops):
    failure(lambda: preview(setup, drop_operations=drops), "invalid_argument")


def test_preview_write_repeat_and_both_deliveries(setup):
    service, _, _ = setup
    before = content(service.workspace.root)
    viewed = preview(setup)
    assert viewed["status"] == "ready" and not viewed["written"]
    scope = viewed["scopes"][0]
    assert scope["version_scope"] == "manager" and scope["directions"] == ["send"]
    assert scope["manager"] == "Менеджер2" and scope["plan_count"] == 1
    assert scope["plans"][0]["plan"] == "ПланФормата"
    assert scope["plans"][0]["versions"] == ["1.20", "1.21"]
    assert scope["without_node"] == {"status": "partial", "versions": []}
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
    assert found["has_more"] and found["auto"] is False
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
    rows = list(viewed["items"])
    while viewed["has_more"]:
        continued = preview(setup, offset=viewed["next_offset"], limit=200)
        assert continued["build_hash"] == viewed["build_hash"]
        assert continued["validation"] == viewed["validation"]
        rows.extend(continued["items"])
        viewed = continued
    assert len(rows) == viewed["total"]
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


def test_authoring_never_walks_dump_and_reuses_preparation(setup, monkeypatch, caplog):
    service, args, root = setup
    opened = service.ed_routes(project="Пример", configuration="Main")
    snapshot = service._require_route(opened["profile_id"])
    document = service._ed_project(args["operations"][0]["target"]["project_id"]).document
    files = []
    original_open = io.open

    def open_file(path, *args, **kwargs):
        files.append(Path(path))
        return original_open(path, *args, **kwargs)

    def forbidden_walk(path, *args, **kwargs):
        raise AssertionError(f"Авторинг не должен обходить каталоги: {path}")

    def forbidden_prepare(*args, **kwargs):
        raise AssertionError("Повторный preview/write должен использовать подготовленный результат")

    monkeypatch.setattr(io, "open", open_file)
    monkeypatch.setattr(Path, "rglob", forbidden_walk)
    monkeypatch.setattr(Path, "iterdir", forbidden_walk)
    caplog.set_level(logging.INFO, logger=module.__name__)
    first = preview(setup)
    entry = next(iter(service._authoring_inputs.values()))
    assert entry.route is snapshot and entry.value.document is document
    assert files.count(root / "CommonModules/Менеджер2/Ext/Module.bsl") == 1  # Только сверка SHA.
    assert len(caplog.records) == 1
    assert all(
        name + "=" in caplog.records[0].message
        for name in ("inputs", "routes", "schemas", "prepare", "render", "files_read")
    )
    monkeypatch.setattr(module, "prepare_authoring", forbidden_prepare)
    files.clear()
    repeated = preview(setup)
    assert repeated == first
    assert not any(p.is_relative_to(root) for p in files)
    # Запись нового комплекта тоже не обходит выгрузку (предыдущего каталога ещё нет).
    result = write(setup, first)
    assert result["status"] == "written"
    assert len(caplog.records) == 3


def test_route_change_during_preparation_is_stale_and_not_published(setup, monkeypatch):
    service, _, root = setup
    original = module.prepare_authoring

    def changed(*args, **kwargs):
        prepared = original(*args, **kwargs)
        path = root / "ExchangePlans/ПланФормата/Ext/ManagerModule.bsl"
        path.write_bytes(path.read_bytes() + b"\n")
        return prepared

    monkeypatch.setattr(module, "prepare_authoring", changed)
    failure(lambda: preview(setup), "ed_authoring_stale")
    assert not (service.workspace.root / "ed-authoring").exists()


def test_candidates_also_verify_cached_route_by_read_files(setup):
    service, args, root = setup
    preview(setup)
    path = root / "ExchangePlans/ПланФормата/Ext/ManagerModule.bsl"
    path.write_bytes(path.read_bytes() + b"\n")
    target = args["operations"][0]["target"] | {"project": "Пример", "configuration": "Main"}
    failure(lambda: service.ed_authoring_candidates(target, "format"), "ed_authoring_stale")


def test_failed_other_schema_becoming_readable_is_stale(setup):
    _, _, root = setup
    path = root / "XDTOPackages/Формат121/Ext/Package.bin"
    original = path.read_bytes()
    path.write_bytes(b"")
    viewed = preview(setup)
    path.write_bytes(original)
    failure(lambda: write(setup, viewed), "ed_authoring_stale")


def test_missing_import_package_appearing_after_preview_is_stale(setup):
    service, _, root = setup
    namespace = "urn:fiction:other"
    xml = etree.parse(str(root / "Configuration.xml")).getroot()
    children = xml[0].find(f"{{{M}}}ChildObjects")
    assert children is not None
    etree.SubElement(children, f"{{{M}}}XDTOPackage").text = "Другие"
    (root / "Configuration.xml").write_bytes(serialize(xml))
    (root / "XDTOPackages/Другие.xml").write_text(_xml("Другие", namespace), encoding="utf-8")
    package = root / "XDTOPackages/Формат121/Ext/Package.bin"
    package.write_text(
        package.read_text("utf-8").replace("urn:fiction:common", namespace), encoding="utf-8"
    )
    service.structure_load_project("Пример", "Main", structure_id="fiction-main", force=True)
    viewed = preview(setup)
    path = root / "XDTOPackages/Другие/Ext/Package.bin"
    path.parent.mkdir(parents=True)
    path.write_text(
        (root / "XDTOPackages/Общие/Ext/Package.bin")
        .read_text("utf-8")
        .replace("urn:fiction:common", namespace),
        encoding="utf-8",
    )
    failure(lambda: write(setup, viewed), "ed_authoring_stale")


def test_preopened_schema_file_missing_is_stale(setup):
    _, _, root = setup
    (root / "XDTOPackages/Общие/Ext/Package.bin").unlink()
    failure(lambda: preview(setup), "ed_authoring_stale")


def test_route_reuse_verifies_every_cached_document_it_consumes(setup):
    service, _, root = setup
    manager = root / "CommonModules/Менеджер1/Ext/Module.bsl"
    service.ed_open(str(manager))
    manager.write_bytes(manager.read_bytes() + b"\n")
    plan = root / "ExchangePlans/ПланФормата/Ext/ManagerModule.bsl"
    plan.write_text(
        plan.read_text("utf-8").replace(
            'Версии.Вставить("1.20",',
            'Версии.Вставить("1.19", Менеджер1);\nВерсии.Вставить("1.20",',
        ),
        encoding="utf-8",
    )
    failure(lambda: preview(setup), "ed_authoring_stale")


def test_foreign_extensions_read_only_known_manager_and_owner_paths(setup, monkeypatch):
    service, _, root = setup
    foreign = root.parent / "foreign"
    foreign.mkdir()
    xml = etree.parse(str(root / "Configuration.xml")).getroot()
    children = xml[0].find(f"{{{M}}}ChildObjects")
    assert children is not None
    children.clear()
    (foreign / "Configuration.xml").write_bytes(serialize(xml))
    owner = foreign / "Catalogs/Товары.xml"
    owner.parent.mkdir()
    owner.write_bytes((root / "Catalogs/Товары.xml").read_bytes())
    manager = foreign / "CommonModules/Менеджер2/Ext/Module.bsl"
    manager.parent.mkdir(parents=True)
    manager.write_text("&НаСервере\nПроцедура Чужая()\nКонецПроцедуры\n", encoding="utf-8")
    unrelated = foreign / "CommonModules/Чужой/Ext/Module.bsl"
    unrelated.parent.mkdir(parents=True)
    unrelated.write_text("Не читается", encoding="utf-8")
    catalog = service.settings.projects_file
    catalog.write_text(
        catalog.read_text("utf-8") + "        extensions: [../foreign]\n", encoding="utf-8"
    )
    service.structure_load_project("Пример", "Main", structure_id="fiction-main", force=True)
    opened = []
    original = io.open

    def read(path, *args, **kwargs):
        path = Path(path)
        if path.is_relative_to(foreign):
            opened.append(path)
        return original(path, *args, **kwargs)

    def forbidden(*args, **kwargs):
        raise AssertionError("Расширения не обходятся")

    monkeypatch.setattr(io, "open", read)
    monkeypatch.setattr(Path, "rglob", forbidden)
    monkeypatch.setattr(Path, "iterdir", forbidden)
    assert preview(setup)["status"] == "ready"
    assert set(opened) == {foreign / "Configuration.xml", manager, owner}
    assert len(opened) == 3


@pytest.mark.parametrize(
    "options",
    [
        {"section": "unknown"},
        {"section": []},
        {"section": "issues_before", "mode": "write"},
        {"section": "scopes", "mode": "write"},
        {"level": "error"},
        {"level": True},
        {"check_prefix": 1},
        {"address_prefix": False},
    ],
)
def test_build_section_arguments_are_validated_before_work(setup, options, monkeypatch):
    service, _, _ = setup

    def unexpected(*args, **kwargs):
        raise AssertionError("Некорректный запрос не должен читать входы")

    monkeypatch.setattr(service, "_inputs_for", unexpected)
    failure(lambda: preview(setup, **options), "invalid_argument")


def test_summary_sections_filters_and_full_bundle_are_independent(setup, monkeypatch):
    from kd2_rules_mcp.authoring.ed.model import ValidationDelta
    from kd2_rules_mcp.authoring.ed.render import render_authoring
    from kd2_rules_mcp.service.ed_authoring_views import json_size
    from kd2_rules_mcp.validation.report import Issue, Level, Skipped

    original = module.prepare_authoring
    captured = []
    old = tuple(
        Issue(Level.WARNING, "legacy.warning", "ПКО/Старое", f"Старое {i}") for i in range(100)
    )
    new = Issue(Level.ERROR, "review.new", "ПКО/Товар/ПКС/Комментарий", "Новое в другой версии")
    unchecked = Skipped("review.unchecked", "Не проверено ПКО/Товар/ПКС/Комментарий")

    def prepare(*args, **kwargs):
        prepared = original(*args, **kwargs)
        selected, other = prepared.selected_profiles[0], prepared.other_profiles[0]
        selected = replace(
            selected,
            before=replace(selected.before, issues=old, skipped=()),
            after=replace(selected.after, issues=old[1:], skipped=()),
            delta=ValidationDelta((), old[:1], ()),
        )
        other = replace(
            other,
            before=replace(other.before, issues=(), skipped=()),
            after=replace(other.after, issues=(new,), skipped=(unchecked,)),
            delta=ValidationDelta((new,), (), (unchecked,)),
        )
        prepared = replace(prepared, selected_profiles=(selected,), other_profiles=(other,))
        captured.append(prepared)
        return prepared

    monkeypatch.setattr(module, "prepare_authoring", prepare)
    summary = preview(setup)
    assert json_size(summary) <= 4096
    assert summary["section"] == "summary"
    assert set(summary["validation"]["before"]) == {"errors", "warnings", "skipped"}
    assert set(summary["validation"]["after"]) == {"errors", "warnings", "skipped"}
    assert summary["validation"]["before"]["warnings"] == 100
    assert summary["validation"]["after"]["warnings"] == 99
    assert summary["validation"]["delta"]["disappeared_warnings"] == 1
    rows = all_items(lambda **options: preview(setup, **options))
    assert {r["kind"] for r in rows} == {
        "notice",
        "issue_new",
        "issue_disappeared",
        "skipped_new",
        "file",
    }
    assert [r["message"] for r in rows if r["kind"] == "issue_disappeared"] == ["Старое 0"]
    assert [r["message"] for r in rows if r["kind"] == "issue_new"] == ["Новое в другой версии"]
    encoded = json.dumps(summary, ensure_ascii=False)
    assert "call_chain" not in encoded and "variants" not in encoded
    assert "issue_before" not in encoded and "issue_after" not in encoded
    for section, expected in (("issues_before", 100), ("issues_after", 99)):
        response = preview(
            setup,
            section=section,
            level="предупреждение",
            check_prefix="legacy.",
            address_prefix="пко/старое",
            limit=2,
        )
        assert response["total"] == expected and len(response["items"]) == 2
        assert response["validation"] == summary["validation"]
        assert response["build_hash"] == summary["build_hash"]
    filtered = preview(setup, section="issues_after", level="ошибка", check_prefix="review.")
    assert filtered["total"] == 1 and filtered["items"][0]["message"] == "Новое в другой версии"
    boundary = preview(setup, section="issues_after", address_prefix="ПКО/Стар")
    assert boundary["total"] == 0  # Префикс адреса — сегмент, не подстрока.
    skipped = preview(setup, section="skipped", check_prefix="review.", address_prefix="ПКО/Товар")
    assert skipped["total"] == 1 and skipped["items"][0]["phase"] == "after"
    runtime = preview(setup, section="skipped", check_prefix="runtime.")
    assert {r["check"] for r in runtime["items"]} == set(summary["runtime_unverified"])
    for response in (filtered, boundary, skipped, runtime):
        assert response["validation"] == summary["validation"]
    same = preview(setup, level="ошибка", check_prefix="no-match", address_prefix="ПКО/Нет")
    assert same == summary
    service, _, _ = setup
    entry = next(iter(service._authoring_inputs.values()))
    expected = render_authoring(captured[-1], service._descriptions(entry, captured[-1].operations))
    assert write(setup, summary)["status"] == "written"
    actual = content(Path(summary["output_dir"]))
    assert actual["validation.json"] == expected.files["validation.json"]
    assert actual["instruction.md"] == expected.files["instruction.md"]


def test_scopes_flat_rows_keep_variants_without_call_chains(setup, monkeypatch):
    from kd2_rules_mcp.ed.route_model import VariantInfo

    service, _, _ = setup
    original = service._inputs_for

    def inputs(*args, **kwargs):
        entry = original(*args, **kwargs)
        routes = entry.value.routes
        plan = routes.plans[0]
        source = replace(plan.entries[0].source, call_chain=(entry.value.document.pko[0].span,))
        variants = tuple(VariantInfo(f"v{i}", f"v{i}", source) for i in range(5))
        plan = replace(
            plan, variants=variants, entries=tuple(replace(e, source=source) for e in plan.entries)
        )
        routes = replace(routes, plans=(plan,), without_node_entries=(plan.entries[0],))
        return replace(entry, value=replace(entry.value, routes=routes))

    monkeypatch.setattr(service, "_inputs_for", inputs)
    summary = preview(setup)
    assert summary["scopes"][0]["plans"][0]["versions"] == ["1.20", "1.21"]
    detail = preview(setup, section="scopes", limit=3)
    assert detail["total"] == 11 and detail["has_more"]
    rows = all_items(lambda **options: preview(setup, **options), section="scopes", limit=3)
    assert {r["variant"] for r in rows if r["plan"]} == {"v0", "v1", "v2", "v3", "v4"}
    assert rows[-1]["plan"] is None
    assert all(isinstance(r["source"], str) and ":" in r["source"] for r in rows)
    assert "call_chain" not in json.dumps(detail)
    assert detail["validation"] == summary["validation"]


def test_candidates_hundreds_of_properties_have_bounded_pages(setup):
    from kd2_rules_mcp.service.ed_authoring_views import json_size

    service, args, root = setup
    path = root / "XDTOPackages/Формат120/Ext/Package.bin"
    schema = etree.parse(str(path)).getroot()
    namespace = "http://v8.1c.ru/8.1/xdto"
    owner = schema.find(f"{{{namespace}}}objectType[@name='Справочник.Товары']")
    assert owner is not None
    for number in range(500):
        etree.SubElement(
            owner,
            f"{{{namespace}}}property",
            name=f"Реквизит{number:03}",
            type="xs:string",
            lowerBound="0",
        )
    path.write_bytes(etree.tostring(schema))
    service.ed_schema_close(args["operations"][0]["target"]["schema_id"])
    opened = service.ed_schema_open(
        "1.20", project="Пример", configuration="Main", package="Формат120"
    )
    service.structure_load_project("Пример", "Main", structure_id="fiction-main", force=True)
    target = args["operations"][0]["target"] | {"project": "Пример", "configuration": "Main"}
    target["schema_id"] = opened["schema_id"]

    def call(**options):
        response = service.ed_authoring_candidates(
            target, "format", text="Реквизит", configuration_attribute="Заметка", **options
        )
        assert json_size(response) <= 4096
        return response

    first = call()
    assert first["total"] == 500 and first["has_more"] and first["next_offset"] < 50
    assert json_size(first) <= 4096
    assert set(first["items"][0]) == {"name", "path", "type", "compatible", "reason"}
    assert first["auto"] is False
    assert all(row["compatible"] for row in first["items"])

    rows = all_items(call)
    assert [r["name"] for r in rows] == [f"Реквизит{i:03}" for i in range(500)]
