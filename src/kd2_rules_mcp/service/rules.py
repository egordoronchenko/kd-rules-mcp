"""Проекты правил, списки и точечные правки."""

from collections.abc import Mapping
from typing import Any

from kd2_rules_mcp.authoring.edits import (
    create_pko_with_properties,
    create_rule,
    delete_rule,
    find_rule,
    update_rule,
    update_rules,
)
from kd2_rules_mcp.authoring.pack import collect, pack_rules
from kd2_rules_mcp.errors import Kd2Error
from kd2_rules_mcp.kd2.diff import SECTIONS, RuleChange, diff_rules, ignored_header_fields
from kd2_rules_mcp.kd2.model import RulesDocument
from kd2_rules_mcp.kd2.rules_io import load_rules
from kd2_rules_mcp.service.base import ServiceBase
from kd2_rules_mcp.service.views import (
    counts,
    edit_view,
    listed_rule_rows,
    node_view,
    overview_groups,
    page_limit,
    report_summary,
    rule_group,
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
            project = self.workspace.get(project_id)
            view = self._project_view(project)
            document = project.document
            if document is not None:
                groups = overview_groups(document)
                if groups:
                    view["groups"] = groups
            return view

    def rules_list(
        self, project_id: str, section: str, text: str | None, offset: int, limit: int
    ) -> dict[str, Any]:
        with self._lock:
            rows = listed_rule_rows(self._document(project_id), section)
        if text:
            needle = text.casefold()
            rows = [row for row in rows if needle in " ".join(map(str, row.values())).casefold()]
        return slice_rows(rows, offset, limit)

    def rules_get(
        self, project_id: str, kind: str, key: str, owner: str, limit: int
    ) -> dict[str, Any]:
        with self._lock:
            rules = self._exchange(project_id)
            node = find_rule(rules, kind, key, owner)
            return node_view(node, page_limit(limit), rule_group(rules, node))

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

    def rule_update_many(
        self,
        project_id: str,
        kind: str,
        owner: str,
        fields: Mapping[str, Any],
        keys: list[str] | None,
        except_keys: list[str] | None,
        source_structure: str | None,
        target_structure: str | None,
    ) -> dict[str, Any]:
        """Одни поля у нескольких ПКС, групп ПКС или ПКЗ. Отказ не помечает проект изменённым."""
        if not fields:
            raise Kd2Error("Не переданы поля для изменения")
        with self._lock, self._sides(source_structure, target_structure) as (source, target):
            rules = self._exchange(project_id)
            results = update_rules(
                rules,
                kind,
                fields,
                owner=owner,
                keys=keys,
                except_keys=() if except_keys is None else except_keys,
                source=source,
                target=target,
            )
            self.workspace.mark_modified(project_id)
            return {
                "owner": owner,
                "kind": kind,
                "count": len(results),
                "updated": [edit_view(item) for item in results],
            }

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

    def rules_diff(
        self,
        left: str,
        right: str,
        include_header: bool,
        order: bool,
        section: str | None,
        offset: int,
        limit: int,
    ) -> dict[str, Any]:
        """Смысловой дифф двух сторон: открытый проект или файл правил по пути агента."""
        if section and section not in SECTIONS:
            known = ", ".join(SECTIONS)
            raise Kd2Error(f"Неизвестный раздел «{section}»; разделы: {known}")
        left_ref, left_doc = self._diff_side(left)
        right_ref, right_doc = self._diff_side(right)
        changes = diff_rules(left_doc, right_doc, include_header=include_header, order=order)
        if section:
            changes = [item for item in changes if item.section == section]
        return {
            "left": left_ref,
            "right": right_ref,
            "kind": "registration" if left_doc.root_tag == "ПравилаРегистрации" else "exchange",
            "ignored_fields": ignored_header_fields(include_header),
            "summary": _diff_summary(changes),
            "changes": slice_rows([_change_row(item) for item in changes], offset, limit),
        }

    def _diff_side(self, ref: str) -> tuple[dict[str, str], RulesDocument]:
        """Проект — по наличию идентификатора среди открытых; иначе путь, как у `rules_open`."""
        with self._lock:
            if ref in self.workspace.ids():
                return {"project_id": ref}, self._document(ref)
        path = self._read_path(ref)
        return {"path": self._host(path)}, load_rules(path)


def _diff_summary(changes: list[RuleChange]) -> dict[str, dict[str, int]]:
    """Счётчики по непустым разделам, в порядке разделов инструментов."""
    counts: dict[str, dict[str, int]] = {}
    for change in changes:
        bucket = counts.setdefault(change.section, {"added": 0, "removed": 0, "changed": 0})
        bucket[change.change] += 1
    return {name: counts[name] for name in SECTIONS if name in counts}


def _change_row(change: RuleChange) -> dict[str, Any]:
    row: dict[str, Any] = {
        "section": change.section,
        "address": change.address,
        "change": change.change,
    }
    if change.field is not None:
        row["field"] = change.field
    if change.old is not None:
        row["old"] = change.old
    if change.new is not None:
        row["new"] = change.new
    if change.handler_diff is not None:
        row["handler_diff"] = change.handler_diff
    return row
