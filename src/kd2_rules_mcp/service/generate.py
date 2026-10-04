"""Правила регистрации и черновик правил корреспондента."""

from collections.abc import Mapping, Sequence
from typing import Any

from kd2_rules_mcp.authoring.correspondent import mirror_rules
from kd2_rules_mcp.authoring.registration import (
    build_registration_rules,
    loss_warning,
    registration_losses,
    replace_registration_filters,
    snapshot_registration,
)
from kd2_rules_mcp.authoring.workspace import RulesProject
from kd2_rules_mcp.errors import Kd2Error
from kd2_rules_mcp.kd2.model import RegistrationRules
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
            existing = self._registration_project(project_id)
            build = build_registration_rules(
                conn, exchange_plan, exchange_rules=exchange, objects=specs
            )
            if existing is not None:
                if not specs:
                    raise ValueError(
                        "Чтобы заменить отборы в существующем проекте правил регистрации, "
                        "передайте objects"
                    )
                document = existing.document
                if not isinstance(document, RegistrationRules):
                    raise Kd2Error(f"Проект «{project_id}» не загружен")
                before = snapshot_registration(document)
                replace_registration_filters(document, build.document, specs)
                self.workspace.mark_modified(existing.id)
                losses = registration_losses(before, document)
                warnings = [*build.warnings, *(loss_warning(item) for item in losses)]
                view = {**self._project_view(existing), "warnings": warnings}
                if losses:
                    view["losses"] = losses
                return view
            project = self.workspace.add(build.document, project_id, label=f"reg-{exchange_plan}")
            return {**self._project_view(project), "warnings": build.warnings}

    def _registration_project(self, project_id: str | None) -> RulesProject | None:
        """Открытый проект правил регистрации с этим идентификатором, иначе None.

        Занятый идентификатор правил обмена — ошибка: новый проект туда не пишется.
        """
        if not project_id or project_id not in self.workspace.ids():
            return None
        project = self.workspace.get(project_id)
        document = project.document
        if isinstance(document, RegistrationRules):
            return project
        raise Kd2Error(
            f"Идентификатор «{project_id}» занят проектом правил обмена, а не правилами регистрации"
        )

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
