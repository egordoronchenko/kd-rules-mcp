"""Ответы сервиса по проектам правил: приватная копия rules_open."""

from pathlib import Path

import pytest

from kd_rules_mcp.errors import Kd2Error
from kd_rules_mcp.service import Kd2Service, PathMap, Settings

DATA = Path(__file__).parent / "data"


def test_migration_rules_get_property_rows_show_code_parameter_and_incoming(tmp_path):
    service = Kd2Service(Settings(cache_dir=tmp_path / "cache", workspace=tmp_path / "workspace"))
    opened = service.rules_open(str(DATA / "exchange_rules.xml"))
    document = service._exchange(opened["project_id"])
    pko = document.pko()[0]
    properties = pko.child("Свойства")
    assert properties is not None
    prop = properties.items[0]
    prop.values["ПередВыгрузкой"] = 'Значение = "Расчетный";'
    prop.values["ПриВыгрузке"] = "ИсходящиеДанные = Параметры.Банк;"
    prop.values["ПослеВыгрузки"] = "   "
    prop.values["ИмяПараметраДляПередачи"] = "Банк"
    prop.values["ПолучитьИзВходящихДанных"] = True
    result = service.rules_get(opened["project_id"], "pko", pko.code, "", 100)
    row = result["properties"]["items"][0]
    assert row["handlers"] == ["ПередВыгрузкой", "ПриВыгрузке"]
    assert row["ИмяПараметраДляПередачи"] == "Банк"
    assert row["ПолучитьИзВходящихДанных"] is True
    assert "Расчетный" not in str(row) and "ИсходящиеДанные" not in str(row)


def test_rules_open_private_copy_save_does_not_touch_shared(tmp_path: Path) -> None:
    """Копия: private и reused false; save копии не меняет saved_path общего проекта."""
    service = Kd2Service(Settings(cache_dir=tmp_path / "cache", workspace=tmp_path / "workspace"))
    path = str(DATA / "exchange_rules.xml")
    shared = service.rules_open(path)
    copy = service.rules_open(path, private=True)
    assert copy["private"] is True
    assert copy["reused"] is False
    assert "source_changed" not in copy
    assert "private" not in shared
    assert copy["project_id"] == f"{shared['project_id']}-p1"
    assert copy["source_path"] == shared["source_path"]

    listed = service.rules_projects()["projects"]
    by_id = {item["project_id"]: item for item in listed}
    assert set(by_id) == {shared["project_id"], copy["project_id"]}
    assert "private" not in by_id[shared["project_id"]]
    assert by_id[copy["project_id"]]["private"] is True

    saved = service.rules_save(shared["project_id"], "shared.xml", False)
    shared_path = service.workspace.get(shared["project_id"]).saved_path
    assert shared_path is not None
    assert shared_path == Path(saved["path"])
    copy_saved = service.rules_save(copy["project_id"], "copy.xml", False)
    assert service.workspace.get(shared["project_id"]).saved_path == shared_path
    copy_path = service.workspace.get(copy["project_id"]).saved_path
    assert copy_path is not None
    assert copy_path == Path(copy_saved["path"])
    assert copy_path != shared_path

    after = {item["project_id"]: item for item in service.rules_projects()["projects"]}
    assert after[shared["project_id"]]["saved_path"] == saved["path"]
    assert after[copy["project_id"]]["saved_path"] == copy_saved["path"]
    assert "private" not in after[shared["project_id"]]
    assert after[copy["project_id"]]["private"] is True


def test_missing_file_inside_workspace_is_not_found(tmp_path: Path) -> None:
    service = Kd2Service(Settings(cache_dir=tmp_path / "cache", workspace=tmp_path / "workspace"))
    missing = service.workspace.root / "no.xml"
    with pytest.raises(Kd2Error, match="Файл не найден"):
        service.rules_open(str(missing))
    with pytest.raises(Kd2Error, match="не входит в подключённые папки"):
        service.rules_open(r"C:\kd2-rules-missing\no.xml")


def test_rules_validate_level_aliases_fail_before_the_project(tmp_path: Path) -> None:
    service = Kd2Service(Settings(cache_dir=tmp_path / "cache", workspace=tmp_path / "workspace"))
    with pytest.raises(ValueError, match="Допустимые"):
        service.rules_validate("missing", None, None, "fatal", None, 0, 20)
    opened = service.rules_open(str(DATA / "exchange_rules.xml"))
    project = opened["project_id"]
    errors = service.rules_validate(project, None, None, "error", None, 0, 20)
    warnings = service.rules_validate(project, None, None, "WARNING", None, 0, 20)
    assert all(item["level"] == "ошибка" for item in errors["issues"]["items"])
    assert all(item["level"] == "предупреждение" for item in warnings["issues"]["items"])


def test_container_source_path_maps_to_the_agent_path() -> None:
    mapped = PathMap.parse(r"C:\1C\Бит=/projects/bit")
    assert mapped.to_host("/projects/bit/Проект/Main") == r"C:\1C\Бит\Проект\Main"
