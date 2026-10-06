"""Чтение проектов ED в памяти процесса под общей блокировкой сервиса."""

import hashlib
import re
import threading
from collections import Counter, OrderedDict
from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from kd2_rules_mcp import ed
from kd2_rules_mcp.ed import address as addresses
from kd2_rules_mcp.ed.schema.profile import ValidationProfile
from kd2_rules_mcp.errors import (
    AmbiguousAddressError,
    EdAuthoringPreconditionError,
    EdFormatError,
    EdReadError,
    EdResourceLimitError,
    EdSchemaNotFoundError,
    Kd2Error,
    ProjectNotFoundError,
    RuleNotFoundError,
    StructureNotFoundError,
)
from kd2_rules_mcp.projects import resolve
from kd2_rules_mcp.service import ed_layers as layer_views
from kd2_rules_mcp.service import ed_views as views
from kd2_rules_mcp.service.base import ServiceBase
from kd2_rules_mcp.service.ed_reopen import with_reopen_hints
from kd2_rules_mcp.service.ed_routes import EdRoutesMixin
from kd2_rules_mcp.service.ed_schema import SchemaProject
from kd2_rules_mcp.service.paths import Settings
from kd2_rules_mcp.validation.ed_layers import (
    validate_effective_links,
    validate_effective_schema,
    validate_effective_structure,
    validate_layers,
)
from kd2_rules_mcp.validation.ed_links import validate_links
from kd2_rules_mcp.validation.ed_projection import effective_document
from kd2_rules_mcp.validation.ed_schema import validate_schema
from kd2_rules_mcp.validation.ed_structure import validate_structure
from kd2_rules_mcp.validation.ed_structure_snapshot import StructureSnapshot
from kd2_rules_mcp.validation.report import Issue, Level, ValidationReport

if TYPE_CHECKING:
    from kd2_rules_mcp.service.ed_writer import EdWriterMixin


@dataclass(frozen=True)
class LayerValidation:
    report: ValidationReport
    metadata: dict[str, Any]


class ValidationRegistry(OrderedDict[str, Any]):
    """Закрытие схемы и вытеснение профиля немедленно удаляют зависимые результаты."""

    def __init__(self, source, removed: Callable[[str], None]):
        self.removed = removed
        super().__init__(source)

    def pop(self, *args, **kwargs):
        present = args[0] in self
        result = super().pop(*args, **kwargs)
        if present:
            self.removed(args[0])
        return result

    def popitem(self, last: bool = True):
        key, value = super().popitem(last=last)
        self.removed(key)
        return key, value

    def __delitem__(self, key):
        super().__delitem__(key)
        self.removed(key)

    def clear(self):
        keys = tuple(self)
        super().clear()
        for key in keys:
            self.removed(key)

    def __setitem__(self, key, value):
        previous = self.get(key)
        super().__setitem__(key, value)
        if previous is not None and previous is not value:
            self.removed(key)


@dataclass(frozen=True)
class EdProject:
    path: Path
    document: ed.EdDocument
    index: addresses.AddressIndex
    entities: dict[str, ed.Entity]
    by_address: dict[str, ed.Entity]
    references: ed.ReferenceIndex | None = None
    layered: layer_views.LayerSnapshot | None = None
    validation_cache: dict[tuple, LayerValidation] = field(default_factory=dict, compare=False)
    manager_project_id: str | None = None


class EdMixin(ServiceBase):
    """Чтение неизменяемых снимков ED и проверка связности открытого модуля."""

    _ed_schemas: dict[str, SchemaProject]

    def __init__(self, settings: Settings) -> None:
        super().__init__(settings)
        self._ed_projects: dict[str, EdProject] = {}
        self._ed_structure_snapshots: dict[str, tuple[tuple[str, str, str], StructureSnapshot]] = {}
        self._ed_structure_snapshot_lock = threading.Lock()

    def _ed_structure_snapshot(self, structure_id: str) -> tuple[StructureSnapshot, str | None]:
        """DTO кэшируется по входу; тяжёлое чтение не держит общую блокировку сервиса."""
        with self._structure(structure_id) as connection:
            meta = dict(connection.execute("SELECT key, value FROM meta"))
            fingerprint = tuple(
                meta.get(k, "") for k in ("input_hash", "loader_version", "schema_version")
            )
            with self._ed_structure_snapshot_lock:
                cached = self._ed_structure_snapshots.get(structure_id)
                if cached and cached[0] == fingerprint:
                    return cached[1], meta.get("input_hash")
                snapshot = StructureSnapshot.load(connection)
                self._ed_structure_snapshots[structure_id] = (fingerprint, snapshot)
            return snapshot, meta.get("input_hash")

    def _ed_project(self, project_id: str) -> EdProject:
        if project_id not in self._ed_projects:
            raise ProjectNotFoundError(f"Проект ED «{project_id}» не открыт")
        project = self._ed_projects[project_id]
        if project.manager_project_id:
            cast("EdWriterMixin", self)._manager_check_document(
                project.manager_project_id, project_id
            )
        return project

    def _ensure_references(self, project_id: str) -> tuple[EdProject, ed.ReferenceIndex]:
        """Индекс ссылок строится один раз и хранится рядом со снимком."""
        project = self._ed_project(project_id)
        cached = project.references
        if cached is not None:
            return project, cached
        cached = ed.build_references(project.document)
        project = replace(project, references=cached)
        self._ed_projects[project_id] = project
        return project, cached

    def ed_open(
        self,
        path: str | None = None,
        configuration_path: str | None = None,
        extensions: list[str] | None = None,
        project: str | None = None,
        module: str | None = None,
        configuration: str = "full",
    ) -> dict[str, Any]:
        if (path is None) == (project is None):
            raise ValueError("Укажите ровно один режим: path или project + module")
        if extensions is not None and (
            not isinstance(extensions, list)
            or any(not isinstance(p, str) or not p.strip() for p in extensions)
        ):
            raise ValueError("extensions: упорядоченный список путей")
        if project is None and (module is not None or configuration != "full"):
            raise ValueError("module и configuration применимы только к project")
        if project is not None and (not isinstance(module, str) or not module.strip()):
            raise ValueError("С project нужно точное имя общего модуля module")
        if extensions is not None and len(extensions) > 16:
            raise EdResourceLimitError("Допустимо не больше 16 расширений")
        root = None
        roots: tuple[Path, ...] = ()
        try:
            if project is not None:
                if configuration_path is not None:
                    raise ValueError("configuration_path применим только к path")
                config = self._catalog().configuration(project, configuration)
                folder = self.settings.project_dirs.get(project)
                if folder is None:
                    raise EdReadError("Папка проекта не подключена")
                root = self._read_path(self._host(resolve(folder, config.dump))).resolve()
                extensions = (
                    [self._host(resolve(folder, e)) for e in config.extensions]
                    if extensions is None
                    else extensions
                )
                roots = layer_views.extension_paths(extensions, self._read_path)
                layer_views.checked_extensions(root, roots)
                if len(roots) > 16:
                    raise EdResourceLimitError("Допустимо не больше 16 расширений")
                path = str(layer_views.module_path(root, roots, module or ""))
            elif extensions:
                if configuration_path is None:
                    raise ValueError("Для extensions нужен configuration_path с Configuration.xml")
                root = self._read_path(configuration_path).resolve()
                roots = layer_views.extension_paths(extensions, self._read_path)
                layer_views.checked_extensions(root, roots)
        except Kd2Error as error:
            if isinstance(error, (AmbiguousAddressError, EdResourceLimitError)):
                raise
            raise EdReadError(str(error)) from error
        if roots:
            assert root is not None and path is not None
            return self._ed_open_layered(path, root, roots, configuration)
        if not isinstance(path, str) or not path.strip():
            raise ValueError("Нужен путь к файлу менеджера ED")
        with self._lock:
            try:
                local = self._read_path(path).resolve()
            except Kd2Error as error:
                # Текст сохраняем: в нём подсказка, какая папка не подключена.
                raise EdReadError(str(error)) from error
            except OSError as error:
                raise EdReadError("Файл менеджера недоступен") from error
            existing = next(
                (
                    (key, p)
                    for key, p in self._ed_projects.items()
                    if p.path == local and p.layered is None
                ),
                None,
            )
            changed = False
            if existing:
                project_id, opened_project = existing
                cast("EdWriterMixin", self)._manager_check_reader_id(project_id)
                try:
                    # Хеш повторного открытия вычисляется потоково, без нового разбора.
                    with local.open("rb") as stream:
                        current = hashlib.file_digest(stream, "sha256").hexdigest()
                except OSError as error:
                    raise EdReadError("Файл менеджера недоступен") from error
                changed = current != opened_project.document.files[0].sha256
            else:
                try:
                    document = ed.read_manager(local)
                except ed.EdResourceLimitError as error:
                    raise EdResourceLimitError(str(error)) from error
                except ed.EdFormatError as error:
                    raise EdFormatError(str(error)) from error
                except ed.EdReadError as error:
                    raise EdReadError(str(error)) from error
                digest = hashlib.sha256(str(local).encode("utf-8")).hexdigest()
                # Модуль выгрузки конфигурации всегда `<Имя>/Ext/Module.bsl`: имя даёт каталог.
                named = local.parent.parent if local.parent.name.casefold() == "ext" else local
                stem = re.sub(r"[^\w-]+", "-", named.stem).strip("-") or "module"
                project_id = f"ed-{stem}-{digest[:12]}"
                cast("EdWriterMixin", self)._manager_check_reader_id(project_id)
                if project_id in self._ed_projects:
                    project_id = f"ed-{stem}-{digest}"
                cast("EdWriterMixin", self)._manager_check_reader_id(project_id)
                index = addresses.build_addresses(document)
                entities = {e.entity_id: e for e in document.entities()}
                by_address = {a.casefold(): e for a, e in index.by_address.items()}
                for entity in entities.values():
                    by_address.setdefault(views.address_of(entity, index).casefold(), entity)
                opened_project = EdProject(local, document, index, entities, by_address)
                self._ed_projects[project_id] = opened_project
            doc = opened_project.document
            return {
                "project_id": project_id,
                "kind": "ed",
                "source_files": [
                    {
                        "file_id": f.file_id,
                        "path": self._host(Path(f.path)),
                        "sha256": f.sha256,
                        "lines": f.lines,
                    }
                    for f in doc.files
                ],
                "manager_version": doc.manager_version,
                "parse_status": doc.parse_status.value,
                "counts": self._ed_counts(doc),
                "reused": existing is not None,
                "source_changed": changed,
                "diagnostics_summary": views.summary(doc),
            }

    def _ed_open_layered(
        self, path: str, root: Path, roots: tuple[Path, ...], configuration: str
    ) -> dict[str, Any]:
        try:
            local = self._read_path(path).resolve()
            key = (str(local), str(root), configuration, *(str(p) for p in roots))
            with self._lock:
                existing = next(
                    (
                        (ident, p)
                        for ident, p in self._ed_projects.items()
                        if p.layered and p.layered.key == key
                    ),
                    None,
                )
            if existing:
                ident, project = existing
                with self._lock:
                    cast("EdWriterMixin", self)._manager_check_reader_id(ident)
                assert project.layered is not None
                changed = layer_views.source_changed(project.layered)
            else:
                # Тяжёлый разбор и построение индексов не держат общую блокировку.
                manager = layer_views.read_selected_layers(local, root, roots)
                snap = layer_views.snapshot(manager, key)
                document = manager.source_document or manager.base
                index = addresses.build_addresses(document)
                entities = {e.entity_id: e for e in document.entities()}
                ident = "ed-layer-" + hashlib.sha256("\0".join(key).encode()).hexdigest()[:20]
                project = EdProject(
                    local,
                    document,
                    index,
                    entities,
                    {a.casefold(): e for a, e in index.by_address.items()},
                    layered=snap,
                )
                with self._lock:
                    cast("EdWriterMixin", self)._manager_check_reader_id(ident)
                    previous = self._ed_projects.setdefault(ident, project)
                changed = False
                if previous is not project:
                    project = previous
                    existing = (ident, project)
                    assert project.layered is not None
                    changed = layer_views.source_changed(project.layered)
        except ed.EdResourceLimitError as error:
            raise EdResourceLimitError(str(error)) from error
        except ed.EdFormatError as error:
            raise EdFormatError(str(error)) from error
        except EdAuthoringPreconditionError:
            raise
        except (ed.EdReadError, OSError, Kd2Error) as error:
            raise EdReadError(str(error)) from error
        assert project.layered is not None
        doc = project.document
        result = {
            "project_id": ident,
            "kind": "ed",
            "source_files": [
                {
                    "file_id": f.file_id,
                    "path": self._host(Path(f.path)),
                    "sha256": f.sha256,
                    "lines": f.lines,
                }
                for f in project.layered.manager.source_files[:16]
            ],
            "manager_version": doc.manager_version,
            "parse_status": doc.parse_status.value,
            "counts": self._ed_counts(doc),
            "reused": existing is not None,
            "source_changed": changed,
            "diagnostics_summary": views.summary(doc),
            **layer_views.composition(project.layered),
        }
        return result

    @staticmethod
    def _ed_counts(document: ed.EdDocument) -> dict[str, int]:
        return {key: value for key, value in document.counts.items() if key != "values"}

    def ed_overview(self, project_id: str) -> dict[str, Any]:
        with self._lock:
            project = self._ed_project(project_id)
            doc = project.document
            mentions = doc.conversion.format_version_mentions
            coverage = doc.coverage
            result = {
                "project_id": project_id,
                "manager_version": doc.manager_version,
                "format_versions": {
                    "status": "mentions_only",
                    "items": [
                        {
                            "value": views.short(e.value),
                            "span": views.span_view(e.span),
                            "context": views.short(e.context),
                        }
                        for e in mentions[:20]
                    ],
                    "total": len(mentions),
                    "has_more": len(mentions) > 20,
                },
                "counts": self._ed_counts(doc),
                "coverage": {
                    "total_lines": doc.files[0].lines,
                    **{f"{key}_lines": value for key, value in coverage.counts.items()},
                    "entity_covered_lines": coverage.entity_covered_lines,
                    "coverage_ratio": round(coverage.coverage_ratio, 6),
                    "classified_ratio": round(coverage.classified_ratio, 6),
                },
                "parse_status": doc.parse_status.value,
                "diagnostics_summary": views.summary(doc),
            }
            if project.layered:
                # Подробные упоминания доступны через kind=version; шапка со слоями компактна.
                result["format_versions"]["items"] = [
                    {
                        **item,
                        "context": str(item["context"])[:160],
                        "value": str(item["value"])[:160],
                    }
                    for item in result["format_versions"]["items"][:4]
                ]
                result["format_versions"]["has_more"] = len(mentions) > 4
                result["composition"] = layer_views.composition(project.layered, overview=True)
                from kd2_rules_mcp.ed.layer_model import HANDLER_EXECUTION_NOTE

                result["handler_execution_note"] = HANDLER_EXECUTION_NOTE
            return result

    def ed_list(
        self,
        project_id: str,
        kind: str,
        text: str | None = None,
        format_object: str | None = None,
        metadata_object: str | None = None,
        offset: int = 0,
        limit: int = 50,
        direction: str | None = None,
        headers_only: bool = False,
        layer: str | None = None,
        entity_id: str | None = None,
    ) -> dict[str, Any]:
        views.validate_page(offset, limit)
        layer_views.validate_context(direction, headers_only)
        if entity_id is not None and kind != "change":
            raise ValueError("entity_id применим только к kind=change")
        if kind not in views.KINDS | layer_views.KINDS:
            raise ValueError(f"Неизвестный вид ED: {kind}")
        with self._lock:
            project = self._ed_project(project_id)
            if project.layered:
                if kind == "change":
                    return layer_views.change_page(
                        project.layered,
                        direction,
                        headers_only,
                        layer,
                        entity_id,
                        text,
                        offset,
                        limit,
                        format_object,
                        metadata_object,
                    )
                rows = layer_views.list_rows(
                    project.layered,
                    kind,
                    direction,
                    headers_only,
                    layer,
                    entity_id,
                    format_object,
                    metadata_object,
                    text,
                )
                for name, value in (
                    ("format_object", format_object),
                    ("configuration_object", metadata_object),
                ):
                    if value is not None and kind in layer_views.KINDS:
                        rows = [
                            r
                            for r in rows
                            if isinstance(r.get(name), str)
                            and r[name].strip().casefold() == value.strip().casefold()
                        ]
                if text is not None and kind in layer_views.KINDS:
                    rows = [
                        r
                        for r in rows
                        if any(
                            text.casefold() in str(r.get(k, "")).casefold()
                            for k in ("name", "address", "configuration_object", "format_object")
                        )
                    ]
                return views.page(rows, offset, limit)
            if direction is not None or headers_only:
                raise ValueError("direction и headers_only доступны на снимке с расширениями")
            if layer is not None:
                raise ValueError(
                    "На снимке без расширений нет слоя; откройте менеджер с extensions"
                )
            if entity_id is not None:
                raise RuleNotFoundError(f"Сущность истории не найдена: {entity_id}")
            if kind in layer_views.KINDS:
                return views.page([], offset, limit)
            found = []
            for entity in project.entities.values():
                if kind in views.ROLES:
                    if not isinstance(entity, ed.Routine) or kind not in entity.roles:
                        continue
                elif entity.kind != kind:
                    continue
                configuration, format_name = views.sides(entity, project.entities)
                if format_object is not None and (
                    format_name is None
                    or format_name.strip().casefold() != format_object.strip().casefold()
                ):
                    continue
                if metadata_object is not None and (
                    configuration is None
                    or configuration.strip().casefold() != metadata_object.strip().casefold()
                ):
                    continue
                row = views.row(entity, project.index, project.entities, kind)
                if text is not None and not any(
                    text.casefold() in value.casefold()
                    for value in (
                        entity.name,
                        row["address"],
                        configuration or "",
                        format_name or "",
                    )
                ):
                    continue
                found.append((entity.span.char_start, row))
            found.sort(key=lambda item: (item[0], item[1]["address"]))
            return views.page([row for _, row in found], offset, limit)

    def ed_get(
        self,
        project_id: str,
        address: str,
        children_kind: str | None = None,
        offset: int = 0,
        limit: int = 50,
        include_text: bool = False,
        text_offset: int = 0,
        text_limit: int = 2000,
        direction: str | None = None,
        headers_only: bool = False,
    ) -> dict[str, Any]:
        views.validate_page(offset, limit)
        views.validate_page(text_offset, text_limit, 8000)
        layer_views.validate_context(direction, headers_only)
        with self._lock:
            project = self._ed_project(project_id)
            if project.layered:
                result = layer_views.get_view(
                    project.layered,
                    address,
                    direction,
                    headers_only,
                    children_kind,
                    offset,
                    limit,
                    include_text,
                    text_offset,
                    text_limit,
                )
                if result.get("kind") == "layer":
                    for source in result["source_files"]["items"]:
                        source["path"] = self._host(Path(source["path"]))
                return result
            if direction is not None or headers_only:
                raise ValueError("direction и headers_only доступны на снимке с расширениями")
            if address.casefold() in project.index.conflicts:
                candidates = project.index.conflicts[address.casefold()]
                error = AmbiguousAddressError(f"Неоднозначный адрес: {address}", candidates)
                error.candidate_page = views.page(list(candidates), offset, limit)
                raise error
            lookup = (
                "Алгоритм/" + address.split("/", 1)[1]
                if address.casefold().startswith("код/")
                else address
            )
            entity = project.by_address.get(lookup.casefold())
            if entity is None:
                raise RuleNotFoundError(f"Сущность ED не найдена: {address}")
            if children_kind == "reference" and views.accepts_code_references(entity):
                project, references = self._ensure_references(project_id)
                selected = views.page(
                    [
                        views.reference_row(item)
                        for item in references.entries
                        if item.owner_id == entity.entity_id
                    ],
                    offset,
                    limit,
                )
            else:
                children, kinds = views.direct_children(entity, project.entities)
                if children_kind is not None:
                    if children_kind not in kinds:
                        raise ValueError(f"Вид детей {children_kind} недоступен для {entity.kind}")
                    children = [pair for pair in children if pair[0] == children_kind]
                selected = views.page(children, offset, limit)
                selected["items"] = [
                    views.child_view(k, item, project.index, project.entities)
                    for k, item in selected["items"]
                ]
            doc = project.document
            result = {
                "address": views.address_of(entity, project.index)
                if isinstance(entity, ed.Routine) and "algorithm" in entity.roles
                else next(
                    (
                        a
                        for a in project.index.by_id.get(entity.entity_id, ())
                        if a.casefold() == address.casefold()
                    ),
                    views.address_of(entity, project.index),
                ),
                "kind": entity.kind,
                "fields": views.entity_fields(entity),
                "span": views.span_view(entity.span),
                "regions": views.page([views.short(r) for r in entity.regions], offset, limit),
                "tags": views.page(
                    [
                        {
                            "name": views.short(t.name),
                            "span": views.span_view(t.span),
                        }
                        for t in doc.tags
                        if t.name in entity.tag_ids
                        and t.span.char_start <= entity.span.char_start < t.span.char_end
                    ],
                    offset,
                    limit,
                ),
                "guards": views.page(
                    [
                        views.row(project.entities[g], project.index, project.entities)
                        for g in entity.guards
                        if g in project.entities
                    ],
                    offset,
                    limit,
                ),
                "children": selected,
                "diagnostics": views.page(
                    [
                        views.row(d, project.index, project.entities)
                        for d in doc.diagnostics
                        if d.owner_id == entity.entity_id
                        or (
                            d.span.file_id == entity.span.file_id
                            and entity.span.char_start <= d.span.char_start < entity.span.char_end
                        )
                    ],
                    offset,
                    limit,
                ),
            }
            if include_text:
                raw = entity.raw_text
                result["text"] = {
                    "text": raw[text_offset : text_offset + text_limit],
                    "total_chars": len(raw),
                    "offset": text_offset,
                    "limit": text_limit,
                    "has_more": text_offset + text_limit < len(raw),
                }
            return result

    def ed_locate(
        self,
        project_id: str,
        line: int,
        offset: int = 0,
        limit: int = 50,
        file_id: str | None = None,
    ) -> dict[str, Any]:
        views.validate_page(offset, limit)
        with self._lock:
            project = self._ed_project(project_id)
            if project.layered:
                return layer_views.locate(project.layered, file_id, line, offset, limit)
            doc = project.document
            if file_id is not None and file_id != doc.files[0].file_id:
                raise ValueError("Неизвестный file_id")
            if type(line) is not int or not 1 <= line <= doc.files[0].lines:
                raise ValueError("Номер строки вне файла")
            # Процедура `ДобавитьПКО_…`/`ДобавитьПОД_…` уже представлена самим правилом.
            found = tuple(
                e
                for e in addresses.locate(doc, line)
                if not (isinstance(e, ed.Routine) and e.roles <= {"rule"})
            )
            matches = [
                {
                    "address": views.address_of(e, project.index),
                    "kind": e.kind,
                    "span": views.span_view(e.span),
                    "relation": "innermost"
                    if n == 0
                    or not (
                        getattr(found[0], "group_id", None) == e.entity_id
                        or (
                            e.span.char_start <= found[0].span.char_start
                            and e.span.char_end >= found[0].span.char_end
                        )
                    )
                    else "ancestor",
                }
                for n, e in enumerate(found)
            ]
            routine_ids = {e.entity_id for e in found if isinstance(e, ed.Routine)}
            for binding in doc.entities():
                if isinstance(binding, ed.HandlerBinding) and binding.target_id in routine_ids:
                    owner = (
                        doc.conversion
                        if binding.owner_id == "conversion"
                        else project.entities.get(binding.owner_id)
                    )
                    if (
                        owner is not None
                        and owner not in found
                        and not any(
                            m["address"] == views.address_of(owner, project.index) for m in matches
                        )
                    ):
                        matches.append(
                            {
                                "address": views.address_of(owner, project.index),
                                "kind": owner.kind,
                                "span": views.span_view(owner.span),
                                "relation": "associated",
                            }
                        )
            return {
                "file_id": doc.files[0].file_id,
                "line": line,
                "classification": doc.coverage.classify_line(line).value,
                "matches": views.page(matches, offset, limit),
            }

    @with_reopen_hints
    def ed_validate(
        self,
        project_id: str,
        level: str | None = None,
        check_prefix: str | None = None,
        offset: int = 0,
        limit: int = 50,
        schema_id: str | None = None,
        structure_id: str | None = None,
        direction: str | None = "both",
        section: str = "issues",
        address_prefix: str | None = None,
        headers_only: bool = False,
        route_profile_id: str | None = None,
    ) -> dict[str, Any]:
        """Связность и профильные проверки открытых снимков; BSL не исполняется."""
        views.validate_page(offset, limit)
        direction = direction or "both"
        if direction not in ("send", "receive", "both"):
            raise ValueError("Направление: send, receive или both")
        if section not in ("issues", "skipped"):
            raise ValueError("Раздел отчёта: issues или skipped")
        if level:
            Level(level)
        layer_views.validate_context(None if direction == "both" else direction, headers_only)
        with self._lock:
            project, references = self._ensure_references(project_id)
            if project.layered is None:
                if direction != "both":
                    raise ValueError("direction доступен на снимке с расширениями")
                if route_profile_id is not None:
                    raise ValueError("route_profile_id доступен на снимке с расширениями")
                if headers_only:
                    raise ValueError("headers_only доступен на снимке с расширениями")
            schema_project = None
            if schema_id is not None:
                schema_project = self._ed_schemas.get(schema_id)
                if schema_project is None:
                    raise EdSchemaNotFoundError("Схема формата не открыта")
        try:
            snapshot, structure_fingerprint = (
                self._ed_structure_snapshot(structure_id)
                if structure_id is not None
                else (None, None)
            )
        except StructureNotFoundError:
            if structure_id:
                self._drop_layer_validations("structure", structure_id)
            raise
        profile = ValidationProfile.build(
            schema_project.schema if schema_project else None,
            schema_project.format_version if schema_project else None,
            direction,
        )
        coverage: Counter[str] = Counter()
        if project.layered:
            return self._ed_validate_layered(
                project_id,
                project,
                schema_project,
                snapshot,
                structure_fingerprint,
                schema_id,
                structure_id,
                direction,
                headers_only,
                route_profile_id,
                level,
                check_prefix,
                address_prefix,
                section,
                offset,
                limit,
            )
        report = validate_links(project.document, project.index, references)
        if schema_project:
            report.extend(
                validate_schema(
                    project.document,
                    schema_project.schema,
                    project.index,
                    profile,
                    snapshot,
                    coverage,
                    include_value_ranges=project.manager_project_id is not None,
                    legacy_atomic_only=project.manager_project_id is None,
                )
            )
            if snapshot is None:
                report.skip(
                    "ed.schema.type_incompatible",
                    "Структура конфигурации не передана: сопоставление типов не выполнялось",
                )
        else:
            report.skip(
                "ed.schema",
                "Схема формата и структура конфигурации не переданы: "
                "проверки по схеме не выполнялись",
            )
        if snapshot:
            report.extend(
                validate_structure(project.document, snapshot, project.index, profile, coverage)
            )
        else:
            report.skip(
                "ed.structure",
                "Структура конфигурации не передана: проверки по структуре не выполнялись",
            )
        report.issues = list(dict.fromkeys(report.issues))
        writer_metadata = {}
        if project.manager_project_id is not None:
            writer_report, writer_metadata = cast("EdWriterMixin", self)._manager_validation(
                project.manager_project_id, project.document.files[0].text, project_id
            )
            report.extend(writer_report)

        def issue_key(issue: Issue) -> tuple[str, int, str, str]:
            entity = project.index.by_address.get(issue.address)
            return (
                entity.span.file_id if entity else project.document.files[0].file_id,
                entity.span.char_start if entity else len(project.document.files[0].text),
                issue.check,
                issue.message,
            )

        report.issues.sort(key=issue_key)
        report.skipped.sort(key=lambda item: (item.check, item.reason))
        return {
            "project_id": project_id,
            **writer_metadata,
            **views.validation_view(
                report,
                level,
                check_prefix,
                address_prefix,
                section,
                offset,
                limit,
                explain_skipped=project.manager_project_id is not None,
            ),
            "references": views.references_summary(references),
            "profile": {
                "schema_id": schema_id,
                "structure_id": structure_id,
                "format_version": profile.format_version or None,
                "active_namespaces": list(profile.active_namespaces),
                "direction": direction,
                "fingerprints": {
                    "module": project.document.files[0].sha256,
                    "schema": hashlib.sha256(
                        "".join(
                            source.sha256
                            for package in schema_project.schema.packages
                            for source in package.sources
                        ).encode("ascii")
                    ).hexdigest()
                    if schema_project
                    else None,
                    "structure": structure_fingerprint,
                },
            },
            "coverage": {
                key: coverage[key]
                for key in (
                    "checked",
                    "not_applicable",
                    "opaque_conditions",
                    "handler_may_supply",
                    "unresolved_schema",
                )
            },
        }

    def _ed_validate_layered(
        self,
        project_id,
        project,
        schema_project,
        snapshot,
        structure_fingerprint,
        schema_id,
        structure_id,
        direction,
        headers_only,
        route_profile_id,
        level,
        check_prefix,
        address_prefix,
        section,
        offset,
        limit,
    ) -> dict[str, Any]:
        snap = project.layered
        route_service = cast(EdRoutesMixin, self)
        route_snapshot = (
            route_service._require_route(route_profile_id) if route_profile_id else None
        )
        routes = route_snapshot.profile if route_snapshot else None
        if route_snapshot:
            expected = tuple(Path(layer.root).resolve() for layer in snap.manager.layers[1:])
            problems = []
            if route_snapshot.root != Path(snap.manager.layers[0].root).resolve():
                problems.append(
                    "основная выгрузка отличается: "
                    + f"менеджер={self._host(Path(snap.manager.layers[0].root))}, "
                    + f"профиль={self._host(route_snapshot.root)}"
                )
            if route_snapshot.extension_roots != expected:
                problems.append(
                    "упорядоченные расширения отличаются: "
                    + f"менеджер={[self._host(p) for p in expected]}, "
                    + f"профиль={[self._host(p) for p in route_snapshot.extension_roots]}"
                )
            if problems:
                raise ValueError("route_profile_id не соответствует снимку: " + "; ".join(problems))
        key = (
            schema_id,
            structure_id,
            direction,
            headers_only,
            route_profile_id,
            id(schema_project),
            (structure_fingerprint, id(snapshot)),
            id(route_snapshot),
        )
        with self._lock:
            if not isinstance(self._ed_schemas, ValidationRegistry):
                self._ed_schemas = ValidationRegistry(
                    self._ed_schemas, lambda ident: self._drop_layer_validations("schema", ident)
                )
            if not isinstance(route_service._routes, ValidationRegistry):
                route_service._routes = ValidationRegistry(
                    route_service._routes,
                    lambda ident: self._drop_layer_validations("route", ident),
                )
            for old in tuple(project.validation_cache):
                if (
                    (old[0] == schema_id and old[5] != key[5])
                    or (old[1] == structure_id and old[6] != key[6])
                    or (old[4] == route_profile_id and old[7] != key[7])
                ):
                    del project.validation_cache[old]
            cached = project.validation_cache.get(key)
        if cached:
            return {
                "project_id": project_id,
                **views.validation_view(
                    cached.report, level, check_prefix, address_prefix, section, offset, limit
                ),
                **deepcopy(cached.metadata),
            }
        report = ValidationReport()
        if routes is None:
            for check in ("ed.layer.route.single_version", "ed.layer.route.context_mismatch"):
                report.skip(check, "Профиль маршрутов не передан")
        coverage: Counter[str] = Counter()
        selected = layer_views.select_views(
            snap, None if direction == "both" else direction, headers_only
        )
        references = []
        profile = ValidationProfile.build(
            schema_project.schema if schema_project else None,
            schema_project.format_version if schema_project else None,
            direction,
        )
        for view in selected:
            context = (
                replace(view.context, version_key=schema_project.format_version)
                if schema_project
                else view.context
            )
            profile = ValidationProfile.build(
                schema_project.schema if schema_project else None,
                schema_project.format_version if schema_project else None,
                context.direction,
            )
            report.extend(validate_effective_links(snap.manager, context, routes))
            report.extend(validate_layers(snap.manager, routes, context=context))
            references.append(ed.build_references(effective_document(snap.manager, context)))
            if schema_project:
                report.extend(
                    validate_effective_schema(
                        snap.manager, context, schema_project.schema, profile, snapshot, coverage
                    )
                )
                if snapshot is None:
                    report.skip(
                        "ed.schema.type_incompatible",
                        "Структура конфигурации не передана: сопоставление типов не выполнялось",
                    )
            else:
                report.skip(
                    "ed.schema",
                    "Схема формата и структура конфигурации не переданы: "
                    "проверки по схеме не выполнялись",
                )
            if snapshot:
                report.extend(
                    validate_effective_structure(snap.manager, context, snapshot, profile, coverage)
                )
            else:
                report.skip(
                    "ed.structure",
                    "Структура конфигурации не передана: проверки по структуре не выполнялись",
                )
        layer_index = layer_views.build_layer_addresses(snap.manager)

        def issue_order(issue):
            entity = project.index.by_address.get(issue.address)
            hit = layer_index.by_address.get(issue.address)
            span = (
                entity.span
                if entity
                else hit.entity.span
                if hit and hit.entity
                else hit.operation.origin.span
                if hit and hit.operation
                else None
            )
            return (
                span.file_id if span else project.document.files[0].file_id,
                span.char_start if span else len(project.document.files[0].text),
                issue.check,
                issue.message,
            )

        report.issues = sorted(set(report.issues), key=issue_order)
        report.skipped = sorted(set(report.skipped), key=lambda s: (s.check, s.reason))
        unknown = sum(
            op.kind == "unknown" or op.resolution == "unknown" for op in snap.manager.operations
        )
        metadata = {
            "references": {
                "contexts": [
                    {**layer_views.context_row(v.context), **views.references_summary(r)}
                    for v, r in zip(selected, references, strict=True)
                ]
            },
            "profile": {
                "schema_id": schema_id,
                "structure_id": structure_id,
                "route_profile_id": route_profile_id,
                "format_version": schema_project.format_version if schema_project else None,
                "active_namespaces": list(profile.active_namespaces),
                "direction": direction,
                "headers_only": headers_only,
                "fingerprints": {
                    "module": project.document.files[0].sha256,
                    "schema": hashlib.sha256(
                        "".join(
                            source.sha256
                            for package in schema_project.schema.packages
                            for source in package.sources
                        ).encode("ascii")
                    ).hexdigest()
                    if schema_project
                    else None,
                    "structure": structure_fingerprint,
                },
            },
            "coverage": {
                key: coverage[key]
                for key in (
                    "checked",
                    "not_applicable",
                    "opaque_conditions",
                    "handler_may_supply",
                    "unresolved_schema",
                )
            },
            "composition": {
                "status": snap.manager.status,
                "contexts": [layer_views.context_row(v.context) for v in selected],
                "known_operations": len(snap.manager.operations) - unknown,
                "unknown_operations": unknown,
            },
        }
        from kd2_rules_mcp.ed.layer_model import HANDLER_EXECUTION_NOTE

        metadata["handler_execution_note"] = HANDLER_EXECUTION_NOTE
        with self._lock:
            if self._ed_projects.get(project_id) is project:
                project.validation_cache[key] = LayerValidation(report, metadata)
        return {
            "project_id": project_id,
            **views.validation_view(
                report, level, check_prefix, address_prefix, section, offset, limit
            ),
            **deepcopy(metadata),
        }

    def _drop_layer_validations(self, dependency: str, ident: str) -> None:
        index = {"schema": 0, "structure": 1, "route": 4}[dependency]
        with self._lock:
            for project in self._ed_projects.values():
                for key in tuple(project.validation_cache):
                    if key[index] == ident:
                        del project.validation_cache[key]

    def ed_close(self, project_id: str) -> dict[str, Any]:
        with self._lock:
            manager_closed = getattr(self, "_manager_close", lambda _: None)(project_id)
            if manager_closed is not None:
                return manager_closed
            # Закрытие старого read-only снимка не требует актуальной модели владельца.
            snapshot = self._ed_projects.get(project_id)
            if snapshot is None:
                raise ProjectNotFoundError(f"Проект ED «{project_id}» не открыт")
            snapshot.validation_cache.clear()
            del self._ed_projects[project_id]
            return {"project_id": project_id, "closed": True}
