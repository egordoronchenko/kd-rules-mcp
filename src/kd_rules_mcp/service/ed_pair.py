"""`ed_pair_validate`: статическая сверка открытых модулей источника и приёмника."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from kd_rules_mcp.errors import EdSchemaNotFoundError
from kd_rules_mcp.service import ed_views as views
from kd_rules_mcp.service.base import ServiceBase
from kd_rules_mcp.service.ed_chain import chain_view, resolve_version
from kd_rules_mcp.validation.ed_pair import ASSUMPTIONS, validate_pair
from kd_rules_mcp.validation.report import Level

if TYPE_CHECKING:
    from kd_rules_mcp.service.ed import EdMixin, EdProject
    from kd_rules_mcp.service.ed_schema import SchemaProject

SECTIONS = ("issues", "objects", "skipped")


class EdPairMixin(ServiceBase):
    """Пара модулей: источник при отправке, приёмник при получении, одна версия формата."""

    _ed_schemas: dict[str, SchemaProject]

    def _ed_pair_side(self, project_id: str, schema_id: str | None):
        project: EdProject
        if not isinstance(project_id, str) or not project_id.strip():
            raise ValueError("Нужен идентификатор открытого модуля")
        project, _ = cast("EdMixin", self)._ensure_references(project_id)
        schema = None
        if schema_id is not None:
            schema = self._ed_schemas.get(schema_id)
            if schema is None:
                raise EdSchemaNotFoundError(f"Схема формата не открыта: {schema_id}")
        return project, schema

    def ed_pair_validate(
        self,
        source_project_id: str,
        target_project_id: str,
        version: str | None = None,
        source_schema_id: str | None = None,
        target_schema_id: str | None = None,
        section: str = "issues",
        level: str | None = None,
        check_prefix: str | None = None,
        address_prefix: str | None = None,
        format_object: str | None = None,
        offset: int = 0,
        limit: int = 50,
    ) -> dict[str, Any]:
        """Отправка источника против получения приёмника; базы не читаются."""
        views.validate_page(offset, limit)
        if section not in SECTIONS:
            raise ValueError("Раздел отчёта: issues, objects или skipped")
        if level:
            Level(level)
        with self._lock:
            source, source_schema = self._ed_pair_side(source_project_id, source_schema_id)
            target, target_schema = self._ed_pair_side(target_project_id, target_schema_id)
            chosen = resolve_version(
                version, ("источника", source_schema), ("приёмника", target_schema)
            )
            result = validate_pair(
                chain_view(
                    source_project_id,
                    source,
                    "send",
                    source_schema,
                    chosen,
                    with_references=True,
                ),
                chain_view(
                    target_project_id,
                    target,
                    "receive",
                    target_schema,
                    chosen,
                    with_references=True,
                ),
            )
        objects = result.objects
        if format_object is not None:
            objects = [row for row in objects if row["format_object"] == format_object.strip()]
        statuses: dict[str, int] = {}
        for row in result.objects:
            statuses[row["status"]] = statuses.get(row["status"], 0) + 1
        response: dict[str, Any] = {
            "source_project_id": source_project_id,
            "target_project_id": target_project_id,
            "version": result.version,
            "source_schema_id": source_schema_id,
            "target_schema_id": target_schema_id,
            "static": True,
            "assumptions": list(ASSUMPTIONS),
        }
        if section == "objects":
            response["summary"] = views.report_summary(result.report)
            response["objects"] = views.page(objects, offset, limit)
        else:
            response.update(
                views.validation_view(
                    result.report,
                    level,
                    check_prefix,
                    address_prefix,
                    section,
                    offset,
                    limit,
                )
            )
        response["summary"]["objects"] = statuses
        response["coverage"] = dict(sorted(result.coverage.items()))
        return response
