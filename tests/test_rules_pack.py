"""`rules_pack`: ZIP правил для загрузки в БСП (задача #1) — без баз 1С."""

import json
import zipfile
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
from kd_rules_mcp.server import create_server
from kd_rules_mcp.service import Kd2Service, Settings

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
    assert default["load_with"] == FORM_SET
    assert [item["file"] for item in default["files"]] == [EXCHANGE, CORRESPONDENT, REGISTRATION]
    assert default["files"][0]["path"] == str((rules_set / EXCHANGE).resolve())
    assert default["files"][0]["rules"]["source"] == "БП"
    assert default["files"][2]["rules"]["exchange_plan"] == "ОбменЗУП"
    assert (rules_dir / "ОбменЗУП.zip").is_file() and beside["warnings"] == []
    assert rejected.is_error
    text = "".join(getattr(item, "text", "") for item in rejected.content)
    assert json.loads(text[text.index("{") :])["code"] == "path_outside_workspace"
