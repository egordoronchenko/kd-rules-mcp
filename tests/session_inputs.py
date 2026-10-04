"""Общие входы тестов: один разбор на сеанс, ключ — путь и время изменения файла.

Неизменяемые снимки (модуль менеджера, профиль маршрутов, схема формата, менеджер
регистрации) возвращаются тем же объектом. Правила КД 2 копируются: модель дописывает
недостающий раздел и её правят тесты авторинга. Структура конфигурации корпуса
собирается один раз, дальше в хранилище копируется готовый файл SQLite: наложение
расширений меняет объекты выгрузки, поэтому сам разбор файлов между тестами не делится.
Каталог копий удаляется в конце сеанса.

Профили маршрутов и сборка структур кэшируются только для корней корпуса. Синтетические
выгрузки тесты переписывают между чтениями, и их разбор остаётся отдельным.
"""

from __future__ import annotations

import hashlib
import importlib
import os
import shutil
import sqlite3
import tempfile
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path

from kd2_rules_mcp.ed.model import EdDocument
from kd2_rules_mcp.ed.registration_model import RegistrationModuleDocument
from kd2_rules_mcp.ed.route_model import RouteProfile
from kd2_rules_mcp.ed.schema.model import EdSchema, SchemaPackage
from kd2_rules_mcp.errors import Kd2Error
from kd2_rules_mcp.kd2.model import ExchangeRules, RegistrationRules
from kd2_rules_mcp.structures.store import (
    BUILDER_VERSION,
    LoadResult,
    StructureStore,
    dump_fingerprint,
)

_FileKey = tuple[str, int, int]
_Rules = ExchangeRules | RegistrationRules

_INSTALLED = False
_TEMP: Path | None = None
_STORE: StructureStore | None = None
_ROOTS: set[Path] | None = None

_orig_read_package: Callable[..., SchemaPackage] | None = None
_orig_load_schema: Callable[..., EdSchema] | None = None
_orig_read_manager: Callable[..., EdDocument] | None = None
_orig_read_routes: Callable[..., RouteProfile] | None = None
_orig_load_rules: Callable[..., _Rules] | None = None
_orig_read_registration: Callable[..., RegistrationModuleDocument] | None = None
_orig_load_xml: Callable[..., LoadResult] | None = None

_packages: dict[tuple[_FileKey, str, int, int], SchemaPackage] = {}
_schemas: list[_SchemaEntry] = []
_managers: dict[_FileKey, EdDocument] = {}
_routes: dict[tuple[str, tuple[_FileKey, ...]], RouteProfile] = {}
_rules: dict[tuple[object, ...], _Rules] = {}
_registration: dict[_FileKey, RegistrationModuleDocument] = {}
_structures: dict[str, _BuiltStructure] = {}


@dataclass(frozen=True, slots=True)
class _SchemaEntry:
    """Схема и отпечатки файлов, из которых она собрана, включая импорты."""

    base: _FileKey
    extensions: tuple[_FileKey, ...]
    located: bool
    limits: tuple[int, ...]
    imports: tuple[tuple[str, _FileKey | None], ...]
    schema: EdSchema
    # Все файлы, которые схема реально прочитала: `path` может быть описанием пакета, а меняется
    # лежащий рядом `Package.bin` — по одному ключу `base` такая правка не видна.
    sources: tuple[_FileKey, ...] = ()


@dataclass(frozen=True, slots=True)
class _BuiltStructure:
    """Готовый файл структуры и поля ответа первой сборки."""

    path: Path
    counts: dict[str, int]
    unresolved: dict[str, int] | None
    message: str
    elapsed_s: float


_VOLATILE_ROOT = Path(tempfile.gettempdir()).resolve()


def _file_key(path: Path) -> _FileKey:
    """Путь, время изменения и размер: правка файла меняет ключ.

    Файл во временном каталоге ключа не получает (`OSError` — обёртки тогда читают по-настоящему):
    такие выгрузки тесты переписывают между вызовами и проверяют на них именно повторное чтение,
    свежесть и учёт прочитанных файлов в сервисе. Кэшируются корпус и статические данные тестов.
    """
    resolved = path.resolve()
    if resolved.is_relative_to(_VOLATILE_ROOT):
        raise OSError("временный файл теста не кэшируется")
    stat = resolved.stat()
    return (str(resolved), stat.st_mtime_ns, stat.st_size)


def _corpus_roots() -> set[Path]:
    """Каталоги основных выгрузок корпуса. Считаются один раз, после настройки окружения."""
    global _ROOTS
    if _ROOTS is not None:
        return _ROOTS
    roots: set[Path] = set()
    # Закрытых тестов в открытой копии репозитория нет: импорт по имени, без них — только
    # каталоги из KD2_CORPUS_DIRS.
    try:
        private = importlib.import_module("tests.private.corpus_private")
    except ImportError:
        private = None
    if private is not None:
        for name in private.PROJECTS:
            path = private.project_dir(name)
            if path.exists():
                roots.add(path.resolve())
    for raw in os.environ.get("KD2_CORPUS_DIRS", "").split(os.pathsep):
        if not raw.strip():
            continue
        path = Path(raw.strip())
        if path.exists():
            roots.add(path.resolve())
    _ROOTS = roots
    return roots


def _is_corpus_main(path: Path) -> bool:
    try:
        return path.resolve() in _corpus_roots()
    except OSError:
        return False


def _dump_stamp(root: Path) -> tuple[_FileKey, ...]:
    """Отпечаток выгрузки без обхода всех файлов: описание и, если есть, ConfigDumpInfo."""
    parts = [_file_key(root / "Configuration.xml")]
    info = root / "ConfigDumpInfo.xml"
    if info.is_file():
        parts.append(_file_key(info))
    return tuple(parts)


def _temp_dir() -> Path:
    global _TEMP
    if _TEMP is None:
        _TEMP = Path(tempfile.mkdtemp(prefix="kd2-pytest-session-"))
    return _TEMP


def cleanup() -> None:
    """Удаляет копии структур, собранные за сеанс."""
    global _TEMP, _STORE
    if _TEMP is not None:
        shutil.rmtree(_TEMP, ignore_errors=True)
        _TEMP = None
        _STORE = None


def session_store() -> StructureStore:
    """Хранилище структур сеанса. Файл удаляется в `cleanup`."""
    global _STORE
    if _STORE is None:
        _STORE = StructureStore(_temp_dir() / "structures")
    return _STORE


@contextmanager
def corpus_structure(root: Path) -> Iterator[sqlite3.Connection]:
    """Соединение со структурой основной выгрузки. Сборка одного отпечатка — один раз."""
    store = session_store()
    structure_id = "c-" + hashlib.sha256(str(Path(root).resolve()).encode()).hexdigest()[:16]
    store.load_xml(structure_id, Path(root))
    connection = store.open(structure_id)
    try:
        yield connection
    finally:
        connection.close()


def _cached_read_package(path: Path, role: str = "base") -> SchemaPackage:
    assert _orig_read_package is not None
    import kd2_rules_mcp.ed.schema.xdto as xdto

    try:
        # Пределы размера и глубины тесты подменяют и ждут отказ, а не прошлый пакет.
        key = (_file_key(Path(path)), role, xdto.MAX_FILE_BYTES, xdto.MAX_XML_DEPTH)
    except OSError:
        return _orig_read_package(path, role)
    found = _packages.get(key)
    if found is not None:
        return found
    package = _orig_read_package(path, role)
    _packages[key] = package
    return package


def _schema_limits() -> tuple[int, ...]:
    """Пределы резолвера входят в ключ: тесты подменяют их и ждут ошибку, а не прошлую схему."""
    import kd2_rules_mcp.ed.schema.resolver as resolver

    return (
        resolver.MAX_TOTAL_BYTES,
        resolver.MAX_FILES,
        resolver.MAX_TYPES,
        resolver.MAX_PROPERTIES,
        resolver.MAX_RESOLUTION_DEPTH,
    )


def _imports_match(entry: _SchemaEntry, locate_import: Callable[[str], Path | None] | None) -> bool:
    """Тот же набор файлов импорта. Вызов без локатора не совпадает с вызовом с локатором."""
    if entry.located != (locate_import is not None):
        return False
    if locate_import is None:
        return True
    for uri, key in entry.imports:
        found = locate_import(uri)
        if found is None:
            if key is not None:
                return False
            continue
        try:
            current = _file_key(Path(found))
        except OSError:
            return False
        if current != key:
            return False
    return True


def _cached_load_schema(
    path: Path,
    *,
    extensions: tuple[Path, ...] = (),
    locate_import: Callable[[str], Path | None] | None = None,
) -> EdSchema:
    assert _orig_load_schema is not None
    try:
        base = _file_key(Path(path))
        extension_keys = tuple(_file_key(Path(item)) for item in extensions)
    except OSError:
        return _orig_load_schema(path, extensions=extensions, locate_import=locate_import)
    limits = _schema_limits()
    for entry in _schemas:
        if (
            entry.base == base
            and entry.extensions == extension_keys
            and entry.limits == limits
            and _imports_match(entry, locate_import)
            and _sources_unchanged(entry)
        ):
            return entry.schema
    recorded: list[tuple[str, _FileKey | None]] = []

    def tracking(uri: str) -> Path | None:
        if locate_import is None:
            return None
        found = locate_import(uri)
        if found is None:
            recorded.append((uri, None))
            return None
        recorded.append((uri, _file_key(Path(found))))
        return found

    schema = _orig_load_schema(
        path,
        extensions=extensions,
        locate_import=tracking if locate_import is not None else None,
    )
    _schemas.append(
        _SchemaEntry(
            base,
            extension_keys,
            locate_import is not None,
            limits,
            tuple(recorded),
            schema,
            _source_keys(schema),
        )
    )
    return schema


def _source_keys(schema: EdSchema) -> tuple[_FileKey, ...]:
    """Отпечатки всех файлов-источников схемы; недоступный файл — пустой ключ, кэш не совпадёт."""
    keys: list[_FileKey] = []
    for package in schema.packages:
        for source in package.sources:
            try:
                keys.append(_file_key(Path(source.path)))
            except OSError:
                keys.append(("", 0, 0))
    return tuple(keys)


def _sources_unchanged(entry: _SchemaEntry) -> bool:
    return bool(entry.sources) and _source_keys(entry.schema) == entry.sources


def _cached_read_manager(path: str | Path) -> EdDocument:
    assert _orig_read_manager is not None
    try:
        key = _file_key(Path(path))
    except OSError:
        return _orig_read_manager(path)
    found = _managers.get(key)
    if found is not None:
        return found
    document = _orig_read_manager(path)
    _managers[key] = document
    return document


def _cached_read_routes(root: Path) -> RouteProfile:
    assert _orig_read_routes is not None
    root = Path(root)
    if not _is_corpus_main(root):
        return _orig_read_routes(root)
    try:
        key = (str(root.resolve()), _dump_stamp(root))
    except OSError:
        return _orig_read_routes(root)
    found = _routes.get(key)
    if found is not None:
        return found
    profile = _orig_read_routes(root)
    _routes[key] = profile
    return profile


def _rules_key(source: bytes | Path) -> tuple[object, ...] | None:
    if isinstance(source, Path):
        try:
            return ("path", *_file_key(source))
        except OSError:
            return None
    if isinstance(source, bytes | bytearray):
        data = bytes(source)
        return ("bytes", hashlib.sha256(data).hexdigest(), len(data))
    return None


def _cached_load_rules(source: bytes | Path) -> _Rules:
    assert _orig_load_rules is not None
    key = _rules_key(source)
    if key is not None and key in _rules:
        return deepcopy(_rules[key])
    document = _orig_load_rules(source)
    if key is None:
        return document
    try:
        copy = deepcopy(document)
    except Exception:
        return document
    _rules[key] = document
    return copy


def _cached_read_registration(path: str | Path) -> RegistrationModuleDocument:
    assert _orig_read_registration is not None
    try:
        key = _file_key(Path(path))
    except OSError:
        return _orig_read_registration(path)
    found = _registration.get(key)
    if found is not None:
        return found
    document = _orig_read_registration(path)
    _registration[key] = document
    return document


def _remember_structure(
    store: StructureStore,
    structure_id: str,
    main: Path,
    extensions: Sequence[Path],
    result: LoadResult,
) -> None:
    try:
        key = dump_fingerprint([Path(main), *(Path(item) for item in extensions)])
        source = store.path(structure_id)
        if not source.is_file():
            return
        blob = _temp_dir() / "blobs" / f"{key}.sqlite"
        blob.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, blob)
    except (OSError, Kd2Error):
        return
    unresolved = None if result.unresolved is None else dict(result.unresolved)
    _structures[key] = _BuiltStructure(
        blob, dict(result.counts), unresolved, result.message, result.elapsed_s
    )


def _materialize(store: StructureStore, structure_id: str, built: _BuiltStructure) -> LoadResult:
    target = store.path(structure_id)
    target.unlink(missing_ok=True)
    shutil.copy2(built.path, target)
    connection = sqlite3.connect(target)
    try:
        connection.execute(
            "INSERT OR REPLACE INTO meta VALUES (?, ?)", ("structure_id", structure_id)
        )
        connection.commit()
    finally:
        connection.close()
    unresolved = None if built.unresolved is None else dict(built.unresolved)
    return LoadResult(
        structure_id,
        reused=False,
        counts=dict(built.counts),
        elapsed_s=built.elapsed_s,
        message=built.message,
        unresolved=unresolved,
    )


def _cached_load_xml(
    self: StructureStore,
    structure_id: str,
    main: Path,
    extensions: Sequence[Path] = (),
    *,
    force: bool = False,
) -> LoadResult:
    assert _orig_load_xml is not None
    main_path = Path(main)
    if not _is_corpus_main(main_path):
        return _orig_load_xml(self, structure_id, main, extensions, force=force)
    if force:
        result = _orig_load_xml(self, structure_id, main, extensions, force=True)
        _remember_structure(self, structure_id, main_path, extensions, result)
        return result
    roots = [main_path, *(Path(item) for item in extensions)]
    if any(not (root / "Configuration.xml").is_file() for root in roots):
        return _orig_load_xml(self, structure_id, main, extensions, force=False)
    try:
        key = dump_fingerprint(roots)
        have = self.exists(structure_id)
    except (OSError, Kd2Error):
        return _orig_load_xml(self, structure_id, main, extensions, force=False)
    if have:
        reused = self._reusable(
            structure_id,
            {"source": "xml", "input_hash": key, "loader_version": BUILDER_VERSION},
        )
        if reused is not None:
            return reused
    built = _structures.get(key)
    if built is not None:
        return _materialize(self, structure_id, built)
    result = _orig_load_xml(self, structure_id, main, extensions, force=False)
    _remember_structure(self, structure_id, main_path, extensions, result)
    return result


def install() -> None:
    """Подменяет читатели до импорта тестов. Повторный вызов ничего не делает."""
    global _INSTALLED
    global _orig_read_package, _orig_load_schema, _orig_read_manager, _orig_read_routes
    global _orig_load_rules, _orig_read_registration, _orig_load_xml
    if _INSTALLED:
        return
    _INSTALLED = True

    import kd2_rules_mcp.ed as ed
    import kd2_rules_mcp.ed.reader as reader
    import kd2_rules_mcp.ed.registration as registration
    import kd2_rules_mcp.ed.routes as routes
    import kd2_rules_mcp.ed.schema as schema
    import kd2_rules_mcp.ed.schema.resolver as resolver
    import kd2_rules_mcp.ed.schema.xdto as xdto
    import kd2_rules_mcp.kd2.rules_io as rules_io
    import kd2_rules_mcp.structures.store as store

    # Резолвер вызывает `read_package` по глобальному имени: подменяются оба места.
    _orig_read_package = xdto.read_package
    xdto.read_package = _cached_read_package
    resolver.read_package = _cached_read_package
    _orig_load_schema = resolver.load_schema
    resolver.load_schema = _cached_load_schema
    schema.load_schema = _cached_load_schema

    _orig_read_manager = reader.read_manager
    reader.read_manager = _cached_read_manager
    ed.read_manager = _cached_read_manager

    # Чтение маршрутов не подменяется: сервис маршрутов строит читатель из исходной функции
    # (учёт прочитанных файлов и уже открытых документов), обёртка это ломает.
    _orig_read_routes = routes.read_routes

    _orig_load_rules = rules_io.load_rules
    rules_io.load_rules = _cached_load_rules

    _orig_read_registration = registration.read_registration_manager
    registration.read_registration_manager = _cached_read_registration

    _orig_load_xml = store.StructureStore.load_xml
    store.StructureStore.load_xml = _cached_load_xml
