"""Постраничная трассировка неизменяемого снимка менеджера EnterpriseData."""

from typing import Any, cast

from kd_rules_mcp.errors import EdSchemaNotFoundError
from kd_rules_mcp.service import ed_views as views
from kd_rules_mcp.service.base import ServiceBase
from kd_rules_mcp.service.ed import EdMixin
from kd_rules_mcp.service.ed_chain import chain_view, resolve_version
from kd_rules_mcp.validation.ed_trace import trace


class EdTraceMixin(ServiceBase):
    """Адаптер общего ядра: открытые проекты под блокировкой и страницы ответа."""

    def ed_trace(
        self,
        project_id: str,
        object: str,
        direction: str = "send",
        schema_id: str | None = None,
        version: str | None = None,
        property: str | None = None,
        depth: int = 1,
        offset: int = 0,
        limit: int = 50,
    ) -> dict[str, Any]:
        views.validate_page(offset, limit)
        if direction not in ("send", "receive"):
            raise ValueError("direction: send или receive")
        if type(depth) is not int or not 0 <= depth <= 3:
            raise ValueError("depth: целое число от 0 до 3")
        if not object.strip():
            raise ValueError("object: пустая строка")
        # EdMixin приходит через EdWriterMixin в Kd2Service.
        reader = cast(EdMixin, self)
        with self._lock:
            project = reader._ed_project(project_id)
            schema = reader._ed_schemas.get(schema_id) if schema_id is not None else None
            if schema_id is not None and schema is None:
                raise EdSchemaNotFoundError("Схема формата не открыта")
            version = resolve_version(version, ("schema", schema))
            view = chain_view(project_id, project, direction, schema, version, with_references=True)
        result: dict[str, Any] = dict(trace(view, object, property=property, depth=depth))
        result["schema_id"] = schema_id
        for section in (
            "matched",
            "paths",
            "properties",
            "handlers",
            "search",
            "cascades",
            "breaks",
        ):
            if section == "search" and direction != "receive":
                result.pop(section)
                continue
            result[section] = views.page(result[section], offset, limit)
        return result
