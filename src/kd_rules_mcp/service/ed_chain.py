"""Представление открытого проекта ED в одном направлении для `ed_trace` и `ed_pair_validate`."""

from __future__ import annotations

from typing import TYPE_CHECKING

from kd_rules_mcp.ed import build_references
from kd_rules_mcp.ed.schema.profile import ValidationProfile
from kd_rules_mcp.validation.ed_chain import ChainView

if TYPE_CHECKING:
    from kd_rules_mcp.service.ed import EdProject
    from kd_rules_mcp.service.ed_schema import SchemaProject


def resolve_version(version: str | None, *schemas: tuple[str, SchemaProject | None]) -> str | None:
    """Явная версия или версия открытых схем; расхождение — отказ, а не выбор."""
    known = {
        name: project.format_version
        for name, project in schemas
        if project is not None and project.format_version
    }
    if version is not None:
        version = version.strip()
        if not version:
            raise ValueError("version: пустая строка")
        for name, value in known.items():
            if value != version:
                raise ValueError(f"version {version} не совпадает с версией схемы {name}: {value}")
        return version
    values = set(known.values())
    if len(values) > 1:
        raise ValueError(
            "Версии схем различаются: "
            + ", ".join(f"{name}={value}" for name, value in sorted(known.items()))
        )
    return next(iter(values), None)


def chain_view(
    project_id: str,
    project: EdProject,
    direction: str,
    schema: SchemaProject | None,
    version: str | None,
    *,
    with_references: bool = False,
) -> ChainView:
    """Снимок без слоёв — сам документ; со слоями — действующий контекст направления."""
    profile = ValidationProfile.build(schema.schema if schema else None, version, direction)
    if project.layered is None:
        return ChainView.build(
            project_id,
            project.document,
            project.index,
            direction,
            profile,
            references=(project.references or build_references(project.document))
            if with_references
            else project.references,
        )
    for view in project.layered.contexts:
        if view.context.direction == direction and not view.context.headers_only:
            return ChainView.build(
                project_id,
                view.document,
                view.index,
                direction,
                profile,
                versions=view.versions,
                references=build_references(view.document) if with_references else None,
                layered=True,
                order_uses=(
                    project.layered.manager.source_document or project.layered.manager.base
                ).rule_uses,
            )
    raise ValueError(f"У снимка со слоями нет контекста направления {direction}")
