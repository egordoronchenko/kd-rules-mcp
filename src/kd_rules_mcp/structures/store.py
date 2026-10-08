"""Кэш структур: один файл SQLite на структуру в каталоге кэша (design.md, Д3).

Идентификатор структуры задаёт вызывающий (например, `bp-ext`). Повторная загрузка того же
входа не разбирает его заново: для MD83Exp — тот же SHA-256 файла, для XML-выгрузки — тот же
отпечаток файлов выгрузок и список расширений; в обоих случаях ещё и та же версия кода загрузки.
Загрузка идёт во временный файл, который заменяет прежний только при успехе: ошибка входа не
портит сохранённую структуру.
"""

import hashlib
import json
import re
import sqlite3
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from types import ModuleType

from kd_rules_mcp.errors import Kd2Error, StructureNotFoundError
from kd_rules_mcp.structures import db, md83exp, xmlbuild, xmldump

_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")
_CHUNK = 1 << 20


def _code_version(*modules: ModuleType) -> str:
    """Версия кода загрузки — хеш исходников: изменился код — сохранённая структура не годится."""
    digest = hashlib.sha256()
    for module in modules:
        digest.update(Path(module.__file__ or "").read_bytes())
    return digest.hexdigest()[:16]


LOADER_VERSION = _code_version(db, md83exp)
BUILDER_VERSION = _code_version(db, xmldump, xmlbuild)


@dataclass(frozen=True, slots=True)
class LoadResult:
    """Итог загрузки структуры."""

    structure_id: str
    reused: bool
    counts: dict[str, int]
    elapsed_s: float
    message: str
    unresolved: dict[str, int] | None = None  # неразрешённые типы → число свойств с ними


def file_hash(path: Path) -> str:
    """SHA-256 содержимого файла (читается потоком)."""
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(_CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


# Файлы, которые Конфигуратор переписывает при каждой выгрузке в XML: версии всех объектов
# (`ConfigDumpInfo.xml`, атрибут `configVersion`) и свойства конфигурации.
DUMP_MARKERS = ("Configuration.xml", "ConfigDumpInfo.xml")


def dump_fingerprint(roots: Sequence[Path]) -> str:
    """Отпечаток XML-выгрузок без обхода всех файлов.

    Выгрузка с `ConfigDumpInfo.xml` описывается содержимым `Configuration.xml` и
    `ConfigDumpInfo.xml`: обход десятков тысяч файлов через монтирование Docker Desktop занимает
    минуты (docs/research/mcp-service.md, 7.2). Правку XML без повторной выгрузки Конфигуратором
    такой отпечаток не видит — для неё загрузка с `force`. Выгрузка без `ConfigDumpInfo.xml`
    отпечатывается целиком: пути, размеры и время изменения всех `*.xml`.
    """
    digest = hashlib.sha256()
    for root in roots:
        digest.update(str(root.resolve()).encode())
        if (root / DUMP_MARKERS[1]).is_file():
            for name in DUMP_MARKERS:
                digest.update(name.encode())
                digest.update(file_hash(root / name).encode())
            continue
        for path in sorted(root.rglob("*.xml")):
            stat = path.stat()
            digest.update(f"{path.relative_to(root)}|{stat.st_size}|{stat.st_mtime_ns}".encode())
    return digest.hexdigest()


class StructureStore:
    """Каталог кэша структур."""

    def __init__(self, cache_dir: Path) -> None:
        self.cache_dir = cache_dir
        cache_dir.mkdir(parents=True, exist_ok=True)

    def path(self, structure_id: str) -> Path:
        """Файл структуры; идентификатор — латиница, цифры, `_.-`, до 64 символов."""
        if not _ID.fullmatch(structure_id):
            raise Kd2Error(
                f"Недопустимый идентификатор структуры «{structure_id}»: нужны латиница, цифры"
                " и символы _.- (до 64 символов)"
            )
        return self.cache_dir / f"{structure_id}.sqlite"

    def exists(self, structure_id: str) -> bool:
        """Есть ли структура в кэше."""
        return self.path(structure_id).is_file()

    def ids(self) -> list[str]:
        """Идентификаторы структур в кэше."""
        return sorted(p.stem for p in self.cache_dir.glob("*.sqlite"))

    def open(self, structure_id: str) -> sqlite3.Connection:
        """Соединение только на чтение; нет структуры — ошибка со списком имеющихся."""
        path = self.path(structure_id)
        if not path.is_file():
            known = ", ".join(self.ids()) or "кэш пуст"
            raise StructureNotFoundError(f"Структуры «{structure_id}» нет в кэше ({known})")
        return db.open_readonly(path)

    def meta(self, structure_id: str) -> dict[str, str]:
        """Метаданные загрузки структуры."""
        connection = self.open(structure_id)
        try:
            return db.read_meta(connection)
        finally:
            connection.close()

    def _reusable(self, structure_id: str, expected: dict[str, str]) -> LoadResult | None:
        """Сохранённая структура, если её вход и версия кода совпадают с ожидаемыми."""
        if not self.exists(structure_id):
            return None
        meta = self.meta(structure_id)
        if meta.get("schema_version") != db.SCHEMA_VERSION:
            return None
        if any(meta.get(key) != value for key, value in expected.items()):
            return None
        return LoadResult(
            structure_id,
            reused=True,
            counts=json.loads(meta.get("counts", "{}")),
            elapsed_s=0.0,
            message="Использована сохранённая структура: вход не изменился",
            unresolved=json.loads(meta.get("unresolved", "{}")),
        )

    def _save(
        self,
        structure_id: str,
        fill: Callable[[sqlite3.Connection], dict[str, str]],
    ) -> dict[str, str]:
        """Заполняет структуру во временном файле и атомарно заменяет прежнюю."""
        target = self.path(structure_id)
        temporary = target.with_suffix(".part")
        connection = db.create(temporary)
        try:
            with connection:
                meta = fill(connection)
                meta |= {
                    "structure_id": structure_id,
                    "loaded_at": datetime.now().isoformat(timespec="seconds"),
                }
                db.write_meta(connection, meta)
            connection.execute("VACUUM")
        except BaseException:
            connection.close()
            temporary.unlink(missing_ok=True)
            raise
        connection.close()
        temporary.replace(target)
        return meta

    def load_md83exp(self, structure_id: str, source: Path, *, force: bool = False) -> LoadResult:
        """Загружает выгрузку MD83Exp под идентификатором; тот же вход повторно не разбирается.

        `force` — разобрать заново, даже если вход не изменился.
        """
        self.path(structure_id)
        if not source.is_file():
            raise Kd2Error(f"Файл структуры не найден: {source}")
        expected = {
            "source": "md83exp",
            "input_hash": file_hash(source),
            "loader_version": LOADER_VERSION,
        }
        reused = None if force else self._reusable(structure_id, expected)
        if reused is not None:
            return reused

        reports: list[md83exp.LoadReport] = []

        def fill(connection: sqlite3.Connection) -> dict[str, str]:
            report = md83exp.load(source, connection)
            reports.append(report)
            return expected | {
                "source_path": str(source),
                "extensions": "[]",
                "elapsed_s": str(report.elapsed_s),
                "unresolved": json.dumps(report.unresolved_types, ensure_ascii=False),
            }

        self._save(structure_id, fill)
        report = reports[0]
        unresolved = len(report.unresolved_types)
        note = f"; неразрешённых типов: {unresolved}" if unresolved else ""
        return LoadResult(
            structure_id,
            reused=False,
            counts=report.counts(),
            elapsed_s=report.elapsed_s,
            message=f"Структура загружена из {source.name} за {report.elapsed_s} с{note}",
            unresolved=dict(report.unresolved_types),
        )

    def load_xml(
        self,
        structure_id: str,
        main: Path,
        extensions: Sequence[Path] = (),
        *,
        force: bool = False,
    ) -> LoadResult:
        """Собирает структуру из XML-выгрузки конфигурации и явно перечисленных расширений.

        Расширения накладываются в заданном порядке; выгрузки, которых нет в списке, не читаются.
        Изменение выгрузки определяется по `dump_fingerprint`;
        `force` — собрать заново в любом случае.
        """
        self.path(structure_id)
        roots = [main, *extensions]
        for root in roots:
            if not (root / "Configuration.xml").is_file():
                raise Kd2Error(f"Нет выгрузки конфигурации: {root / 'Configuration.xml'}")
        expected = {
            "source": "xml",
            "input_hash": dump_fingerprint(roots),
            "loader_version": BUILDER_VERSION,
        }
        reused = None if force else self._reusable(structure_id, expected)
        if reused is not None:
            return reused
        started = time.monotonic()
        dumps = [xmldump.read_dump(root) for root in roots]
        metadata = xmlbuild.Metadata(dumps[0], dumps[1:])

        reports: list[xmlbuild.BuildReport] = []

        def fill(connection: sqlite3.Connection) -> dict[str, str]:
            report = xmlbuild.build(metadata, connection)
            reports.append(report)
            return expected | {
                "source_path": str(main),
                "config_name": report.config_name,
                "config_synonym": report.config_synonym,
                "config_version": report.config_version,
                "extensions": json.dumps(report.extensions, ensure_ascii=False),
                "counts": json.dumps(report.counts()),
                "elapsed_s": str(round(time.monotonic() - started, 1)),
                "unresolved": json.dumps(report.unresolved_types, ensure_ascii=False),
            }

        meta = self._save(structure_id, fill)
        report = reports[0]
        unresolved = len(report.unresolved_types)
        note = f"; неразрешённых типов: {unresolved}" if unresolved else ""
        applied = f" с расширениями {', '.join(report.extensions)}" if report.extensions else ""
        return LoadResult(
            structure_id,
            reused=False,
            counts=report.counts(),
            elapsed_s=float(meta["elapsed_s"]),
            message=f"Структура собрана из {main.name}{applied} за {meta['elapsed_s']} с{note}",
            unresolved=dict(report.unresolved_types),
        )
