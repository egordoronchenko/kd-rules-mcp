"""Рабочий проект правил (спецификация `rules-authoring`, «Рабочий проект правил»)."""

import hashlib
import re
import shutil
import sys
import uuid
from datetime import datetime
from pathlib import Path

import pytest

from kd2_rules_mcp.authoring.workspace import RulesProject, RulesWorkspace
from kd2_rules_mcp.errors import (
    DuplicateProjectError,
    Kd2Error,
    ProjectNotFoundError,
    RulesFormatError,
    StructureNotFoundError,
    UnknownFieldError,
    WorkspacePathError,
)
from kd2_rules_mcp.kd2.canonical import canonical_diff, canonical_form
from kd2_rules_mcp.kd2.model import ExchangeRules
from kd2_rules_mcp.kd2.rules_io import dump_rules, load_rules
from kd2_rules_mcp.service import Kd2Service, Settings
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
    return workspace, workspace.open_rules(DATA / "exchange_rules.xml").project


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
    project = workspace.open_rules(source).project
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


def test_save_resets_modified(tmp_path: Path) -> None:
    """Успешное сохранение сбрасывает признак правок."""
    workspace, project = _workspace_with_sample(tmp_path)
    workspace.mark_modified(project.id)
    assert project.modified is True
    workspace.save(project.id, "rules.xml")
    assert project.modified is False


def test_repeated_save_needs_overwrite(tmp_path: Path) -> None:
    workspace, project = _workspace_with_sample(tmp_path)
    saved = workspace.save(project.id, "rules.xml")
    original = saved.read_bytes()
    with pytest.raises(Kd2Error, match="overwrite=True"):
        workspace.save(project.id, "rules.xml")
    assert saved.read_bytes() == original
    assert project.document is not None
    project.document.root.values["Комментарий"] = "правка"
    rewritten = workspace.save(project.id, saved, overwrite=True)
    assert rewritten == saved
    assert "правка".encode() in saved.read_bytes()
    assert project.saved_path == saved


def test_unknown_project_lists_open_projects(tmp_path: Path) -> None:
    workspace = RulesWorkspace(tmp_path / "ws")
    first = workspace.open_rules(DATA / "exchange_rules.xml").project
    second = workspace.open_rules(DATA / "registration_rules.xml").project
    with pytest.raises(ProjectNotFoundError, match="ghost") as error:
        workspace.save("ghost", "a.xml")
    text = str(error.value)
    assert first.id in text
    assert second.id in text
    assert not (workspace.root / "a.xml").exists()


def _copy_rules(folder: Path, name: str) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / name
    shutil.copy(DATA / "exchange_rules.xml", target)
    return target


def test_reopen_same_path_reuses_project(tmp_path: Path) -> None:
    """Один путь дважды — один проект, документ не перечитывается."""
    workspace = RulesWorkspace(tmp_path / "ws")
    source = DATA / "exchange_rules.xml"
    first = workspace.open_rules(source)
    assert first.reused is False
    assert first.source_changed is False
    assert isinstance(first.project.document, ExchangeRules)
    first.project.document.root.values["Комментарий"] = "живёт"
    second = workspace.open_rules(source)
    assert second.reused is True
    assert second.source_changed is False
    assert second.project is first.project
    assert second.project.document is first.project.document
    assert workspace.ids() == [first.project.id]


def test_same_stem_in_different_directories(tmp_path: Path) -> None:
    """Два файла с одним именем получают разные идентификаторы с общим stem."""
    workspace = RulesWorkspace(tmp_path / "ws")
    left = _copy_rules(tmp_path / "a", "ExchangeRules.xml")
    right = _copy_rules(tmp_path / "b", "ExchangeRules.xml")
    first = workspace.open_rules(left).project
    second = workspace.open_rules(right).project
    assert first.id != second.id
    assert first.id.startswith("ExchangeRules-")
    assert second.id.startswith("ExchangeRules-")


def test_cyrillic_file_name_uses_kind_prefix(tmp_path: Path) -> None:
    """Кириллица в имени файла не входит в идентификатор: префикс вида правил."""
    source = _copy_rules(tmp_path / "src", "ПравилаОбмена.xml")
    project = RulesWorkspace(tmp_path / "ws").open_rules(source).project
    assert project.id.startswith("exchange-")
    assert "Правила" not in project.id


def test_hash_collision_lengthens_id(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Одинаковые первые 4 знака хеша — идентификатор удлиняется до 6."""

    def digest(path_key: str) -> str:
        return "abcd" + hashlib.sha256(path_key.encode("utf-8")).hexdigest()

    monkeypatch.setattr("kd2_rules_mcp.authoring.workspace.path_digest", digest)
    workspace = RulesWorkspace(tmp_path / "ws")
    first = workspace.open_rules(_copy_rules(tmp_path / "a", "exchange_rules.xml")).project
    second = workspace.open_rules(_copy_rules(tmp_path / "b", "exchange_rules.xml")).project
    assert first.id == "exchange_rules-abcd"
    assert second.id.startswith("exchange_rules-")
    assert len(second.id.removeprefix("exchange_rules-")) == 6
    assert second.id != first.id


def test_source_changed_after_file_changes(tmp_path: Path) -> None:
    """Изменились размер или время файла — повторное открытие сообщает source_changed."""
    source = _copy_rules(tmp_path / "src", "exchange_rules.xml")
    workspace = RulesWorkspace(tmp_path / "ws")
    first = workspace.open_rules(source)
    source.write_bytes(source.read_bytes() + b"\n")
    second = workspace.open_rules(source)
    assert second.reused is True
    assert second.source_changed is True
    assert second.project is first.project


def test_derived_project_id_and_duplicate(tmp_path: Path) -> None:
    """Без файла идентификатор выводится; явный занятый — duplicate_project со списком."""
    store = _synthetic_store(tmp_path / "cache")
    workspace = RulesWorkspace(tmp_path / "ws")
    first = workspace.create_exchange(store, "source", "target")
    second = workspace.create_exchange(store, "source", "target")
    assert first.id == "new-source-target"
    assert second.id == "new-source-target-2"
    named = workspace.create_exchange(store, "source", "target", "bp-zup-new")
    assert named.id == "bp-zup-new"
    with pytest.raises(DuplicateProjectError, match="bp-zup-new") as error:
        workspace.create_exchange(store, "source", "target", "bp-zup-new")
    text = str(error.value)
    assert "new-source-target" in text
    assert "bp-zup-new" in text
    with pytest.raises(Kd2Error, match="латинские"):
        workspace.create_exchange(store, "source", "target", "../evil")

    registration = load_rules(DATA / "registration_rules.xml")
    cyrillic = workspace.add(registration, label="reg-Обмен")
    assert cyrillic.id.startswith("reg-")
    assert re.fullmatch(r"[A-Za-z0-9_.-]+", cyrillic.id)
    again = workspace.add(load_rules(DATA / "registration_rules.xml"), label="reg-Обмен")
    assert again.id == f"{cyrillic.id}-2"


def test_snapshot_matches_dump_and_failed_edit_keeps_it(tmp_path: Path) -> None:
    """После rule_update снимок равен dump_rules; отказ правки его не меняет."""
    workspace = tmp_path / "ws"
    service = Kd2Service(Settings(cache_dir=tmp_path / "cache", workspace=workspace))
    opened = service.rules_open(str(DATA / "exchange_rules.xml"))
    project_id = opened["project_id"]
    service.rule_update(
        project_id, "pko", "Организации", fields={"Наименование": "Организации (снимок)"}
    )
    project = service.workspace.get(project_id)
    assert isinstance(project.document, ExchangeRules)
    snapshot = workspace / ".projects" / project_id / "rules.xml"
    assert snapshot.read_bytes() == dump_rules(project.document)
    before = snapshot.read_bytes()
    with pytest.raises(UnknownFieldError):
        service.rule_update(project_id, "pko", "Организации", fields={"Чужое": "1"})
    assert snapshot.read_bytes() == before


def test_restored_workspace_loads_edits_lazily(tmp_path: Path) -> None:
    """Новая рабочая папка видит проект; документ разбирается при get, правки на месте."""
    root = tmp_path / "ws"
    workspace = RulesWorkspace(root)
    opened = workspace.open_rules(DATA / "exchange_rules.xml")
    assert isinstance(opened.project.document, ExchangeRules)
    opened.project.document.root.values["Наименование"] = "правка-снимка"
    workspace.mark_modified(opened.project.id)
    restored = RulesWorkspace(root)
    assert restored.ids() == [opened.project.id]
    assert restored._projects[opened.project.id].document is None
    loaded = restored.get(opened.project.id)
    assert isinstance(loaded.document, ExchangeRules)
    assert loaded.document.root.get("Наименование") == "правка-снимка"
    assert loaded.modified is True


def test_broken_snapshot_does_not_block_other_projects(tmp_path: Path) -> None:
    """Битый rules.xml даёт RulesFormatError только своему проекту."""
    root = tmp_path / "ws"
    workspace = RulesWorkspace(root)
    good = workspace.open_rules(DATA / "exchange_rules.xml").project
    bad = workspace.open_rules(DATA / "registration_rules.xml").project
    (root / ".projects" / bad.id / "rules.xml").write_bytes(b"<nope")
    restored = RulesWorkspace(root)
    assert good.id in restored.ids()
    assert bad.id in restored.ids()
    with pytest.raises(RulesFormatError, match=r"rules\.xml") as error:
        restored.get(bad.id)
    text = str(error.value)
    assert str(bad.source_path) in text
    loaded = restored.get(good.id)
    assert isinstance(loaded.document, ExchangeRules)


def test_close_removes_snapshot_and_keeps_saved_file(tmp_path: Path) -> None:
    workspace, project = _workspace_with_sample(tmp_path)
    saved = workspace.save(project.id, "kept.xml")
    assert workspace.close(project.id) is True
    assert project.id not in workspace.ids()
    assert not (workspace.root / ".projects" / project.id).exists()
    assert saved.is_file()
    with pytest.raises(ProjectNotFoundError):
        workspace.get(project.id)
