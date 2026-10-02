"""Проекты правил, списки и точечные правки."""

from collections.abc import Mapping
from typing import Any

from kd2_rules_mcp.authoring.edits import (
    create_pko_with_properties,
    create_rule,
    delete_rule,
    find_rule,
    update_rule,
)
from kd2_rules_mcp.authoring.pack import collect, pack_rules
from kd2_rules_mcp.service.base import ServiceBase
from kd2_rules_mcp.service.views import (
    counts,
    node_view,
    page_limit,
    report_summary,
    rule_row,
    section_rules,
    slice_rows,
)
from kd2_rules_mcp.validation.format import check_format


class RulesMixin(ServiceBase):
    """Открытие проектов правил, чтение разделов и правки по адресу."""

    def rules_open(self, path: str) -> dict[str, Any]:
        with self._lock:
            opened = self.workspace.open_rules(self._read_path(path))
            view = self._project_view(opened.project)
            view["reused"] = opened.reused
            if opened.reused:
                view["source_changed"] = opened.source_changed
            return view

    def rules_create(
        self, source_structure: str, target_structure: str, project_id: str | None = None
    ) -> dict[str, Any]:
        with self._lock:
            project = self.workspace.create_exchange(
                self.store, source_structure, target_structure, project_id
            )
            return self._project_view(project)

    def rules_projects(self) -> dict[str, Any]:
        with self._lock:
            return {
                "projects": [self._project_view(item) for item in self.workspace.iter_projects()]
            }

    def rules_close(self, project_id: str) -> dict[str, Any]:
        """Удаляет рабочий проект и его снимок. Файл `rules_save` не трогает."""
        with self._lock:
            removed = self.workspace.close(project_id)
            return {"project_id": project_id, "closed": True, "snapshot_removed": removed}

    def rules_overview(self, project_id: str) -> dict[str, Any]:
        with self._lock:
            return self._project_view(self.workspace.get(project_id))

    def rules_list(
        self, project_id: str, section: str, text: str | None, offset: int, limit: int
    ) -> dict[str, Any]:
        with self._lock:
            document = self._document(project_id)
            rows = [rule_row(node) for node in section_rules(document, section)]
        if text:
            needle = text.casefold()
            rows = [row for row in rows if needle in " ".join(map(str, row.values())).casefold()]
        return slice_rows(rows, offset, limit)

    def rules_get(
        self, project_id: str, kind: str, key: str, owner: str, limit: int
    ) -> dict[str, Any]:
        with self._lock:
            node = find_rule(self._exchange(project_id), kind, key, owner)
            return node_view(node, page_limit(limit))

    def rules_save(self, project_id: str, path: str, overwrite: bool) -> dict[str, Any]:
        with self._lock:
            saved = self.workspace.save(
                project_id,
                self._writable(path),
                overwrite=overwrite,
                allowed=list(self.rules_dirs().values()),
            )
            document = self._document(project_id)
            report = check_format(document)
            return {
                "project_id": project_id,
                "path": self._host(saved),
                "size_bytes": saved.stat().st_size,
                "counts": counts(document),
                "format_check": report_summary(report),
            }

    def rules_pack(
        self,
        folder: str,
        exchange_rules: str,
        correspondent_rules: str,
        registration_rules: str,
        path: str,
        overwrite: bool,
    ) -> dict[str, Any]:
        with self._lock:
            source_dir = self._read_path(folder) if folder else None
            explicit = {
                role: self._read_path(value) if value else None
                for role, value in (
                    ("exchange", exchange_rules),
                    ("correspondent", correspondent_rules),
                    ("registration", registration_rules),
                )
            }
            files = collect(source_dir, explicit)
            default = source_dir.name if source_dir is not None else files["exchange"].stem
            result = pack_rules(
                files, self._writable(path or f"{default}.zip"), overwrite=overwrite
            )
            return {
                "path": self._host(result.path),
                "size_bytes": result.path.stat().st_size,
                "load_with": result.form,
                "files": [
                    {
                        "file": item.name,
                        "path": self._host(item.source),
                        "size_bytes": item.size_bytes,
                        "rules": item.summary,
                    }
                    for item in result.files
                ],
                "warnings": result.warnings,
            }

    def rule_create(
        self, project_id: str, kind: str, key: str, group: str = "", **options: Any
    ) -> dict[str, Any]:
        return self._edit(create_rule, project_id, kind, key, group=group, **options)

    def rule_update(self, project_id: str, kind: str, key: str, **options: Any) -> dict[str, Any]:
        return self._edit(update_rule, project_id, kind, key, **options)

    def rule_delete(
        self,
        project_id: str,
        kind: str,
        key: str,
        owner: str,
        source_structure: str | None,
        target_structure: str | None,
    ) -> dict[str, Any]:
        with self._lock, self._sides(source_structure, target_structure) as (source, target):
            rules = self._exchange(project_id)
            result = delete_rule(rules, kind, key, owner=owner, source=source, target=target)
            return self._edited(project_id, result)

    def pko_create_from_candidates(
        self,
        project_id: str,
        code: str,
        source_structure: str,
        target_structure: str,
        source_object: str,
        target_object: str,
        fields: Mapping[str, Any] | None,
        group: str = "",
    ) -> dict[str, Any]:
        with (
            self._lock,
            self._structure(source_structure) as source,
            self._structure(target_structure) as target,
        ):
            rules = self._exchange(project_id)
            result = create_pko_with_properties(
                rules, code, source, target, source_object, target_object, fields, group=group
            )
            return self._edited(project_id, result)
