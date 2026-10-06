"""Кандидаты нового менеджера: объект конфигурации ↔ тип формата и реквизиты шапки.

Сервер ничего не выбирает. Каждая пара — подсказка с `auto = false`: совпадение имени
или синонима не является смысловым назначением. Вид объекта и вид типа формата должны
совпадать; документ и справочник в кандидаты сами не сводятся.

Путь свойства формата — физический, через точку (`КлючевыеСвойства.Наименование`), как его
хранит генератор КД 3. Имя свойства — лист этого пути: так свойство записано в типовых
модулях и так его ищет исполнитель. У типа схемы нет отдельного описания, поэтому класс
«по синониму» сравнивает синоним конфигурации с именем типа формата.
"""

import hashlib
import sqlite3
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from kd2_rules_mcp.authoring.candidates import KD_NAME_ANALOGS, Confidence
from kd2_rules_mcp.authoring.ed.candidates import compatibility, primitive_limits
from kd2_rules_mcp.ed.address import AddressIndex, build_addresses
from kd2_rules_mcp.ed.model import EdDocument
from kd2_rules_mcp.ed.schema.model import EdSchema, QName, SchemaProperty, SchemaType
from kd2_rules_mcp.ed.schema.profile import Applicability, ValidationProfile
from kd2_rules_mcp.ed.schema.resolver import (
    effective_properties,
    is_reference,
    property_type,
    table_row,
)
from kd2_rules_mcp.ed.schema.xdto import XS
from kd2_rules_mcp.errors import Kd2Error
from kd2_rules_mcp.structures.queries import MAX_LIMIT, NotFound, find_object
from kd2_rules_mcp.structures.xmldump import KINDS
from kd2_rules_mcp.validation.ed_structure_snapshot import StructureProperty, metadata_key

_HEADER_KINDS = frozenset({"Реквизит", "Свойство", "Измерение", "Ресурс"})
_REFERENCE_PREFIXES = tuple(item[4] for item in KINDS.values() if "Ссылка." in item[4])
_KIND_BY_FOLDED = {item[1].casefold(): item[1] for item in KINDS.values()}
_PRIMITIVES = frozenset({"Строка", "Булево", "Число", "Дата"})
_RANK = {
    Confidence.EXACT: 0,
    Confidence.KD_SYNONYM: 1,
    Confidence.BY_SYNONYM: 2,
    Confidence.NO_PAIR: 3,
}
_ID_HEX = frozenset("0123456789abcdef")


class StaleCandidateError(Kd2Error):
    """Идентификатор кандидата выпущен для других структуры или схемы."""


class CandidateLookupError(Kd2Error):
    """Объект конфигурации или тип формата не найден."""


@dataclass(frozen=True, slots=True)
class _ConfigObject:
    kind: str
    name: str
    synonym: str

    @property
    def full_name(self) -> str:
        return f"{self.kind}.{self.name}"


@dataclass(frozen=True, slots=True)
class _FormatObject:
    kind: str
    name: str
    namespace: str
    local: str


@dataclass(frozen=True, slots=True)
class _Attribute:
    prop: StructureProperty
    synonym: str


@dataclass(frozen=True, slots=True)
class _FormatProperty:
    prop: SchemaProperty
    path: str
    leaf: str
    table: bool
    namespace: str
    wrapper: bool
    nested: bool


def object_candidates(
    structure: sqlite3.Connection,
    schema: EdSchema,
    *,
    direction: str,
    kinds: Iterable[str] | None = None,
    text: str = "",
    offset: int = 0,
    limit: int = 50,
) -> dict[str, Any]:
    """Пары «объект конфигурации ↔ объектный тип схемы» одной страницей.

    `kinds` ограничивает вид объектов конфигурации. Строка вместо списка не принимается:
    иначе отбор шёл бы по буквам. `text` — подстрока имени, синонима или типа формата.
    """
    _direction(direction)
    allowed = _kinds(kinds)
    offset, limit = _window(offset, limit)
    binding = _binding(structure, schema)
    needle = text.casefold()
    configs = [item for item in _config_objects(structure) if _kind_allowed(item.kind, allowed)]
    formats = [item for item in _format_objects(schema) if _kind_allowed(item.kind, allowed)]
    by_name: dict[tuple[str, str], list[_FormatObject]] = {}
    for item in formats:
        by_name.setdefault((_fold(item.kind), _fold(item.name)), []).append(item)
    paired_cfg: set[tuple[str, str]] = set()
    paired_fmt: set[tuple[str, str]] = set()
    rows: list[dict[str, Any]] = []
    for cfg in configs:
        for fmt in by_name.get((_fold(cfg.kind), _fold(cfg.name)), ()):
            paired_cfg.add((_fold(cfg.kind), _fold(cfg.name)))
            paired_fmt.add((fmt.namespace, fmt.local))
            if _text_hit(needle, cfg, fmt.local):
                rows.append(
                    _object_row(binding, cfg, fmt, direction, Confidence.EXACT, "совпадает имя")
                )
    free_cfg = [item for item in configs if (_fold(item.kind), _fold(item.name)) not in paired_cfg]
    free_fmt = [item for item in formats if (item.namespace, item.local) not in paired_fmt]
    rows.extend(_synonym_objects(binding, free_cfg, free_fmt, direction, needle))
    rows.sort(
        key=lambda item: (
            _RANK[item["confidence"]],
            _fold(item["configuration"]),
            _fold(item["format_type"]),
            item["namespace"],
        )
    )
    return _page(rows, offset, limit)


def property_candidates(
    structure: sqlite3.Connection,
    schema: EdSchema,
    config_object: str,
    format_type: str,
    *,
    direction: str,
    offset: int = 0,
    limit: int = 200,
    reference_document: EdDocument | None = None,
    reference_document_id: str = "",
    reference_index: AddressIndex | None = None,
) -> dict[str, Any]:
    """Реквизиты шапки выбранной пары и отдельно имена табличных частей.

    Свойства без пары и реквизиты без пары возвращаются своими страницами. Окно одно
    на все четыре списка. Табличная часть и поля её строки в свойства шапки не входят.
    """
    _direction(direction)
    offset, limit = _window(offset, limit)
    found = find_object(structure, config_object)
    if isinstance(found, NotFound):
        raise CandidateLookupError(found.message)
    typ, namespace = _resolve_format_type(schema, format_type)
    binding = _binding(structure, schema)
    full_name = f"{found['kind']}.{found['name']}"
    attributes, tables = _attributes(structure, int(found["id"]))
    header, tabular = _format_properties(schema, typ)
    visible_header = [item for item in header if not item.wrapper and not item.nested]
    visible_tabular = [item for item in tabular if not item.wrapper and not item.nested]
    properties, used_attr, used_fmt = _match_properties(
        binding,
        full_name,
        typ.qname.local if typ.qname else "",
        direction,
        schema,
        attributes,
        visible_header,
    )
    for row in properties:
        if row["class"] == "needs_pkpd":
            _add_value_pairs(structure, schema, visible_header, row)
    if reference_document is not None:
        reference_rows, reference_attrs, reference_props = _reference_properties(
            binding,
            full_name,
            typ,
            direction,
            schema,
            attributes,
            visible_header,
            reference_document,
            reference_document_id,
            reference_index or build_addresses(reference_document),
        )
        _merge_reference_properties(binding, properties, reference_rows)
        used_attr.update(reference_attrs)
        used_fmt.update(reference_props)
    table_rows, used_tables, used_tabular = _match_tables(
        binding,
        full_name,
        typ.qname.local if typ.qname else "",
        direction,
        tables,
        visible_tabular,
    )
    unmatched_format = [
        {"name": item.leaf, "path": item.path, "namespace": item.namespace}
        for item in (*visible_header, *visible_tabular)
        if id(item) not in used_fmt and id(item) not in used_tabular
    ]
    unmatched_configuration = [
        {"name": item.prop.path, "kind": item.prop.kind}
        for item in attributes
        if id(item) not in used_attr
    ]
    unmatched_configuration.extend(
        {"name": name, "kind": "ТабличнаяЧасть"} for name in tables if name not in used_tables
    )
    properties.sort(
        key=lambda item: (
            _RANK.get(item["confidence"], 4),
            _fold(item["configuration"]),
            _fold(item["format_path"]),
        )
    )
    table_rows.sort(key=lambda item: (_fold(item["configuration"]), _fold(item["format_path"])))
    unmatched_format.sort(key=lambda item: (_fold(item["path"]), _fold(item["name"])))
    unmatched_configuration.sort(key=lambda item: (_fold(item["name"]), _fold(item["kind"])))
    return {
        "configuration": full_name,
        "format_type": typ.qname.local if typ.qname else "",
        "namespace": namespace,
        "direction": direction,
        "properties": _page(properties, offset, limit),
        "table_parts": _page(table_rows, offset, limit),
        "unmatched_format": _page(unmatched_format, offset, limit),
        "unmatched_configuration": _page(unmatched_configuration, offset, limit),
    }


def check_candidate(candidate_id: str, structure: sqlite3.Connection, schema: EdSchema) -> None:
    """Отклоняет идентификатор, если структура или схема уже другие.

    Сверяется вся строка: полный отпечаток входов, хеш пары и контрольная сумма.
    Укороченный префикс, чужой отпечаток и дописанный хвост не принимаются.
    Другая пара тех же входов остаётся годной: функция проверяет входы, не выбор пары.
    """
    binding = _binding(structure, schema)
    if not _identifier_matches(candidate_id, binding):
        raise StaleCandidateError(
            "Идентификатор кандидата устарел: изменились структура конфигурации или схема формата"
        )


def _direction(direction: str) -> None:
    if direction not in ("send", "receive"):
        raise ValueError("Направление: send или receive")


def _kinds(kinds: Iterable[str] | None) -> frozenset[str] | None:
    if kinds is None:
        return None
    if isinstance(kinds, str):
        raise TypeError("Виды объектов передаются списком, не строкой")
    return frozenset(_fold(item) for item in kinds)


def _kind_allowed(kind: str, allowed: frozenset[str] | None) -> bool:
    return allowed is None or _fold(kind) in allowed


def _window(offset: int, limit: int) -> tuple[int, int]:
    if offset < 0:
        raise ValueError(f"Смещение страницы не может быть отрицательным: {offset}")
    if limit < 1:
        raise ValueError(f"Размер страницы должен быть не меньше 1: {limit}")
    return offset, min(limit, MAX_LIMIT)


def _page(items: list[dict[str, Any]], offset: int, limit: int) -> dict[str, Any]:
    window = items[offset : offset + limit]
    return {
        "items": window,
        "total": len(items),
        "offset": offset,
        "limit": limit,
        "has_more": offset + len(window) < len(items),
    }


def _fold(value: str) -> str:
    return value.strip().casefold()


def _text_hit(needle: str, cfg: _ConfigObject, format_name: str) -> bool:
    if not needle:
        return True
    return (
        needle in cfg.full_name.casefold()
        or needle in cfg.synonym.casefold()
        or needle in format_name.casefold()
    )


def _binding(structure: sqlite3.Connection, schema: EdSchema) -> str:
    raw = f"{_structure_hash(structure)}\n{schema.schema_id}".encode()
    return hashlib.sha256(raw).hexdigest()


def _candidate_id(binding: str, *parts: str) -> str:
    pair = hashlib.sha256("\n".join(parts).encode()).hexdigest()
    mac = hashlib.sha256(f"{binding}\n{pair}".encode()).hexdigest()[:16]
    return binding + pair + mac


def _identifier_matches(candidate_id: object, binding: str) -> bool:
    if not isinstance(candidate_id, str) or len(candidate_id) != 144:
        return False
    if any(char not in _ID_HEX for char in candidate_id):
        return False
    if candidate_id[:64] != binding:
        return False
    pair = candidate_id[64:128]
    mac = hashlib.sha256(f"{binding}\n{pair}".encode()).hexdigest()[:16]
    return candidate_id[128:] == mac


def _structure_hash(structure: sqlite3.Connection) -> str:
    """Отпечаток содержимого. Кэш по адресу соединения не годится: сервис закрывает его.

    У загруженной структуры берётся `input_hash`, который сравнивает `force`
    (`structures/store.py`). Иначе хеш строк объектов и свойств считается заново.
    """
    stored = _stored_fingerprint(structure)
    if stored:
        return stored
    return _content_hash(structure)


def _stored_fingerprint(structure: sqlite3.Connection) -> str:
    try:
        rows = structure.execute("SELECT key, value FROM meta").fetchall()
    except sqlite3.Error:
        return ""
    meta = {str(key): str(value) for key, value in rows}
    raw = meta.get("input_hash", "")
    if not raw:
        return ""
    return hashlib.sha256(f"{raw}\n{meta.get('loader_version', '')}".encode()).hexdigest()


def _content_hash(structure: sqlite3.Connection) -> str:
    if structure.row_factory is not sqlite3.Row:
        structure.row_factory = sqlite3.Row
    digest = hashlib.sha256()
    for row in structure.execute(
        "SELECT kind, name, type_name, synonym FROM objects WHERE is_group = 0 "
        "ORDER BY kind, name, type_name"
    ):
        digest.update(repr(tuple(row)).encode())
        digest.update(b"\0")
    for row in structure.execute(
        "SELECT o.kind, o.name, p.path, p.kind, p.synonym, p.number_length, p.number_precision, "
        "p.number_nonnegative, p.string_length, p.string_fixed, p.date_parts, "
        "IFNULL(p.unresolved, ''), IFNULL(ts.types, '') "
        "FROM properties AS p JOIN objects AS o ON o.id = p.object_id "
        "LEFT JOIN type_sets AS ts ON ts.id = p.type_set_id "
        "WHERE o.is_group = 0 ORDER BY o.kind, o.name, p.path, p.kind"
    ):
        digest.update(repr(tuple(row)).encode())
        digest.update(b"\0")
    return digest.hexdigest()


def _config_objects(structure: sqlite3.Connection) -> list[_ConfigObject]:
    if structure.row_factory is not sqlite3.Row:
        structure.row_factory = sqlite3.Row
    return [
        _ConfigObject(str(row["kind"]), str(row["name"]), str(row["synonym"]))
        for row in structure.execute(
            "SELECT kind, name, synonym FROM objects WHERE is_group = 0 AND kind <> '' "
            "AND instr(type_name, '.') > 0 ORDER BY kind, name"
        )
    ]


def _format_objects(schema: EdSchema) -> list[_FormatObject]:
    active = (schema.base_namespace, *schema.extension_namespaces)
    found: list[_FormatObject] = []
    for namespace in active:
        for qname, typ in schema.types.items():
            if qname.namespace != namespace or typ.kind != "object" or typ.qname is None:
                continue
            head, separator, tail = typ.qname.local.partition(".")
            kind = _KIND_BY_FOLDED.get(head.casefold())
            if separator and kind is not None and "." not in tail and tail:
                found.append(_FormatObject(kind, tail, namespace, typ.qname.local))
    return found


def _synonym_objects(
    binding: str,
    configs: list[_ConfigObject],
    formats: list[_FormatObject],
    direction: str,
    needle: str,
) -> list[dict[str, Any]]:
    by_synonym: dict[tuple[str, str], list[_ConfigObject]] = {}
    for item in configs:
        synonym = item.synonym.strip()
        if synonym:
            by_synonym.setdefault((_fold(item.kind), _fold(synonym)), []).append(item)
    by_name: dict[tuple[str, str], list[_FormatObject]] = {}
    for item in formats:
        by_name.setdefault((_fold(item.kind), _fold(item.name)), []).append(item)
    rows: list[dict[str, Any]] = []
    for key, group in by_synonym.items():
        targets = by_name.get(key, [])
        if len(group) != 1 or len(targets) != 1:
            continue
        cfg, fmt = group[0], targets[0]
        if not _text_hit(needle, cfg, fmt.local):
            continue
        reason = f"совпадает синоним «{cfg.synonym.strip()}» и имя типа формата"
        rows.append(_object_row(binding, cfg, fmt, direction, Confidence.BY_SYNONYM, reason))
    return rows


def _object_row(
    binding: str,
    cfg: _ConfigObject,
    fmt: _FormatObject,
    direction: str,
    confidence: Confidence,
    reason: str,
) -> dict[str, Any]:
    return {
        "candidate_id": _candidate_id(
            binding, "object", direction, cfg.full_name, fmt.namespace, fmt.local
        ),
        "configuration": cfg.full_name,
        "format_type": fmt.local,
        "namespace": fmt.namespace,
        "confidence": confidence.value,
        "reason": reason,
        "auto": False,
        "direction": direction,
    }


def _resolve_format_type(schema: EdSchema, name: str) -> tuple[SchemaType, str]:
    local = name.strip()
    namespace = ""
    if local.startswith("{") and "}" in local:
        namespace, local = local[1:].split("}", 1)
    active = (schema.base_namespace, *schema.extension_namespaces)
    if namespace:
        typ = schema.types.get(QName(namespace, local))
        if typ is None or typ.kind != "object":
            raise CandidateLookupError(f"Тип формата «{name}» не найден в схеме")
        return typ, namespace
    found = [
        (item, schema.types[QName(item, local)])
        for item in active
        if QName(item, local) in schema.types and schema.types[QName(item, local)].kind == "object"
    ]
    base = [item for item in found if item[0] == schema.base_namespace]
    if len(base) == 1:
        return base[0][1], base[0][0]
    if len(found) == 1:
        return found[0][1], found[0][0]
    if not found:
        raise CandidateLookupError(f"Тип формата «{name}» не найден в схеме")
    raise CandidateLookupError(f"Тип формата «{name}» неоднозначен: несколько пространств имён")


def _attributes(
    structure: sqlite3.Connection, object_id: int
) -> tuple[list[_Attribute], list[str]]:
    if structure.row_factory is not sqlite3.Row:
        structure.row_factory = sqlite3.Row
    rows = list(
        structure.execute(
            "SELECT p.id, p.parent_id, p.kind, p.name, p.path, p.synonym, p.number_length, "
            "p.number_precision, p.number_nonnegative, p.string_length, p.string_fixed, "
            "p.date_parts, p.usage, p.indexing, p.autoregistration, p.unresolved, ts.types "
            "FROM properties AS p LEFT JOIN type_sets AS ts ON ts.id = p.type_set_id "
            "WHERE p.object_id = ? ORDER BY p.id",
            (object_id,),
        )
    )
    parent_kind = {int(row["id"]): str(row["kind"]) for row in rows}
    attributes: list[_Attribute] = []
    tables: list[str] = []
    for row in rows:
        kind = str(row["kind"])
        parent = parent_kind.get(int(row["parent_id"]), "") if row["parent_id"] is not None else ""
        if kind == "ТабличнаяЧасть" and not parent:
            tables.append(str(row["name"]))
            continue
        if parent or kind not in _HEADER_KINDS:
            continue
        types = _lines(row["types"])
        qualifiers: Mapping[str, str | int] = MappingProxyType(
            {
                "number_length": int(row["number_length"]),
                "number_precision": int(row["number_precision"]),
                "number_nonnegative": int(row["number_nonnegative"]),
                "string_length": int(row["string_length"]),
                "string_fixed": int(row["string_fixed"]),
                "date_parts": str(row["date_parts"] or ""),
                "usage": str(row["usage"] or ""),
                "indexing": int(row["indexing"]),
                "autoregistration": int(row["autoregistration"]),
            }
        )
        prop = StructureProperty(
            int(row["id"]),
            str(row["path"]),
            kind,
            parent,
            types,
            _lines(row["unresolved"]),
            qualifiers,
        )
        attributes.append(_Attribute(prop, str(row["synonym"])))
    return attributes, tables


def _lines(value: object) -> tuple[str, ...]:
    if not isinstance(value, str) or not value:
        return ()
    return tuple(line for line in value.splitlines() if line)


def _format_properties(
    schema: EdSchema, typ: SchemaType
) -> tuple[list[_FormatProperty], list[_FormatProperty]]:
    """Свойства базового типа и расширений с тем же локальным именем.

    Исполнитель дополняет список свойствами расширения того же имени типа
    (`ДополнитьСвойстваПакетаИзРасширений`, ОбменДаннымиXDTOСервер:9903–9947):
    берёт свойства, чьё пространство имён — пространство расширения, и не
    повторяет уже имеющиеся. Имя с пространством и без него дают один набор.
    """
    header: list[_FormatProperty] = []
    tables: list[_FormatProperty] = []
    seen: set[tuple[str, str]] = set()
    for owner in _related_types(schema, typ):
        owner_ns = owner.qname.namespace if owner.qname else ""
        extension = owner_ns in schema.extension_namespaces
        for prop, physical in effective_properties(schema, owner):
            if extension and prop.name.namespace != owner_ns:
                continue
            path = ".".join(part.local for part in physical)
            key = (prop.name.namespace, path)
            if key in seen:
                continue
            seen.add(key)
            target = property_type(schema, prop)
            item = _FormatProperty(
                prop,
                path,
                prop.name.local,
                table_row(schema, prop) is not None,
                prop.name.namespace,
                _is_service_wrapper(prop, target),
                _is_nested_path(schema, owner, physical),
            )
            if item.table:
                tables.append(item)
            elif prop.upper == 1:
                header.append(item)
    return header, tables


def _related_types(schema: EdSchema, typ: SchemaType) -> list[SchemaType]:
    local = typ.qname.local if typ.qname else ""
    found: list[SchemaType] = []
    base = schema.types.get(QName(schema.base_namespace, local)) if local else None
    if base is not None and base.kind == "object":
        found.append(base)
    if typ.kind == "object" and typ not in found:
        found.append(typ)
    for namespace in schema.extension_namespaces:
        ext = schema.types.get(QName(namespace, local)) if local else None
        if ext is not None and ext.kind == "object" and ext not in found:
            found.append(ext)
    return found or [typ]


def _is_service_wrapper(prop: SchemaProperty, target: SchemaType | None) -> bool:
    """Оболочка шапки: своё `КлючевыеСвойства` и общие свойства объекта, не поле внутри."""
    if prop.name.local == "КлючевыеСвойства":
        return True
    return bool(target and target.qname and target.qname.local.startswith("ОбщиеСвойстваОбъектов"))


def _is_nested_reference(prop: SchemaProperty, target: SchemaType | None) -> bool:
    """Чужой объект внутри шапки: его ключевые поля не пара реквизитам этого объекта.

    Своя оболочка ``КлючевыеСвойства`` и группы вроде ``ОбщиеСвойстваПБДС`` сюда
    не входят: их поля — поля этого объекта. Вложенный тип ``КлючевыеСвойства*``
    и классификатор (``…Классификатор``) — другой объект.
    """
    if target is None or target.kind != "object" or target.qname is None:
        return False
    local = target.qname.local
    if local.startswith("КлючевыеСвойства") and prop.name.local != "КлючевыеСвойства":
        return True
    return "Классификатор" in local


def _is_nested_path(schema: EdSchema, owner: SchemaType, physical: tuple[QName, ...]) -> bool:
    """В раскрытых схемой путях сохраняет свои группы под корневым `КлючевыеСвойства`.

    Имя группы общих свойств не ограничено. Группа должна быть объектным типом,
    не ссылкой Ref и не чужим типом ключей.
    Вложенная ссылка/ключи другого объекта обрывают весь путь, включая более глубокие
    группы. Вне своих ключей эвристика классификатора по-прежнему отсекает чужие поля.
    """
    current = owner
    for position, part in enumerate(physical[:-1]):
        prop = _property_named(schema, current, part.local)
        if prop is None:
            return False
        target = property_type(schema, prop)
        if target is None:
            return False
        own_group = (
            position > 0
            and physical[0].local == "КлючевыеСвойства"
            and target.kind == "object"
            and not _is_key_properties_type(target)
            and not is_reference(schema, target)
        )
        if position > 0 and _is_key_properties_type(target):
            return True
        if not own_group and _is_nested_reference(prop, target):
            return True
        current = target
    return False


def _property_named(schema: EdSchema, typ: SchemaType, local: str) -> SchemaProperty | None:
    for prop in schema.inherited.get(typ.id, ()):
        if prop.name.local == local:
            return prop
    return None


def _match_properties(
    binding: str,
    configuration: str,
    format_type: str,
    direction: str,
    schema: EdSchema,
    attributes: list[_Attribute],
    header: list[_FormatProperty],
) -> tuple[list[dict[str, Any]], set[int], set[int]]:
    profile = ValidationProfile(
        schema,
        None,
        direction,
        (schema.base_namespace, *schema.extension_namespaces),
        MappingProxyType({}),
        MappingProxyType({}),
    )
    by_attr: dict[str, list[_Attribute]] = {}
    for item in attributes:
        by_attr.setdefault(_fold(item.prop.path), []).append(item)
    by_leaf: dict[str, list[_FormatProperty]] = {}
    for item in header:
        by_leaf.setdefault(_fold(item.leaf), []).append(item)
    rows: list[dict[str, Any]] = []
    used_attr: set[int] = set()
    used_fmt: set[int] = set()
    for leaf, props in by_leaf.items():
        for attr in by_attr.get(leaf, ()):
            for prop in props:
                rows.append(
                    _property_row(
                        binding,
                        configuration,
                        format_type,
                        direction,
                        schema,
                        profile,
                        attr,
                        prop,
                        Confidence.EXACT,
                        "совпадает имя",
                    )
                )
                used_attr.add(id(attr))
                used_fmt.add(id(prop))
    _analog_properties(
        binding,
        configuration,
        format_type,
        direction,
        schema,
        profile,
        attributes,
        header,
        used_attr,
        used_fmt,
        rows,
    )
    _synonym_properties(
        binding,
        configuration,
        format_type,
        direction,
        schema,
        profile,
        attributes,
        header,
        used_attr,
        used_fmt,
        rows,
    )
    return rows, used_attr, used_fmt


def _analog_properties(
    binding: str,
    configuration: str,
    format_type: str,
    direction: str,
    schema: EdSchema,
    profile: ValidationProfile,
    attributes: list[_Attribute],
    header: list[_FormatProperty],
    used_attr: set[int],
    used_fmt: set[int],
    rows: list[dict[str, Any]],
) -> None:
    free_attr = {_fold(item.prop.path): item for item in attributes if id(item) not in used_attr}
    for prop in header:
        if id(prop) in used_fmt:
            continue
        analog = KD_NAME_ANALOGS.get(prop.leaf)
        attr = free_attr.get(_fold(analog)) if analog else None
        if attr is None:
            continue
        rows.append(
            _property_row(
                binding,
                configuration,
                format_type,
                direction,
                schema,
                profile,
                attr,
                prop,
                Confidence.KD_SYNONYM,
                f"имя-аналог «{attr.prop.path}» и «{prop.leaf}»",
            )
        )
        used_attr.add(id(attr))
        used_fmt.add(id(prop))
        free_attr.pop(_fold(attr.prop.path), None)


def _synonym_properties(
    binding: str,
    configuration: str,
    format_type: str,
    direction: str,
    schema: EdSchema,
    profile: ValidationProfile,
    attributes: list[_Attribute],
    header: list[_FormatProperty],
    used_attr: set[int],
    used_fmt: set[int],
    rows: list[dict[str, Any]],
) -> None:
    by_synonym: dict[str, list[_Attribute]] = {}
    for item in attributes:
        if id(item) in used_attr or not item.synonym.strip():
            continue
        by_synonym.setdefault(_fold(item.synonym), []).append(item)
    by_leaf: dict[str, list[_FormatProperty]] = {}
    for item in header:
        if id(item) not in used_fmt:
            by_leaf.setdefault(_fold(item.leaf), []).append(item)
    for leaf, props in by_leaf.items():
        group = by_synonym.get(leaf, [])
        if len(group) != 1 or len(props) != 1:
            continue
        attr, prop = group[0], props[0]
        rows.append(
            _property_row(
                binding,
                configuration,
                format_type,
                direction,
                schema,
                profile,
                attr,
                prop,
                Confidence.BY_SYNONYM,
                f"совпадает синоним «{attr.synonym.strip()}» и имя свойства формата",
            )
        )
        used_attr.add(id(attr))
        used_fmt.add(id(prop))


def _property_row(
    binding: str,
    configuration: str,
    format_type: str,
    direction: str,
    schema: EdSchema,
    profile: ValidationProfile,
    attr: _Attribute,
    prop: _FormatProperty,
    confidence: Confidence,
    match_reason: str,
) -> dict[str, Any]:
    classified = _classify(schema, profile, prop.prop, attr.prop, direction)
    row = {
        "candidate_id": _candidate_id(
            binding, "property", direction, configuration, format_type, attr.prop.path, prop.path
        ),
        "configuration": attr.prop.path,
        "format_path": prop.path,
        "format_name": prop.leaf,
        "namespace": prop.namespace,
        "class": classified.match,
        "confidence": confidence.value,
        "reason": classified.reason if classified.match != "direct" else match_reason,
        "compatibility": classified.reason,
        "auto": False,
        "direction": direction,
        "configuration_types": list(attr.prop.types),
        "format_type": classified.type_name,
        "needs_rule": classified.needs_rule,
    }
    if classified.match == "direct":
        row["value_range"] = classified.value_range
    if classified.match == "reference":
        row["format_object"] = classified.format_object
    if classified.match == "mismatch":
        row["reason"] += "; выберите другую пару или задайте ПКО/алгоритм преобразования"
    return row


@dataclass(frozen=True, slots=True)
class _Classified:
    match: str
    reason: str
    value_range: str | None
    needs_rule: bool
    type_name: str | None
    format_object: str | None


def _classify(
    schema: EdSchema,
    profile: ValidationProfile,
    prop: SchemaProperty,
    attr: StructureProperty,
    direction: str,
) -> _Classified:
    target = property_type(schema, prop)
    type_name = _type_name(prop, target)
    # Ссылку определяем до primitive_limits: цепочка к Ref заканчивается xs:string,
    # и классификатор примитивов сам по себе принял бы её за строку.
    # В EnterpriseData ссылочное свойство типизировано объектом ключевых свойств,
    # а не наследником Ref: `Организация` имеет тип `КлючевыеСвойстваОрганизация`.
    key_type = bool(target and _is_key_properties_type(target))
    fmt_ref = bool(target and is_reference(schema, target))
    cfg_ref = _all_references(attr)
    if (
        not attr.unresolved
        and len(attr.types) == 1
        and attr.types[0].startswith("ПеречислениеСсылка.")
        and _enumeration_values(schema, target) is not None
    ):
        return _Classified(
            "needs_pkpd",
            "Перечисление: нужно правило предопределённых данных",
            None,
            True,
            type_name,
            None,
        )
    if cfg_ref and (key_type or fmt_ref):
        return _Classified(
            "reference",
            "Ссылочные типы: нужно правило объекта или предопределённых данных",
            None,
            True,
            type_name,
            _key_owner(schema, target) if key_type and target is not None else None,
        )
    family, _, _ = primitive_limits(profile, prop)
    if family is not None and _single_primitive(attr) and not fmt_ref and not key_type:
        check = compatibility(profile, prop, attr, direction)
        if check.compatible:
            return _Classified("direct", check.reason, check.value_range, False, type_name, None)
        return _Classified("mismatch", check.reason, None, False, type_name, None)
    return _Classified(
        "mismatch",
        _mismatch_reason(attr, fmt_ref or key_type, family),
        None,
        False,
        type_name,
        None,
    )


def _enumeration_values(schema: EdSchema, typ: SchemaType | None) -> tuple[str, ...] | None:
    """Конечные значения полного одиночного типа; ограничения предков пересекаются."""
    seen = set()
    values: set[str] | None = None
    while typ is not None:
        if (
            typ.id in seen
            or typ.status != "complete"
            or typ.kind != "value"
            or typ.members
            or typ.variety.casefold() in ("list", "union")
        ):
            return None
        seen.add(typ.id)
        declared = {f.lexical for f in typ.facets if f.kind == "enumeration"}
        if declared:
            values = declared if values is None else values & declared
        if typ.base is None or typ.base.namespace == XS:
            break
        typ = schema.types.get(typ.base)
        if typ is None:
            return None
    return tuple(sorted(values)) if values is not None else None


def _add_value_pairs(structure, schema, header, row) -> None:
    """Только равные имена; несовпадения оставляем агенту явно."""
    prop = next(
        p for p in header if p.path == row["format_path"] and p.namespace == row["namespace"]
    )
    format_values = _enumeration_values(schema, property_type(schema, prop.prop)) or ()
    cfg = [
        str(v[0])
        for v in structure.execute(
            "SELECT v.name FROM object_values v JOIN objects o ON o.id=v.object_id "
            "WHERE o.kind='Перечисление' AND o.type_name=? ORDER BY v.name",
            (row["configuration_types"][0],),
        )
    ]
    pairs = [{"configuration": c, "format": f} for c in cfg for f in format_values if c == f]
    row.update(
        {
            "value_pairs": pairs,
            "unmatched_configuration_values": [c for c in cfg if c not in format_values],
            "unmatched_format_values": [f for f in format_values if f not in cfg],
        }
    )


def _reference_properties(
    binding, configuration, typ, direction, schema, attributes, header, document, document_id, index
):
    """Декларации ПКС шапки выбранной пары; обработчики никогда не включаются в ответ."""
    profile = ValidationProfile.build(schema, None, direction)
    applicability = Applicability.build(document, profile)
    pkpd = {r.declared_name or r.name for r in document.pkpd}
    pko = {r.declared_name or r.name for r in document.pko}
    rows, used_attr, used_fmt = [], set(), set()
    source_hash = tuple(f.sha256 for f in document.files)
    for rule in document.pko:
        key, _ = metadata_key(rule.configuration_object.value)
        fmt = rule.format_object.value if rule.format_object else None
        if key != tuple(part.casefold() for part in configuration.split(".", 1)):
            continue
        if not typ.qname or fmt != typ.qname.local:
            continue
        active = applicability.evaluate(rule, direction)
        if active is False:
            continue
        for prop in rule.properties:
            if prop.group_id or not prop.format_property:
                continue
            formats = [
                p
                for p in header
                if (p.leaf == prop.format_property or p.path == prop.format_property)
                and (not prop.namespace or p.namespace == prop.namespace)
            ]
            attrs = [
                a
                for a in attributes
                if a.prop.path.casefold() == prop.configuration_property.casefold()
            ]
            # Неразрешённые декларации не выдаём как доказанную пару текущих входов.
            if not formats or (prop.configuration_property and not attrs):
                continue
            kind = (
                "algorithm"
                if prop.algorithm_flag
                else "pkpd"
                if prop.conversion_rule in pkpd
                else "reference"
                if prop.conversion_rule in pko
                else "unresolved"
                if prop.conversion_rule
                else "direct"
            )
            for field in formats:
                used_attr.update(id(a) for a in attrs)
                used_fmt.add(id(field))
                rows.append(
                    {
                        "candidate_id": _candidate_id(
                            binding,
                            "reference_property",
                            str(source_hash),
                            direction,
                            configuration,
                            str(typ.qname),
                            prop.entity_id,
                            field.path,
                        ),
                        "configuration": attrs[0].prop.path if attrs else "",
                        "configuration_types": list(attrs[0].prop.types) if attrs else [],
                        "format_path": field.path,
                        "format_name": field.leaf,
                        "namespace": field.namespace,
                        "format_type": _type_name(field.prop, property_type(schema, field.prop)),
                        "class": "reference_module",
                        "confidence": "reference",
                        "auto": False,
                        "reason": "так в типовом модуле",
                        "direction": direction,
                        "property_kind": kind,
                        "rule_name": prop.conversion_rule or None,
                        "needs_rule": kind in ("reference", "pkpd", "unresolved"),
                        "applicability": "active" if active is True else "unknown",
                        "origin": {
                            "document_id": document_id,
                            "address": index.by_id.get(prop.entity_id, ("",))[0],
                        },
                    }
                )
    return rows, used_attr, used_fmt


def _merge_reference_properties(binding: str, rows: list[dict], references: list[dict]) -> None:
    """Равенство пары не зависит от вида ПКС: способы типового сохраняются как свидетельства.

    Имя реквизита сравнивается без регистра, физический путь XDTO и URI — точно.
    При разных путях обе пары остаются видны. Несколько деклараций не теряются.
    """

    def pair(row):
        return (_fold(row["configuration"]), row["format_path"], row["namespace"])

    def evidence(row):
        return {k: row[k] for k in ("property_kind", "rule_name", "origin", "applicability")}

    by_pair = {pair(row): row for row in rows}
    for reference in references:
        key = pair(reference)
        row = by_pair.get(key)
        if row is None:
            rows.append(reference)
            by_pair[key] = reference
            continue
        sources = row.setdefault(
            "references", [evidence(row)] if row["class"] == "reference_module" else []
        )
        source = evidence(reference)
        if source not in sources:
            sources.append(source)
            row["candidate_id"] = _candidate_id(
                binding, "property_evidence", row["candidate_id"], reference["candidate_id"]
            )


def _is_key_properties_type(typ: SchemaType) -> bool:
    return (
        typ.kind == "object"
        and typ.qname is not None
        and typ.qname.local.startswith("КлючевыеСвойства")
        and typ.qname.local != "КлючевыеСвойства"
    )


def _key_owner(schema: EdSchema, key_type: SchemaType) -> str | None:
    """Объектный тип формата, чьи `КлючевыеСвойства` имеют этот тип. None — не один."""
    active = {schema.base_namespace, *schema.extension_namespaces}
    owners: list[str] = []
    for qname, typ in schema.types.items():
        if qname.namespace not in active or typ.kind != "object" or typ.qname is None:
            continue
        if typ.id == key_type.id:
            continue
        for prop in schema.inherited.get(typ.id, ()):
            if prop.name.local != "КлючевыеСвойства":
                continue
            target = property_type(schema, prop)
            if target is not None and target.id == key_type.id and typ.qname.local not in owners:
                owners.append(typ.qname.local)
            break
    if len(owners) == 1:
        return owners[0]
    return None


def _type_name(prop: SchemaProperty, target: SchemaType | None) -> str | None:
    if target and target.qname:
        return target.qname.local
    if prop.type_ref is None:
        return None
    if prop.type_ref.namespace == XS:
        return prop.type_ref.local
    return str(prop.type_ref)


def _single_primitive(attr: StructureProperty) -> bool:
    return not attr.unresolved and len(attr.types) == 1 and attr.types[0] in _PRIMITIVES


def _all_references(attr: StructureProperty) -> bool:
    return (
        bool(attr.types)
        and not attr.unresolved
        and all(
            any(name.startswith(prefix) for prefix in _REFERENCE_PREFIXES) for name in attr.types
        )
    )


def _mismatch_reason(attr: StructureProperty, fmt_ref: bool, family: str | None) -> str:
    if attr.unresolved or len(attr.types) != 1:
        return "Тип реквизита составной или не разрешён"
    if _single_primitive(attr) and fmt_ref:
        return "Примитив реквизита и ссылка формата несовместимы"
    if _all_references(attr) and family is not None:
        return "Ссылка реквизита и примитив формата несовместимы"
    if _all_references(attr):
        return "Ссылка реквизита и объектный тип формата несовместимы"
    if family is not None:
        return "Тип реквизита и примитив свойства различаются"
    return "Типы реквизита и свойства формата несовместимы"


def _match_tables(
    binding: str,
    configuration: str,
    format_type: str,
    direction: str,
    tables: list[str],
    tabular: list[_FormatProperty],
) -> tuple[list[dict[str, Any]], set[str], set[int]]:
    by_leaf: dict[str, list[_FormatProperty]] = {}
    for item in tabular:
        by_leaf.setdefault(_fold(item.leaf), []).append(item)
    rows: list[dict[str, Any]] = []
    used_names: set[str] = set()
    used_fmt: set[int] = set()
    for name in tables:
        for prop in by_leaf.get(_fold(name), ()):
            rows.append(
                {
                    "candidate_id": _candidate_id(
                        binding, "table", direction, configuration, format_type, name, prop.path
                    ),
                    "configuration": name,
                    "format": prop.leaf,
                    "format_path": prop.path,
                    "auto": False,
                    "direction": direction,
                }
            )
            used_names.add(name)
            used_fmt.add(id(prop))
    return rows, used_names, used_fmt
