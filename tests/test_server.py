"""Сервер MCP через клиент MCP (спецификация `mcp-service`): каждый инструмент вызывается."""

import json
import shutil
from pathlib import Path
from typing import Any

import pytest
from mcp import Client

from kd2_rules_mcp.server import create_server
from kd2_rules_mcp.service import Kd2Service, PathMap, Settings

DATA = Path(__file__).parent / "data"
DUMP = DATA / "xmldump" / "main"

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def service(tmp_path: Path) -> Kd2Service:
    return Kd2Service(Settings(cache_dir=tmp_path / "cache", workspace=tmp_path / "workspace"))


async def _call(client: Client, tool: str, /, **arguments: Any) -> dict[str, Any]:
    result = await client.call_tool(tool, arguments)
    assert not result.is_error, f"{tool}: {result.content}"
    assert result.structured_content is not None, tool
    return result.structured_content


async def _error(client: Client, tool: str, /, **arguments: Any) -> dict[str, Any]:
    result = await client.call_tool(tool, arguments)
    assert result.is_error, tool
    text = "".join(getattr(item, "text", "") for item in result.content)
    # SDK добавляет «Error executing tool <имя>: » перед текстом ошибки.
    return json.loads(text[text.index("{") :])


EXPECTED_TOOLS = {
    "project_list",
    "structure_load_project",
    "structure_list",
    "structure_load_xml",
    "structure_load_md83exp",
    "structure_objects",
    "structure_object",
    "structure_values",
    "structure_plan_content",
    "structure_compare",
    "match_objects",
    "match_properties",
    "match_values",
    "rules_open",
    "rules_create",
    "rules_projects",
    "rules_overview",
    "rules_list",
    "rules_get",
    "rules_save",
    "rule_create",
    "rule_update",
    "rule_delete",
    "pko_create_from_candidates",
    "rules_validate",
    "handlers_export",
    "handlers_locate",
    "registration_build",
    "correspondent_draft",
}


async def test_tools_have_descriptions_and_parameter_schemas(service: Kd2Service) -> None:
    async with Client(create_server(service)) as client:
        tools = (await client.list_tools()).tools
    assert {tool.name for tool in tools} == EXPECTED_TOOLS
    for tool in tools:
        assert tool.description, tool.name
        properties = tool.input_schema.get("properties", {})
        for name, schema in properties.items():
            assert schema.get("description"), f"{tool.name}.{name} без описания"


async def test_structure_tools(service: Kd2Service) -> None:
    async with Client(create_server(service)) as client:
        loaded = await _call(
            client, "structure_load_xml", structure_id="dump", configuration_path=str(DUMP)
        )
        assert loaded["reused"] is False and loaded["counts"]["objects"] > 0
        again = await _call(
            client, "structure_load_xml", structure_id="dump", configuration_path=str(DUMP)
        )
        assert again["reused"] is True
        forced = await _call(
            client,
            "structure_load_xml",
            structure_id="dump",
            configuration_path=str(DUMP),
            force=True,
        )
        assert forced["reused"] is False
        listed = await _call(client, "structure_list")
        assert [item["structure_id"] for item in listed["structures"]] == ["dump"]
        objects = await _call(client, "structure_objects", structure_id="dump", kind="Справочник")
        assert "Справочник.Контрагенты" in [item["name"] for item in objects["items"]]
        described = await _call(
            client, "structure_object", structure_id="dump", name="Справочник.Контрагенты", limit=5
        )
        assert described["properties"]["limit"] == 5
        values = await _call(
            client, "structure_values", structure_id="dump", name="Перечисление.Виды"
        )
        assert values["total"] > 0
        content = await _call(
            client, "structure_plan_content", structure_id="dump", exchange_plan="ПланОбмена.Обмен"
        )
        assert content["total"] > 0
        diff = await _call(client, "structure_compare", old_structure="dump", new_structure="dump")
        assert set(diff["counts"].values()) == {0}
        matched = await _call(
            client, "match_objects", source_structure="dump", target_structure="dump", limit=200
        )
        assert {item["confidence"] for item in matched["items"]} == {"точно"}
        assert all(isinstance(item["source"], str) for item in matched["items"])
        filtered = await _call(
            client,
            "match_objects",
            source_structure="dump",
            target_structure="dump",
            text="контраг",
        )
        assert [item["target"] for item in filtered["items"]] == ["Справочник.Контрагенты"]
        properties = await _call(
            client,
            "match_properties",
            source_structure="dump",
            target_structure="dump",
            source_object="Справочник.Контрагенты",
            target_object="Справочник.Контрагенты",
        )
        assert properties["total"] > 0
        enum_values = await _call(
            client,
            "match_values",
            source_structure="dump",
            target_structure="dump",
            source_object="Перечисление.Виды",
            target_object="Перечисление.Виды",
        )
        assert enum_values["total"] == values["total"]


async def test_md83exp_load(service: Kd2Service) -> None:
    async with Client(create_server(service)) as client:
        loaded = await _call(
            client,
            "structure_load_md83exp",
            structure_id="small",
            path=str(DATA / "md83exp_small.xml"),
        )
    assert loaded["counts"]["objects"] > 0


async def test_rules_tools(service: Kd2Service) -> None:
    async with Client(create_server(service)) as client:
        await _call(client, "structure_load_xml", structure_id="dump", configuration_path=str(DUMP))
        opened = await _call(client, "rules_open", path=str(DATA / "exchange_rules.xml"))
        project = opened["project_id"]
        assert opened["kind"] == "exchange" and opened["counts"]["pko"] > 0
        assert (await _call(client, "rules_projects"))["projects"][0]["project_id"] == project
        overview = await _call(client, "rules_overview", project_id=project)
        assert overview["source"] == "БП"
        listed = await _call(client, "rules_list", project_id=project, section="pko")
        assert "Организации" in [row["code"] for row in listed["items"]]
        rule = await _call(client, "rules_get", project_id=project, kind="pko", key="Организации")
        assert rule["fields"]["Источник"] == "СправочникСсылка.Организации"

        updated = await _call(
            client,
            "rule_update",
            project_id=project,
            kind="pko",
            key="Организации",
            fields={"Наименование": "Организации (правка)"},
        )
        assert updated["address"] == "ПКО «Организации»"
        await _call(
            client,
            "rule_create",
            project_id=project,
            kind="algorithm",
            key="Новый",
            fields={"Текст": "Возврат 2;"},
        )
        await _call(client, "rule_delete", project_id=project, kind="algorithm", key="Новый")

        validated = await _call(client, "rules_validate", project_id=project)
        assert "summary" in validated and validated["issues"]["offset"] == 0
        exported = await _call(client, "handlers_export", project_id=project, folder="bsl")
        assert exported["count"] > 0
        first = exported["files"][0]["file"]
        located = await _call(
            client, "handlers_locate", project_id=project, file_name=first, line=1
        )
        assert located["found"] is False  # строка 1 — объявление переменной модуля

        draft = await _call(
            client, "correspondent_draft", project_id=project, codes=["Организации"]
        )
        assert draft["draft"] is True and draft["project_id"] != project
        saved = await _call(client, "rules_save", project_id=project, path="out/rules.xml")
        assert Path(saved["path"]).is_file()
        assert saved["counts"]["pko"] == opened["counts"]["pko"]
        assert "Правило" not in json.dumps(saved, ensure_ascii=False)

        created = await _call(
            client, "rules_create", source_structure="dump", target_structure="dump"
        )
        new_project = created["project_id"]
        pko = await _call(
            client,
            "pko_create_from_candidates",
            project_id=new_project,
            code="Контрагенты",
            source_structure="dump",
            target_structure="dump",
            source_object="Справочник.Контрагенты",
            target_object="Справочник.Контрагенты",
        )
        assert pko["address"] == "ПКО «Контрагенты»"
        registration = await _call(
            client,
            "registration_build",
            structure_id="dump",
            exchange_plan="Обмен",
            objects=[{"metadata_name": "Справочник.Контрагенты"}],
        )
        assert registration["kind"] == "registration"
        assert registration["counts"]["registration_rules"] == 1


async def test_unknown_structure_lists_loaded(service: Kd2Service) -> None:
    async with Client(create_server(service)) as client:
        await _call(client, "structure_load_xml", structure_id="dump", configuration_path=str(DUMP))
        error = await _error(client, "structure_objects", structure_id="missing")
    assert error["code"] == "structure_not_found"
    assert error["structures"] == ["dump"]


async def test_missing_object_has_suggestions(service: Kd2Service) -> None:
    async with Client(create_server(service)) as client:
        await _call(client, "structure_load_xml", structure_id="dump", configuration_path=str(DUMP))
        error = await _error(
            client, "structure_object", structure_id="dump", name="Справочник.Контрагент"
        )
    assert error["code"] == "object_not_found"
    assert "Справочник.Контрагенты" in error["suggestions"]


async def test_write_into_project_is_rejected(service: Kd2Service, tmp_path: Path) -> None:
    project_dir = tmp_path / "Проект"
    project_dir.mkdir()
    async with Client(create_server(service)) as client:
        opened = await _call(client, "rules_open", path=str(DATA / "exchange_rules.xml"))
        target = project_dir / "Template.txt"
        error = await _error(
            client, "rules_save", project_id=opened["project_id"], path=str(target)
        )
        assert error["code"] == "path_outside_workspace"
        assert error["workspace"] == str(service.workspace.root.resolve())
        assert not target.exists()
        missing = await _error(client, "rules_overview", project_id="99")
        assert missing["code"] == "project_not_found"


async def test_path_map_translates_agent_paths(tmp_path: Path) -> None:
    mounted = tmp_path / "projects"
    shutil.copytree(DATA, mounted / "data")
    workspace = tmp_path / "ws"
    path_map = PathMap.parse(rf"D:\Work\Проекты={mounted};D:\Work\Проекты\КД\workspace={workspace}")
    service = Kd2Service(
        Settings(cache_dir=tmp_path / "cache", workspace=workspace, path_map=path_map)
    )
    async with Client(create_server(service)) as client:
        opened = await _call(client, "rules_open", path=r"D:\Work\проекты\data\exchange_rules.xml")
        saved = await _call(
            client,
            "rules_save",
            project_id=opened["project_id"],
            path=r"D:\Work\Проекты\КД\workspace\x.xml",
        )
        rejected = await _error(
            client,
            "rules_save",
            project_id=opened["project_id"],
            path=r"D:\Work\Проекты\data\x.xml",
        )
    assert saved["path"] == r"D:\Work\Проекты\КД\workspace\x.xml"
    assert (workspace / "x.xml").is_file()
    assert rejected["code"] == "path_outside_workspace"
    assert rejected["workspace"] == r"D:\Work\Проекты\КД\workspace"
    assert not (mounted / "data" / "x.xml").exists()
