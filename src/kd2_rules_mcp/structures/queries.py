"""Запросы к загруженной структуре и сравнение двух структур по именам.

Функции принимают соединение `StructureStore.open` (`row_factory = sqlite3.Row`).
Группы-разделы (`is_group = 1`) и примитивные типы-заглушки объектами метаданных не считаются:
в списках их нет и по имени они не находятся. Набор констант MD83Exp пишет отдельно:
`Description` = `КонстантыНабор`, `Тип` = `НаборКонстант`, а реквизиты этой строки — сами
константы (`reference/kd2-dist-src/MD83Exp/ВыгрузкаМетаданных/Ext/ObjectModule.bsl:133-178`),
поэтому из сравнения и списков он не выпадает.
"""

import json
import sqlite3
from dataclasses import dataclass
from difflib import get_close_matches
from typing import Any

MAX_LIMIT = 200
_SUGGESTIONS = 5

# Имена типов-заглушек, как их пишет ВыгрузитьПримитивныеОбъекты (там же, строки 127–133).
# У шести примитивов `type_name` совпадает с этим именем. У набора констант `type_name` —
# «КонстантыНабор», не «НаборКонстант», и отбор по этому списку его не скрывает.
_STUB_TYPE_NAMES = (
    "Число",
    "Строка",
    "Дата",
    "Булево",
    "ХранилищеЗначения",
    "УникальныйИдентификатор",
    "НаборКонстант",
)

_PLAN_CONTENT_ITEM = "ЭлементСоставаПланаОбмена"
_QUALIFIER_INTS = ("number_length", "number_precision", "string_length")
_QUALIFIER_FLAGS = ("number_nonnegative", "string_fixed")


@dataclass(slots=True)
class Page:
    """Одна страница списка."""

    items: list[dict[str, Any]]
    total: int
    offset: int
    limit: int

    @property
    def has_more(self) -> bool:
        """Есть ли элементы дальше этой страницы."""
        return self.offset + len(self.items) < self.total


@dataclass(slots=True)
class NotFound:
    """Объект не найден или не подходит запросу."""

    name: str
    suggestions: list[str]
    message: str


@dataclass(slots=True)
class StructureDiff:
    """Различие двух структур по именам, а не по внутренним id."""

    added_objects: list[str]
    removed_objects: list[str]
    added_properties: list[str]
    removed_properties: list[str]
    changed_properties: list[dict[str, Any]]
    added_values: list[str]
    removed_values: list[str]

    def counts(self) -> dict[str, int]:
        """Число элементов в каждом списке различия."""
        return {
            "added_objects": len(self.added_objects),
            "removed_objects": len(self.removed_objects),
            "added_properties": len(self.added_properties),
            "removed_properties": len(self.removed_properties),
            "changed_properties": len(self.changed_properties),
            "added_values": len(self.added_values),
            "removed_values": len(self.removed_values),
        }


def list_objects(
    conn: sqlite3.Connection,
    kind: str | None = None,
    text: str | None = None,
    offset: int = 0,
    limit: int = 50,
) -> Page:
    """Объекты метаданных по виду и подстроке имени или синонима, по полному имени."""
    offset, limit = _page_window(offset, limit)
    _prepare(conn)
    sql = f"SELECT kind, name, type_name, synonym FROM objects WHERE {_real_where()} "
    params: list[object] = list(_STUB_TYPE_NAMES)
    if kind is not None:
        sql += "AND kind = ? "
        params.append(kind)
    sql += "ORDER BY kind || '.' || name"
    rows = list(conn.execute(sql, params))
    if text:
        needle = text.casefold()
        rows = [
            row
            for row in rows
            if needle in str(row["name"]).casefold() or needle in str(row["synonym"]).casefold()
        ]
    page = rows[offset : offset + limit]
    items = [
        {
            "name": f"{row['kind']}.{row['name']}",
            "type_name": row["type_name"],
            "synonym": row["synonym"],
        }
        for row in page
    ]
    return Page(items, len(rows), offset, limit)


@dataclass(frozen=True, slots=True)
class ObjectProperty:
    """Свойство объекта в порядке файла: то, что нужно сверке, без квалификаторов страницы."""

    path: str
    kind: str
    is_group: bool
    types: tuple[str, ...]
    unresolved: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ObjectCard:
    """Объект метаданных и полный список свойств. Промах — не карточка, а отсутствие."""

    name: str
    type_name: str
    kind: str
    properties: tuple[ObjectProperty, ...]


def read_object_card(conn: sqlite3.Connection, name: str) -> ObjectCard | None:
    """Объект и все его свойства одной выборкой, без подсказок при промахе.

    Отбор объекта тот же, что у `describe_object`. Страницы здесь нет: проверка
    регистрации читает свойства целиком и текст подсказки «похожие» не использует.
    Пустые строки набора типов отбрасываются, как в выдаче страницы.
    """
    _prepare(conn)
    row = _find_object(conn, name)
    if row is None:
        return None
    properties = tuple(
        ObjectProperty(
            str(item["path"]),
            str(item["kind"]),
            bool(item["is_group"]),
            tuple(_lines(item["types"])),
            tuple(_lines(item["unresolved"])),
        )
        for item in conn.execute(
            "SELECT p.path, p.kind, p.is_group, p.unresolved, ts.types AS types "
            "FROM properties AS p LEFT JOIN type_sets AS ts ON ts.id = p.type_set_id "
            "WHERE p.object_id = ? ORDER BY p.id",
            (row["id"],),
        )
    )
    return ObjectCard(
        f"{row['kind']}.{row['name']}",
        str(row["type_name"]),
        str(row["kind"]),
        properties,
    )


def exchange_plan_autoregistration(conn: sqlite3.Connection, name: str) -> dict[str, bool]:
    """Тип элемента состава → авторегистрация. Первое вхождение типа побеждает.

    Порядок и правила пустого набора типов те же, что у постраничного
    `exchange_plan_content`: нет плана или это не план обмена — пустой словарь.
    """
    _prepare(conn)
    lookup = name if "." in name else f"ПланОбмена.{name}"
    row = _find_object(conn, lookup)
    if row is None or row["kind"] != "ПланОбмена":
        return {}
    result: dict[str, bool] = {}
    for item in conn.execute(
        "SELECT p.autoregistration, p.unresolved, ts.types AS types FROM properties AS p "
        "LEFT JOIN type_sets AS ts ON ts.id = p.type_set_id "
        "WHERE p.object_id = ? AND p.kind = ? ORDER BY p.id",
        (row["id"], _PLAN_CONTENT_ITEM),
    ):
        types = _lines(item["types"])
        names = types if types else _lines(item["unresolved"])
        flag = bool(item["autoregistration"])
        for type_name in names:
            result.setdefault(type_name, flag)
    return result


def describe_object(
    conn: sqlite3.Connection,
    name: str,
    offset: int = 0,
    limit: int = 50,
) -> dict[str, Any] | NotFound:
    """Объект и плоский список его свойств в порядке файла."""
    offset, limit = _page_window(offset, limit)
    _prepare(conn)
    row = _find_object(conn, name)
    if row is None:
        return _not_found(conn, name)
    total, rows = _select_page(
        conn,
        "SELECT COUNT(*) FROM properties WHERE object_id = ?",
        "SELECT p.path, p.kind, p.synonym, p.is_group, p.number_length, p.number_precision, "
        "p.number_nonnegative, p.string_length, p.string_fixed, p.date_parts, p.unresolved, "
        "ts.types AS types FROM properties AS p "
        "LEFT JOIN type_sets AS ts ON ts.id = p.type_set_id "
        "WHERE p.object_id = ? ORDER BY p.id LIMIT ? OFFSET ?",
        (row["id"],),
        offset,
        limit,
    )
    return {
        "name": f"{row['kind']}.{row['name']}",
        "type_name": row["type_name"],
        "kind": row["kind"],
        "synonym": row["synonym"],
        "attrs": _attrs(str(row["attrs"])),
        "properties": Page([_property_item(item) for item in rows], total, offset, limit),
    }


def object_values(
    conn: sqlite3.Connection,
    name: str,
    offset: int = 0,
    limit: int = 50,
) -> Page | NotFound:
    """Значения перечисления или предопределённые элементы в порядке файла."""
    offset, limit = _page_window(offset, limit)
    _prepare(conn)
    row = _find_object(conn, name)
    if row is None:
        return _not_found(conn, name)
    total, rows = _select_page(
        conn,
        "SELECT COUNT(*) FROM object_values WHERE object_id = ?",
        "SELECT name, synonym, predefined FROM object_values "
        "WHERE object_id = ? ORDER BY id LIMIT ? OFFSET ?",
        (row["id"],),
        offset,
        limit,
    )
    items = [
        {"name": item["name"], "synonym": item["synonym"], "predefined": bool(item["predefined"])}
        for item in rows
    ]
    return Page(items, total, offset, limit)


def exchange_plan_content(
    conn: sqlite3.Connection,
    name: str,
    offset: int = 0,
    limit: int = 50,
) -> Page | NotFound:
    """Элементы состава плана обмена. Объект другого вида — не найден как план обмена.

    Имя без точки — имя плана: сначала ищется ``ПланОбмена.<имя>``. Нет такого объекта —
    подсказки по исходному имени, как при любом другом промахе.
    """
    offset, limit = _page_window(offset, limit)
    _prepare(conn)
    lookup = name if "." in name else f"ПланОбмена.{name}"
    row = _find_object(conn, lookup)
    if row is None:
        return _not_found(conn, name)
    if row["kind"] != "ПланОбмена":
        full_name = f"{row['kind']}.{row['name']}"
        return NotFound(
            name=name,
            suggestions=[],
            message=f"Объект «{full_name}» не является планом обмена",
        )
    total, rows = _select_page(
        conn,
        "SELECT COUNT(*) FROM properties WHERE object_id = ? "
        "AND kind = 'ЭлементСоставаПланаОбмена'",
        "SELECT p.name, p.autoregistration, p.unresolved, ts.types AS types "
        "FROM properties AS p LEFT JOIN type_sets AS ts ON ts.id = p.type_set_id "
        "WHERE p.object_id = ? AND p.kind = 'ЭлементСоставаПланаОбмена' "
        "ORDER BY p.id LIMIT ? OFFSET ?",
        (row["id"],),
        offset,
        limit,
    )
    return Page([_content_item(item) for item in rows], total, offset, limit)


def compare_structures(old: sqlite3.Connection, new: sqlite3.Connection) -> StructureDiff:
    """Добавленные, удалённые и изменённые объекты, свойства и значения двух структур.

    Ключ объекта — `Вид.Имя`, свойства — `Вид.Имя|path|kind` (у элемента состава плана обмена
    ещё `|тип`), значения — `Вид.Имя|name`.
    Свойство изменено, если у того же ключа различаются набор типов или квалификаторы.
    """
    _prepare(old)
    _prepare(new)
    added_objects, removed_objects = _set_diff(_object_keys(old), _object_keys(new))
    old_properties = _property_index(old)
    new_properties = _property_index(new)
    added_properties, removed_properties = _set_diff(set(old_properties), set(new_properties))
    added_values, removed_values = _set_diff(_value_keys(old), _value_keys(new))
    return StructureDiff(
        added_objects,
        removed_objects,
        added_properties,
        removed_properties,
        _changed_properties(old_properties, new_properties),
        added_values,
        removed_values,
    )


def _page_window(offset: int, limit: int) -> tuple[int, int]:
    """Проверяет окно и обрезает размер страницы до MAX_LIMIT."""
    if offset < 0:
        raise ValueError(f"Смещение страницы не может быть отрицательным: {offset}")
    if limit < 1:
        raise ValueError(f"Размер страницы должен быть не меньше 1: {limit}")
    return offset, min(limit, MAX_LIMIT)


def _prepare(conn: sqlite3.Connection) -> None:
    if conn.row_factory is not sqlite3.Row:
        conn.row_factory = sqlite3.Row


def _placeholders() -> str:
    return ", ".join("?" * len(_STUB_TYPE_NAMES))


def _real_where(alias: str = "") -> str:
    prefix = f"{alias}." if alias else ""
    return f"{prefix}is_group = 0 AND {prefix}type_name NOT IN ({_placeholders()})"


def _select_page(
    conn: sqlite3.Connection,
    count_sql: str,
    select_sql: str,
    params: tuple[object, ...],
    offset: int,
    limit: int,
) -> tuple[int, list[sqlite3.Row]]:
    counted = conn.execute(count_sql, params).fetchone()
    total = 0 if counted is None else int(counted[0])
    rows = list(conn.execute(select_sql, (*params, limit, offset)))
    return total, rows


def find_object(conn: sqlite3.Connection, name: str) -> sqlite3.Row | NotFound:
    """Строка объекта (`id`, `kind`, `name`, `type_name`, `synonym`, `attrs`).

    `name` — `Вид.Имя` или имя типа (`СправочникСсылка.Имя`); нет объекта — `NotFound` с похожими.
    """
    _prepare(conn)
    row = _find_object(conn, name)
    return row if row is not None else _not_found(conn, name)


def _find_object(conn: sqlite3.Connection, name: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT id, kind, name, type_name, synonym, attrs FROM objects WHERE "
        f"{_real_where()} AND (kind || '.' || name = ? OR type_name = ?) "
        "ORDER BY CASE WHEN kind || '.' || name = ? THEN 0 ELSE 1 END LIMIT 1",
        (*_STUB_TYPE_NAMES, name, name, name),
    ).fetchone()


def _not_found(conn: sqlite3.Connection, name: str) -> NotFound:
    kind, word = _suggestion_kind(conn, name)
    suggestions: list[str] = []
    if kind is not None:
        names = [
            str(row[0])
            for row in conn.execute(
                f"SELECT kind || '.' || name FROM objects WHERE {_real_where()} AND kind = ?",
                (*_STUB_TYPE_NAMES, kind),
            )
        ]
        suggestions = get_close_matches(word, names, n=_SUGGESTIONS)
    similar = ", ".join(suggestions) if suggestions else "нет"
    return NotFound(
        name=name,
        suggestions=suggestions,
        message=f"Объект «{name}» не найден; похожие: {similar}",
    )


def _suggestion_kind(conn: sqlite3.Connection, name: str) -> tuple[str | None, str]:
    """Вид для подсказок и полное имя, с которым сравниваются кандидаты."""
    if "." not in name:
        return None, name
    head, tail = name.split(".", 1)
    row = conn.execute(
        "SELECT kind FROM objects WHERE "
        f"{_real_where()} AND (kind = ? OR type_name LIKE ? ESCAPE '\\') "
        "ORDER BY CASE WHEN kind = ? THEN 0 ELSE 1 END LIMIT 1",
        (*_STUB_TYPE_NAMES, head, _like_prefix(head), head),
    ).fetchone()
    if row is None:
        return None, name
    kind = str(row["kind"])
    return kind, f"{kind}.{tail}"


def _like_prefix(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"{escaped}.%"


def _attrs(raw: str) -> dict[str, Any]:
    parsed: Any = json.loads(raw or "{}")
    if not isinstance(parsed, dict):
        return {}
    return {str(key): value for key, value in parsed.items()}


def _lines(value: str | None) -> list[str]:
    if not value:
        return []
    return [line for line in value.split("\n") if line]


def _qualifier_signature(row: sqlite3.Row) -> tuple[tuple[str, Any], ...]:
    items: list[tuple[str, Any]] = []
    for key in _QUALIFIER_INTS:
        value = int(row[key])
        if value != 0:
            items.append((key, value))
    for key in _QUALIFIER_FLAGS:
        if int(row[key]) != 0:
            items.append((key, True))
    date_parts = str(row["date_parts"])
    if date_parts:
        items.append(("date_parts", date_parts))
    return tuple(items)


def _property_item(row: sqlite3.Row) -> dict[str, Any]:
    item: dict[str, Any] = {
        "path": row["path"],
        "kind": row["kind"],
        "synonym": row["synonym"],
        "is_group": bool(row["is_group"]),
        "types": _lines(row["types"]),
        "qualifiers": dict(_qualifier_signature(row)),
    }
    unresolved = _lines(row["unresolved"])
    if unresolved:
        item["unresolved"] = unresolved
    return item


def _content_item(row: sqlite3.Row) -> dict[str, Any]:
    item: dict[str, Any] = {
        "name": row["name"],
        "types": _lines(row["types"]),
        "autoregistration": bool(row["autoregistration"]),
    }
    unresolved = _lines(row["unresolved"])
    if unresolved:
        item["unresolved"] = unresolved
    return item


def _object_keys(conn: sqlite3.Connection) -> set[str]:
    sql = f"SELECT kind || '.' || name FROM objects WHERE {_real_where()}"
    return {str(row[0]) for row in conn.execute(sql, _STUB_TYPE_NAMES)}


def _value_keys(conn: sqlite3.Connection) -> set[str]:
    sql = (
        "SELECT o.kind || '.' || o.name || '|' || v.name FROM object_values AS v "
        f"JOIN objects AS o ON o.id = v.object_id WHERE {_real_where('o')}"
    )
    return {str(row[0]) for row in conn.execute(sql, _STUB_TYPE_NAMES)}


def _property_index(
    conn: sqlite3.Connection,
) -> dict[str, tuple[tuple[str, ...], tuple[tuple[str, Any], ...]]]:
    """Ключ свойства → (типы, квалификаторы). Наборы типов читаются один раз."""
    type_sets: dict[int, tuple[str, ...]] = {}
    for row in conn.execute("SELECT id, types FROM type_sets"):
        raw = row["types"]
        type_sets[int(row["id"])] = tuple(str(raw).split("\n")) if raw else ()
    sql = (
        "SELECT o.kind AS object_kind, o.name AS object_name, p.path, p.kind AS property_kind, "
        "p.type_set_id, p.number_length, p.number_precision, p.number_nonnegative, "
        "p.string_length, p.string_fixed, p.date_parts "
        "FROM properties AS p JOIN objects AS o ON o.id = p.object_id "
        f"WHERE {_real_where('o')}"
    )
    index: dict[str, tuple[tuple[str, ...], tuple[tuple[str, Any], ...]]] = {}
    for row in conn.execute(sql, _STUB_TYPE_NAMES):
        key = f"{row['object_kind']}.{row['object_name']}|{row['path']}|{row['property_kind']}"
        set_id = row["type_set_id"]
        types = type_sets.get(int(set_id), ()) if set_id is not None else ()
        if row["property_kind"] == _PLAN_CONTENT_ITEM:
            # В составе плана обмена бывают одноимённые элементы разных видов
            # (документ и регистр `ВыработкаМатериалов`): тип — часть ключа.
            # Неразрешённый тип (последовательность) — «?»: его GUID у MD83Exp случаен.
            key += "|" + ("\n".join(types) or "?")
        index[key] = (types, _qualifier_signature(row))
    return index


def _set_diff(old: set[str], new: set[str]) -> tuple[list[str], list[str]]:
    return sorted(new - old), sorted(old - new)


def _changed_properties(
    old: dict[str, tuple[tuple[str, ...], tuple[tuple[str, Any], ...]]],
    new: dict[str, tuple[tuple[str, ...], tuple[tuple[str, Any], ...]]],
) -> list[dict[str, Any]]:
    changed: list[dict[str, Any]] = []
    for key in sorted(old.keys() & new.keys()):
        old_types, old_qualifiers = old[key]
        new_types, new_qualifiers = new[key]
        if old_types == new_types and old_qualifiers == new_qualifiers:
            continue
        changed.append(
            {
                "key": key,
                "old_types": list(old_types),
                "new_types": list(new_types),
                "old_qualifiers": dict(old_qualifiers),
                "new_qualifiers": dict(new_qualifiers),
            }
        )
    return changed
