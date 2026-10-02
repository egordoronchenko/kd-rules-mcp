"""Рабочий проект правил (спецификация `rules-authoring`, «Рабочий проект правил»)."""

import re
import sys
import uuid
from datetime import datetime
from pathlib import Path

import pytest

from kd2_rules_mcp.authoring.workspace import RulesProject, RulesWorkspace
from kd2_rules_mcp.errors import (
    Kd2Error,
    ProjectNotFoundError,
    StructureNotFoundError,
    WorkspacePathError,
)
from kd2_rules_mcp.kd2.canonical import canonical_diff, canonical_form
from kd2_rules_mcp.kd2.model import ExchangeRules
from kd2_rules_mcp.kd2.rules_io import dump_rules, load_rules
from kd2_rules_mcp.structures.store import StructureStore
from kd2_rules_mcp.validation.format import check_format

DATA = Path(__file__).parent / "data"
DUMP = DATA / "xmldump"
MAIN = DUMP / "main"
EXT = DUMP / "ext"

_CREATED = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}")


def _synthetic_store(cache: Path) -> StructureStore:
    store = StructureStore(cache)
    store.load_xml("source", MAIN)
    store.load_xml("target", EXT)
    return store


def _workspace_with_sample(tmp_path: Path) -> tuple[RulesWorkspace, RulesProject]:
    workspace = RulesWorkspace(tmp_path / "ws")
    return workspace, workspace.open_rules(DATA / "exchange_rules.xml")


def _assert_same(left: bytes | Path, right: bytes | Path) -> None:
    diff = canonical_diff(canonical_form(left), canonical_form(right))
    assert diff == [], diff


def _assert_side(rules: ExchangeRules, tag: str, meta: dict[str, str]) -> None:
    name, attrs = rules.config(tag)
    assert name == meta["config_name"]
    assert attrs["СинонимКонфигурации"] == meta["config_synonym"]
    assert attrs["ВерсияКонфигурации"] == meta["config_version"]
    assert attrs["ВерсияПлатформы"] == ""


def _assert_empty_header(
    rules: ExchangeRules, source: dict[str, str], target: dict[str, str]
) -> None:
    version = rules.root.child("ВерсияФормата")
    assert version is not None
    assert version.text == "2.01"
    assert version.attrs["РежимСовместимости"] == "РежимСовместимостиСБСП20"
    raw_id = str(rules.root.get("Ид"))
    assert uuid.UUID(raw_id.strip()).version == 4
    assert raw_id == raw_id.strip().ljust(40)
    expected_name = f"{source['config_name'].strip()} --> {target['config_name'].strip()}".strip()
    assert rules.root.get("Наименование") == expected_name
    created = str(rules.root.get("ДатаВремяСоздания"))
    assert _CREATED.fullmatch(created)
    stamp = datetime.strptime(created, "%Y-%m-%dT%H:%M:%S")
    assert abs((datetime.now() - stamp).total_seconds()) < 10
    _assert_side(rules, "Источник", source)
    _assert_side(rules, "Приемник", target)
    assert rules.pko() == []
    assert rules.pvd() == []
    assert rules.pod() == []
    assert rules.algorithms() == []
    assert rules.queries() == []


def _assert_saved_round_trip(
    workspace: RulesWorkspace, project: RulesProject, relative: str
) -> None:
    assert isinstance(project.document, ExchangeRules)
    original = dump_rules(project.document)
    saved = workspace.save(project.id, relative)
    assert project.saved_path == saved
    assert saved.is_file()
    _assert_same(saved, original)
    reloaded = load_rules(saved)
    assert isinstance(reloaded, ExchangeRules)
    _assert_same(dump_rules(reloaded), original)
    report = check_format(reloaded)
    assert report.errors == [], [issue.message for issue in report.errors]
    assert reloaded.pko() == []
    assert reloaded.pvd() == []
    assert reloaded.pod() == []
    assert reloaded.algorithms() == []
    assert reloaded.queries() == []


def test_new_rules_for_synthetic_structures(tmp_path: Path) -> None:
    """Сценарий «Новые правила для пары конфигураций» на маленькой выгрузке
    `tests/data/xmldump`, без внешних структур."""
    store = _synthetic_store(tmp_path / "cache")
    workspace = RulesWorkspace(tmp_path / "ws")
    project = workspace.create_exchange(store, "source", "target")
    other = workspace.create_exchange(store, "source", "target")
    assert project.id != other.id
    assert isinstance(project.document, ExchangeRules)
    assert isinstance(other.document, ExchangeRules)
    assert project.document.root.get("Ид") != other.document.root.get("Ид")
    _assert_empty_header(project.document, store.meta("source"), store.meta("target"))
    _assert_saved_round_trip(workspace, project, "nested/empty.xml")


def test_unknown_structure_does_not_open_project(tmp_path: Path) -> None:
    store = _synthetic_store(tmp_path / "cache")
    workspace = RulesWorkspace(tmp_path / "ws")
    with pytest.raises(StructureNotFoundError, match="missing"):
        workspace.create_exchange(store, "missing", "source")
    with pytest.raises(StructureNotFoundError, match="missing"):
        workspace.create_exchange(store, "source", "missing")
    with pytest.raises(ProjectNotFoundError, match="нет открытых проектов"):
        workspace.get("1")


@pytest.mark.parametrize("filename", ["exchange_rules.xml", "registration_rules.xml"])
def test_open_sample_save_preserves_canonical_form(tmp_path: Path, filename: str) -> None:
    source = DATA / filename
    workspace = RulesWorkspace(tmp_path / "ws")
    project = workspace.open_rules(source)
    assert project.source_path == source.resolve()
    assert project.saved_path is None
    saved = workspace.save(project.id, Path("saved") / filename)
    assert project.saved_path == saved
    _assert_same(saved, source)


def test_relative_and_absolute_paths_inside_workspace(tmp_path: Path) -> None:
    workspace, project = _workspace_with_sample(tmp_path)
    relative = workspace.save(project.id, Path("dir") / ".." / "inside.xml")
    assert relative == (workspace.root / "inside.xml").resolve()
    assert relative.is_file()
    absolute = workspace.save(project.id, workspace.root / "abs.xml")
    assert absolute == (workspace.root / "abs.xml").resolve()
    assert project.saved_path == absolute


def test_relative_backslash_is_a_subdirectory(tmp_path: Path) -> None:
    """`sub\\rules.xml` — подпапка, а не файл с обратной косой в имени."""
    workspace, project = _workspace_with_sample(tmp_path)
    saved = workspace.save(project.id, "sub\\rules.xml")
    root = workspace.root.resolve()
    assert saved.relative_to(root).parts == ("sub", "rules.xml")
    assert (root / "sub").is_dir()
    assert saved.is_file()
    assert all("\\" not in item.name for item in root.iterdir())


def test_save_outside_workspace_is_rejected(tmp_path: Path) -> None:
    """Сценарий «Сохранение вне рабочей папки»: абсолютный путь и `..\\..\\x.xml`."""
    workspace, project = _workspace_with_sample(tmp_path)
    allowed = str(workspace.root.resolve())
    outside_dir = tmp_path / "nope"
    outside = outside_dir / "x.xml"
    escaped = (workspace.root / ".." / ".." / "x.xml").resolve()
    escaped_before = escaped.exists()
    try:
        for destination in (outside, Path(r"..\..\x.xml")):
            with pytest.raises(WorkspacePathError, match=re.escape(allowed)):
                workspace.save(project.id, destination)
        assert not outside.exists()
        assert not outside_dir.exists()
        if not escaped_before:
            assert not escaped.exists()
    finally:
        if not escaped_before and escaped.exists():
            escaped.unlink()


def test_symlink_outside_workspace_is_rejected(tmp_path: Path) -> None:
    """Путь через ссылку наружу не создаёт файл.

    На Windows без прав на символическую ссылку берётся junction каталога — прав она не требует.
    """
    workspace, project = _workspace_with_sample(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    link = workspace.root / "link"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError as error:
        if sys.platform != "win32":
            pytest.skip(f"Нельзя создать символическую ссылку: {error}")
        import _winapi

        _winapi.CreateJunction(str(outside), str(link))
    allowed = str(workspace.root.resolve())
    with pytest.raises(WorkspacePathError, match=re.escape(allowed)):
        workspace.save(project.id, link / "x.xml")
    assert not (outside / "x.xml").exists()


def test_repeated_save_needs_overwrite(tmp_path: Path) -> None:
    workspace, project = _workspace_with_sample(tmp_path)
    saved = workspace.save(project.id, "rules.xml")
    original = saved.read_bytes()
    with pytest.raises(Kd2Error, match="overwrite=True"):
        workspace.save(project.id, "rules.xml")
    assert saved.read_bytes() == original
    project.document.root.values["Комментарий"] = "правка"
    rewritten = workspace.save(project.id, saved, overwrite=True)
    assert rewritten == saved
    assert "правка".encode() in saved.read_bytes()
    assert project.saved_path == saved


def test_unknown_project_lists_open_projects(tmp_path: Path) -> None:
    workspace = RulesWorkspace(tmp_path / "ws")
    first = workspace.open_rules(DATA / "exchange_rules.xml")
    second = workspace.open_rules(DATA / "registration_rules.xml")
    with pytest.raises(ProjectNotFoundError, match="ghost") as error:
        workspace.save("ghost", "a.xml")
    text = str(error.value)
    assert first.id in text
    assert second.id in text
    assert not (workspace.root / "a.xml").exists()
