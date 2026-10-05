"""Рабочие проекты полных менеджеров: память, атомарный manifest и точные тела."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from threading import RLock

from kd2_rules_mcp.ed.writer_model import (
    ImportReport,
    ManagerModel,
    decode_dto,
    json_bytes,
    load_model,
    pack_json,
    snapshot_parts,
    unpack_json,
)
from kd2_rules_mcp.errors import EdAuthoringResourceLimitError, EdAuthoringStaleError

from .manager_operations import ManagerOperation, ManagerPreview, apply, preview

MAX_PROJECTS = 100


@dataclass(frozen=True, slots=True)
class ManagerProject:
    id: str
    model: ManagerModel
    snapshot_hash: str


class ManagerWorkspace:
    """Библиотечная блокировка; чужой изменённый manifest не перезаписывается."""

    def __init__(self, root: Path):
        self.root = Path(root).resolve()
        self.directory = self.root / ".ed-projects"
        self._lock = RLock()
        self._projects: dict[str, ManagerProject] = {}
        self._damaged: dict[str, str] = {}
        if self.directory.is_dir():
            for folder in sorted(self.directory.iterdir()):
                if folder.is_dir() and (folder / "manager.ed.json").is_file():
                    try:
                        self._folder(folder.name)
                        data = (folder / "manager.ed.json").read_bytes()
                        payload = json.loads(data)
                        if any(not _valid_hash(key) for key in payload.get("blob_hashes", ())):
                            raise ValueError("Неверное имя контентного файла ED")
                        blobs = {
                            key: (folder / "bodies" / f"{key}.bsl").read_bytes()
                            for key in payload.get("blob_hashes", ())
                        }
                        payload.pop("blob_hashes", None)
                        report = decode_dto(
                            ImportReport,
                            unpack_json(json.loads((folder / "import-report.json").read_bytes())),
                        )
                        model = load_model(json_bytes(payload), blobs=blobs, import_report=report)
                        if model.project_id != folder.name:
                            raise ValueError("Идентичность снимка ED не совпадает с каталогом")
                        if any(key.casefold() == folder.name.casefold() for key in self._projects):
                            raise ValueError("Идентификаторы проектов совпадают без учёта регистра")
                        self._projects[model.project_id] = ManagerProject(
                            model.project_id, model, hashlib.sha256(data).hexdigest()
                        )
                    except Exception as error:
                        self._damaged[folder.name] = f"{type(error).__name__}: {error}"
                    if len(self._projects) > MAX_PROJECTS:
                        raise EdAuthoringResourceLimitError("Превышен лимит проектов ED")

    def _folder(self, project_id: str) -> Path:
        if (
            not project_id
            or project_id in (".", "..")
            or len(project_id) > 128
            or not all(c.isalnum() or c in "_.-" for c in project_id)
            or project_id.endswith((".", " "))
            or project_id.split(".")[0].upper()
            in {
                "CON",
                "PRN",
                "AUX",
                "NUL",
                *(f"COM{n}" for n in range(1, 10)),
                *(f"LPT{n}" for n in range(1, 10)),
                *(f"COM{n}" for n in "¹²³"),
                *(f"LPT{n}" for n in "¹²³"),
            }
        ):
            raise ValueError("Неверный идентификатор проекта ED")
        folder = (self.directory / project_id).resolve()
        if not folder.is_relative_to(self.root) or folder == self.root:
            raise ValueError("Снимок ED вне рабочей папки")
        return folder

    @contextmanager
    def _disk_lock(self, project_id: str):
        folder = self._folder(project_id)
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / ".lock"
        try:
            descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            raw = path.read_bytes()
            try:
                lock = json.loads(raw)
                stale = time.time() - lock["created"] > 300 or not _process_alive(lock["pid"])
            except (ValueError, KeyError, TypeError):
                stale = time.time() - path.stat().st_mtime > 300
            if not stale:
                raise EdAuthoringStaleError("Проект менеджера изменяется конкурентно") from None
            # Отдельный эксклюзивный файл защищает снятие старого замка от гонки.
            reclaim = folder / ".reclaim"
            try:
                reclaim_fd = os.open(reclaim, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError as error:
                previous = reclaim.read_bytes()
                try:
                    info = json.loads(previous)
                    expired = time.time() - info["created"] > 300 or not _process_alive(info["pid"])
                except (ValueError, KeyError, TypeError):
                    expired = time.time() - reclaim.stat().st_mtime > 300
                if not expired or reclaim.read_bytes() != previous:
                    raise EdAuthoringStaleError(
                        "Замок менеджера восстанавливается конкурентно"
                    ) from error
                reclaim.unlink()
                try:
                    reclaim_fd = os.open(reclaim, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                except FileExistsError as race:
                    raise EdAuthoringStaleError(
                        "Замок менеджера восстанавливается конкурентно"
                    ) from race
            try:
                with os.fdopen(reclaim_fd, "wb") as stream:
                    stream.write(json_bytes({"pid": os.getpid(), "created": time.time()}))
                if path.read_bytes() != raw:
                    raise EdAuthoringStaleError("Замок менеджера сменился")
                path.unlink()
                descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            finally:
                reclaim.unlink(missing_ok=True)
        raw = json_bytes({"pid": os.getpid(), "created": time.time()})
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(raw)
            yield folder
        finally:
            if path.exists() and path.read_bytes() == raw:
                path.unlink()

    def ids(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(self._projects)

    def get(self, project_id: str) -> ManagerProject:
        with self._lock:
            self._folder(project_id)
            if project_id in self._damaged:
                raise ValueError("Повреждённый снимок проекта ED: " + self._damaged[project_id])
            return self._projects[project_id]

    def _check_current(self, project: ManagerProject) -> None:
        path = self._folder(project.id) / "manager.ed.json"
        if (
            not path.is_file()
            or hashlib.sha256(path.read_bytes()).hexdigest() != project.snapshot_hash
        ):
            raise EdAuthoringStaleError("Снимок менеджера изменён другим владельцем")

    def create(self, model: ManagerModel) -> ManagerProject:
        with self._lock, self._disk_lock(model.project_id) as folder:
            if any(
                p.name.casefold() == model.project_id.casefold() and p.name != model.project_id
                for p in self.directory.iterdir()
            ):
                raise ValueError("Идентификатор проекта занят без учёта регистра")
            if model.project_id in self._projects or (folder / "manager.ed.json").exists():
                raise ValueError("Проект менеджера уже существует")
            if len(self._projects) >= MAX_PROJECTS:
                raise EdAuthoringResourceLimitError("Превышен лимит проектов ED")
            return self._persist(model)

    def preview(
        self, project_id: str, operations: tuple[ManagerOperation, ...], *, expected_revision: str
    ) -> ManagerPreview:
        with self._lock:
            project = self.get(project_id)
            self._check_current(project)
            return preview(project.model, operations, expected_revision=expected_revision)

    def apply(
        self,
        project_id: str,
        operations: tuple[ManagerOperation, ...],
        *,
        expected_revision: str,
        expected_preview_hash: str,
        confirmations: tuple[tuple[str, str], ...] = (),
    ) -> ManagerProject:
        with self._lock, self._disk_lock(project_id):
            project = self.get(project_id)
            self._check_current(project)
            updated = apply(
                project.model,
                operations,
                expected_revision=expected_revision,
                expected_preview_hash=expected_preview_hash,
                confirmations=confirmations,
            )
            if updated is project.model:
                return project
            return self._persist(updated)

    def _persist(self, model: ManagerModel) -> ManagerProject:
        folder = self._folder(model.project_id)
        model = model.with_revision()
        value, blobs = snapshot_parts(model)
        data = json_bytes(
            {"storage_version": 3, "model": pack_json(value), "blob_hashes": sorted(blobs)}
        )
        # Исходник (с BOM), тела и manifest адресуются хешами; тексты блоков
        # восстанавливаются из диапазонов единственного исходника.
        for key, content in blobs.items():
            path = folder / "bodies" / f"{key}.bsl"
            if not path.resolve().is_relative_to(folder):
                raise ValueError("Сохранённое тело ED вне каталога проекта")
            if path.exists():
                if path.read_bytes() != content:
                    raise EdAuthoringStaleError("Сохранённое тело менеджера изменено")
            else:
                _atomic_write(path, content)
        report_path = folder / "import-report.json"
        previous = self._projects.get(model.project_id)
        if previous is None or previous.model.import_report is not model.import_report:
            _atomic_write(report_path, json_bytes(pack_json(model.import_report)))
        _atomic_write(folder / "manager.ed.json", data)
        project = ManagerProject(model.project_id, model, hashlib.sha256(data).hexdigest())
        self._projects[model.project_id] = project
        active = {f"{key}.bsl" for key in blobs}
        for path in (folder / "bodies").glob("*.bsl"):
            if path.name not in active:
                path.unlink()
        return project

    def close(self, project_id: str) -> bool:
        with self._lock, self._disk_lock(project_id) as folder:
            project = self.get(project_id)
            self._check_current(project)
            # Удаление снимка только в проверенном каталоге своего проекта;
            # опубликованные комплекты этот слой вообще не знает.
            for child in folder.iterdir():
                if child.name == ".lock":
                    continue
                if not child.resolve().is_relative_to(folder):
                    raise ValueError("Файл снимка ED вне каталога проекта")
                if child.is_dir():
                    shutil.rmtree(child)
                else:
                    child.unlink()
            del self._projects[project_id]
        folder.rmdir()
        return True


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".ed-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _valid_hash(value: str) -> bool:
    return (
        isinstance(value, str) and len(value) == 64 and all(c in "0123456789abcdef" for c in value)
    )


def _process_alive(pid: int) -> bool:
    if type(pid) is not int or pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
        kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            return ctypes.get_last_error() == 5
        try:
            code = wintypes.DWORD()
            return bool(kernel.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == 259
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True
