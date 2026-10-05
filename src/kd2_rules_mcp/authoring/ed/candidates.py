"""Свободные поля и узкая совместимость примитивов; выбор всегда делает агент."""

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Literal

from kd2_rules_mcp.ed.model import ObjectRule
from kd2_rules_mcp.ed.schema.model import SchemaType
from kd2_rules_mcp.ed.schema.profile import Applicability, ValidationProfile
from kd2_rules_mcp.validation.ed_compatibility import compatibility as compatibility
from kd2_rules_mcp.validation.ed_compatibility import primitive_limits as primitive_limits
from kd2_rules_mcp.validation.ed_structure_snapshot import (
    StructureObject,
    metadata_key,
)

from .context import AuthoringContext
from .model import AuthoringInputs, AuthoringTarget


def target_objects(
    inputs: AuthoringInputs, target: AuthoringTarget, context: AuthoringContext | None = None
) -> tuple[ObjectRule, ValidationProfile, Applicability, SchemaType | None, StructureObject | None]:
    context = context or AuthoringContext(inputs)
    rule = context.index(inputs.document).find(target.pko_address)
    if not isinstance(rule, ObjectRule):
        raise ValueError("Адрес должен указывать на ПКО")
    profile = context.profile(target.format_version, target.direction)
    applicable = context.applicable(inputs.document, target.format_version, target.direction)
    typ, _ = profile.owner_type(rule, target.direction, applicable)
    key, _ = metadata_key(rule.configuration_object.value)
    return rule, profile, applicable, typ, inputs.structure.objects.get(key) if key else None


def occupied_properties(
    rule: ObjectRule,
    profile: ValidationProfile,
    applicable: Applicability,
    typ: SchemaType,
    direction: str,
) -> tuple[set[str], set[str], bool]:
    ids, attributes = set(), set()
    unknown = False
    for group, properties in [(None, rule.properties), *((g, g.properties) for g in rule.groups)]:
        for prop in properties:
            state = applicable.evaluate(prop, direction, group or rule)
            if state is False:
                continue
            unknown |= state is None
            resolved = profile.resolve(
                typ, prop.format_property, group.format_property if group else None
            )
            ids.update(resolved.property_ids)
            # Реквизит строки ТЧ не занимает одноимённый реквизит шапки.
            if group is None:
                attributes.add(prop.configuration_property.strip(" ").casefold())
    return ids, attributes, unknown


@dataclass(frozen=True, slots=True)
class Candidate:
    name: str
    path: str
    types: tuple[str, ...]
    qualifiers: Mapping[str, str | int]
    compatible: bool | None
    reason: str
    source: str
    auto: bool = False


def candidates(
    inputs: AuthoringInputs,
    target: AuthoringTarget,
    kind: Literal["format", "configuration"],
    *,
    configuration_attribute: str | None = None,
    format_property: str | None = None,
    text: str = "",
    include_unsupported: bool = False,
) -> tuple[Candidate, ...]:
    """Полный упорядоченный список; пагинация и выбор находятся вне чистого ядра."""
    rule, profile, applicable, typ, owner = target_objects(inputs, target)
    if typ is None or owner is None or applicable.evaluate(rule, target.direction) is not True:
        return ()
    ids, attributes, unknown = occupied_properties(rule, profile, applicable, typ, target.direction)
    if unknown:
        return ()
    result = []
    if kind == "format":
        actual = owner.property(configuration_attribute) if configuration_attribute else ()
        for prop, physical in profile.effective[typ.id]:
            if prop.id in ids:
                continue
            path = ".".join(q.local for q in physical)
            resolved = profile.resolve(typ, path)
            family, _, limits = primitive_limits(profile, prop)
            supported = family is not None and resolved.status == "resolved"
            check = (
                compatibility(profile, prop, actual[0], target.direction)
                if len(actual) == 1
                else None
            )
            if supported or include_unsupported:
                result.append(
                    Candidate(
                        prop.name.local,
                        path,
                        (str(prop.type_ref),),
                        MappingProxyType({k: str(v) for k, v in limits.items()}),
                        check.compatible
                        if check
                        else False
                        if configuration_attribute or not supported
                        else None,
                        check.reason
                        if check
                        else "Реквизит конфигурации отсутствует или неоднозначен"
                        if configuration_attribute
                        else "Совместимость пары не проверена"
                        if supported
                        else "Требуется конвертация или путь неоднозначен",
                        prop.origin[-1].span.source_id if prop.origin else "",
                    )
                )
    elif kind == "configuration":
        resolved = profile.resolve(typ, format_property) if format_property else None
        prop = (
            profile.properties[resolved.property_ids[0]]
            if resolved and resolved.status == "resolved"
            else None
        )
        for rows in owner.properties.values():
            for attr in rows:
                if (
                    attr.kind != "Реквизит"
                    or attr.parent_kind
                    or attr.path.casefold() in attributes
                ):
                    continue
                supported = (
                    len(rows) == 1
                    and not attr.unresolved
                    and len(attr.types) == 1
                    and attr.types[0] in ("Строка", "Булево", "Число", "Дата")
                )
                check = compatibility(profile, prop, attr, target.direction) if prop else None
                if supported or include_unsupported:
                    result.append(
                        Candidate(
                            attr.path,
                            attr.path,
                            attr.types,
                            attr.qualifiers,
                            check.compatible
                            if check
                            else False
                            if format_property or not supported
                            else None,
                            check.reason
                            if check
                            else "Свойство формата отсутствует или неоднозначно"
                            if format_property
                            else "Совместимость пары не проверена"
                            if supported
                            else "Требуется конвертация",
                            "структура",
                        )
                    )
    else:
        raise ValueError("Вид кандидата: format или configuration")
    return tuple(
        sorted(
            (c for c in result if text.casefold() in (c.name + " " + c.path).casefold()),
            key=lambda c: (c.name, c.path),
        )
    )
