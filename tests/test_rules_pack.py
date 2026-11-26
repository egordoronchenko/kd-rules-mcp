"""`rules_pack`: ZIP правил для загрузки в БСП (задача #1) — без баз 1С."""

import base64
import hashlib
import json
import zipfile
from datetime import UTC, datetime
from importlib.metadata import version
from pathlib import Path
from typing import Any

import pytest
from lxml import etree
from mcp import Client

from kd_rules_mcp.authoring.pack import (
    CORRESPONDENT,
    EXCHANGE,
    FORM_CONVERSION,
    FORM_SET,
    REGISTRATION,
    collect,
    pack_rules,
)
from kd_rules_mcp.errors import Kd2Error, RulesFormatError
from kd_rules_mcp.kd2.rules_io import load_rules
from kd_rules_mcp.server import create_server
from kd_rules_mcp.service import Kd2Service, PathMap, Settings

DATA = Path(__file__).parent / "data"


def _mirror(data: bytes) -> bytes:
    """Правила корреспондента для синтетики: элементы `Источник` и `Приемник` поменяны местами."""
    root = etree.fromstring(data)
    source, target = root.find("Источник"), root.find("Приемник")
    assert source is not None and target is not None
    source.tag, target.tag = "Приемник", "Источник"
    return etree.tostring(root, encoding="utf-8", xml_declaration=False)


@pytest.fixture
def rules_set(tmp_path: Path) -> Path:
    """Папка комплекта: правила БП → ЗУП, корреспондент ЗУП → БП, регистрация для БП."""
    folder = tmp_path / "ПравилаОбмена" / "ОбменЗУП"
    folder.mkdir(parents=True)
    exchange = (DATA / "exchange_rules.xml").read_bytes()
    (folder / EXCHANGE).write_bytes(exchange)
    (folder / CORRESPONDENT).write_bytes(_mirror(exchange))
    (folder / REGISTRATION).write_bytes((DATA / "registration_rules.xml").read_bytes())
    return folder


def test_full_set_is_packed_byte_for_byte(rules_set: Path, tmp_path: Path) -> None:
    result = pack_rules(collect(rules_set, {}), tmp_path / "out" / "set.zip")
    assert result.form == FORM_SET and result.warnings == []
    with zipfile.ZipFile(result.path) as archive:
        # Только файлы и без каталогов: БСП считает всё, что найдёт НайтиФайлы.
        assert archive.namelist() == [EXCHANGE, CORRESPONDENT, REGISTRATION]
        for name in archive.namelist():
            assert archive.read(name) == (rules_set / name).read_bytes()
    summary = {item.name: item.summary for item in result.files}
    assert summary[EXCHANGE]["source"] == "БП" and summary[CORRESPONDENT]["source"] == "ЗУП"
    assert summary[REGISTRATION] == {
        "name": "Регистрация",
        "exchange_plan": "ОбменЗУП",
        "configuration": "БП",
    }


def test_pair_without_registration_goes_to_conversion_form(rules_set: Path, tmp_path: Path) -> None:
    (rules_set / REGISTRATION).unlink()
    result = pack_rules(collect(rules_set, {}), tmp_path / "pair.zip")
    assert result.form == FORM_CONVERSION
    with zipfile.ZipFile(result.path) as archive:
        assert archive.namelist() == [EXCHANGE, CORRESPONDENT]


def test_missing_and_wrong_files_are_rejected(rules_set: Path, tmp_path: Path) -> None:
    (rules_set / CORRESPONDENT).unlink()
    with pytest.raises(Kd2Error, match=CORRESPONDENT):
        collect(rules_set, {})
    explicit = {"exchange": DATA / "registration_rules.xml", "correspondent": rules_set / EXCHANGE}
    with pytest.raises(RulesFormatError, match="ожидаются правила обмена"):
        pack_rules(collect(None, explicit), tmp_path / "x.zip")
    assert not (tmp_path / "x.zip").exists()


def test_inconsistent_parts_give_warnings(rules_set: Path, tmp_path: Path) -> None:
    files = collect(rules_set, {"correspondent": rules_set / EXCHANGE})
    registration = rules_set / REGISTRATION
    registration.write_text(
        registration.read_text("utf-8-sig").replace(">БП</Конфигурация>", ">УТ</Конфигурация>"),
        encoding="utf-8",
    )
    warnings = pack_rules(files, tmp_path / "w.zip").warnings
    assert any("не зеркальны" in item for item in warnings)
    assert any("для конфигурации УТ" in item for item in warnings)


def test_existing_archive_needs_overwrite(rules_set: Path, tmp_path: Path) -> None:
    target = tmp_path / "set.zip"
    pack_rules(collect(rules_set, {}), target)
    with pytest.raises(Kd2Error, match="overwrite"):
        pack_rules(collect(rules_set, {}), target)
    pack_rules(collect(rules_set, {}), target, overwrite=True)


def test_manifest_describes_exact_archive_and_headers(rules_set: Path, tmp_path: Path) -> None:
    before = datetime.now(UTC).replace(microsecond=0)
    result = pack_rules(collect(rules_set, {}), tmp_path / "set.zip")
    after = datetime.now(UTC)
    assert result.manifest == result.path.with_suffix(".manifest.json")
    raw = result.manifest.read_bytes()
    manifest = json.loads(raw)
    assert (
        raw == (json.dumps(manifest, sort_keys=True, ensure_ascii=False, indent=2) + "\n").encode()
    )
    assert b"\r" not in raw
    assert manifest["format"] == "kd2-rules-pack/1"
    assert manifest["server_version"] == version("kd-rules-mcp")
    created = datetime.fromisoformat(manifest["created_at"])
    assert created.utcoffset() == UTC.utcoffset(created) and created.microsecond == 0
    assert before <= created <= after
    assert manifest["archive"] == {
        "file": "set.zip",
        "size_bytes": result.path.stat().st_size,
        "sha256": result.sha256,
    }
    assert [item["role"] for item in manifest["files"]] == [
        "exchange",
        "correspondent",
        "registration",
    ]
    with zipfile.ZipFile(result.path) as archive:
        assert archive.namelist() == [EXCHANGE, CORRESPONDENT, REGISTRATION]
        for item, packed in zip(manifest["files"], result.files, strict=True):
            content = archive.read(item["file"])
            document = load_rules(content)
            assert item["path"] == str(packed.source)
            assert item["size_bytes"] == len(content)
            assert item["sha256"] == base64.b64encode(hashlib.sha256(content).digest()).decode()
            assert item["rules"] == {
                **packed.summary,
                "id": document.root.values["Ид"],
                "created_at": document.root.values["ДатаВремяСоздания"],
                "format_version": "2.01",
                **(
                    {"configuration_version": "3.0"}
                    if item["role"] == "registration"
                    else {
                        "source_version": "3.0" if item["role"] == "exchange" else "3.1",
                        "target_version": "3.1" if item["role"] == "exchange" else "3.0",
                    }
                ),
            }
            assert item["rules_project"] is None
            assert item["rules_project_reason"] == "not_tracked"


@pytest.mark.parametrize("has_manifest", [False, True])
def test_existing_zip_refusal_writes_nothing(
    rules_set: Path, tmp_path: Path, has_manifest: bool
) -> None:
    archive = tmp_path / "set.zip"
    manifest = archive.with_suffix(".manifest.json")
    archive.write_bytes(b"existing archive")
    if has_manifest:
        manifest.write_bytes(b"existing manifest")
    before = {path.name: path.read_bytes() for path in tmp_path.iterdir() if path.is_file()}
    with pytest.raises(Kd2Error, match="overwrite"):
        pack_rules(collect(rules_set, {}), archive)
    assert {path.name: path.read_bytes() for path in tmp_path.iterdir() if path.is_file()} == before


def test_orphan_manifest_requires_overwrite(rules_set: Path, tmp_path: Path) -> None:
    archive = tmp_path / "set.zip"
    manifest = archive.with_suffix(".manifest.json")
    manifest.write_bytes(b"old manifest")
    with pytest.raises(Kd2Error, match="overwrite"):
        pack_rules(collect(rules_set, {}), archive)
    assert not archive.exists() and manifest.read_bytes() == b"old manifest"
    pack_rules(collect(rules_set, {}), archive, overwrite=True)
    assert json.loads(manifest.read_bytes())["archive"]["file"] == archive.name
    manifest.unlink()
    pack_rules(collect(rules_set, {}), archive, overwrite=True)
    assert manifest.is_file()


def _pack(service: Kd2Service, folder: Path, name: str = "set.zip") -> dict[str, Any]:
    return service.rules_pack(str(folder), "", "", "", name, False)


@pytest.mark.parametrize("source_kind", ["xml", "md83exp", "project"])
def test_saved_file_tracks_project_and_structure_fingerprints_after_restart(
    rules_set: Path, tmp_path: Path, source_kind: str
) -> None:
    catalog = tmp_path / "projects.yaml"
    catalog.write_text(
        "projects:\n  alpha:\n    configurations:\n      full:\n        dump: main\n",
        encoding="utf-8",
    )
    settings = Settings(
        cache_dir=tmp_path / "cache",
        workspace=tmp_path / "ws",
        projects_file=catalog,
        project_dirs={"alpha": DATA / "xmldump"} if source_kind == "project" else {},
        rules_dirs={"kit": rules_set},
    )
    service = Kd2Service(settings)
    if source_kind == "md83exp":
        service.structure_load_md83exp("source", str(DATA / "md83exp_small.xml"))
    elif source_kind == "project":
        service.structure_load_project("alpha", "full", "source")
    else:
        service.structure_load_xml("source", str(DATA / "xmldump/main"))
    service.structure_load_md83exp("target", str(DATA / "md83exp_small.xml"))
    source_hash = service.store.meta("source")["input_hash"]
    target_hash = service.store.meta("target")["input_hash"]
    project_id = service.rules_open(str(rules_set / EXCHANGE))["project_id"]
    service.rules_save(
        project_id,
        str(rules_set / EXCHANGE),
        True,
        True,
        source_structure="source",
        target_structure="target",
    )
    # Кэш структуры может измениться; манифест описывает входы последнего сохранения.
    service.structure_load_xml("source", str(DATA / "xmldump/ext"), force=True)
    service = Kd2Service(settings)
    result = _pack(service, rules_set)
    manifest = json.loads(Path(result["manifest"]).read_bytes())
    origin = manifest["files"][0]["rules_project"]
    assert origin["project_id"] == project_id
    expected_source = {
        "structure_id": "source",
        "source": source_kind,
        "input_hash": source_hash,
    }
    if source_kind == "project":
        expected_source |= {"project": "alpha", "configuration": "full"}
    assert origin["structures"] == {
        "source": expected_source,
        "target": {"structure_id": "target", "source": "md83exp", "input_hash": target_hash},
    }
    # Даже корректный XML с изменённым содержимым больше не подтверждён снимком.
    path = rules_set / EXCHANGE
    path.write_bytes(path.read_bytes().replace("БП --&gt; ЗУП".encode(), "Новые правила".encode()))
    changed = json.loads(Path(_pack(service, rules_set, "changed.zip")["manifest"]).read_bytes())
    assert changed["files"][0]["rules_project"] is None
    assert changed["files"][0]["rules_project_reason"] == "changed_since_save"


def test_created_project_keeps_structure_origins_without_validation(
    rules_set: Path, tmp_path: Path
) -> None:
    service = Kd2Service(Settings(cache_dir=tmp_path / "cache", workspace=tmp_path / "ws"))
    service.structure_load_xml("source", str(DATA / "xmldump/main"))
    service.structure_load_xml("target", str(DATA / "xmldump/ext"))
    project_id = service.rules_create("source", "target")["project_id"]
    service.rules_save(project_id, "exchange.xml", False, True)
    result = service.rules_pack(
        str(rules_set), str(service.workspace.root / "exchange.xml"), "", "", "set.zip", False
    )
    origin = json.loads(Path(result["manifest"]).read_bytes())["files"][0]["rules_project"]
    assert origin["project_id"] == project_id
    assert origin["structures"]["source"]["structure_id"] == "source"
    assert origin["structures"]["target"]["structure_id"] == "target"


def test_old_snapshot_without_saved_provenance_is_not_tracked(
    rules_set: Path, tmp_path: Path
) -> None:
    settings = Settings(cache_dir=tmp_path / "cache", workspace=tmp_path / "ws")
    service = Kd2Service(settings)
    project_id = service.rules_open(str(rules_set / EXCHANGE))["project_id"]
    service.rules_save(project_id, "exchange.xml", False, True)
    meta_path = settings.workspace / ".projects" / project_id / "meta.json"
    meta = json.loads(meta_path.read_bytes())
    meta.pop("saved_sha256")
    meta.pop("saved_structures")
    meta_path.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    service = Kd2Service(settings)
    project = service.workspace.get(project_id)
    assert project.document is not None and project.saved_sha256 is None
    assert project.saved_structures == {}
    result = service.rules_pack(
        str(rules_set), str(settings.workspace / "exchange.xml"), "", "", "set.zip", False
    )
    item = json.loads(Path(result["manifest"]).read_bytes())["files"][0]
    assert item["rules_project"] is None and item["rules_project_reason"] == "not_tracked"


def test_only_last_saved_path_is_tracked(rules_set: Path, tmp_path: Path) -> None:
    service = Kd2Service(Settings(cache_dir=tmp_path / "cache", workspace=tmp_path / "ws"))
    project_id = service.rules_open(str(rules_set / EXCHANGE))["project_id"]
    opened = json.loads(Path(_pack(service, rules_set, "opened.zip")["manifest"]).read_bytes())
    assert opened["files"][0]["rules_project_reason"] == "not_tracked"
    service.rules_save(project_id, "first.xml", False, True)
    service.rules_save(project_id, "last.xml", False, True)
    assert (service.workspace.root / "first.xml").read_bytes() == (
        service.workspace.root / "last.xml"
    ).read_bytes()
    result = service.rules_pack(
        str(rules_set), str(service.workspace.root / "first.xml"), "", "", "old.zip", False
    )
    old = json.loads(Path(result["manifest"]).read_bytes())["files"][0]
    assert old["rules_project"] is None and old["rules_project_reason"] == "not_tracked"


def test_manifest_paths_use_agent_path_mapping(rules_set: Path, tmp_path: Path) -> None:
    agent = "Z:/Agent"
    service = Kd2Service(
        Settings(
            cache_dir=tmp_path / "cache",
            workspace=tmp_path / "ws",
            path_map=PathMap.parse(f"{agent}={tmp_path.resolve()}"),
        )
    )
    result = _pack(service, rules_set)
    assert result["manifest"] == service._host(service.workspace.root / "set.manifest.json")
    manifest = json.loads((service.workspace.root / "set.manifest.json").read_bytes())
    assert [item["path"] for item in manifest["files"]] == [
        item["path"] for item in result["files"]
    ]
    assert all(item["path"].replace("\\", "/").startswith(agent) for item in manifest["files"])


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


async def _call(client: Client, tool: str, /, **arguments: Any) -> dict[str, Any]:
    result = await client.call_tool(tool, arguments)
    assert not result.is_error, result.content
    assert result.structured_content is not None
    return result.structured_content


@pytest.mark.anyio
async def test_tool_packs_folder_into_workspace_or_rules_dir(
    rules_set: Path, tmp_path: Path
) -> None:
    rules_dir = rules_set.parent
    settings = Settings(
        cache_dir=tmp_path / "cache", workspace=tmp_path / "ws", rules_dirs={"alpha": rules_dir}
    )
    async with Client(create_server(Kd2Service(settings))) as client:
        default = await _call(client, "rules_pack", folder=str(rules_set))
        beside = await _call(
            client, "rules_pack", folder=str(rules_set), path=str(rules_dir / "ОбменЗУП.zip")
        )
        rejected = await client.call_tool(
            "rules_pack", {"folder": str(rules_set), "path": str(tmp_path / "x.zip")}
        )
    assert default["path"] == str((tmp_path / "ws" / "ОбменЗУП.zip").resolve())
    assert default["manifest"] == str((tmp_path / "ws" / "ОбменЗУП.manifest.json").resolve())
    assert set(default) == {
        "path",
        "manifest",
        "size_bytes",
        "sha256",
        "load_with",
        "files",
        "warnings",
    }
    assert default["load_with"] == FORM_SET
    assert [item["file"] for item in default["files"]] == [EXCHANGE, CORRESPONDENT, REGISTRATION]
    assert default["files"][0]["path"] == str((rules_set / EXCHANGE).resolve())
    assert default["files"][0]["rules"]["source"] == "БП"
    assert default["files"][2]["rules"]["exchange_plan"] == "ОбменЗУП"
    # Хеши — base64 SHA-256, как `hash` правил у live_rules_dump/live_status.
    archive_bytes = (tmp_path / "ws" / "ОбменЗУП.zip").read_bytes()
    assert default["sha256"] == base64.b64encode(hashlib.sha256(archive_bytes).digest()).decode()
    with zipfile.ZipFile(tmp_path / "ws" / "ОбменЗУП.zip") as archive:
        for item in default["files"]:
            packed = archive.read(item["file"])
            assert packed == (rules_set / item["file"]).read_bytes()
            assert item["sha256"] == base64.b64encode(hashlib.sha256(packed).digest()).decode()
    assert len({item["sha256"] for item in default["files"]}) == 3
    assert (rules_dir / "ОбменЗУП.zip").is_file() and beside["warnings"] == []
    assert rejected.is_error
    text = "".join(getattr(item, "text", "") for item in rejected.content)
    assert json.loads(text[text.index("{") :])["code"] == "path_outside_workspace"
