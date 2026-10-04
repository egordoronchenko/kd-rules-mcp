"""Проверка правил и выгрузка обработчиков."""

from typing import Any

from kd2_rules_mcp.errors import Kd2Error
from kd2_rules_mcp.kd2.model import ExchangeRules, RegistrationRules
from kd2_rules_mcp.service.base import ServiceBase
from kd2_rules_mcp.service.views import page_limit, report_view
from kd2_rules_mcp.validation.algorithms import check_algorithm_refs
from kd2_rules_mcp.validation.exchange_plan import check_exchange_plan
from kd2_rules_mcp.validation.format import check_format
from kd2_rules_mcp.validation.handlers import export_handlers, locate
from kd2_rules_mcp.validation.registration import check_registration
from kd2_rules_mcp.validation.search import check_search_objects, check_search_params
from kd2_rules_mcp.validation.structure import check_structures


class ChecksMixin(ServiceBase):
    """rules_validate, handlers_export и handlers_locate."""

    def rules_validate(
        self,
        project_id: str,
        source_structure: str | None,
        target_structure: str | None,
        level: str | None,
        check_prefix: str | None,
        offset: int,
        limit: int,
    ) -> dict[str, Any]:
        with self._lock, self._sides(source_structure, target_structure) as (source, target):
            document = self._document(project_id)
            report = check_format(document)
            if isinstance(document, ExchangeRules):
                report.extend(check_structures(document, source, target))
                report.extend(check_algorithm_refs(document))
                report.extend(check_search_params(document))
                report.extend(check_search_objects(document, target))
                report.extend(check_exchange_plan(document, source, target))
            elif isinstance(document, RegistrationRules):
                report.extend(check_registration(document, source))
        return report_view(report, level, check_prefix, offset, limit)

    def handlers_export(self, project_id: str, folder: str, limit: int) -> dict[str, Any]:
        with self._lock:
            rules = self._exchange(project_id)
            out_dir = self._writable(folder)
            export = export_handlers(rules, out_dir)
            self.workspace.remember_handlers(project_id, export)
        limit = page_limit(limit)
        files = [
            {"file": item.name, "address": item.address, "event": item.event}
            for item in export.files
        ]
        return {
            "folder": self._host(out_dir),
            "count": len(files),
            "removed": len(export.removed),
            "files": files[:limit],
            "has_more": len(files) > limit,
        }

    def handlers_locate(self, project_id: str, file_name: str, line: int) -> dict[str, Any]:
        with self._lock:
            export = self.workspace.get(project_id).handlers
        if export is None:
            raise Kd2Error(
                f"Обработчики проекта «{project_id}» ещё не выгружались: сначала handlers_export"
            )
        found = locate(export, file_name, line)
        if found is None:
            return {"found": False, "file": file_name, "line": line}
        return {
            "found": True,
            "address": found.address,
            "event": found.event,
            "handler_line": found.line,
        }
