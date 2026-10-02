"""Правила регистрации и черновик правил корреспондента."""

from collections.abc import Mapping, Sequence
from typing import Any

from kd2_rules_mcp.authoring.correspondent import mirror_rules
from kd2_rules_mcp.authoring.registration import build_registration_rules
from kd2_rules_mcp.service.base import ServiceBase
from kd2_rules_mcp.service.views import clip, page_limit, registration_object


class GenerateMixin(ServiceBase):
    """registration_build и correspondent_draft."""

    def registration_build(
        self,
        structure_id: str,
        exchange_plan: str,
        rules_project_id: str | None,
        objects: Sequence[Mapping[str, Any]] | None,
        project_id: str | None = None,
    ) -> dict[str, Any]:
        with self._lock, self._structure(structure_id) as conn:
            exchange = self._exchange(rules_project_id) if rules_project_id else None
            specs = [registration_object(item) for item in objects] if objects else None
            build = build_registration_rules(
                conn, exchange_plan, exchange_rules=exchange, objects=specs
            )
            project = self.workspace.add(build.document, project_id, label=f"reg-{exchange_plan}")
            return {**self._project_view(project), "warnings": build.warnings}

    def correspondent_draft(
        self,
        project_id: str,
        codes: Sequence[str],
        target_structure: str | None,
        limit: int,
        new_project_id: str | None = None,
    ) -> dict[str, Any]:
        limit = page_limit(limit)
        with self._lock, self._sides(target_structure, None) as (target, _):
            result = mirror_rules(self._exchange(project_id), codes, target)
            project = self.workspace.add(result.rules, new_project_id, label=f"corr-{project_id}")
            handlers = [
                {
                    "address": item.address,
                    "event": item.event,
                    "note": item.note,
                    "code": clip(item.code),
                }
                for item in result.handlers
            ]
            disabled = [
                {"address": item.address, "reason": item.reason} for item in result.disabled
            ]
            return {
                **self._project_view(project),
                "draft": result.draft,
                "missing": result.missing,
                "notes": result.notes[:limit],
                "handlers_total": len(handlers),
                "handlers": handlers[:limit],
                "disabled_total": len(disabled),
                "disabled": disabled[:limit],
            }
