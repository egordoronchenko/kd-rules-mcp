"""Чтение проектов ED в памяти процесса под общей блокировкой сервиса."""

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from kd2_rules_mcp import ed
from kd2_rules_mcp.ed import address as addresses
from kd2_rules_mcp.errors import (
    AmbiguousAddressError,
    EdFormatError,
    EdReadError,
    EdResourceLimitError,
    Kd2Error,
    ProjectNotFoundError,
    RuleNotFoundError,
)
from kd2_rules_mcp.service import ed_views as views
from kd2_rules_mcp.service.base import ServiceBase
from kd2_rules_mcp.service.paths import Settings


@dataclass(frozen=True)
class EdProject:
    path: Path
    document: ed.EdDocument
    index: addresses.AddressIndex
    entities: dict[str, ed.Entity]
    by_address: dict[str, ed.Entity]


class EdMixin(ServiceBase):
    """Шесть инструментов только для чтения неизменяемых снимков ED."""

    def __init__(self, settings: Settings) -> None:
        super().__init__(settings)
        self._ed_projects: dict[str, EdProject] = {}

    def _ed_project(self, project_id: str) -> EdProject:
        if project_id not in self._ed_projects:
            raise ProjectNotFoundError(f"Проект ED «{project_id}» не открыт")
        return self._ed_projects[project_id]

    def ed_open(self, path: str) -> dict[str, Any]:
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
                ((key, p) for key, p in self._ed_projects.items() if p.path == local), None
            )
            changed = False
            if existing:
                project_id, project = existing
                try:
                    # Хеш повторного открытия вычисляется потоково, без нового разбора.
                    with local.open("rb") as stream:
                        current = hashlib.file_digest(stream, "sha256").hexdigest()
                except OSError as error:
                    raise EdReadError("Файл менеджера недоступен") from error
                changed = current != project.document.files[0].sha256
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
                if project_id in self._ed_projects:
                    project_id = f"ed-{stem}-{digest}"
                index = addresses.build_addresses(document)
                entities = {e.entity_id: e for e in document.entities()}
                by_address = {a.casefold(): e for a, e in index.by_address.items()}
                for entity in entities.values():
                    by_address.setdefault(views.address_of(entity, index).casefold(), entity)
                project = EdProject(local, document, index, entities, by_address)
                self._ed_projects[project_id] = project
            doc = project.document
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

    @staticmethod
    def _ed_counts(document: ed.EdDocument) -> dict[str, int]:
        return {key: value for key, value in document.counts.items() if key != "values"}

    def ed_overview(self, project_id: str) -> dict[str, Any]:
        with self._lock:
            doc = self._ed_project(project_id).document
            mentions = doc.conversion.format_version_mentions
            coverage = doc.coverage
            return {
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

    def ed_list(
        self,
        project_id: str,
        kind: str,
        text: str | None = None,
        format_object: str | None = None,
        metadata_object: str | None = None,
        offset: int = 0,
        limit: int = 50,
    ) -> dict[str, Any]:
        views.validate_page(offset, limit)
        if kind not in views.KINDS:
            raise ValueError(f"Неизвестный вид ED: {kind}")
        with self._lock:
            project = self._ed_project(project_id)
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
    ) -> dict[str, Any]:
        views.validate_page(offset, limit)
        views.validate_page(text_offset, text_limit, 8000)
        with self._lock:
            project = self._ed_project(project_id)
            if address.casefold() in project.index.conflicts:
                candidates = project.index.conflicts[address.casefold()]
                error = AmbiguousAddressError(f"Неоднозначный адрес: {address}", candidates)
                error.candidate_page = views.page(list(candidates), offset, limit)
                raise error
            entity = project.by_address.get(address.casefold())
            if entity is None:
                raise RuleNotFoundError(f"Сущность ED не найдена: {address}")
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
                "address": next(
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
        self, project_id: str, line: int, offset: int = 0, limit: int = 50
    ) -> dict[str, Any]:
        views.validate_page(offset, limit)
        with self._lock:
            project = self._ed_project(project_id)
            doc = project.document
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

    def ed_close(self, project_id: str) -> dict[str, Any]:
        with self._lock:
            self._ed_project(project_id)
            del self._ed_projects[project_id]
            return {"project_id": project_id, "closed": True}
