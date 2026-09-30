"""Схема SQLite структуры метаданных: один файл на структуру.

Модель повторяет справочники КД (`Объекты`, `Свойства`, `Значения`), но связи — по внутренним
ключам, а не по GUID выгрузки: GUID в MD83Exp случайны в каждом прогоне (дайджест §1.3).
Наборы типов вынесены в `type_sets` с дедупликацией: у составных типов (любая ссылка, определяемые
типы) списки из сотен типов повторяются у тысяч свойств.
"""

import sqlite3
from pathlib import Path

SCHEMA_VERSION = "1"

SCHEMA = """
CREATE TABLE meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- Объекты метаданных, группы-разделы (Справочники, Документы…) и примитивные типы-заглушки.
CREATE TABLE objects (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL,              -- Тип: Справочник, Документ…; '' у групп-разделов
    name TEXT NOT NULL,              -- Имя
    type_name TEXT NOT NULL,         -- имя типа в нотации КД: СправочникСсылка.Имя
    synonym TEXT NOT NULL DEFAULT '',
    comment TEXT NOT NULL DEFAULT '',
    is_group INTEGER NOT NULL DEFAULT 0,
    group_id INTEGER REFERENCES objects(id),
    attrs TEXT NOT NULL DEFAULT '{}' -- настраиваемые свойства объекта (Иерархический…), JSON
);
CREATE INDEX objects_kind_name ON objects(kind, name);
CREATE INDEX objects_type_name ON objects(type_name);

-- Наборы типов: `types` — имена типов, отсортированные и соединённые переводом строки.
CREATE TABLE type_sets (
    id INTEGER PRIMARY KEY,
    types TEXT NOT NULL UNIQUE
);
CREATE TABLE type_set_items (
    set_id INTEGER NOT NULL REFERENCES type_sets(id),
    type_name TEXT NOT NULL,
    PRIMARY KEY (set_id, type_name)
) WITHOUT ROWID;
CREATE INDEX type_set_items_type ON type_set_items(type_name);

-- Дерево свойств объекта: реквизиты, измерения, ресурсы, табличные части, наборы движений,
-- состав плана обмена и его элементы.
CREATE TABLE properties (
    id INTEGER PRIMARY KEY,
    object_id INTEGER NOT NULL REFERENCES objects(id),
    parent_id INTEGER REFERENCES properties(id),
    kind TEXT NOT NULL,              -- Вид
    name TEXT NOT NULL,
    path TEXT NOT NULL,              -- имена от корня объекта через точку
    synonym TEXT NOT NULL DEFAULT '',
    comment TEXT NOT NULL DEFAULT '',
    is_group INTEGER NOT NULL DEFAULT 0,
    code TEXT NOT NULL DEFAULT '',
    type_set_id INTEGER REFERENCES type_sets(id),
    number_length INTEGER NOT NULL DEFAULT 0,
    number_precision INTEGER NOT NULL DEFAULT 0,
    number_nonnegative INTEGER NOT NULL DEFAULT 0,
    string_length INTEGER NOT NULL DEFAULT 0,
    string_fixed INTEGER NOT NULL DEFAULT 0,
    date_parts TEXT NOT NULL DEFAULT '',
    usage TEXT NOT NULL DEFAULT '',
    indexing INTEGER NOT NULL DEFAULT 0,
    autoregistration INTEGER NOT NULL DEFAULT 0,
    unresolved TEXT                  -- неразрешённые типы по строкам; NULL — все разрешены
);
CREATE INDEX properties_object ON properties(object_id, parent_id);
CREATE INDEX properties_kind ON properties(kind);

-- Значения перечислений, предопределённые элементы, точки маршрута.
CREATE TABLE object_values (
    id INTEGER PRIMARY KEY,
    object_id INTEGER NOT NULL REFERENCES objects(id),
    parent_id INTEGER REFERENCES object_values(id),
    name TEXT NOT NULL,
    synonym TEXT NOT NULL DEFAULT '',
    comment TEXT NOT NULL DEFAULT '',
    code TEXT NOT NULL DEFAULT '',
    predefined INTEGER NOT NULL DEFAULT 0,
    type_set_id INTEGER REFERENCES type_sets(id),
    unresolved TEXT
);
CREATE INDEX object_values_object ON object_values(object_id);
"""

# Ключи таблицы meta.
META_KEYS = (
    "schema_version",
    "structure_id",
    "source",  # md83exp | xml
    "source_path",
    "input_hash",  # sha256 входных файлов
    "loader_version",  # хеш кода загрузчика: другой код — структура загружается заново
    "loaded_at",
    "config_name",
    "config_synonym",
    "config_version",
    "extensions",  # JSON-список наложенных расширений
    "counts",  # JSON: objects / properties / values / type_sets / unresolved_types
    "elapsed_s",
)


def create(path: Path) -> sqlite3.Connection:
    """Новый пустой файл структуры со схемой; существующий файл по этому пути удаляется."""
    path.unlink(missing_ok=True)
    connection = sqlite3.connect(path)
    connection.executescript(SCHEMA)
    connection.execute("INSERT INTO meta VALUES ('schema_version', ?)", (SCHEMA_VERSION,))
    return connection


def open_readonly(path: Path) -> sqlite3.Connection:
    """Открывает файл структуры только на чтение."""
    connection = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def read_meta(connection: sqlite3.Connection) -> dict[str, str]:
    """Метаданные загрузки."""
    return dict(connection.execute("SELECT key, value FROM meta").fetchall())


def write_meta(connection: sqlite3.Connection, values: dict[str, str]) -> None:
    """Записывает или заменяет значения метаданных."""
    connection.executemany("INSERT OR REPLACE INTO meta VALUES (?, ?)", values.items())
