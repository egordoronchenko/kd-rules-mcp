"""Имена решений разрешаются один раз; строковые сравнения исполнителя точные."""

import unicodedata
from dataclasses import replace
from types import MappingProxyType
from typing import cast

from kd2_rules_mcp.ed.address import AmbiguousAddressError, EntityNotFoundError
from kd2_rules_mcp.ed.model import ObjectRule
from kd2_rules_mcp.ed.schema.model import SchemaType
from kd2_rules_mcp.ed.schema.profile import ValidationProfile
from kd2_rules_mcp.validation.ed_structure_snapshot import metadata_key

from .context import AuthoringContext
from .model import (
    AddAlgorithmicHeaderProperty,
    AddHeaderProperty,
    AuthoringInputs,
    AuthoringPreconditionError,
    Failure,
    Operation,
    SetObjectHandler,
    _canonical_operation,
    order_operations,
)


def canonical_property(
    profile: ValidationProfile, typ: SchemaType, value: str, context: AuthoringContext | None = None
) -> str | None:
    """Сохраняет выбор короткого/полного пути; использует существующий resolver."""
    value = value.strip(" ")
    if not value or any(c.isspace() or unicodedata.category(c).startswith("C") for c in value):
        return None
    exact = profile.resolve(typ, value)
    if exact.status == "resolved" and len(exact.property_ids) == 1:
        return value
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


def canonicalize_operations[T: Operation](
    inputs: AuthoringInputs,
    operations: tuple[T, ...],
    context: AuthoringContext | None = None,
) -> tuple[T, ...]:
    """Не исправляет неизвестные имена; предусловия объясняют неразрешённый вход."""
    context = context or AuthoringContext(inputs)
    index = context.index(inputs.document)
    result: list[T] = []
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
        if isinstance(op, SetObjectHandler):
            previous = next(
                (
                    r.name
                    for r in inputs.document.routines
                    if r.name.casefold() == op.expected_previous.strip(" ").casefold()
                ),
                op.expected_previous,
            )
            result.append(cast(T, replace(op, target=target, expected_previous=previous)))
            continue
        if not isinstance(op, (AddHeaderProperty, AddAlgorithmicHeaderProperty)):
            result.append(cast(T, replace(op, target=target)))
            continue
        key, _ = metadata_key(rule.configuration_object.value)
        owner = inputs.structure.objects.get(key) if key else None
        requested_attribute = op.configuration_attribute.strip(" ")
        from .hook import valid_identifier

        if not valid_identifier(requested_attribute):
            failures.append(
                Failure(
                    "ed.author.identifier_conflict",
                    target.pko_address,
                    f"Недопустимый идентификатор «{op.configuration_attribute}» "
                    "или префикс нового реквизита",
                )
            )
        rows = owner.property(requested_attribute) if owner else ()
        draft = op.new_attribute if isinstance(op, AddHeaderProperty) else None
        attribute = (
            draft.name
            if draft and draft.name.casefold() == requested_attribute.casefold()
            else rows[0].path
            if len(rows) == 1
            else requested_attribute
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
        operation_type = _canonical_operation if prop is not None else AddHeaderProperty
        if isinstance(op, AddAlgorithmicHeaderProperty):
            conversion = next(
                (
                    r.declared_name or r.name
                    for r in (*inputs.document.pko, *inputs.document.pkpd)
                    if (r.declared_name or r.name).casefold()
                    == op.conversion_rule.strip(" ").casefold()
                ),
                op.conversion_rule,
            )
            result.append(
                cast(
                    T,
                    replace(
                        op,
                        target=target,
                        configuration_attribute=attribute,
                        format_property=prop or op.format_property,
                        conversion_rule=conversion,
                    ),
                )
            )
        else:
            result.append(
                cast(T, operation_type(target, attribute, prop or op.format_property, draft))
            )
    if failures:
        raise AuthoringPreconditionError(tuple(failures))
    renamed = {
        old.operation_id: new.operation_id
        for old, new in zip(operations, result, strict=True)
        if old.operation_id != new.operation_id
    }
    return order_operations(tuple(result), canonical_ids=renamed)
