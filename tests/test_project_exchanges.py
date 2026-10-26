"""Шаги обмена — готовый контракт инструментов, без исполнения и сетевых вызовов."""

from pathlib import Path

import pytest
import yaml
from mcp import Client

from kd_rules_mcp.projects import ProjectConfigError
from kd_rules_mcp.server import create_server
from kd_rules_mcp.service import Kd2Service, PathMap, Settings


def prepare(tmp_path: Path, *, technology: str | None = "kd2", legacy: bool = False) -> Kd2Service:
    projects = {}
    sides = []
    for name in ("bp", "do"):
        folder = tmp_path / name
        rules = folder / "rules" / "План"
        rules.mkdir(parents=True)
        for filename in (
            "ExchangeRules.xml",
            "CorrespondentExchangeRules.xml",
            "RegistrationRules.xml",
        ):
            (rules / filename).write_text("test", encoding="utf-8")
        projects[name] = {
            "name": name,
            "configurations": {"full": {"dump": "main"}},
            "bases": {
                "dev": {
                    "role": "песочница",
                    "configuration": "full",
                    "connection": f'Ref="{name}";',
                    "data_mcp": "data",
                }
            },
        }
        side = {"project": name, "base": "dev", "plan": f"План{name}"}
        side.update({"module": f"Manager{name}"} if technology == "ed" else {"rules": "rules/План"})
        sides.append(side)
    exchange: dict = {"plan": "План", "projects": ["bp", "do"]}
    if not legacy:
        exchange.update({"id": "bp-do", "technology": technology, "sides": sides})
    path = tmp_path / "projects.yaml"
    path.write_text(
        yaml.safe_dump({"projects": projects, "exchanges": [exchange]}, allow_unicode=True),
        encoding="utf-8",
    )
    settings = Settings(
        projects_file=path,
        workspace=tmp_path / "ws",
        cache_dir=tmp_path / "cache",
        project_dirs={name: tmp_path / name for name in projects},
        path_map=PathMap.parse(f"D:/Projects={tmp_path}"),
    )
    return Kd2Service(settings)


def view(service: Kd2Service) -> dict:
    return service.project_exchanges()["exchanges"][0]


def test_kd2_sandbox_steps_and_fill(tmp_path: Path) -> None:
    service = prepare(tmp_path)
    exchange = view(service)
    steps = exchange["steps"]
    assert [s["n"] for s in steps] == list(range(1, len(steps) + 1))
    tools = [s["tool"] for s in steps]
    assert tools.count("structure_load_project") == 2
    assert tools.count("rules_open") == 4
    assert tools.count("rules_validate") == 4
    assert tools.count("rules_diff") == 2
    for tool in ("live_base_check", "live_status", "live_rules_dump", "live_rules_check"):
        assert tools.count(tool) == 2
    for step in steps:
        if step["tool"] == "rules_validate":
            assert steps[step["fill"]["project_id"] - 1]["tool"] == "rules_open"
        if step["tool"] == "live_base_check":
            assert step["arguments"]["exchange"] == "bp-do"
        if step["tool"] == "live_rules_check":
            assert step["arguments"]["files"].replace("\\", "/").startswith("D:/Projects/")
    forward = exchange["directions"][0]
    assert forward["source"] == "bp" and forward["target"] == "do"
    assert "/bp/" in forward["export"].replace("\\", "/")
    assert "/do/" in forward["import"].replace("\\", "/")
    assert "CorrespondentExchangeRules.xml" in forward["import"]
    assert all(s["structure_loaded"] is False for s in exchange["sides"])
    assert any("live_exchange_run" in n for n in exchange["notes"])


def test_production_side_has_no_live_steps(tmp_path: Path) -> None:
    service = prepare(tmp_path)
    path = service.settings.projects_file
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    data["projects"]["do"]["bases"]["dev"]["role"] = "боевая"
    path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    exchange = view(service)
    assert all(s["arguments"].get("base") != "do.dev" for s in exchange["steps"])
    assert any("боевая" in n and "do.dev" in n for n in exchange["notes"])


def test_missing_registration_file_is_not_opened(tmp_path: Path) -> None:
    service = prepare(tmp_path)
    (tmp_path / "bp/rules/План/RegistrationRules.xml").unlink()
    exchange = view(service)
    file = exchange["sides"][0]["rules"]["files"]["registration"]
    assert file["exists"] is False
    assert not any(s["arguments"].get("path") == file["path"] for s in exchange["steps"])


def test_legacy_only_loads_structures(tmp_path: Path) -> None:
    exchange = view(prepare(tmp_path, legacy=True))
    assert [s["tool"] for s in exchange["steps"]] == ["structure_load_project"] * 2
    assert all(s["base"] is None and "rules" not in s for s in exchange["sides"])


def test_ed_opens_module_and_validates_side(tmp_path: Path) -> None:
    exchange = view(prepare(tmp_path, technology="ed"))
    opened = [s for s in exchange["steps"] if s["tool"] == "ed_open"]
    assert len(opened) == 2
    assert opened[0]["arguments"] == {
        "project": "bp",
        "module": "Managerbp",
        "configuration": "full",
    }
    checks = [s for s in exchange["steps"] if s["tool"] == "ed_validate"]
    assert checks[0]["arguments"] == {"structure_id": "bp-full"}
    assert checks[0]["fill"] == {"project_id": opened[0]["n"]}


def test_unspecified_technology_only_structure_and_base_check(tmp_path: Path) -> None:
    exchange = view(prepare(tmp_path, technology=None))
    assert {s["tool"] for s in exchange["steps"]} == {"structure_load_project", "live_base_check"}
    assert any("Технологию" in n for n in exchange["notes"])


def test_filters_and_unknown_ids(tmp_path: Path) -> None:
    service = prepare(tmp_path)
    expected = service.project_exchanges()
    assert service.project_exchanges(project="bp", exchange="bp-do") == expected
    for kwargs, known in (({"project": "none"}, "bp, do"), ({"exchange": "none"}, "bp-do")):
        with pytest.raises(ProjectConfigError, match=known):
            service.project_exchanges(**kwargs)
    path = service.settings.projects_file
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    data["projects"]["third"] = {"name": "third", "configurations": {"full": {"dump": "main"}}}
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    assert service.project_exchanges(project="third") == {"exchanges": []}


def test_invisible_project_preserves_profile_paths(tmp_path: Path) -> None:
    service = prepare(tmp_path)
    service.settings.project_dirs.clear()
    exchange = view(service)
    assert exchange["sides"][0]["rules"]["folder"] == "rules/План"
    assert exchange["sides"][0]["rules"]["files"]["exchange"] == {
        "path": "rules/План/ExchangeRules.xml",
        "exists": None,
    }
    # Путь от папки проекта инструменты не примут: шагов с ним нет, заметка называет причину.
    assert not any(s["tool"] in ("rules_open", "live_rules_check") for s in exchange["steps"])
    assert any("не видна серверу" in n and "bp" in n for n in exchange["notes"])


def test_receiver_only_side_opens_nothing(tmp_path: Path) -> None:
    """Сторона только приёмник: её правила обмена и регистрации в направлении не работают."""
    service = prepare(tmp_path)
    path = service.settings.projects_file
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    data["exchanges"][0]["directions"] = [["bp", "do"]]
    path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    exchange = view(service)
    opened = [
        s["arguments"]["path"].replace("\\", "/")
        for s in exchange["steps"]
        if s["tool"] == "rules_open"
    ]
    assert len(opened) == 2 and all("/bp/" in p for p in opened)
    assert [s["tool"] for s in exchange["steps"]].count("rules_diff") == 1


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.anyio
@pytest.mark.parametrize("technology", ["kd2", "ed", None])
async def test_step_argument_names_match_server_tools(
    tmp_path: Path, technology: str | None
) -> None:
    service = prepare(tmp_path, technology=technology)
    async with Client(create_server(service)) as client:
        tools = {tool.name: tool.input_schema for tool in (await client.list_tools()).tools}
        response = (
            await client.call_tool("project_exchanges", {"exchange": "bp-do"})
        ).structured_content
    assert response is not None
    for step in response["exchanges"][0]["steps"]:
        schema = tools[step["tool"]]
        provided = step["arguments"].keys() | step.get("fill", {}).keys()
        assert provided <= schema["properties"].keys(), step
        assert set(schema.get("required", [])) <= provided, step
