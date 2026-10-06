"""Авторинг ED: прочитанные снимки, ограниченный кэш входов и запись только в workspace."""

import hashlib
import json
import logging
import os
import shutil
import tempfile
from collections import OrderedDict, defaultdict
from collections.abc import Mapping
from contextlib import contextmanager, suppress
from contextvars import ContextVar
from dataclasses import asdict, dataclass, field, fields, replace
from functools import wraps
from pathlib import Path
from time import perf_counter
from typing import TYPE_CHECKING, Any, cast

from kd2_rules_mcp.authoring.ed.artifacts import (
    artifact_name,
    combine_artifacts,
    previous_artifact,
    with_source_hashes,
)
from kd2_rules_mcp.authoring.ed.candidates import candidates, target_objects
from kd2_rules_mcp.authoring.ed.canonical import canonical_property
from kd2_rules_mcp.authoring.ed.context import AuthoringContext
from kd2_rules_mcp.authoring.ed.handler_render import procedure_block
from kd2_rules_mcp.authoring.ed.handlers import (
    HandlerOperationsPlan,
    merge_operations,
    operation_from_input,
)
from kd2_rules_mcp.authoring.ed.identity import IdentityMap
from kd2_rules_mcp.authoring.ed.manifest import ArtifactManifest, json_bytes, sha256
from kd2_rules_mcp.authoring.ed.model import (
    AddAlgorithmicHeaderProperty,
    AddHeaderProperty,
    AuthoringInputs,
    AuthoringPreconditionError,
    AuthoringTarget,
    ExtensionIdentity,
    Failure,
    Notice,
    Operation,
    PreparedAuthoring,
    PreserveMissingHeaderProperty,
    ProfileComparison,
    ProfileReport,
    SetObjectHandler,
    SourceSet,
    digest,
)
from kd2_rules_mcp.authoring.ed.operations import validate_preconditions
from kd2_rules_mcp.authoring.ed.render import (
    RenderedAuthoring,
    render_authoring,
    render_handlers_authoring,
)
from kd2_rules_mcp.authoring.ed.xml_dump import M, parse_xml, read_description
from kd2_rules_mcp.ed.address import AmbiguousAddressError, EntityNotFoundError, build_addresses
from kd2_rules_mcp.ed.layer_model import LayerDescriptor
from kd2_rules_mcp.ed.layer_reader import read_extension_text
from kd2_rules_mcp.ed.layers import compose_manager
from kd2_rules_mcp.ed.lexer import lex
from kd2_rules_mcp.ed.model import Classification, ObjectRule
from kd2_rules_mcp.ed.refs import build_references
from kd2_rules_mcp.ed.schema import EdSchema
from kd2_rules_mcp.errors import (
    EdAuthoringAckRequiredError,
    EdAuthoringIoError,
    EdAuthoringPathError,
    EdAuthoringPreconditionError,
    EdAuthoringResourceLimitError,
    EdAuthoringStaleError,
)
from kd2_rules_mcp.projects import resolve
from kd2_rules_mcp.service import ed_authoring_views as views
from kd2_rules_mcp.service.ed import EdMixin
from kd2_rules_mcp.service.ed_reopen import with_reopen_hints
from kd2_rules_mcp.service.ed_routes import (
    EdRoutesMixin,
    _file_stamp,
    _load_for_uri,
    _RouteSnapshot,
)
from kd2_rules_mcp.service.ed_schema import EdSchemaMixin
from kd2_rules_mcp.service.ed_views import validate_page
from kd2_rules_mcp.service.paths import Settings
from kd2_rules_mcp.validation.ed_authoring import (
    check_profile,
    compare_reports,
    enforce_delta,
    own_property_version_notice,
    prepare_authoring,
    prepare_handler_operations,
)
from kd2_rules_mcp.validation.ed_authoring_handlers import preset_procedure_text
from kd2_rules_mcp.validation.ed_layers import (
    validate_effective_links,
    validate_layers,
)
from kd2_rules_mcp.validation.ed_projection import effective_document, select_context
from kd2_rules_mcp.validation.ed_routes import _format_uri
from kd2_rules_mcp.validation.ed_structure_snapshot import metadata_key

if TYPE_CHECKING:
    from kd2_rules_mcp.service.ed_writer import EdWriterMixin

MAX_OPERATIONS = 100
MAX_MANAGERS = 16
MAX_PROFILES = 64
MAX_FILES = 1000
MAX_BYTES = 32 * 1024 * 1024
MAX_INPUTS = 32
MAX_INPUT_BYTES = 128 * 1024 * 1024
MAX_HANDLER_BODY_BYTES = 1024 * 1024

logger = logging.getLogger(__name__)


@dataclass
class _Timing:
    phases: dict[str, float] = field(
        default_factory=lambda: dict.fromkeys(
            ("inputs", "routes", "schemas", "prepare", "render"), 0.0
        )
    )
    files_read: int = 0
    routes_checked: set[int] = field(default_factory=set)
    catalogs: dict[Path, Any] = field(default_factory=dict)
    catalog_stamps: dict[Path, tuple[int, int] | None] = field(default_factory=dict)


_timing: ContextVar[_Timing | None] = ContextVar("ed_authoring_timing", default=None)


@contextmanager
def _phase(name: str):
    start = perf_counter()
    timing = _timing.get()
    child_before = sum(timing.phases.values()) if timing else 0.0
    try:
        yield
    finally:
        if timing:
            # Вложенные маршруты/схемы исключены из времени входов.
            timing.phases[name] += (
                perf_counter() - start - (sum(timing.phases.values()) - child_before)
            )


def _read_count(count: int = 1) -> None:
    if timing := _timing.get():
        timing.files_read += count


def _timed_build(function):
    @wraps(function)
    def call(*args, **kwargs):
        timing = _Timing()
        token = _timing.set(timing)
        start = perf_counter()
        try:
            return function(*args, **kwargs)
        finally:
            logger.info(
                "ed_authoring_build total=%.3fs inputs=%.3fs routes=%.3fs schemas=%.3fs "
                "prepare=%.3fs render=%.3fs files_read=%d",
                perf_counter() - start,
                *(timing.phases[k] for k in timing.phases),
                timing.files_read,
            )
            _timing.reset(token)

    return call


@dataclass(slots=True)
class _Inputs:
    value: AuthoringInputs
    files: dict[Path, tuple[int, int] | None]
    route: _RouteSnapshot
    roots: tuple[Path, ...]
    structure_id: str
    structure_fingerprint: str
    stored_bytes: int
    descriptions: dict[str, str]
    prepared_key: str = ""
    prepared: PreparedAuthoring | HandlerOperationsPlan | None = None


@dataclass(slots=True)
class _CachedSchema:
    value: EdSchema | str
    files: dict[Path, tuple[int, int] | None]
    stored_bytes: int


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name}: нужна непустая строка")
    return value


def _mapping(value: object, name: str, allowed: set[str]) -> dict:
    if not isinstance(value, dict) or set(value) - allowed:
        raise ValueError(f"{name}: неверный набор полей")
    return value


def _failure(error: AuthoringPreconditionError) -> EdAuthoringPreconditionError:
    return EdAuthoringPreconditionError(
        "Предусловия авторинга не выполнены",
        {
            "failures": [
                {
                    "id": f.id,
                    "address": f.address,
                    "message": f.message,
                    "source": {"file": f.file, "line": f.line},
                }
                for f in error.failures
            ],
            "summary": {"failures": len(error.failures)},
        },
    )


class EdAuthoringMixin(EdRoutesMixin, EdSchemaMixin, EdMixin):
    """Входы кэшируются, проекта авторинга и draft_id в хранилище нет."""

    def __init__(self, settings: Settings) -> None:
        super().__init__(settings)
        self._authoring_inputs: OrderedDict[tuple, _Inputs] = OrderedDict()
        self._authoring_input_bytes = 0
        self._authoring_schemas: OrderedDict[tuple[str, str], _CachedSchema] = OrderedDict()
        self._authoring_schema_bytes = 0
        self._authoring_previews: OrderedDict[str, ArtifactManifest] = OrderedDict()
        self._authoring_validated: OrderedDict[str, RenderedAuthoring] = OrderedDict()
        self._authoring_validated_bytes = 0

    def _catalog(self):
        """Один и тот же projects.yaml не открываем для каждой выдаваемой строки пути."""
        timing = _timing.get()
        if timing is None:
            return super()._catalog()
        path = self.settings.projects_file
        if path not in timing.catalogs:
            timing.catalog_stamps[path] = _file_stamp(path)
            _read_count()
            timing.catalogs[path] = super()._catalog()
        return timing.catalogs[path]

    def _authoring_target(
        self, raw: object, project: str, configuration: str
    ) -> tuple[AuthoringTarget, tuple[str, str, str]]:
        value = _mapping(
            raw,
            "target",
            {
                "plan",
                "variant",
                "format_version",
                "direction",
                "pko_address",
                "project_id",
                "schema_id",
                "structure_id",
            },
        )
        try:
            refs = tuple(_text(value[k], k) for k in ("project_id", "schema_id", "structure_id"))
            variant = value.get("variant")
            if variant is not None:
                variant = _text(variant, "variant")
            direction = _text(value["direction"], "direction")
            if direction not in ("send", "receive"):
                raise ValueError("direction: send или receive")
            target = AuthoringTarget(
                project,
                configuration,
                _text(value["plan"], "plan"),
                variant,
                _text(value["format_version"], "format_version"),
                direction,
                _text(value["pko_address"], "pko_address"),
            )
        except KeyError as error:
            raise ValueError(f"Отсутствует поле target: {error.args[0]}") from error
        return target, (refs[0], refs[1], refs[2])

    def _authoring_operation(
        self, raw: object, project: str, configuration: str
    ) -> tuple[AddHeaderProperty, tuple[str, str, str]]:
        """Совместимость прежних внутренних потребителей прямой ПКС."""
        operation, refs = self._operation(raw, project, configuration)
        if not isinstance(operation, AddHeaderProperty):
            raise ValueError("Ожидалась прямая ПКС")
        return operation, refs

    def _operation(
        self, raw: object, project: str, configuration: str
    ) -> tuple[Operation, tuple[str, str, str]]:
        if not isinstance(raw, dict):
            raise ValueError("operation: нужен объект")
        try:
            if raw.get("new_attribute") is not None and (
                not isinstance(raw["new_attribute"], dict)
                or not isinstance(raw["new_attribute"].get("qualifiers"), dict)
            ):
                raise ValueError("new_attribute и qualifiers: нужны объекты")
            target, refs = self._authoring_target(raw["target"], project, configuration)
            operation = operation_from_input(raw | {"target": asdict(target)})
            # Форму декодирует ядро; здесь только транспортные типы значений DTO.
            for descriptor in fields(operation):
                key, value = descriptor.name, getattr(operation, descriptor.name)
                if key not in ("target", "new_attribute") and not isinstance(value, str):
                    raise ValueError("Поля операции должны быть строками")
            if isinstance(operation, AddHeaderProperty) and operation.new_attribute:
                draft = operation.new_attribute
                _text(draft.name, "name")
                _text(draft.synonym, "synonym")
                if draft.primitive not in ("string", "boolean", "number", "date"):
                    raise ValueError("primitive: string, boolean, number или date")
            if isinstance(operation, (AddHeaderProperty, AddAlgorithmicHeaderProperty)):
                _text(operation.configuration_attribute, "configuration_attribute")
                _text(operation.format_property, "format_property")
            if isinstance(operation, PreserveMissingHeaderProperty):
                _text(operation.property_operation_id, "property_operation_id")
            if isinstance(operation, AddAlgorithmicHeaderProperty):
                _text(operation.handler_operation_id, "handler_operation_id")
            return operation, refs
        except (KeyError, TypeError, AttributeError) as error:
            raise ValueError("Неверные поля операции авторинга") from error

    @staticmethod
    def _hash_file(path: Path) -> str:
        _read_count()
        with path.open("rb") as stream:
            return hashlib.file_digest(stream, "sha256").hexdigest()

    def _verify_inputs(self, entry: _Inputs) -> None:
        changed = []
        try:
            changed = [self._host(p) for p, stamp in entry.files.items() if _file_stamp(p) != stamp]
            if timing := _timing.get():
                changed.extend(
                    self._host(p)
                    for p, stamp in timing.catalog_stamps.items()
                    if _file_stamp(p) != stamp
                )
            meta = self.store.meta(entry.structure_id)
            if meta.get("input_hash") != entry.structure_fingerprint:
                changed.append("structure:" + entry.structure_id)
        except OSError as error:
            raise EdAuthoringStaleError(
                "Входные файлы недоступны; перечитайте снимки", {}
            ) from error
        if changed:
            raise EdAuthoringStaleError(
                "Входные файлы изменились; перечитайте снимки", {"changed": changed}
            )

    def _inputs_for(
        self,
        project: str,
        configuration: str,
        refs: tuple[str, str, str],
        operations: tuple[Operation, ...] = (),
    ) -> _Inputs:
        # Открытые идентификаторы проверяются даже при попадании в кэш.
        doc_project = self._ed_project(refs[0])
        if doc_project.layered is not None:
            raise ValueError(
                "Авторинг снимка со слоями пока не поддерживается; "
                "откройте менеджер без расширений (extensions=[])"
            )
        schema_project = self._schema_project(refs[1])
        self._require_structure(refs[2])
        key = (project, configuration, *refs)
        cached = self._authoring_inputs.get(key)
        if cached is not None and (
            cached.value.document is not doc_project.document
            or cached.value.schemas.get(schema_project.format_version) is not schema_project.schema
            or cached.structure_fingerprint != self.store.meta(refs[2]).get("input_hash")
            or self._route_roots.get(str(cached.route.root)) != cached.route.profile.profile_id
        ):
            # Вызывающий перечитал снимки: старый кэш не препятствует новой подготовке.
            del self._authoring_inputs[key]
            self._authoring_input_bytes -= cached.stored_bytes
            cached = None
        if cached is not None:
            self._verify_inputs(cached)
            if _timing.get() is None:
                self._check_route(cached.route)
            self._extension_sources(cached, operations, doc_project.index)
            self._authoring_inputs.move_to_end(key)
            return cached
        root = self._project_root(project, configuration)
        with _phase("routes"):
            route, reused, stale = self._open_snapshot(
                root,
                project,
                configuration,
                False,
                documents={p.path: p.document for p in self._ed_projects.values()},
                read_files_only=True,
                verify_read_files=False,
            )
            if not reused:
                _read_count(
                    len(route.file_hashes)
                    - sum(
                        p in route.file_hashes for p in (d.path for d in self._ed_projects.values())
                    )
                )
        if stale:
            raise EdAuthoringStaleError(
                "Снимок маршрутов изменился; повторите ed_routes с force", {}
            )
        config = self._catalog().configuration(project, configuration)
        folder = self.settings.project_dirs[project]
        roots = (root, *(resolve(folder, p).resolve() for p in config.extensions))
        extension_names = []
        extension_stamps = {}
        extension_sources: dict[str, str] = {}
        for number, extension_root in enumerate(roots[1:]):
            path = extension_root / "Configuration.xml"
            before = _file_stamp(path)
            _read_count()
            text = path.read_text("utf-8-sig")
            xml = parse_xml("Configuration.xml", text)[0]
            extension_sources[f"extensions/{number}/Configuration.xml"] = text
            if _file_stamp(path) != before:
                raise EdAuthoringStaleError("Описание расширения изменилось во время чтения", {})
            extension_stamps[path] = before
            extension_names.append(xml.findtext(f"{{{M}}}Properties/{{{M}}}Name", ""))
        meta = self.store.meta(refs[2])
        expected = meta.get("input_hash", "")
        if (
            meta.get("source") != "xml"
            or Path(meta.get("source_path", "")).resolve() != root
            or json.loads(meta.get("extensions", "[]")) != extension_names
        ):
            raise _failure(
                AuthoringPreconditionError(
                    (
                        Failure(
                            "ed.author.snapshot_mismatch",
                            "",
                            "Структура не соответствует выбранной выгрузке и расширениям",
                        ),
                    )
                )
            )
        document = doc_project.document
        selected = schema_project.schema
        if not all(
            Path(s.path).resolve().is_relative_to(root / "XDTOPackages")
            for s in selected.packages[0].sources
        ):
            raise AuthoringPreconditionError(
                (
                    Failure(
                        "ed.author.snapshot_mismatch",
                        "",
                        "Выбранная схема прочитана из другой выгрузки",
                    ),
                )
            )
        paths: dict[Path, str] = {doc_project.path: document.files[0].sha256}
        if not reused:
            # В новый профиль могли войти и другие открытые менеджеры. Их кэш тоже
            # подтверждаем SHA один раз, прежде чем доверять его версии интерфейса.
            paths.update(
                {
                    p.path: p.document.files[0].sha256
                    for p in self._ed_projects.values()
                    if p.path in route.file_hashes
                }
            )
        paths[self.settings.projects_file.resolve()] = self._hash_file(self.settings.projects_file)
        schemas: dict[str, EdSchema | str] = {schema_project.format_version: selected}
        manager = next(
            (
                m
                for m in route.profile.managers
                if m.path and (root / m.path).resolve() == doc_project.path
            ),
            None,
        )
        versions = {
            e.key
            for e in (
                *(e for p in route.profile.plans for e in p.entries),
                *route.profile.without_node_entries,
            )
            if manager and e.manager_name and e.manager_name.casefold() == manager.name.casefold()
        }
        self._limit(len(versions), MAX_PROFILES, "профили версий")
        for version in sorted(versions - {schema_project.format_version}):
            uris = {
                uri
                for plan in route.profile.plans
                if any(
                    e.key == version and manager and e.manager_name == manager.name
                    for e in plan.entries
                )
                if (uri := _format_uri(plan.base_namespace, version)) is not None
            }
            if len(uris) == 1:
                uri = next(iter(uris))
                cache_key = (route.profile.profile_id, uri)
                with _phase("schemas"):
                    cached_schema = self._authoring_schemas.get(cache_key)
                    if cached_schema and any(
                        _file_stamp(p) != stamp for p, stamp in cached_schema.files.items()
                    ):
                        self._authoring_schema_bytes -= cached_schema.stored_bytes
                        del self._authoring_schemas[cache_key]
                    if cache_key not in self._authoring_schemas:
                        loaded = _load_for_uri(route, uri)
                        schema_files = {
                            root / (p.package_path or p.description_path): _file_stamp(
                                root / (p.package_path or p.description_path)
                            )
                            for p in route.profile.packages
                            if p.namespace == uri
                        }
                        size = 0
                        if isinstance(loaded, EdSchema):
                            _read_count(sum(len(p.sources) for p in loaded.packages))
                            schema_files.update(
                                {
                                    Path(s.path): _file_stamp(Path(s.path))
                                    for p in loaded.packages
                                    for s in p.sources
                                }
                            )
                            size = sum(s.bytes for p in loaded.packages for s in p.sources)
                        self._authoring_schemas[cache_key] = _CachedSchema(
                            loaded if isinstance(loaded, EdSchema) else loaded.reason,
                            schema_files,
                            size,
                        )
                        self._authoring_schema_bytes += size
                        while (
                            len(self._authoring_schemas) > MAX_PROFILES
                            or self._authoring_schema_bytes > MAX_INPUT_BYTES
                        ):
                            _, removed = self._authoring_schemas.popitem(last=False)
                            self._authoring_schema_bytes -= removed.stored_bytes
                            if not self._authoring_schemas:
                                break
                        if cache_key not in self._authoring_schemas:
                            schemas[version] = (
                                loaded if isinstance(loaded, EdSchema) else loaded.reason
                            )
                            continue
                    self._authoring_schemas.move_to_end(cache_key)
                    schemas[version] = self._authoring_schemas[cache_key].value
            else:
                schemas[version] = "Пакет версии отсутствует или неоднозначен"
        for schema in schemas.values():
            if isinstance(schema, EdSchema):
                paths.update({Path(s.path): s.sha256 for p in schema.packages for s in p.sources})
        # Сверка уже открытых снимков один раз; далее — размер/mtime прочитанных файлов.
        stamps = dict(extension_stamps)
        for path, fingerprint in paths.items():
            before = _file_stamp(path)
            try:
                actual = self._hash_file(path)
            except OSError as error:
                raise EdAuthoringStaleError(
                    "Открытый снимок недоступен; перечитайте входы", {}
                ) from error
            if actual != fingerprint or before != _file_stamp(path):
                raise EdAuthoringStaleError(
                    "Открытый снимок изменился; перечитайте входы", {"changed": [self._host(path)]}
                )
            stamps[path] = before
        structure, _ = self._ed_structure_snapshot(refs[2])
        source = SourceSet(
            project,
            configuration,
            digest(
                (
                    document.parser_version,
                    document.manager_version,
                    tuple((f.path, f.sha256) for f in document.files),
                )
            ),
            digest(
                tuple(
                    (v, s.schema_id if isinstance(s, EdSchema) else s)
                    for v, s in sorted(schemas.items())
                )
            ),
            expected,
            route.profile.sources_fingerprint,
            tuple(config.extensions),
            digest(extension_sources),
        )
        value = AuthoringInputs(
            document, schemas, structure, route.profile, source, extension_sources
        )
        size = (
            sum(len(s.text.encode("utf-8")) for s in document.files)
            + sum(
                s.bytes
                for schema in schemas.values()
                if isinstance(schema, EdSchema)
                for p in schema.packages
                for s in p.sources
            )
            + sum(len(t.encode("utf-8")) for t in extension_sources.values())
        )
        self._limit(size, MAX_INPUT_BYTES, "байты входов")
        for (profile_id, _), cached_schema in self._authoring_schemas.items():
            if profile_id == route.profile.profile_id:
                stamps.update(cached_schema.files)
        # Маркеры выгрузки — известные пути; снимок структуры не инвентаризируем вновь.
        for source_root in roots:
            for name in ("Configuration.xml", "ConfigDumpInfo.xml"):
                stamps.setdefault(source_root / name, _file_stamp(source_root / name))
        entry = _Inputs(value, stamps, route, roots, refs[2], expected, size, {})
        self._extension_sources(entry, operations, doc_project.index)
        self._verify_inputs(entry)
        if _timing.get() is None:
            self._check_route(entry.route)
        while self._authoring_inputs and (
            len(self._authoring_inputs) >= MAX_INPUTS
            or self._authoring_input_bytes + entry.stored_bytes > MAX_INPUT_BYTES
        ):
            _, old = self._authoring_inputs.popitem(last=False)
            self._authoring_input_bytes -= old.stored_bytes
        self._authoring_inputs[key] = entry
        self._authoring_input_bytes += entry.stored_bytes
        return entry

    def _check_route(self, route: _RouteSnapshot) -> None:
        """Проверяем открытый профиль один раз за вызов, по его прочитанным файлам."""
        timing = _timing.get()
        if timing and id(route) in timing.routes_checked:
            return
        with _phase("routes"):
            if route.stale or any(_file_stamp(p) != stamp for p, stamp in route.read_files.items()):
                raise EdAuthoringStaleError(
                    "Снимок маршрутов изменился; повторите ed_routes с force", {}
                )
        if timing:
            timing.routes_checked.add(id(route))

    @staticmethod
    def _description(entry: _Inputs, name: str) -> str:
        if name not in entry.descriptions:
            path = entry.route.root / name
            before = _file_stamp(path)
            _read_count()
            text = path.read_text("utf-8-sig")
            if before != _file_stamp(path) or (path in entry.files and before != entry.files[path]):
                raise EdAuthoringStaleError(
                    "Описание метаданных изменилось", {"changed": [str(path)]}
                )
            entry.files[path] = before
            entry.descriptions[name] = text
        return entry.descriptions[name]

    def _extension_sources(self, entry: _Inputs, operations: tuple[Operation, ...], index) -> None:
        names = {entry.value.document.files[0].path.replace("\\", "/").split("CommonModules/")[-1]}
        for op in operations:
            if isinstance(op, AddHeaderProperty) and op.new_attribute:
                try:
                    rule = index.find(op.target.pko_address)
                except (EntityNotFoundError, AmbiguousAddressError):
                    continue
                key, _ = metadata_key(rule.configuration_object.value)
                if key and (owner := entry.value.structure.objects.get(key)):
                    names.add(
                        ("Catalogs/" if key[0] == "справочник" else "Documents/")
                        + owner.name
                        + ".xml"
                    )
        names = {"CommonModules/" + n if n.endswith("/Ext/Module.bsl") else n for n in names}
        sources = dict(entry.value.extension_sources)
        for number, root in enumerate(entry.roots[1:]):
            for name in sorted(names):
                path = root / name
                if path in entry.files:
                    continue
                before = _file_stamp(path)
                entry.files[path] = before
                if before is not None:
                    _read_count()
                    sources[f"extensions/{number}/{name}"] = path.read_text("utf-8-sig")
                    if _file_stamp(path) != before:
                        raise EdAuthoringStaleError("Расширение изменилось во время чтения", {})
        if sources != entry.value.extension_sources:
            size = sum(len(t.encode("utf-8")) for t in sources.values())
            old_size = sum(len(t.encode("utf-8")) for t in entry.value.extension_sources.values())
            self._limit(entry.stored_bytes + size - old_size, MAX_INPUT_BYTES, "байты входов")
            entry.stored_bytes += size - old_size
            if any(e is entry for e in self._authoring_inputs.values()):
                self._authoring_input_bytes += size - old_size
            entry.value = replace(
                entry.value,
                extension_sources=sources,
                source_set=replace(entry.value.source_set, extensions_hash=digest(sources)),
                input_fingerprints=replace(entry.value.source_set, extensions_hash=digest(sources)),
            )
            entry.prepared = None

    @staticmethod
    def _limit(count: int, maximum: int, name: str) -> None:
        if count > maximum:
            raise EdAuthoringResourceLimitError(
                f"Превышен предел: {name} ({maximum})",
                {"resource": name, "limit": maximum, "actual": count},
            )

    @with_reopen_hints
    def ed_authoring_candidates(
        self,
        target: dict,
        kind: str,
        text: str = "",
        offset: int = 0,
        limit: int = 50,
        configuration_attribute: str | None = None,
        format_property: str | None = None,
        scope: str | None = None,
        reference_document_id: str | None = None,
    ) -> dict[str, Any]:
        if scope == "manager":
            if configuration_attribute is not None or format_property is not None:
                raise ValueError("scope=manager не принимает поля прежнего авторинга")
            return cast("EdWriterMixin", self)._manager_candidates(
                target, kind, text, offset, limit, reference_document_id
            )
        if scope not in (None, "overlay") or reference_document_id is not None:
            raise ValueError("scope: overlay или manager; reference_document_id только для manager")
        validate_page(offset, limit)
        if kind not in ("format", "configuration") or not isinstance(text, str):
            raise ValueError("kind: format или configuration; text: строка")
        value = dict(
            _mapping(
                target,
                "target",
                {
                    "project",
                    "configuration",
                    "plan",
                    "variant",
                    "format_version",
                    "direction",
                    "pko_address",
                    "project_id",
                    "schema_id",
                    "structure_id",
                },
            )
        )
        project = _text(value.pop("project", None), "project")
        configuration = _text(value.pop("configuration", None), "configuration")
        resolved, refs = self._authoring_target(value, project, configuration)
        for name, item in (
            ("configuration_attribute", configuration_attribute),
            ("format_property", format_property),
        ):
            if item is not None:
                _text(item, name)
        with self._lock:
            try:
                entry = self._inputs_for(project, configuration, refs)
                self._require_selected_schema(resolved, refs, entry.value)
                try:
                    rule, profile, applicable, typ, owner = target_objects(entry.value, resolved)
                except (EntityNotFoundError, AmbiguousAddressError) as error:
                    identifier = (
                        "pko_ambiguous"
                        if isinstance(error, AmbiguousAddressError)
                        else "pko_missing"
                    )
                    raise AuthoringPreconditionError(
                        (
                            Failure(
                                "ed.author." + identifier,
                                resolved.pko_address,
                                str(error),
                                self._host(self._ed_project(refs[0]).path),
                                1,
                            ),
                        )
                    ) from error
                skipped = []
                state = applicable.evaluate(rule, resolved.direction)
                if typ is None or owner is None or state is not True:
                    skipped.append(
                        {
                            "check": "ed.author.candidates",
                            "reason": "Тип, владелец или применимость ПКО не подтверждены",
                        }
                    )
                if owner and configuration_attribute:
                    actual = owner.property(configuration_attribute.strip(" "))
                    if len(actual) == 1:
                        configuration_attribute = actual[0].path
                if typ and format_property:
                    format_property = (
                        canonical_property(profile, typ, format_property) or format_property
                    )
                canonical_target = dict(target)
                from kd2_rules_mcp.ed.address import build_addresses

                canonical_target["pko_address"] = build_addresses(entry.value.document).by_id[
                    rule.entity_id
                ][0]
                canonical_target["plan"] = next(
                    (
                        p.plan_name
                        for p in entry.value.routes.plans
                        if p.plan_name.casefold() == resolved.plan.casefold()
                    ),
                    resolved.plan,
                )
                found = candidates(
                    entry.value,
                    resolved,
                    kind,
                    configuration_attribute=configuration_attribute,
                    format_property=format_property,
                    text=text,
                )
                rows = [
                    {
                        "name": c.name,
                        "path": c.path,
                        "type": list(c.types),
                        "compatible": c.compatible,
                        "reason": c.reason,
                    }
                    for c in found
                ]
                return views.compact_page(
                    {
                        "target": canonical_target,
                        "scope": {"version_scope": "manager"},
                        "auto": False,
                        "skipped": skipped,
                    },
                    rows,
                    offset,
                    limit,
                )
            except AuthoringPreconditionError as error:
                raise _failure(error) from error
            except (OSError, UnicodeError) as error:
                raise EdAuthoringIoError(
                    "Ошибка чтения входов авторинга",
                    {"path": self._host_text(str(getattr(error, "filename", "") or ""))},
                ) from error

    def _descriptions(
        self, entry: _Inputs, operations: tuple[AddHeaderProperty, ...]
    ) -> dict[str, str]:
        needed = {"Configuration.xml"}
        module = (
            entry.value.document.files[0]
            .path.replace("\\", "/")
            .split("CommonModules/")[-1]
            .split("/")[0]
        )
        needed.add("CommonModules/" + module + ".xml")
        configuration_text = self._description(entry, "Configuration.xml")
        language = parse_xml("Configuration.xml", configuration_text)[0].findtext(
            f"{{{M}}}Properties/{{{M}}}DefaultLanguage", ""
        )
        needed.add("Languages/" + language.removeprefix("Language.") + ".xml")
        from kd2_rules_mcp.ed.address import build_addresses

        index = build_addresses(entry.value.document)
        for op in operations:
            if op.new_attribute:
                rule = index.find(op.target.pko_address)
                from kd2_rules_mcp.ed.model import ObjectRule

                assert isinstance(rule, ObjectRule)
                key, _ = metadata_key(rule.configuration_object.value)
                if key:
                    owner = entry.value.structure.objects.get(key)
                    if owner:
                        kind = "Catalogs" if key[0] == "справочник" else "Documents"
                        needed.add(kind + "/" + owner.name + ".xml")
        return {name: self._description(entry, name) for name in sorted(needed)}

    def _require_selected_schema(
        self, target: AuthoringTarget, refs: tuple[str, str, str], inputs: AuthoringInputs
    ) -> None:
        selected = self._schema_project(refs[1])
        plans = [p for p in inputs.routes.plans if p.plan_name.casefold() == target.plan.casefold()]
        expected_uri = (
            _format_uri(plans[0].base_namespace, target.format_version) if len(plans) == 1 else None
        )
        if selected.format_version != target.format_version or (
            expected_uri is not None and selected.schema.base_namespace != expected_uri
        ):
            raise AuthoringPreconditionError(
                (
                    Failure(
                        "ed.author.snapshot_mismatch",
                        target.pko_address,
                        "schema_id не соответствует выбранной версии и URI маршрута",
                    ),
                )
            )

    def _destination(self, name: str, output_dir: str | None) -> Path:
        root = self.workspace.root.absolute()
        target = root / "ed-authoring" / name
        if output_dir is not None:
            _text(output_dir, "output_dir")
            supplied = self._local(output_dir)
            if supplied is None:
                raise EdAuthoringPathError("Каталог комплекта вне workspace", {})
            supplied = supplied if supplied.is_absolute() else root / supplied
            if supplied.absolute() != target.absolute():
                raise EdAuthoringPathError("Разрешён только вычисленный каталог комплекта", {})
        self._safe_path(target)
        return target

    def _safe_path(self, target: Path) -> None:
        root = self.workspace.root.absolute()
        if not target.absolute().is_relative_to(root):
            raise EdAuthoringPathError("Каталог комплекта вне workspace", {})
        for item in (target, *target.parents):
            if item.is_symlink() or item.is_junction():
                raise EdAuthoringPathError("Символические ссылки и junction запрещены", {})
        if not target.resolve().is_relative_to(root.resolve()):
            raise EdAuthoringPathError("Каталог комплекта вне workspace", {})

    def _previous(self, destination: Path) -> tuple[ArtifactManifest | None, dict[str, bytes]]:
        self._safe_path(destination)
        if not destination.exists():
            return None, {}
        if not destination.is_dir():
            raise EdAuthoringPathError("Каталог комплекта занят чужим файлом", {})
        files = {}
        for path in destination.rglob("*"):
            self._safe_path(path)
            if path.is_file():
                files[path.relative_to(destination).as_posix()] = path.read_bytes()
                self._limit(len(files), MAX_FILES, "файлы прежнего комплекта")
                self._limit(
                    sum(len(b) for b in files.values()), MAX_BYTES, "байты прежнего комплекта"
                )
        if files and "manifest.json" not in files:
            raise EdAuthoringPathError("Каталог содержит чужие файлы", {})
        if "manifest.json" in files:
            manifest = ArtifactManifest.from_bytes(files["manifest.json"])
            if set(files) - {*manifest.file_hashes, "manifest.json"}:
                raise EdAuthoringPathError("Каталог содержит чужие файлы", {})
            allowed_dirs = {
                parent.as_posix()
                for name in manifest.file_hashes
                for parent in Path(name).parents
                if str(parent) != "."
            }
            if any(
                p.relative_to(destination).as_posix() not in allowed_dirs
                for p in destination.rglob("*")
                if p.is_dir()
            ):
                raise EdAuthoringPathError("Каталог содержит чужие подкаталоги", {})
        elif any(destination.iterdir()):
            raise EdAuthoringPathError("Каталог содержит чужие подкаталоги", {})
        return previous_artifact(files), files

    def _write_artifact(
        self,
        destination: Path,
        files: Mapping[str, bytes],
        previous: Mapping[str, bytes],
        entries: list[_Inputs],
        *,
        previous_reader=None,
        verify=None,
    ) -> str:
        previous_reader = previous_reader or self._previous
        self._safe_path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix=".staging-", dir=destination.parent))
        backup = staging.with_name(staging.name + "-previous")
        moved = False
        try:
            for name, content in files.items():
                path = staging / name
                self._safe_path(path)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(content)
            for entry in entries:
                self._verify_inputs(entry)
                self._check_route(entry.route)
            if verify is not None:
                verify()
            try:
                _, current = previous_reader(destination)
            except (AuthoringPreconditionError, EdAuthoringPathError) as error:
                raise EdAuthoringStaleError(
                    "Прежний комплект изменился во время записи", {}
                ) from error
            if current != dict(previous):
                raise EdAuthoringStaleError("Прежний комплект изменился во время записи", {})
            if dict(files) == current:
                return "unchanged"
            self._safe_path(destination)
            if destination.exists():
                os.replace(destination, backup)
                moved = True
            try:
                if moved:
                    try:
                        _, saved = previous_reader(backup)
                        if saved != dict(previous):
                            raise EdAuthoringStaleError("Комплект изменился при переименовании", {})
                    except (AuthoringPreconditionError, EdAuthoringPathError) as error:
                        raise EdAuthoringStaleError(
                            "Комплект изменился при переименовании", {}
                        ) from error
                os.replace(staging, destination)
            except (OSError, EdAuthoringStaleError):
                if moved:
                    os.replace(backup, destination)
                    moved = False
                raise
            if moved:
                # Публикация завершена; недоступный для удаления backup не отменяет её.
                with suppress(OSError):
                    shutil.rmtree(backup)
            return "written"
        finally:
            if staging.exists():
                shutil.rmtree(staging)

    def _previous_refs(
        self, operation: Operation, entries: dict[tuple[str, str, str], _Inputs]
    ) -> tuple[str, str, str]:
        """После перезапуска прежние решения восстанавливаются по карте, без хранимого draft."""
        target = operation.target
        if entries:
            entry = next(iter(entries.values()))
            route, structure_id = entry.route, entry.structure_id
        else:
            root = self._project_root(target.project, target.configuration)
            route, _, stale = self._open_snapshot(root, target.project, target.configuration, False)
            if stale:
                raise EdAuthoringStaleError(
                    "Снимок маршрутов изменился; повторите ed_routes с force", {}
                )
            config = self._catalog().configuration(target.project, target.configuration)
            folder = self.settings.project_dirs[target.project]
            structure_id = "ed-authoring-" + digest((target.project, target.configuration))[:24]
            self.store.load_xml(
                structure_id, root, [resolve(folder, path) for path in config.extensions]
            )
        plans = [p for p in route.profile.plans if p.plan_name.casefold() == target.plan.casefold()]
        names = {
            e.manager_name
            for p in plans
            for e in p.entries
            if e.key == target.format_version and e.state == "effective" and e.manager_name
        }
        managers = [m for m in route.profile.managers if m.name in names and m.path]
        uris = {_format_uri(p.base_namespace, target.format_version) for p in plans}
        packages = [p for p in route.profile.packages if p.namespace in uris]
        if len(managers) != 1 or len(packages) != 1:
            raise AuthoringPreconditionError(
                (
                    Failure(
                        "ed.author.route_unresolved",
                        target.pko_address,
                        "Прежний менеджер или выбранная схема не разрешены однозначно",
                    ),
                )
            )
        opened = self.ed_open(self._host(route.root / (managers[0].path or "")))
        if opened["source_changed"]:
            self.ed_close(opened["project_id"])
            opened = self.ed_open(self._host(route.root / (managers[0].path or "")))
        schema = self.ed_schema_open(
            target.format_version,
            project=target.project,
            configuration=target.configuration,
            package=packages[0].metadata_name,
        )
        if schema["source_changed"]:
            self.ed_schema_close(schema["schema_id"])
            schema = self.ed_schema_open(
                target.format_version,
                project=target.project,
                configuration=target.configuration,
                package=packages[0].metadata_name,
            )
        return opened["project_id"], schema["schema_id"], structure_id

    def _previous_failure(self, error: AuthoringPreconditionError, operation: Operation):
        mapped = _failure(error)
        for failure in mapped.details["failures"]:
            failure.update(operation_id=operation.operation_id, from_previous=True)
            failure["source"]["file"] = self._host_text(failure["source"]["file"])
        return mapped

    @staticmethod
    def _source_hashes(
        entry: _Inputs, descriptions: Mapping[str, str] | None = None
    ) -> dict[str, str]:
        """Отпечатки прочитанных входов: пути относительно выгрузки, без обхода диска."""
        sources = {
            p: sha256(t.encode("utf-8"))
            for p, t in (descriptions if descriptions is not None else entry.descriptions).items()
        }
        known = dict(entry.route.file_hashes)
        known.update({Path(f.path): f.sha256 for f in entry.value.document.files})
        for schema in entry.value.schemas.values():
            if isinstance(schema, EdSchema):
                known.update({Path(s.path): s.sha256 for p in schema.packages for s in p.sources})
        for path, fingerprint in known.items():
            if path.is_relative_to(entry.roots[0]):
                sources[path.relative_to(entry.roots[0]).as_posix()] = fingerprint
        sources.update(
            {p: sha256(t.encode("utf-8")) for p, t in entry.value.extension_sources.items()}
        )
        return sources

    def _handler_limits(self, plan: HandlerOperationsPlan) -> None:
        bodies = [op.body for op in plan.operations if isinstance(op, SetObjectHandler)]
        self._limit(
            sum(len(b.encode("utf-8")) for b in bodies), MAX_HANDLER_BODY_BYTES, "байты тел"
        )
        self._limit(len(plan.bindings), 100, "слоты обработчиков")
        for body in bodies:
            self._limit(len(body.encode("utf-8")), 64 * 1024, "байты тела")
            self._limit(len(body.splitlines()), 2000, "строки тела")

    def _validate_handler_layer(
        self, entry: _Inputs, bundle: RenderedAuthoring, plan: HandlerOperationsPlan
    ) -> RenderedAuthoring:
        """Читатель слоя проверяет именно порождённый текст, включая расширения проекта.

        Полный отчёт входит в отпечаток через validation.json. Свидетельства пилота
        шаблона хранятся отдельно от runtime_verified конкретного комплекта.
        """
        document = entry.value.document
        cache_key = digest(
            (
                tuple((p, sha256(b)) for p, b in bundle.files.items()),
                plan.runtime_verified,
                tuple(b.runtime_verified for b in plan.bindings),
                entry.value.source_set,
            )
        )
        if cached := self._authoring_validated.get(cache_key):
            self._authoring_validated.move_to_end(cache_key)
            return cached
        version = document.manager_version
        module_path = bundle.prepared.generated_hook.source.path.removeprefix("modules/")
        flagged = {
            (d.span.file_id, d.span.char_start)
            for d in document.diagnostics
            if d.code == "helper_semantics_unverified"
        }
        helpers = frozenset(
            r.name.casefold()
            for r in document.routines
            if r.name.casefold() in ("добавитьпкс", "добавитьпктч")
            and (r.span.file_id, r.span.char_start) not in flagged
        )
        targets = {r.name.casefold(): r for r in document.routines}
        readings = []
        for number, root in enumerate(entry.roots[1:]):
            key = f"extensions/{number}/{module_path}"
            if key in entry.value.extension_sources:
                readings.append(
                    read_extension_text(
                        entry.value.extension_sources[key],
                        layer=LayerDescriptor(
                            f"L{number + 1:02d}", number + 1, root.name, str(root), None, ""
                        ),
                        version=version,
                        helpers=helpers,
                        targets=targets,
                        path=str(root / module_path),
                        file_id=key,
                    )
                )
        before_layer = compose_manager(document, readings=readings, version=version)
        layer_id = "authoring"
        generated = read_extension_text(
            bundle.files["modules/" + module_path].decode("utf-8"),
            layer=LayerDescriptor(
                layer_id, len(entry.roots), bundle.manifest.identity.name, "<generated>", None, ""
            ),
            version=version,
            helpers=helpers,
            targets=targets,
            path="modules/" + module_path,
            file_id=layer_id + ":" + module_path,
        )
        after_layer = compose_manager(document, readings=[*readings, generated], version=version)
        by_id = {op.operation_id: op for op in plan.operations}
        body_bytes = 0
        for binding in plan.bindings:
            operations = [by_id[ident] for ident in binding.operation_ids]
            if isinstance(operations[0], SetObjectHandler):
                body = operations[0].body
                expected = f"Процедура {binding.handler_name}({', '.join(binding.parameters)})\n"
                if binding.previous_name:
                    expected += (
                        f"\t{binding.previous_name}({', '.join(binding.previous_arguments)});\n"
                    )
                expected += body + "КонецПроцедуры\n"
            else:
                pairs = []
                for operation in operations:
                    assert isinstance(operation, PreserveMissingHeaderProperty)
                    prop = by_id[operation.property_operation_id]
                    assert isinstance(prop, AddHeaderProperty)
                    pairs.append((prop.format_property, prop.configuration_attribute))
                expected = preset_procedure_text(binding.handler_name, tuple(pairs))
                body = expected[expected.index("\n") + 1 : expected.rindex("КонецПроцедуры")]
            self._limit(len(body.encode("utf-8")), 64 * 1024, "байты тела")
            self._limit(len(body.splitlines()), 2000, "строки тела")
            body_bytes += len(body.encode("utf-8"))
            if procedure_block(generated.source.text, binding.handler_name) != expected:
                raise AuthoringPreconditionError(
                    (
                        Failure(
                            "ed.author.new_issues",
                            binding.target.pko_address,
                            "Тело прочитанной процедуры отличается от плана",
                        ),
                    )
                )
        self._limit(body_bytes, MAX_HANDLER_BODY_BYTES, "байты тел")
        unknown = generated.coverage.line_classes.count(Classification.UNKNOWN)
        unresolved = 0
        certain = not generated.skips and not after_layer.skipped
        base_index = build_addresses(document)
        body_known = body_unparsed = 0
        source_map = []
        documents, links = {}, {}
        for direction in ("send", "receive"):
            before_context = select_context(before_layer, direction)
            after_context = select_context(after_layer, direction)
            certain &= not after_context.taints and all(
                e.certainty == "known" for e in after_context.entities
            )
            chains = {c.target_name: c for c in after_context.dispatch_chains}
            for binding in plan.bindings:
                if binding.target.direction == direction:
                    chain = chains.get(binding.handler_name)
                    unresolved += int(chain is None or chain.resolution != "call")
            before_doc = effective_document(before_layer, before_context)
            after_doc = effective_document(after_layer, after_context)
            documents[direction] = (before_doc, after_doc)
            links[direction] = (
                validate_effective_links(before_layer, before_context),
                validate_effective_links(after_layer, after_context),
            )
            enforce_delta(
                compare_reports(
                    ProfileReport(
                        "links",
                        direction,
                        tuple(links[direction][0].issues),
                        tuple(links[direction][0].skipped),
                    ),
                    ProfileReport(
                        "links",
                        direction,
                        tuple(links[direction][1].issues),
                        tuple(links[direction][1].skipped),
                    ),
                ),
                plan.operations,
                document,
            )
            # Объявленные изменения сопоставляем с действующим состоянием обоих направлений.
            if len(before_doc.pko) != len(after_doc.pko):
                raise AuthoringPreconditionError(
                    (Failure("ed.author.new_issues", "", "Изменился состав ПКО"),)
                )
            for old, current in zip(before_doc.pko, after_doc.pko, strict=True):

                def prop_key(prop):
                    return (
                        prop.configuration_property,
                        prop.format_property,
                        prop.algorithm_flag,
                        prop.conversion_rule,
                        prop.namespace,
                        prop.condition_name,
                    )

                expected_properties = []
                expected_events = {b.event: b.target_name for b in old.events}
                for op in plan.operations:
                    if (
                        op.target.direction != direction
                        or base_index.find(op.target.pko_address).entity_id != old.entity_id
                    ):
                        continue
                    if isinstance(op, (AddHeaderProperty, AddAlgorithmicHeaderProperty)):
                        expected_properties.append(
                            (
                                op.configuration_attribute,
                                op.format_property,
                                int(isinstance(op, AddAlgorithmicHeaderProperty)),
                                op.conversion_rule
                                if isinstance(op, AddAlgorithmicHeaderProperty)
                                else "",
                                "",
                                "",
                            )
                        )
                for binding in plan.bindings:
                    if (
                        binding.target.direction == direction
                        and base_index.find(binding.target.pko_address).entity_id == old.entity_id
                    ):
                        expected_events[binding.event] = binding.handler_name
                from collections import Counter

                if (
                    Counter(map(prop_key, current.properties))
                    != Counter(map(prop_key, old.properties)) + Counter(expected_properties)
                    or {b.event: b.target_name for b in current.events} != expected_events
                    or any(
                        getattr(old, key) != getattr(current, key)
                        for key in (
                            "name",
                            "procedure_name",
                            "declared_name",
                            "configuration_object",
                            "format_object",
                            "group_flag",
                            "identification",
                            "groups",
                            "search_sets",
                            "extensions",
                        )
                    )
                ):
                    raise AuthoringPreconditionError(
                        (
                            Failure(
                                "ed.author.new_issues",
                                "",
                                "Состав прочитанного слоя отличается от плана",
                            ),
                        )
                    )
            if any(
                getattr(before_doc, key) != getattr(after_doc, key)
                for key in ("pod", "pkpd", "parameters", "rule_uses")
            ):
                raise AuthoringPreconditionError(
                    (Failure("ed.author.new_issues", "", "Изменились незатронутые правила"),)
                )
            if version == 3:
                headers_before = effective_document(
                    before_layer, select_context(before_layer, direction, True)
                )
                headers_after = effective_document(
                    after_layer, select_context(after_layer, direction, True)
                )
                if headers_before.pko != headers_after.pko:
                    raise AuthoringPreconditionError(
                        (Failure("ed.author.new_issues", "", "Изменились правила headers_only"),)
                    )
            refs = build_references(after_doc)
            owned_ids = {
                r.entity_id
                for r in after_doc.routines
                if r.span.file_id == generated.source.file_id and "handler" in r.roles
            }
            body_known += sum(r.owner_id in owned_ids and r.name is not None for r in refs.entries)
            body_unparsed += sum(refs.unparsed_by_owner.get(i, 0) for i in owned_ids)
        layer_before = validate_layers(before_layer)
        layer_after = validate_layers(after_layer)
        layer_delta = compare_reports(
            ProfileReport("layer", "both", tuple(layer_before.issues), tuple(layer_before.skipped)),
            ProfileReport("layer", "both", tuple(layer_after.issues), tuple(layer_after.skipped)),
        )
        if not certain or unknown or unresolved:
            raise AuthoringPreconditionError(
                (
                    Failure(
                        "ed.author.new_issues",
                        "",
                        "Порождённый слой содержит unknown, taint "
                        "или неразрешённую диспетчеризацию",
                    ),
                )
            )
        enforce_delta(layer_delta, plan.operations, document)
        selected = {(op.target.format_version, op.target.direction) for op in plan.operations}
        comparisons, other_comparisons, expected_skipped = [], [], []
        authoring_context = AuthoringContext(
            replace(entry.value, structure=bundle.prepared.structure_after)
        )
        authoring_context.target_ids = frozenset(
            base_index.find(op.target.pko_address).entity_id for op in plan.operations
        )
        for key, schema in sorted(entry.value.schemas.items()):
            if not isinstance(schema, EdSchema):
                continue
            for direction in sorted({op.target.direction for op in plan.operations}):
                reports = []
                before_doc, after_doc = documents[direction]
                for doc, link_report in zip((before_doc, after_doc), links[direction], strict=True):
                    reports.append(
                        check_profile(
                            doc if (key, direction) in selected else authoring_context.scope(doc),
                            schema,
                            bundle.prepared.structure_after,
                            key,
                            direction,
                            context=authoring_context,
                            links=link_report,
                            full_skip_addresses=True,
                        )
                    )
                after_index = build_addresses(after_doc)
                logical = {
                    address: base_index.by_id[ident][0]
                    for ident, addresses in after_index.by_id.items()
                    if ident in base_index.by_id
                    for address in addresses
                }
                proven, algorithmic_addresses, relevant = [], [], []
                for op in plan.operations:
                    if (
                        isinstance(op, (AddHeaderProperty, AddAlgorithmicHeaderProperty))
                        and op.target.direction == direction
                    ):
                        rule = after_index.find(op.target.pko_address)
                        assert isinstance(rule, ObjectRule)
                        for prop in rule.properties:
                            if (
                                prop.configuration_property == op.configuration_attribute
                                and prop.format_property == op.format_property
                            ):
                                proven.append(after_index.by_id[prop.entity_id][0])
                                relevant.append(after_index.by_id[prop.entity_id][0])
                                if isinstance(op, AddAlgorithmicHeaderProperty):
                                    algorithmic_addresses.append(
                                        after_index.by_id[prop.entity_id][0]
                                    )
                for binding in plan.bindings:
                    if binding.target.direction == direction:
                        rule = after_index.find(binding.target.pko_address)
                        assert isinstance(rule, ObjectRule)
                        relevant.extend(
                            after_index.by_id.get(
                                b.entity_id,
                                (binding.target.pko_address + "/Обработчик/" + binding.event,),
                            )[0]
                            for b in rule.events
                            if b.event == binding.event
                        )
                delta = compare_reports(
                    *reports,
                    logical_addresses=logical,
                    relevant_addresses=tuple(relevant),
                    proven_type_addresses=tuple(proven),
                )
                for item in reports[1].skipped:
                    reason = item.reason.partition("; ")[0].rpartition(": ")[0]
                    if item.check == "ed.schema.type_incompatible" and reason in (
                        "handler_may_supply",
                        "non_atomic_type",
                    ):
                        for address in algorithmic_addresses:
                            if address in item.reason.partition("; ")[2].split(", "):
                                expected_skipped.append(
                                    {
                                        "check": item.check,
                                        "reason": reason,
                                        "address": address,
                                        "format_version": key,
                                        "direction": direction,
                                    }
                                )
                comparison = ProfileComparison(reports[0], reports[1], delta)
                if (key, direction) in selected:
                    enforce_delta(delta, plan.operations, document)
                    comparisons.append(comparison)
                else:
                    other_comparisons.append(comparison)
        for op in plan.operations:
            binding = next((b for b in plan.bindings if op.operation_id in b.operation_ids), None)
            routine = next(
                (r for r in generated.routines if binding and r.name == binding.handler_name), None
            )
            if routine:
                source_map.append(
                    {
                        "operation_id": op.operation_id,
                        "file": routine.span.file_id,
                        "line_start": routine.span.line_start,
                        "line_end": routine.span.line_end,
                    }
                )
            elif isinstance(op, (AddHeaderProperty, AddAlgorithmicHeaderProperty)):
                _, after_doc = documents[op.target.direction]
                rule = build_addresses(after_doc).find(op.target.pko_address)
                assert isinstance(rule, ObjectRule)
                prop = next(
                    p
                    for p in rule.properties
                    if p.configuration_property == op.configuration_attribute
                    and p.format_property == op.format_property
                )
                source_map.append(
                    {
                        "operation_id": op.operation_id,
                        "file": prop.span.file_id,
                        "line_start": prop.span.line_start,
                        "line_end": prop.span.line_end,
                    }
                )
        previous_calls = []
        statements = lex(generated.source).statements
        for binding in plan.bindings:
            if not binding.previous_name:
                continue
            routine = next(r for r in generated.routines if r.name == binding.handler_name)
            calls = [
                s
                for s in statements
                if routine.body_span.char_start <= s.span.char_start < routine.body_span.char_end
                and len(s.tokens) > 1
                and s.tokens[0].folded == binding.previous_name.casefold()
                and s.tokens[1].value == "("
            ]
            if len(calls) != 1:
                raise AuthoringPreconditionError(
                    (
                        Failure(
                            "ed.author.new_issues",
                            binding.target.pko_address,
                            "Прежний обработчик должен вызываться один раз",
                        ),
                    )
                )
            rule = base_index.find(binding.target.pko_address)
            assert isinstance(rule, ObjectRule)
            previous_calls.append(
                {
                    "target_name": binding.previous_name,
                    "rule_name": rule.declared_name or rule.name,
                    "event": binding.event,
                    "call_count": len(calls),
                    "source": {"file": "modules/" + module_path, "line": calls[0].span.line_start},
                }
            )
        expected_previous = {b.previous_name for b in plan.bindings if b.previous_name}
        for call in generated.previous_calls:
            if call.target_name not in expected_previous or call.call_count != 1:
                raise AuthoringPreconditionError(
                    (
                        Failure(
                            "ed.author.new_issues",
                            "",
                            "Прежний обработчик вызывается иначе, чем задано планом",
                        ),
                    )
                )
        preserved = {
            op.property_operation_id
            for op in plan.operations
            if isinstance(op, PreserveMissingHeaderProperty)
        }
        notices = {
            n.notice_id: n
            for n in bundle.prepared.notices
            if not (n.id == "ed.author.missing_value_clears" and n.operation_id in preserved)
        }
        for notice in plan.notices:
            # Перечень версий value_range уже собран подготовкой прямой ПКС.
            if notice.id == "ed.author.value_range" and notice.notice_id in notices:
                continue
            notices[notice.notice_id] = notice
        for op in plan.operations:
            unavailable = tuple(
                k for k, schema in entry.value.schemas.items() if not isinstance(schema, EdSchema)
            )
            if unavailable:
                notice = Notice(
                    "ed.author.other_version_unverified",
                    op.operation_id,
                    op.target.pko_address,
                    "Схемы других версий недоступны",
                    unavailable,
                )
                notices[notice.notice_id] = notice
            # Текст прямой ПКС остаётся таким, как до перехода на новую форму.
            # Алгоритмическая ПКС называет только своё свойство; обработчик чужую ПКС не получает.
            if isinstance(op, AddAlgorithmicHeaderProperty):
                own = own_property_version_notice(op, tuple(other_comparisons))
                if own is not None:
                    notices[own.notice_id] = own
        validation = json.loads(bundle.files["validation.json"])

        def profile_rows(comparisons):
            return [
                {
                    "version": c.before.version,
                    "direction": c.before.direction,
                    "before": [i.to_dict() for i in c.before.issues],
                    "after": [i.to_dict() for i in c.after.issues],
                    "new": [i.to_dict() for i in c.delta.new],
                    "disappeared": [i.to_dict() for i in c.delta.disappeared],
                    "new_relevant_skipped": [s.to_dict() for s in c.delta.new_relevant_skipped],
                    "skipped_before": [s.to_dict() for s in c.before.skipped],
                    "skipped_after": [s.to_dict() for s in c.after.skipped],
                }
                for c in comparisons
            ]

        validation.update(
            selected_profiles=profile_rows(comparisons),
            other_profiles=profile_rows(other_comparisons),
            notices=[asdict(n) | {"notice_id": n.notice_id} for n in notices.values()],
            layer={
                "certain": bool(certain),
                "unknown_lines": unknown,
                "unresolved_dispatch": unresolved,
                "new_issues": len(layer_delta.new),
            },
            layer_before={
                "issues": [i.to_dict() for i in layer_before.issues],
                "skipped": [s.to_dict() for s in layer_before.skipped],
            },
            layer_after={
                "issues": [i.to_dict() for i in layer_after.issues],
                "skipped": [s.to_dict() for s in layer_after.skipped],
            },
            expected_semantic_skipped=expected_skipped,
            handler_slots=[
                {
                    "handler_name": b.handler_name,
                    "event": b.event,
                    "operation_ids": list(b.operation_ids),
                    "runtime_verified": False,
                }
                for b in plan.bindings
            ],
            previous_calls=previous_calls,
            body_refs={"known": body_known, "unparsed": body_unparsed, "bytes": body_bytes},
            source_map=source_map,
            template_evidence={
                "runtime_verified": plan.runtime_verified,
                "bindings": [
                    {"handler_name": b.handler_name, "runtime_verified": b.runtime_verified}
                    for b in plan.bindings
                ],
            },
        )
        files = dict(bundle.files) | {"validation.json": json_bytes(validation)}
        manifest = replace(
            bundle.manifest,
            notices=tuple(notices),
            file_hashes={p: sha256(b) for p, b in files.items() if p != "manifest.json"},
        )
        files["manifest.json"] = manifest.to_bytes()
        result = replace(
            bundle,
            files=files,
            manifest=manifest,
            prepared=replace(
                bundle.prepared,
                selected_profiles=tuple(comparisons),
                other_profiles=tuple(other_comparisons),
                notices=tuple(notices.values()),
            ),
        )
        size = sum(len(b) for b in result.files.values())
        if size <= MAX_BYTES:
            while self._authoring_validated and self._authoring_validated_bytes + size > MAX_BYTES:
                _, old = self._authoring_validated.popitem(last=False)
                self._authoring_validated_bytes -= sum(len(b) for b in old.files.values())
            self._authoring_validated[cache_key] = result
            self._authoring_validated_bytes += size
        return result

    @_timed_build
    @with_reopen_hints
    def ed_authoring_build(
        self,
        project: str | None = None,
        configuration: str | None = None,
        extension: dict | None = None,
        operations: list[dict] | None = None,
        version_scope: str | None = None,
        mode: str = "preview",
        delivery: str = "extension",
        output_dir: str | None = None,
        expected_preview_hash: str | None = None,
        acknowledged_notices: list[str] | None = None,
        offset: int = 0,
        limit: int = 50,
        section: str = "summary",
        level: str | None = None,
        check_prefix: str | None = None,
        address_prefix: str | None = None,
        drop_operations: list[str] | None = None,
        scope: str | None = None,
        project_id: str | None = None,
        expected_revision: str | None = None,
        route: dict | None = None,
    ) -> dict[str, Any]:
        if scope == "manager":
            if any(
                v is not None
                for v in (
                    project,
                    configuration,
                    extension,
                    operations,
                    version_scope,
                    drop_operations,
                )
            ):
                raise ValueError("scope=manager не принимает поля прежнего авторинга")
            return cast("EdWriterMixin", self)._manager_build(
                project_id,
                expected_revision,
                route,
                mode,
                delivery,
                output_dir,
                expected_preview_hash,
                acknowledged_notices,
                offset,
                limit,
                section,
                level,
                check_prefix,
                address_prefix,
            )
        if scope not in (None, "overlay") or any(
            v is not None for v in (project_id, expected_revision, route)
        ):
            raise ValueError("scope: overlay или manager; поля менеджера требуют scope=manager")
        validate_page(offset, limit)
        views.validate_options(section, level, check_prefix, address_prefix)
        if mode == "write" and section != "summary":
            raise ValueError("mode=write допускает только section=summary")
        project = _text(project, "project")
        configuration = _text(configuration, "configuration")
        if mode not in ("preview", "write"):
            raise ValueError("mode: preview или write")
        if delivery == "extension_with_load":
            raise ValueError("extension_with_load появится вместе со скриптом загрузки")
        if delivery not in ("extension", "manual"):
            raise ValueError("delivery: extension или manual")
        if version_scope not in (None, "manager"):
            raise ValueError("version_scope: manager или null")
        if not isinstance(operations, list):
            raise ValueError("Нужен список операций")
        self._limit(len(operations), MAX_OPERATIONS, "операции")
        identity_data = _mapping(
            extension, "extension", {"name", "prefix", "synonym", "version", "compatibility_mode"}
        )
        try:
            identity = ExtensionIdentity(**identity_data)
            for key in ("name", "prefix", "version"):
                _text(getattr(identity, key), key)
            if not isinstance(identity.synonym, str) or (
                identity.compatibility_mode is not None
                and not isinstance(identity.compatibility_mode, str)
            ):
                raise ValueError("Неверные поля extension")
        except TypeError as error:
            raise ValueError("Неверные поля extension") from error
        if acknowledged_notices is None:
            acknowledged_notices = []
        if not isinstance(acknowledged_notices, list) or any(
            not isinstance(n, str) for n in acknowledged_notices
        ):
            raise ValueError("acknowledged_notices: нужен список строк")
        if expected_preview_hash is not None:
            _text(expected_preview_hash, "expected_preview_hash")
        if drop_operations is None:
            drop_operations = []
        if (
            not isinstance(drop_operations, list)
            or any(not isinstance(op, str) or not op for op in drop_operations)
            or len(set(drop_operations)) != len(drop_operations)
        ):
            raise ValueError("drop_operations: нужен список уникальных идентификаторов операций")
        parsed = [self._operation(o, project, configuration) for o in operations]
        groups: dict[tuple[str, str, str], list[Operation]] = defaultdict(list)
        for operation, refs in parsed:
            groups[refs].append(operation)
        self._limit(len({r[0] for r in groups}), MAX_MANAGERS, "менеджеры")
        with self._lock:
            try:
                with _phase("inputs"):
                    entries = {
                        refs: self._inputs_for(project, configuration, refs, tuple(ops))
                        for refs, ops in groups.items()
                    }
                manager_versions: dict[str, str] = {}
                for op, refs in parsed:
                    manager_path = entries[refs].value.document.files[0].path
                    if (
                        manager_path in manager_versions
                        and manager_versions[manager_path] != op.target.format_version
                    ):
                        raise AuthoringPreconditionError(
                            (
                                Failure(
                                    "ed.author.scope_required",
                                    op.target.pko_address,
                                    "операции одного менеджера проверяются "
                                    "по одной выбранной версии; "
                                    "остальные версии показываются как другие",
                                ),
                            )
                        )
                    manager_versions[manager_path] = op.target.format_version
                config_path = self._project_root(project, configuration) / "Configuration.xml"
                config_description = read_description(
                    "Configuration.xml",
                    self._description(next(iter(entries.values())), "Configuration.xml")
                    if entries
                    else config_path.read_text("utf-8-sig"),
                    "Configuration",
                )
                destination = self._destination(
                    artifact_name(identity.name, config_description.uuid), output_dir
                )
                previous, previous_files = self._previous(destination)
                if not operations and previous is None:
                    raise ValueError("Нужен непустой список операций без прежнего комплекта")
                previous_operations = (
                    (*previous.operations, *previous.handler_operations) if previous else ()
                )
                previous_ids = (
                    {op.operation_id for op in previous_operations} if previous else set()
                )
                if set(drop_operations) - previous_ids:
                    raise ValueError("drop_operations содержит операцию вне прежнего комплекта")
                retained_ids = previous_ids - set(drop_operations)
                if previous and not retained_ids and not operations:
                    raise ValueError("Итоговый набор операций должен оставаться непустым")
                if previous:
                    if previous.identity != identity:
                        raise _failure(
                            AuthoringPreconditionError(
                                (
                                    Failure(
                                        "ed.author.owned_content_changed",
                                        "",
                                        "Идентичность прежнего комплекта отличается",
                                    ),
                                )
                            )
                        )
                    for op in previous_operations:
                        if op.operation_id not in retained_ids:
                            continue
                        matches = [
                            refs
                            for refs, entry in entries.items()
                            if any(
                                p.plan_name == op.target.plan
                                and any(
                                    e.key == op.target.format_version
                                    and e.manager_name
                                    and entry.value.document.files[0]
                                    .path.replace("\\", "/")
                                    .endswith("CommonModules/" + e.manager_name + "/Ext/Module.bsl")
                                    for e in p.entries
                                )
                                for p in entry.value.routes.plans
                            )
                        ]
                        if not matches:
                            try:
                                refs = self._previous_refs(op, entries)
                                with _phase("inputs"):
                                    entries[refs] = self._inputs_for(
                                        project, configuration, refs, (op,)
                                    )
                            except AuthoringPreconditionError as error:
                                raise self._previous_failure(error, op) from error
                            matches = [refs]
                        if len(matches) != 1:
                            raise ValueError("Несколько снимков прежнего менеджера в одном вызове")
                        groups[matches[0]].append(op)
                bundles = []
                preparations = []
                handler_plans = []
                use_handlers = bool(previous and previous.schema_version == 2) or any(
                    not isinstance(op, AddHeaderProperty) for ops in groups.values() for op in ops
                )
                known_attributes: dict[tuple[str, str], frozenset[str]] = {}
                for refs, ops in groups.items():
                    index = self._ed_project(refs[0]).index
                    for op in ops:
                        if not isinstance(op, AddHeaderProperty) or not op.new_attribute:
                            continue
                        try:
                            rule = index.find(op.target.pko_address)
                        except (EntityNotFoundError, AmbiguousAddressError):
                            continue
                        if isinstance(rule, ObjectRule):
                            key, _ = metadata_key(rule.configuration_object.value)
                            if key:
                                known_attributes[key] = known_attributes.get(key, frozenset()) | {
                                    op.new_attribute.name.casefold()
                                }
                self._limit(len({r[0] for r in entries}), MAX_MANAGERS, "менеджеры")
                for refs, ops in groups.items():
                    entry = entries[refs]
                    for op in ops:
                        try:
                            self._require_selected_schema(op.target, refs, entry.value)
                        except AuthoringPreconditionError as error:
                            if op.operation_id in retained_ids:
                                raise self._previous_failure(error, op) from error
                            raise
                    with _phase("inputs"):
                        self._extension_sources(entry, tuple(ops), self._ed_project(refs[0]).index)
                    if use_handlers:
                        ops = list(merge_operations((), tuple(ops)))
                    prepared_key = digest(
                        (
                            tuple(ops),
                            identity,
                            version_scope,
                            entry.value.source_set,
                            known_attributes if use_handlers else None,
                        )
                    )
                    with _phase("prepare"):
                        if entry.prepared is None or entry.prepared_key != prepared_key:
                            context = AuthoringContext(entry.value)
                            opened = self._ed_project(refs[0])
                            context.indices[id(opened.document)] = opened.index
                            context.references = opened.references
                            for old in ops if not use_handlers else ():
                                if old.operation_id in retained_ids:
                                    try:
                                        validate_preconditions(
                                            entry.value,
                                            (old,),
                                            identity,
                                            version_scope=version_scope,
                                            context=context,
                                        )
                                    except AuthoringPreconditionError as error:
                                        raise self._previous_failure(error, old) from error
                            if use_handlers:
                                entry.prepared = prepare_handler_operations(
                                    entry.value,
                                    tuple(ops),
                                    identity,
                                    version_scope=version_scope,
                                    known_attributes=known_attributes,
                                )
                            else:
                                entry.prepared = prepare_authoring(
                                    entry.value,
                                    tuple(op for op in ops if isinstance(op, AddHeaderProperty)),
                                    identity,
                                    version_scope=version_scope,
                                    context=context,
                                    other_rules_only=True,
                                )
                            entry.prepared_key = prepared_key
                            self._ed_projects[refs[0]] = replace(
                                opened, references=context.references
                            )
                        prepared = entry.prepared
                    with _phase("inputs"):
                        descriptions = self._descriptions(
                            entry,
                            tuple(
                                op
                                for op in prepared.operations
                                if isinstance(op, AddHeaderProperty)
                            ),
                        )
                    with _phase("render"):
                        plan = None
                        form_evidence: dict[str, bool] = {}
                        if isinstance(prepared, HandlerOperationsPlan):
                            self._handler_limits(prepared)
                            if set(drop_operations) & {
                                op.operation_id for op in prepared.operations
                            }:
                                raise ValueError("Снятая операция остаётся в итоговом наборе")
                            # Свидетельство пилота шаблона не доказывает исполнение комплекта:
                            # признак комплекта — Ложь, свидетельство формы — в инструкцию.
                            form_evidence = {
                                b.handler_name: b.runtime_verified for b in prepared.bindings
                            }
                            plan = replace(
                                prepared,
                                dispatcher_name=prepared.dispatcher_name
                                if prepared.bindings
                                else "",
                                runtime_verified=False,
                                bindings=tuple(
                                    replace(b, runtime_verified=False) for b in prepared.bindings
                                ),
                            )
                            rendered = render_handlers_authoring(
                                entry.value,
                                plan,
                                identity,
                                descriptions,
                                delivery=delivery,
                                keep_handlers_version=use_handlers,
                                form_evidence=form_evidence,
                            )
                        else:
                            rendered = render_authoring(prepared, descriptions, delivery=delivery)
                        if previous:
                            current_ids = rendered.manifest.identity_map
                            preserved = IdentityMap(
                                current_ids.artifact_uuid,
                                {
                                    p: previous.identity_map.objects.get(p, v)
                                    for p, v in current_ids.objects.items()
                                },
                                current_ids.borrowed,
                            )
                            if preserved != current_ids:
                                if isinstance(prepared, HandlerOperationsPlan):
                                    assert plan is not None
                                    rendered = render_handlers_authoring(
                                        entry.value,
                                        plan,
                                        identity,
                                        descriptions,
                                        delivery=delivery,
                                        identity_map=preserved,
                                        keep_handlers_version=use_handlers,
                                        form_evidence=form_evidence,
                                    )
                                else:
                                    rendered = render_authoring(
                                        prepared,
                                        descriptions,
                                        delivery=delivery,
                                        identity_map=preserved,
                                    )
                        if isinstance(prepared, HandlerOperationsPlan):
                            rendered = self._validate_handler_layer(entry, rendered, prepared)
                            handler_plans.append(prepared)
                        preparations.append(rendered.prepared if use_handlers else prepared)
                        bundles.append(
                            with_source_hashes(
                                rendered,
                                self._source_hashes(entry, descriptions if use_handlers else None),
                            )
                        )
                self._limit(
                    sum(len(p.operations) for p in preparations)
                    if not use_handlers
                    else sum(len(p.operations) for p in handler_plans),
                    MAX_OPERATIONS,
                    "операции",
                )
                self._limit(sum(len(p.bindings) for p in handler_plans), 100, "слоты обработчиков")
                self._limit(
                    sum(
                        json.loads(b.files["validation.json"])["body_refs"]["bytes"]
                        for b in bundles
                        if use_handlers
                    ),
                    MAX_HANDLER_BODY_BYTES,
                    "байты тел",
                )
                self._limit(
                    sum(len(p.selected_profiles) + len(p.other_profiles) for p in preparations),
                    MAX_PROFILES,
                    "профили версий",
                )
                bundle = combine_artifacts(bundles)
                if previous and (
                    previous.identity_map.artifact_uuid
                    != bundle.manifest.identity_map.artifact_uuid
                ):
                    raise AuthoringPreconditionError(
                        (
                            Failure(
                                "ed.author.owned_content_changed",
                                "",
                                "Карта UUID прежнего комплекта отличается "
                                "от порождённой идентичности",
                            ),
                        )
                    )
                changed_inputs = previous.changed_inputs(bundle.manifest) if previous else {}
                self._limit(len(bundle.files), MAX_FILES, "файлы комплекта")
                self._limit(
                    sum(len(b) for b in bundle.files.values()), MAX_BYTES, "байты комплекта"
                )
                build_hash = sha256(bundle.manifest.to_bytes())
                with _phase("inputs"):
                    for entry in entries.values():
                        self._verify_inputs(entry)
                        if mode == "preview":
                            self._check_route(entry.route)
                status = "ready"
                if mode == "write":
                    if expected_preview_hash != build_hash:
                        viewed = self._authoring_previews.get(expected_preview_hash or "")
                        comparison = viewed or previous
                        raise EdAuthoringStaleError(
                            "Нужен expected_preview_hash текущего preview",
                            {
                                "preview_hash": build_hash,
                                "changed_inputs": comparison.changed_inputs(bundle.manifest)
                                if comparison
                                else {},
                                "decisions_changed": viewed is not None
                                and (
                                    viewed.operations != bundle.manifest.operations
                                    or viewed.handler_operations
                                    != bundle.manifest.handler_operations
                                    or viewed.identity != bundle.manifest.identity
                                    or viewed.delivery != bundle.manifest.delivery
                                ),
                            },
                        )
                    missing = sorted(set(bundle.manifest.notices) - set(acknowledged_notices))
                    if missing:
                        raise EdAuthoringAckRequiredError(
                            "Замечания требуют явного подтверждения",
                            {"required_acknowledgements": missing, "preview_hash": build_hash},
                        )
                    status = self._write_artifact(
                        destination, bundle.files, previous_files, list(entries.values())
                    )
                result = views.build_view(
                    bundle,
                    preparations,
                    build_hash=build_hash,
                    status=status,
                    output_path=self._host(destination),
                    written=mode == "write",
                    offset=offset,
                    limit=limit,
                    inputs=[entries[refs].value for refs in groups],
                    section=section,
                    level=level,
                    check_prefix=check_prefix,
                    address_prefix=address_prefix,
                    rebuild=bool(changed_inputs),
                    changed_inputs=changed_inputs,
                    handler_plans=handler_plans,
                    previous=previous,
                    previous_files=previous_files,
                    migration=bool(previous and previous.schema_version == 1 and use_handlers),
                )
                if mode == "preview":
                    self._authoring_previews[build_hash] = bundle.manifest
                    self._authoring_previews.move_to_end(build_hash)
                    while len(self._authoring_previews) > MAX_INPUTS:
                        self._authoring_previews.popitem(last=False)
                return result
            except AuthoringPreconditionError as error:
                mapped = _failure(error)
                for failure in mapped.details["failures"]:
                    failure["source"]["file"] = self._host_text(failure["source"]["file"])
                raise mapped from error
            except (OSError, UnicodeError) as error:
                raise EdAuthoringIoError(
                    "Ошибка чтения или атомарной записи комплекта",
                    {"path": self._host_text(str(getattr(error, "filename", "") or ""))},
                ) from error
