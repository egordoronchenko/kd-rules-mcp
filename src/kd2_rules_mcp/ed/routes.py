"""Ограниченное чтение маршрутов EnterpriseData из XML-выгрузки конфигурации.

Грамматика — §2 спецификации маршрутов: литеральные вставки, прямой вызов,
ModuleAlias и StaticSubsystem. Всё прочее остаётся skipped, BSL не исполняется.
"""

from __future__ import annotations

import hashlib
from bisect import bisect_left
from collections.abc import Callable, Iterable, Mapping, Sequence
from copy import copy
from dataclasses import dataclass, field
from pathlib import Path
from stat import S_ISREG

from lxml import etree as ET

from .errors import EdFormatError, EdReadError, EdResourceLimitError
from .forms import MANAGER_VERSIONS, VERSION_ROUTINE
from .lexer import Lexed, Statement, Token, lex, normalized, split_arguments, tokenize
from .model import EdDocument, ParseStatus, SourceFile, SourceSpan
from .reader import read_manager
from .route_model import (
    ConditionKind,
    ConditionValue,
    EntryState,
    FormatExtension,
    ManagerInfo,
    PackageInfo,
    PlanRoute,
    RegistrationProfile,
    RouteCondition,
    RouteEntry,
    RouteProfile,
    RouteReading,
    RouteSkip,
    RouteSource,
    RouteStatus,
    VariantInfo,
)
from .schema.errors import EdSchemaFormatError, EdSchemaReadError
from .schema.xdto import metadata as package_metadata

READER_VERSION = "1"
MD_NS = "http://v8.1c.ru/8.3/MDClasses"
ED_BASE = "http://v8.1c.ru/edi/edi_stnd/EnterpriseData"
MAX_FILE_BYTES = 32 * 1024 * 1024
MAX_TOTAL_BSL = 256 * 1024 * 1024
MAX_BSL_FILES = 4096
MAX_XML_DEPTH = 128
MAX_EDGES = 4
MAX_VISITED = 64
MAX_CONDITIONS = 32
MAX_CALLS = 256
MAX_ENTRIES = 4096
_OVERRIDE = "ОбменДаннымиПереопределяемый"
_DISABLED = "ПриОпределенииОтключенныхПодсистем"
_VERSIONS = "ПриПолученииДоступныхВерсийФормата"
_EXTENSIONS = "ПриПолученииДоступныхРасширенийФормата"
_SETTINGS = "ПриПолученииНастроек"
_VARIANTS = "ПриПолученииВариантовНастроекОбмена"
_DESCRIBE = "ПриПолученииОписанияВариантаНастройки"
_COMMON = "ОбщегоНазначения"
_NODE_REASON = (
    "Версия конкретного узла неизвестна; при пустом значении "
    "исполнитель выбирает минимальную версию карты (XDTO:3618)."
)
_EXTENSION_REASON = "Расширения конфигурации не учитывались; слой base не равен живой базе."
_KNOWN_SETTINGS = frozenset(
    {
        "этопланобменаxdto",
        "форматобмена",
        "версииформатаобмена",
        "правиларегистрациивменеджере",
        "имяменеджерарегистрации",
        "расширенияформатаобмена",
    }
)
_KNOWN_ALGORITHMS = frozenset(
    {
        "приполучениивариантовнастроекобмена",
        "приполученииописаниявариантанастройки",
    }
)


class _Exit(Exception):
    """Возврат или исключение завершает только текущую ветку процедуры."""


class _Unclosed(Exception):
    """В теле нет парного КонецЕсли, КонецЦикла или КонецПопытки."""


@dataclass
class _Live:
    ident: int
    key_raw: str
    key: str
    value: str | None
    value_kind: str
    source: RouteSource
    conditions: tuple[RouteCondition, ...]
    state: str


@dataclass
class _Map:
    role: str | None
    entries: list[_Live] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class _Alias:
    module_name: str
    span: SourceSpan


@dataclass
class _Settings:
    values: dict[str, object]
    algorithms: dict[str, object]
    retained: list[_Map]


@dataclass(frozen=True, slots=True)
class _Param:
    name: str
    by_value: bool
    default: tuple[Token, ...]


@dataclass
class _Stmt:
    statement: Statement


@dataclass
class _If:
    clauses: list[tuple[tuple[Token, ...], list[_Node], Statement]]
    else_body: list[_Node]
    else_statement: Statement | None
    preprocessor: bool = False


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
class _Routine:
    name: str
    kind: str
    params: tuple[_Param, ...]
    nodes: list[_Node]
    statements: tuple[Statement, ...]
    tokens: tuple[Token, ...]
    file: SourceFile
    header: SourceSpan
    closed: bool = True


@dataclass
class _Module:
    file: SourceFile
    lexical: Lexed
    routines: dict[str, _Routine]


@dataclass
class _Env:
    locals: dict[str, object]
    settings: _Settings | None
    maps: list[_Map]


@dataclass
class _Run:
    """Один корень: настройки плана или глобальный обработчик."""

    depth: int = 0
    calls: int = 0
    partial: bool = False
    aborted: bool = False
    speculative: bool = False
    collect_literals: bool = False
    stack: list[tuple[str, str]] = field(default_factory=list)
    chain: list[SourceSpan] = field(default_factory=list)
    conditions: list[RouteCondition] = field(default_factory=list)
    procedure: str = ""
    file: SourceFile | None = None
    seen: set[tuple[str, str]] = field(default_factory=set)
    inserts: set[tuple[str, int, int, str, str]] = field(default_factory=set)
    tail_conditions: list[RouteCondition] = field(default_factory=list)
    direct_calls: int = 0
    module_aliases: int = 0
    reached_calls: int = 0
    skips: list[RouteSkip] = field(default_factory=list)
    checks: list[tuple[str, str]] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class _Inventory:
    configuration_name: str | None
    plans: tuple[str, ...]
    modules: tuple[str, ...]
    subsystems: tuple[str, ...]
    packages: tuple[str, ...]
    module_names: dict[str, str]


@dataclass(frozen=True, slots=True)
class RouteFileObservation:
    """Зависимость маршрутов: абсолютный путь, SHA-256 и (размер, mtime_ns).

    stamp=None означает отсутствие файла. sha256=None при наличии stamp — проверку
    наличия без чтения содержимого (например, макета регистрации или Package.bin).
    Повторное сообщение о том же пути может дополнить проверку наличия хешем чтения.
    """

    path: Path
    sha256: str | None
    stamp: tuple[int, int] | None


def read_routes(
    root: Path,
    *,
    documents: Mapping[Path, EdDocument] | None = None,
    observe: Callable[[RouteFileObservation], None] | None = None,
) -> RouteProfile:
    """Читает профиль маршрутов основной выгрузки с Configuration.xml.

    documents заменяет повторный разбор менеджера, только если SHA-256 его файла
    совпадает со снимком. observe получает зависимости, включая отсутствующие файлы;
    содержимое файлов, у которых проверяется только наличие, не читается.
    """
    root = Path(root)
    if not root.is_dir() or not (root / "Configuration.xml").is_file():
        if observe is not None:
            observe(RouteFileObservation((root / "Configuration.xml").resolve(), None, None))
        raise EdFormatError("Нет Configuration.xml: это не полная XML-выгрузка конфигурации")
    return _Reader(root.resolve(), documents=documents, observe=observe).build()


def compare_versions(left: str, right: str) -> int | None:
    """Сравнение версий формата как у исполнителя: первые два числа, beta меньше любой.

    None — форма не из двух или трёх частей (XDTO:9163, XDTO:9176, XDTO:9182).
    Равенство нуля не сливает разные строки ключей.
    """
    if left.strip() == right.strip():
        return 0
    first = _version_parts(left)
    second = _version_parts(right)
    if first is None or second is None:
        return None
    if len(first) == 3 and first[2] == "beta":
        return -1
    if len(second) == 3 and second[2] == "beta":
        return 1
    for index in (0, 1):
        delta = int(first[index]) - int(second[index])
        if delta:
            return delta
    return 0


def _ascii_digits(part: str) -> bool:
    """Только цифры ASCII: `isdigit` принимает «²», а `int` на ней падает."""
    return bool(part) and part.isascii() and part.isdigit()


def _version_parts(value: str) -> tuple[str, ...] | None:
    parts = tuple(piece.strip() for piece in value.strip().split("."))
    if len(parts) not in (2, 3):
        return None
    if not _ascii_digits(parts[0]) or not _ascii_digits(parts[1]):
        return None
    if len(parts) == 3 and parts[2] != "beta" and not _ascii_digits(parts[2]):
        return None
    return parts


def _local(tag: object) -> str:
    if not isinstance(tag, str):
        return ""
    return tag.rsplit("}", 1)[-1]


def _bare(tokens: Sequence[Token]) -> tuple[Token, ...]:
    if tokens and tokens[-1].value == ";":
        return tuple(tokens[:-1])
    return tuple(tokens)


def _text_of(source: SourceFile, tokens: Sequence[Token]) -> str:
    if not tokens:
        return ""
    return source.text[tokens[0].start : tokens[-1].end]


class _Reader:
    def __init__(
        self,
        root: Path,
        *,
        documents: Mapping[Path, EdDocument] | None = None,
        observe: Callable[[RouteFileObservation], None] | None = None,
    ) -> None:
        self.root = root
        self.documents = {Path(path).resolve(): doc for path, doc in (documents or {}).items()}
        self.observe = observe
        self.stamps: dict[Path, tuple[int, int] | None] = {}
        self.files: dict[str, str] = {}
        self.bsl_bytes = 0
        self.bsl_files = 0
        self.modules: dict[str, _Module] = {}
        self.skips: list[RouteSkip] = []
        self.variant_skips: list[RouteSkip] = []
        self.map_skip_count = 0
        self.direct_calls_plan = 0
        self.calls_without_node = 0
        self.module_aliases = 0
        self.subsystem_checks: list[tuple[str, str]] = []
        self.variant_string = 0
        self.variant_const = 0
        self.ed_assignments = 0
        self.insert_sites: set[tuple[str, int, int, str, str]] = set()
        self.unreadable: set[str] = set()
        self._idents = 0
        self._callback: bool | None = None
        self._callback_noted = False
        self.inventory = self._inventory()

    def _is_file(self, path: Path) -> bool:
        if self.observe is None:
            return path.is_file()
        try:
            stat = path.stat()
        except (FileNotFoundError, NotADirectoryError):
            stamp = None
            present = False
        else:
            stamp = (stat.st_size, stat.st_mtime_ns)
            present = S_ISREG(stat.st_mode)
        if path not in self.stamps or self.stamps[path] != stamp:
            self.observe(RouteFileObservation(path, None, stamp))
        self.stamps[path] = stamp
        return present

    def _observed(self, path: Path, sha256: str) -> None:
        if self.observe is not None:
            if path not in self.stamps:
                self._is_file(path)
            self.observe(RouteFileObservation(path, sha256, self.stamps[path]))

    def _observe_unparsed(self, path: Path) -> None:
        """Неуспешный разбор внешним читателем тоже зависит от прочитанных байтов."""
        if self.observe is None:
            return
        try:
            with path.open("rb") as stream:
                raw = stream.read(MAX_FILE_BYTES + 1)
        except OSError:
            return  # Состояние недоступного файла уже передано при проверке наличия.
        if len(raw) <= MAX_FILE_BYTES:
            self._observed(path, hashlib.sha256(raw).hexdigest())

    def build(self) -> RouteProfile:
        plans = tuple(self._read_plan(name) for name in self.inventory.plans)
        without, extensions, without_status = self._read_global()
        packages = self._read_packages()
        self._note_ambiguous_packages(packages)
        referenced = self._referenced_managers(plans, without)
        managers = tuple(self._manager(name) for name in sorted(referenced))
        missing_body = any(item.metadata_exists and not item.source_exists for item in managers)
        route_partial = without_status == "partial" or any(
            plan.status == "partial" for plan in plans
        )
        status: RouteStatus = "partial" if missing_body or route_partial else "complete"
        skipped = self._assemble_skips()
        reading = self._reading(plans, without, packages)
        fingerprint = hashlib.sha256(
            "\n".join(f"{path}\t{digest}" for path, digest in sorted(self.files.items())).encode()
        ).hexdigest()
        identity = hashlib.sha256(
            f"{READER_VERSION}\n{self.root}\n{fingerprint}".encode()
        ).hexdigest()
        return RouteProfile(
            profile_id=f"route-{identity[:24]}",
            root=str(self.root),
            project=None,
            configuration=None,
            configuration_name=self.inventory.configuration_name,
            sources_fingerprint=fingerprint,
            plans=plans,
            without_node_entries=without,
            format_extensions=extensions,
            packages=packages,
            managers=managers,
            skipped=skipped,
            status=status,
            reading=reading,
            declared_modules=self.inventory.modules,
            declared_subsystems=self.inventory.subsystems,
            without_node_status=without_status,
            reader_version=READER_VERSION,
        )

    def _assemble_skips(self) -> tuple[RouteSkip, ...]:
        items = [
            RouteSkip("ed.route.node_state", _NODE_REASON),
            RouteSkip("ed.route.extensions", _EXTENSION_REASON),
            *self.skips,
        ]
        if self.variant_skips:
            positions = [
                f"{item.relative_file}:{item.line}"
                for item in self.variant_skips
                if item.relative_file
            ]
            head = ", ".join(positions[:3])
            items.append(
                RouteSkip(
                    "ed.route.variant_context",
                    f"Непрозрачные условия вариантов: {len(self.variant_skips)}; первые: {head}",
                )
            )
            items.extend(self.variant_skips)
        return tuple(
            sorted(items, key=lambda item: (item.code, item.relative_file, item.line, item.reason))
        )

    def _reading(
        self,
        plans: tuple[PlanRoute, ...],
        without: tuple[RouteEntry, ...],
        packages: tuple[PackageInfo, ...],
    ) -> RouteReading:
        ed_plans = [plan for plan in plans if plan.is_ed is True]
        unreachable = {
            (entry.source.relative_file, entry.source.line_start)
            for plan in ed_plans
            for entry in plan.entries
            if entry.state == "unreachable"
        }
        unreachable.update(
            (entry.source.relative_file, entry.source.line_start)
            for entry in without
            if entry.state == "unreachable"
        )
        names = {
            entry.manager_name
            for plan in ed_plans
            for entry in plan.entries
            if entry.state == "effective" and entry.manager_name
        }
        names.update(
            entry.manager_name
            for entry in without
            if entry.state == "effective" and entry.manager_name
        )
        return RouteReading(
            xml_plans=sum(
                1
                for name in self.inventory.plans
                if self._is_file(self.root / "ExchangePlans" / f"{name}.xml")
            ),
            manager_modules=sum(
                1
                for name in self.inventory.plans
                if self._is_file(self.root / "ExchangePlans" / name / "Ext" / "ManagerModule.bsl")
            ),
            ed_assignments=self.ed_assignments,
            literal_insertions=len(self.insert_sites),
            effective_plan=sum(len(plan.effective_map()) for plan in ed_plans),
            effective_without_node=sum(1 for entry in without if entry.state == "effective"),
            unreachable_insertions=len(unreachable),
            direct_calls_plan=self.direct_calls_plan,
            calls_without_node=self.calls_without_node,
            module_aliases_reached=self.module_aliases,
            static_subsystem_checks=tuple(self.subsystem_checks),
            variant_string_assigns=self.variant_string,
            variant_const_assigns=self.variant_const,
            base_ed_packages=len(
                {item.namespace for item in packages if _base_version(item.namespace)}
            ),
            unparsed_map_operations=self.map_skip_count,
            ed_managers=len(names),
        )

    def _inventory(self) -> _Inventory:
        root = self._xml(self.root / "Configuration.xml")
        configuration = _find_child(root, "Configuration")
        if configuration is None:
            raise EdFormatError("Повреждён Configuration.xml")
        properties = _find_child(configuration, "Properties")
        name_node = None if properties is None else _find_child(properties, "Name")
        configuration_name = (
            (name_node.text or "").strip() if name_node is not None and name_node.text else None
        )
        children = _find_child(configuration, "ChildObjects")
        plans: list[str] = []
        modules: list[str] = []
        subsystems: list[str] = []
        packages: list[str] = []
        if children is not None:
            for node in children:
                label = (node.text or "").strip()
                kind = _local(node.tag)
                if not label:
                    continue
                if kind == "ExchangePlan":
                    plans.append(label)
                elif kind == "CommonModule":
                    modules.append(label)
                elif kind == "Subsystem":
                    subsystems.append(label)
                elif kind == "XDTOPackage":
                    packages.append(label)
        folded = {item.casefold(): item for item in modules}
        return _Inventory(
            configuration_name,
            tuple(plans),
            tuple(modules),
            tuple(subsystems),
            tuple(packages),
            folded,
        )

    def _read_plan(self, name: str) -> PlanRoute:
        relative = f"ExchangePlans/{name}.xml"
        path = self.root / "ExchangePlans" / f"{name}.xml"
        empty_registration = RegistrationProfile("none")
        if not self._is_file(path):
            self._skip(
                "ed.route.reading", "Нет XML плана, объявленного в конфигурации", relative, 1
            )
            return _empty_plan(name, relative, "partial", None, empty_registration)
        try:
            templates = _template_names(self._xml(path))
        except EdResourceLimitError:
            raise
        except (EdFormatError, EdReadError):
            self._skip("ed.route.reading", "Повреждённый XML плана обмена", relative, 1)
            return _empty_plan(name, relative, "partial", None, empty_registration)
        bsl = self.root / "ExchangePlans" / name / "Ext" / "ManagerModule.bsl"
        if not self._is_file(bsl):
            return _empty_plan(name, relative, "complete", False, empty_registration)
        module = self._load_bsl(bsl)
        bsl_relative = _relative(self.root, bsl)
        if module is None:
            if bsl_relative not in self.unreadable:
                self._skip(
                    "ed.route.reading",
                    "Модуль менеджера плана не разобран",
                    bsl_relative,
                    1,
                )
            return _empty_plan(name, relative, "partial", None, empty_registration)
        self.ed_assignments += _count_windows(
            _procedure_tokens(module, _SETTINGS),
            ("настройки", ".", "этопланобменаxdto", "=", "истина"),
        )
        routine = module.routines.get(_SETTINGS.casefold())
        if routine is None:
            return _empty_plan(name, relative, "complete", False, empty_registration)
        run = _Run(collect_literals=True, procedure=routine.name, file=module.file)
        settings = _default_settings()
        env = _Env({}, settings, [])
        _register_maps(env, settings)
        self._exec_routine(module, routine, env, run, (settings,))
        self._commit(run)
        is_ed = _flag(settings, "этопланобменаxdto")
        if is_ed is True:
            self.insert_sites |= run.inserts
            self.direct_calls_plan += run.direct_calls
            self.module_aliases += run.module_aliases
        version_map = settings.values.get("версииформатаобмена")
        entries = _entries_of(version_map, settings)
        extensions = _extensions_of(settings)
        base = settings.values.get("форматобмена")
        namespace = base if isinstance(base, str) and base else None
        registration = (
            self._registration(settings, name, templates, relative)
            if is_ed is True
            else empty_registration
        )
        variants: tuple[VariantInfo, ...] = ()
        if is_ed is True and settings.algorithms.get("приполучениивариантовнастроекобмена") is True:
            variants = self._variants(module, name)
        if (
            is_ed is True
            and settings.algorithms.get("приполученииописаниявариантанастройки") is True
        ):
            self._scan_descriptions(module)
        fallback, tied = _min_version(entries)
        plan_status: RouteStatus = "partial" if run.partial or is_ed is None else "complete"
        return PlanRoute(
            plan_name=name,
            metadata_path=relative,
            is_ed=is_ed,
            base_namespace=namespace,
            settings_source=_source(module.file, routine.header, routine.name, ()),
            entries=tuple(entries),
            registration=registration,
            variants=variants,
            declared_plan_extensions=tuple(extensions),
            status=plan_status,
            empty_node_fallback=fallback,
            empty_node_tied_minima=tied,
        )

    def _read_global(
        self,
    ) -> tuple[tuple[RouteEntry, ...], tuple[FormatExtension, ...], RouteStatus]:
        declared = _OVERRIDE.casefold() in self.inventory.module_names
        path = self.root / "CommonModules" / _OVERRIDE / "Ext" / "Module.bsl"
        if not declared or not self._is_file(path):
            self._skip(
                "ed.route.reading",
                "Нет модуля ОбменДаннымиПереопределяемый: пустая карта без узла не доказана",
                f"CommonModules/{_OVERRIDE}/Ext/Module.bsl",
                1,
            )
            return (), (), "partial"
        module = self._load_bsl(path)
        if module is None:
            relative = _relative(self.root, path)
            if relative not in self.unreadable:
                self._skip("ed.route.reading", "Модуль переопределения не разобран", relative, 1)
            return (), (), "partial"
        versions = module.routines.get(_VERSIONS.casefold())
        if versions is None:
            self._skip(
                "ed.route.reading",
                "Нет ПриПолученииДоступныхВерсийФормата: пустая карта без узла не доказана",
                module.file.file_id,
                1,
            )
            return (), (), "partial"
        receiver = _Map("versions")
        run = _Run(collect_literals=True, procedure=versions.name, file=module.file)
        env = _Env({}, None, [receiver])
        self._exec_routine(module, versions, env, run, (receiver,))
        self._commit(run)
        self.insert_sites |= run.inserts
        self.calls_without_node += run.reached_calls
        self.module_aliases += run.module_aliases
        extensions: list[FormatExtension] = []
        extension_routine = module.routines.get(_EXTENSIONS.casefold())
        if extension_routine is not None:
            extension_map = _Map("extensions")
            extra = _Run(procedure=extension_routine.name, file=module.file)
            extra_env = _Env({}, None, [extension_map])
            self._exec_routine(module, extension_routine, extra_env, extra, (extension_map,))
            self._commit(extra)
            if extra.partial:
                run.partial = True
            extensions = _extension_entries(extension_map)
        status = "partial" if run.partial else "complete"
        return tuple(_route_entries(receiver)), tuple(extensions), status

    def _variants(self, module: _Module, plan_name: str) -> tuple[VariantInfo, ...]:
        routine = module.routines.get(_VARIANTS.casefold())
        if routine is None:
            self._skip(
                "ed.route.reading", "Нет процедуры вариантов настройки", module.file.file_id, 1
            )
            return ()
        found: list[VariantInfo] = []
        rows: set[str] = set()
        collection = routine.params[0].name.casefold() if routine.params else ""
        self._walk_variants(routine.nodes, module, plan_name, [], rows, found, collection)
        return tuple(found)

    def _walk_variants(
        self,
        nodes: list[_Node],
        module: _Module,
        plan_name: str,
        stack: list[RouteCondition],
        rows: set[str],
        found: list[VariantInfo],
        collection: str,
    ) -> None:
        for node in nodes:
            if isinstance(node, _If):
                for tokens, body, statement in node.clauses:
                    condition = self._condition(
                        tokens, statement, _Env({}, None, []), _Run(), module
                    )
                    if condition.value == "unknown":
                        self._variant_skip(
                            module.file, statement, "Условие варианта зависит от данных сеанса"
                        )
                    self._walk_variants(
                        body, module, plan_name, [*stack, condition], rows, found, collection
                    )
                if node.else_body:
                    first = node.clauses[0][2] if node.clauses else None
                    if first is not None and len(node.clauses) == 1:
                        opened = self._condition(
                            node.clauses[0][0], first, _Env({}, None, []), _Run(), module
                        )
                        flipped = _flip(opened)
                    else:
                        flipped = RouteCondition(
                            "opaque",
                            "Иначе",
                            "unknown",
                            _source(module.file, node.else_statement.span, _VARIANTS, ())
                            if node.else_statement is not None
                            else _source(module.file, module.file.span(0, 0), _VARIANTS, ()),
                        )
                    self._walk_variants(
                        node.else_body,
                        module,
                        plan_name,
                        [*stack, flipped],
                        rows,
                        found,
                        collection,
                    )
                continue
            if isinstance(node, _Loop | _Try):
                body = node.body if isinstance(node, _Loop) else [*node.body, *node.handler]
                self._walk_variants(list(body), module, plan_name, stack, rows, found, collection)
                continue
            if not isinstance(node, _Stmt):
                continue
            self._variant_statement(
                node.statement, module, plan_name, stack, rows, found, collection
            )

    def _variant_statement(
        self,
        statement: Statement,
        module: _Module,
        plan_name: str,
        stack: list[RouteCondition],
        rows: set[str],
        found: list[VariantInfo],
        collection: str,
    ) -> None:
        tokens = _bare(statement.tokens)
        assigned = _assignment(tokens)
        if assigned is None:
            return
        lhs, rhs = assigned
        call = _call(rhs)
        dotted = None if call is None else _dotted(call[0])
        if (
            len(lhs) == 1
            and lhs[0].kind == "identifier"
            and dotted is not None
            and len(dotted) == 2
            and dotted[0].casefold() == collection
            and dotted[1].casefold() == "добавить"
        ):
            rows.add(lhs[0].folded)
            return
        if len(lhs) == 3 and lhs[1].value == "." and lhs[2].folded == "идентификаторнастройки":
            if lhs[0].folded not in rows:
                return
            resolved, kind = self._const_or_string(rhs, module, plan_name)
            raw = _text_of(module.file, rhs)
            if kind == "string":
                self.variant_string += 1
            elif kind == "const" and resolved is not None:
                self.variant_const += 1
            elif resolved is None:
                self._variant_skip(
                    module.file, statement, "Идентификатор варианта не является литералом"
                )
            correspondent_raw = _correspondent_literal(_tokens_of_conditions(stack))
            metadata = tuple(item for item in stack if item.kind == "static_metadata")
            found.append(
                VariantInfo(
                    id=resolved,
                    raw_id=raw,
                    source=_source(module.file, statement.span, _VARIANTS, ()),
                    conditions=tuple(stack),
                    correspondent=None,
                    correspondent_raw=correspondent_raw,
                    metadata_predicates=metadata,
                )
            )

    def _scan_descriptions(self, module: _Module) -> None:
        routine = module.routines.get(_DESCRIBE.casefold())
        if routine is None:
            return
        for statement in routine.statements:
            if any(
                token.folded == "нстр" and token.kind == "identifier" for token in statement.tokens
            ):
                self._variant_skip(
                    module.file, statement, "НСтр не вычисляется; поле описания сохранено как raw"
                )

    def _registration(
        self,
        settings: _Settings,
        plan_name: str,
        templates: tuple[str, ...],
        metadata_path: str,
    ) -> RegistrationProfile:
        flag = settings.values.get("правиларегистрациивменеджере")
        source = _setting_source(settings, "правиларегистрациивменеджере")
        if not isinstance(flag, bool):
            return RegistrationProfile("unknown", source=source)
        if flag is True:
            raw_name = settings.values.get("имяменеджерарегистрации")
            name = raw_name if isinstance(raw_name, str) and raw_name else None
            path = None
            if name and name.casefold() in self.inventory.module_names:
                body = (
                    self.root
                    / "CommonModules"
                    / self.inventory.module_names[name.casefold()]
                    / "Ext"
                    / "Module.bsl"
                )
                path = _relative(self.root, body) if self._is_file(body) else None
            return RegistrationProfile("manager", name, path, source=source)
        template_meta = metadata_path if "ПравилаРегистрации" in templates else None
        body = (
            self.root
            / "ExchangePlans"
            / plan_name
            / "Templates"
            / "ПравилаРегистрации"
            / "Ext"
            / "Template.txt"
        )
        return RegistrationProfile(
            "xml",
            template_metadata_path=template_meta,
            template_body_path=_relative(self.root, body) if self._is_file(body) else None,
            source=source,
        )

    def _read_packages(self) -> tuple[PackageInfo, ...]:
        found: list[PackageInfo] = []
        for name in self.inventory.packages:
            relative = f"XDTOPackages/{name}.xml"
            path = self.root / "XDTOPackages" / f"{name}.xml"
            if not self._is_file(path):
                self._skip("ed.route.reading", "Нет описания пакета XDTO", relative, 1)
                continue
            try:
                metadata_name, namespace, revision, _source_info = package_metadata(path)
            except (EdSchemaFormatError, EdSchemaReadError) as error:
                self._observe_unparsed(path)
                self._skip("ed.route.reading", str(error), relative, 1)
                continue
            binary = self.root / "XDTOPackages" / name / "Ext" / "Package.bin"
            self._observed(path, _source_info.sha256)
            binary_relative = _relative(self.root, binary) if self._is_file(binary) else None
            sources = (relative,) if binary_relative is None else (relative, binary_relative)
            self.files[_relative(self.root, path)] = _source_info.sha256
            found.append(
                PackageInfo(
                    metadata_name,
                    namespace,
                    relative,
                    binary_relative,
                    revision,
                    sources,
                )
            )
        return tuple(found)

    def _note_ambiguous_packages(self, packages: tuple[PackageInfo, ...]) -> None:
        groups: dict[str, list[str]] = {}
        for item in packages:
            groups.setdefault(item.namespace, []).append(item.metadata_name)
        for namespace, names in groups.items():
            if len(names) > 1:
                joined = ", ".join(names)
                self._skip(
                    "ed.route.schema_ambiguous",
                    f"Один Namespace {namespace} у пакетов: {joined}",
                    "",
                    0,
                )

    def _referenced_managers(
        self, plans: tuple[PlanRoute, ...], without: tuple[RouteEntry, ...]
    ) -> set[str]:
        names: set[str] = set()
        for plan in plans:
            for entry in plan.entries:
                if entry.manager_name:
                    names.add(entry.manager_name)
            if plan.registration.manager_name:
                names.add(plan.registration.manager_name)
        for entry in without:
            if entry.manager_name:
                names.add(entry.manager_name)
        canonical: set[str] = set()
        for name in names:
            canonical.add(self.inventory.module_names.get(name.casefold(), name))
        return canonical

    def _manager(self, name: str) -> ManagerInfo:
        declared = name.casefold() in self.inventory.module_names
        canonical = self.inventory.module_names.get(name.casefold(), name)
        body = self.root / "CommonModules" / canonical / "Ext" / "Module.bsl"
        source_exists = declared and self._is_file(body)
        relative = _relative(self.root, body) if source_exists else None
        if not source_exists:
            return ManagerInfo(canonical, declared, False, None, None, "unknown", ())
        try:
            document = self.documents.get(body)
            if document is not None:
                try:
                    with body.open("rb") as stream:
                        raw = stream.read(MAX_FILE_BYTES + 1)
                except OSError as error:
                    raise EdReadError("Файл менеджера недоступен") from error
                if len(raw) > MAX_FILE_BYTES:
                    raise EdResourceLimitError("Размер менеджера превышает 32 MiB")
                fingerprint = hashlib.sha256(raw).hexdigest()
                self._observed(body, fingerprint)
                if not document.files or document.files[0].sha256 != fingerprint:
                    document = None
            if document is None:
                document = read_manager(body)
        except EdResourceLimitError:
            raise
        except EdFormatError:
            self._observe_unparsed(body)
            return ManagerInfo(canonical, True, True, relative, None, "unknown", (), ("ed_format",))
        except EdReadError as error:
            self._observe_unparsed(body)
            self._skip("ed.route.reading", str(error), relative or "", 1)
            return ManagerInfo(canonical, True, False, None, None, "unknown", ())
        if document.files:
            self.files.setdefault(_relative(self.root, body), document.files[0].sha256)
            self._observed(body, document.files[0].sha256)
        present = any(
            item.name.casefold() == VERSION_ROUTINE.casefold() for item in document.routines
        )
        if document.manager_version in MANAGER_VERSIONS:
            version: int | None = document.manager_version
            origin = "declared"
        elif not present:
            version = 1
            origin = "fallback"
        else:
            version = None
            origin = "unknown"
        directions: set[str] = set()
        for use in document.rule_uses:
            if use.direction == "both":
                directions.update(("send", "receive"))
            elif use.direction in ("send", "receive"):
                directions.add(use.direction)
        codes: list[str] = []
        for item in document.diagnostics:
            if item.code not in codes:
                codes.append(item.code)
        if document.parse_status is ParseStatus.PARTIAL and "partial" not in codes:
            codes.append("partial")
        return ManagerInfo(
            canonical,
            True,
            True,
            relative,
            version,
            origin,
            tuple(item for item in ("send", "receive") if item in directions),
            tuple(codes),
        )

    def _exec_routine(
        self,
        module: _Module,
        routine: _Routine,
        env: _Env,
        run: _Run,
        arguments: tuple[object, ...],
    ) -> None:
        if run.aborted:
            return
        if not routine.closed:
            self._map_skip(module.file, routine.header, "Незакрытый структурный оператор", run)
            return
        key = (module.file.file_id, routine.name.casefold())
        if key in run.stack:
            self._map_skip(module.file, routine.header, "Цикл вызовов при чтении карты", run)
            return
        if key not in run.seen:
            run.seen.add(key)
            if len(run.seen) > MAX_VISITED:
                self._map_skip(
                    module.file, routine.header, "Превышен предел посещённых методов", run
                )
                run.aborted = True
                return
        if not self._bind(module, routine, env, run, arguments):
            return
        previous = (run.procedure, run.file)
        run.stack.append(key)
        run.procedure = routine.name
        run.file = module.file
        try:
            self._exec_nodes(routine.nodes, env, run)
        except _Exit:
            pass
        finally:
            run.stack.pop()
            run.procedure, run.file = previous

    def _bind(
        self,
        module: _Module,
        routine: _Routine,
        env: _Env,
        run: _Run,
        arguments: tuple[object, ...],
    ) -> bool:
        if len(arguments) > len(routine.params):
            span = routine.header
            self._map_skip(module.file, span, "Неизвестная сигнатура вызова", run)
            return False
        for index, param in enumerate(routine.params):
            if index < len(arguments):
                value = arguments[index]
                if value is _UNKNOWN:
                    self._map_skip(module.file, routine.header, "Неизвестный аргумент вызова", run)
                    return False
                if param.by_value and isinstance(value, _Map | _Settings):
                    self._map_skip(
                        module.file,
                        routine.header,
                        "Передача коллекции со Знач не подтверждает тот же объект",
                        run,
                    )
                    return False
                env.locals[param.name.casefold()] = value
                continue
            if param.default:
                env.locals[param.name.casefold()] = self._eval(param.default, env, module, None)
                continue
            self._map_skip(module.file, routine.header, "Неизвестная сигнатура вызова", run)
            return False
        return True

    def _exec_nodes(self, nodes: Sequence[object], env: _Env, run: _Run) -> None:
        for node in nodes:
            if run.aborted:
                return
            if isinstance(node, _Stmt):
                self._exec_statement(node.statement, env, run)
            elif isinstance(node, _If):
                self._exec_if(node, env, run)
            elif isinstance(node, _Loop | _Try):
                body = node.body if isinstance(node, _Loop) else [*node.body, *node.handler]
                if _affects_route(body, env):
                    file = run.file
                    if file is not None:
                        self._map_skip(
                            file,
                            node.statement.span,
                            "Цикл или исключение с картой не читается",
                            run,
                        )
                    if env.settings is not None:
                        for field_name in _settings_writes(body, env):
                            _mark_unread(env.settings, field_name)
                continue

    def _exec_if(self, node: _If, env: _Env, run: _Run) -> None:
        if len(run.conditions) >= MAX_CONDITIONS:
            file = run.file
            if file is not None and node.clauses:
                self._map_skip(file, node.clauses[0][2].span, "Превышена глубина условий", run)
            run.aborted = True
            return
        file = run.file
        module = None if file is None else self.modules.get(file.file_id)
        if module is None or not node.clauses:
            return
        if node.preprocessor:
            tokens, _body, statement = node.clauses[0]
            opened = self._condition(tokens, statement, env, run, module)
            unknown = RouteCondition("opaque", opened.raw, "unknown", opened.source)
            self._fork_unknown(node, env, run, unknown, "Ветки препроцессора не выбираются")
            return
        known: list[tuple[RouteCondition, list[_Node]]] = []
        for tokens, body, statement in node.clauses:
            condition = self._condition(tokens, statement, env, run, module)
            if condition.value == "unknown":
                self._fork_unknown(node, env, run, condition)
                return
            if condition.value == "true":
                try:
                    self._exec_guarded(body, env, run, condition)
                finally:
                    for _, other, _ in node.clauses:
                        if other is not body:
                            self._harvest(other, env, run, "unreachable", condition)
                    if node.else_body:
                        self._harvest(node.else_body, env, run, "unreachable", _flip(condition))
                return
            known.append((condition, body))
        else_condition = _flip(known[-1][0]) if known else None
        if node.else_body and else_condition is not None:
            try:
                self._exec_guarded(node.else_body, env, run, else_condition)
            finally:
                for condition, body in known:
                    self._harvest(body, env, run, "unreachable", condition)
            return
        for condition, body in known:
            self._harvest(body, env, run, "unreachable", condition)

    def _exec_guarded(
        self, nodes: Sequence[object], env: _Env, run: _Run, condition: RouteCondition
    ) -> None:
        run.conditions.append(condition)
        if condition.kind == "static_metadata":
            run.chain.append(_span_of(condition))
            chained = True
        else:
            chained = False
        try:
            self._exec_nodes(nodes, env, run)
        finally:
            if chained:
                run.chain.pop()
            run.conditions.pop()

    def _fork_unknown(
        self,
        node: _If,
        env: _Env,
        run: _Run,
        condition: RouteCondition,
        reason: str = "Условие по данным не определяет карту",
    ) -> None:
        bodies: list[list[_Node]] = [body for _, body, _ in node.clauses]
        if node.else_statement is not None:
            bodies.append(node.else_body)
        if not any(_affects_route(body, env) for body in bodies):
            return
        file = run.file
        if file is not None:
            self._map_skip(file, _span_of(condition), reason, run)
        saved = _snapshot(env)
        saved_tail = list(run.tail_conditions)
        speculative = run.speculative
        run.speculative = True
        branches: list[list[tuple[_Map, _Live]]] = []
        exits: list[bool] = []
        try:
            for body in bodies:
                _restore(env, saved)
                run.tail_conditions[:] = saved_tail
                try:
                    self._exec_guarded(body, env, run, condition)
                except _Exit:
                    exits.append(True)
                else:
                    exits.append(False)
                branches.append(_fresh_entries(env, saved))
        finally:
            run.speculative = speculative
            run.tail_conditions[:] = saved_tail
            _restore(env, saved)
        if env.settings is not None:
            for body in bodies:
                for field_name in _settings_writes(body, env):
                    _mark_unread(env.settings, field_name)
        live = {id(item) for item in env.maps}
        touched: set[tuple[int, str]] = set()
        for branch in branches:
            for receiver, item in branch:
                if id(receiver) not in live:
                    continue
                touched.add((id(receiver), item.key))
                item.state = "conditional"
                receiver.entries.append(item)
                self._note_literal(run, item)
        for receiver in env.maps:
            for item in receiver.entries:
                if item.state == "effective" and (id(receiver), item.key) in touched:
                    item.state = "conditional"
        fallthrough = node.else_statement is None
        paths = [*exits, False] if fallthrough else exits
        if paths and all(paths):
            raise _Exit
        if any(exits):
            run.tail_conditions.append(condition)

    def _exec_statement(self, statement: Statement, env: _Env, run: _Run) -> None:
        if statement.tokens and statement.tokens[0].kind == "directive":
            return
        if statement.head in ("возврат", "вызватьисключение"):
            raise _Exit
        tokens = _bare(statement.tokens)
        assigned = _assignment(tokens)
        if assigned is not None:
            self._assign(assigned[0], assigned[1], statement, env, run)
            return
        call = _call(tokens)
        if call is not None:
            self._exec_call(call[0], call[1], statement, env, run)

    def _assign(
        self,
        lhs: tuple[Token, ...],
        rhs: tuple[Token, ...],
        statement: Statement,
        env: _Env,
        run: _Run,
    ) -> None:
        file = run.file
        module = None if file is None else self.modules.get(file.file_id)
        if module is None:
            return
        names = _dotted(lhs)
        if names is None:
            if _passes_tracked(rhs, env):
                self._map_skip(
                    module.file, statement.span, "Карта уходит в неподдержанное присваивание", run
                )
            return
        folded = tuple(part.casefold() for part in names)
        if len(folded) == 1 and _is_new_map(rhs):
            created = _Map(None)
            env.maps.append(created)
            env.locals[folded[0]] = created
            return
        alias = self._module_alias(rhs, statement, module)
        if len(folded) == 1 and alias is not None:
            if alias is _UNKNOWN:
                self._map_skip(module.file, statement.span, "Вычисляемое имя общего модуля", run)
                env.locals[folded[0]] = _UNKNOWN
            else:
                env.locals[folded[0]] = alias
            return
        call = _call(rhs)
        if call is not None and _args_pass_tracked(call[1], env):
            self._map_skip(
                module.file,
                statement.span,
                "Карта или настройки переданы в неподдержанный вызов",
                run,
            )
            if len(folded) == 1:
                env.locals[folded[0]] = _UNKNOWN
                return
            bound = _bound_settings(env, folded[0])
            if bound is not None and len(folded) == 2 and folded[1] in _KNOWN_SETTINGS:
                _mark_unread(bound, folded[1])
            return
        target = self._eval(rhs, env, module, _current_plan(module))
        if len(folded) == 1:
            env.locals[folded[0]] = target
            return
        settings = _bound_settings(env, folded[0])
        if settings is None:
            if isinstance(target, _Map | _Settings):
                self._map_skip(
                    module.file, statement.span, "Карта записывается в неизвестное поле", run
                )
            return
        self._assign_setting(folded[1:], target, settings, module, statement, run)

    def _assign_setting(
        self,
        folded: tuple[str, ...],
        value: object,
        settings: _Settings,
        module: _Module,
        statement: Statement,
        run: _Run,
    ) -> None:
        if len(folded) == 2 and folded[0] == "алгоритмы":
            if folded[1] not in _KNOWN_ALGORITHMS:
                return
            if isinstance(value, bool) and not run.tail_conditions:
                settings.algorithms[folded[1]] = value
                return
            settings.algorithms[folded[1]] = _UNREAD
            if not run.tail_conditions:
                self._map_skip(
                    module.file, statement.span, "Значение поля настроек не прочитано", run
                )
            return
        if len(folded) != 1:
            return
        field_name = folded[0]
        if field_name not in _KNOWN_SETTINGS:
            if isinstance(value, _Map | _Settings):
                self._map_skip(
                    module.file, statement.span, "Карта записывается в неизвестное поле", run
                )
            return
        if isinstance(value, _Map):
            if field_name == "версииформатаобмена":
                _replace_map(settings, field_name, value, "versions")
            elif field_name == "расширенияформатаобмена":
                _replace_map(settings, field_name, value, "extensions")
            else:
                self._map_skip(
                    module.file, statement.span, "Карта записывается в скалярное поле", run
                )
                return
            if run.tail_conditions:
                for entry in value.entries:
                    if entry.state == "effective":
                        entry.state = "conditional"
                        entry.conditions = (*entry.conditions, *run.tail_conditions)
            return
        if run.tail_conditions:
            _mark_unread(settings, field_name)
            return
        if isinstance(value, bool | str):
            settings.values[field_name] = value
            settings.values[f"{field_name}#source"] = _source(
                module.file, statement.span, run.procedure, tuple(run.chain)
            )
            return
        _mark_unread(settings, field_name)
        self._map_skip(module.file, statement.span, "Значение поля настроек не прочитано", run)

    def _exec_call(
        self,
        callee: tuple[Token, ...],
        arguments: tuple[tuple[Token, ...], ...],
        statement: Statement,
        env: _Env,
        run: _Run,
    ) -> None:
        file = run.file
        module = None if file is None else self.modules.get(file.file_id)
        if module is None:
            return
        parts = _dotted(callee)
        if parts is None:
            if _args_pass_tracked(arguments, env):
                self._map_skip(
                    module.file,
                    statement.span,
                    "Карта или настройки переданы в неподдержанный вызов",
                    run,
                )
            return
        folded = tuple(part.casefold() for part in parts)
        if (
            len(parts) == 2
            and folded[0] in env.locals
            and isinstance(env.locals[folded[0]], _Alias)
        ):
            alias = env.locals[folded[0]]
            assert isinstance(alias, _Alias)
            self._follow(
                alias.module_name, parts[1], arguments, statement, env, run, alias.span, True
            )
            return
        if (
            len(parts) == 2
            and folded[0] not in env.locals
            and folded[0] in self.inventory.module_names
        ):
            self._follow(
                self.inventory.module_names[folded[0]],
                parts[1],
                arguments,
                statement,
                env,
                run,
                None,
                False,
            )
            return
        receiver = self._receiver(folded[:-1], env)
        if isinstance(receiver, _Settings):
            self._settings_call(receiver, folded[-1], arguments, statement, env, run)
            return
        if folded[-1] == "вставить" and isinstance(receiver, _Map):
            self._insert(receiver, arguments, statement, env, run)
            return
        if _args_pass_tracked(arguments, env):
            self._map_skip(
                module.file,
                statement.span,
                "Карта или настройки переданы в неподдержанный вызов",
                run,
            )

    def _follow(
        self,
        module_name: str,
        procedure: str,
        arguments: tuple[tuple[Token, ...], ...],
        statement: Statement,
        env: _Env,
        run: _Run,
        alias_span: SourceSpan | None,
        via_alias: bool,
    ) -> None:
        file = run.file
        if file is None:
            return
        module = self.modules[file.file_id]
        values = tuple(self._eval(arg, env, module, _current_plan(module)) for arg in arguments)
        tracked = any(isinstance(item, _Map | _Settings) for item in values) or _args_pass_tracked(
            arguments, env
        )
        if not tracked:
            return
        if run.depth + 1 > MAX_EDGES:
            self._map_skip(file, statement.span, "Превышена глубина межмодульных вызовов", run)
            return
        if run.calls + 1 > MAX_CALLS:
            self._map_skip(file, statement.span, "Превышен предел вызовов", run)
            run.aborted = True
            return
        canonical = self.inventory.module_names.get(module_name.casefold())
        path = (
            None
            if canonical is None
            else self.root / "CommonModules" / canonical / "Ext" / "Module.bsl"
        )
        if canonical is None or path is None or not self._is_file(path):
            self._map_skip(file, statement.span, f"Нет общего модуля {module_name}", run)
            return
        module_name = canonical
        target = self._load_bsl(path)
        if target is None:
            self._map_skip(file, statement.span, f"Модуль {module_name} не разобран", run)
            return
        routine = target.routines.get(procedure.casefold())
        if routine is None or routine.kind != "procedure":
            self._map_skip(file, statement.span, f"Нет процедуры {procedure}", run)
            return
        if any(item is _UNKNOWN for item in values):
            self._map_skip(file, statement.span, "Неизвестный аргумент вызова", run)
            return
        if not any(isinstance(item, _Map | _Settings) for item in values):
            self._map_skip(
                file,
                statement.span,
                "Карта или настройки переданы в неподдержанный вызов",
                run,
            )
            return
        has_map = any(isinstance(item, _Map) for item in values)
        run.calls += 1
        if not run.speculative and has_map:
            run.reached_calls += 1
            if via_alias:
                run.module_aliases += 1
            else:
                run.direct_calls += 1
        if alias_span is not None:
            run.chain.append(alias_span)
        run.chain.append(statement.span)
        child = _Env({}, env.settings, env.maps)
        run.depth += 1
        try:
            self._exec_routine(target, routine, child, run, values)
        finally:
            run.depth -= 1
            run.chain.pop()
            if alias_span is not None:
                run.chain.pop()

    def _insert(
        self,
        receiver: _Map,
        arguments: tuple[tuple[Token, ...], ...],
        statement: Statement,
        env: _Env,
        run: _Run,
    ) -> None:
        file = run.file
        if file is None:
            return
        if len(arguments) != 2 or len(arguments[0]) != 1 or arguments[0][0].kind != "string":
            self._map_skip(file, statement.span, "Ключ или значение вставки не литеральны", run)
            return
        key_raw = arguments[0][0].value
        second = arguments[1]
        role = receiver.role
        if len(second) == 1 and second[0].kind == "string" and role in (None, "extensions"):
            receiver.role = "extensions"
            value = second[0].value
            kind = "version"
        elif (
            len(second) == 1
            and second[0].kind == "identifier"
            and second[0].folded not in env.locals
            and role in (None, "versions")
        ):
            receiver.role = "versions"
            value = second[0].value
            kind = "module"
        else:
            self._map_skip(file, statement.span, "Ключ или значение вставки не литеральны", run)
            return
        if sum(len(item.entries) for item in env.maps) >= MAX_ENTRIES:
            self._map_skip(file, statement.span, "Превышен предел записей карты", run)
            run.aborted = True
            return
        self._idents += 1
        state = "conditional" if run.tail_conditions else "effective"
        entry = _Live(
            self._idents,
            key_raw,
            key_raw.strip(),
            value,
            kind,
            _source(file, statement.span, run.procedure, tuple(run.chain)),
            (*run.conditions, *run.tail_conditions),
            state,
        )
        for previous in receiver.entries:
            if previous.state == "effective" and previous.key == entry.key:
                previous.state = "overwritten" if state == "effective" else "conditional"
        receiver.entries.append(entry)
        self._note_literal(run, entry)

    def _harvest(
        self,
        nodes: Sequence[object],
        env: _Env,
        run: _Run,
        state: str,
        condition: RouteCondition,
    ) -> None:
        for node in nodes:
            if isinstance(node, _Stmt):
                self._harvest_statement(node.statement, env, run, state, condition)
            elif isinstance(node, _If):
                for _, body, _ in node.clauses:
                    self._harvest(body, env, run, state, condition)
                self._harvest(node.else_body, env, run, state, condition)
            elif isinstance(node, _Loop):
                self._harvest(node.body, env, run, state, condition)
            elif isinstance(node, _Try):
                self._harvest([*node.body, *node.handler], env, run, state, condition)

    def _harvest_statement(
        self,
        statement: Statement,
        env: _Env,
        run: _Run,
        state: str,
        condition: RouteCondition,
    ) -> None:
        call = _call(_bare(statement.tokens))
        if call is None or len(call[1]) != 2:
            return
        parts = _dotted(call[0])
        if parts is None or parts[-1].casefold() != "вставить":
            return
        receiver = self._receiver(tuple(part.casefold() for part in parts[:-1]), env)
        if not isinstance(receiver, _Map):
            return
        key, value = call[1]
        if len(key) != 1 or key[0].kind != "string":
            return
        if len(value) != 1 or value[0].kind not in ("string", "identifier"):
            return
        if value[0].kind == "identifier" and value[0].folded in env.locals:
            return
        file = run.file
        if file is None:
            return
        self._idents += 1
        receiver.entries.append(
            _Live(
                self._idents,
                key[0].value,
                key[0].value.strip(),
                value[0].value,
                "version" if value[0].kind == "string" else "module",
                _source(file, statement.span, run.procedure, tuple(run.chain)),
                (*run.conditions, condition),
                state,
            )
        )
        self._note_literal(run, receiver.entries[-1])

    def _condition(
        self,
        tokens: tuple[Token, ...],
        statement: Statement,
        env: _Env,
        run: _Run,
        module: _Module,
    ) -> RouteCondition:
        value, kind = self._eval_condition(tokens, env, run, module)
        return RouteCondition(
            _as_kind(kind),
            _text_of(module.file, tokens),
            _as_value(value),
            _source(
                module.file, statement.span, run.procedure or module.file.file_id, tuple(run.chain)
            ),
        )

    def _eval_condition(
        self, tokens: tuple[Token, ...], env: _Env, run: _Run, module: _Module
    ) -> tuple[str, str]:
        return _combine(self._parse_or(tokens), env, run, module, self)

    def _parse_or(self, tokens: tuple[Token, ...]) -> object:
        parts = _split_keyword(tokens, "или")
        if len(parts) == 1:
            return self._parse_and(parts[0])
        return ("or", [self._parse_and(part) for part in parts])

    def _parse_and(self, tokens: tuple[Token, ...]) -> object:
        parts = _split_keyword(tokens, "и")
        if len(parts) == 1:
            return self._parse_not(parts[0])
        return ("and", [self._parse_not(part) for part in parts])

    def _parse_not(self, tokens: tuple[Token, ...]) -> object:
        if tokens and tokens[0].folded == "не" and tokens[0].kind == "identifier":
            return ("not", self._parse_not(tokens[1:]))
        if (
            len(tokens) >= 2
            and tokens[0].value == "("
            and tokens[-1].value == ")"
            and _wraps(tokens)
        ):
            return self._parse_or(tokens[1:-1])
        return ("atom", tokens)

    def _eval(
        self, tokens: tuple[Token, ...], env: _Env, module: _Module, plan_name: str | None
    ) -> object:
        if len(tokens) == 1:
            token = tokens[0]
            if token.kind == "string":
                return token.value
            if token.folded in ("истина", "ложь"):
                return token.folded == "истина"
            if token.kind == "identifier" and token.folded in env.locals:
                return env.locals[token.folded]
            return _UNKNOWN
        if _is_new_map(tokens):
            created = _Map(None)
            env.maps.append(created)
            return created
        resolved, _kind = self._const_or_string(tokens, module, plan_name)
        if resolved is not None and _kind == "const":
            return resolved
        names = _dotted(tokens)
        if names is not None:
            return self._receiver(tuple(part.casefold() for part in names), env) or _UNKNOWN
        return _UNKNOWN

    def _const_or_string(
        self, tokens: tuple[Token, ...], module: _Module, plan_name: str | None
    ) -> tuple[str | None, str]:
        if len(tokens) == 1 and tokens[0].kind == "string":
            return tokens[0].value, "string"
        call = _call(tokens)
        if call is None or any(call[1]):
            return None, ""
        parts = _dotted(call[0])
        if parts is None:
            return None, ""
        target: _Module | None
        folded = tuple(part.casefold() for part in parts)
        if len(parts) == 1:
            target = module
            name = parts[0]
        elif len(parts) == 2 and folded[0] in self.inventory.module_names:
            path = (
                self.root
                / "CommonModules"
                / self.inventory.module_names[folded[0]]
                / "Ext"
                / "Module.bsl"
            )
            target = self._load_bsl(path) if self._is_file(path) else None
            name = parts[1]
        elif (
            len(parts) == 3
            and folded[0] == "планыобмена"
            and plan_name
            and folded[1] == plan_name.casefold()
        ):
            path = self.root / "ExchangePlans" / plan_name / "Ext" / "ManagerModule.bsl"
            target = self._load_bsl(path) if self._is_file(path) else None
            name = parts[2]
        else:
            return None, ""
        if target is None:
            return None, ""
        routine = target.routines.get(name.casefold())
        if routine is None or routine.kind != "function" or routine.params:
            return None, ""
        body = [
            item
            for item in routine.statements
            if item.tokens and item.tokens[0].kind != "directive"
        ]
        if len(body) != 1:
            return None, ""
        returned = _bare(body[0].tokens)
        if (
            len(returned) == 2
            and normalized(returned)[0] == "возврат"
            and returned[1].kind == "string"
        ):
            return returned[1].value, "const"
        return None, ""

    def _module_alias(
        self, tokens: tuple[Token, ...], statement: Statement, module: _Module
    ) -> _Alias | object | None:
        call = _call(tokens)
        if call is None:
            return None
        parts = _dotted(call[0])
        if parts is None or [part.casefold() for part in parts] != [
            "общегоназначения",
            "общиймодуль",
        ]:
            return None
        if len(call[1]) != 1:
            return _UNKNOWN
        argument = call[1][0]
        if len(argument) == 1 and argument[0].kind == "string":
            return _Alias(argument[0].value, statement.span)
        return _UNKNOWN

    def _settings_call(
        self,
        settings: _Settings,
        method: str,
        arguments: tuple[tuple[Token, ...], ...],
        statement: Statement,
        env: _Env,
        run: _Run,
    ) -> None:
        file = run.file
        if file is None:
            return
        field_name = ""
        if method == "вставить" and arguments and len(arguments[0]) == 1:
            key = arguments[0][0]
            if key.kind == "string":
                field_name = key.value.casefold()
        if field_name in _KNOWN_SETTINGS or field_name in _KNOWN_ALGORITHMS:
            _mark_unread(settings, field_name)
            if not run.tail_conditions:
                self._map_skip(file, statement.span, "Значение поля настроек не прочитано", run)
            return
        if field_name:
            return
        if _args_pass_tracked(arguments, env):
            self._map_skip(
                file,
                statement.span,
                "Карта или настройки переданы в неподдержанный вызов",
                run,
            )

    def _note_literal(self, run: _Run, entry: _Live) -> None:
        if run.speculative or not run.collect_literals or entry.value_kind != "module":
            return
        value = entry.value or ""
        run.inserts.add(
            (
                entry.source.relative_file,
                entry.source.line_start,
                entry.source.line_end,
                entry.key_raw,
                value,
            )
        )

    def _receiver(self, parts: tuple[str, ...], env: _Env) -> object:
        if not parts:
            return None
        head = env.locals.get(parts[0])
        if isinstance(head, _Settings):
            if len(parts) == 1:
                return head
            if len(parts) == 2:
                value = head.values.get(parts[1])
                return value if isinstance(value, _Map) else None
            return None
        if len(parts) == 1:
            return head
        return None

    def _load_bsl(self, path: Path) -> _Module | None:
        relative = _relative(self.root, path)
        cached = self.modules.get(relative)
        if cached is not None:
            return cached
        if self.bsl_files >= MAX_BSL_FILES:
            raise EdResourceLimitError("Число файлов BSL превышает 4096")
        if self.observe is not None and path not in self.stamps:
            self._is_file(path)
        try:
            raw = path.read_bytes()
        except OSError:
            self.unreadable.add(relative)
            self._skip("ed.route.reading", "Модуль недоступен", relative, 1)
            return None
        fingerprint = hashlib.sha256(raw).hexdigest()
        self._observed(path, fingerprint)
        if len(raw) > MAX_FILE_BYTES:
            raise EdResourceLimitError("Файл BSL превышает 32 MiB")
        if self.bsl_bytes + len(raw) > MAX_TOTAL_BSL:
            raise EdResourceLimitError("Суммарный размер BSL превышает 256 MiB")
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeError:
            self.unreadable.add(relative)
            self._skip("ed.route.reading", "Модуль не в UTF-8", relative, 1)
            return None
        self.bsl_bytes += len(raw)
        self.bsl_files += 1
        self.files[relative] = fingerprint
        source = _source_file(relative, str(path), text, raw)
        try:
            lexical = lex(source)
        except EdFormatError:
            return None
        module = _Module(source, lexical, _routines(source, lexical))
        self.modules[relative] = module
        return module

    def _xml(self, path: Path) -> ET._Element:
        if self.observe is not None and path not in self.stamps:
            self._is_file(path)
        try:
            raw = path.read_bytes()
        except OSError as error:
            raise EdReadError("XML выгрузки недоступен") from error
        fingerprint = hashlib.sha256(raw).hexdigest()
        self._observed(path, fingerprint)
        if len(raw) > MAX_FILE_BYTES:
            raise EdResourceLimitError("XML выгрузки превышает 32 MiB")
        try:
            root = ET.fromstring(
                raw,
                ET.XMLParser(
                    resolve_entities=False, load_dtd=False, no_network=True, huge_tree=False
                ),
            )
        except ET.XMLSyntaxError as error:
            raise EdFormatError("Повреждённый XML выгрузки") from error
        if getattr(root.getroottree().docinfo, "doctype", "") or any(
            isinstance(node, ET._Entity) or node.tag == "{http://www.w3.org/2001/XInclude}include"
            for node in root.iter()
        ):
            raise EdFormatError("DTD, сущности и XInclude в выгрузке запрещены")
        stack = [(root, 1)]
        while stack:
            node, depth = stack.pop()
            if depth > MAX_XML_DEPTH:
                raise EdResourceLimitError("Глубина XML превышает 128")
            stack.extend((child, depth + 1) for child in node if isinstance(child.tag, str))
        self.files[_relative(self.root, path)] = fingerprint
        return root

    def _map_skip(self, file: SourceFile, span: SourceSpan, reason: str, run: _Run) -> None:
        run.partial = True
        if run.speculative:
            return
        run.skips.append(RouteSkip("ed.route.reading", reason, file.file_id, span.line_start))

    def _commit(self, run: _Run) -> None:
        self.skips.extend(run.skips)
        self.map_skip_count += len(run.skips)
        self.subsystem_checks.extend(run.checks)

    def _skip(self, code: str, reason: str, relative: str, line: int) -> None:
        self.skips.append(RouteSkip(code, reason, relative, line))

    def _variant_skip(self, file: SourceFile, statement: Statement, reason: str) -> None:
        self.variant_skips.append(
            RouteSkip("ed.route.variant_context", reason, file.file_id, statement.span.line_start)
        )

    def _subsystem(self, path: str, run: _Run) -> str:
        value = "unknown" if not self._disabled_callback_empty() else self._subsystem_tree(path)
        if not run.speculative:
            run.checks.append((path, value))
        return value

    def _disabled_callback_empty(self) -> bool:
        if self._callback is not None:
            return self._callback
        name = "ОбщегоНазначенияПереопределяемый"
        path = self.root / "CommonModules" / name / "Ext" / "Module.bsl"
        module = (
            self._load_bsl(path)
            if name.casefold() in self.inventory.module_names and self._is_file(path)
            else None
        )
        routine = None if module is None else module.routines.get(_DISABLED.casefold())
        empty = routine is not None and not [
            item
            for item in routine.statements
            if item.tokens and item.tokens[0].kind != "directive"
        ]
        self._callback = empty
        if not empty and not self._callback_noted:
            self._callback_noted = True
            where = (
                module.file.file_id
                if module is not None
                else f"CommonModules/{name}/Ext/Module.bsl"
            )
            line = 1 if routine is None else routine.header.line_start
            self.skips.append(
                RouteSkip(
                    "ed.route.reading",
                    "Callback отключённых подсистем не доказан пустым",
                    where,
                    line,
                )
            )
        return empty

    def _subsystem_tree(self, path: str) -> str:
        segments = path.split(".")
        if not segments or any(not segment for segment in segments):
            return "unknown"
        available = {name.casefold(): name for name in self.inventory.subsystems}
        directory = self.root / "Subsystems"
        for segment in segments:
            canonical = available.get(segment.casefold())
            if canonical is None:
                return "false"
            xml = directory / f"{canonical}.xml"
            if not self._is_file(xml):
                self._skip(
                    "ed.route.reading",
                    "Нет XML подсистемы, на которую есть ссылка",
                    _relative(self.root, xml),
                    1,
                )
                return "unknown"
            try:
                node = self._xml(xml)
            except EdResourceLimitError:
                raise
            except (EdFormatError, EdReadError):
                self._skip(
                    "ed.route.reading",
                    "Повреждённый XML подсистемы",
                    _relative(self.root, xml),
                    1,
                )
                return "unknown"
            include = _include_flag(node)
            if include is None:
                return "unknown"
            if include:
                return "false"
            subsystem = _find_child(node, "Subsystem")
            if subsystem is None:
                return "unknown"
            available = {name.casefold(): name for name in _named_children(subsystem, "Subsystem")}
            directory = directory / canonical / "Subsystems"
        return "true"


_UNKNOWN = object()
_UNREAD = object()


def _combine(
    expr: object, env: _Env, run: _Run, module: _Module, reader: _Reader
) -> tuple[str, str]:
    if not isinstance(expr, tuple):
        return "unknown", "opaque"
    kind = expr[0]
    if kind == "or":
        return _logic(expr[1], env, run, module, reader, False)
    if kind == "and":
        return _logic(expr[1], env, run, module, reader, True)
    if kind == "not":
        value, group = _combine(expr[1], env, run, module, reader)
        flipped = {"true": "false", "false": "true"}.get(value, "unknown")
        return flipped, group
    if kind == "atom":
        return _atom(expr[1], env, run, module, reader)
    return "unknown", "opaque"


def _logic(
    parts: object, env: _Env, run: _Run, module: _Module, reader: _Reader, conjunction: bool
) -> tuple[str, str]:
    if not isinstance(parts, list):
        return "unknown", "opaque"
    group = "literal_variant"
    unknown = False
    for part in parts:
        value, part_kind = _combine(part, env, run, module, reader)
        group = _merge_kind(group, part_kind)
        if conjunction and value == "false":
            return "false", group
        if not conjunction and value == "true":
            return "true", group
        if value == "unknown":
            unknown = True
    if unknown:
        return "unknown", "opaque" if group == "literal_variant" else group
    return ("true" if conjunction else "false"), group


def _atom(
    tokens: object, env: _Env, run: _Run, module: _Module, reader: _Reader
) -> tuple[str, str]:
    if not isinstance(tokens, tuple):
        return "unknown", "opaque"
    if len(tokens) == 1 and tokens[0].folded in ("истина", "ложь"):
        return ("true" if tokens[0].folded == "истина" else "false"), "literal_variant"
    call = _call(tokens)
    if call is not None:
        parts = _dotted(call[0])
        folded = () if parts is None else tuple(part.casefold() for part in parts)
        if folded == ("значениезаполнено",) and len(call[1]) == 1:
            return _filled(call[1][0], env), "literal_variant"
        if folded == ("общегоназначения", "подсистемасуществует") and len(call[1]) == 1:
            argument = call[1][0]
            if len(argument) == 1 and argument[0].kind == "string":
                return reader._subsystem(argument[0].value, run), "static_metadata"
            return "unknown", "opaque"
    compared = _comparison(tokens)
    if compared is not None:
        left, op, right = compared
        metadata = _metadata_name(left, op, right, reader.inventory.configuration_name)
        if metadata is not None:
            return metadata, "static_metadata"
        variant = _variant_compare(left, op, right, env, module, reader)
        if variant is not None:
            return variant, "literal_variant"
    return "unknown", "opaque"


def _filled(tokens: tuple[Token, ...], env: _Env) -> str:
    if len(tokens) == 1 and tokens[0].kind == "identifier":
        value = env.locals.get(tokens[0].folded, _UNKNOWN)
        if isinstance(value, str):
            return "false" if value == "" else "true"
    if len(tokens) == 1 and tokens[0].kind == "string":
        return "false" if tokens[0].value == "" else "true"
    return "unknown"


def _metadata_name(
    left: tuple[Token, ...], op: str, right: tuple[Token, ...], configuration: str | None
) -> str | None:
    if configuration is None or len(right) != 1 or right[0].kind != "string":
        return None
    if [token.folded for token in left] != ["метаданные", ".", "имя"]:
        return None
    same = configuration.casefold() == right[0].value.casefold()
    if op == "=":
        return "true" if same else "false"
    if op == "<>":
        return "false" if same else "true"
    return None


def _variant_compare(
    left: tuple[Token, ...],
    op: str,
    right: tuple[Token, ...],
    env: _Env,
    module: _Module,
    reader: _Reader,
) -> str | None:
    if len(left) != 1 or left[0].folded != "вариантнастройки":
        return None
    current = env.locals.get("вариантнастройки", _UNKNOWN)
    if not isinstance(current, str):
        return None
    expected, kind = reader._const_or_string(right, module, _current_plan(module))
    if kind not in ("string", "const") or expected is None:
        return None
    same = current == expected
    if op == "=":
        return "true" if same else "false"
    if op == "<>":
        return "false" if same else "true"
    return None


def _default_settings() -> _Settings:
    versions = _Map("versions")
    extensions = _Map("extensions")
    source = RouteSource("", 0, 0, "НастройкиПланаОбменаПоУмолчанию")
    return _Settings(
        {
            "этопланобменаxdto": False,
            "форматобмена": "",
            "версииформатаобмена": versions,
            "правиларегистрациивменеджере": False,
            "имяменеджерарегистрации": "",
            "расширенияформатаобмена": extensions,
            "правиларегистрациивменеджере#source": source,
        },
        dict.fromkeys(_KNOWN_ALGORITHMS, False),
        [],
    )


def _replace_map(settings: _Settings, field_name: str, new_map: _Map, role: str) -> None:
    if new_map.role is None:
        new_map.role = role
    old = settings.values.get(field_name)
    if isinstance(old, _Map) and old is not new_map:
        for entry in old.entries:
            if entry.state == "effective":
                entry.state = "overwritten"
        settings.retained.append(old)
    settings.values[field_name] = new_map


def _register_maps(env: _Env, settings: _Settings) -> None:
    for value in settings.values.values():
        if isinstance(value, _Map):
            env.maps.append(value)


def _entries_of(current: object, settings: _Settings) -> list[RouteEntry]:
    maps = [item for item in settings.retained if item.role == "versions"]
    if isinstance(current, _Map):
        maps.append(current)
    return [entry for item in maps for entry in _route_entries(item)]


def _extensions_of(settings: _Settings) -> list[FormatExtension]:
    maps = [item for item in settings.retained if item.role == "extensions"]
    current = settings.values.get("расширенияформатаобмена")
    if isinstance(current, _Map):
        maps.append(current)
    return [item for receiver in maps for item in _extension_entries(receiver)]


def _route_entries(receiver: _Map) -> list[RouteEntry]:
    return [
        RouteEntry(
            item.key_raw, item.key, item.value, item.source, item.conditions, _state(item.state)
        )
        for item in receiver.entries
        if item.value_kind == "module"
    ]


def _extension_entries(receiver: _Map) -> list[FormatExtension]:
    return [
        FormatExtension(item.key, item.value or "", item.source, _state(item.state))
        for item in receiver.entries
        if item.value_kind == "version" and item.value is not None
    ]


def _as_kind(kind: str) -> ConditionKind:
    if kind == "literal_variant":
        return "literal_variant"
    if kind == "static_metadata":
        return "static_metadata"
    return "opaque"


def _as_value(value: str) -> ConditionValue:
    if value == "true":
        return "true"
    if value == "false":
        return "false"
    return "unknown"


def _state(value: str) -> EntryState:
    if value == "effective":
        return "effective"
    if value == "overwritten":
        return "overwritten"
    if value == "unreachable":
        return "unreachable"
    return "conditional"


def _flag(settings: _Settings, name: str) -> bool | None:
    value = settings.values.get(name)
    if isinstance(value, bool):
        return value
    return None


def _setting_source(settings: _Settings, name: str) -> RouteSource | None:
    value = settings.values.get(f"{name}#source")
    return value if isinstance(value, RouteSource) else None


def _min_version(entries: Sequence[RouteEntry]) -> tuple[str | None, tuple[str, ...]]:
    """Минимум пустого узла. Неподдержанная форма ключа — минимум не подтверждён.

    Ключ сохраняется в карте как есть: версию не «чинят» и не выбрасывают из сравнения
    исполнителя (спецификация §1.2).
    """
    keys = []
    seen: set[str] = set()
    for entry in entries:
        if entry.state == "effective" and entry.key not in seen:
            seen.add(entry.key)
            keys.append(entry.key)
    if not keys or any(_version_parts(key) is None for key in keys):
        return None, ()
    minima = [keys[0]]
    for key in keys[1:]:
        compared = compare_versions(key, minima[0])
        if compared is None:
            continue
        if compared < 0:
            minima = [key]
        elif compared == 0:
            minima.append(key)
    if len(minima) == 1:
        return minima[0], ()
    return None, tuple(minima)


def _empty_plan(
    name: str,
    metadata_path: str,
    status: RouteStatus,
    is_ed: bool | None,
    registration: RegistrationProfile,
) -> PlanRoute:
    plan_status: RouteStatus = "partial" if status == "partial" else "complete"
    return PlanRoute(
        name,
        metadata_path,
        is_ed,
        None,
        None,
        (),
        registration,
        (),
        (),
        plan_status,
    )


def _routines(source: SourceFile, lexical: Lexed) -> dict[str, _Routine]:
    statements = lexical.statements
    starts = [token.start for token in lexical.tokens]
    found: dict[str, _Routine] = {}
    index = 0
    while index < len(statements):
        statement = statements[index]
        if statement.head not in ("процедура", "функция"):
            index += 1
            continue
        name, params = _header(statement)
        index += 1
        body: list[Statement] = []
        ending = "конецпроцедуры" if statement.head == "процедура" else "конецфункции"
        while index < len(statements) and statements[index].head != ending:
            body.append(statements[index])
            index += 1
        end = index
        if index < len(statements):
            index += 1
        start_char = statement.span.char_start
        end_char = (
            statements[end].span.char_end if end < len(statements) else statement.span.char_end
        )
        left = bisect_left(starts, start_char)
        right = bisect_left(starts, end_char)
        tokens = lexical.tokens[left:right]
        closed = True
        try:
            nodes, _cursor = _parse_block(body, 0, set())
        except _Unclosed:
            nodes = []
            closed = False
        if name:
            found[name.casefold()] = _Routine(
                name,
                "procedure" if statement.head == "процедура" else "function",
                params,
                nodes,
                tuple(body),
                tokens,
                source,
                statement.span,
                closed,
            )
    return found


def _header(statement: Statement) -> tuple[str, tuple[_Param, ...]]:
    tokens = _bare(statement.tokens)
    if len(tokens) < 2 or tokens[1].kind != "identifier":
        return "", ()
    name = tokens[1].value
    try:
        open_at = next(index for index, token in enumerate(tokens) if token.value == "(")
        close_at = next(index for index, token in enumerate(tokens) if token.value == ")")
    except StopIteration:
        return name, ()
    try:
        parts = split_arguments(tokens[open_at + 1 : close_at])
    except EdFormatError:
        return name, ()
    params: list[_Param] = []
    for part in parts:
        if not part:
            continue
        cursor = 0
        by_value = part[0].folded == "знач"
        if by_value:
            cursor = 1
        if cursor >= len(part) or part[cursor].kind != "identifier":
            continue
        param_name = part[cursor].value
        default: tuple[Token, ...] = ()
        if cursor + 1 < len(part) and part[cursor + 1].value == "=":
            default = tuple(part[cursor + 2 :])
        params.append(_Param(param_name, by_value, default))
    return name, tuple(params)


def _parse_block(
    statements: Sequence[Statement], index: int, stop: set[str]
) -> tuple[list[_Node], int]:
    nodes: list[_Node] = []
    while index < len(statements):
        statement = statements[index]
        head = _block_head(statement)
        if head in stop:
            break
        if head == "#если":
            node, index = _parse_pre_if(statements, index)
            nodes.append(node)
            continue
        if head == "если":
            node, index = _parse_if(statements, index)
            nodes.append(node)
            continue
        if head in ("пока", "для"):
            node, index = _parse_until(statements, index, "конеццикла")
            nodes.append(_Loop(node, statement))
            continue
        if head == "попытка":
            node, index = _parse_try(statements, index)
            nodes.append(node)
            continue
        if head in ("#иначе", "#иначеесли", "#конецесли"):
            index += 1
            continue
        if head in (
            "иначе",
            "иначеесли",
            "конецесли",
            "конеццикла",
            "исключение",
            "конецпопытки",
        ):
            break
        nodes.append(_Stmt(statement))
        index += 1
    return nodes, index


def _parse_if(statements: Sequence[Statement], index: int) -> tuple[_If, int]:
    clauses: list[tuple[tuple[Token, ...], list[_Node], Statement]] = []
    else_body: list[_Node] = []
    else_statement: Statement | None = None
    while index < len(statements) and statements[index].head in ("если", "иначеесли"):
        header = statements[index]
        tokens = _bare(header.tokens)
        condition = tokens[1:-1] if len(tokens) >= 2 else ()
        index += 1
        body, index = _parse_block(statements, index, {"иначеесли", "иначе", "конецесли"})
        clauses.append((condition, body, header))
    if index < len(statements) and statements[index].head == "иначе":
        else_statement = statements[index]
        index += 1
        else_body, index = _parse_block(statements, index, {"конецесли"})
    if index >= len(statements) or statements[index].head != "конецесли":
        raise _Unclosed
    return _If(clauses, else_body, else_statement), index + 1


def _parse_pre_if(statements: Sequence[Statement], index: int) -> tuple[_If, int]:
    clauses: list[tuple[tuple[Token, ...], list[_Node], Statement]] = []
    else_body: list[_Node] = []
    else_statement: Statement | None = None
    stops = {"#иначеесли", "#иначе", "#конецесли"}
    while index < len(statements) and _block_head(statements[index]) in ("#если", "#иначеесли"):
        header = statements[index]
        index += 1
        body, index = _parse_block(statements, index, stops)
        clauses.append((header.tokens, body, header))
    if index < len(statements) and _block_head(statements[index]) == "#иначе":
        else_statement = statements[index]
        index += 1
        else_body, index = _parse_block(statements, index, {"#конецесли"})
    if index >= len(statements) or _block_head(statements[index]) != "#конецесли":
        raise _Unclosed
    return _If(clauses, else_body, else_statement, True), index + 1


def _directive_word(statement: Statement) -> str:
    if not statement.tokens or statement.tokens[0].kind != "directive":
        return ""
    return statement.tokens[0].value.split(None, 1)[0].casefold()


def _block_head(statement: Statement) -> str:
    return _directive_word(statement) or statement.head


def _parse_until(
    statements: Sequence[Statement], index: int, ending: str
) -> tuple[list[_Node], int]:
    index += 1
    body, index = _parse_block(statements, index, {ending})
    if index >= len(statements) or statements[index].head != ending:
        raise _Unclosed
    return body, index + 1


def _parse_try(statements: Sequence[Statement], index: int) -> tuple[_Try, int]:
    header = statements[index]
    index += 1
    body, index = _parse_block(statements, index, {"исключение", "конецпопытки"})
    handler: list[_Node] = []
    if index < len(statements) and statements[index].head == "исключение":
        index += 1
        handler, index = _parse_block(statements, index, {"конецпопытки"})
    if index >= len(statements) or statements[index].head != "конецпопытки":
        raise _Unclosed
    return _Try(body, handler, header), index + 1


def _assignment(tokens: tuple[Token, ...]) -> tuple[tuple[Token, ...], tuple[Token, ...]] | None:
    depth = 0
    for index, token in enumerate(tokens):
        if token.value in ("(", "["):
            depth += 1
        elif token.value in (")", "]"):
            depth -= 1
        elif token.value == "=" and depth == 0 and token.kind == "symbol":
            return tokens[:index], tokens[index + 1 :]
    return None


def _call(
    tokens: tuple[Token, ...],
) -> tuple[tuple[Token, ...], tuple[tuple[Token, ...], ...]] | None:
    if len(tokens) < 3 or tokens[-1].value != ")":
        return None
    depth = 0
    open_at = None
    for index in range(len(tokens) - 1, -1, -1):
        if tokens[index].value == ")":
            depth += 1
        elif tokens[index].value == "(":
            depth -= 1
            if depth == 0:
                open_at = index
                break
    if open_at is None:
        return None
    try:
        arguments = split_arguments(tokens[open_at + 1 : -1])
    except EdFormatError:
        return None
    return tokens[:open_at], arguments


def _dotted(tokens: tuple[Token, ...]) -> tuple[str, ...] | None:
    if not tokens or len(tokens) % 2 == 0:
        return None
    parts: list[str] = []
    for index, token in enumerate(tokens):
        if index % 2 == 0:
            if token.kind != "identifier":
                return None
            parts.append(token.value)
        elif token.value != ".":
            return None
    return tuple(parts)


def _is_new_map(tokens: tuple[Token, ...]) -> bool:
    if len(tokens) < 2 or tokens[0].folded != "новый" or tokens[1].folded != "соответствие":
        return False
    return len(tokens) == 2 or (
        len(tokens) == 4 and tokens[2].value == "(" and tokens[3].value == ")"
    )


def _split_keyword(tokens: tuple[Token, ...], keyword: str) -> list[tuple[Token, ...]]:
    parts: list[tuple[Token, ...]] = []
    depth = 0
    start = 0
    for index, token in enumerate(tokens):
        if token.value in ("(", "["):
            depth += 1
        elif token.value in (")", "]"):
            depth -= 1
        elif depth == 0 and token.kind == "identifier" and token.folded == keyword:
            parts.append(tokens[start:index])
            start = index + 1
    parts.append(tokens[start:])
    return parts


def _comparison(
    tokens: tuple[Token, ...],
) -> tuple[tuple[Token, ...], str, tuple[Token, ...]] | None:
    depth = 0
    for index, token in enumerate(tokens):
        if token.value in ("(", "["):
            depth += 1
        elif token.value in (")", "]"):
            depth -= 1
        elif depth == 0 and token.value in ("=", "<>"):
            return tokens[:index], token.value, tokens[index + 1 :]
    return None


def _wraps(tokens: tuple[Token, ...]) -> bool:
    depth = 0
    for index, token in enumerate(tokens):
        if token.value == "(":
            depth += 1
        elif token.value == ")":
            depth -= 1
            if depth == 0:
                return index == len(tokens) - 1
    return False


def _count_windows(tokens: tuple[Token, ...], pattern: tuple[str, ...]) -> int:
    width = len(pattern)
    return sum(
        1
        for index in range(len(tokens) - width + 1)
        if tuple(token.folded for token in tokens[index : index + width]) == pattern
    )


def _procedure_tokens(module: _Module, name: str) -> tuple[Token, ...]:
    routine = module.routines.get(name.casefold())
    return () if routine is None else routine.tokens


def _current_plan(module: _Module) -> str | None:
    parts = module.file.file_id.split("/")
    if len(parts) >= 2 and parts[0] == "ExchangePlans":
        return parts[1]
    return None


def _passes_tracked(tokens: tuple[Token, ...], env: _Env) -> bool:
    for token in tokens:
        if token.kind == "identifier" and isinstance(
            env.locals.get(token.folded), _Map | _Settings
        ):
            return True
    return False


def _args_pass_tracked(arguments: tuple[tuple[Token, ...], ...], env: _Env) -> bool:
    return any(_passes_tracked(argument, env) for argument in arguments)


def _bound_settings(env: _Env, name: str) -> _Settings | None:
    value = env.locals.get(name)
    return value if isinstance(value, _Settings) else None


def _affects_route(nodes: Sequence[object], env: _Env) -> bool:
    """Ветка значима, если способна оборвать чтение или изменить карту и настройки."""
    names = {name for name, value in env.locals.items() if isinstance(value, _Map | _Settings)}
    for statement in _iter_statements(nodes):
        if statement.head in ("возврат", "вызватьисключение"):
            return True
        for token in statement.tokens:
            if token.kind != "identifier":
                continue
            if token.folded in names or token.folded in _KNOWN_SETTINGS | _KNOWN_ALGORITHMS:
                return True
    return False


def _settings_writes(nodes: Sequence[object], env: _Env) -> set[str]:
    names = {name for name, value in env.locals.items() if isinstance(value, _Settings)}
    found: set[str] = set()
    for statement in _iter_statements(nodes):
        tokens = _bare(statement.tokens)
        assigned = _assignment(tokens)
        if assigned is not None:
            lhs, rhs = assigned
            parts = _dotted(lhs)
            if (
                parts
                and len(parts) == 1
                and len(rhs) == 1
                and rhs[0].kind == "identifier"
                and rhs[0].folded in names
            ):
                names.add(parts[0].casefold())
            if parts:
                folded = tuple(part.casefold() for part in parts)
                if folded and folded[0] in names:
                    if len(folded) == 2 and folded[1] in _KNOWN_SETTINGS:
                        found.add(folded[1])
                    elif (
                        len(folded) == 3
                        and folded[1] == "алгоритмы"
                        and folded[2] in _KNOWN_ALGORITHMS
                    ):
                        found.add(folded[2])
        call = _call(tokens)
        if call is None:
            continue
        callee = _dotted(call[0])
        if callee is None or len(callee) != 2 or callee[-1].casefold() != "вставить":
            continue
        if callee[0].casefold() not in names or not call[1]:
            continue
        key = call[1][0]
        if len(key) == 1 and key[0].kind == "string":
            field_name = key[0].value.casefold()
            if field_name in _KNOWN_SETTINGS or field_name in _KNOWN_ALGORITHMS:
                found.add(field_name)
    return found


def _mark_unread(settings: _Settings, field_name: str) -> None:
    if field_name in _KNOWN_ALGORITHMS:
        settings.algorithms[field_name] = _UNREAD
        return
    old = settings.values.get(field_name)
    if isinstance(old, _Map):
        for entry in old.entries:
            if entry.state == "effective":
                entry.state = "overwritten"
        if old not in settings.retained:
            settings.retained.append(old)
    settings.values[field_name] = _UNREAD


def _iter_statements(nodes: Sequence[object]) -> Iterable[Statement]:
    for node in nodes:
        if isinstance(node, _Stmt):
            yield node.statement
        elif isinstance(node, _If):
            for _, body, _ in node.clauses:
                yield from _iter_statements(body)
            yield from _iter_statements(node.else_body)
        elif isinstance(node, _Loop):
            yield from _iter_statements(node.body)
        elif isinstance(node, _Try):
            yield from _iter_statements(node.body)
            yield from _iter_statements(node.handler)


def _source(
    file: SourceFile, span: SourceSpan, procedure: str, chain: tuple[SourceSpan, ...]
) -> RouteSource:
    return RouteSource(
        file.file_id, span.line_start, span.line_end, procedure, chain, "base", file.sha256
    )


def _span_of(condition: RouteCondition) -> SourceSpan:
    return SourceSpan(
        condition.source.relative_file, condition.source.line_start, condition.source.line_end, 0, 0
    )


def _flip(condition: RouteCondition) -> RouteCondition:
    value: ConditionValue = "unknown"
    if condition.value == "true":
        value = "false"
    elif condition.value == "false":
        value = "true"
    return RouteCondition(condition.kind, f"Не ({condition.raw})", value, condition.source)


@dataclass
class _Shot:
    locals: dict[str, object]
    map_ids: tuple[int, ...]
    entries: dict[int, list[_Live]]
    roles: dict[int, str | None]
    values: dict[str, object]
    retained: list[_Map]
    algorithms: dict[str, object]


def _snapshot(env: _Env) -> _Shot:
    return _Shot(
        dict(env.locals),
        tuple(id(item) for item in env.maps),
        {id(item): [copy(entry) for entry in item.entries] for item in env.maps},
        {id(item): item.role for item in env.maps},
        {} if env.settings is None else dict(env.settings.values),
        [] if env.settings is None else list(env.settings.retained),
        {} if env.settings is None else dict(env.settings.algorithms),
    )


def _restore(env: _Env, saved: _Shot) -> None:
    env.locals.clear()
    env.locals.update(saved.locals)
    by_id = {id(item): item for item in env.maps}
    env.maps[:] = [by_id[item] for item in saved.map_ids if item in by_id]
    for item in env.maps:
        item.role = saved.roles.get(id(item))
        item.entries[:] = [copy(entry) for entry in saved.entries.get(id(item), [])]
    if env.settings is not None:
        env.settings.values.clear()
        env.settings.values.update(saved.values)
        env.settings.retained[:] = saved.retained
        env.settings.algorithms.clear()
        env.settings.algorithms.update(saved.algorithms)


def _fresh_entries(env: _Env, saved: _Shot) -> list[tuple[_Map, _Live]]:
    before = {entry.ident for entries in saved.entries.values() for entry in entries}
    found: list[tuple[_Map, _Live]] = []
    for item in env.maps:
        for entry in item.entries:
            if entry.ident not in before:
                found.append((item, copy(entry)))
    return found


def _merge_kind(left: str, right: str) -> str:
    if "opaque" in (left, right):
        return "opaque"
    if "static_metadata" in (left, right):
        return "static_metadata"
    return "literal_variant"


def _tokens_of_conditions(stack: Sequence[RouteCondition]) -> tuple[Token, ...]:
    return tuple(token for item in stack for token in tokenize(item.raw))


def _correspondent_literal(tokens: tuple[Token, ...]) -> str | None:
    folded = [token.folded for token in tokens]
    for index in range(len(tokens) - 4):
        if folded[index : index + 3] != ["параметрыконтекста", ".", "имякорреспондента"]:
            continue
        if tokens[index + 3].value == "=" and tokens[index + 4].kind == "string":
            return tokens[index + 4].value
    return None


def _source_file(file_id: str, path: str, text: str, raw: bytes) -> SourceFile:
    lines = text.splitlines(keepends=True)
    offsets: list[int] = []
    position = 0
    for line in lines:
        offsets.append(position)
        position += len(line)
    newline = (
        "mixed"
        if "\r\n" in text and "\n" in text.replace("\r\n", "")
        else ("\r\n" if "\r\n" in text else "\n")
    )
    return SourceFile(
        file_id,
        path,
        text,
        hashlib.sha256(raw).hexdigest(),
        tuple(offsets),
        bom=raw.startswith(b"\xef\xbb\xbf"),
        newline=newline,
    )


def _relative(root: Path, path: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        try:
            return path.resolve().relative_to(root).as_posix()
        except ValueError:
            return path.name


def _find_child(node: ET._Element, name: str) -> ET._Element | None:
    for child in node:
        if _local(child.tag) == name:
            return child
    return None


def _named_children(node: ET._Element, name: str) -> tuple[str, ...]:
    objects = _find_child(node, "ChildObjects")
    if objects is None:
        return ()
    return tuple(
        (child.text or "").strip() for child in objects if _local(child.tag) == name and child.text
    )


def _template_names(node: ET._Element) -> tuple[str, ...]:
    plan = _find_child(node, "ExchangePlan")
    if plan is None:
        return ()
    return _named_children(plan, "Template")


def _include_flag(node: ET._Element) -> bool | None:
    subsystem = _find_child(node, "Subsystem")
    properties = None if subsystem is None else _find_child(subsystem, "Properties")
    if properties is None:
        return None
    flag = _find_child(properties, "IncludeInCommandInterface")
    if flag is None or flag.text is None:
        return None
    text = flag.text.strip()
    if text == "true":
        return True
    if text == "false":
        return False
    return None


def _base_version(namespace: str) -> bool:
    prefix = ED_BASE + "/"
    if not namespace.startswith(prefix):
        return False
    tail = namespace[len(prefix) :]
    if "/" in tail:
        return False
    parts = tail.split(".")
    return len(parts) == 2 and all(part.isdigit() and part for part in parts)
