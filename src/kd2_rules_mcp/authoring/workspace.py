"""Рабочий проект правил: открытие XML, пустые правила для пары структур, запись в рабочую папку.

Писатель заголовка — `reference/kd2-cfg/DataProcessors/ВыгрузкаКонвертации/Ext/ObjectModule.bsl`
(`ВыгрузитьРеквизитыКонвертации`, `ВыгрузитьКонвертацию`). Ниже строки этого файла обозначены `ВК:`.

Снимки проектов лежат в `<корень>/.projects/<id>/` и восстанавливаются при создании рабочей папки:
`meta.json` читается сразу, `rules.xml` — при первом `get`.
"""

import hashlib
import json
import logging
import os
import re
import shutil
import tempfile
import uuid
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath, PureWindowsPath

from kd2_rules_mcp.errors import (
    DuplicateProjectError,
    Kd2Error,
    ProjectNotFoundError,
    RulesFormatError,
    WorkspacePathError,
)
from kd2_rules_mcp.kd2.model import ExchangeRules, Node, RegistrationRules, RulesDocument
from kd2_rules_mcp.kd2.rules_io import SUPPORTED_FORMAT_VERSION, dump_rules, load_rules
from kd2_rules_mcp.kd2.xmlstyle import KD_STYLE, preserve_line_endings
from kd2_rules_mcp.structures.store import StructureStore
from kd2_rules_mcp.validation.handlers import HandlerExport, HandlerFile

logger = logging.getLogger("kd2_rules_mcp")

# Каталог снимков внутри рабочей папки. Имя проекта — один компонент пути из `_ID_PART`.
_PROJECTS_DIR = ".projects"
_META_VERSION = 1
# Идентификатор каталога снимка и аргумент инструментов: латиница, чтобы клиенты MCP
# не искажали его. Кириллица в имени файла сюда не попадает — см. `open_rules`.
_ID_PART = re.compile(r"^[A-Za-z0-9_.-]+$")
# Длины префикса SHA-256 при коллизии идентификатора файла (design Р1).
_HASH_LENGTHS = (4, 6, 8)

# Значение заполнения Конвертации.РежимСовместимости
# (reference/kd2-cfg/Catalogs/Конвертации.xml:1404); имя значения пишет ВК:422.
_COMPATIBILITY = "РежимСовместимостиСБСП20"

# Код справочника — строка фиксированной длины 40
# (reference/kd2-cfg/Catalogs/Конвертации.xml:43, :46).
_CODE_LENGTH = 40


@dataclass(eq=False, slots=True)
class RulesProject:
    """Открытый рабочий проект: документ в памяти, пути и признак несохранённых правок.

    `modified` — документ менялся после открытия, создания или последнего сохранения.
    Новый проект, который ещё ни разу не писали, виден по `saved_path is None`.
    `document` равен `None`, пока снимок не разобран (`get`).
    `normalized_path` — ключ исходного файла (`identity_path`); `source_mtime_ns` и
    `source_size` — его время и размер на момент открытия.
    `handlers` — карта строк последнего `handlers_export`.
    """

    id: str
    document: RulesDocument | None
    source_path: Path | None
    saved_path: Path | None = None
    modified: bool = False
    kind: str = "exchange"
    normalized_path: str | None = None
    source_mtime_ns: int | None = None
    source_size: int | None = None
    handlers: HandlerExport | None = None


@dataclass(frozen=True, slots=True)
class OpenedProject:
    """Результат `open_rules`: проект и признаки повторного открытия.

    `reused` — тот же нормализованный путь уже открыт, документ с диска не читался.
    `source_changed` — у файла изменились время или размер с момента открытия;
    смотреть вместе с `reused`.
    """

    project: RulesProject
    reused: bool
    source_changed: bool


class RulesWorkspace:
    """Рабочая папка правил (design.md, Д5): проекты в памяти и снимки в `.projects`.

    Запись правил — только внутрь корня (или явно разрешённых папок). Снимок пишется
    после открытия, создания, правки, сохранения и экспорта обработчиков.
    """

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._projects: dict[str, RulesProject] = {}
        # Нормализованный путь исходного файла → идентификатор проекта.
        self._by_source: dict[str, str] = {}
        self._restore()

    def open_rules(self, path: Path | str) -> OpenedProject:
        """Открывает правила обмена или регистрации из XML по любому читаемому пути.

        Идентификатор — `<stem>-<hash>`: `stem` — имя файла без расширения, если оно
        целиком из `[A-Za-z0-9_.-]`, иначе латинский префикс вида правил (`exchange` или
        `registration`). Кириллицу в идентификатор не кладём: клиенты MCP искажают такие
        аргументы. `hash` — первые 4 шестнадцатеричных знака SHA-256 нормализованного
        пути сервера (`identity_path`); если идентификатор уже занят другим путём —
        6, затем 8 знаков. Повтор того же пути возвращает тот же проект, файл не
        перечитывается.
        """
        source = Path(path)
        source_key = identity_path(source)
        existing_id = self._by_source.get(source_key)
        if existing_id is not None:
            project = self.get(existing_id)
            return OpenedProject(project, True, _source_changed(project, source))
        document = load_rules(source)
        kind = _document_kind(document)
        project_id = self._file_id(_file_stem(source, kind), source_key)
        mtime_ns, size = _source_stat(source)
        project = self._remember(
            document,
            source.resolve(),
            project_id,
            kind=kind,
            normalized_path=source_key,
            source_mtime_ns=mtime_ns,
            source_size=size,
        )
        return OpenedProject(project, False, False)

    def create_exchange(
        self,
        store: StructureStore,
        source_id: str,
        target_id: str,
        project_id: str | None = None,
    ) -> RulesProject:
        """Пустые правила обмена для пары структур.

        Без `project_id` идентификатор — `new-<источник>-<приёмник>`; занятый получает
        суффикс `-2`, `-3`. Явный занятый — `DuplicateProjectError`.
        Неизвестный идентификатор структуры — `StructureNotFoundError` из кэша, проект не создаётся.
        """
        source = store.meta(source_id)
        target = store.meta(target_id)
        chosen = self._explicit_or_derived(
            project_id, f"new-{_safe_token(source_id)}-{_safe_token(target_id)}"
        )
        return self._remember(_empty_exchange(source, target), None, chosen, kind="exchange")

    def get(self, project_id: str) -> RulesProject:
        """Рабочий проект по идентификатору; при первом обращении разбирает снимок.

        Нет такого — ошибка со списком открытых. Битый снимок — `RulesFormatError`
        с путём снимка и `source_path`, остальные проекты не затрагиваются.
        """
        project = self._lookup(project_id)
        if project.document is None:
            self._load_snapshot(project)
        return project

    def iter_projects(self) -> Iterator[RulesProject]:
        """Проекты для списка. Снимок разбирается; битый остаётся без документа.

        Ошибка разбора пишется в лог и не мешает остальным проектам.
        """
        for project_id in self.ids():
            try:
                yield self.get(project_id)
            except RulesFormatError as error:
                logger.warning("%s", error)
                yield self._lookup(project_id)

    def save(
        self,
        project_id: str,
        path: Path | str,
        *,
        overwrite: bool = False,
        allowed: Sequence[Path] = (),
    ) -> Path:
        """Пишет XML проекта внутрь рабочей папки (или разрешённой папки) и запоминает путь.

        Путь относительный к корню или абсолютный. После `resolve` (включая `..` и симлинки)
        файл должен лежать внутри корня или одной из `allowed` (папки живых правил проектов),
        иначе `WorkspacePathError`. Существующий файл заменяется только при `overwrite=True`.
        Успешная запись сбрасывает `modified` и обновляет снимок.
        Файл сохранения снимок не заменяет: каталог `.projects` живёт отдельно.
        """
        project = self.get(project_id)
        document = project.document
        if document is None:
            raise RulesFormatError(f"Проект «{project_id}» не загружен из снимка")
        destination = self._destination(path, allowed)
        if destination.is_file() and not overwrite:
            raise Kd2Error(
                f"Файл «{destination}» уже существует; повторная запись только при overwrite=True"
            )
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.is_file():
            data = preserve_line_endings(destination.read_bytes(), dump_rules(document))
        else:
            data = dump_rules(document)
        destination.write_bytes(data)
        project.saved_path = destination
        project.modified = False
        self.snapshot(project_id)
        return destination

    def ids(self) -> list[str]:
        """Идентификаторы открытых проектов в порядке открытия (после старта — по имени снимка)."""
        return list(self._projects)

    def add(
        self, document: RulesDocument, project_id: str | None = None, *, label: str
    ) -> RulesProject:
        """Новый проект из документа, собранного сервером (правила регистрации, черновик).

        `label` — основа идентификатора (`reg-<план>`, `corr-<проект>`), если `project_id` пуст.
        Символы вне `[A-Za-z0-9_.-]` в основе заменяются коротким хешем.
        """
        chosen = self._explicit_or_derived(project_id, _safe_label(label))
        return self._remember(document, None, chosen, kind=_document_kind(document))

    def mark_modified(self, project_id: str) -> None:
        """Помечает проект изменённым и записывает снимок.

        Вызывается только после успешной правки: отказ с откатом сюда не доходит.
        """
        self.get(project_id).modified = True
        self.snapshot(project_id)

    def remember_handlers(self, project_id: str, export: HandlerExport) -> None:
        """Запоминает карту строк выгрузки обработчиков и записывает её в снимок."""
        project = self.get(project_id)
        project.handlers = export
        self.snapshot(project_id)

    def snapshot(self, project_id: str) -> None:
        """Пишет `rules.xml`, `handlers.json` (если карта есть) и `meta.json` атомарно.

        `meta.json` — последним: обрыв записи не подменяет сведения о проекте новыми
        при старых правилах. Пути в снимке — серверные.
        """
        project = self._lookup(project_id)
        document = project.document
        if document is None:
            raise RulesFormatError(f"Проект «{project_id}» не загружен, снимок не записан")
        directory = self._snapshot_dir(project.id)
        directory.mkdir(parents=True, exist_ok=True)
        _atomic_write(directory / "rules.xml", dump_rules(document))
        if project.handlers is not None:
            _atomic_write(directory / "handlers.json", _dump_handlers(project.handlers))
        _atomic_write(directory / "meta.json", _dump_meta(project))

    def close(self, project_id: str) -> bool:
        """Удаляет проект из памяти и каталог снимка. Файлы `save` не трогает.

        Возвращает, был ли каталог снимка на диске.
        """
        project = self._lookup(project_id)
        self._projects.pop(project.id)
        if (
            project.normalized_path is not None
            and self._by_source.get(project.normalized_path) == project.id
        ):
            self._by_source.pop(project.normalized_path)
        directory = self._snapshot_dir(project.id)
        existed = directory.is_dir()
        if existed:
            shutil.rmtree(directory)
        return existed

    def resolve(self, path: Path | str, allowed: Sequence[Path] = ()) -> Path:
        """Проверенный путь внутри рабочей папки или `allowed`; вне их — `WorkspacePathError`."""
        return self._destination(path, allowed)

    def _remember(
        self,
        document: RulesDocument,
        source_path: Path | None,
        project_id: str,
        *,
        kind: str,
        normalized_path: str | None = None,
        source_mtime_ns: int | None = None,
        source_size: int | None = None,
    ) -> RulesProject:
        project = RulesProject(
            project_id,
            document,
            source_path,
            kind=kind,
            normalized_path=normalized_path,
            source_mtime_ns=source_mtime_ns,
            source_size=source_size,
        )
        self._projects[project.id] = project
        if normalized_path is not None:
            self._by_source[normalized_path] = project.id
        self.snapshot(project.id)
        return project

    def _lookup(self, project_id: str) -> RulesProject:
        project = self._projects.get(project_id)
        if project is not None:
            return project
        raise ProjectNotFoundError(
            f"Рабочего проекта «{project_id}» нет ({_known(self._projects)})"
        )

    def _explicit_or_derived(self, project_id: str | None, derived: str) -> str:
        if project_id:
            return self._claim(project_id)
        if derived not in self._projects:
            return derived
        number = 2
        while f"{derived}-{number}" in self._projects:
            number += 1
        return f"{derived}-{number}"

    def _claim(self, project_id: str) -> str:
        if not _valid_id(project_id):
            raise Kd2Error(
                "Идентификатор проекта правил может содержать только латинские буквы, "
                "цифры, точку, подчёркивание и дефис"
            )
        if project_id in self._projects:
            raise DuplicateProjectError(
                f"Идентификатор «{project_id}» уже занят ({_known(self._projects)})"
            )
        return project_id

    def _file_id(self, stem: str, source_key: str) -> str:
        digest = path_digest(source_key)
        for length in _HASH_LENGTHS:
            candidate = f"{stem}-{digest[:length]}"
            other = self._projects.get(candidate)
            if other is None or other.normalized_path == source_key:
                return candidate
        candidate = f"{stem}-{digest}"
        if candidate not in self._projects:
            return candidate
        number = 2
        while f"{candidate}-{number}" in self._projects:
            number += 1
        return f"{candidate}-{number}"

    def _snapshot_dir(self, project_id: str) -> Path:
        return self.root / _PROJECTS_DIR / project_id

    def _load_snapshot(self, project: RulesProject) -> None:
        directory = self._snapshot_dir(project.id)
        rules_path = directory / "rules.xml"
        try:
            document = load_rules(rules_path)
        except (OSError, RulesFormatError) as error:
            raise _snapshot_error(project, rules_path, error) from error
        handlers: HandlerExport | None = None
        handlers_path = directory / "handlers.json"
        if handlers_path.is_file():
            try:
                handlers = _load_handlers(handlers_path)
            except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError) as error:
                raise _snapshot_error(project, handlers_path, error) from error
        project.document = document
        project.kind = _document_kind(document)
        project.handlers = handlers

    def _restore(self) -> None:
        base = self.root / _PROJECTS_DIR
        if not base.is_dir():
            return
        for directory in sorted(path for path in base.iterdir() if path.is_dir()):
            self._restore_one(directory)

    def _restore_one(self, directory: Path) -> None:
        if not _valid_id(directory.name):
            logger.warning(
                "Снимок %s пропущен: имя каталога не является идентификатором проекта", directory
            )
            return
        meta_path = directory / "meta.json"
        if not meta_path.is_file():
            logger.warning("Снимок %s пропущен: нет meta.json", directory)
            return
        try:
            payload = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            logger.warning("Снимок %s пропущен: meta.json не читается (%s)", directory, error)
            return
        if not isinstance(payload, dict) or payload.get("version") != _META_VERSION:
            version = payload.get("version") if isinstance(payload, dict) else None
            logger.warning("Снимок %s пропущен: версия %s", directory, version)
            return
        if payload.get("id") != directory.name:
            logger.warning("Снимок %s пропущен: id в meta.json не совпадает с каталогом", directory)
            return
        kind = payload.get("kind")
        if kind not in ("exchange", "registration"):
            logger.warning("Снимок %s пропущен: неизвестный вид %s", directory, kind)
            return
        source_path = _meta_path(payload.get("source_path"))
        normalized = identity_path(source_path) if source_path is not None else None
        project = RulesProject(
            directory.name,
            None,
            source_path,
            saved_path=_meta_path(payload.get("saved_path")),
            modified=bool(payload.get("modified", False)),
            kind=kind,
            normalized_path=normalized,
            source_mtime_ns=_meta_int(payload.get("source_mtime_ns")),
            source_size=_meta_int(payload.get("source_size")),
        )
        self._projects[project.id] = project
        if normalized is None:
            return
        previous = self._by_source.get(normalized)
        if previous is not None and previous != project.id:
            logger.warning("Снимок %s: путь %s уже у проекта %s", directory, normalized, previous)
        self._by_source[normalized] = project.id

    def _destination(self, path: Path | str, allowed: Sequence[Path] = ()) -> Path:
        roots = [self.root.resolve(), *(Path(folder).resolve() for folder in allowed)]
        candidate = Path(normalize_relative(os.fspath(path)))
        if not candidate.is_absolute():
            candidate = self.root / candidate
        resolved = candidate.resolve()
        if not any(_is_inside(resolved, root) for root in roots):
            raise WorkspacePathError(
                f"Сохранение «{resolved}» отклонено: путь вне рабочей папки"
                f"{' и папок правил проектов' if allowed else ''}. "
                f"Разрешено: {', '.join(str(root) for root in roots)}"
            )
        return resolved


def identity_path(path: Path) -> str:
    """Нормализованный путь сервера: `resolve()`, `os.path.normcase`, разделители `/`.

    Один и тот же файл (другой регистр, другой вид косой черты, симлинк) даёт одну строку.
    По ней считается хеш идентификатора и ищется уже открытый проект.
    """
    return os.path.normcase(str(path.resolve())).replace("\\", "/")


def path_digest(path_key: str) -> str:
    """SHA-256 нормализованного пути, шестнадцатеричная строка.

    Отдельная функция: тест подменяет её, чтобы проверить удлинение хеша при коллизии.
    """
    return hashlib.sha256(path_key.encode("utf-8")).hexdigest()


def normalize_relative(path: str) -> str:
    """Обратная косая в относительном пути — разделитель каталогов, не символ имени.

    Абсолютный путь Windows (`C:\\…`, `\\\\сервер\\…`) и POSIX (`/…`) не меняется:
    его переводит соответствие путей агента и сервера.
    """
    if PureWindowsPath(path).is_absolute() or PurePosixPath(path).is_absolute():
        return path
    return path.replace("\\", "/")


def _valid_id(value: str) -> bool:
    """Идентификатор — один компонент пути, без `.` и `..`."""
    return _ID_PART.fullmatch(value) is not None and value not in {".", ".."}


def _file_stem(path: Path, kind: str) -> str:
    """Имя файла без расширения либо латинский префикс вида правил.

    Кириллица и прочие символы в идентификатор не входят: клиенты MCP искажают такие
    аргументы. Тогда префикс — `exchange` или `registration`.
    """
    stem = path.stem
    if _valid_id(stem):
        return stem
    return kind


def _safe_token(value: str) -> str:
    """Фрагмент идентификатора: как есть, если он из допустимых символов, иначе 4 знака хеша."""
    if _valid_id(value):
        return value
    return path_digest(value)[:4]


def _safe_label(label: str) -> str:
    """Основа идентификатора без файла. Недопустимые символы заменяются хешем хвоста."""
    if _valid_id(label):
        return label
    prefix, separator, _rest = label.partition("-")
    head = prefix if separator and _valid_id(prefix) else "project"
    return f"{head}-{path_digest(label)[:4]}"


def _document_kind(document: RulesDocument) -> str:
    return "registration" if isinstance(document, RegistrationRules) else "exchange"


def _known(projects: dict[str, RulesProject]) -> str:
    return ", ".join(projects) or "нет открытых проектов"


def _source_stat(path: Path) -> tuple[int, int]:
    stat = path.stat()
    return stat.st_mtime_ns, stat.st_size


def _source_changed(project: RulesProject, source: Path) -> bool:
    try:
        mtime_ns, size = _source_stat(source)
    except OSError:
        return True
    return mtime_ns != project.source_mtime_ns or size != project.source_size


def _snapshot_error(project: RulesProject, path: Path, error: Exception) -> RulesFormatError:
    source = f", источник: {project.source_path}" if project.source_path else ""
    return RulesFormatError(f"Снимок «{path}» не разбирается{source}: {error}")


def _meta_path(value: object) -> Path | None:
    if not isinstance(value, str) or not value:
        return None
    return Path(value)


def _meta_int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _dump_meta(project: RulesProject) -> bytes:
    payload = {
        "version": _META_VERSION,
        "id": project.id,
        "kind": project.kind,
        "source_path": os.fspath(project.source_path) if project.source_path else None,
        "saved_path": os.fspath(project.saved_path) if project.saved_path else None,
        "modified": project.modified,
        "source_mtime_ns": project.source_mtime_ns,
        "source_size": project.source_size,
        "updated_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    return _json_bytes(payload)


def _dump_handlers(export: HandlerExport) -> bytes:
    payload = {
        "files": [
            {
                "path": os.fspath(item.path),
                "address": item.address,
                "event": item.event,
                "body_start": item.body_start,
                "body_end": item.body_end,
                "line_offset": item.line_offset,
            }
            for item in export.files
        ]
    }
    return _json_bytes(payload)


def _load_handlers(path: Path) -> HandlerExport:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("handlers.json: ожидается объект")
    raw_files = payload.get("files")
    if not isinstance(raw_files, list):
        raise ValueError("handlers.json: нет списка files")
    files: list[HandlerFile] = []
    for item in raw_files:
        if not isinstance(item, dict):
            raise ValueError("handlers.json: элемент files не объект")
        files.append(
            HandlerFile(
                path=Path(str(item["path"])),
                address=str(item["address"]),
                event=str(item["event"]),
                body_start=int(item["body_start"]),
                body_end=int(item["body_end"]),
                line_offset=int(item["line_offset"]),
            )
        )
    return HandlerExport(tuple(files))


def _json_bytes(payload: object) -> bytes:
    return (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def _atomic_write(path: Path, data: bytes) -> None:
    """Запись во временный файл того же каталога и `os.replace`."""
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _is_inside(path: Path, root: Path) -> bool:
    """Разрешённый путь лежит внутри корня и не совпадает с самим корнем."""
    folded_path = os.path.normcase(str(path))
    folded_root = os.path.normcase(str(root))
    if folded_path == folded_root:
        return False
    try:
        Path(folded_path).relative_to(folded_root)
    except ValueError:
        return False
    return True


def _empty_exchange(source: dict[str, str], target: dict[str, str]) -> ExchangeRules:
    """Заголовок новых правил обмена без правил — как его пишет `ВыгрузитьКонвертацию`.

    Контейнеры разделов в модели не создаются: писатель всё равно открывает их пустыми
    (ВК:1457–1490 — ПКО, ПВД, ПОД, алгоритмы, запросы; ВК:517 — обработки). У новой
    конвертации включена выгрузка параметров по версии 2.01
    (`Catalogs/Конвертации/Ext/ObjectModule.bsl:22-24`), поэтому пустой контейнер
    `Параметры` тоже пишется (ВК:482–512). Сериализатор выводит эти контейнеры по
    политике ALWAYS, даже если узла в модели нет.
    """
    root = Node.new("exchange_rules", "ПравилаОбмена")
    version = Node.new("format_version", "ВерсияФормата")
    # мВерсияФормата (ВК:2602), текст тега — ВыгрузитьДанныеВерсии (ВК:420).
    version.text = SUPPORTED_FORMAT_VERSION
    version.attrs["РежимСовместимости"] = _COMPATIBILITY
    root.children["ВерсияФормата"] = version
    root.values["Ид"] = _new_code()
    root.values["Наименование"] = _conversion_name(
        source.get("config_name", ""), target.get("config_name", "")
    )
    # ДатаОбновления = ТекущаяДата() (ВК:397), в XML — XMLСтрока этой даты (ВК:405, ВК:90).
    root.values["ДатаВремяСоздания"] = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    root.children["Источник"] = _config_side("Источник", source)
    root.children["Приемник"] = _config_side("Приемник", target)
    return ExchangeRules(root, KD_STYLE)


def _new_code() -> str:
    """Новый код конвертации: GUID, дополненный пробелами до длины кода.

    `СгенерироватьУникальныйКод` — `Catalogs/Конвертации/Ext/ObjectModule.bsl:10`
    (`Строка(Новый УникальныйИдентификатор())`). Писатель выводит `Строка(Код)` (ВК:403);
    код фиксированной длины хранится с хвостовыми пробелами.
    """
    return str(uuid.uuid4()).ljust(_CODE_LENGTH)


def _conversion_name(source_name: str, target_name: str) -> str:
    """Наименование новой конвертации: «Источник --> Приемник».

    `глНаименованиеКонвертации` (`CommonModules/ОбщегоНазначения/Ext/Module.bsl:141`).
    Пустое наименование перед записью заполняется так
    (`Catalogs/Конвертации/Ext/ObjectModule.bsl:45-47`), писатель выводит его (ВК:404).
    Представление конфигурации — её наименование, а выгрузка структуры кладёт туда имя
    конфигурации (`MD83Exp/ВыгрузкаМетаданных/Ext/ObjectModule.bsl:114-115`), то есть `config_name`.
    """
    return f"{source_name.strip()} --> {target_name.strip()}".strip()


def _config_side(tag: str, meta: dict[str, str]) -> Node:
    """`Источник` или `Приемник`: имя текстом, синоним и версия из meta структуры.

    Текст — `Конфигурация.Имя` (ВК:378), атрибуты синонима и версии — ВК:380–381.
    Версии платформы в структуре нет: писатель всегда ставит атрибут (ВК:379) из
    `ПолучитьПредставлениеПриложения` (ВК:2588–2596). Значение заполнения реквизита
    `Конфигурации.Приложение` не задано (`Catalogs/Конфигурации.xml:332`), пустое
    приложение даёт пустую строку (ВК:2595).
    """
    node = Node.new("config", tag)
    node.text = meta.get("config_name", "")
    node.attrs["ВерсияПлатформы"] = ""
    node.attrs["ВерсияКонфигурации"] = meta.get("config_version", "")
    node.attrs["СинонимКонфигурации"] = meta.get("config_synonym", "")
    return node
