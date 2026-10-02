"""Общее состояние сервиса: кэш структур, рабочая папка, пути и блокировка правок."""

import sqlite3
import threading
from collections.abc import Callable, Generator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from kd2_rules_mcp.authoring.edits import EditResult
from kd2_rules_mcp.authoring.workspace import RulesProject, RulesWorkspace, normalize_relative
from kd2_rules_mcp.errors import (
    Kd2Error,
    RulesFormatError,
    StructureNotFoundError,
    WorkspacePathError,
)
from kd2_rules_mcp.kd2.model import ExchangeRules, RegistrationRules, RulesDocument
from kd2_rules_mcp.projects import Catalog, load_catalog, project_rules_dirs
from kd2_rules_mcp.service.paths import Settings, _is_absolute
from kd2_rules_mcp.service.views import counts, edit_view
from kd2_rules_mcp.structures.store import LoadResult, StructureStore


class ServiceBase:
    """Настройки, кэш структур, рабочая папка и помощники, общие для миксинов."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.store = StructureStore(settings.cache_dir)
        self.workspace = RulesWorkspace(settings.workspace)
        # Проекты правил меняются на месте — вызовы, которые их трогают, идут по одному.
        # Снимок пишет `RulesWorkspace` из этих же вызовов, под этой блокировкой.
        self._lock = threading.RLock()

    def _edit(
        self,
        operation: Callable[..., EditResult],
        project_id: str,
        kind: str,
        key: str,
        *,
        fields: Mapping[str, Any] | None = None,
        owner: str = "",
        source_structure: str | None = None,
        target_structure: str | None = None,
        group: str | None = None,
    ) -> dict[str, Any]:
        with self._lock, self._sides(source_structure, target_structure) as (source, target):
            rules = self._exchange(project_id)
            # `group` передаётся только из `rule_create`: у `update_rule` такого параметра нет.
            extra: dict[str, Any] = {}
            if group is not None:
                extra["group"] = group
            result = operation(
                rules, kind, key, fields, owner=owner, source=source, target=target, **extra
            )
            return self._edited(project_id, result)

    def _edited(self, project_id: str, result: EditResult) -> dict[str, Any]:
        """Успешная правка документа проекта: помечает его изменённым, пишет снимок, отдаёт итог.

        Отказ правки сюда не попадает — снимок остаётся прежним.
        """
        self.workspace.mark_modified(project_id)
        return edit_view(result)

    def _document(self, project_id: str) -> RulesDocument:
        """Документ проекта. `get` разбирает снимок, если его ещё не читали."""
        document = self.workspace.get(project_id).document
        if document is None:
            raise RulesFormatError(f"Проект «{project_id}» не загружен из снимка")
        return document

    def _catalog(self) -> Catalog:
        return load_catalog(self.settings.projects_file)

    def rules_dirs(self) -> dict[str, Path]:
        """Папки живых правил проектов на сервере (запись разрешена): `KD2_RULES_DIRS` или
        `rules_dir` проектов от их папок; нет файла проектов — пусто."""
        if self.settings.rules_dirs:
            return self.settings.rules_dirs
        if not self.settings.projects_file.is_file():
            return {}
        return project_rules_dirs(self._catalog(), self.settings.project_dirs)

    def writable_dirs(self) -> list[str]:
        """Каталоги, куда сервер пишет, — путями агента (для ответов и ошибок)."""
        folders = [self.workspace.root, *self.rules_dirs().values()]
        return [self._host(folder) for folder in folders]

    def _exchange(self, project_id: str) -> ExchangeRules:
        document = self._document(project_id)
        if not isinstance(document, ExchangeRules):
            raise Kd2Error(f"Проект «{project_id}» — правила регистрации, а нужны правила обмена")
        return document

    def _require_structure(self, structure_id: str) -> None:
        """Структура есть в кэше; иначе ошибка со списком загруженных."""
        if not self.store.exists(structure_id):
            known = ", ".join(self.store.ids()) or "кэш пуст"
            raise StructureNotFoundError(
                f"Структура «{structure_id}» не загружена (загружены: {known})"
            )

    @contextmanager
    def _structure(self, structure_id: str) -> Generator[sqlite3.Connection]:
        self._require_structure(structure_id)
        conn = self.store.open(structure_id)
        try:
            yield conn
        finally:
            conn.close()

    @contextmanager
    def _sides(
        self, source_id: str | None, target_id: str | None
    ) -> Generator[tuple[sqlite3.Connection | None, sqlite3.Connection | None]]:
        opened: list[sqlite3.Connection] = []
        try:
            source = self._open_optional(source_id, opened)
            target = self._open_optional(target_id, opened)
            yield source, target
        finally:
            for conn in opened:
                conn.close()

    def _open_optional(
        self, structure_id: str | None, opened: list[sqlite3.Connection]
    ) -> sqlite3.Connection | None:
        if not structure_id:
            return None
        self._require_structure(structure_id)
        conn = self.store.open(structure_id)
        opened.append(conn)
        return conn

    def _read_path(self, path: str) -> Path:
        local = self._local(path)
        if local is None:
            raise Kd2Error(
                f"Путь «{path}» серверу не виден: папка не подключена (projects.local.yaml → "
                "scripts/setup_local.py)"
            )
        if not local.exists():
            # Внутренний путь контейнера агенту не показываем: остаётся путь, который он передал.
            raise Kd2Error(
                f"Путь «{path}» не найден: путь не входит в подключённые папки проектов "
                "(`project_list`)"
            )
        return local

    def _write_path(self, path: str) -> Path:
        if not _is_absolute(path):
            return Path(path)
        local = self._local(path)
        if local is None:
            raise WorkspacePathError(
                f"Сохранение «{path}» отклонено: путь вне рабочей папки и папок правил проектов. "
                f"Разрешено: {', '.join(self.writable_dirs())}"
            )
        return local

    def _writable(self, path: str) -> Path:
        """Путь записи на сервере — в рабочей папке или `rules_dir` проекта; иначе ошибка с
        разрешёнными каталогами путями агента (пути контейнера агенту ничего не скажут)."""
        try:
            return self.workspace.resolve(self._write_path(path), list(self.rules_dirs().values()))
        except WorkspacePathError as error:
            raise WorkspacePathError(
                f"Сохранение «{path}» отклонено: путь вне рабочей папки и папок правил проектов. "
                f"Разрешено: {', '.join(self.writable_dirs())}"
            ) from error

    def _local(self, path: str) -> Path | None:
        r"""Локальный путь сервера; абсолютный путь агента, который не удалось перевести на этой ОС
        (например, `C:\…` в Linux-контейнере вне подключённых папок), — `None`."""
        if not _is_absolute(path):
            path = normalize_relative(path)
        local = self.settings.path_map.to_local(path)
        if _is_absolute(path) and not local.is_absolute():
            return None
        return local

    def _host(self, path: Path) -> str:
        return self.settings.path_map.to_host(path.resolve())

    def _host_text(self, path: str) -> str:
        return self.settings.path_map.to_host(Path(path)) if path else ""

    def _project_view(self, project: RulesProject) -> dict[str, Any]:
        document = project.document
        if isinstance(document, RegistrationRules):
            kind = "registration"
        elif isinstance(document, ExchangeRules):
            kind = "exchange"
        else:
            kind = project.kind
        view: dict[str, Any] = {
            "project_id": project.id,
            "kind": kind,
            "source_path": self._host(project.source_path) if project.source_path else None,
            "saved_path": self._host(project.saved_path) if project.saved_path else None,
            "modified": project.modified,
        }
        if document is None:
            return view
        view["counts"] = counts(document)
        if isinstance(document, ExchangeRules):
            view["name"] = str(document.root.values.get("Наименование", ""))
            view["source"] = document.source_name
            view["target"] = document.target_name
        elif isinstance(document, RegistrationRules):
            view["name"] = str(document.root.values.get("Наименование", ""))
            view["exchange_plan"] = document.exchange_plan
        return view

    def _load_view(self, result: LoadResult) -> dict[str, Any]:
        view: dict[str, Any] = {
            "structure_id": result.structure_id,
            "reused": result.reused,
            "counts": result.counts,
            "elapsed_s": result.elapsed_s,
            "message": result.message,
        }
        if result.unresolved:
            top = sorted(result.unresolved.items(), key=lambda item: -item[1])[:20]
            view["unresolved_total"] = len(result.unresolved)
            view["unresolved_top"] = dict(top)
        return view
