"""Ответы сервиса по проектам правил: приватная копия rules_open."""

from pathlib import Path

from kd2_rules_mcp.service import Kd2Service, Settings

DATA = Path(__file__).parent / "data"


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
