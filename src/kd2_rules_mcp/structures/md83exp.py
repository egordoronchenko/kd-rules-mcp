"""Потоковый загрузчик выгрузки структуры метаданных MD83Exp в SQLite.

Файл — плоская последовательность записей `CatalogObject.Конфигурации / Объекты / Свойства /
Значения`, связанных GUID одного прогона (дайджест §1.1–1.3; писатель —
`reference/kd2-dist-src/MD83Exp/ВыгрузкаМетаданных/Ext/ObjectModule.bsl`).

КД читает файл в два прохода, чтобы ссылки на ещё не прочитанные объекты разрешались
(`ЗагрузкаСтруктурыМетаданных`, строки 19–151). Здесь один потоковый проход складывает записи в
промежуточные таблицы с GUID, а ссылки (владелец, родитель, типы) разрешаются в конце запросами —
результат тот же, файл читается один раз. В памяти — только соответствие GUID типа → имя типа.
"""

import json
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from lxml import etree

from kd2_rules_mcp.errors import StructureFormatError
from kd2_rules_mcp.structures import db

ROOT_TAG = "Конфигурация"
CONFIG_TAG = "CatalogObject.Конфигурации"
OBJECT_TAG = "CatalogObject.Объекты"
PROPERTY_TAG = "CatalogObject.Свойства"
VALUE_TAG = "CatalogObject.Значения"
EMPTY_REF = "00000000-0000-0000-0000-000000000000"

# Поля записи Объекты для objects.attrs (ВыгрузитьНастраиваемыеСвойстваОбъекта, 275-406).
OBJECT_ATTRS = (
    "Иерархический",
    "ВидИерархии",
    "ОграничиватьКоличествоУровней",
    "КоличествоУровней",
    "СерииКодов",
    "КонтрольУникальности",
    "АвтоНумерация",
    "Периодичность",
    "Подчиненный",
)

STAGING = """
CREATE TEMP TABLE stg_objects (id INTEGER PRIMARY KEY, ref TEXT NOT NULL, parent_ref TEXT);
CREATE TEMP TABLE stg_properties (
    id INTEGER PRIMARY KEY, ref TEXT NOT NULL, owner_ref TEXT NOT NULL, parent_ref TEXT,
    type_refs TEXT NOT NULL
);
CREATE TEMP TABLE stg_values (
    id INTEGER PRIMARY KEY, ref TEXT NOT NULL, owner_ref TEXT NOT NULL, parent_ref TEXT,
    type_refs TEXT NOT NULL
);
"""


@dataclass(slots=True)
class LoadReport:
    """Итог загрузки: счётчики и неразрешённые ссылки."""

    config_name: str = ""
    config_synonym: str = ""
    config_version: str = ""
    objects: int = 0
    properties: int = 0
    values: int = 0
    type_sets: int = 0
    unresolved_types: dict[str, int] = field(default_factory=dict)
    orphans: int = 0  # свойства и значения без найденного владельца (пропущены)
    elapsed_s: float = 0.0

    def counts(self) -> dict[str, int]:
        """Счётчики для метаданных структуры."""
        return {
            "objects": self.objects,
            "properties": self.properties,
            "values": self.values,
            "type_sets": self.type_sets,
            "unresolved_types": len(self.unresolved_types),
            "orphans": self.orphans,
        }


def _fields(element: etree._Element) -> dict[str, str]:
    """Простые дочерние теги записи: тег → текст."""
    return {str(child.tag): child.text or "" for child in element if len(child) == 0}


def _type_refs(element: etree._Element) -> str:
    return "\n".join(row.findtext("Тип") or "" for row in element.iterfind("Типы/Row"))


def _ref_or_none(value: str) -> str | None:
    return None if not value or value == EMPTY_REF else value


def _flag(value: str) -> int:
    return 1 if value == "true" else 0


def _int(value: str) -> int:
    return int(value) if value.lstrip("-").isdigit() else 0


class _Loader:
    """Состояние одного прохода по файлу."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection
        self.report = LoadReport()
        self.type_names: dict[str, str] = {}
        self.seen_config = False

    def record(self, element: etree._Element) -> None:
        tag = str(element.tag)
        if not self.seen_config:
            if tag != CONFIG_TAG:
                raise StructureFormatError(
                    f"Первая запись файла — «{tag}», ожидалась «{CONFIG_TAG}»:"
                    " файл не похож на выгрузку MD83Exp"
                )
            self.seen_config = True
            self._config(element)
        elif tag == OBJECT_TAG:
            self._object(element)
        elif tag == PROPERTY_TAG:
            self._property(element)
        elif tag == VALUE_TAG:
            self._value(element)

    def _config(self, element: etree._Element) -> None:
        values = _fields(element)
        self.report.config_name = values.get("Имя", "")
        self.report.config_synonym = values.get("Синоним", "")
        self.report.config_version = values.get("Версия", "")

    def _object(self, element: etree._Element) -> None:
        values = _fields(element)
        ref = values.get("Ref", "")
        type_name = values.get("Description", "")
        self.type_names[ref] = type_name
        attrs = {key: values[key] for key in OBJECT_ATTRS if key in values}
        cursor = self.connection.execute(
            "INSERT INTO objects (kind, name, type_name, synonym, comment, is_group, attrs)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                values.get("Тип", ""),
                values.get("Имя", ""),
                type_name,
                values.get("Синоним", ""),
                values.get("Комментарий", ""),
                _flag(values.get("IsFolder", "")),
                json.dumps(attrs, ensure_ascii=False),
            ),
        )
        self.connection.execute(
            "INSERT INTO stg_objects VALUES (?, ?, ?)",
            (cursor.lastrowid, ref, _ref_or_none(values.get("Parent", ""))),
        )

    def _property(self, element: etree._Element) -> None:
        values = _fields(element)
        cursor = self.connection.execute(
            "INSERT INTO properties (object_id, kind, name, path, synonym, comment, is_group, code,"
            " number_length, number_precision, number_nonnegative, string_length, string_fixed,"
            " date_parts, usage, indexing, autoregistration)"
            " VALUES (0, ?, ?, '', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                values.get("Вид", ""),
                values.get("Description", ""),
                values.get("Синоним", ""),
                values.get("Комментарий", ""),
                _flag(values.get("IsFolder", "")),
                values.get("Code", ""),
                _int(values.get("КвалификаторыЧисла_Длина", "")),
                _int(values.get("КвалификаторыЧисла_Точность", "")),
                _flag(values.get("КвалификаторыЧисла_Неотрицательное", "")),
                _int(values.get("КвалификаторыСтроки_Длина", "")),
                _flag(values.get("КвалификаторыСтроки_Фиксированная", "")),
                values.get("КвалификаторыДаты_Состав", ""),
                values.get("Использование", ""),
                _flag(values.get("Индексирование", "")),
                _flag(values.get("Авторегистрация", "")),
            ),
        )
        self.connection.execute(
            "INSERT INTO stg_properties VALUES (?, ?, ?, ?, ?)",
            (
                cursor.lastrowid,
                values.get("Ref", ""),
                element.findtext("Owner") or "",
                _ref_or_none(values.get("Parent", "")),
                _type_refs(element),
            ),
        )

    def _value(self, element: etree._Element) -> None:
        values = _fields(element)
        cursor = self.connection.execute(
            "INSERT INTO object_values (object_id, name, synonym, comment, code, predefined)"
            " VALUES (0, ?, ?, ?, ?, ?)",
            (
                values.get("Description", ""),
                values.get("Синоним", ""),
                values.get("Комментарий", ""),
                values.get("Code", ""),
                _flag(values.get("Предопределенное", "")),
            ),
        )
        self.connection.execute(
            "INSERT INTO stg_values VALUES (?, ?, ?, ?, ?)",
            (
                cursor.lastrowid,
                values.get("Ref", ""),
                element.findtext("Owner") or "",
                _ref_or_none(values.get("Parent", "")),
                _type_refs(element),
            ),
        )

    # --- Разрешение ссылок после прохода ---------------------------------------------------

    def resolve(self) -> None:
        run = self.connection.executescript
        run(
            """
            CREATE INDEX stg_objects_ref ON stg_objects(ref);
            CREATE INDEX stg_properties_ref ON stg_properties(ref);
            CREATE INDEX stg_values_ref ON stg_values(ref);
            UPDATE objects SET group_id = (
                SELECT g.id FROM stg_objects s JOIN stg_objects g ON g.ref = s.parent_ref
                WHERE s.id = objects.id);
            UPDATE properties SET
                object_id = COALESCE((SELECT o.id FROM stg_properties s
                    JOIN stg_objects o ON o.ref = s.owner_ref WHERE s.id = properties.id), 0),
                parent_id = (SELECT p.id FROM stg_properties s
                    JOIN stg_properties p ON p.ref = s.parent_ref WHERE s.id = properties.id);
            UPDATE object_values SET
                object_id = COALESCE((SELECT o.id FROM stg_values s
                    JOIN stg_objects o ON o.ref = s.owner_ref WHERE s.id = object_values.id), 0),
                parent_id = (SELECT p.id FROM stg_values s
                    JOIN stg_values p ON p.ref = s.parent_ref WHERE s.id = object_values.id);
            """
        )
        orphans = self.connection.execute(
            "SELECT (SELECT COUNT(*) FROM properties WHERE object_id = 0)"
            " + (SELECT COUNT(*) FROM object_values WHERE object_id = 0)"
        ).fetchone()[0]
        self.report.orphans = orphans
        self.connection.execute("DELETE FROM properties WHERE object_id = 0")
        self.connection.execute("DELETE FROM object_values WHERE object_id = 0")
        self._resolve_types("properties", "stg_properties")
        self._resolve_types("object_values", "stg_values")
        self._build_paths()

    def _resolve_types(self, table: str, staging: str) -> None:
        sets: dict[str, int] = {
            types: set_id
            for set_id, types in self.connection.execute("SELECT id, types FROM type_sets")
        }
        updates: list[tuple[int | None, str | None, int]] = []
        rows = self.connection.execute(f"SELECT id, type_refs FROM {staging}").fetchall()
        for row_id, type_refs in rows:
            names: set[str] = set()
            unresolved: list[str] = []
            for ref in filter(None, type_refs.split("\n")):
                name = self.type_names.get(ref)
                if name is None:
                    unresolved.append(ref)
                    self.report.unresolved_types[ref] = self.report.unresolved_types.get(ref, 0) + 1
                else:
                    names.add(name)
            set_id = None
            if names:
                key = "\n".join(sorted(names))
                set_id = sets.get(key)
                if set_id is None:
                    cursor = self.connection.execute(
                        "INSERT INTO type_sets (types) VALUES (?)", (key,)
                    )
                    set_id = int(cursor.lastrowid or 0)
                    sets[key] = set_id
                    self.connection.executemany(
                        "INSERT INTO type_set_items VALUES (?, ?)",
                        [(set_id, name) for name in sorted(names)],
                    )
            updates.append((set_id, "\n".join(unresolved) or None, row_id))
        self.connection.executemany(
            f"UPDATE {table} SET type_set_id = ?, unresolved = ? WHERE id = ?", updates
        )

    def _build_paths(self) -> None:
        rows = self.connection.execute("SELECT id, parent_id, name FROM properties").fetchall()
        parents = {row_id: (parent_id, name) for row_id, parent_id, name in rows}
        paths: dict[int, str] = {}

        def path_of(row_id: int) -> str:
            known = paths.get(row_id)
            if known is not None:
                return known
            parent_id, name = parents[row_id]
            path = (
                name
                if parent_id is None or parent_id not in parents
                else (f"{path_of(parent_id)}.{name}")
            )
            paths[row_id] = path
            return path

        self.connection.executemany(
            "UPDATE properties SET path = ? WHERE id = ?",
            [(path_of(row_id), row_id) for row_id in parents],
        )


def load(
    source: Path,
    connection: sqlite3.Connection,
    progress: Callable[[int], None] | None = None,
) -> LoadReport:
    """Загружает MD83Exp из `source` в пустую базу структуры `connection`."""
    started = time.monotonic()
    connection.executescript(STAGING)
    loader = _Loader(connection)
    depth = 0
    records = 0
    try:
        for event, element in etree.iterparse(
            str(source), events=("start", "end"), huge_tree=True, resolve_entities=False
        ):
            if event == "start":
                depth += 1
                if depth == 1 and element.tag != ROOT_TAG:
                    raise StructureFormatError(
                        f"Корневой элемент «{element.tag}», ожидался «{ROOT_TAG}»:"
                        " файл не является выгрузкой MD83Exp"
                    )
                continue
            if depth == 2:
                loader.record(element)
                records += 1
                if progress is not None and records % 10000 == 0:
                    progress(records)
                element.clear()
                parent = element.getparent()
                if parent is not None:
                    while element.getprevious() is not None:
                        del parent[0]
            depth -= 1
    except etree.XMLSyntaxError as error:
        raise StructureFormatError(f"Файл структуры не является корректным XML: {error}") from error
    if not loader.seen_config:
        raise StructureFormatError(f"В файле нет записи «{CONFIG_TAG}»: это не выгрузка MD83Exp")
    loader.resolve()
    report = loader.report
    count = connection.execute
    report.objects = count("SELECT COUNT(*) FROM objects").fetchone()[0]
    report.properties = count("SELECT COUNT(*) FROM properties").fetchone()[0]
    report.values = count("SELECT COUNT(*) FROM object_values").fetchone()[0]
    report.type_sets = count("SELECT COUNT(*) FROM type_sets").fetchone()[0]
    report.elapsed_s = round(time.monotonic() - started, 1)
    db.write_meta(
        connection,
        {
            "config_name": report.config_name,
            "config_synonym": report.config_synonym,
            "config_version": report.config_version,
            "counts": json.dumps(report.counts()),
        },
    )
    return report
