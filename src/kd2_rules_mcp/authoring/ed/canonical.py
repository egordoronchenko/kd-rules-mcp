"""Имена решений разрешаются один раз; строковые сравнения исполнителя точные."""

import unicodedata
from dataclasses import replace
from types import MappingProxyType

from kd2_rules_mcp.ed.address import AmbiguousAddressError, EntityNotFoundError
from kd2_rules_mcp.ed.model import ObjectRule
from kd2_rules_mcp.ed.schema.model import SchemaType
from kd2_rules_mcp.ed.schema.profile import ValidationProfile
from kd2_rules_mcp.validation.ed_structure_snapshot import metadata_key

from .context import AuthoringContext
from .model import (
    AddHeaderProperty,
    AuthoringInputs,
    AuthoringPreconditionError,
    CanonicalHeaderProperty,
    Failure,
    order_operations,
)


def canonical_property(
    profile: ValidationProfile, typ: SchemaType, value: str, context: AuthoringContext | None = None
) -> str | None:
    """Сохраняет выбор короткого/полного пути; использует существующий resolver."""
    value = value.strip(" ")
    if not value or any(c.isspace() or unicodedata.category(c).startswith("C") for c in value):
        return None
    folded = context.folded_profiles.get(id(profile)) if context else None
    if folded is None:
        folded = replace(
            profile,
            effective=MappingProxyType(
                {
                    key: tuple(
                        (
                            replace(
                                prop, name=replace(prop.name, local=prop.name.local.casefold())
                            ),
                            path,
                        )
                        for prop, path in rows
                    )
                    for key, rows in profile.effective.items()
                }
            ),
        )
        if context:
            context.folded_profiles[id(profile)] = folded
    result = folded.resolve(typ, value.casefold())
    if result.status != "resolved" or len(result.property_ids) != 1:
        return None
    if "." not in value:
        return profile.properties[result.property_ids[0]].name.local
    parts = iter(result.physical_paths[0])
    names = []
    for requested in value.split("."):
        found = next(
            (part.local for part in parts if part.local.casefold() == requested.casefold()), None
        )
        if found is None:
            return None
        names.append(found)
    return ".".join(names)


def canonicalize_operations(
    inputs: AuthoringInputs,
    operations: tuple[AddHeaderProperty, ...],
    context: AuthoringContext | None = None,
) -> tuple[AddHeaderProperty, ...]:
    """Не исправляет неизвестные имена; предусловия объясняют неразрешённый вход."""
    context = context or AuthoringContext(inputs)
    index = context.index(inputs.document)
    result = []
    failures = []
    for op in operations:
        try:
            rule = index.find(op.target.pko_address)
        except (EntityNotFoundError, AmbiguousAddressError):
            result.append(op)
            continue
        if not isinstance(rule, ObjectRule):
            result.append(op)
            continue
        target = replace(
            op.target,
            pko_address=index.by_id[rule.entity_id][0],
            project=inputs.source_set.project or op.target.project,
            configuration=inputs.source_set.configuration or op.target.configuration,
            plan=next(
                (
                    p.plan_name
                    for p in inputs.routes.plans
                    if p.plan_name.casefold() == op.target.plan.casefold()
                ),
                op.target.plan,
            ),
        )
        # Нельзя канонизацией скрыть несоответствие контура из предусловий.
        if (
            op.target.project.casefold() != target.project.casefold()
            or op.target.configuration.casefold() != target.configuration.casefold()
        ):
            target = replace(
                target, project=op.target.project, configuration=op.target.configuration
            )
        key, _ = metadata_key(rule.configuration_object.value)
        owner = inputs.structure.objects.get(key) if key else None
        rows = owner.property(op.configuration_attribute) if owner else ()
        attribute = (
            op.new_attribute.name
            if op.new_attribute
            and op.new_attribute.name.casefold() == op.configuration_attribute.casefold()
            else rows[0].path
            if len(rows) == 1
            else op.configuration_attribute
        )
        profile = context.profile(target.format_version, target.direction)
        applicable = context.applicable(inputs.document, target.format_version, target.direction)
        typ, _ = profile.owner_type(rule, target.direction, applicable)
        prop = canonical_property(profile, typ, op.format_property, context) if typ else None
        if prop is None and typ:
            source = next(f for f in inputs.document.files if f.file_id == rule.span.file_id)
            failures.append(
                Failure(
                    "ed.author.schema_property_missing",
                    target.pko_address,
                    f"Свойство формата «{op.format_property}» не разрешено однозначно",
                    source.path,
                    rule.span.line_start,
                )
            )
        operation_type = CanonicalHeaderProperty if prop is not None else AddHeaderProperty
        result.append(
            operation_type(target, attribute, prop or op.format_property, op.new_attribute)
        )
    if failures:
        raise AuthoringPreconditionError(tuple(failures))
    return order_operations(tuple(result))
