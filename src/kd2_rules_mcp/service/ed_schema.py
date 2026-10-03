"""Четыре инструмента навигации по снимкам XDTO в памяти процесса."""

import hashlib
import json
import re
from collections import Counter
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from kd2_rules_mcp.ed.schema import EdSchema, QName, load_schema
from kd2_rules_mcp.ed.schema.xdto import metadata
from kd2_rules_mcp.errors import (
    EdSchemaAmbiguousImportError,
    EdSchemaNotFoundError,
    EdSchemaProfileMismatchError,
    EdSchemaReadError,
    EdSchemaResourceLimitError,
    EdSchemaTypeNotFoundError,
    Kd2Error,
)
from kd2_rules_mcp.projects import ProjectConfigError, resolve
from kd2_rules_mcp.service import ed_schema_views as views
from kd2_rules_mcp.service.base import ServiceBase
from kd2_rules_mcp.service.paths import Settings

MAX_SCHEMAS = 256
MAX_STORED_BYTES = 512 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class SchemaProject:
    schema: EdSchema
    format_version: str


class EdSchemaMixin(ServiceBase):
    """Разбор вне общей блокировки, атомарная публикация и неизменяемые снимки."""

    def __init__(self, settings: Settings) -> None:
        super().__init__(settings)
        self._ed_schemas: dict[str, SchemaProject] = {}
        self._ed_schema_requests: dict[str, str] = {}

    def _schema_path(self, path: str) -> Path:
        try:
            return self._read_path(path).resolve()
        except (Kd2Error, OSError) as error:
            raise EdSchemaReadError("Пакет XDTO недоступен по переданному пути") from error

    def _schema_project(self, schema_id: str) -> SchemaProject:
        try:
            return self._ed_schemas[schema_id]
        except KeyError as error:
            raise EdSchemaNotFoundError("Схема формата не открыта") from error

    @staticmethod
    def _schema_changed(project: SchemaProject) -> bool:
        for package in project.schema.packages:
            for source in package.sources:
                try:
                    with Path(source.path).open("rb") as stream:
                        digest = hashlib.file_digest(stream, "sha256").hexdigest()
                except OSError:
                    return True
                if digest != source.sha256:
                    return True
        return False

    @staticmethod
    def _schema_open_view(project: SchemaProject, reused: bool, changed: bool) -> dict[str, Any]:
        schema = project.schema
        base = schema.packages[0]
        return {
            "schema_id": schema.schema_id,
            "base_namespace": schema.base_namespace,
            "format_version": project.format_version,
            "package": base.metadata_name,
            "counts": views.counts(schema),
            "status": schema.status,
            "diagnostics_summary": dict(Counter(d.code for d in schema.diagnostics)),
            "reused": reused,
            "source_changed": changed,
        }

    def ed_schema_open(
        self,
        format_version: str,
        path: str | None = None,
        project: str | None = None,
        configuration: str = "full",
        package: str | None = None,
        imports: dict[str, str] | None = None,
        extensions: list[str] | None = None,
    ) -> dict[str, Any]:
        if not isinstance(format_version, str) or not format_version.strip():
            raise ValueError("Нужна явно указанная версия формата")
        if (path is not None) == (project is not None) or (
            path is not None and package is not None
        ):
            raise ValueError("Укажите path либо project и package")
        if path is not None and (not path.strip() or configuration != "full"):
            raise ValueError("Configuration применим только к project")
        if project is not None and (not project.strip() or not package or not package.strip()):
            raise ValueError("Нужны project и package")
        if package and (package in (".", "..") or any(c in package for c in "/\\:")):
            raise ValueError("Нужно точное имя пакета без разделителей пути")
        if imports is not None and (
            not isinstance(imports, dict)
            or any(
                not isinstance(k, str) or not k or not isinstance(v, str) or not v.strip()
                for k, v in imports.items()
            )
        ):
            raise ValueError("Imports — словарь URI → путь")
        if extensions is not None and (
            not isinstance(extensions, list)
            or any(not isinstance(p, str) or not p.strip() for p in extensions)
        ):
            raise ValueError("Extensions — список путей")
        catalog_paths: dict[str, list[Path]] = {}
        if project is not None:
            config = self._catalog().configuration(project, configuration)
            folder = self.settings.project_dirs.get(project)
            if folder is None:
                raise ProjectConfigError("Папка проекта не подключена")
            root = self._schema_path(self._host(resolve(folder, config.dump))) / "XDTOPackages"
            local = self._schema_path(self._host(root / f"{package}.xml"))
        else:
            local = self._schema_path(path or "")
            root = None
        explicit = {uri: self._schema_path(p) for uri, p in (imports or {}).items()}
        extra = tuple(self._schema_path(p) for p in (extensions or []))
        request = json.dumps(
            [
                str(local),
                format_version,
                sorted((k, str(v)) for k, v in explicit.items()),
                [str(p) for p in extra],
                str(root),
            ],
            ensure_ascii=False,
        )
        with self._lock:
            ident = self._ed_schema_requests.get(request)
            existing = self._ed_schemas.get(ident or "")
        if existing:
            return self._schema_open_view(existing, True, self._schema_changed(existing))
        if root is not None:
            for description in sorted(root.glob("*.xml")):
                _name, uri, _, _ = metadata(description)
                catalog_paths.setdefault(uri, []).append(description)

        def locate(uri: str) -> Path | None:
            if uri in explicit:
                return explicit[uri]
            candidates = catalog_paths.get(uri, [])
            if len(candidates) > 1:
                raise EdSchemaAmbiguousImportError(
                    "URI импорта имеет несколько пакетов; задайте imports"
                )
            return candidates[0] if candidates else None

        schema = load_schema(local, extensions=extra, locate_import=locate)
        match = re.search(r"/EnterpriseData/(\d+(?:\.\d+)*)/?$", schema.base_namespace)
        if match and tuple(map(int, match[1].split("."))) != tuple(
            map(int, format_version.split("."))
        ):
            raise EdSchemaProfileMismatchError("Версия URI пакета не совпадает с format_version")
        schema_id = hashlib.sha256((schema.schema_id + format_version).encode()).hexdigest()[:24]
        schema = replace(schema, schema_id="schema-" + schema_id)
        candidate = SchemaProject(schema, format_version)
        with self._lock:
            ident = self._ed_schema_requests.get(request)
            if ident:
                winner = self._schema_project(ident)
                return self._schema_open_view(winner, True, self._schema_changed(winner))
            existing = self._ed_schemas.get(schema.schema_id)
            if existing:
                self._ed_schema_requests[request] = schema.schema_id
                return self._schema_open_view(existing, True, self._schema_changed(existing))
            stored = sum(
                s.bytes
                for p in self._ed_schemas.values()
                for pkg in p.schema.packages
                for s in pkg.sources
            )
            added = sum(s.bytes for pkg in schema.packages for s in pkg.sources)
            if len(self._ed_schemas) >= MAX_SCHEMAS or stored + added > MAX_STORED_BYTES:
                raise EdSchemaResourceLimitError("Превышен лимит открытых схем или исходных байтов")
            self._ed_schemas[schema.schema_id] = candidate
            self._ed_schema_requests[request] = schema.schema_id
        return self._schema_open_view(candidate, False, False)

    def ed_schema_types(
        self,
        schema_id: str,
        namespace: str | None = None,
        kind: str | None = None,
        text: str | None = None,
        offset: int = 0,
        limit: int = 50,
    ) -> dict[str, Any]:
        with self._lock:
            schema = self._schema_project(schema_id).schema
        return views.types_page(schema, namespace, kind, text, offset, limit)

    def ed_schema_type(
        self,
        schema_id: str,
        qname: str,
        section: str = "properties",
        offset: int = 0,
        limit: int = 50,
        include_origin: bool = False,
    ) -> dict[str, Any]:
        with self._lock:
            schema = self._schema_project(schema_id).schema
        typ = schema.by_id.get(qname)
        if typ is None:
            match = re.fullmatch(r"\{([^}]*)\}(.+)", qname)
            if match:
                typ = schema.types.get(QName(*match.groups()))
            else:
                # Короткое имя — только в активных пространствах: база, затем расширения по порядку.
                # Импорт сам по себе пространство не активирует.
                found = [
                    schema.types[q]
                    for uri in (schema.base_namespace, *schema.extension_namespaces)
                    if (q := QName(uri, qname)) in schema.types
                ]
                if len(found) > 1:
                    raise ValueError(
                        "Имя типа есть в нескольких пространствах имён: укажите полное `{URI}Имя`"
                    )
                typ = found[0] if found else None
        if typ is None:
            raise EdSchemaTypeNotFoundError("Тип не найден в схеме")
        return views.type_view(schema, typ, section, offset, limit, include_origin)

    def ed_schema_close(self, schema_id: str) -> dict[str, Any]:
        with self._lock:
            closed = self._ed_schemas.pop(schema_id, None) is not None
            self._ed_schema_requests = {
                k: v for k, v in self._ed_schema_requests.items() if v != schema_id
            }
        return {"schema_id": schema_id, "closed": closed}
