"""Инструменты структур метаданных и списка проектов."""

from collections.abc import Sequence
from typing import Any

from kd2_rules_mcp.errors import Kd2Error
from kd2_rules_mcp.projects import resolve
from kd2_rules_mcp.service.base import ServiceBase
from kd2_rules_mcp.service.views import page_limit, page_view, project_structure_id, require_found
from kd2_rules_mcp.structures.queries import (
    compare_structures,
    describe_object,
    exchange_plan_content,
    list_objects,
    object_values,
)


class StructuresMixin(ServiceBase):
    """Загрузка и запросы структур, список проектов."""

    def structure_list(self) -> dict[str, Any]:
        items = []
        for structure_id in self.store.ids():
            meta = self.store.meta(structure_id)
            items.append(
                {
                    "structure_id": structure_id,
                    "configuration": meta.get("config_name", ""),
                    "synonym": meta.get("config_synonym", ""),
                    "version": meta.get("config_version", ""),
                    "source": meta.get("source", ""),
                    "source_path": self._host_text(meta.get("source_path", "")),
                    "extensions": meta.get("extensions", "[]"),
                    "loaded_at": meta.get("loaded_at", ""),
                }
            )
        return {"structures": items}

    def project_list(self) -> dict[str, Any]:
        catalog = self._catalog()
        writable = self.rules_dirs()
        projects = []
        for project in catalog.projects.values():
            folder = self.settings.project_dirs.get(project.id)
            rules_dir = writable.get(project.id)
            row: dict[str, Any] = {
                "project": project.id,
                "name": project.name,
                "available": folder is not None and folder.is_dir(),
                "configurations": {
                    config.id: {
                        "structure_id": project_structure_id(project.id, config.id),
                        "dump": config.dump,
                        "extensions": list(config.extensions),
                    }
                    for config in project.configurations.values()
                },
                "bases": {
                    base.id: {
                        "role": base.role,
                        "configuration": base.configuration,
                        **(
                            {
                                "data_mcp": base.data_mcp,
                                "data_mcp_server": f"{project.id}-{base.data_mcp}",
                            }
                            if base.data_mcp and base.is_sandbox
                            else {}
                        ),
                    }
                    for base in project.bases.values()
                },
                "code_mcp": list(project.code_mcp),
                "code_mcp_server": [f"{project.id}-{name}" for name in project.code_mcp],
            }
            if folder is not None:
                row["folder"] = self._host(folder)
            if project.rules_dir:
                row["rules_dir"] = {
                    "path": self._host(rules_dir) if rules_dir else project.rules_dir,
                    "writable": rules_dir is not None and rules_dir.is_dir(),
                }
            projects.append(row)
        exchanges = [
            {"plan": item.plan, "projects": list(item.projects)} for item in catalog.exchanges
        ]
        return {
            "projects": projects,
            "exchanges": exchanges,
            "workspace": self._host(self.workspace.root),
            "shared_mcp": list(catalog.shared_mcp),
        }

    def structure_load_project(
        self,
        project_id: str,
        configuration_id: str = "full",
        structure_id: str | None = None,
        force: bool = False,
    ) -> dict[str, Any]:
        catalog = self._catalog()
        config = catalog.configuration(project_id, configuration_id)
        folder = self.settings.project_dirs.get(project_id)
        if folder is None:
            known = ", ".join(self.settings.project_dirs) or "нет"
            raise Kd2Error(
                f"Папка проекта «{project_id}» на сервере не задана (projects.local.yaml → "
                f"scripts/setup_local.py); заданы: {known}"
            )
        main = resolve(folder, config.dump)
        extensions = [resolve(folder, item) for item in config.extensions]
        for path in (main, *extensions):
            if not path.is_dir():
                raise Kd2Error(f"Нет каталога выгрузки {self._host(path)}")
        target_id = structure_id or project_structure_id(project_id, configuration_id)
        result = self.store.load_xml(target_id, main, extensions, force=force)
        return {**self._load_view(result), "project": project_id, "configuration": config.id}

    def structure_load_md83exp(
        self, structure_id: str, path: str, force: bool = False
    ) -> dict[str, Any]:
        source = self._read_path(path)
        return self._load_view(self.store.load_md83exp(structure_id, source, force=force))

    def structure_load_xml(
        self,
        structure_id: str,
        configuration_path: str,
        extension_paths: Sequence[str] = (),
        force: bool = False,
    ) -> dict[str, Any]:
        main = self._read_path(configuration_path)
        extensions = [self._read_path(item) for item in extension_paths]
        return self._load_view(self.store.load_xml(structure_id, main, extensions, force=force))

    def structure_objects(
        self, structure_id: str, kind: str | None, text: str | None, offset: int, limit: int
    ) -> dict[str, Any]:
        with self._structure(structure_id) as conn:
            return page_view(list_objects(conn, kind or None, text or None, offset, limit))

    def structure_object(
        self, structure_id: str, name: str, offset: int, limit: int
    ) -> dict[str, Any]:
        with self._structure(structure_id) as conn:
            found = require_found(describe_object(conn, name, offset, limit))
            return {**found, "properties": page_view(found["properties"])}

    def structure_values(
        self, structure_id: str, name: str, offset: int, limit: int
    ) -> dict[str, Any]:
        with self._structure(structure_id) as conn:
            return page_view(require_found(object_values(conn, name, offset, limit)))

    def structure_plan_content(
        self, structure_id: str, exchange_plan: str, offset: int, limit: int
    ) -> dict[str, Any]:
        with self._structure(structure_id) as conn:
            return page_view(
                require_found(exchange_plan_content(conn, exchange_plan, offset, limit))
            )

    def structure_compare(self, old_id: str, new_id: str, limit: int) -> dict[str, Any]:
        with self._structure(old_id) as old, self._structure(new_id) as new:
            diff = compare_structures(old, new)
        limit = page_limit(limit)
        lists = {
            "added_objects": diff.added_objects,
            "removed_objects": diff.removed_objects,
            "added_properties": diff.added_properties,
            "removed_properties": diff.removed_properties,
            "changed_properties": diff.changed_properties,
            "added_values": diff.added_values,
            "removed_values": diff.removed_values,
        }
        return {
            "counts": diff.counts(),
            "limit": limit,
            **{key: values[:limit] for key, values in lists.items()},
        }
