"""Чтение метаданных выгрузки и ограниченной грамматики перехватчиков.

BSL не исполняется. Аннотация разбирается как один directive-токен лексера,
а не поиском по файлу. Непонятая форма становится пропуском с файлом и строкой.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from lxml import etree as ET

from .coverage import build_coverage
from .errors import EdFormatError, EdReadError, EdResourceLimitError
from .forms import (
    DISPATCHERS,
    ENTRYPOINTS,
    PKO_FIELDS,
    PKPD_FIELDS,
    POD_FIELDS,
    helper_forms,
)
from .layer_model import (
    Applicability,
    Continuation,
    ExtensionReading,
    Footprint,
    Hook,
    HookKind,
    LayerDescriptor,
    LayerOperation,
    LayerSkip,
    MapEntry,
    OperationKind,
    Origin,
    Pred,
    SourceFile,
)
from .lexer import Statement, Token, lex, normalized, split_arguments, tokenize
from .model import (
    Classification,
    DispatcherCase,
    Entity,
    Expr,
    FormalParameter,
    Guard,
    HandlerBinding,
    ObjectRule,
    Parameter,
    ParseStatus,
    PredefinedRule,
    ProcessingRule,
    PropertyGroup,
    PropertyRule,
    Routine,
    RuleRef,
    SearchSet,
    SourceSpan,
    ValueMapping,
)
from .model import (
    Field as ValueField,
)

MAX_FILE_BYTES = 16 * 1024 * 1024
MAX_METHODS = 64
MAX_CALLS = 256
MAX_CONDITIONS = 32
MAX_EDGES = 4
MAX_OPS = 100_000
_MD = "http://v8.1c.ru/8.3/MDClasses"
_PARSER = ET.XMLParser(resolve_entities=False, no_network=True, load_dtd=False, huge_tree=False)
_UNIT = "\x1f"
_FILLER_NAMES = {name.casefold() for name in ENTRYPOINTS}
_DISPATCH_NAMES = {name.casefold() for name in DISPATCHERS}
_ROUTE_CALLBACKS = {
    "приполучениинастроек": ("procedure", ("Настройки",)),
    "приполучениидоступныхверсийформата": ("procedure", ("ВерсииФормата",)),
    "приполучениидоступныхрасширенийформата": ("procedure", ("РасширенияФормата",)),
}
_FILLER_VARIANTS: dict[str, tuple[tuple[str, tuple[str, ...]], ...]] = {
    "заполнитьправилаобработкиданных": (
        ("procedure", ("НаправлениеОбмена", "ПравилаОбработкиДанных")),
    ),
    "заполнитьправилаконвертацииобъектов": (
        ("procedure", ("НаправлениеОбмена", "ПравилаКонвертации")),
        ("procedure", ("КомпонентыОбмена", "ПравилаКонвертации", "ТолькоЗаголовки")),
    ),
    "заполнитьправилаконвертациипредопределенныхданных": (
        ("procedure", ("НаправлениеОбмена", "ПравилаКонвертации")),
    ),
    "заполнитьпараметрыконвертации": (("procedure", ("ПараметрыКонвертации",)),),
    "выполнитьпроцедурумодуляменеджера": (("procedure", ("ИмяПроцедуры", "Параметры")),),
    "выполнитьфункциюмодуляменеджера": (("function", ("ИмяФункции", "Параметры")),),
}
_HOOK_KINDS = {
    "перед": HookKind.BEFORE,
    "before": HookKind.BEFORE,
    "после": HookKind.AFTER,
    "after": HookKind.AFTER,
    "вместо": HookKind.AROUND,
    "around": HookKind.AROUND,
    "изменениеиконтроль": HookKind.CHANGE_CONTROL,
    "changeandvalidate": HookKind.CHANGE_CONTROL,
}
_NAME_FIELDS = frozenset({"имяпко", "имя", "имяпкпд"})
_PROPERTY_FIELDS = {
    "свойствоконфигурации": "configuration_property",
    "свойствоформата": "format_property",
    "используетсяалгоритмконвертации": "algorithm_flag",
    "правилоконвертациисвойства": "conversion_rule",
    "пространствоимен": "namespace",
    "условиеприменения": "condition_name",
}
_PKO_SET = {name.casefold(): name for name in PKO_FIELDS}
_POD_SET = {name.casefold(): name for name in POD_FIELDS}
_PKPD_SET = {name.casefold(): name for name in PKPD_FIELDS}


@dataclass(frozen=True, slots=True)
class DumpObject:
    kind: str
    name: str
    uuid: str | None
    belonging: str | None
    extended_uuid: str | None
    module_extended: bool
    xml_path: str | None
    module_path: str | None
    server: bool | None


@dataclass(frozen=True, slots=True)
class DumpInfo:
    root: str
    name: str | None
    configuration_uuid: str | None
    objects: tuple[DumpObject, ...]
    skips: tuple[LayerSkip, ...]
    fingerprint: str
    failed: bool = False


@dataclass(frozen=True, slots=True)
class FillerCall:
    name: str
    collection: str
    pred: Pred
    span: SourceSpan
    effect: str = "call"


class _Abort(Exception):
    def __init__(self, reason: str, origin: Origin) -> None:
        super().__init__(reason)
        self.reason = reason
        self.origin = origin


@dataclass
class _Bind:
    tag: str
    collection: str = ""
    column: str = ""
    literal: str | None = None
    rule_key: str = ""
    format_property: str = ""
    config_property: str = ""
    map_role: str = ""
    plan_name: str | None = None
    builder: str = ""
    snapshot: str = ""
    paths: tuple[tuple[Pred, _Bind], ...] = ()


def _join_bindings(env: dict[str, _Bind], paths: list[tuple[Pred, dict[str, _Bind]]]) -> None:
    """Привязка на стыке хранит условия выходов, никогда не прежнее значение."""
    for name in set(env).union(*(child.keys() for _, child in paths)):
        alternatives = [
            (path, child.get(name, _Bind("unknown")))
            for path, child in paths
            if not _is_false(path)
        ]
        if not alternatives:
            continue
        values = [value for _, value in alternatives]
        env[name] = (
            values[0]
            if all(value == values[0] for value in values)
            else _Bind("choice", paths=tuple(alternatives))
        )


def _collection_parameters(routine: Routine, env: dict[str, _Bind]) -> dict[str, str]:
    return {
        (param.name or "").casefold(): env[(param.name or "").casefold()].collection
        for param in routine.parameters
        if not param.by_value
        and env.get((param.name or "").casefold(), _Bind("")).tag == "collection"
    }


def _variable_declarations(source: SourceFile) -> tuple[set[str], dict[str, set[str]]]:
    """Переменная модуля не становится локальной от присваивания в процедуре."""
    module: set[str] = set()
    local: dict[str, set[str]] = {}
    routine = ""
    for statement in lex(source).statements:
        if statement.head in ("процедура", "функция"):
            routine = statement.tokens[1].folded
        elif statement.head in ("конецпроцедуры", "конецфункции"):
            routine = ""
        elif statement.head == "перем":
            names = {
                token.folded
                for token in statement.tokens[1:]
                if token.kind == "identifier" and token.folded != "экспорт"
            }
            (local.setdefault(routine, set()) if routine else module).update(names)
    return module, local


def _dynamic_code(statement: Statement) -> bool:
    return any(
        token.kind == "identifier"
        and token.folded in ("выполнить", "вычислить", "execute", "eval")
        and (index == 0 or statement.tokens[index - 1].value != ".")
        for index, token in enumerate(statement.tokens)
    )


def _environment_footprints(
    env: dict[str, _Bind], parameters: dict[str, str]
) -> tuple[Footprint, ...]:
    result = {Footprint("collection", collection) for collection in parameters.values()}
    for bind in env.values():
        if bind.tag == "collection":
            result.add(Footprint("collection", bind.collection))
        elif bind.collection:
            ref = bind.rule_key or bind.builder or bind.collection
            result.add(Footprint("entity" if _UNIT in ref else "collection", ref))
    return tuple(sorted(result, key=lambda fp: (fp.ref, fp.scope)))


def _scalar_read(tokens: tuple[Token, ...], index: int, env: dict[str, _Bind]) -> bool:
    """Только скалярное поле, без последующего метода/индекса/доступа к объекту."""
    if index + 2 >= len(tokens) or tokens[index + 1].value != ".":
        return False
    bind = env.get(tokens[index].folded)
    if not bind:
        return False
    if bind.tag == "collection":
        return (
            index + 4 < len(tokens)
            and tokens[index + 2].folded == "количество"
            and tokens[index + 3].value == "("
            and tokens[index + 4].value == ")"
        )
    fields = (
        _PROPERTY_FIELDS
        if bind.tag == "property"
        else {"pko": _PKO_SET, "pod": _POD_SET, "pkpd": _PKPD_SET}.get(bind.collection, {})
    )
    field = tokens[index + 2].folded
    return (
        bind.tag in ("rule", "property", "builder")
        and (field in fields or field in _NAME_FIELDS)
        and field not in ("свойства", "свойстватабличныхчастей", "используемыепко")
        and (index + 3 == len(tokens) or tokens[index + 3].value not in (".", "[", "("))
    )


def _operation_success(operation: LayerOperation) -> Pred | None:
    if operation.resolution != "applied" or _UNIT not in operation.target_ref:
        return None
    if operation.kind not in (OperationKind.ADD, OperationKind.SET, OperationKind.DELETE):
        return None
    if operation.kind == OperationKind.ADD and not operation.field_path:
        return None
    if operation.target_ref.startswith("parameters" + _UNIT):
        return None
    path = operation.field_path
    if path[:1] == ("properties",) and len(path) >= (
        5 if operation.kind == OperationKind.SET else 4
    ):
        column, snapshot = path[-2:]
        return Pred.atom(
            "property_present",
            _UNIT.join((operation.target_ref, column, path[1])) + "\x1e" + snapshot,
        )
    return Pred.atom("rule_found", operation.target_ref)


@dataclass
class _Stmt:
    statement: Statement


@dataclass
class _Branch:
    """Одна ветвь ``Если``/``ИначеЕсли``/``Иначе``. ``condition is None`` — ветвь ``Иначе``."""

    condition: tuple[Token, ...] | None
    body: list[_Node]
    statement: Statement


@dataclass
class _If:
    branches: list[_Branch]
    statement: Statement

    @property
    def condition(self) -> tuple[Token, ...]:
        first = self.branches[0].condition if self.branches else None
        return first or ()

    @property
    def then(self) -> list[_Node]:
        return self.branches[0].body if self.branches else []

    @property
    def otherwise(self) -> list[_Node]:
        for branch in self.branches[1:]:
            if branch.condition is None:
                return branch.body
        return []


@dataclass
class _Loop:
    body: list[_Node]
    statement: Statement


@dataclass
class _Try:
    body: list[_Node]
    handler: list[_Node]
    statement: Statement


_Node = _Stmt | _If | _Loop | _Try


@dataclass
class _Builder:
    kind: str
    variable: str
    collection: str
    start: Statement
    end: Statement
    name: str | None = None
    fields: dict[str, Expr] = field(default_factory=dict)
    properties: list[PropertyRule] = field(default_factory=list)
    groups: list[PropertyGroup] = field(default_factory=list)
    events: list[HandlerBinding] = field(default_factory=list)
    used: list[RuleRef] = field(default_factory=list)
    searches: list[SearchSet] = field(default_factory=list)
    mappings: list[ValueMapping] = field(default_factory=list)
    extensions: list[str] = field(default_factory=list)
    tainted: bool = False
    array_ready: bool = False
    preds: tuple[Pred, ...] = ()


def layer_key(ordinal: int, name: str) -> str:
    return f"L{ordinal:02d}-{name}"


def _local(element: ET._Element) -> str:
    return ET.QName(element).localname


def _child(element: ET._Element, name: str) -> ET._Element | None:
    for node in element:
        if _local(node) == name:
            return node
    return None


def _text(element: ET._Element | None, name: str) -> str | None:
    if element is None:
        return None
    node = _child(element, name)
    if node is None or node.text is None or not node.text.strip():
        return None
    return node.text.strip()


def _module_extended(element: ET._Element) -> bool:
    for node in element.iter():
        if _local(node) != "PropertyState":
            continue
        if _text(node, "Property") == "Module" and _text(node, "State") == "Extended":
            return True
    return False


def _origin(
    layer: LayerDescriptor,
    metadata_kind: str,
    metadata_name: str,
    source: SourceFile,
    span: SourceSpan,
    procedure: str | None,
    hook_id: str | None = None,
    chain: tuple[SourceSpan, ...] = (),
) -> Origin:
    return Origin(
        layer.id,
        metadata_kind,
        metadata_name,
        source.file_id,
        span,
        procedure,
        chain,
        hook_id,
        source.path,
    )


def _blank_origin(layer: LayerDescriptor, root: str) -> Origin:
    span = SourceSpan("configuration", 1, 1, 0, 0)
    return Origin(
        layer.id, "Configuration", layer.name, "configuration", span, None, (), None, root
    )


def load_source(path: Path, file_id: str) -> SourceFile:
    """Читает UTF-8 файл. BOM сохраняется признаком и не попадает в текст."""
    try:
        raw = path.read_bytes()
    except OSError as error:
        raise EdReadError(f"Файл недоступен: {path}") from error
    if len(raw) > MAX_FILE_BYTES:
        raise EdResourceLimitError("Размер BSL превышает 16 MiB")
    bom = raw.startswith(b"\xef\xbb\xbf")
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeError as error:
        raise EdReadError(f"Ожидался UTF-8: {path}") from error
    return source_from_text(text, file_id, str(path), bom, raw)


def source_from_text(text: str, file_id: str, path: str, bom: bool, raw: bytes) -> SourceFile:
    # CR имеет ту же ширину, что LF: позиции исходного файла сохраняются.
    cr_only = "\r" in text and "\n" not in text
    if cr_only:
        text = text.replace("\r", "\n")
    lines = text.splitlines(keepends=True)
    offsets: list[int] = []
    position = 0
    for line in lines:
        offsets.append(position)
        position += len(line)
    if not offsets:
        offsets = [0]
    newline = (
        "\r"
        if cr_only
        else "mixed"
        if "\r\n" in text and "\n" in text.replace("\r\n", "")
        else ("\r\n" if "\r\n" in text else "\n")
    )
    return SourceFile(
        file_id,
        path,
        text,
        hashlib.sha256(raw).hexdigest(),
        tuple(offsets),
        bom=bom,
        newline=newline,
    )


def read_dump(root: Path) -> DumpInfo:
    """Читает Configuration.xml и описания объектов. Чужие каталоги не обходит."""
    root = Path(root)
    config_path = root / "Configuration.xml"
    layer = LayerDescriptor("dump", 0, root.name, str(root), None, "")
    if not config_path.is_file():
        skip = LayerSkip(
            "missing_configuration",
            _blank_origin(layer, str(root)),
            ("manager",),
            str(config_path),
            ("manager",),
        )
        return DumpInfo(str(root), None, None, (), (skip,), "", True)
    try:
        raw = config_path.read_bytes()
        document = ET.fromstring(raw, _PARSER)
    except (ET.XMLSyntaxError, OSError, UnicodeError):
        skip = LayerSkip(
            "metadata_xml",
            _blank_origin(layer, str(config_path)),
            ("manager",),
            str(config_path),
            ("manager",),
        )
        return DumpInfo(
            str(root), None, None, (), (skip,), hashlib.sha256(b"bad").hexdigest(), True
        )
    config = document if _local(document) == "Configuration" else _child(document, "Configuration")
    if config is None:
        skip = LayerSkip(
            "metadata_xml",
            _blank_origin(layer, str(config_path)),
            ("manager",),
            "нет Configuration",
            ("manager",),
        )
        return DumpInfo(str(root), None, None, (), (skip,), hashlib.sha256(raw).hexdigest(), True)
    name = _text(_child(config, "Properties"), "Name")
    uuid = config.get("uuid")
    children = _child(config, "ChildObjects")
    objects: list[DumpObject] = []
    skips: list[LayerSkip] = []
    digest = hashlib.sha256()
    digest.update(raw)
    if children is not None:
        for node in children:
            kind = _local(node)
            if kind not in ("CommonModule", "ExchangePlan"):
                continue
            object_name = (node.text or "").strip()
            if not object_name:
                continue
            folder = "CommonModules" if kind == "CommonModule" else "ExchangePlans"
            xml_path = root / folder / f"{object_name}.xml"
            module_rel = (
                Path(folder) / object_name / "Ext" / "Module.bsl"
                if kind == "CommonModule"
                else Path(folder) / object_name / "Ext" / "ManagerModule.bsl"
            )
            module_path = root / module_rel
            parsed = _read_object_xml(xml_path, kind, object_name, layer, skips)
            if parsed is None:
                objects.append(
                    DumpObject(
                        kind,
                        object_name,
                        None,
                        None,
                        None,
                        False,
                        None,
                        str(module_path) if module_path.is_file() else None,
                        None,
                    )
                )
                continue
            digest.update(parsed[0])
            belonging, extended, server, object_uuid, module_flag = parsed[1]
            objects.append(
                DumpObject(
                    kind,
                    object_name,
                    object_uuid,
                    belonging,
                    extended,
                    module_flag,
                    str(xml_path),
                    str(module_path) if module_path.is_file() else None,
                    server,
                )
            )
    return DumpInfo(str(root), name, uuid, tuple(objects), tuple(skips), digest.hexdigest(), False)


def _read_object_xml(
    path: Path,
    kind: str,
    name: str,
    layer: LayerDescriptor,
    skips: list[LayerSkip],
) -> tuple[bytes, tuple[str | None, str | None, bool | None, str | None, bool]] | None:
    if not path.is_file():
        skips.append(
            LayerSkip(
                "missing_metadata",
                _blank_origin(layer, str(path)),
                (f"{kind}/{name}",),
                str(path),
                (kind,),
            )
        )
        return None
    try:
        raw = path.read_bytes()
        document = ET.fromstring(raw, _PARSER)
    except (ET.XMLSyntaxError, OSError, UnicodeError):
        skips.append(
            LayerSkip(
                "metadata_xml",
                _blank_origin(layer, str(path)),
                (f"{kind}/{name}",),
                str(path),
                (kind,),
            )
        )
        return None
    obj = document if _local(document) == kind else _child(document, kind)
    if obj is None:
        skips.append(
            LayerSkip(
                "metadata_xml",
                _blank_origin(layer, str(path)),
                (f"{kind}/{name}",),
                "нет объекта",
                (kind,),
            )
        )
        return None
    props = _child(obj, "Properties")
    server_text = _text(props, "Server")
    server = None if server_text is None else server_text.casefold() == "true"
    return raw, (
        _text(props, "ObjectBelonging"),
        _text(props, "ExtendedConfigurationObject"),
        server,
        obj.get("uuid"),
        _module_extended(obj),
    )


def _annotation_text(value: str) -> str:
    """Отрезает комментарий директивы. Лексер отдаёт строку целиком, сам лексер не меняется."""
    comment = value.find("//")
    return value if comment < 0 else value[:comment].rstrip()


def parse_annotation(value: str) -> tuple[str, str] | None:
    """Разбирает одну директиву. Без аргумента это не перехват."""
    value = _annotation_text(value)
    if not value.startswith("&"):
        return None
    index = 1
    while index < len(value) and (value[index].isalnum() or value[index] == "_"):
        index += 1
    name = value[1:index].casefold()
    kind = _HOOK_KINDS.get(name)
    if kind is None:
        return None
    rest = value[index:].strip()
    if not rest.startswith("("):
        return None
    inner = tokenize(rest)
    if (
        len(inner) == 3
        and inner[0].value == "("
        and inner[1].kind == "string"
        and inner[2].value == ")"
    ):
        return kind, inner[1].value
    return None


def unrecognized_annotation(value: str) -> bool:
    """Имя перехвата есть, но форма не ``&Имя("литерал")``."""
    text = _annotation_text(value).strip()
    if not text.startswith("&") or parse_annotation(text) is not None:
        return False
    body = text[1:]
    if body[:1].isspace():
        body = body.lstrip()
        head = _directive_name(body)
        return head in _HOOK_KINDS
    head = _directive_name(body)
    if head not in _HOOK_KINDS:
        return False
    rest = body[len(head) :].strip()
    return rest.startswith("(")


def _directive_name(body: str) -> str:
    index = 0
    while index < len(body) and (body[index].isalnum() or body[index] == "_"):
        index += 1
    return body[:index].casefold()


def _head(statement: Statement) -> str:
    token = statement.tokens[0]
    if token.kind == "directive":
        return token.value.split()[0].casefold()
    return statement.head


def _bare(tokens: tuple[Token, ...]) -> tuple[Token, ...]:
    return tokens[:-1] if tokens and tokens[-1].value == ";" else tokens


def _join(tokens: tuple[Token, ...]) -> str:
    return "".join(token.value for token in tokens)


def _assignment(statement: Statement) -> tuple[str, tuple[Token, ...]] | None:
    tokens = _bare(statement.tokens)
    for index, token in enumerate(tokens):
        if token.value == "=" and token.kind == "symbol":
            if index % 2 == 1 and all(
                item.kind == "identifier" if position % 2 == 0 else item.value == "."
                for position, item in enumerate(tokens[:index])
            ):
                return _join(tokens[:index]), tokens[index + 1 :]
            break
    return None


def _call(tokens: tuple[Token, ...]) -> tuple[str, tuple[tuple[Token, ...], ...]] | None:
    tokens = _bare(tokens)
    if not tokens or tokens[-1].value != ")":
        return None
    for index, token in enumerate(tokens):
        if token.value == "(" and token.kind == "symbol":
            if not tokens[:index] or not all(
                item.kind == "identifier" or item.value == "." for item in tokens[:index]
            ):
                return None
            depth = 0
            for cursor in range(index, len(tokens)):
                current = tokens[cursor]
                if current.kind == "symbol":
                    depth += (current.value == "(") - (current.value == ")")
                    if depth == 0 and cursor != len(tokens) - 1:
                        return None
            return _join(tokens[:index]), split_arguments(tokens[index + 1 : -1])
    return None


def _expr(source: SourceFile, tokens: tuple[Token, ...], fallback: SourceSpan) -> Expr:
    if not tokens:
        return Expr("", source.span(fallback.char_start, fallback.char_start))
    span = source.span(tokens[0].start, tokens[-1].end)
    raw = source.text[span.char_start : span.char_end]
    kind: str | None = None
    value: str | int | float | bool | None = None
    if len(tokens) == 1:
        token = tokens[0]
        if token.kind == "string":
            kind, value = "string", token.value
        elif token.kind == "number":
            try:
                value = float(token.value) if "." in token.value else int(token.value)
                kind = "number"
            except ValueError:
                pass
        elif token.folded in ("истина", "ложь"):
            kind, value = "boolean", token.folded == "истина"
        elif token.folded in ("неопределено", "null"):
            kind = "undefined"
    parts: tuple[str, ...] = ()
    if all(
        token.kind == ("identifier" if i % 2 == 0 else "symbol")
        and (i % 2 == 0 or token.value == ".")
        for i, token in enumerate(tokens)
    ):
        parts = tuple(token.value for token in tokens[::2])
    return Expr(raw, span, kind, value, parts)


def _split_kw(tokens: tuple[Token, ...], keyword: str) -> list[tuple[Token, ...]] | None:
    parts: list[tuple[Token, ...]] = []
    start = 0
    depth = 0
    found = False
    for index, token in enumerate(tokens):
        if token.kind == "symbol" and token.value in ("(", "["):
            depth += 1
        elif token.kind == "symbol" and token.value in (")", "]"):
            depth -= 1
        elif depth == 0 and token.kind == "identifier" and token.folded == keyword:
            parts.append(tokens[start:index])
            start = index + 1
            found = True
    if not found:
        return None
    parts.append(tokens[start:])
    return parts


def build_tree(statements: tuple[Statement, ...] | list[Statement]) -> list[_Node]:
    pos = 0
    rows = list(statements)

    def parse_until(stop: frozenset[str]) -> list[_Node]:
        nonlocal pos
        nodes: Sequence[_Node] = []
        while pos < len(rows):
            head = _head(rows[pos])
            if head in stop:
                break
            if head in ("если", "#если"):
                nodes.append(parse_if())
            elif head in ("для", "пока"):
                nodes.append(parse_loop())
            elif head == "попытка":
                nodes.append(parse_try())
            else:
                nodes.append(_Stmt(rows[pos]))
                pos += 1
        return nodes

    def parse_if() -> _If:
        nonlocal pos
        header = rows[pos]
        pos += 1
        stops = frozenset({"иначеесли", "иначе", "конецесли", "#иначеесли", "#иначе", "#конецесли"})
        branches = [_Branch(_condition_tokens(header), parse_until(stops), header)]
        while pos < len(rows) and _head(rows[pos]) in ("иначеесли", "#иначеесли"):
            elseif = rows[pos]
            pos += 1
            branches.append(_Branch(_condition_tokens(elseif), parse_until(stops), elseif))
        if pos < len(rows) and _head(rows[pos]) in ("иначе", "#иначе"):
            other = rows[pos]
            pos += 1
            branches.append(
                _Branch(None, parse_until(frozenset({"конецесли", "#конецесли"})), other)
            )
        if pos >= len(rows) or _head(rows[pos]) not in ("конецесли", "#конецесли"):
            raise EdFormatError("Незакрытое условие BSL")
        pos += 1
        return _If(branches, header)

    def parse_loop() -> _Loop:
        nonlocal pos
        header = rows[pos]
        pos += 1
        body = parse_until(frozenset({"конеццикла"}))
        if pos >= len(rows) or _head(rows[pos]) != "конеццикла":
            raise EdFormatError("Незакрытый цикл BSL")
        pos += 1
        return _Loop(body, header)

    def parse_try() -> _Try:
        nonlocal pos
        header = rows[pos]
        pos += 1
        body = parse_until(frozenset({"исключение", "конецпопытки"}))
        handler: list[_Node] = []
        if pos < len(rows) and _head(rows[pos]) == "исключение":
            pos += 1
            handler = parse_until(frozenset({"конецпопытки"}))
        if pos >= len(rows) or _head(rows[pos]) != "конецпопытки":
            raise EdFormatError("Незакрытая попытка BSL")
        pos += 1
        return _Try(body, handler, header)

    tree = parse_until(frozenset())
    if pos != len(rows):
        raise EdFormatError("Лишний структурный оператор BSL")
    return tree


def _routine_trees(
    source: SourceFile,
) -> tuple[list[tuple[Routine, Statement, list[_Node]]], tuple[tuple[str, SourceSpan], ...]]:
    lexical = lex(source)
    routines: list[tuple[Routine, Statement, list[_Node]]] = []
    header: Statement | None = None
    body: list[Statement] = []
    for statement in lexical.statements:
        head = _head(statement)
        if head in ("процедура", "функция"):
            if header is not None:
                raise EdFormatError("Вложенное объявление метода")
            header, body = statement, []
        elif head in ("конецпроцедуры", "конецфункции"):
            if header is None:
                raise EdFormatError("Несогласованный конец метода")
            routine = _make_routine(source, header, statement)
            routines.append((routine, header, build_tree(body)))
            header = None
        elif header is not None:
            body.append(statement)
    if header is not None:
        raise EdFormatError("Незакрытый метод")
    return routines, lexical.warnings


def _make_routine(source: SourceFile, header: Statement, end: Statement) -> Routine:
    tokens = header.tokens
    if len(tokens) < 4 or tokens[1].kind != "identifier" or tokens[2].value != "(":
        raise EdFormatError("Неверный заголовок метода")
    exported = tokens[-1].folded == "экспорт"
    close = len(tokens) - (2 if exported else 1)
    if tokens[close].value != ")":
        raise EdFormatError("Незакрытые параметры метода")
    params: list[FormalParameter] = []
    for part in split_arguments(tokens[3:close]):
        expression = _expr(source, part, header.span)
        by_value = bool(part and part[0].folded == "знач")
        start = 1 if by_value else 0
        name = part[start].value if len(part) > start and part[start].kind == "identifier" else None
        default = None
        if len(part) > start + 1 and part[start + 1].value == "=":
            default = _expr(source, part[start + 2 :], header.span)
        params.append(FormalParameter(name, by_value, default, expression.raw, expression.span))
    span = source.span(header.span.char_start, end.span.char_end)
    return Routine(
        entity_id=f"{source.file_id}:routine:{header.span.char_start}",
        kind="routine",
        name=tokens[1].value,
        span=span,
        raw_text=source.text[span.char_start : span.char_end],
        routine_kind="function" if header.head == "функция" else "procedure",
        parameters_raw=source.text[tokens[2].end : tokens[close].start],
        parameters=tuple(params),
        exported=exported,
        roles=frozenset(),
        body_span=source.span(header.span.char_end, end.span.char_start),
    )


def _signature_ok(
    routine: Routine,
    variants: tuple[tuple[str, tuple[str, ...]], ...],
    by_value: tuple[bool, ...] | None = None,
) -> bool:
    """Число параметров и позиции ``Знач``. Имена локальных параметров не сравниваются."""
    for kind, names in variants:
        if routine.routine_kind != kind or len(routine.parameters) != len(names):
            continue
        if (
            by_value is not None
            and tuple(param.by_value for param in routine.parameters) != by_value
        ):
            continue
        return True
    return False


def _condition_tokens(header: Statement) -> tuple[Token, ...]:
    """Условие ветви. Тело директивы ``#Если`` токенизируется заново."""
    if header.tokens and header.tokens[0].kind == "directive":
        words = header.tokens[0].value.split(None, 1)
        body = words[1] if len(words) > 1 else ""
        folded = body.casefold()
        if folded.endswith("тогда"):
            body = body[: -len("тогда")].rstrip()
        return tokenize(body) if body else ()
    if header.tokens and header.tokens[-1].folded == "тогда":
        return header.tokens[1:-1]
    return header.tokens[1:]


_MAX_CLAUSES = 32
_Lit = tuple[bool, str, str]
_FLIP = {
    "headers": "not_headers",
    "not_headers": "headers",
    "rule_found": "rule_missing",
    "rule_missing": "rule_found",
    "property_absent": "property_present",
    "property_present": "property_absent",
}


def _true() -> Pred:
    return Pred("and")


def _false() -> Pred:
    return Pred("or")


def _is_opaque(pred: Pred) -> bool:
    return pred.op == "opaque"


def _is_true(pred: Pred) -> bool:
    return pred.op == "and" and not pred.kids


def _is_false(pred: Pred) -> bool:
    return pred.op == "or" and not pred.kids


def _canon_lit(neg: bool, kind: str, arg: str) -> _Lit | None:
    """Литерал над закрытым набором атомов. ``None`` — атом вне набора."""
    if kind in ("opaque_path", "collection_nonempty"):
        return neg, kind, arg
    if kind == "direction_is":
        if arg not in ("send", "receive"):
            return None
        if neg:
            arg = "receive" if arg == "send" else "send"
        return False, kind, arg
    if kind in _FLIP:
        if neg:
            kind = _FLIP[kind]
        return False, kind, arg
    if kind == "dispatch_is":
        return neg, kind, arg
    return None


def _contradicts(clause: frozenset[_Lit]) -> bool:
    seen: dict[tuple[str, str], bool] = {}
    directions: set[str] = set()
    for neg, kind, arg in clause:
        if kind == "direction_is" and not neg:
            directions.add(arg)
        key = (kind, arg)
        if key in seen and seen[key] != neg:
            return True
        seen[key] = neg
    return len(directions) > 1


def _simplify(clauses: list[frozenset[_Lit]]) -> list[frozenset[_Lit]] | None:
    kept: list[frozenset[_Lit]] = []
    for clause in clauses:
        if _contradicts(clause):
            continue
        if any(clause > other for other in kept):
            continue
        kept = [other for other in kept if not other > clause]
        if clause not in kept:
            kept.append(clause)
    if len(kept) > _MAX_CLAUSES:
        return None
    # Общая часть двух взаимодополняющих выходов остаётся доказанной:
    # (A И X) ИЛИ (A И НЕ X) = A, даже когда X непрозрачен.
    changed = True
    while changed:
        changed = False
        for index, left in enumerate(kept):
            for right in kept[index + 1 :]:
                one, two = left - right, right - left
                if len(one) != 1 or len(two) != 1:
                    continue
                neg, kind, arg = next(iter(one))
                if _canon_lit(not neg, kind, arg) != next(iter(two)):
                    continue
                common = left & right
                kept = [clause for clause in kept if not clause >= common]
                kept.append(common)
                changed = True
                break
            if changed:
                break
    return kept


def _clauses_of(pred: Pred) -> list[frozenset[_Lit]] | None:
    if _is_opaque(pred):
        return None
    if _is_true(pred):
        return [frozenset()]
    if pred.op == "or" and not pred.kids:
        return []
    if pred.op == "atom":
        lit = _canon_lit(False, pred.kind, pred.arg)
        if lit is None:
            return None
        return [frozenset({lit})]
    if pred.op == "not" and len(pred.kids) == 1 and pred.kids[0].op == "atom":
        lit = _canon_lit(True, pred.kids[0].kind, pred.kids[0].arg)
        if lit is None:
            return None
        return [frozenset({lit})]
    if pred.op == "not" and pred.kids:
        return _negate(_clauses_of(pred.kids[0]))
    if pred.op == "and":
        acc: list[frozenset[_Lit]] = [frozenset()]
        for kid in pred.kids:
            part = _clauses_of(kid)
            if part is None:
                return None
            merged = _conj(acc, part)
            if merged is None:
                return None
            acc = merged
        return acc
    if pred.op == "or":
        acc = []
        for kid in pred.kids:
            part = _clauses_of(kid)
            if part is None:
                return None
            acc.extend(part)
        return _simplify(acc)
    return None


def _conj(
    left: list[frozenset[_Lit]], right: list[frozenset[_Lit]]
) -> list[frozenset[_Lit]] | None:
    if not left or not right:
        return []
    if len(left) * len(right) > _MAX_CLAUSES:
        return None
    merged = [one | two for one in left for two in right]
    return _simplify(merged)


def _negate(clauses: list[frozenset[_Lit]] | None) -> list[frozenset[_Lit]] | None:
    """Отрицание ДНФ по де Моргану: не дизъюнкции — конъюнкция отрицаний клауз."""
    if clauses is None:
        return None
    if not clauses:
        return [frozenset()]
    acc: list[frozenset[_Lit]] = [frozenset()]
    for clause in clauses:
        if not clause:
            return []
        flipped: list[frozenset[_Lit]] = []
        for neg, kind, arg in clause:
            lit = _canon_lit(not neg, kind, arg)
            if lit is None:
                return None
            flipped.append(frozenset({lit}))
        merged = _conj(acc, flipped)
        if merged is None:
            return None
        acc = merged
    return acc


def _from_clauses(clauses: list[frozenset[_Lit]] | None) -> Pred:
    if clauses is None:
        return Pred.opaque()
    simplified = _simplify(clauses)
    if simplified is None:
        return Pred.opaque()
    if not simplified:
        return _false()
    parts: list[Pred] = []
    for clause in simplified:
        atoms: list[Pred] = []
        for neg, kind, arg in sorted(clause):
            atom = Pred.atom(kind, arg)
            atoms.append(Pred("not", kids=(atom,)) if neg else atom)
        parts.append(Pred("and", kids=tuple(atoms)))
    if len(parts) == 1:
        return parts[0]
    return Pred("or", kids=tuple(parts))


def _dnf(pred: Pred) -> Pred:
    if _is_opaque(pred):
        return pred
    result = _from_clauses(_clauses_of(pred))
    return Pred("opaque", "condition_limit") if _is_opaque(result) else result


def _formula_and(left: Pred, right: Pred) -> Pred:
    if _is_opaque(left) or _is_opaque(right):
        return Pred.opaque()
    if _is_false(left) or _is_false(right):
        return _false()
    if _is_true(left):
        return _dnf(right)
    if _is_true(right):
        return _dnf(left)
    one, two = _clauses_of(left), _clauses_of(right)
    if one is None or two is None:
        return Pred.opaque()
    return _from_clauses(_conj(one, two))


def _formula_or(left: Pred, right: Pred) -> Pred:
    if _is_true(left) or _is_true(right):
        return _true()
    if _is_false(left):
        return _dnf(right)
    if _is_false(right):
        return _dnf(left)
    if _is_opaque(left) or _is_opaque(right):
        return Pred.opaque()
    one, two = _clauses_of(left), _clauses_of(right)
    if one is None or two is None:
        return Pred.opaque()
    return _from_clauses([*one, *two])


def _formula_not(pred: Pred) -> Pred:
    if _is_opaque(pred):
        return pred
    result = _from_clauses(_negate(_clauses_of(pred)))
    return Pred("opaque", "condition_limit") if _is_opaque(result) else result


def _and(preds: tuple[Pred, ...]) -> Pred:
    result = _true()
    for pred in preds:
        result = _formula_and(result, pred)
        if _is_opaque(result):
            return result
    return result


class _Walker:
    def __init__(
        self,
        source: SourceFile,
        layer: LayerDescriptor,
        metadata_kind: str,
        metadata_name: str,
        routines: dict[str, tuple[Routine, list[_Node]]],
        version: int | None,
        helpers: dict[str, bool],
        plan_name: str | None,
    ) -> None:
        self.source = source
        self.layer = layer
        self.metadata_kind = metadata_kind
        self.metadata_name = metadata_name
        self.routines = routines
        self.version = version
        self.helpers = helpers
        self.plan_name = plan_name
        self.operations: list[LayerOperation] = []
        self.skips: list[LayerSkip] = []
        self.marks: list[tuple[SourceSpan, Classification]] = []
        self.calls = 0
        self.edges = 0
        self.stack: list[str] = []
        self.tainted: set[str] = set()
        self.flow_unknown = False
        self.builders: dict[str, _Builder] = {}
        self.counter = 0
        self.condition_serial = 0
        self.handled: dict[str, tuple[str, ...]] = {}
        self.opaque_depth = 0
        self.cond_depth = 0
        self.collection = ""
        self.statement_preds: tuple[Pred, ...] = ()
        self.touched: tuple[Footprint, ...] = ()
        self.module_variables, self.local_variables = _variable_declarations(source)
        for routine, _tree in routines.values():
            self.local_variables.setdefault(routine.name.casefold(), set()).update(
                (param.name or "").casefold() for param in routine.parameters
            )
        self.parameter_collections: dict[str, str] = {}
        self.catches: list[list[tuple[Pred, dict[str, _Bind]]]] = []
        self.uncaught: list[Pred] = []

    def origin(
        self,
        span: SourceSpan,
        procedure: str | None,
        hook_id: str | None = None,
        chain: tuple[SourceSpan, ...] = (),
    ) -> Origin:
        return _origin(
            self.layer,
            self.metadata_kind,
            self.metadata_name,
            self.source,
            span,
            procedure,
            hook_id,
            chain,
        )

    def skip(
        self,
        reason: str,
        span: SourceSpan,
        procedure: str | None,
        affected: tuple[str, ...],
        raw: str,
        scope: tuple[str, ...],
        hook_id: str | None = None,
    ) -> None:
        self.skips.append(
            LayerSkip(
                reason,
                self.origin(span, procedure, hook_id),
                affected,
                raw,
                scope,
                preds=self.statement_preds,
                operation_offset=len(self.operations),
            )
        )

    def op(
        self,
        kind: str,
        target: str,
        path: tuple[str, ...],
        value: Expr | Entity | None,
        span: SourceSpan,
        procedure: str,
        preds: tuple[Pred, ...],
        hook_id: str | None,
        resolution: str = "applied",
        footprint: Footprint | None = None,
        chain: tuple[SourceSpan, ...] = (),
    ) -> None:
        if len(self.operations) >= MAX_OPS:
            raise _Abort("resource_limit", self.origin(span, procedure, hook_id))
        if self.opaque_depth and resolution == "applied":
            resolution = "unknown"
            footprint = footprint or Footprint(
                "entity" if "\x1f" in target else "collection", target or self.collection
            )
        self.counter += 1
        guards = _guards(self.source, span, preds)
        self.operations.append(
            LayerOperation(
                f"{self.layer.id}:op:{self.counter}",
                kind,
                target,
                path,
                value,
                self.origin(span, procedure, hook_id, chain),
                guards,
                footprint,
                resolution,
                preds,
                hook_id,
            )
        )

    def walk_hook(
        self,
        hook_id: str,
        routine: Routine,
        tree: list[_Node],
        kind: str,
        target: str,
    ) -> str:
        self.flow_unknown = False
        self.tainted = set()
        self.statement_preds = ()
        self.uncaught = []
        env = _parameter_env(target, routine, self.plan_name)
        self.parameter_collections = _collection_parameters(routine, env)
        collection = _filler_collection(target)
        self.collection = collection
        if target.casefold() in _DISPATCH_NAMES:
            return self._walk_dispatch(hook_id, routine, tree, kind, env)
        if kind == HookKind.BEFORE and collection:
            self._area_skip(
                "before_filler",
                routine.span,
                routine.name,
                collection,
                routine.raw_text,
                hook_id,
                "collection",
            )
            return Continuation.ONCE
        if kind == HookKind.CHANGE_CONTROL:
            scope = collection or "manager"
            self._area_skip(
                "change_control",
                routine.span,
                routine.name,
                scope,
                routine.raw_text,
                hook_id,
                "collection" if collection else "manager",
            )
            return Continuation.UNKNOWN
        if kind == HookKind.AROUND and collection:
            continued = _leading_continue(tree, routine)
            if continued:
                self.walk(tree[1:], env, _true(), hook_id, routine.name, ())
                return Continuation.ONCE
            self._area_skip(
                "arbitrary_replacement",
                routine.span,
                routine.name,
                collection,
                routine.raw_text,
                hook_id,
                "collection",
            )
            return Continuation.NONE
        replaced = self._replaced_callee(kind, target, tree, routine, hook_id)
        if replaced:
            return replaced
        self.walk(tree, env, _true(), hook_id, routine.name, ())
        self._flush_all(hook_id, routine.name, ())
        return Continuation.ONCE

    def _area_skip(
        self,
        reason: str,
        span: SourceSpan,
        procedure: str | None,
        ref: str,
        raw: str,
        hook_id: str | None,
        scope: str,
    ) -> None:
        self.skip(reason, span, procedure, (ref,), raw, (ref,), hook_id)
        self.op(
            OperationKind.UNKNOWN,
            ref,
            (),
            None,
            span,
            procedure or "",
            self.statement_preds,
            hook_id,
            "unknown",
            Footprint(scope, ref),
        )

    def _replaced_callee(
        self,
        kind: str,
        target: str,
        tree: list[_Node],
        routine: Routine,
        hook_id: str,
    ) -> str:
        """``&Вместо`` без ``ПродолжитьВызов`` подменяет helper или процедуру правила."""
        if kind != HookKind.AROUND or _leading_continue(tree, routine):
            return ""
        folded = target.casefold()
        if folded in ("добавитьпкс", "добавитьпктч"):
            self._area_skip(
                "helper_replaced",
                routine.span,
                routine.name,
                "pko",
                routine.raw_text,
                hook_id,
                "collection",
            )
            return Continuation.NONE
        if folded.startswith("добавитьпко_") or folded.startswith("добавитьпод_"):
            self._area_skip(
                "rule_procedure_replaced",
                routine.span,
                routine.name,
                target,
                routine.raw_text,
                hook_id,
                "procedure",
            )
            return Continuation.NONE
        return ""

    def _walk_dispatch(
        self,
        hook_id: str,
        routine: Routine,
        tree: list[_Node],
        kind: str,
        env: dict[str, _Bind],
    ) -> str:
        literals: list[str] = []
        continuation = Continuation.NONE

        def visit(nodes: Sequence[_Node]) -> None:
            nonlocal continuation
            for node in nodes:
                if isinstance(node, _If):
                    for branch in node.branches:
                        if branch.condition is None:
                            if _single_continue(branch.body, routine):
                                continuation = Continuation.ONCE
                            elif branch.body and not _direct_call(branch.body):
                                continuation = Continuation.UNKNOWN
                            visit(branch.body)
                            continue
                        cond = self._condition(branch.condition, env)
                        name = _dispatch_literal(cond)
                        if name is not None and _direct_call(branch.body):
                            literals.append(name)
                            call_statement = next(
                                item.statement
                                for item in branch.body
                                if isinstance(item, _Stmt) and item.statement.head != "возврат"
                            )
                            case = _case(self.source, routine, name, call_statement)
                            if case is None:
                                self.skip(
                                    "opaque_dispatch",
                                    call_statement.span,
                                    routine.name,
                                    (name,),
                                    call_statement.raw_text,
                                    ("dispatcher",),
                                    hook_id,
                                )
                                continuation = Continuation.UNKNOWN
                            else:
                                self.op(
                                    OperationKind.DISPATCH,
                                    name,
                                    (),
                                    case,
                                    call_statement.span,
                                    routine.name,
                                    (),
                                    hook_id,
                                )
                        else:
                            visit(branch.body)
                    continue
                if not isinstance(node, _Stmt):
                    continue
                if _raises(node.statement):
                    self.marks.append((node.statement.span, Classification.DECLARATIVE))
                    continuation = Continuation.UNKNOWN

        visit(tree)
        self.handled[hook_id] = tuple(literals)
        if kind == HookKind.AFTER:
            return Continuation.ONCE
        if kind == HookKind.BEFORE:
            return Continuation.ONCE
        return continuation

    def walk(
        self,
        nodes: Sequence[_Node],
        env: dict[str, _Bind],
        entry: Pred,
        hook_id: str | None,
        procedure: str,
        chain: tuple[SourceSpan, ...],
    ) -> Pred:
        """Условие путей, на которых последовательность делает ``Возврат``.

        Оператор под неизвестным условием становится операцией ``unknown``, а не
        пропадает и не выполняется безусловно.
        """
        exited = _false()
        current = entry
        pending = list(nodes)
        while pending:
            if _is_false(current):
                break
            choice = next(((name, bind) for name, bind in env.items() if bind.paths), None)
            if choice:
                name, binding = choice
                result = exited
                outputs = []
                for path, value in binding.paths:
                    child = dict(env)
                    child[name] = value
                    branch_entry = _formula_and(current, path)
                    branch_exit = self.walk(pending, child, branch_entry, hook_id, procedure, chain)
                    result = _formula_or(result, branch_exit)
                    outputs.append((_formula_and(branch_entry, _formula_not(branch_exit)), child))
                _join_bindings(env, outputs)
                return result
            if self.flow_unknown:
                self._emit_unknown(pending, env, hook_id, procedure, chain, current)
                return Pred.opaque()
            if _is_false(current):
                break
            node = pending.pop(0)
            self.statement_preds = _as_preds(current)
            if not isinstance(node, _If):
                current, failure = self._guard_dereferences(
                    node.statement, env, current, hook_id, procedure
                )
                exited = _formula_or(exited, failure)
                self.statement_preds = _as_preds(current)
            if _is_opaque(current):
                self._emit_unknown([node, *pending], env, hook_id, procedure, chain)
                self.skip(
                    "opaque_flow",
                    node.statement.span,
                    procedure,
                    (self.collection or "manager",),
                    node.statement.raw_text,
                    (self.collection or "manager",),
                    hook_id,
                )
                return Pred.opaque()
            if isinstance(node, _If):
                branch_exit = self._walk_if(node, env, current, hook_id, procedure, chain)
                if _unknown_path(branch_exit):
                    refs = tuple(fp.ref for fp in self._footprints(pending, env))
                    if refs:
                        self.statement_preds = (Pred("uncertain", kids=(branch_exit,)),)
                        self.skip(
                            "opaque_return",
                            node.statement.span,
                            procedure,
                            refs,
                            node.statement.raw_text,
                            refs,
                            hook_id,
                        )
                if _is_opaque(branch_exit):
                    self._emit_unknown(pending, env, hook_id, procedure, chain, current)
                    return Pred.opaque()
                exited = _formula_or(exited, branch_exit)
                current = _formula_and(current, _formula_not(branch_exit))
            elif isinstance(node, _Loop):
                self._loop(node, env, procedure, hook_id)
                self._emit_unknown(node.body, dict(env), hook_id, procedure, chain, current)
                _invalidate_after_opaque([node], env)
                if _returns(node.body):
                    self._emit_unknown(pending, env, hook_id, procedure, chain, current)
                    return Pred.opaque()
            elif isinstance(node, _Try):
                branch_exit = self._walk_try(node, env, current, hook_id, procedure, chain)
                exited = _formula_or(exited, branch_exit)
                current = _formula_and(current, _formula_not(branch_exit))
            elif isinstance(node, _Stmt):
                head = node.statement.head
                if (head == "возврат" or _raises(node.statement)) and _dynamic_code(node.statement):
                    self._reject(
                        node.statement,
                        _environment_footprints(env, self.parameter_collections),
                        procedure,
                        hook_id,
                        "dynamic_code",
                    )
                if _raises(node.statement):
                    self.marks.append((node.statement.span, Classification.DECLARATIVE))
                    if self.catches:
                        self.catches[-1].append((current, dict(env)))
                        self.marks.append((node.statement.span, Classification.DECLARATIVE))
                        return _formula_or(exited, current)
                    self.op(
                        OperationKind.UNKNOWN,
                        "filler",
                        (),
                        None,
                        node.statement.span,
                        procedure,
                        _as_preds(current),
                        hook_id,
                        "filler_error",
                        Footprint("filler", "filler"),
                    )
                    self.uncaught.append(current)
                    return _formula_or(exited, current)
                if head == "возврат":
                    return _formula_or(exited, current)
                if head == "перейти" or node.statement.raw_text.lstrip().startswith("~"):
                    self._area_skip(
                        "goto",
                        node.statement.span,
                        procedure,
                        self.collection or "manager",
                        node.statement.raw_text,
                        hook_id,
                        "collection",
                    )
                    self._emit_unknown(pending, env, hook_id, procedure, chain, current)
                    return Pred.opaque()
                if self.catches and self._may_throw(node.statement, env):
                    failure = Pred.atom(
                        "opaque_path",
                        f"{self.source.file_id}:{node.statement.span.char_start}:exception",
                    )
                    self.catches[-1].append((_formula_and(current, failure), dict(env)))
                    current = _formula_and(current, _formula_not(failure))
                first_op = len(self.operations)
                first_throw = len(self.catches[-1]) if self.catches else len(self.uncaught)
                self._statement(node.statement, env, _as_preds(current), hook_id, procedure, chain)
                thrown = _false()
                paths = (
                    [path for path, _state in self.catches[-1][first_throw:]]
                    if self.catches
                    else self.uncaught[first_throw:]
                )
                for path in paths:
                    thrown = _formula_or(thrown, path)
                current = _formula_and(current, _formula_not(thrown))
                if not self.catches:
                    exited = _formula_or(exited, thrown)
                if self.catches:
                    for op_index in range(first_op, len(self.operations)):
                        operation = self.operations[op_index]
                        success = _operation_success(operation)
                        if success is None:
                            continue
                        self.catches[-1].append(
                            (_formula_and(current, _formula_not(success)), dict(env))
                        )
                        current = _formula_and(current, success)
                        self.operations[op_index] = replace(
                            operation, preds=_as_preds(_formula_and(_and(operation.preds), success))
                        )
        return exited

    def _guard_dereferences(self, statement, env, entry, hook_id, procedure):
        success = _true()
        for index, token in enumerate(statement.tokens[:-1]):
            if token.kind != "identifier" or statement.tokens[index + 1].value != ".":
                continue
            bind = env.get(token.folded)
            if bind and bind.tag == "rule":
                success = _formula_and(success, Pred.atom("rule_found", bind.rule_key))
            elif bind and bind.tag == "property":
                arg = _UNIT.join((bind.rule_key, bind.column, bind.literal or ""))
                if bind.snapshot:
                    arg += "\x1e" + bind.snapshot
                success = _formula_and(success, Pred.atom("property_present", arg))
        if _is_true(success):
            return entry, _false()
        failure = _formula_and(entry, _formula_not(success))
        if self.catches:
            self.catches[-1].append((failure, dict(env)))
        else:
            self.op(
                OperationKind.UNKNOWN,
                "filler",
                (),
                None,
                statement.span,
                procedure,
                _as_preds(failure),
                hook_id,
                "filler_error",
                Footprint("filler", "filler"),
            )
            self.uncaught.append(failure)
        return _formula_and(entry, success), failure

    def _may_throw(self, statement: Statement, env: dict[str, _Bind]) -> bool:
        assignment = _assignment(statement)
        call = _call(assignment[1] if assignment else statement.tokens)
        if not call or self._benign_columns(call, env):
            return False
        name = call[0].casefold()
        return (
            name not in self.routines
            and name
            not in (
                "добавитьпкс",
                "добавитьпктч",
                "сообщить",
                "записьжурналарегистрации",
                "значениезаполнено",
            )
            and not name.startswith("обменданнымиxdtoсервер.инициализироватьправило")
            and not name.endswith((".найти", ".удалить"))
        )

    def _walk_try(self, node, env, entry, hook_id, procedure, chain) -> Pred:
        """Возврат выходит из процедуры, исключение — в ближайший обработчик."""
        caught: list[tuple[Pred, dict[str, _Bind]]] = []
        child = dict(env)
        self.catches.append(caught)
        try:
            body_exit = self.walk(node.body, child, entry, hook_id, procedure, chain)
        finally:
            self.catches.pop()
        thrown = _false()
        for path, _state in caught:
            thrown = _formula_or(thrown, path)
        returned = _formula_and(body_exit, _formula_not(thrown))
        outputs = [(_formula_and(entry, _formula_not(_formula_or(body_exit, thrown))), child)]
        for path, state in caught:
            handler_exit = self.walk(node.handler, state, path, hook_id, procedure, chain)
            returned = _formula_or(returned, handler_exit)
            outputs.append((_formula_and(path, _formula_not(handler_exit)), state))
        _join_bindings(env, outputs)
        self.marks.append((node.statement.span, Classification.DECLARATIVE))
        return returned

    def _walk_if(
        self,
        node: _If,
        env: dict[str, _Bind],
        entry: Pred,
        hook_id: str | None,
        procedure: str,
        chain: tuple[SourceSpan, ...],
    ) -> Pred:
        self._materialize(env, hook_id, procedure)
        self.cond_depth += 1
        if self.cond_depth > MAX_CONDITIONS:
            raise _Abort("resource_limit", self.origin(node.statement.span, procedure, hook_id))
        self.marks.append((node.statement.span, Classification.DECLARATIVE))
        try:
            return self._walk_if_body(node, env, entry, hook_id, procedure, chain)
        finally:
            self.cond_depth -= 1

    def _walk_if_body(
        self,
        node: _If,
        env: dict[str, _Bind],
        entry: Pred,
        hook_id: str | None,
        procedure: str,
        chain: tuple[SourceSpan, ...],
    ) -> Pred:
        exited, negated = _false(), _true()
        joined: list[tuple[Pred, dict[str, _Bind]]] = []
        for branch in node.branches:
            preproc = _preproc_branch(branch)
            if preproc is False:
                continue
            if preproc is True:
                return self.walk(branch.body, env, entry, hook_id, procedure, chain)
            boundary = _formula_and(entry, negated)
            if _is_false(boundary):
                continue
            reason = None
            if branch.condition is None:
                guard = negated
            else:
                self.condition_serial += 1
                boundary, failure = self._guard_dereferences(
                    branch.statement, env, boundary, hook_id, procedure
                )
                exited = _formula_or(exited, failure)
                condition = self._prepare_condition(branch, env, boundary, hook_id, procedure)
                parsed, reason = _path_condition(
                    condition, env, branch.statement.span, str(self.condition_serial)
                )
                guard = _formula_and(parsed, negated)
                guard = _formula_and(guard, boundary)
                negated = _formula_and(negated, _formula_not(parsed))
                negated = _formula_and(negated, _formula_not(failure))
            path = _formula_and(entry, guard)
            if _is_opaque(path) or _is_opaque(negated):
                reason = "condition_limit"
                path = boundary
            child = dict(env)
            if reason:
                self.statement_preds = _as_preds(boundary)
                condition_refs = self._statement_footprints(branch.statement, env)
                if condition_refs and not self._column_check(branch.condition or (), env):
                    self._reject(branch.statement, condition_refs, procedure, hook_id, reason)
                _invalidate_context_call(branch.condition or (), child)
                self.marks.append((branch.statement.span, Classification.UNKNOWN))
                first_op = len(self.operations)
                body_exit = self.walk(branch.body, child, path, hook_id, procedure, chain)
                refs = tuple(
                    sorted(
                        {
                            (op.footprint.ref if op.footprint else op.target_ref)
                            for op in self.operations[first_op:]
                            if op.resolution not in ("observe", "filler_error") and op.target_ref
                        }
                    )
                )
                if refs:
                    self.statement_preds = (Pred("uncertain", kids=(path,)),)
                    self.skip(
                        reason,
                        branch.statement.span,
                        procedure,
                        refs,
                        branch.statement.raw_text,
                        refs,
                        hook_id,
                    )
            else:
                body_exit = self.walk(branch.body, child, path, hook_id, procedure, chain)
            self._materialize(child, hook_id, procedure)
            joined.append((_formula_and(path, _formula_not(body_exit)), child))
            exited = _formula_or(exited, body_exit)
        if not any(branch.condition is None for branch in node.branches):
            joined.append((_formula_and(entry, negated), dict(env)))
        _join_bindings(env, joined)
        return exited

    def _prepare_condition(
        self,
        branch: _Branch,
        env: dict[str, _Bind],
        entry: Pred,
        hook_id: str | None,
        procedure: str,
    ) -> tuple[Token, ...]:
        tokens: list[Token] = list(branch.condition or ())
        if _dynamic_code(branch.statement):
            self.statement_preds = _as_preds(entry)
            self._reject(
                branch.statement,
                _environment_footprints(env, self.parameter_collections),
                procedure,
                hook_id,
                "dynamic_code",
            )
        index = 0
        while index < len(tokens):
            if tokens[index].kind != "identifier":
                index += 1
                continue
            depth = 0
            for end in range(index, len(tokens)):
                if tokens[end].value == "(":
                    depth += 1
                elif tokens[end].value == ")":
                    depth -= 1
                    if depth == 0:
                        call = _call(tuple(tokens[index : end + 1]))
                        if (
                            call
                            and call[0].casefold().endswith(".свойства.найти")
                            and len(call[1]) == 2
                        ):
                            name = f"inline_find_{branch.statement.span.char_start}_{index}"
                            self._find(
                                branch.statement,
                                name,
                                call,
                                env,
                                _as_preds(entry),
                                procedure,
                                hook_id,
                            )
                            tokens[index : end + 1] = [
                                Token("identifier", name, tokens[index].start, tokens[end].end)
                            ]
                        break
            index += 1
        return tuple(tokens)

    def _emit_unknown(
        self,
        nodes: Sequence[_Node],
        env: dict[str, _Bind],
        hook_id: str | None,
        procedure: str,
        chain: tuple[SourceSpan, ...],
        entry: Pred | None = None,
    ) -> Pred:
        """Тот же обход пути; неизвестна семантика операций, а не известные границы пути."""
        self.opaque_depth += 1
        previous_flow = self.flow_unknown
        self.flow_unknown = False
        try:
            return self.walk(
                nodes, env, _true() if entry is None else entry, hook_id, procedure, chain
            )
        finally:
            self.flow_unknown = previous_flow
            self.opaque_depth -= 1

    def _loop(
        self, node: _Loop, env: dict[str, _Bind], procedure: str, hook_id: str | None
    ) -> None:
        if _dynamic_code(node.statement):
            self._reject(
                node.statement,
                _environment_footprints(env, self.parameter_collections),
                procedure,
                hook_id,
                "dynamic_code",
            )
        footprints = (
            *self._statement_footprints(node.statement, env),
            *self._footprints(node.body, env),
        )
        if not footprints and _returns(node.body):
            footprints = (Footprint("collection", self.collection or "manager"),)
        if not footprints:
            self.marks.append((node.statement.span, Classification.OPAQUE_CODE))
            return
        affected = tuple(sorted({fp.ref for fp in footprints}))
        for fp in footprints:
            self.op(
                OperationKind.UNKNOWN,
                "",
                (),
                None,
                node.statement.span,
                procedure or "",
                self.statement_preds,
                hook_id,
                "unknown",
                fp,
            )
        self.skip(
            "loop",
            node.statement.span,
            procedure,
            affected,
            node.statement.raw_text,
            affected,
            hook_id,
        )
        self.marks.append((node.statement.span, Classification.UNKNOWN))

    def _statement(
        self,
        statement: Statement,
        env: dict[str, _Bind],
        preds: tuple[Pred, ...],
        hook_id: str | None,
        procedure: str,
        chain: tuple[SourceSpan, ...],
    ) -> None:
        """Классификация замкнута: непонятый оператор понижает все свои ссылки."""
        touched = self._statement_footprints(statement, env)
        first_skip, first_op = len(self.skips), len(self.operations)
        self._classify_statement(statement, env, preds, hook_id, procedure, chain)
        uncertain = any(
            skip.origin.span == statement.span and skip.certainty == "unknown"
            for skip in self.skips[first_skip:]
        ) or any(
            op.origin.span == statement.span and op.resolution == "unknown"
            for op in self.operations[first_op:]
        )
        if uncertain:
            for fp in touched:
                if any(op.footprint == fp for op in self.operations[first_op:]):
                    continue
                self.op(
                    OperationKind.UNKNOWN,
                    fp.ref,
                    fp.field_path,
                    None,
                    statement.span,
                    procedure,
                    preds,
                    hook_id,
                    "unknown",
                    fp,
                )

    def _classify_statement(
        self,
        statement: Statement,
        env: dict[str, _Bind],
        preds: tuple[Pred, ...],
        hook_id: str | None,
        procedure: str,
        chain: tuple[SourceSpan, ...],
    ) -> None:
        self.statement_preds = preds
        touched = self._statement_footprints(statement, env)
        self.touched = touched
        assignment = _assignment(statement)
        if _dynamic_code(statement):
            scope = _environment_footprints(env, self.parameter_collections)
            self._reject(statement, scope, procedure, hook_id, "dynamic_code")
            return
        if assignment:
            left, _right = assignment
            collection = self.parameter_collections.get(left.casefold())
            if collection:
                self._reject(
                    statement,
                    (Footprint("collection", collection),),
                    procedure,
                    hook_id,
                    "collection_rebind",
                )
                env[left.casefold()] = _Bind("unknown", collection=collection)
                return
            if (
                left.casefold() in self.module_variables
                and left.casefold() not in self.local_variables.get(procedure.casefold(), set())
                and touched
            ):
                self._materialize(env, hook_id, procedure)
                self._reject(
                    statement,
                    self._statement_footprints(statement, env),
                    procedure,
                    hook_id,
                    "module_variable_escape",
                )
                return
        call = _call(assignment[1] if assignment else statement.tokens)
        if call and any(self._expression_touches_call(arg, env) for arg in call[1]):
            self._reject(statement, touched, procedure, hook_id, "unknown_call")
            return
        if self._benign_columns(call, env):
            self.marks.append((statement.span, Classification.DECLARATIVE))
            return
        if self._map_statement(statement, assignment, call, env, preds, hook_id, procedure):
            self.marks.append((statement.span, Classification.DECLARATIVE))
            return
        if self._builder_statement(
            statement, assignment, call, env, preds, hook_id, procedure, chain
        ):
            self.marks.append((statement.span, Classification.DECLARATIVE))
            return
        if (
            assignment
            and call
            and call[0].casefold().endswith(".найти")
            and self._assign_statement(statement, assignment, env, preds, hook_id, procedure)
        ):
            self.marks.append((statement.span, Classification.DECLARATIVE))
            return
        if call and self._call_statement(statement, call, env, preds, hook_id, procedure, chain):
            if assignment:
                left = assignment[0].casefold()
                env[left] = _Bind(
                    "unknown", collection=touched[0].ref.split(_UNIT)[0] if touched else ""
                )
                if touched:
                    self.skip(
                        "computed_receiver",
                        statement.span,
                        procedure,
                        tuple(fp.ref for fp in touched),
                        statement.raw_text,
                        tuple(fp.ref for fp in touched),
                        hook_id,
                    )
            return
        if assignment and self._assign_statement(
            statement, assignment, env, preds, hook_id, procedure
        ):
            self.marks.append((statement.span, Classification.DECLARATIVE))
            return
        if statement.head in ("если", "конецесли", "иначе", "иначеесли"):
            return
        self._reject(statement, touched, procedure, hook_id, "unknown_statement")

    @staticmethod
    def _benign_columns(call, env: dict[str, _Bind]) -> bool:
        """Добавление колонки не меняет строки правил ПОД.

        reference/kd3-cfg/DataProcessors/ВыгрузкаМодуля/Ext/ObjectModule.bsl:2690-2692.
        """
        if not call:
            return False
        parts = call[0].casefold().split(".")
        bind = env.get(parts[0])
        return bool(
            len(parts) == 3
            and parts[1:] == ["колонки", "добавить"]
            and bind
            and bind.tag == "collection"
            and len(call[1]) == 1
            and len(call[1][0]) == 1
            and call[1][0][0].kind == "string"
        )

    @staticmethod
    def _column_check(tokens: tuple[Token, ...], env: dict[str, _Bind]) -> bool:
        index = _op_index(tokens)
        if index is None or tokens[index].value != "=":
            return False
        tail = tokens[index + 1 :]
        call = _call(tokens[:index])
        if not call or len(tail) != 1 or tail[0].folded != "неопределено":
            return False
        parts = call[0].casefold().split(".")
        bind = env.get(parts[0])
        return bool(
            len(parts) == 3
            and parts[1:] == ["колонки", "найти"]
            and bind
            and bind.tag == "collection"
            and len(call[1]) == 1
            and len(call[1][0]) == 1
            and call[1][0][0].kind == "string"
        )

    def _expression_touches_call(self, tokens: tuple[Token, ...], env: dict[str, _Bind]) -> bool:
        """Вложенный вызов с отслеживаемой ссылкой не является литеральным аргументом."""
        return any(token.value == "(" for token in tokens) and any(
            token.kind == "identifier"
            and token.folded in env
            and (env[token.folded].collection or env[token.folded].rule_key)
            and not _scalar_read(tokens, index, env)
            for index, token in enumerate(tokens)
        )

    def _statement_footprints(
        self, statement: Statement, env: dict[str, _Bind]
    ) -> tuple[Footprint, ...]:
        """Запрет по умолчанию: ищем ссылки везде, включая владельца метода и выражения."""
        result: list[Footprint] = []
        assignment = _assignment(statement)
        lhs_end = (
            next((i for i, token in enumerate(statement.tokens) if token.value == "="), -1)
            if assignment
            else -1
        )
        for index, token in enumerate(statement.tokens):
            if token.kind != "identifier":
                continue
            bind = env.get(token.folded)
            if bind is None:
                continue
            if index > lhs_end and _scalar_read(statement.tokens, index, env):
                continue
            if bind.tag == "collection":
                fp = Footprint("collection", bind.collection)
            elif bind.tag in ("rule", "property", "properties", "builder") or bind.collection:
                ref = bind.rule_key or bind.builder or bind.collection
                properties = bind.tag in ("property", "properties") or (
                    index + 2 < len(statement.tokens)
                    and statement.tokens[index + 1].value == "."
                    and statement.tokens[index + 2].folded
                    in ("свойства", "свойстватабличныхчастей")
                )
                fp = Footprint(
                    "entity" if "\x1f" in ref else "collection",
                    ref,
                    ("properties",) if properties else (),
                )
            else:
                continue
            if fp not in result:
                result.append(fp)
        return tuple(result)

    def _footprints(self, nodes: Sequence[_Node], env: dict[str, _Bind]) -> tuple[Footprint, ...]:
        result: list[Footprint] = []
        for node in nodes:
            if isinstance(node, _Stmt):
                if self._benign_columns(_call(node.statement.tokens), env):
                    continue
                for fp in self._statement_footprints(node.statement, env):
                    if fp not in result:
                        result.append(fp)
            elif isinstance(node, _If):
                for branch in node.branches:
                    result.extend(self._footprints(branch.body, env))
            else:
                result.extend(self._footprints(node.body, env))
        return tuple(result)

    def _reject(
        self,
        statement: Statement,
        footprints: tuple[Footprint, ...],
        procedure: str,
        hook_id: str | None,
        reason: str,
    ) -> None:
        if not footprints:
            self.marks.append((statement.span, Classification.OPAQUE_CODE))
            return
        for fp in footprints:
            self.op(
                OperationKind.UNKNOWN,
                fp.ref,
                fp.field_path,
                None,
                statement.span,
                procedure,
                self.statement_preds,
                hook_id,
                "unknown",
                fp,
            )
        refs = tuple(fp.ref for fp in footprints)
        self.skip(reason, statement.span, procedure, refs, statement.raw_text, refs, hook_id)
        self.marks.append((statement.span, Classification.UNKNOWN))

    def _map_statement(
        self,
        statement: Statement,
        assignment: tuple[str, tuple[Token, ...]] | None,
        call: tuple[str, tuple[tuple[Token, ...], ...]] | None,
        env: dict[str, _Bind],
        preds: tuple[Pred, ...],
        hook_id: str | None,
        procedure: str,
    ) -> bool:
        if assignment:
            left, right = assignment
            norm = tuple(
                token.folded for token in right if token.kind != "symbol" or token.value != "."
            )
            route = procedure.casefold() in _ROUTE_CALLBACKS or left.casefold().endswith(
                "версииформатаобмена"
            )
            if norm == ("новый", "соответствие") and route:
                env[left.casefold()] = _Bind("map", map_role="pending", plan_name=self.plan_name)
                return True
            target = env.get(left.casefold())
            joined = _join(right)
            if (
                target
                and target.tag == "settings"
                and left.casefold().endswith("версииформатаобмена")
            ):
                return True
            source = env.get(joined.casefold()) if joined else None
            if (
                target
                and target.tag == "settings"
                and left.casefold().endswith(".версииформатаобмена")
                and source
                and source.tag == "map"
            ):
                source.map_role = "plan"
                source.plan_name = self.plan_name
                return True
            if joined.casefold() in ("настройки.версииформатаобмена",):
                env[left.casefold()] = _Bind("map", map_role="plan", plan_name=self.plan_name)
                return True
        if not call:
            return False
        name = call[0]
        owner, _, method = name.rpartition(".")
        if method.casefold() != "вставить" or len(call[1]) < 2:
            return False
        bind = env.get(owner.casefold())
        key = call[1][0]
        module = call[1][1]
        if len(key) != 1 or key[0].kind != "string":
            return False
        if not module or module[0].kind != "identifier" or len(module) != 1:
            return False
        role = ""
        plan = self.plan_name
        if bind and bind.tag == "map":
            role = bind.map_role
            plan = bind.plan_name
        elif owner.casefold() == "настройки.версииформатаобмена" or (
            bind and bind.tag == "settings" and owner.casefold().endswith("версииформатаобмена")
        ):
            role = "plan"
        if role in ("", "pending"):
            return False
        value = _expr(self.source, module, statement.span)
        target = "without_node" if role == "without_node" else f"plan/{plan or ''}"
        self.op(
            OperationKind.MAP_INSERT,
            target,
            (key[0].value,),
            value,
            statement.span,
            procedure,
            preds,
            hook_id,
        )
        return True

    def _builder_statement(
        self,
        statement: Statement,
        assignment: tuple[str, tuple[Token, ...]] | None,
        call: tuple[str, tuple[tuple[Token, ...], ...]] | None,
        env: dict[str, _Bind],
        preds: tuple[Pred, ...],
        hook_id: str | None,
        procedure: str,
        chain: tuple[SourceSpan, ...],
    ) -> bool:
        if assignment and call:
            started = self._start_builder(assignment[0], call, env, statement)
            if started:
                return True
        if assignment and not call:
            return self._builder_assign(statement, assignment, env, preds, hook_id, procedure)
        return bool(
            call and self._builder_call(statement, call, env, preds, hook_id, procedure, chain)
        )

    def _start_builder(
        self,
        variable: str,
        call: tuple[str, tuple[tuple[Token, ...], ...]],
        env: dict[str, _Bind],
        statement: Statement,
    ) -> bool:
        name = call[0]
        method = name.rpartition(".")[2].casefold()
        receiver = name.rpartition(".")[0]
        bind = env.get(receiver.casefold())
        kind = ""
        collection = ""
        if (
            name.casefold() == "обменданнымиxdtoсервер.инициализироватьправилоконвертацииобъекта"
            and len(call[1]) == 1
        ):
            argument = call[1][0]
            host = (
                env.get(argument[0].folded)
                if len(argument) == 1 and argument[0].kind == "identifier"
                else None
            )
            if host and host.collection == "pko":
                kind, collection = "pko", "pko"
        elif (
            method == "добавить"
            and not call[1]
            and bind
            and bind.tag == "collection"
            and bind.collection == "pod"
        ):
            kind, collection = "pod", "pod"
        elif (
            method == "добавить"
            and not call[1]
            and bind
            and bind.tag == "collection"
            and bind.collection == "pkpd"
        ):
            kind, collection = "pkpd", "pkpd"
        if not kind:
            return False
        self._flush(variable.casefold(), (), None, "")
        builder = _Builder(kind, variable, collection, statement, statement)
        builder.preds = (Pred.opaque(),) if self.opaque_depth else self.statement_preds
        self.builders[variable.casefold()] = builder
        env[variable.casefold()] = _Bind(
            "builder", collection=collection, builder=variable.casefold()
        )
        return True

    def _builder_assign(
        self,
        statement: Statement,
        assignment: tuple[str, tuple[Token, ...]],
        env: dict[str, _Bind],
        preds: tuple[Pred, ...],
        hook_id: str | None,
        procedure: str,
    ) -> bool:
        left, right = assignment
        base, _, field_name = left.rpartition(".")
        bind = env.get((base or left).casefold())
        if bind and bind.tag == "builder" and field_name:
            builder = self.builders.get(bind.builder)
            if builder is None:
                return False
            builder.end = statement
            folded = field_name.casefold()
            if folded == "используемыепко" and _new_type(right, "массив"):
                builder.array_ready = True
                return True
            if folded in (
                "конвертациизначенийприотправке",
                "конвертациизначенийприполучении",
            ):
                value = env.get(right[0].folded) if len(right) == 1 else None
                return bool(value and value.tag == "values" and value.builder == bind.builder)
            expression = _expr(self.source, right, statement.span)
            if not expression.literal_type and (
                not expression.reference_parts or expression.reference_parts[0].casefold() in env
            ):
                builder.tainted = True
                self._reject(statement, self.touched, procedure, hook_id, "computed_value")
                return True
            table = {"pko": _PKO_SET, "pod": _POD_SET, "pkpd": _PKPD_SET}[builder.kind]
            if folded not in table:
                builder.tainted = True
                self.skip(
                    "unknown_field",
                    statement.span,
                    procedure,
                    (builder.collection,),
                    statement.raw_text,
                    (builder.collection,),
                    hook_id,
                )
                return True
            canonical = table[folded]
            if folded in _NAME_FIELDS and expression.literal_type == "string":
                builder.name = str(expression.literal_value)
            builder.fields[canonical] = expression
            if (
                canonical
                in {
                    "ПриОтправкеДанных",
                    "ПриОбработке",
                    "ВыборкаДанных",
                }
                and expression.literal_type == "string"
            ):
                builder.events.append(
                    HandlerBinding(
                        entity_id=f"{self.source.file_id}:binding:{statement.span.char_start}",
                        kind="binding",
                        name=canonical,
                        span=statement.span,
                        raw_text=statement.raw_text,
                        owner_id="",
                        event=canonical,
                        target_name=str(expression.literal_value),
                    )
                )
            _ = preds
            return True
        if bind and bind.tag == "builder" and not field_name:
            return False
        prop = env.get(base.casefold()) if base else None
        if prop and prop.tag == "properties" and prop.builder:
            builder = self.builders.get(prop.builder)
            if builder is None:
                return False
            _ = field_name
            return False
        return False

    def _builder_call(
        self,
        statement: Statement,
        call: tuple[str, tuple[tuple[Token, ...], ...]],
        env: dict[str, _Bind],
        preds: tuple[Pred, ...],
        hook_id: str | None,
        procedure: str,
        chain: tuple[SourceSpan, ...],
    ) -> bool:
        name = call[0].casefold()
        if name in ("добавитьпкс", "добавитьпктч"):
            return self._add_property(
                statement, call, env, preds, hook_id, procedure, chain, builder_only=False
            )
        owner, _, method = call[0].rpartition(".")
        if method.casefold() == "добавить" and owner.casefold().endswith("используемыепко"):
            base_name = owner.rpartition(".")[0]
            host = env.get(base_name.casefold()) if base_name else None
            builder = self.builders.get(host.builder) if host and host.tag == "builder" else None
            if (
                builder is not None
                and len(call[1]) == 1
                and len(call[1][0]) == 1
                and call[1][0][0].kind == "string"
            ):
                builder.used.append(RuleRef(call[1][0][0].value, statement.span))
                builder.end = statement
                return True
        bind = env.get(owner.casefold())
        if bind and bind.tag == "builder":
            builder = self.builders.get(bind.builder)
            if builder is None:
                return False
            builder.end = statement
            one_string = (
                len(call[1]) == 1 and len(call[1][0]) == 1 and call[1][0][0].kind == "string"
            )
            adding = method.casefold() == "добавить"
            if adding and owner.casefold().endswith("используемыепко") and one_string:
                builder.used.append(RuleRef(call[1][0][0].value, statement.span))
                return True
            if adding and "поляпоиска" in owner.casefold() and one_string:
                raw = call[1][0][0].value
                builder.searches.append(
                    SearchSet(
                        entity_id=f"{self.source.file_id}:search:{statement.span.char_start}",
                        kind="search",
                        name=str(len(builder.searches) + 1),
                        span=statement.span,
                        raw_text=statement.raw_text,
                        owner_id="",
                        value_raw=raw,
                        fields=tuple(part.strip() for part in raw.split(",")),
                        ordinal=len(builder.searches) + 1,
                    )
                )
                return True
            if method.casefold() == "вставить" and bind.collection == "pkpd":
                return self._mapping(statement, call, env, builder)
        if method.casefold() == "вставить":
            side = env.get(owner.casefold())
            if side and side.tag == "values":
                builder = self.builders.get(side.builder)
                if builder is not None and len(call[1]) == 2:
                    first = _expr(self.source, call[1][0], statement.span)
                    second = _expr(self.source, call[1][1], statement.span)
                    sending = side.map_role == "send"
                    builder.mappings.append(
                        ValueMapping(
                            entity_id=f"{self.source.file_id}:value:{statement.span.char_start}",
                            kind="value",
                            name=str(len(builder.mappings) + 1),
                            span=statement.span,
                            raw_text=statement.raw_text,
                            configuration_value=first if sending else second,
                            format_value=second if sending else first,
                            direction="send" if sending else "receive",
                            ordinal=len(builder.mappings) + 1,
                        )
                    )
                    builder.end = statement
                    return True
        if (
            name == "обменданнымиxdtoсервер.инициализироватьрасширениеправилаконвертацииобъекта"
            and len(call[1]) == 2
        ):
            target = self._rule_target(call[1][0], env)
            uri = call[1][1]
            if target and len(uri) == 1 and uri[0].kind == "string":
                builder = self.builders.get(target[1])
                if builder is not None:
                    builder.extensions.append(uri[0].value)
                    builder.end = statement
                    return True
                self.op(
                    OperationKind.INIT_EXTENSION,
                    target[0],
                    ("extensions",),
                    _expr(self.source, uri, statement.span),
                    statement.span,
                    procedure,
                    preds,
                    hook_id,
                    chain=chain,
                )
                return True
        return False

    def _mapping(
        self,
        statement: Statement,
        call: tuple[str, tuple[tuple[Token, ...], ...]],
        env: dict[str, _Bind],
        builder: _Builder,
    ) -> bool:
        _ = (statement, call, env, builder)
        return False

    def _add_property(
        self,
        statement: Statement,
        call: tuple[str, tuple[tuple[Token, ...], ...]],
        env: dict[str, _Bind],
        preds: tuple[Pred, ...],
        hook_id: str | None,
        procedure: str,
        chain: tuple[SourceSpan, ...],
        builder_only: bool,
    ) -> bool:
        group = call[0].casefold() == "добавитьпктч"
        helper = call[0].casefold()
        if self.version == 1 and group:
            self.skip(
                "v1_group",
                statement.span,
                procedure,
                ("pko",),
                statement.raw_text,
                ("pko",),
                hook_id,
            )
            self.marks.append((statement.span, Classification.UNKNOWN))
            return True
        if not self.helpers.get(helper, False):
            self._unknown_call(statement, procedure, hook_id, "helper_unverified")
            self.marks.append((statement.span, Classification.UNKNOWN))
            return True
        maximum = (5 if group else 7) if self.version == 3 else (4 if group else 6)
        if not 3 <= len(call[1]) <= maximum:
            self._unknown_call(statement, procedure, hook_id, "unsupported_property_arguments")
            return True
        parent = self._properties_of(call[1][0], env)
        if parent is None:
            self._unknown_call(statement, procedure, hook_id, "unknown_property_parent")
            return True
        args = [_expr(self.source, part, statement.span) for part in call[1]]
        if any(
            arg.literal_type != "string"
            for index, arg in enumerate(args)
            if index > 0 and (group or index != 3)
        ):
            self._unknown_call(statement, procedure, hook_id, "nonliteral_property")
            return True
        strings = [str(arg.literal_value) if arg.literal_type == "string" else "" for arg in args]
        while len(strings) < 7:
            strings.append("")
        presence = tuple(bool(part) for part in call[1])
        _collection, rule_key, builder_id = parent
        if group:
            prop_group = PropertyGroup(
                entity_id=f"{self.source.file_id}:pktch:{statement.span.char_start}",
                kind="pktch",
                name=strings[2] or strings[1],
                span=statement.span,
                raw_text=statement.raw_text,
                owner_id=rule_key,
                configuration_property=strings[1],
                format_property=strings[2],
                namespace=strings[3],
                condition_name=strings[4],
            )
            if builder_id:
                builder = self.builders[builder_id]
                builder.groups.append(prop_group)
                builder.end = statement
                env_key = f"group:{statement.span.char_start}"
                self.builders[builder_id] = builder
                _ = env_key
            else:
                self.op(
                    OperationKind.ADD,
                    rule_key,
                    ("groups", prop_group.format_property or prop_group.configuration_property),
                    prop_group,
                    statement.span,
                    procedure,
                    preds,
                    hook_id,
                    chain=chain,
                )
            return True
        flag = 0
        if len(args) > 3 and args[3].literal_type == "number":
            flag = int(args[3].literal_value or 0)
        elif len(call[1]) > 3:
            self._unknown_call(statement, procedure, hook_id, "nonliteral_property")
            return True
        prop = PropertyRule(
            entity_id=f"{self.source.file_id}:pks:{statement.span.char_start}",
            kind="pks",
            name=strings[2] or strings[1],
            span=statement.span,
            raw_text=statement.raw_text,
            owner_id=rule_key,
            group_id=None,
            configuration_property=strings[1],
            format_property=strings[2],
            algorithm_flag=flag,
            conversion_rule=strings[4],
            namespace=strings[5],
            condition_name=strings[6],
            argument_presence=presence,
            raw_arguments=tuple(args),
        )
        if builder_id:
            builder = self.builders[builder_id]
            builder.properties.append(prop)
            builder.end = statement
            return True
        if builder_only:
            return False
        resolution = "unknown" if self.flow_unknown else "applied"
        self.op(
            OperationKind.ADD,
            rule_key,
            ("properties", prop.format_property or prop.configuration_property),
            prop,
            statement.span,
            procedure,
            preds,
            hook_id,
            resolution,
            None if resolution == "applied" else Footprint("entity", rule_key, ("properties",)),
            chain,
        )
        self.marks.append(
            (
                statement.span,
                Classification.DECLARATIVE if resolution == "applied" else Classification.UNKNOWN,
            )
        )
        return True

    def _properties_of(
        self, tokens: tuple[Token, ...], env: dict[str, _Bind]
    ) -> tuple[str, str, str] | None:
        if len(tokens) == 1 and tokens[0].kind == "identifier":
            bind = env.get(tokens[0].folded)
            if bind and bind.tag == "properties":
                return bind.collection, bind.rule_key, bind.builder
            return None
        if len(tokens) == 3 and tokens[1].value == "." and tokens[2].folded == "свойства":
            bind = env.get(tokens[0].folded)
            if bind and bind.tag == "builder":
                return bind.collection, bind.builder, bind.builder
            if bind and bind.tag == "rule":
                return bind.collection, bind.rule_key, ""
        return None

    def _rule_target(
        self, tokens: tuple[Token, ...], env: dict[str, _Bind]
    ) -> tuple[str, str] | None:
        if len(tokens) == 1 and tokens[0].kind == "identifier":
            bind = env.get(tokens[0].folded)
            if bind and bind.tag == "builder":
                return bind.builder, bind.builder
            if bind and bind.tag == "rule":
                return bind.rule_key, ""
        return None

    def _call_statement(
        self,
        statement: Statement,
        call: tuple[str, tuple[tuple[Token, ...], ...]],
        env: dict[str, _Bind],
        preds: tuple[Pred, ...],
        hook_id: str | None,
        procedure: str,
        chain: tuple[SourceSpan, ...],
    ) -> bool:
        if call[0].casefold() in ("добавитьпкс", "добавитьпктч"):
            return self._add_property(statement, call, env, preds, hook_id, procedure, chain, False)
        if self._collection_method(statement, call, env, preds, hook_id, procedure):
            return True
        if "." in call[0]:
            if _rule_edge(call, env):
                self.edges += 1
                if self.edges > MAX_EDGES:
                    self._area_skip(
                        "resource_limit",
                        statement.span,
                        procedure,
                        "manager",
                        statement.raw_text,
                        hook_id,
                        "manager",
                    )
                    self.flow_unknown = True
                    return True
            self._foreign(statement, call, env, procedure, hook_id)
            return True
        local = self.routines.get(call[0].casefold())
        if local is None:
            self._foreign(statement, call, env, procedure, hook_id)
            return True
        self.calls += 1
        if self.calls > MAX_CALLS:
            raise _Abort("resource_limit", self.origin(statement.span, procedure, hook_id))
        if call[0].casefold() in self.stack:
            raise _Abort("recursion", self.origin(statement.span, procedure, hook_id))
        routine, tree = local
        required = sum(param.default is None for param in routine.parameters)
        if not required <= len(call[1]) <= len(routine.parameters):
            self._reject(
                statement,
                self._statement_footprints(statement, env),
                procedure,
                hook_id,
                "unknown_call",
            )
            return True
        references = [
            _join(arg).casefold()
            for param, arg in zip(routine.parameters, call[1], strict=False)
            if not param.by_value and arg and arg[0].folded in env
        ]
        if len(references) != len(set(references)):
            _invalidate_context_call(statement.tokens, env)
            self._reject(
                statement,
                self._statement_footprints(statement, env),
                procedure,
                hook_id,
                "unknown_call",
            )
            return True
        child_env: dict[str, _Bind] = {}
        for param, arg in zip(routine.parameters, call[1], strict=False):
            key = (param.name or "").casefold()
            if len(arg) == 1 and arg[0].kind == "identifier" and arg[0].folded in env:
                child_env[key] = env[arg[0].folded]
            else:
                props = self._properties_of(arg, env)
                if props:
                    child_env[key] = _Bind(
                        "properties", collection=props[0], rule_key=props[1], builder=props[2]
                    )
                elif any(
                    token.kind == "identifier"
                    and token.folded in env
                    and env[token.folded].collection
                    and not _scalar_read(arg, index, env)
                    for index, token in enumerate(arg)
                ):
                    self._reject(
                        statement,
                        self._statement_footprints(statement, env),
                        procedure,
                        hook_id,
                        "unknown_call",
                    )
                    return True
                else:
                    child_env[key] = _Bind("unknown")
        self.stack.append(call[0].casefold())
        first_caught = len(self.catches[-1]) if self.catches else 0
        previous_parameters = self.parameter_collections
        self.parameter_collections = _collection_parameters(routine, child_env)
        try:
            self.walk(tree, child_env, _and(preds), hook_id, routine.name, (*chain, statement.span))
            self._flush_all(hook_id, routine.name, preds)
            for param, arg in zip(routine.parameters, call[1], strict=False):
                if not param.by_value and len(arg) == 1 and arg[0].folded in env:
                    env[arg[0].folded] = child_env.get(
                        (param.name or "").casefold(), _Bind("unknown")
                    )
            if self.catches:
                for catch_index in range(first_caught, len(self.catches[-1])):
                    path, state = self.catches[-1][catch_index]
                    caller = dict(env)
                    for param, arg in zip(routine.parameters, call[1], strict=False):
                        if not param.by_value and len(arg) == 1 and arg[0].folded in env:
                            caller[arg[0].folded] = state.get(
                                (param.name or "").casefold(), _Bind("unknown")
                            )
                    self.catches[-1][catch_index] = path, caller
        finally:
            self.parameter_collections = previous_parameters
            self.stack.pop()
        self.marks.append((statement.span, Classification.DECLARATIVE))
        return True

    def _collection_method(
        self,
        statement: Statement,
        call: tuple[str, tuple[tuple[Token, ...], ...]],
        env: dict[str, _Bind],
        preds: tuple[Pred, ...],
        hook_id: str | None,
        procedure: str,
    ) -> bool:
        owner, _, method = call[0].rpartition(".")
        if not method:
            return False
        bind = env.get(owner.casefold())
        folded = method.casefold()
        parameters = (
            bind and bind.collection == "parameters"
        ) or owner.casefold() == "параметрыконвертации"
        if (
            parameters
            and folded == "вставить"
            and call[1]
            and len(call[1][0]) == 1
            and call[1][0][0].kind == "string"
        ):
            name = call[1][0][0].value
            if len(call[1]) == 1:
                value: Expr | None = Expr("Неопределено", statement.span, "undefined", None)
            elif len(call[1]) == 2:
                value = _expr(self.source, call[1][1], statement.span)
                if value.literal_type is None and not value.reference_parts:
                    self.skip(
                        "computed_value",
                        statement.span,
                        procedure,
                        (_ref("parameters", "Имя", name),),
                        statement.raw_text,
                        ("parameters",),
                        hook_id,
                    )
                    return True
            else:
                return False
            self.op(
                OperationKind.SET,
                _ref("parameters", "Имя", name),
                ("default",),
                value,
                statement.span,
                procedure,
                preds,
                hook_id,
            )
            self.marks.append((statement.span, Classification.DECLARATIVE))
            return True
        if (
            parameters
            and folded == "удалить"
            and len(call[1]) == 1
            and len(call[1][0]) == 1
            and call[1][0][0].kind == "string"
        ):
            name = call[1][0][0].value
            self.op(
                OperationKind.DELETE,
                _ref("parameters", "Имя", name),
                (),
                None,
                statement.span,
                procedure,
                preds,
                hook_id,
            )
            self.marks.append((statement.span, Classification.DECLARATIVE))
            return True
        if folded == "удалить" and bind and bind.tag == "collection" and len(call[1]) == 1:
            target = self._rule_target(call[1][0], env)
            if target and target[0]:
                if target[0].split(_UNIT)[0] != bind.collection:
                    self._unknown_call(statement, procedure, hook_id, "unresolved_delete")
                    return True
                self.op(
                    OperationKind.DELETE,
                    target[0],
                    (),
                    None,
                    statement.span,
                    procedure,
                    preds,
                    hook_id,
                )
                self.marks.append((statement.span, Classification.DECLARATIVE))
                return True
            self.skip(
                "unresolved_delete",
                statement.span,
                procedure,
                (bind.collection,),
                statement.raw_text,
                (bind.collection,),
                hook_id,
            )
            return True
        prop_owner = env.get(owner.casefold())
        if prop_owner is None:
            parent = self._properties_of(tokenize(owner), env)
            if parent:
                prop_owner = _Bind(
                    "properties", collection=parent[0], rule_key=parent[1], builder=parent[2]
                )
        if (
            folded == "удалить"
            and prop_owner
            and prop_owner.tag == "properties"
            and len(call[1]) == 1
        ):
            removed = env.get(_join(call[1][0]).casefold()) if len(call[1][0]) == 1 else None
            if removed and removed.tag == "property":
                if removed.rule_key != prop_owner.rule_key:
                    self._unknown_call(statement, procedure, hook_id, "unresolved_delete")
                    return True
                label = removed.format_property or removed.config_property
                self.op(
                    OperationKind.DELETE,
                    removed.rule_key,
                    ("properties", label, removed.column, removed.snapshot),
                    None,
                    statement.span,
                    procedure,
                    preds,
                    hook_id,
                )
                self.marks.append((statement.span, Classification.DECLARATIVE))
                return True
        return False

    def _foreign(
        self,
        statement: Statement,
        call: tuple[str, tuple[tuple[Token, ...], ...]],
        env: dict[str, _Bind],
        procedure: str,
        hook_id: str | None,
    ) -> None:
        _invalidate_context_call(statement.tokens, env)
        self._reject(
            statement,
            self._statement_footprints(statement, env),
            procedure,
            hook_id,
            "unknown_call",
        )

    def _unknown_call(
        self, statement: Statement, procedure: str, hook_id: str | None, reason: str
    ) -> None:
        self._reject(
            statement,
            self.touched or (Footprint("collection", self.collection or "pko"),),
            procedure,
            hook_id,
            reason,
        )

    def _assign_statement(
        self,
        statement: Statement,
        assignment: tuple[str, tuple[Token, ...]],
        env: dict[str, _Bind],
        preds: tuple[Pred, ...],
        hook_id: str | None,
        procedure: str,
    ) -> bool:
        left, right = assignment
        if "." not in left and len(right) == 1:
            if right[0].folded in ("истина", "ложь"):
                env[left.casefold()] = _Bind("boolean", literal=right[0].folded)
                return True
            if right[0].folded == "неопределено":
                env[left.casefold()] = _Bind("undefined")
                return True
            if right[0].kind == "string" and env.get(left.casefold(), _Bind("")).tag == "direction":
                env[left.casefold()] = _Bind("literal", literal=right[0].value)
                return True
        call = _call(right)
        if call and call[0].casefold().endswith(".найти") and len(call[1]) == 2:
            return self._find(statement, left, call, env, preds, procedure, hook_id)
        if (
            "." not in left
            and len(right) >= 1
            and right[-1].folded == "направлениеобмена"
            and (len(right) == 1 or (len(right) == 3 and right[1].value == "."))
        ):
            host = env.get(right[0].folded) if len(right) == 3 else None
            if (
                len(right) == 1
                or (host and host.tag == "components")
                or right[0].folded == "компонентыобмена"
            ):
                env[left.casefold()] = _Bind("direction")
                return True
        if (
            "." not in left
            and len(right) == 3
            and right[1].value == "."
            and right[2].folded in ("свойства", "свойстватабличныхчастей")
        ):
            owner = env.get(right[0].folded)
            if owner and owner.tag in ("rule", "builder"):
                env[left.casefold()] = _Bind(
                    "properties",
                    collection=owner.collection,
                    rule_key=owner.rule_key or owner.builder,
                    builder=owner.builder,
                )
                return True
        if (
            "." not in left
            and len(right) == 1
            and right[0].kind == "identifier"
            and right[0].folded in env
        ):
            env[left.casefold()] = env[right[0].folded]
            return True
        if _new_type(right, "массив"):
            owner_name = left.casefold()
            if owner_name.endswith(".используемыепко"):
                return True
        base, _, field_name = left.rpartition(".")
        target = env.get(base.casefold()) if base else None
        if target and target.tag == "rule" and field_name:
            return self._set_field(
                statement, target, field_name, right, preds, hook_id, procedure, env
            )
        if target and target.tag == "property" and field_name:
            return self._set_property_field(
                statement, target, field_name, right, preds, hook_id, procedure
            )
        if target and target.tag == "builder":
            return False
        if call:
            return False
        norm = tuple(token.folded for token in right)
        if len(norm) >= 2 and norm[0] == "новый":
            side = (
                "send"
                if "отправ" in left.casefold()
                else "receive"
                if "получ" in left.casefold()
                else ""
            )
            if side and _new_type(right, "соответствие"):
                env[left.casefold()] = _Bind("values", map_role=side, builder=_builder_id(env))
                return True
        if base:
            bind = env.get(base.casefold())
            if bind and bind.tag == "values":
                builder = self.builders.get(bind.builder) if bind.builder else None
                if builder is not None:
                    return True
        previous = env.get(left.casefold())
        env[left.casefold()] = replace(previous, tag="unknown") if previous else _Bind("unknown")
        return False

    def _find(
        self,
        statement: Statement,
        variable: str,
        call: tuple[str, tuple[tuple[Token, ...], ...]],
        env: dict[str, _Bind],
        preds: tuple[Pred, ...],
        procedure: str,
        hook_id: str | None,
    ) -> bool:
        receiver = call[0].rpartition(".")[0]
        if env.get(receiver.casefold(), _Bind("")).tag == "collection":
            self._materialize(env, hook_id, procedure)
        bind = env.get(receiver.casefold())
        if bind is None and receiver.casefold().endswith(".свойства"):
            host = env.get(receiver.rpartition(".")[0].casefold())
            if host and host.tag == "rule":
                bind = _Bind(
                    "properties",
                    collection=host.collection,
                    rule_key=host.rule_key,
                    builder=host.builder,
                )
        key_tokens, column_tokens = call[1]
        if (
            len(column_tokens) != 1
            or column_tokens[0].kind != "string"
            or len(key_tokens) != 1
            or key_tokens[0].kind != "string"
        ):
            env[variable.casefold()] = _Bind("unknown")
            self.skip(
                "computed_name",
                statement.span,
                procedure,
                (
                    (bind.rule_key or bind.collection if bind else "")
                    or self.collection
                    or "manager",
                ),
                statement.raw_text,
                (
                    (bind.rule_key or bind.collection if bind else "")
                    or self.collection
                    or "manager",
                ),
                hook_id,
            )
            self.marks.append((statement.span, Classification.UNKNOWN))
            return True
        column = column_tokens[0].value
        literal = key_tokens[0].value
        collection = ""
        rule_key = ""
        if bind and bind.tag == "collection" and column in _columns(bind.collection):
            collection = bind.collection
            rule_key = _ref(collection, column, literal)
            env[variable.casefold()] = _Bind(
                "rule", collection=collection, column=column, literal=literal, rule_key=rule_key
            )
            self.marks.append((statement.span, Classification.DECLARATIVE))
            return True
        if (
            bind
            and bind.tag == "properties"
            and column in ("СвойствоФормата", "СвойствоКонфигурации")
        ):
            snapshot = str(statement.span.char_start)
            env[variable.casefold()] = _Bind(
                "property",
                collection=bind.collection,
                column=column,
                literal=literal,
                rule_key=bind.rule_key,
                format_property=literal if column == "СвойствоФормата" else "",
                config_property=literal if column == "СвойствоКонфигурации" else "",
                builder=bind.builder,
                snapshot=snapshot,
            )
            self.op(
                OperationKind.APPEND,
                _UNIT.join((bind.rule_key, column, literal)) + "\x1e" + snapshot,
                ("snapshot",),
                None,
                statement.span,
                procedure,
                preds,
                hook_id,
                "observe",
            )
            self.marks.append((statement.span, Classification.DECLARATIVE))
            return True
        env[variable.casefold()] = _Bind("unknown")
        self.skip(
            "unknown_find",
            statement.span,
            procedure,
            ((bind.rule_key or bind.collection if bind else "") or self.collection or "manager",),
            statement.raw_text,
            ((bind.rule_key or bind.collection if bind else "") or self.collection or "manager",),
            hook_id,
        )
        self.marks.append((statement.span, Classification.UNKNOWN))
        return True

    def _set_field(
        self,
        statement: Statement,
        target: _Bind,
        field_name: str,
        right: tuple[Token, ...],
        preds: tuple[Pred, ...],
        hook_id: str | None,
        procedure: str,
        env: dict[str, _Bind],
    ) -> bool:
        folded = field_name.casefold()
        if folded in _NAME_FIELDS:
            self.skip(
                "name_mutation",
                statement.span,
                procedure,
                (target.rule_key,),
                statement.raw_text,
                (target.collection,),
                hook_id,
            )
            self.marks.append((statement.span, Classification.UNKNOWN))
            return True
        table = {"pko": _PKO_SET, "pod": _POD_SET, "pkpd": _PKPD_SET}.get(target.collection, {})
        if folded not in table:
            self.skip(
                "unknown_field",
                statement.span,
                procedure,
                (target.rule_key,),
                statement.raw_text,
                (target.rule_key,),
                hook_id,
            )
            return True
        expression = _expr(self.source, right, statement.span)
        resolution = (
            "applied"
            if expression.literal_type
            or (expression.reference_parts and expression.reference_parts[0].casefold() not in env)
            else "unknown"
        )
        if resolution == "unknown":
            self.skip(
                "computed_value",
                statement.span,
                procedure,
                (target.rule_key,),
                statement.raw_text,
                (target.rule_key, table[folded]),
                hook_id,
            )
        self.op(
            OperationKind.SET if resolution == "applied" else OperationKind.UNKNOWN,
            target.rule_key,
            (table[folded],),
            expression,
            statement.span,
            procedure,
            preds,
            hook_id,
            resolution,
            None
            if resolution == "applied"
            else Footprint("field", target.rule_key, (table[folded],)),
        )
        return True

    def _set_property_field(
        self,
        statement: Statement,
        target: _Bind,
        field_name: str,
        right: tuple[Token, ...],
        preds: tuple[Pred, ...],
        hook_id: str | None,
        procedure: str,
    ) -> bool:
        mapped = _PROPERTY_FIELDS.get(field_name.casefold())
        if mapped is None:
            self.skip(
                "unknown_field",
                statement.span,
                procedure,
                (target.rule_key,),
                statement.raw_text,
                (target.rule_key,),
                hook_id,
            )
            return True
        expression = _expr(self.source, right, statement.span)
        known = expression.literal_type is not None
        label = target.format_property or target.config_property
        self.op(
            OperationKind.SET if known else OperationKind.UNKNOWN,
            target.rule_key,
            ("properties", label, mapped, target.column, target.snapshot),
            expression,
            statement.span,
            procedure,
            preds,
            hook_id,
            "applied" if known else "unknown",
            None if known else Footprint("field", target.rule_key, ("properties", label, mapped)),
        )
        return True

    def _flush_all(self, hook_id: str | None, procedure: str, preds: tuple[Pred, ...]) -> None:
        for key in list(self.builders):
            self._flush(key, preds, hook_id, procedure)
        self.builders.clear()

    def _materialize(self, env: dict[str, _Bind], hook_id: str | None, procedure: str) -> None:
        """Перед развилкой блок AddRule становится строкой с условием создания."""
        keys = {}
        for key, builder in list(self.builders.items()):
            if builder.name:
                column = {"pko": "ИмяПКО", "pod": "Имя", "pkpd": "ИмяПКПД"}[builder.kind]
                keys[key] = _ref(builder.collection, column, builder.name)
            self._flush(key, (), hook_id, procedure)
        for name, bind in list(env.items()):
            if bind.builder in keys:
                env[name] = replace(
                    bind,
                    tag="rule" if bind.tag == "builder" else bind.tag,
                    builder="",
                    rule_key=keys[bind.builder],
                )

    def _flush(
        self, key: str, preds: tuple[Pred, ...], hook_id: str | None, procedure: str
    ) -> None:
        builder = self.builders.pop(key, None)
        if builder is None or not procedure:
            return
        span = self.source.span(builder.start.span.char_start, builder.end.span.char_end)
        raw = self.source.text[span.char_start : span.char_end]
        if builder.tainted or not builder.name:
            self.skip(
                "incomplete_rule",
                span,
                procedure,
                (builder.collection,),
                raw,
                (builder.collection,),
                hook_id,
            )
            return
        entity = _entity_from_builder(self.source, builder, span, raw)
        column = {"pko": "ИмяПКО", "pod": "Имя", "pkpd": "ИмяПКПД"}[builder.kind]
        self.op(
            OperationKind.ADD,
            _ref(builder.collection, column, builder.name),
            (),
            entity,
            span,
            procedure,
            builder.preds,
            hook_id,
            footprint=Footprint("collection", builder.collection),
        )

    def _condition(self, tokens: tuple[Token, ...], env: dict[str, _Bind]) -> Pred:
        if not tokens:
            return Pred.opaque()
        return _parse_or(tokens, env)


def _builder_id(env: dict[str, _Bind]) -> str:
    for bind in env.values():
        if bind.tag == "builder" and bind.builder:
            return bind.builder
    return ""


def _new_type(tokens: tuple[Token, ...], name: str) -> bool:
    """Только пустой конструктор; выражение с вызовом не доказывает создание коллекции."""
    return (
        len(tokens) in (2, 4)
        and tokens[0].folded == "новый"
        and tokens[1].folded == name
        and (len(tokens) == 2 or tuple(token.value for token in tokens[2:]) == ("(", ")"))
    )


def _invalidate_context_call(tokens: tuple[Token, ...], env: dict[str, _Bind]) -> None:
    """Непроверенный вызов может изменить переданное по ссылке условие будущего пути."""
    if not any(token.value == "(" for token in tokens):
        return
    for token in tokens:
        bind = env.get(token.folded)
        if bind and bind.tag in ("direction", "components", "headers"):
            env[token.folded] = _Bind("unknown")


def _invalidate_after_opaque(nodes: Sequence[_Node], env: dict[str, _Bind]) -> None:
    """Цикл/попытка не доказывают итог присваивания, но прежняя роль уже недостоверна."""
    for node in nodes:
        if isinstance(node, _If):
            for branch in node.branches:
                _invalidate_after_opaque(branch.body, env)
        elif isinstance(node, (_Loop, _Try)):
            _invalidate_after_opaque(node.body, env)
            if isinstance(node, _Try):
                _invalidate_after_opaque(node.handler, env)
        tokens = node.statement.tokens
        _invalidate_context_call(tokens, env)
        for index, token in enumerate(tokens[:-1]):
            bind = env.get(token.folded)
            if bind and tokens[index + 1].value == "=":
                env[token.folded] = replace(bind, tag="unknown", paths=())


def _columns(collection: str) -> frozenset[str]:
    if collection == "pko":
        return frozenset({"ИмяПКО"})
    if collection == "pod":
        return frozenset({"Имя"})
    if collection == "pkpd":
        return frozenset({"ИмяПКПД"})
    return frozenset()


def _ref(collection: str, column: str, name: str) -> str:
    return _UNIT.join((collection, column, name))


def _unwrap(tokens: tuple[Token, ...]) -> tuple[Token, ...]:
    while len(tokens) >= 2 and tokens[0].value == "(" and tokens[-1].value == ")":
        depth = 0
        wraps = True
        for index, token in enumerate(tokens):
            if token.value == "(":
                depth += 1
            elif token.value == ")":
                depth -= 1
                if depth == 0 and index != len(tokens) - 1:
                    wraps = False
                    break
        if not wraps or depth != 0:
            break
        tokens = tokens[1:-1]
    return tokens


def _parse_or(tokens: tuple[Token, ...], env: dict[str, _Bind]) -> Pred:
    tokens = _unwrap(tokens)
    parts = _split_kw(tokens, "или")
    if not parts:
        return _parse_and(tokens, env)
    kids = tuple(_parse_and(part, env) for part in parts)
    return Pred("or", kids=kids)


def _parse_and(tokens: tuple[Token, ...], env: dict[str, _Bind]) -> Pred:
    parts = _split_kw(tokens, "и")
    if not parts:
        return _parse_not(tokens, env)
    kids = tuple(_parse_not(part, env) for part in parts)
    return Pred("and", kids=kids)


def _parse_not(tokens: tuple[Token, ...], env: dict[str, _Bind]) -> Pred:
    unwrapped = _unwrap(tokens)
    if unwrapped != tokens:
        return _parse_or(unwrapped, env)
    if tokens and tokens[0].folded == "не":
        inner = _parse_not(tokens[1:], env)
        if inner.op == "atom" and inner.kind == "headers":
            return Pred.atom("not_headers")
        return Pred("not", kids=(inner,))
    return _parse_atom(tokens, env)


def _path_condition(
    tokens: tuple[Token, ...], env: dict[str, _Bind], span: SourceSpan, evaluation: str = ""
) -> tuple[Pred, str | None]:
    """Непрозрачный остаток — отдельный неизвестный атом, с известными границами пути.

    Это метка результата одной проверки, а не исполнение выражения BSL. Её значение
    остаётся None в holds; отрицание нужно только для взаимоисключающих ветвей.
    """
    reason = None
    serial = 0

    def convert(pred: Pred) -> Pred:
        nonlocal reason, serial
        if pred.op == "opaque":
            reason = pred.kind or "opaque_condition"
            serial += 1
            return Pred.atom(
                "opaque_path", f"{span.file_id}:{span.char_start}:{evaluation}:{serial}"
            )
        return replace(pred, kids=tuple(convert(kid) for kid in pred.kids))

    parsed = _dnf(convert(_parse_or(tokens, env)))
    if _is_opaque(parsed):
        return Pred.atom(
            "opaque_path", f"{span.file_id}:{span.char_start}:{evaluation}:limit"
        ), "condition_limit"
    return parsed, reason


def _unknown_path(pred: Pred) -> bool:
    return (
        pred.op == "opaque"
        or pred.kind == "opaque_path"
        or any(_unknown_path(kid) for kid in pred.kids)
    )


def _parse_atom(tokens: tuple[Token, ...], env: dict[str, _Bind]) -> Pred:
    call = _call(tokens)
    if call and call[0].casefold() == "значениезаполнено" and len(call[1]) == 1:
        return _compare(call[1][0], "<>", (Token("identifier", "Неопределено", 0, 0),), env)
    if (
        len(tokens) == 7
        and tokens[1].value == "."
        and tokens[2].folded == "количество"
        and tokens[3].value == "("
        and tokens[4].value == ")"
        and tokens[5].value == ">"
        and tokens[6].value == "0"
    ):
        bind = env.get(tokens[0].folded)
        if bind and bind.tag == "collection":
            return Pred.atom("collection_nonempty", bind.collection)
    if len(tokens) == 1:
        if tokens[0].folded in ("истина", "ложь"):
            return _true() if tokens[0].folded == "истина" else _false()
        bind = env.get(tokens[0].folded)
        if bind and bind.tag == "boolean":
            return _true() if bind.literal == "истина" else _false()
        if _is_headers(tokens, env):
            return Pred.atom("headers")
    op_index = _op_index(tokens)
    if op_index is None:
        return Pred.opaque()
    return _compare(tokens[:op_index], tokens[op_index].value, tokens[op_index + 1 :], env)


def _op_index(tokens: tuple[Token, ...]) -> int | None:
    depth = 0
    for index, token in enumerate(tokens):
        if token.value in ("(", "["):
            depth += 1
        elif token.value in (")", "]"):
            depth -= 1
        elif depth == 0 and token.value in ("=", "<>"):
            return index
    return None


def _is_chain(tokens: tuple[Token, ...]) -> bool:
    return bool(tokens)


def _compare(
    left: tuple[Token, ...], op: str, right: tuple[Token, ...], env: dict[str, _Bind]
) -> Pred:
    if (
        len(right) == 1
        and right[0].kind == "string"
        and right[0].value in ("Отправка", "Получение")
    ):
        direction = "send" if right[0].value == "Отправка" else "receive"
        if len(left) == 1:
            bind = env.get(left[0].folded)
            if bind and bind.tag == "literal":
                same = bind.literal == right[0].value
                return _true() if same == (op == "=") else _false()
        if _is_direction(left, env):
            if op == "=":
                return Pred.atom("direction_is", direction)
            return Pred.atom("direction_is", "receive" if direction == "send" else "send")
        return Pred.opaque()
    if len(right) == 1 and right[0].kind == "string" and len(left) == 1:
        bind = env.get(left[0].folded)
        if bind and bind.tag == "undefined":
            return _false() if op == "=" else _true()
        if bind and bind.tag == "dispatch_name" and op == "=":
            return Pred.atom("dispatch_is", right[0].value)
    if (
        len(left) == 1
        and _is_headers(left, env)
        and len(right) == 1
        and right[0].folded in ("истина", "ложь")
    ):
        flag = right[0].folded == "истина"
        if op == "<>":
            flag = not flag
        return Pred.atom("headers" if flag else "not_headers")
    if len(right) == 1 and right[0].folded == "неопределено" and len(left) == 1:
        bind = env.get(left[0].folded)
        if bind and bind.tag == "undefined":
            return _true() if op == "=" else _false()
        if bind and bind.tag == "rule":
            kind = "rule_missing" if op == "=" else "rule_found"
            return Pred.atom(kind, bind.rule_key)
        if bind and bind.tag == "property":
            kind = "property_absent" if op == "=" else "property_present"
            arg = _UNIT.join((bind.rule_key, bind.column, bind.literal or ""))
            if bind.snapshot:
                arg = f"{arg}\x1e{bind.snapshot}"
            return Pred.atom(kind, arg)
        return Pred.opaque()
    return Pred.opaque()


def _is_headers(tokens: tuple[Token, ...], env: dict[str, _Bind]) -> bool:
    if len(tokens) != 1:
        return False
    bind = env.get(tokens[0].folded)
    return bool(bind and bind.tag == "headers") or (
        bind is None and tokens[0].folded == "толькозаголовки"
    )


def _is_direction(tokens: tuple[Token, ...], env: dict[str, _Bind]) -> bool:
    if len(tokens) == 1:
        bind = env.get(tokens[0].folded)
        return bool(bind and bind.tag == "direction") or (
            bind is None and tokens[0].folded == "направлениеобмена"
        )
    if len(tokens) == 3 and tokens[2].folded == "направлениеобмена":
        bind = env.get(tokens[0].folded)
        return bool(bind and bind.tag == "components") or (
            bind is None and tokens[0].folded == "компонентыобмена"
        )
    return False


def _role_bind(role: str, plan_name: str | None) -> _Bind:
    folded = role.casefold()
    if folded == "направлениеобмена":
        return _Bind("direction")
    if folded == "правилаконвертации":
        return _Bind("collection", collection="pko")
    if folded == "правилаобработкиданных":
        return _Bind("collection", collection="pod")
    if folded == "компонентыобмена":
        return _Bind("components")
    if folded == "толькозаголовки":
        return _Bind("headers")
    if folded == "параметрыконвертации":
        return _Bind("collection", collection="parameters")
    if folded == "настройки":
        return _Bind("settings", plan_name=plan_name)
    if folded == "версииформата":
        return _Bind("map", map_role="without_node", plan_name=plan_name)
    if folded in ("имяпроцедуры", "имяфункции"):
        return _Bind("dispatch_name")
    if folded == "параметры":
        return _Bind("unknown")
    return _Bind("unknown")


def _parameter_env(target: str, routine: Routine, plan_name: str | None) -> dict[str, _Bind]:
    folded = target.casefold()
    variants = _FILLER_VARIANTS.get(folded)
    if variants is None and folded in _ROUTE_CALLBACKS:
        variants = (_ROUTE_CALLBACKS[folded],)
    if variants:
        for kind, roles in variants:
            if routine.routine_kind == kind and len(routine.parameters) == len(roles):
                env: dict[str, _Bind] = {}
                for param, role in zip(routine.parameters, roles, strict=True):
                    bind = _role_bind(role, plan_name)
                    if (
                        folded == "заполнитьправилаконвертациипредопределенныхданных"
                        and role.casefold() == "правилаконвертации"
                    ):
                        bind = _Bind("collection", collection="pkpd")
                    env[(param.name or "").casefold()] = bind
                return env
    names = [(param.name or "").casefold() for param in routine.parameters]
    env: dict[str, _Bind] = {}
    if folded == "заполнитьправилаконвертацииобъектов":
        for name in names:
            if name == "направлениеобмена":
                env[name] = _Bind("direction")
            elif name == "правилаконвертации":
                env[name] = _Bind("collection", collection="pko")
            elif name == "компонентыобмена":
                env[name] = _Bind("components")
            elif name == "толькозаголовки":
                env[name] = _Bind("headers")
    elif folded == "заполнитьправилаобработкиданных":
        for name in names:
            if name == "направлениеобмена":
                env[name] = _Bind("direction")
            elif name == "правилаобработкиданных":
                env[name] = _Bind("collection", collection="pod")
    elif folded == "заполнитьправилаконвертациипредопределенныхданных":
        for name in names:
            if name == "направлениеобмена":
                env[name] = _Bind("direction")
            elif name == "правилаконвертации":
                env[name] = _Bind("collection", collection="pkpd")
    elif folded == "заполнитьпараметрыконвертации":
        for name in names:
            if name == "параметрыконвертации":
                env[name] = _Bind("collection", collection="parameters")
    elif folded == "приполучениинастроек":
        for name in names:
            if name == "настройки":
                env[name] = _Bind("settings", plan_name=plan_name)
    elif folded == "приполучениидоступныхверсийформата":
        for name in names:
            if name == "версииформата":
                env[name] = _Bind("map", map_role="without_node", plan_name=plan_name)
    elif folded in _DISPATCH_NAMES:
        for name in names:
            if name in ("имяпроцедуры", "имяфункции"):
                env[name] = _Bind("dispatch_name")
    return env


def _filler_collection(target: str) -> str:
    folded = target.casefold()
    return {
        "заполнитьправилаконвертацииобъектов": "pko",
        "заполнитьправилаобработкиданных": "pod",
        "заполнитьправилаконвертациипредопределенныхданных": "pkpd",
        "заполнитьпараметрыконвертации": "parameters",
    }.get(folded, "")


def _leading_continue(tree: list[_Node], routine: Routine) -> bool:
    significant = [node for node in tree if isinstance(node, _Stmt)]
    if not significant:
        return False
    return _single_continue(significant[:1], routine)


def _single_continue(nodes: Sequence[_Node], routine: Routine) -> bool:
    stmts = [node for node in nodes if isinstance(node, _Stmt)]
    if len(stmts) != 1 or len(nodes) != 1:
        return False
    tokens = _bare(stmts[0].statement.tokens)
    if tokens and tokens[0].folded == "возврат":
        tokens = tokens[1:]
    call = _call(tokens)
    if not call or call[0].casefold() != "продолжитьвызов":
        return False
    expected = tuple((param.name or "").casefold() for param in routine.parameters)
    actual = tuple(_join(arg).casefold() for arg in call[1])
    return actual == expected


def _return_only(nodes: Sequence[_Node]) -> bool:
    stmts = [node for node in nodes if isinstance(node, _Stmt)]
    return len(nodes) == 1 and len(stmts) == 1 and stmts[0].statement.head == "возврат"


def _returns(nodes: Sequence[_Node]) -> bool:
    for node in nodes:
        if isinstance(node, _Stmt) and node.statement.head == "возврат":
            return True
        if isinstance(node, _If) and any(_returns(branch.body) for branch in node.branches):
            return True
        if isinstance(node, (_Loop, _Try)) and _returns(node.body):
            return True
        if isinstance(node, _Try) and _returns(node.handler):
            return True
    return False


def _direct_call(nodes: Sequence[_Node]) -> bool:
    seen = False
    for node in nodes:
        if not isinstance(node, _Stmt):
            return False
        if node.statement.head == "возврат":
            return False
        call = _call(_bare(node.statement.tokens))
        if call is None or "." in call[0]:
            return False
        if seen:
            return False
        seen = True
    return seen


def _raises(statement: Statement) -> bool:
    if statement.head == "вызватьисключение":
        return True
    call = _call(statement.tokens)
    return call is not None and call[0].casefold() == "вызватьисключение"


def _as_preds(formula: Pred) -> tuple[Pred, ...]:
    """Конъюнкция атомов хранится списком: ``holds`` соединяет его через «И»."""
    if _is_true(formula):
        return ()
    if formula.op == "and":
        return formula.kids
    return (formula,)


def _rule_edge(call: tuple[str, tuple[tuple[Token, ...], ...]], env: dict[str, _Bind]) -> bool:
    """Межмодульный вызов коллекции или правила. Журнал и прочие точки не считаются."""
    owner = call[0].rpartition(".")[0]
    if not owner or owner.casefold() in env:
        return False
    for arg in call[1]:
        if len(arg) == 1 and arg[0].kind == "identifier":
            bind = env.get(arg[0].folded)
            if bind and bind.tag in ("collection", "rule", "properties", "property", "builder"):
                return True
    folded = call[0].casefold()
    return any(part in folded for part in ("пко", "под", "пкс", "пктч", "правила"))


def _preproc_branch(branch: _Branch) -> bool | None:
    """``True`` — ветвь серверного контекста, ``False`` — клиентская, ``None`` — обычное условие."""
    head = _head(branch.statement)
    if head not in ("#если", "#иначеесли"):
        return None
    text = "".join(token.value for token in (branch.condition or ())).casefold()
    text = "".join(text.split())
    server = {
        "сервер",
        "сервериливнешнеесоединение",
        "внешнеесоединениеилисервер",
    }
    client = {"клиент", "толстыйклиент", "тонкийклиент", "вебклиент", "мобильныйклиент"}
    if text in server:
        return True
    if text in client:
        return False
    return None


def _dispatch_literal(pred: Pred) -> str | None:
    if pred.op == "atom" and pred.kind == "dispatch_is":
        return pred.arg
    return None


def _case(
    source: SourceFile, routine: Routine, literal: str, statement: Statement
) -> DispatcherCase | None:
    tokens = _bare(statement.tokens)
    returns = statement.head == "возврат"
    call = _call(tokens[1:] if returns else tokens)
    if call is None:
        return None
    opening = next((index for index, token in enumerate(tokens) if token.value == "("), None)
    if opening is None:
        return None
    target_tokens = tokens[1:opening] if returns else tokens[:opening]
    return DispatcherCase(
        entity_id=f"{source.file_id}:case:{statement.span.char_start}",
        kind="case",
        name=literal,
        span=statement.span,
        raw_text=statement.raw_text,
        dispatcher_id=routine.entity_id,
        literal_name=literal,
        target=_expr(source, target_tokens, statement.span),
        arguments=tuple(_expr(source, arg, statement.span) for arg in call[1]),
        returns=returns,
    )


def _guards(source: SourceFile, span: SourceSpan, preds: tuple[Pred, ...]) -> tuple[Guard, ...]:
    flat: list[Pred] = []

    def visit(pred: Pred) -> None:
        if pred.op == "and":
            for kid in pred.kids:
                visit(kid)
        elif pred.op != "or":
            flat.append(pred)

    for pred in preds:
        visit(pred)
    result: list[Guard] = []
    for index, pred in enumerate(flat):
        result.append(
            Guard(
                entity_id=f"{span.file_id}:layer-guard:{span.char_start}:{index}:{pred.kind}",
                kind="guard",
                name=pred.kind or pred.op,
                span=span,
                raw_text=f"{pred.kind}:{pred.arg}",
                expression_raw=f"{pred.kind}:{pred.arg}",
                branch="if",
                known_direction=pred.arg if pred.kind == "direction_is" else None,
                guard_kind=pred.kind or pred.op,
            )
        )
    return tuple(result)


def _entity_from_builder(
    source: SourceFile, builder: _Builder, span: SourceSpan, raw: str
) -> Entity:
    name = builder.name or ""
    common = {
        "entity_id": f"{source.file_id}:{builder.kind}:{span.char_start}",
        "kind": builder.kind,
        "name": name,
        "span": span,
        "raw_text": raw,
        "status": ParseStatus.PARTIAL if builder.tainted else ParseStatus.COMPLETE,
    }
    if builder.kind == "pko":

        def field_of(key: str) -> Any:
            expression = builder.fields.get(key)
            if expression is None:
                return ValueField()
            if key in ("ОбъектДанных",):
                return ValueField("expression", expression, (expression,))
            return ValueField("literal", expression.literal_value, (expression,))

        return ObjectRule(
            **common,
            procedure_name="",
            declared_name=name,
            configuration_object=field_of("ОбъектДанных"),
            format_object=field_of("ОбъектФормата"),
            group_flag=field_of("ПравилоДляГруппыСправочника"),
            identification=field_of("ВариантИдентификации"),
            events=tuple(builder.events),
            properties=tuple(builder.properties),
            groups=tuple(builder.groups),
            search_sets=tuple(builder.searches),
            extensions=tuple(builder.extensions),
        )
    if builder.kind == "pod":

        def pod_field(key: str) -> Any:
            expression = builder.fields.get(key)
            if expression is None:
                return ValueField()
            if key == "ОбъектВыборкиМетаданные":
                return ValueField("expression", expression, (expression,))
            return ValueField("literal", expression.literal_value, (expression,))

        return ProcessingRule(
            **common,
            procedure_name="",
            declared_name=name,
            configuration_selection=pod_field("ОбъектВыборкиМетаданные"),
            format_selection=pod_field("ОбъектВыборкиФормат"),
            clear_data=pod_field("ОчисткаДанных"),
            events=tuple(builder.events),
            used_pko=tuple(builder.used),
        )

    def pkpd_field(key: str) -> Any:
        expression = builder.fields.get(key)
        if expression is None:
            return ValueField()
        if key == "ТипДанных":
            return ValueField("expression", expression, (expression,))
        return ValueField("literal", expression.literal_value, (expression,))

    return PredefinedRule(
        **common,
        declared_name=name,
        configuration_type=pkpd_field("ТипДанных"),
        format_type=pkpd_field("ТипXDTO"),
        mappings=tuple(builder.mappings),
    )


def _helper_trust(
    routines: list[Routine], source: SourceFile, version: int | None
) -> dict[str, bool]:
    found: dict[str, bool] = {}
    for routine in routines:
        if routine.name.casefold() not in ("добавитьпкс", "добавитьпктч"):
            continue
        actual = normalized(
            tuple(token for token in tokenize(routine.raw_text) if token.kind != "comment")
        )
        versions = (version,) if version in (1, 2, 3) else (1, 2, 3)
        ok = False
        for item in versions:
            forms = helper_forms(routine.name, item)
            if actual in {normalized(tokenize(text)) for text in forms}:
                ok = True
        found[routine.name.casefold()] = ok
        _ = source
    return found


def read_extension_text(
    text: str,
    *,
    layer: LayerDescriptor,
    metadata_kind: str = "CommonModule",
    metadata_name: str = "",
    version: int | None = 2,
    helpers: frozenset[str] | None = None,
    targets: dict[str, Routine] | None = None,
    path: str = "<memory>",
    file_id: str = "extension",
    plan_name: str | None = None,
) -> ExtensionReading:
    """Разбирает текст модуля расширения. Базовый документ не требуется и не меняется."""
    raw = text.encode("utf-8")
    bom = text.startswith("\ufeff")
    source = source_from_text(text.removeprefix("\ufeff"), file_id, path, bom, raw)
    return _read_module(
        source,
        layer,
        metadata_kind,
        metadata_name or layer.name,
        version,
        set(helpers or ()),
        targets or {},
        plan_name,
    )


def read_extension_file(
    path: Path,
    *,
    layer: LayerDescriptor,
    metadata_kind: str,
    metadata_name: str,
    version: int | None,
    helpers: frozenset[str],
    targets: dict[str, Routine],
    plan_name: str | None = None,
    file_id: str | None = None,
) -> ExtensionReading:
    source = load_source(path, file_id or str(path))
    return _read_module(
        source, layer, metadata_kind, metadata_name, version, set(helpers), targets, plan_name
    )


def _read_module(
    source: SourceFile,
    layer: LayerDescriptor,
    metadata_kind: str,
    metadata_name: str,
    version: int | None,
    helpers: set[str],
    targets: dict[str, Routine],
    plan_name: str | None,
) -> ExtensionReading:
    origin = _origin(
        layer, metadata_kind, metadata_name, source, source.span(0, min(1, len(source.text))), None
    )
    if len(source.text.encode("utf-8")) > MAX_FILE_BYTES:
        return _empty_reading(
            layer,
            source,
            LayerSkip("resource_limit", origin, ("manager",), source.path, ("manager",)),
        )
    try:
        parsed, warnings = _routine_trees(source)
    except EdFormatError as error:
        return _empty_reading(
            layer,
            source,
            LayerSkip("bsl_syntax", origin, ("manager",), str(error), ("manager",)),
        )
    if len(parsed) > MAX_METHODS:
        return _empty_reading(
            layer,
            source,
            LayerSkip("resource_limit", origin, ("manager",), source.path, ("manager",)),
        )
    routines = [item[0] for item in parsed]
    trust = {name: True for name in helpers}
    trust.update(_helper_trust(routines, source, version))
    by_name = {routine.name.casefold(): (routine, tree) for routine, _, tree in parsed}
    walker = _Walker(
        source, layer, metadata_kind, metadata_name, by_name, version, trust, plan_name
    )
    hooks: list[Hook] = []
    try:
        hooks = _collect_hooks(parsed, source, layer, metadata_kind, metadata_name, targets, walker)
    except _Abort as aborted:
        return _empty_reading(
            layer,
            source,
            LayerSkip(aborted.reason, aborted.origin, ("manager",), aborted.reason, ("manager",)),
        )
    marks = list(walker.marks)
    for _routine, header, _tree in parsed:
        marks.append((header.span, Classification.DECLARATIVE))
    for code, span in warnings:
        _ = code
        marks.append((span, Classification.UNKNOWN))
    tokens = tokenize(source.text)
    coverage = build_coverage(source, tokens, marks, (routine.span for routine in routines))
    seen: dict[str, int] = {}
    for hook in hooks:
        seen[hook.target_name.casefold()] = seen.get(hook.target_name.casefold(), 0) + 1
    skips = list(walker.skips)
    for hook in hooks:
        if seen[hook.target_name.casefold()] > 1:
            skips.append(
                LayerSkip(
                    "duplicate_hook",
                    hook.origin,
                    (hook.target_name,),
                    hook.routine.raw_text,
                    (_filler_collection(hook.target_name) or "dispatcher",),
                    certainty="conditional",
                )
            )
    return ExtensionReading(
        layer.id,
        tuple(hooks),
        tuple(walker.operations),
        tuple(routines),
        tuple(skips),
        source,
        coverage,
    )


def _empty_reading(layer: LayerDescriptor, source: SourceFile, skip: LayerSkip) -> ExtensionReading:
    tokens = ()
    try:
        tokens = tokenize(source.text)
    except EdFormatError:
        tokens = ()
    coverage = build_coverage(source, tokens, (), ())
    return ExtensionReading(layer.id, (), (), (), (skip,), source, coverage)


def _collect_hooks(
    parsed: list[tuple[Routine, Statement, list[_Node]]],
    source: SourceFile,
    layer: LayerDescriptor,
    metadata_kind: str,
    metadata_name: str,
    targets: dict[str, Routine],
    walker: _Walker,
) -> list[Hook]:
    lexical = lex(source)
    directives: list[tuple[str, str, SourceSpan]] = []
    hooks: list[Hook] = []
    routine_at = {header.span.char_start: (routine, tree) for routine, header, tree in parsed}
    pending: list[tuple[str, str, SourceSpan]] = []
    for statement in lexical.statements:
        if statement.tokens[0].kind == "directive" and statement.tokens[0].value.startswith("&"):
            parsed_hook = parse_annotation(statement.tokens[0].value)
            if parsed_hook is None:
                if unrecognized_annotation(statement.tokens[0].value):
                    walker.skip(
                        "unrecognized_annotation",
                        statement.span,
                        None,
                        ("manager",),
                        statement.raw_text,
                        ("manager",),
                    )
                    walker.op(
                        OperationKind.UNKNOWN,
                        "manager",
                        (),
                        None,
                        statement.span,
                        "",
                        (),
                        None,
                        "unknown",
                        Footprint("manager", "manager"),
                    )
                continue
            pending.append((parsed_hook[0], parsed_hook[1], statement.span))
            continue
        if statement.head in ("процедура", "функция") and pending:
            routine_tree = routine_at.get(statement.span.char_start)
            if routine_tree is None:
                pending.clear()
                continue
            routine, tree = routine_tree
            kind, target, span = pending[-1]
            pending.clear()
            hook = _bind_hook(
                kind,
                target,
                span,
                routine,
                tree,
                source,
                layer,
                metadata_kind,
                metadata_name,
                targets,
                walker,
            )
            if hook is not None:
                hooks.append(hook)
            continue
        if statement.head in ("процедура", "функция"):
            pending.clear()
            continue
        pending.clear()
    _ = directives
    return hooks


def _bind_hook(
    kind: str,
    target: str,
    span: SourceSpan,
    routine: Routine,
    tree: list[_Node],
    source: SourceFile,
    layer: LayerDescriptor,
    metadata_kind: str,
    metadata_name: str,
    targets: dict[str, Routine],
    walker: _Walker,
) -> Hook | None:
    origin = _origin(layer, metadata_kind, metadata_name, source, span, routine.name)
    hook_id = f"{layer.id}:hook:{routine.span.char_start}"
    folded = target.casefold()
    known = targets.get(folded)
    variants = _FILLER_VARIANTS.get(folded) or (
        (_ROUTE_CALLBACKS[folded],) if folded in _ROUTE_CALLBACKS else ()
    )
    if folded in _DISPATCH_NAMES:
        variants = _FILLER_VARIANTS[folded]
    applicability = Applicability.KNOWN
    target_origin = None
    if known is not None:
        expected = (
            (
                known.routine_kind,
                tuple(param.name or "" for param in known.parameters),
            ),
        )
        by_value = tuple(param.by_value for param in known.parameters)
        if not _signature_ok(routine, expected, by_value):
            applicability = Applicability.INVALID
        target_origin = _origin(layer, metadata_kind, metadata_name, source, known.span, known.name)
    elif variants:
        if not _signature_ok(routine, variants):
            applicability = Applicability.INVALID
    elif known is None and folded not in _FILLER_NAMES | _DISPATCH_NAMES | set(_ROUTE_CALLBACKS):
        applicability = Applicability.INVALID
        walker.skip("missing_target", span, routine.name, (target,), target, ("hook",), hook_id)
    if (
        known is not None
        and known.routine_kind == "function"
        and routine.routine_kind != "function"
    ):
        applicability = Applicability.INVALID
        walker.skip(
            "manager_signature",
            span,
            routine.name,
            (target,),
            routine.raw_text,
            ("dispatcher",),
            hook_id,
        )
    if applicability == Applicability.INVALID and folded in _FILLER_NAMES | _DISPATCH_NAMES | set(
        _ROUTE_CALLBACKS
    ):
        walker.skip(
            "manager_signature",
            span,
            routine.name,
            (target,),
            routine.raw_text,
            (_filler_collection(target) or "dispatcher",),
            hook_id,
        )
    continuation = Continuation.UNKNOWN
    if applicability == Applicability.KNOWN and "handler" in (
        known.roles if known else frozenset()
    ):
        walker.marks.append((routine.body_span, Classification.OPAQUE_CODE))
        continuation = Continuation.ONCE
    elif applicability == Applicability.KNOWN:
        continuation = walker.walk_hook(hook_id, routine, tree, kind, target)
    elif applicability == Applicability.INVALID and folded in _FILLER_NAMES:
        continuation = Continuation.UNKNOWN
    return Hook(hook_id, kind, target, routine, target_origin, continuation, applicability, origin)


def filler_calls(source: SourceFile) -> tuple[FillerCall, ...]:
    """Вызовы локальных заполнителей из точек входа базового менеджера."""
    try:
        parsed, _warnings = _routine_trees(source)
    except EdFormatError:
        return ()
    calls: list[FillerCall] = []
    module_variables, local_variables = _variable_declarations(source)
    for routine, _header, tree in parsed:
        if routine.name.casefold() not in _FILLER_NAMES:
            continue
        env = _parameter_env(routine.name, routine, None)
        for name, collection in _collection_parameters(routine, env).items():
            env[f"byref:{name}"] = _Bind("marker", collection=collection)
        shadowed = local_variables.get(routine.name.casefold(), set()) | {
            (param.name or "").casefold() for param in routine.parameters
        }
        for name in module_variables - shadowed:
            env[f"module:{name}"] = _Bind("marker")
        _collect_calls(tree, env, _true(), calls, source, _filler_collection(routine.name))
    return tuple(calls)


def module_routines(source: SourceFile) -> tuple[Routine, ...]:
    """Цели перехватов берутся из заимствованного модуля, включая модуль маршрута."""
    return tuple(routine for routine, _header, _tree in _routine_trees(source)[0])


def _base_mutation(statement: Statement, env: dict[str, _Bind], source: SourceFile) -> bool:
    """Заполнитель базы тоже должен доказать отсутствие чужой мутации своей таблицы."""
    assignment = _assignment(statement)
    if assignment and "byref:" + assignment[0].casefold() in env:
        return True
    if _dynamic_code(statement) and any(bind.collection for bind in env.values()):
        return True
    touched = any(
        token.kind == "identifier"
        and token.folded in env
        and env[token.folded].collection
        and not _scalar_read(statement.tokens, index, env)
        for index, token in enumerate(statement.tokens)
    )
    if not touched:
        return False
    if statement.head in ("если", "иначеесли") and _Walker._column_check(
        _condition_tokens(statement), env
    ):
        return False
    if assignment:
        left, right = assignment
        if "module:" + left.casefold() in env:
            return True
        if "." not in left and len(right) == 1 and right[0].folded in env:
            env[left.casefold()] = env[right[0].folded]
            return False
        call = _call(right)
        if call and "." not in left:
            owner, _, method = call[0].rpartition(".")
            bind = env.get(owner.casefold())
            if (
                bind
                and bind.tag == "collection"
                and method.casefold() == "добавить"
                and not call[1]
            ):
                env[left.casefold()] = _Bind("base_rule", collection=bind.collection)
                return False
        owner, _, field = left.rpartition(".")
        bind = env.get(owner.casefold())
        if bind and bind.tag == "base_rule":
            # ПКПД строятся в самом заполнителе: те же поля формы генератора,
            # что использует _builder_assign (ed/forms.py: PKPD_FIELDS).
            table = {"pkpd": _PKPD_SET, "pod": _POD_SET, "pko": _PKO_SET}.get(bind.collection, {})
            if field.casefold() in table:
                expression = _expr(source, right, statement.span)
                return not (expression.literal_type or expression.reference_parts)
            if field.casefold() in (
                "конвертациизначенийприотправке",
                "конвертациизначенийприполучении",
            ):
                return not (len(right) == 1 and right[0].kind == "identifier")
        return True
    call = _call(statement.tokens)
    if not call:
        return True
    if _Walker._benign_columns(call, env):
        return False
    if call[0].casefold().startswith(("добавитьпко_", "добавитьпод_", "добавитьпкпд_")):
        return any(any(token.value == "(" for token in arg) for arg in call[1])
    owner, _, method = call[0].rpartition(".")
    bind = env.get(owner.casefold())
    if bind and bind.collection == "parameters" and method.casefold() in ("вставить", "удалить"):
        return not call[1] or not all(
            len(arg) == 1
            and (
                arg[0].kind in ("string", "number")
                or arg[0].folded in ("истина", "ложь", "неопределено")
            )
            for arg in call[1]
        )
    return True


def _collect_calls(
    nodes: Sequence[_Node],
    env: dict[str, _Bind],
    entry: Pred,
    calls: list[FillerCall],
    source: SourceFile,
    collection: str,
    properties: dict[int, Pred] | None = None,
    evaluations: list[int] | None = None,
) -> Pred:
    """Та же формула пути, что у перехватчика. Вложенный вызов не доказывает правило."""
    exited = _false()
    current = entry
    evaluations = [0] if evaluations is None else evaluations
    for node_index, node in enumerate(nodes):
        if _is_false(current):
            break
        choice = next(((name, bind) for name, bind in env.items() if bind.paths), None)
        if choice:
            name, binding = choice
            outputs = []
            for path, value in binding.paths:
                child = dict(env)
                child[name] = value
                branch_entry = _formula_and(current, path)
                branch_exit = _collect_calls(
                    nodes[node_index:],
                    child,
                    branch_entry,
                    calls,
                    source,
                    collection,
                    properties,
                    evaluations,
                )
                exited = _formula_or(exited, branch_exit)
                outputs.append((_formula_and(branch_entry, _formula_not(branch_exit)), child))
            _join_bindings(env, outputs)
            return exited
        if isinstance(node, (_Loop, _Try)):
            if _dynamic_code(node.statement) and _base_mutation(node.statement, env, source):
                calls.append(
                    FillerCall("", collection, current, node.statement.span, "unknown_call")
                )
            opaque_entry = Pred(
                "and",
                kids=(
                    current,
                    Pred.atom(
                        "opaque_path", f"{source.file_id}:{node.statement.span.char_start}:control"
                    ),
                ),
            )
            _collect_calls(
                node.body,
                dict(env),
                opaque_entry,
                calls,
                source,
                collection,
                properties,
                evaluations,
            )
            if isinstance(node, _Try):
                _collect_calls(
                    node.handler,
                    dict(env),
                    opaque_entry,
                    calls,
                    source,
                    collection,
                    properties,
                    evaluations,
                )
            _invalidate_after_opaque([node], env)
            if _returns(node.body):
                return Pred.opaque()
            continue
        if isinstance(node, _If):
            branch_exit = _collect_if(
                node, env, current, calls, source, collection, properties, evaluations
            )
            exited = _formula_or(exited, branch_exit)
            current = _formula_and(current, _formula_not(branch_exit))
            continue
        if not isinstance(node, _Stmt):
            continue
        if _raises(node.statement):
            calls.append(FillerCall("", collection, current, node.statement.span, "filler_error"))
            return _formula_or(exited, current)
        if node.statement.head == "возврат":
            return _formula_or(exited, current)
        if node.statement.head == "перейти":
            return Pred.opaque()
        _note_direction(node.statement, env)
        if _base_mutation(node.statement, env, source):
            calls.append(FillerCall("", collection, current, node.statement.span, "unknown_call"))
        if properties is not None:
            call = _call(node.statement.tokens)
            if call and call[0].casefold() in ("добавитьпкс", "добавитьпктч"):
                offset = node.statement.span.char_start
                properties[offset] = _formula_or(properties.get(offset, _false()), current)
        if _is_opaque(current):
            _note_call(node.statement, Pred.opaque(), calls, collection)
            continue
        _note_call(node.statement, current, calls, collection)
    return exited


def _collect_if(
    node, env, entry, calls, source, collection, properties=None, evaluations=None
) -> Pred:
    exited, negated = _false(), _true()
    joined = []
    evaluations = [0] if evaluations is None else evaluations
    for branch in node.branches:
        preproc = _preproc_branch(branch)
        if preproc is False:
            continue
        if preproc is True:
            return _collect_calls(
                branch.body, env, entry, calls, source, collection, properties, evaluations
            )
        boundary = _formula_and(entry, negated)
        if _is_false(boundary):
            continue
        if branch.condition is None:
            guard = negated
        else:
            evaluations[0] += 1
            parsed, reason = _path_condition(
                branch.condition, env, branch.statement.span, str(evaluations[0])
            )
            if reason and _base_mutation(branch.statement, env, source):
                calls.append(FillerCall("", collection, boundary, branch.statement.span, reason))
            guard = _formula_and(parsed, negated)
            negated = _formula_and(negated, _formula_not(parsed))
        child = dict(env)
        path = _formula_and(entry, guard)
        body_exit = _collect_calls(
            branch.body, child, path, calls, source, collection, properties, evaluations
        )
        joined.append((_formula_and(path, _formula_not(body_exit)), child))
        exited = _formula_or(exited, body_exit)
    if not any(branch.condition is None for branch in node.branches):
        joined.append((_formula_and(entry, negated), dict(env)))
    _join_bindings(env, joined)
    return exited


def _note_direction(statement: Statement, env: dict[str, _Bind]) -> None:
    assignment = _assignment(statement)
    if not assignment:
        return
    left, right = assignment
    if "." not in left:
        if len(right) == 1 and right[0].folded in env:
            env[left.casefold()] = env[right[0].folded]
            return
        previous = env.get(left.casefold())
        if previous and previous.tag in ("direction", "components", "headers"):
            if len(right) == 1 and right[0].folded in ("истина", "ложь"):
                env[left.casefold()] = _Bind("boolean", literal=right[0].folded)
                return
            if len(right) == 1 and right[0].kind == "string":
                env[left.casefold()] = _Bind("literal", literal=right[0].value)
                return
            env[left.casefold()] = _Bind("unknown")
        if len(right) == 3 and right[1].value == "." and right[2].folded == "направлениеобмена":
            host = env.get(right[0].folded)
            if host and host.tag == "components":
                env[left.casefold()] = _Bind("direction")


def _note_call(statement: Statement, pred: Pred, calls: list[FillerCall], collection: str) -> None:
    call = _call(statement.tokens)
    if not call or "." in call[0]:
        return
    head = call[0].casefold()
    if head.startswith("добавитьпко_"):
        calls.append(FillerCall(call[0], "pko", pred, statement.span))
    elif head.startswith("добавитьпод_"):
        calls.append(FillerCall(call[0], "pod", pred, statement.span))
    _ = collection


def property_conditions(source: SourceFile) -> dict[int, Pred]:
    """Условие каждой ``ДобавитьПКС`` базового модуля: ключ — начало оператора."""
    try:
        parsed, _warnings = _routine_trees(source)
    except EdFormatError:
        return {}
    found: dict[int, Pred] = {}

    for routine, _header, tree in parsed:
        if not _has_property_call(tree):
            continue
        _collect_calls(
            tree, _parameter_env(routine.name, routine, None), _true(), [], source, "", found
        )
    return found


def _has_property_call(nodes: Sequence[_Node]) -> bool:
    """Условия нужны только процедурам с действительным вызовом добавления ПКС."""
    for node in nodes:
        if isinstance(node, _Stmt):
            call = _call(node.statement.tokens)
            if call and call[0].casefold() in ("добавитьпкс", "добавитьпктч"):
                return True
        elif isinstance(node, _If):
            if any(_has_property_call(branch.body) for branch in node.branches):
                return True
        elif _has_property_call(node.body) or (
            isinstance(node, _Try) and _has_property_call(node.handler)
        ):
            return True
    return False


def recover_parameters(source: SourceFile) -> tuple[Parameter, ...]:
    """Двухаргументная вставка параметра, которую базовый читатель оставляет неизвестной."""
    try:
        parsed, _warnings = _routine_trees(source)
    except EdFormatError:
        return ()
    result: list[Parameter] = []
    for routine, _header, tree in parsed:
        if routine.name.casefold() != "заполнитьпараметрыконвертации":
            continue
        _recover_parameter_nodes(tree, source, result)
    return tuple(result)


def _recover_parameter_nodes(
    nodes: Sequence[_Node], source: SourceFile, result: list[Parameter]
) -> None:
    for node in nodes:
        if isinstance(node, _If):
            for branch in node.branches:
                _recover_parameter_nodes(branch.body, source, result)
        elif isinstance(node, _Stmt):
            call = _call(node.statement.tokens)
            if not call or call[0].casefold() != "параметрыконвертации.вставить":
                continue
            if not call[1] or len(call[1][0]) != 1 or call[1][0][0].kind != "string":
                continue
            name = call[1][0][0].value
            default = None
            source_kind = "implicit"
            if len(call[1]) > 1:
                default = _expr(source, call[1][1], node.statement.span)
                source_kind = "explicit"
            result.append(
                Parameter(
                    entity_id=f"{source.file_id}:parameter:{node.statement.span.char_start}",
                    kind="parameter",
                    name=name,
                    span=node.statement.span,
                    raw_text=node.statement.raw_text,
                    default=default,
                    default_source=source_kind,
                )
            )


def dispatcher_throws(source: SourceFile) -> bool:
    """Истина, если несовпавшее имя в базовом диспетчере доказуемо вызывает исключение."""
    try:
        parsed, _warnings = _routine_trees(source)
    except EdFormatError:
        return False
    for routine, _header, tree in parsed:
        if routine.name.casefold() not in _DISPATCH_NAMES:
            continue
        for node in tree:
            if isinstance(node, _If) and _branch_throws(node.otherwise):
                return True
    return False


def _branch_throws(nodes: Sequence[_Node]) -> bool:
    for node in nodes:
        if isinstance(node, _Stmt):
            call = _call(node.statement.tokens)
            thrown = node.statement.head == "вызватьисключение" or (
                call is not None and call[0].casefold() == "вызватьисключение"
            )
            if thrown:
                return True
        if isinstance(node, _If) and any(_branch_throws(branch.body) for branch in node.branches):
            return True
    return False


def base_map(
    source: SourceFile,
    procedure: str,
    role: str,
    plan_name: str | None,
    layer: LayerDescriptor,
    metadata_kind: str,
    metadata_name: str,
) -> tuple[MapEntry, ...]:
    """Литеральные вставки карты в теле процедуры основной выгрузки."""
    try:
        parsed, _warnings = _routine_trees(source)
    except EdFormatError:
        return ()
    for routine, _header, tree in parsed:
        if routine.name.casefold() != procedure.casefold():
            continue
        env = _parameter_env(procedure, routine, plan_name)
        entries: list[MapEntry] = []
        _scan_map(
            tree,
            env,
            source,
            role,
            plan_name,
            layer,
            metadata_kind,
            metadata_name,
            routine.name,
            entries,
        )
        return tuple(entries)
    return ()


def _scan_map(
    nodes: Sequence[_Node],
    env: dict[str, _Bind],
    source: SourceFile,
    role: str,
    plan_name: str | None,
    layer: LayerDescriptor,
    metadata_kind: str,
    metadata_name: str,
    procedure: str,
    entries: list[MapEntry],
) -> None:
    for node in nodes:
        if isinstance(node, _If):
            for branch in node.branches:
                _scan_map(
                    branch.body,
                    env,
                    source,
                    role,
                    plan_name,
                    layer,
                    metadata_kind,
                    metadata_name,
                    procedure,
                    entries,
                )
            continue
        if not isinstance(node, _Stmt):
            continue
        statement = node.statement
        assignment = _assignment(statement)
        call = _call(assignment[1] if assignment else statement.tokens)
        if assignment:
            left, right = assignment
            if any(token.folded == "соответствие" for token in right) and any(
                token.folded == "новый" for token in right
            ):
                env[left.casefold()] = _Bind("map", map_role="pending", plan_name=plan_name)
            joined = _join(right).casefold()
            source_bind = env.get(joined)
            if (
                left.casefold().endswith("версииформатаобмена")
                and source_bind
                and source_bind.tag == "map"
            ):
                source_bind.map_role = role or "plan"
                for entry in entries:
                    if entry.state == "pending":
                        pass
                source_bind.literal = "published"
        if not call:
            continue
        owner, _, method = call[0].rpartition(".")
        if method.casefold() != "вставить" or len(call[1]) < 2:
            continue
        key, module = call[1][0], call[1][1]
        if (
            len(key) != 1
            or key[0].kind != "string"
            or len(module) != 1
            or module[0].kind != "identifier"
        ):
            continue
        bind = env.get(owner.casefold())
        entry_role = role
        if bind and bind.tag == "map":
            entry_role = "plan" if role == "plan" else bind.map_role or role
        elif owner.casefold().endswith("версииформатаобмена"):
            entry_role = "plan"
        if entry_role in ("", "pending"):
            continue
        origin = _origin(layer, metadata_kind, metadata_name, source, statement.span, procedure)
        entries.append(
            MapEntry(
                "without_node" if entry_role == "without_node" else "plan",
                None if entry_role == "without_node" else plan_name,
                key[0].value,
                module[0].value,
                "effective",
                origin,
            )
        )


def handled_literals(reading: ExtensionReading, hook_id: str) -> tuple[str, ...]:
    return tuple(
        operation.target_ref
        for operation in reading.operations
        if operation.hook_id == hook_id and operation.kind == OperationKind.DISPATCH
    )
