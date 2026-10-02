"""Сервер MCP через клиент MCP (спецификация `mcp-service`): каждый инструмент вызывается."""

import json
import logging
import shutil
from pathlib import Path
from typing import Any

import pytest
from mcp import Client

from kd2_rules_mcp.errors import DuplicateRuleError, RuleNotFoundError
from kd2_rules_mcp.kd2.model import ExchangeRules
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
    "rules_close",
    "rules_pack",
    "rule_create",
    "rule_update",
    "rule_delete",
    "pko_create_from_candidates",
    "rules_validate",
    "rules_diff",
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


async def test_rules_project_modified_tracks_edits_and_save(service: Kd2Service) -> None:
    """`modified` ложен после открытия и сохранения и истинен после правки."""
    async with Client(create_server(service)) as client:
        opened = await _call(client, "rules_open", path=str(DATA / "exchange_rules.xml"))
        project = opened["project_id"]
        listed = await _call(client, "rules_projects")
        assert listed["projects"][0]["modified"] is False
        await _call(
            client,
            "rule_update",
            project_id=project,
            kind="pko",
            key="Организации",
            fields={"Наименование": "Организации (правка)"},
        )
        assert (await _call(client, "rules_projects"))["projects"][0]["modified"] is True
        assert (await _call(client, "rules_overview", project_id=project))["modified"] is True
        await _call(client, "rules_save", project_id=project, path="out/rules.xml")
        assert (await _call(client, "rules_projects"))["projects"][0]["modified"] is False
        assert (await _call(client, "rules_overview", project_id=project))["modified"] is False


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
        assert exported["removed"] == 0
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


async def test_relative_backslash_folder_is_a_subdirectory(service: Kd2Service) -> None:
    """`handlers_export(folder='out\\\\handlers')` создаёт каталог `out/handlers`."""
    async with Client(create_server(service)) as client:
        opened = await _call(client, "rules_open", path=str(DATA / "exchange_rules.xml"))
        exported = await _call(
            client, "handlers_export", project_id=opened["project_id"], folder="out\\handlers"
        )
    folder = Path(exported["folder"])
    root = service.workspace.root.resolve()
    assert folder.relative_to(root).parts == ("out", "handlers")
    assert "\\" not in folder.name
    assert folder.is_dir()


def _info_lines(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [
        record.getMessage()
        for record in caplog.records
        if record.name == "kd2_rules_mcp" and record.levelno == logging.INFO
    ]


async def test_unexpected_exception_is_internal(
    service: Kd2Service, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Исключение вне Kd2Error и ValueError — JSON code internal и трассировка в логе."""

    def project_list() -> dict[str, Any]:
        raise RuntimeError("сбой")

    # Имя функции — имя инструмента: call пишет function.__name__.
    monkeypatch.setattr(service, "project_list", project_list)
    with caplog.at_level(logging.DEBUG, logger="kd2_rules_mcp"):
        async with Client(create_server(service)) as client:
            error = await _error(client, "project_list")
    assert error["code"] == "internal"
    assert "сбой" in error["message"]
    errors = [
        record
        for record in caplog.records
        if record.name == "kd2_rules_mcp" and record.levelno == logging.ERROR
    ]
    assert len(errors) == 1
    assert errors[0].exc_info is not None
    assert errors[0].exc_info[0] is RuntimeError
    assert "project_list" in errors[0].getMessage()


async def test_tool_calls_are_logged(service: Kd2Service, caplog: pytest.LogCaptureFixture) -> None:
    """Успех и project_not_found дают по строке INFO с именем инструмента и итогом."""
    with caplog.at_level(logging.DEBUG, logger="kd2_rules_mcp"):
        async with Client(create_server(service)) as client:
            await _call(client, "structure_list")
            error = await _error(client, "rules_overview", project_id="нет")
    assert error["code"] == "project_not_found"
    messages = _info_lines(caplog)
    assert any(line.startswith("structure_list ") and line.endswith(" ok") for line in messages)
    assert any(
        line.startswith("rules_overview ") and line.endswith(" project_not_found")
        for line in messages
    )
    assert not any(
        record.name == "kd2_rules_mcp" and record.levelno >= logging.ERROR
        for record in caplog.records
    )


async def test_pko_unknown_object_is_object_not_found(service: Kd2Service) -> None:
    async with Client(create_server(service)) as client:
        await _call(client, "structure_load_xml", structure_id="dump", configuration_path=str(DUMP))
        created = await _call(
            client, "rules_create", source_structure="dump", target_structure="dump"
        )
        error = await _error(
            client,
            "pko_create_from_candidates",
            project_id=created["project_id"],
            code="Нет",
            source_structure="dump",
            target_structure="dump",
            source_object="Справочник.Контрагент",
            target_object="Справочник.Контрагенты",
        )
    assert error["code"] == "object_not_found"
    assert "suggestions" in error
    assert "Справочник.Контрагенты" in error["suggestions"]


async def test_rule_create_puts_rule_into_group(service: Kd2Service) -> None:
    """`rules_get` и `rules_list` группу не отдают — повтор того же кода даёт duplicate_rule."""
    opened = service.rules_open(str(DATA / "exchange_rules.xml"))
    project = opened["project_id"]
    created = service.rule_create(
        project,
        "pko",
        "Контрагенты",
        fields={"Наименование": "Справочник: Контрагенты"},
        group="Справочники",
    )
    assert created["address"] == "ПКО «Контрагенты»"
    listed = service.rules_list(project, "pko", None, 0, 50)
    assert "Контрагенты" in [row["code"] for row in listed["items"]]
    got = service.rules_get(project, "pko", "Контрагенты", "", 50)
    assert got["fields"]["Наименование"] == "Справочник: Контрагенты"

    document = service.workspace.get(project).document
    assert isinstance(document, ExchangeRules)
    section = document.root.children["ПравилаКонвертацииОбъектов"]
    catalogs = next(item for item in section.items if item.is_group and item.code == "Справочники")
    assert any(item.code == "Контрагенты" and not item.is_group for item in catalogs.items)
    assert all(item.code != "Контрагенты" for item in section.items)

    with pytest.raises(DuplicateRuleError, match="Контрагенты"):
        service.rule_create(project, "pko", "Контрагенты")


async def test_rules_open_reused_and_duplicate_project_id(service: Kd2Service) -> None:
    """Повторный rules_open — reused; занятый project_id — duplicate_project."""
    async with Client(create_server(service)) as client:
        await _call(client, "structure_load_xml", structure_id="dump", configuration_path=str(DUMP))
        first = await _call(client, "rules_open", path=str(DATA / "exchange_rules.xml"))
        second = await _call(client, "rules_open", path=str(DATA / "exchange_rules.xml"))
        assert first["reused"] is False
        assert "source_changed" not in first
        assert second["reused"] is True
        assert second["source_changed"] is False
        assert second["project_id"] == first["project_id"]
        listed = await _call(client, "rules_projects")
        assert [item["project_id"] for item in listed["projects"]] == [first["project_id"]]

        created = await _call(
            client,
            "rules_create",
            source_structure="dump",
            target_structure="dump",
            project_id="bp-zup-new",
        )
        assert created["project_id"] == "bp-zup-new"
        error = await _error(
            client,
            "rules_create",
            source_structure="dump",
            target_structure="dump",
            project_id="bp-zup-new",
        )
        assert error["code"] == "duplicate_project"
        assert "bp-zup-new" in error["message"]
        assert first["project_id"] in error["message"]


def test_restart_restores_edits_handlers_and_close(service: Kd2Service) -> None:
    """Новый Kd2Service видит правки и карту строк; rules_close удаляет только снимок."""
    opened = service.rules_open(str(DATA / "exchange_rules.xml"))
    project_id = opened["project_id"]
    service.rule_update(project_id, "pko", "Организации", fields={"Наименование": "После снимка"})
    exported = service.handlers_export(project_id, "bsl", 50)
    file_name = exported["files"][0]["file"]
    located = service.handlers_locate(project_id, file_name, 1)
    assert located["found"] is False

    restored = Kd2Service(service.settings)
    listed = [item["project_id"] for item in restored.rules_projects()["projects"]]
    assert listed == [project_id]
    got = restored.rules_get(project_id, "pko", "Организации", "", 20)
    assert got["fields"]["Наименование"] == "После снимка"
    assert restored.handlers_locate(project_id, file_name, 1) == located

    saved = restored.rules_save(project_id, "kept.xml", True)
    closed = restored.rules_close(project_id)
    assert closed == {"project_id": project_id, "closed": True, "snapshot_removed": True}
    assert restored.rules_projects()["projects"] == []
    assert Path(saved["path"]).is_file()
    assert not (service.settings.workspace / ".projects" / project_id).exists()


async def test_pko_create_from_candidates_requires_existing_group(service: Kd2Service) -> None:
    service.structure_load_xml("dump", str(DUMP))
    created = service.rules_create("dump", "dump")
    with pytest.raises(RuleNotFoundError, match="Группа «Справочники»"):
        service.pko_create_from_candidates(
            created["project_id"],
            "Контрагенты",
            "dump",
            "dump",
            "Справочник.Контрагенты",
            "Справочник.Контрагенты",
            None,
            group="Справочники",
        )


async def test_rules_diff_project_against_file_and_errors(service: Kd2Service) -> None:
    """Правка проекта против исходного файла; чужая сторона и разные виды — отказ."""
    source = str(DATA / "exchange_rules.xml")
    missing = r"C:\kd2-rules-missing\no.xml"
    async with Client(create_server(service)) as client:
        opened = await _call(client, "rules_open", path=source)
        project = opened["project_id"]
        await _call(
            client,
            "rule_update",
            project_id=project,
            kind="pko",
            key="Организации",
            fields={"Наименование": "Организации (правка)"},
        )
        diff = await _call(client, "rules_diff", left=source, right=project)
        assert diff["left"]["path"] == source
        assert diff["right"] == {"project_id": project}
        assert diff["kind"] == "exchange"
        assert diff["ignored_fields"] == ["ДатаВремяСоздания", "Ид"]
        assert diff["summary"] == {"pko": {"added": 0, "removed": 0, "changed": 1}}
        assert diff["changes"]["total"] == 1
        change = diff["changes"]["items"][0]
        assert change["section"] == "pko"
        assert change["address"] == "ПКО «Организации»"
        assert change["change"] == "changed"
        assert change["field"] == "Наименование"
        assert change["new"] == "Организации (правка)"

        unknown = await _error(client, "rules_diff", left=missing, right=source)
        opened_missing = await _error(client, "rules_open", path=missing)
        assert unknown["code"] == opened_missing["code"]
        assert unknown["code"] in {"project_not_found", "rejected"}

        registration = await _call(client, "rules_open", path=str(DATA / "registration_rules.xml"))
        mismatch = await _error(
            client, "rules_diff", left=project, right=registration["project_id"]
        )
        assert mismatch["code"] == "rejected"
        assert "правила обмена" in mismatch["message"]
        assert "правила регистрации" in mismatch["message"]


async def test_rules_diff_pages_large_result(service: Kd2Service) -> None:
    """Изменений больше страницы: total, has_more и вторая страница по offset."""
    source = str(DATA / "exchange_rules.xml")
    async with Client(create_server(service)) as client:
        opened = await _call(client, "rules_open", path=source)
        project = opened["project_id"]
        await _call(
            client,
            "rule_update",
            project_id=project,
            kind="pko",
            key="Организации",
            fields={"Наименование": "Новое имя", "Комментарий": "Новый комментарий"},
        )
        first = await _call(client, "rules_diff", left=project, right=source, limit=1)
        assert first["changes"]["total"] == 2
        assert first["changes"]["has_more"] is True
        assert len(first["changes"]["items"]) == 1
        second = await _call(client, "rules_diff", left=project, right=source, limit=1, offset=1)
        assert second["changes"]["has_more"] is False
        assert second["changes"]["total"] == 2
        assert second["changes"]["items"][0]["field"] != first["changes"]["items"][0]["field"]
        assert {first["changes"]["items"][0]["field"], second["changes"]["items"][0]["field"]} == {
            "Наименование",
            "Комментарий",
        }
