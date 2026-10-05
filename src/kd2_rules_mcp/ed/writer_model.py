"""Авторский оригинал полного менеджера ED; координаты не служат идентичностью."""

from __future__ import annotations

import base64
import hashlib
import json
import math
import re
import types
import zlib
from collections import OrderedDict
from dataclasses import MISSING, dataclass, field, fields, is_dataclass, replace
from functools import cache, lru_cache
from itertools import pairwise
from typing import Any, Literal, Union, get_args, get_origin, get_type_hints

from kd2_rules_mcp.errors import EdAuthoringResourceLimitError

ImportState = Literal["editable", "retained", "blocked"]
Direction = Literal["send", "receive", "both"]


def _direction_order(direction: str) -> int:
    return {"send": 0, "receive": 1, "both": 2}.get(direction, 3)


ValueState = Literal[
    "unset", "string", "boolean", "number", "date", "undefined", "reference", "unknown"
]
MAX_MODEL_BYTES = 128 * 1024 * 1024
MAX_ENTITIES = 100_000


def text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def json_value(value: Any) -> Any:
    """Детерминированное представление DTO; массивы сохраняют исходный порядок."""
    if is_dataclass(value) and not isinstance(value, type):
        return {
            item.name: json_value(getattr(value, item.name))
            for item in fields(value)
            if not item.name.startswith("_")
            and not (item.name == "restore_dispatcher" and getattr(value, item.name) is None)
            and not (item.name == "identification" and getattr(value, item.name) is None)
        }
    if isinstance(value, tuple | list):
        return [json_value(item) for item in value]
    if isinstance(value, dict):
        return {key: json_value(item) for key, item in value.items()}
    return value


def json_bytes(value: Any) -> bytes:
    return (
        json.dumps(
            value,
            default=_json_default,
            ensure_ascii=False,
            sort_keys=True,
            allow_nan=False,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")


def _json_default(value: Any) -> dict[str, Any]:
    """JSON-кодировщик сам обходит готовые словари; DTO раскрываются по одному узлу."""
    if is_dataclass(value) and not isinstance(value, type):
        return {
            f.name: getattr(value, f.name)
            for f in fields(value)
            if not f.name.startswith("_")
            and not (f.name == "restore_dispatcher" and getattr(value, f.name) is None)
            and not (f.name == "identification" and getattr(value, f.name) is None)
        }
    raise TypeError("Значение не поддержано форматом JSON ED")


def digest(value: Any) -> str:
    return hashlib.sha256(json_bytes(value)).hexdigest()


def logical_id(project_id: str, client_id: str) -> str:
    return "ed-" + digest((project_id, client_id))[:32]


# Узлы DTO неизменяемы: повторно хешируются только заменённые ветви дерева.
# Сильная ссылка исключает повторное использование id другого объекта.
_HASHES: OrderedDict[int, tuple[Any, str]] = OrderedDict()


@cache
def _hash_fields(kind: type) -> tuple[tuple[str, bytes], ...]:
    return tuple(
        (f.name, f.name.encode("ascii") + b"\0")
        for f in fields(kind)
        if not f.name.startswith("_")
        and not (kind is ManagerModel and f.name in ("revision", "import_report"))
    )


def content_hash(value: Any) -> str:
    kind = type(value)
    if kind in (str, int, bool, float, type(None)):
        return _scalar_hash(kind, value)
    stored = getattr(value, "_cached_hash", "")
    if stored:
        return stored
    dto = is_dataclass(value) and not isinstance(value, type)
    composite = dto or isinstance(value, tuple)
    if composite:
        cached = _HASHES.get(id(value))
        if cached is not None and cached[0] is value:
            return cached[1]
    if dto:
        hasher = hashlib.sha256(kind.__name__.encode("ascii"))
        for name, label in _hash_fields(kind):
            # Отсутствующие новые поля не меняют ревизию снимков предыдущего B1.
            if (
                (kind is SourceSlice and name == "container_id" and not getattr(value, name))
                or (
                    kind is SourceSlice
                    and name == "compact_rule_separator"
                    and not getattr(value, name)
                )
                or (
                    kind is Header and name == "helper_variant" and getattr(value, name) == "modern"
                )
                or (kind is Header and name == "clear_data_column" and not getattr(value, name))
                or (
                    kind is Header
                    and name == "module_identifier"
                    and getattr(value, name) == Value()
                )
                or (
                    kind is PropertyGroup
                    and name == "argument_presence"
                    and getattr(value, name) == (True, True, True)
                )
                or (
                    kind is Decision
                    and name in ("position_container", "position_after")
                    and getattr(value, name) is None
                )
                or (kind is ManagerModel and name == "module_styles" and not getattr(value, name))
                or (kind is CodeUnit and name == "parameters_text" and getattr(value, name) is None)
                or (kind is CodeUnit and name == "frame_comment" and not getattr(value, name))
                or (
                    kind is PredefinedRule and name == "field_comments" and not getattr(value, name)
                )
            ):
                continue
            hasher.update(label + bytes.fromhex(content_hash(getattr(value, name))))
        result = hasher.hexdigest()
    elif isinstance(value, tuple):
        hasher = hashlib.sha256(b"tuple")
        for item in value:
            hasher.update(bytes.fromhex(content_hash(item)))
        result = hasher.hexdigest()
    else:
        result = _scalar_hash(type(value), value)
    if composite:
        if hasattr(value, "_cached_hash"):
            object.__setattr__(value, "_cached_hash", result)
        else:
            if len(_HASHES) >= 250_000:
                _HASHES.popitem(last=False)
            _HASHES[id(value)] = (value, result)
    return result


@lru_cache(maxsize=65536)
def _scalar_hash(kind: type, value: Any) -> str:
    if kind is str:
        raw = value.encode("utf-8")
    elif kind is int:
        raw = (str(value) + "\n").encode("ascii")
    elif kind is bool:
        raw = b"true\n" if value else b"false\n"
    elif value is None:
        raw = b"null\n"
    else:
        raw = json_bytes(value)
    return hashlib.sha256(kind.__name__.encode("ascii") + b"\0" + raw).hexdigest()


@dataclass(frozen=True, slots=True)
class Value:
    """Отсутствие, литерал, ссылка и непонятое выражение различаются явно."""

    state: Literal[
        "unset", "string", "boolean", "number", "date", "undefined", "reference", "unknown"
    ] = "unset"
    value: str | int | float | bool | None = None
    raw: str = ""
    reference_parts: tuple[str, ...] = ()
    alternatives: tuple[Value, ...] = ()
    _cached_hash: str = field(default="", init=False, compare=False, repr=False)
    _cached_json: dict[str, Any] | None = field(default=None, init=False, compare=False, repr=False)

    def __post_init__(self) -> None:
        valid = {
            "unset": self.value is None,
            "undefined": self.value is None,
            "string": type(self.value) is str,
            "date": type(self.value) is str,
            "boolean": type(self.value) is bool,
            # Десятичный литерал хранится точной строкой, без округления через float.
            "number": type(self.value) in (int, float)
            or (
                isinstance(self.value, str)
                and bool(re.fullmatch(r"[+-]?\d+(?:\.\d+)?", self.value))
            ),
            "reference": self.value is None and bool(self.reference_parts),
            "unknown": self.value is None,
        }
        if not valid.get(self.state, False):
            raise ValueError("Неверное типизированное значение")
        if isinstance(self.value, float) and not math.isfinite(self.value):
            raise ValueError("Нефинитное число")
        if self.state not in ("reference", "unknown") and (
            self.raw or self.reference_parts or self.alternatives
        ):
            raise ValueError("Литерал не содержит непрозрачных присваиваний")


@dataclass(frozen=True, slots=True)
class TextStyle:
    encoding: str = "utf-8"
    newline: Literal["\n", "\r\n", "mixed"] = "\r\n"
    bom: bool = True
    indent: str = "\t"


@dataclass(frozen=True, slots=True)
class EntityStyle:
    """Мода оформления по виду и направлению; при равенстве частот — первый образец."""

    kind: Literal["pko", "pod", "identification", "pkpd", "parameter"]
    direction: Literal["send", "receive", "both"]
    assignment_width: int = 0
    field_widths: tuple[tuple[str, int], ...] = ()
    opening_blank_lines: int = 0
    indent: str = "\t"
    line_suffixes: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True, slots=True)
class Header:
    model_version: int = 1
    manager_name: str = "Manager"
    interface_version: int | None = 2
    title: Value = Value()
    generated_at: Value = Value()
    helper_variant: Literal["modern", "legacy-v2"] | None = "modern"
    clear_data_column: bool = False
    module_identifier: Value = Value()
    text_style: TextStyle = TextStyle()


@dataclass(frozen=True, slots=True)
class Evidence:
    name: str
    sha256: str


@dataclass(frozen=True, slots=True)
class Host:
    configuration: str = ""
    structure_id: str = ""
    structure_hash: str = ""
    extensions: tuple[Evidence, ...] = ()


@dataclass(frozen=True, slots=True)
class FormatBinding:
    key: str
    uri: str
    schema_hash: str
    schema_id: str = ""


@dataclass(frozen=True, slots=True)
class Formal:
    name: str | None
    by_value: bool = False
    default: Value = Value()


@dataclass(frozen=True, slots=True)
class Signature:
    routine_kind: Literal["procedure", "function"] = "procedure"
    exported: bool = False
    parameters: tuple[Formal, ...] = ()


@dataclass(frozen=True, slots=True)
class ExecutorProfile:
    profile_id: str = ""
    receive_mode: str = ""


@dataclass(frozen=True, slots=True)
class _LegacyExecutorProfile:
    """Только чтение прежних снимков; доказательства не попадают в текущую модель."""

    profile_id: str = ""
    revision: str = ""
    evidence: tuple[Evidence, ...] = ()
    capabilities: tuple[str, ...] = ()
    interfaces: tuple[int, ...] = (2,)
    signatures: tuple[Signature, ...] = ()
    helpers: tuple[Evidence, ...] = ()
    receive_mode: str = ""
    runtime_verified: bool = False


@dataclass(frozen=True, slots=True)
class Reference:
    kind: str
    target_id: str | None = None
    name: str = ""
    resolution: Literal["resolved", "missing", "ambiguous", "computed"] = "missing"


@dataclass(frozen=True, slots=True, kw_only=True)
class Member:
    logical_id: str
    name: str
    inside_leaf_id: str | None = None
    trailing_comment: str = ""
    state: ImportState = "editable"
    guards: tuple[str, ...] = ()
    _cached_hash: str = field(default="", init=False, compare=False, repr=False)
    _cached_json: dict[str, Any] | None = field(default=None, init=False, compare=False, repr=False)


@dataclass(frozen=True, slots=True, kw_only=True)
class Event(Member):
    event: str
    target: Reference


@dataclass(frozen=True, slots=True, kw_only=True)
class SearchSet(Member):
    fields: tuple[str, ...]


@dataclass(frozen=True, slots=True, kw_only=True)
class Identification(Member):
    mode: Value = Value()
    search_sets: tuple[SearchSet, ...] = ()
    not_found_policy: Value = Value()


@dataclass(frozen=True, slots=True, kw_only=True)
class Property(Member):
    configuration_property: str
    format_property: str
    property_kind: Literal["direct", "reference", "pkpd", "algorithm"] = "direct"
    algorithm_flag: int = 0
    conversion: Reference = Reference("conversion")
    namespace: str = ""
    condition_name: str = ""
    argument_presence: tuple[bool, ...] = (True, True, True)
    argument_values: tuple[Value, ...] = ()


@dataclass(frozen=True, slots=True, kw_only=True)
class PropertyGroup(Member):
    configuration_property: str
    format_property: str
    namespace: str = ""
    condition_name: str = ""
    row_type: str = ""
    # Совместимость снимков W2; поле не выбирает политику и всегда остаётся unset.
    update_policy: Value = Value()
    argument_presence: tuple[bool, ...] = (True, True, True)
    properties: tuple[Property, ...] = ()


@dataclass(frozen=True, slots=True, kw_only=True)
class ObjectRule(Member):
    procedure_name: str
    signature: Signature = Signature()
    directions: tuple[Direction, ...] = ()
    configuration_object: Value = Value()
    format_object: Value = Value()
    group_flag: Value = Value()
    identification: Identification
    events: tuple[Event, ...] = ()
    properties: tuple[Property, ...] = ()
    groups: tuple[PropertyGroup, ...] = ()
    extensions: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "directions", tuple(sorted(self.directions, key=_direction_order)))


@dataclass(frozen=True, slots=True, kw_only=True)
class ProcessingRule(Member):
    procedure_name: str
    signature: Signature = Signature()
    directions: tuple[Direction, ...] = ()
    configuration_selection: Value = Value()
    format_selection: Value = Value()
    clear_data: Value = Value()
    events: tuple[Event, ...] = ()
    used_pko: tuple[Reference, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "directions", tuple(sorted(self.directions, key=_direction_order)))


@dataclass(frozen=True, slots=True, kw_only=True)
class ValueMapping(Member):
    configuration_value: Value
    format_value: Value
    direction: Direction


@dataclass(frozen=True, slots=True, kw_only=True)
class PredefinedRule(Member):
    directions: tuple[Direction, ...] = ()
    data_kind: Literal["enumeration", "predefined"] = "enumeration"
    configuration_type: Value = Value()
    format_type: Value = Value()
    mappings: tuple[ValueMapping, ...] = ()
    field_comments: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True, slots=True, kw_only=True)
class Parameter(Member):
    default: Value = Value()
    default_source: Literal["implicit", "explicit"] = "implicit"


@dataclass(frozen=True, slots=True, kw_only=True)
class CodeUnit(Member):
    signature: Signature
    body: str
    sha256: str
    origin: Literal["imported_opaque", "authored"] = "imported_opaque"
    roles: tuple[str, ...] = ()
    dependencies: tuple[Reference, ...] = ()
    file_id: str = ""
    body_start: int = 0
    body_end: int = 0
    helper_verified: bool | None = None
    parameters_text: str | None = None
    frame_comment: str = ""

    def __post_init__(self) -> None:
        if text_hash(self.body) != self.sha256:
            raise ValueError("Хеш тела не совпадает")


@dataclass(frozen=True, slots=True, kw_only=True)
class RuleUse(Member):
    rule: Reference
    direction: Direction | None


@dataclass(frozen=True, slots=True, kw_only=True)
class Guard(Member):
    expression: str
    branch: str
    guard_kind: str
    parent_id: str | None = None
    direction: Direction | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class DispatcherCase(Member):
    dispatcher: Reference
    target: Reference
    arguments: tuple[Value, ...]
    returns: bool


@dataclass(frozen=True, slots=True)
class CodeOccurrence:
    """Лексическое вхождение; строка считается от начала точного тела, с единицы."""

    owner_id: str
    target_id: str
    kind: Literal["algorithm_call", "algorithm_literal", "rule_literal"]
    name: str
    start: int
    end: int
    line: int


@dataclass(frozen=True, slots=True)
class SourceMapEntry:
    reader_id: str
    logical_id: str
    address: str
    file_id: str
    source_hash: str
    char_start: int
    char_end: int
    line_start: int
    line_end: int
    _cached_hash: str = field(default="", init=False, compare=False, repr=False)


@dataclass(frozen=True, slots=True)
class SourceSnapshot:
    file_id: str
    source_name: str
    text: str
    sha256: str
    bom: bool = False
    _cached_hash: str = field(default="", init=False, compare=False, repr=False)

    def bytes(self) -> bytes:
        return (b"\xef\xbb\xbf" if self.bom else b"") + self.text.encode("utf-8")

    def __post_init__(self) -> None:
        if hashlib.sha256(self.bytes()).hexdigest() != self.sha256:
            raise ValueError("Хеш исходного файла не совпадает")
        if "/" in self.source_name or "\\" in self.source_name:
            raise ValueError("Исходный файл хранится без абсолютного пути")


@dataclass(frozen=True, slots=True, kw_only=True)
class RetainedBlock(Member):
    kind: str
    text: str
    sha256: str
    file_id: str
    source_hash: str
    owner_id: str | None = None
    regions: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()
    reason: str = "outside_w1"
    locks_context: bool = False
    char_start: int = 0
    char_end: int = 0
    dependencies: tuple[Reference, ...] = ()

    def __post_init__(self) -> None:
        if text_hash(self.text) != self.sha256:
            raise ValueError("Хеш сохранённого блока не совпадает")


@dataclass(frozen=True, slots=True)
class SourceSlice:
    """Отрезок и отпечаток; container_id — исходный владелец, BOM — только стиль шапки."""

    file_id: str
    char_start: int
    char_end: int
    fingerprint: str = ""
    assignment_width: int = 0
    opening_blank_lines: int = 0
    line_suffix: str = ""
    container_id: str = ""
    compact_rule_separator: bool = False

    def __post_init__(self) -> None:
        if (
            self.assignment_width < 0
            or self.opening_blank_lines < 0
            or any(c not in " \t" for c in self.line_suffix)
        ):
            raise ValueError("Повреждён стиль исходного отрезка")


@dataclass(frozen=True, slots=True)
class LayoutElement:
    logical_id: str
    kind: Literal["entity", "text", "container"]
    entity_id: str | None = None
    block_id: str | None = None
    container_id: str | None = None
    source: SourceSlice | None = None
    trailing_comment: str = ""
    _cached_hash: str = field(default="", init=False, compare=False, repr=False)
    _cached_json: Any = field(default=None, init=False, compare=False, repr=False)
    field: str = ""


@dataclass(frozen=True, slots=True)
class LayoutContainer:
    """Единственный порядок вывода — elements, без порядковых номеров и якорей.

    Открывающий и закрывающий каркас принадлежат контейнеру (opening/closing).
    При обходе это его крайние листья. Сохранённая процедура всегда один лист.
    Хвостовой комментарий принадлежит оператору и перемещается вместе с ним;
    отдельные строки комментариев и пустые строки — самостоятельные элементы,
    при перемещении соседнего оператора они остаются на своём месте.
    Коллекции сущностей — каталоги для чтения, их порядок не задаёт вывод.
    """

    logical_id: str
    kind: Literal[
        "module",
        "rule",
        "entrypoint",
        "conditional",
        "predefined",
        "values",
        "code",
        "dispatcher",
        "table_part",
    ]
    name: str
    owner_id: str | None = None
    state: ImportState = "editable"
    direction: Direction | None = None
    branch: Literal["if", "elseif", "chain"] = "if"
    signature: Signature = Signature()
    opening: SourceSlice | None = None
    closing: SourceSlice | None = None
    elements: tuple[LayoutElement, ...] = ()
    _cached_hash: str = field(default="", init=False, compare=False, repr=False)
    _cached_json: Any = field(default=None, init=False, compare=False, repr=False)


def layout_leaves(model: ManagerModel) -> tuple[tuple[str, SourceSlice], ...]:
    """Обход исходных листьев: каркас контейнера и его элементы ровно один раз."""
    containers = {c.logical_id: c for c in model.layouts}
    result = []

    def walk(key: str) -> None:
        container = containers[key]
        if container.opening is not None:
            result.append((key + "/opening", container.opening))
        for element in container.elements:
            if element.container_id is not None:
                walk(element.container_id)
            elif element.source is not None:
                result.append((element.logical_id, element.source))
        if container.closing is not None:
            result.append((key + "/closing", container.closing))

    for key in model.root_layouts:
        walk(key)
    return tuple(result)


def leaf_fingerprint(
    model: ManagerModel,
    item: LayoutElement | LayoutContainer,
    members: dict[str, Any] | None = None,
) -> str:
    """Отпечаток только своего оператора, без дочерних сущностей и координат."""
    if isinstance(item, LayoutContainer):
        if item.kind == "table_part":
            member = next(g for r in model.pko for g in r.groups if g.logical_id == item.logical_id)
            return digest(
                (
                    item.kind,
                    member.configuration_property,
                    member.format_property,
                    member.namespace,
                    member.condition_name,
                    member.argument_presence,
                    member.trailing_comment,
                )
            )
        if item.kind in ("code", "dispatcher"):
            unit = next(u for u in model.code_units if u.logical_id == item.logical_id)
            return digest(
                (item.kind, unit.name, unit.signature, unit.parameters_text, unit.frame_comment)
            )
        if item.kind == "predefined":
            member = next(r for r in model.pkpd if r.logical_id == item.logical_id)
            return digest(
                (
                    item.kind,
                    member.name,
                    member.configuration_type,
                    member.format_type,
                    member.directions,
                    member.data_kind,
                )
                + ((member.field_comments,) if member.field_comments else ())
            )
        return digest((item.kind, item.name, item.signature, item.direction, item.branch))
    if item.field.startswith("header."):
        name = item.field.removeprefix("header.")
        if name == "title":
            return digest((name, model.header.title, model.header.generated_at))
        return digest((name, getattr(model.header, name)))
    if members is None:
        members = {m.logical_id: m for m in model.members()}
    member = members.get(item.entity_id or "")
    if isinstance(member, DispatcherCase):
        owner = next(c for c in model.layouts if c.logical_id == member.dispatcher.target_id)
        first = next(
            e.entity_id
            for e in owner.elements
            if isinstance(members.get(e.entity_id or ""), DispatcherCase)
        )
        return digest((member, first == member.logical_id))
    if member is None:
        return digest((item.field, item.trailing_comment))
    if item.field:
        return digest((item.field, getattr(member, item.field, None), item.trailing_comment))
    excluded = {"logical_id", "inside_leaf_id", "guards", "state"}
    return digest(
        {
            f.name: getattr(member, f.name)
            for f in fields(member)
            if not f.name.startswith("_") and f.name not in excluded
        }
    )


def partition_report(model: ManagerModel) -> tuple[tuple[str, int, int, bool], ...]:
    """Проверяет разбиение по координатам и побайтовую склейку, а не покрытие строк."""
    leaves = layout_leaves(model)
    result = []
    for source in model.source_files:
        slices = [s for _, s in leaves if s.file_id == source.file_id]
        cursor = 0
        exact = True
        parts = []
        for span in slices:
            exact &= span.char_start == cursor and span.char_end >= span.char_start
            parts.append(source.text[span.char_start : span.char_end].encode("utf-8"))
            cursor = span.char_end
        raw = (b"\xef\xbb\xbf" if model.header.text_style.bom else b"") + b"".join(parts)
        exact &= cursor == len(source.text) and raw == source.bytes()
        result.append((source.file_id, len(raw), len(source.bytes()), exact))
    return tuple(result)


@dataclass(frozen=True, slots=True)
class Decision:
    """Позиция создания нужна для повторов и порядка вставок после одного соседа."""

    client_id: str
    operation_hash: str
    result_ids: tuple[str, ...] = ()
    address_aliases: tuple[str, ...] = ()
    position_container: str | None = None
    position_after: str | None = None


@dataclass(frozen=True, slots=True)
class ImportEntry:
    kind: str
    address: str
    logical_id: str
    state: ImportState
    reason: str = ""


@dataclass(frozen=True, slots=True)
class ImportReport:
    entries: tuple[ImportEntry, ...]
    diagnostics: tuple[tuple[str, str], ...] = ()
    source_partition: tuple[tuple[str, int, int, bool], ...] = ()

    @property
    def counts(self) -> dict[str, dict[str, int]]:
        result: dict[str, dict[str, int]] = {}
        for entry in self.entries:
            row = result.setdefault(
                entry.kind, dict.fromkeys(("editable", "retained", "blocked"), 0)
            )
            row[entry.state] += 1
        return result


@dataclass(frozen=True, slots=True)
class ManagerModel:
    project_id: str
    header: Header = Header()
    host: Host = Host()
    format_bindings: tuple[FormatBinding, ...] = ()
    executor_profile: ExecutorProfile = ExecutorProfile()
    module_styles: tuple[EntityStyle, ...] = ()
    pod: tuple[ProcessingRule, ...] = ()
    pko: tuple[ObjectRule, ...] = ()
    pkpd: tuple[PredefinedRule, ...] = ()
    parameters: tuple[Parameter, ...] = ()
    code_units: tuple[CodeUnit, ...] = ()
    conversion_events: tuple[Event, ...] = ()
    rule_uses: tuple[RuleUse, ...] = ()
    guards: tuple[Guard, ...] = ()
    dispatcher_cases: tuple[DispatcherCase, ...] = ()
    dispatcher_unknown_policy: Literal["error", "retained"] = "error"
    source_map: tuple[SourceMapEntry, ...] = ()
    retained_blocks: tuple[RetainedBlock, ...] = ()
    source_files: tuple[SourceSnapshot, ...] = ()
    layouts: tuple[LayoutContainer, ...] = ()
    root_layouts: tuple[str, ...] = ()
    decisions: tuple[Decision, ...] = ()
    confirmations: tuple[tuple[str, str], ...] = ()
    import_report: ImportReport = ImportReport(())
    revision: str = ""
    _cached_hash: str = field(default="", init=False, compare=False, repr=False)
    _cached_addresses: dict[str, str] | None = field(
        default=None, init=False, compare=False, repr=False
    )

    def __post_init__(self) -> None:
        if not self.layouts and not self.source_files:
            key = logical_id(self.project_id, "module")
            object.__setattr__(self, "layouts", (LayoutContainer(key, "module", "Manager"),))
            object.__setattr__(self, "root_layouts", (key,))

    def ordered_entity_ids(self, container_id: str) -> tuple[str, ...]:
        """Производный порядок чтения каталога; собственной нумерации у него нет."""
        containers = {c.logical_id: c for c in self.layouts}
        result = []

        def walk(key):
            for element in containers[key].elements:
                if element.container_id is not None:
                    result.append(element.container_id)
                    walk(element.container_id)
                elif element.entity_id is not None and not element.field:
                    result.append(element.entity_id)

        walk(container_id)
        return tuple(result)

    def members(self) -> tuple[Member, ...]:
        rows: list[Member] = [
            *self.pod,
            *self.pko,
            *self.pkpd,
            *self.parameters,
            *self.code_units,
            *self.conversion_events,
            *self.rule_uses,
            *self.guards,
            *self.dispatcher_cases,
            *self.retained_blocks,
        ]
        for rule in self.pko:
            rows.extend(
                (
                    rule.identification,
                    *rule.identification.search_sets,
                    *rule.events,
                    *rule.properties,
                    *rule.groups,
                )
            )
            for group in rule.groups:
                rows.extend(group.properties)
        for pod in self.pod:
            rows.extend(pod.events)
        for predefined in self.pkpd:
            rows.extend(predefined.mappings)
        return tuple(rows)

    @property
    def counts(self) -> dict[str, int]:
        return {
            "pko": len(self.pko),
            "pod": len(self.pod),
            "pkpd": len(self.pkpd),
            "pks": sum(
                len(r.properties) + sum(len(g.properties) for g in r.groups) for r in self.pko
            ),
            "pktch": sum(len(r.groups) for r in self.pko),
            "search_sets": sum(len(r.identification.search_sets) for r in self.pko),
            "values": sum(len(r.mappings) for r in self.pkpd),
            "parameters": len(self.parameters),
        }

    def with_revision(self) -> ManagerModel:
        return replace(self, revision=content_hash(self))


def _compact(value: Any) -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        cached = getattr(value, "_cached_json", None)
        if cached is not None:
            return cached
        result = {}
        for item in fields(value):
            if item.name.startswith("_"):
                continue
            current = getattr(value, item.name)
            default = item.default
            if default is not MISSING and current == default:
                continue
            result[item.name] = _compact(current)
        if hasattr(value, "_cached_json"):
            object.__setattr__(value, "_cached_json", result)
        return result
    if isinstance(value, tuple):
        return [_compact(item) for item in value]
    return value


def snapshot_parts(model: ManagerModel) -> tuple[dict[str, Any], dict[str, bytes]]:
    """Тексты хранятся контентно один раз, блоки ссылаются на диапазоны исходника."""
    model = model.with_revision()
    external = {"source_map", "source_files", "code_units", "retained_blocks", "import_report"}
    value = {
        f.name: _compact(getattr(model, f.name))
        for f in fields(model)
        if not f.name.startswith("_")
        and f.name not in external
        and (f.default is MISSING or getattr(model, f.name) != f.default)
    }
    blobs = {s.sha256: s.bytes() for s in model.source_files}
    sources = {s.file_id: s for s in model.source_files}
    if model.source_files:
        value["source_files"] = [
            {k: v for k, v in _compact(s).items() if k != "text"} for s in model.source_files
        ]
    if model.code_units:
        value["code_units"] = []
        for unit in model.code_units:
            row = dict(_compact(unit))
            row.pop("body")
            value["code_units"].append(row)
            source = sources.get(unit.file_id)
            if source is None or source.text[unit.body_start : unit.body_end] != unit.body:
                blobs[unit.sha256] = unit.body.encode("utf-8")
                row["blob"] = True
    if model.retained_blocks:
        value["retained_blocks"] = []
        sources = {s.file_id: s for s in model.source_files}
        for block in model.retained_blocks:
            row = dict(_compact(block))
            row.pop("text")
            source = sources.get(block.file_id)
            if source is None or source.text[block.char_start : block.char_end] != block.text:
                blobs[block.sha256] = block.text.encode("utf-8")
                row["blob"] = True
            value["retained_blocks"].append(row)
    if model.source_map:
        indexes = {s.file_id: n for n, s in enumerate(model.source_files)}
        value["source_map"] = [
            [
                e.reader_id,
                e.logical_id,
                e.address,
                indexes[e.file_id],
                e.char_start,
                e.char_end,
                e.line_start,
                e.line_end,
            ]
            for e in model.source_map
        ]
    return value, blobs


def dump_model(model: ManagerModel) -> bytes:
    validate_model(model)
    value, blobs = snapshot_parts(model)
    result = json_bytes(
        {
            "storage_version": 3,
            "model": pack_json(value),
            "blobs": {k: base64.b64encode(v).decode("ascii") for k, v in blobs.items()},
            "import_report": pack_json(_compact(model.import_report)),
        }
    )
    if len(result) > MAX_MODEL_BYTES:
        raise EdAuthoringResourceLimitError("Превышен лимит модели ED")
    return result


@cache
def _hints(kind: type) -> dict[str, Any]:
    return get_type_hints(kind)


@cache
def _shape(kind: Any) -> tuple[Any, tuple[Any, ...]]:
    return get_origin(kind), get_args(kind)


@cache
def _dto_fields(kind: type) -> frozenset[str]:
    return frozenset(f.name for f in fields(kind) if f.init)


def decode_dto(kind: Any, value: Any) -> Any:
    """Строгая загрузка закрытых DTO: неизвестные поля и неверные типы запрещены."""
    if kind in (str, int, bool, float, type(None)):
        if type(value) is not kind:
            raise ValueError("Неверный тип поля DTO")
        return value
    origin, arguments = _shape(kind)
    if origin in (types.UnionType, Union):
        for alternative in arguments:
            try:
                return decode_dto(alternative, value)
            except (TypeError, ValueError):
                pass
        raise ValueError("Значение не соответствует объединению DTO")
    if origin is Literal:
        if not any(type(value) is type(item) and value == item for item in arguments):
            raise ValueError("Неизвестное значение перечисления")
        return value
    if origin is tuple:
        if not isinstance(value, list | tuple):
            raise ValueError("Ожидался массив")
        if len(arguments) == 2 and arguments[1] is Ellipsis:
            return tuple(decode_dto(arguments[0], item) for item in value)
        if len(arguments) != len(value):
            raise ValueError("Неверная длина массива DTO")
        return tuple(decode_dto(typ, item) for typ, item in zip(arguments, value, strict=True))
    if isinstance(kind, type) and is_dataclass(kind):
        if not isinstance(value, dict):
            raise ValueError("Ожидался объект DTO")
        hints = _hints(kind)
        if set(value) - _dto_fields(kind):
            raise ValueError("Неизвестные поля DTO")
        decoded = {}
        for key, item in value.items():
            try:
                decoded[key] = decode_dto(hints[key], item)
            except ValueError as error:
                raise ValueError(f"{kind.__name__}.{key}: {error}") from error
        try:
            return kind(**decoded)
        except TypeError as error:
            raise ValueError("Отсутствует обязательное поле DTO") from error
    if type(value) is not kind:
        raise ValueError("Неверный тип поля DTO")
    return value


def load_model(
    data: bytes | str,
    *,
    blobs: dict[str, bytes] | None = None,
    import_report: ImportReport | None = None,
) -> ManagerModel:
    restored_report = import_report
    if len(data if isinstance(data, bytes) else data.encode("utf-8")) > MAX_MODEL_BYTES:
        raise EdAuthoringResourceLimitError("Превышен лимит модели ED")
    value = json.loads(data)
    if "storage_version" in value:
        if value["storage_version"] != 3 or set(value) - {
            "storage_version",
            "model",
            "blobs",
            "import_report",
        }:
            raise ValueError("Неизвестный формат снимка ED")
        available = (
            blobs
            if blobs is not None
            else {k: base64.b64decode(v, validate=True) for k, v in value.get("blobs", {}).items()}
        )
        for key, content in available.items():
            if hashlib.sha256(content).hexdigest() != key:
                raise ValueError("Хеш содержимого снимка ED не совпадает")
        report = import_report or decode_dto(
            ImportReport, unpack_json(value.get("import_report", {"entries": []}))
        )
        restored_report = report
        value = unpack_json(value["model"])
        sources = []
        for source in value.get("source_files", []):
            raw = available[source["sha256"]]
            source["text"] = raw.decode("utf-8-sig" if source.get("bom") else "utf-8")
            sources.append(source)
        for unit in value.get("code_units", []):
            if unit.pop("blob", False):
                unit["body"] = available[unit["sha256"]].decode("utf-8")
            else:
                source = next(s for s in sources if s["file_id"] == unit["file_id"])
                unit["body"] = source["text"][unit.get("body_start", 0) : unit.get("body_end", 0)]
        for block in value.get("retained_blocks", []):
            if block.pop("blob", False):
                block["text"] = available[block["sha256"]].decode("utf-8")
            else:
                source = next(s for s in sources if s["file_id"] == block["file_id"])
                block["text"] = source["text"][
                    block.get("char_start", 0) : block.get("char_end", 0)
                ]
        value["source_map"] = [
            dict(
                zip(
                    (
                        "reader_id",
                        "logical_id",
                        "address",
                        "file_id",
                        "source_hash",
                        "char_start",
                        "char_end",
                        "line_start",
                        "line_end",
                    ),
                    (*row[:3], sources[row[3]]["file_id"], sources[row[3]]["sha256"], *row[4:]),
                    strict=True,
                )
            )
            for row in value.get("source_map", [])
        ]
        # Отчёт уже проверен отдельно; повторное разворачивание и декодирование
        # десятков тысяч записей не относится к восстановлению ревизии модели.
        value.pop("import_report", None)
    previous_profile = value.get("executor_profile", {})
    if isinstance(previous_profile, dict):
        legacy_fields = _dto_fields(_LegacyExecutorProfile) - _dto_fields(ExecutorProfile)
        value["executor_profile"] = {
            key: item for key, item in previous_profile.items() if key not in legacy_fields
        }
    model: ManagerModel = decode_dto(ManagerModel, value)
    if restored_report is not None:
        model = replace(model, import_report=restored_report)
    validate_model(model)
    if model.revision != model.with_revision().revision:
        if model.revision != _legacy_profile_revision(model, previous_profile):
            raise ValueError("Ревизия модели не совпадает с содержимым")
        model = model.with_revision()
    return model


def _legacy_profile_revision(model: ManagerModel, payload: Any) -> str:
    """Сверяет прежнюю ревизию до удаления доверенных ранее полей профиля."""
    old = decode_dto(_LegacyExecutorProfile, payload)
    profile_hash = hashlib.sha256(b"ExecutorProfile")
    for item in fields(old):
        profile_hash.update(
            item.name.encode("ascii") + b"\0" + bytes.fromhex(content_hash(getattr(old, item.name)))
        )
    result = hashlib.sha256(b"ManagerModel")
    for name, label in _hash_fields(ManagerModel):
        if name == "module_styles" and not model.module_styles:
            continue
        hashed = (
            profile_hash.hexdigest()
            if name == "executor_profile"
            else content_hash(getattr(model, name))
        )
        result.update(label + bytes.fromhex(hashed))
    return result.hexdigest()


def pack_json(value: Any) -> dict[str, str]:
    """Сжатие без времени/имён файлов; внешняя оболочка остаётся JSON."""
    return {
        "encoding": "zlib+base64",
        "data": base64.b64encode(zlib.compress(json_bytes(value), 1)).decode("ascii"),
    }


def unpack_json(value: Any) -> Any:
    if not isinstance(value, dict) or "encoding" not in value:
        return value
    if set(value) != {"encoding", "data"} or value["encoding"] != "zlib+base64":
        raise ValueError("Неизвестное кодирование снимка ED")
    compressed = base64.b64decode(value["data"], validate=True)
    decoder = zlib.decompressobj()
    try:
        raw = decoder.decompress(compressed, MAX_MODEL_BYTES + 1)
    except zlib.error as error:
        raise ValueError("Повреждённое сжатое содержимое ED") from error
    if len(raw) > MAX_MODEL_BYTES or not decoder.eof or decoder.unused_data:
        raise ValueError("Неверный размер сжатого снимка ED")
    return json.loads(raw)


def validate_model(model: ManagerModel) -> None:
    """Структурные инварианты; проверка исполнителя и схемы относится к запуску B."""
    if (
        not model.project_id
        or type(model.header.model_version) is not int
        or model.header.model_version != 1
    ):
        raise ValueError("Неверная идентичность или версия модели")
    if model.header.interface_version is not None and (
        type(model.header.interface_version) is not int
        or model.header.interface_version not in (1, 2, 3)
    ):
        raise ValueError("Неверный интерфейс менеджера")
    if (
        model.header.helper_variant not in (None, "modern", "legacy-v2")
        or type(model.header.clear_data_column) is not bool
        or model.header.module_identifier.state not in ("unset", "string")
        or any(c in str(model.header.module_identifier.value) for c in "\r\n")
    ):
        raise ValueError("Неверные поля шапки W3")
    members = model.members()
    ids = [member.logical_id for member in members]
    if len(ids) > MAX_ENTITIES:
        raise EdAuthoringResourceLimitError("Превышен лимит сущностей ED")
    if len(ids) != len(set(ids)) or any(not key for key in ids):
        raise ValueError("Повтор или отсутствие логического идентификатора")
    if any(
        member.state not in ("editable", "retained", "blocked")
        or type(member.logical_id) is not str
        or type(member.name) is not str
        for member in members
    ):
        raise ValueError("Неверное состояние или имя сущности")
    if len({item.key for item in model.format_bindings}) != len(model.format_bindings):
        raise ValueError("Повтор ключа версии формата")
    if len({(s.kind, s.direction) for s in model.module_styles}) != len(model.module_styles):
        raise ValueError("Повтор стиля вида и направления")
    for style in model.module_styles:
        if (
            style.kind not in ("pko", "pod", "identification", "pkpd", "parameter")
            or style.direction not in ("send", "receive", "both")
            or style.assignment_width < 0
            or style.opening_blank_lines < 0
            or not style.indent
            or any(c not in " \t" for c in style.indent)
            or len(dict(style.field_widths)) != len(style.field_widths)
            or any(n < 0 for _, n in style.field_widths)
            or len(dict(style.line_suffixes)) != len(style.line_suffixes)
            or any(any(c not in " \t" for c in suffix) for _, suffix in style.line_suffixes)
        ):
            raise ValueError("Неверный стиль модуля")
    if len({item.client_id for item in model.decisions}) != len(model.decisions):
        raise ValueError("Повтор идентификатора решения")
    for rule in (*model.pko, *model.pod, *model.pkpd):
        if (
            any(direction not in ("send", "receive", "both") for direction in rule.directions)
            or len(set(rule.directions)) != len(rule.directions)
            or ("both" in rule.directions and len(rule.directions) != 1)
        ):
            raise ValueError("Неизвестное направление правила")
    live = set(ids)
    containers = {c.logical_id: c for c in model.layouts}
    if len(containers) != len(model.layouts):
        raise ValueError("Повтор контейнера раскладки")
    blocks = {b.logical_id: b for b in model.retained_blocks}
    visited: set[str] = set()
    elements: set[str] = set()
    leaves: set[str] = set()
    entities: set[str] = set()
    sources = {s.file_id: s for s in model.source_files}
    if model.source_files and not model.root_layouts:
        raise ValueError("Исходник без раскладки")

    def check_slice(span: SourceSlice | None) -> None:
        if span is not None and (
            span.file_id not in sources
            or not 0 <= span.char_start <= span.char_end <= len(sources[span.file_id].text)
        ):
            raise ValueError("Неверный отрезок раскладки")

    def walk(key: str) -> None:
        if key not in containers or key in visited:
            raise ValueError("Висячий, повторный или циклический контейнер")
        visited.add(key)
        container = containers[key]
        if container.kind not in (
            "module",
            "rule",
            "entrypoint",
            "conditional",
            "predefined",
            "values",
            "code",
            "dispatcher",
            "table_part",
        ) or container.state not in ("editable", "retained", "blocked"):
            raise ValueError("Неверный вид или состояние контейнера")
        check_slice(container.opening)
        check_slice(container.closing)
        for element in container.elements:
            if element.kind not in ("entity", "text", "container"):
                raise ValueError("Неверный вид элемента раскладки")
            if element.logical_id in elements:
                raise ValueError("Повтор элемента раскладки")
            elements.add(element.logical_id)
            check_slice(element.source)
            if element.kind == "container":
                if (
                    element.container_id is None
                    or element.source is not None
                    or element.block_id is not None
                ):
                    raise ValueError("Контейнер не может одновременно быть листом")
                child = containers.get(element.container_id)
                if child is not None and child.kind == "conditional" and child.owner_id != key:
                    raise ValueError("Владелец условной группы противоречит раскладке")
                walk(element.container_id)
            else:
                if element.container_id is not None:
                    raise ValueError("У листа есть вложенный контейнер")
                if element.kind == "entity" and element.entity_id is None:
                    raise ValueError("Элемент сущности без ссылки")
                if element.entity_id is not None:
                    if element.entity_id not in live and not element.field.startswith("header."):
                        raise ValueError("Висячая сущность раскладки")
                    entities.add(element.entity_id)
                if element.block_id is not None:
                    if element.block_id not in blocks or element.block_id in leaves:
                        raise ValueError("Висячий или повторный сохранённый лист")
                    leaves.add(element.block_id)
                    if blocks[element.block_id].owner_id != key:
                        raise ValueError("Владелец блока противоречит раскладке")
                if element.kind == "text" and element.block_id is None:
                    raise ValueError("Текстовый лист без блока")

    for key in model.root_layouts:
        if key in containers and containers[key].kind != "module":
            raise ValueError("Корень раскладки должен быть модулем")
        walk(key)
    if visited != set(containers) or leaves != set(blocks):
        raise ValueError("Раскладка содержит недостижимые контейнеры или блоки")
    for container in model.layouts:
        if container.kind == "conditional" and container.owner_id not in containers:
            raise ValueError("Условная группа без владельца")
        if (
            container.kind == "conditional"
            and container.branch == "chain"
            and (
                container.direction is not None
                or any(e.kind != "container" for e in container.elements)
            )
        ):
            raise ValueError("Цепочка условий содержит только упорядоченные ветки")
    intervals: dict[str, list[tuple[int, int]]] = {}
    for _, span in layout_leaves(model):
        if span.char_end > span.char_start:
            intervals.setdefault(span.file_id, []).append((span.char_start, span.char_end))
    for rows in intervals.values():
        rows.sort()
        if any(left < previous_end for (_, previous_end), (left, _) in pairwise(rows)):
            raise ValueError("Исходные листья раскладки перекрываются")
    for member in members:
        if member.inside_leaf_id is not None and (
            member.inside_leaf_id not in leaves or member.logical_id in entities
        ):
            raise ValueError(
                "Представление должно принадлежать живому листу и не входить в раскладку"
            )
    for rule in model.pko:
        if any(g.update_policy != Value() for g in rule.groups):
            raise ValueError(
                "Политика обновления ТЧ не выбирается: исполнитель заменяет её целиком"
            )
        for prop in (*rule.properties, *(p for g in rule.groups for p in g.properties)):
            if prop.state == "editable" and (
                len(prop.argument_presence) != len(prop.argument_values) + 1
                or not 3 <= len(prop.argument_presence) <= 7
                or (
                    prop.namespace
                    and (len(prop.argument_presence) < 6 or not prop.argument_presence[5])
                )
                or any(
                    flag != (value.state != "unset")
                    for flag, value in zip(
                        prop.argument_presence[1:], prop.argument_values, strict=False
                    )
                )
                or prop.argument_values[:2]
                != (
                    Value("string", prop.configuration_property),
                    Value("string", prop.format_property),
                )
            ):
                raise ValueError("Аргументы ПКС не соответствуют полям")
