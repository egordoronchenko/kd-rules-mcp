"""Свободные поля и узкая совместимость примитивов; выбор всегда делает агент."""

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, localcontext
from types import MappingProxyType
from typing import Literal

from kd2_rules_mcp.ed.model import ObjectRule
from kd2_rules_mcp.ed.schema.model import SchemaProperty, SchemaType
from kd2_rules_mcp.ed.schema.profile import Applicability, ValidationProfile
from kd2_rules_mcp.ed.schema.resolver import property_type
from kd2_rules_mcp.ed.schema.xdto import XS
from kd2_rules_mcp.validation.ed_structure_snapshot import (
    StructureObject,
    StructureProperty,
    metadata_key,
)

from .context import AuthoringContext
from .model import AuthoringInputs, AuthoringTarget

FAMILIES = {
    "string": "Строка",
    "boolean": "Булево",
    "decimal": "Число",
    "integer": "Число",
    "date": "Дата",
    "dateTime": "Дата",
    "time": "Дата",
}
INTEGER_BOUNDS = {
    "byte": (-128, 127),
    "short": (-32768, 32767),
    "int": (-2147483648, 2147483647),
    "long": (-9223372036854775808, 9223372036854775807),
    "unsignedByte": (0, 255),
    "unsignedShort": (0, 65535),
    "unsignedInt": (0, 4294967295),
    "unsignedLong": (0, 18446744073709551615),
    "nonNegativeInteger": (0, None),
    "positiveInteger": (1, None),
    "nonPositiveInteger": (None, 0),
    "negativeInteger": (None, -1),
}


@dataclass(frozen=True, slots=True)
class Compatibility:
    compatible: bool
    reason: str
    refusal: str = ""
    value_range: str | None = None


def primitive_limits(
    profile: ValidationProfile,
    prop: SchemaProperty,
) -> tuple[str | None, str | None, Mapping[str, Decimal]]:
    """Все предки/facets должны быть известны; enum/pattern/list/union запрещены (§2.4)."""
    schema = profile.schema
    assert schema is not None
    if prop.status != "complete" or prop.upper != 1:
        return None, None, {}
    target = property_type(schema, prop)
    limits: dict[str, Decimal] = {}
    seen = set()
    ref = prop.type_ref
    allowed = {
        "maxLength",
        "minLength",
        "length",
        "totalDigits",
        "fractionDigits",
        "minInclusive",
        "maxInclusive",
        "minExclusive",
        "maxExclusive",
    }
    while target is not None:
        if (
            target.id in seen
            or target.status != "complete"
            or target.kind != "value"
            or target.members
            or target.variety.casefold() in ("list", "union")
        ):
            return None, None, {}
        seen.add(target.id)
        for facet in target.facets:
            if facet.kind not in allowed:
                return None, None, {}
            try:
                value = Decimal(facet.lexical)
                if not value.is_finite():
                    return None, None, {}
            except InvalidOperation:
                return None, None, {}
            existing = limits.get(facet.kind)
            limits[facet.kind] = (
                value
                if existing is None
                else (
                    max(existing, value) if facet.kind.startswith("min") else min(existing, value)
                )
            )
        ref = target.base
        if ref is None:
            return None, None, {}
        if ref.namespace == XS:
            break
        target = schema.types.get(ref)
        if target is None:
            return None, None, {}
    if ref is None or ref.namespace != XS:
        return None, None, {}
    local = ref.local
    family = FAMILIES.get(local)
    if local in INTEGER_BOUNDS:
        family = "Число"
        low, high = INTEGER_BOUNDS[local]
        if low is not None:
            limits["minInclusive"] = max(limits.get("minInclusive", Decimal(low)), Decimal(low))
        if high is not None:
            limits["maxInclusive"] = min(limits.get("maxInclusive", Decimal(high)), Decimal(high))
    if family == "Число" and local != "decimal":
        limits["fractionDigits"] = Decimal(0)
    applicable_facets = {
        "Строка": {"length", "minLength", "maxLength"},
        "Число": {
            "totalDigits",
            "fractionDigits",
            "minInclusive",
            "maxInclusive",
            "minExclusive",
            "maxExclusive",
        },
        "Булево": set(),
        "Дата": set(),
    }.get(family or "", set())
    if set(limits) - applicable_facets:
        return None, None, {}
    for key in ("length", "minLength", "maxLength", "totalDigits", "fractionDigits"):
        value = limits.get(key)
        if value is not None and (value < 0 or value != value.to_integral_value()):
            return None, None, {}
    if limits.get("totalDigits") == 0 or (
        "totalDigits" in limits and limits.get("fractionDigits", Decimal(0)) > limits["totalDigits"]
    ):
        return None, None, {}
    if "length" in limits and not (
        limits.get("minLength", limits["length"])
        <= limits["length"]
        <= limits.get("maxLength", limits["length"])
    ):
        return None, None, {}
    if limits.get("minLength", Decimal(0)) > limits.get("maxLength", Decimal("Infinity")):
        return None, None, {}
    lower = limits.get("minInclusive", limits.get("minExclusive"))
    upper = limits.get("maxInclusive", limits.get("maxExclusive"))
    if (
        lower is not None
        and upper is not None
        and (lower > upper or (lower == upper and any(k.endswith("Exclusive") for k in limits)))
    ):
        return None, None, {}
    return family, local, MappingProxyType(limits)


def compatibility(
    profile: ValidationProfile,
    prop: SchemaProperty,
    attribute: StructureProperty,
    direction: str,
) -> Compatibility:
    family, local, limits = primitive_limits(profile, prop)
    if family is None or attribute.unresolved or len(attribute.types) != 1:
        return Compatibility(
            False,
            "Требуется конвертация: не одиночный подтверждённый примитив",
            "conversion_required",
        )
    if attribute.types[0] not in ("Строка", "Булево", "Число", "Дата"):
        return Compatibility(
            False, "Тип реквизита и примитив свойства различаются", "type_incompatible"
        )
    if attribute.types[0] != family:
        return Compatibility(
            False, "Примитивные семьи реквизита и свойства различаются", "type_incompatible"
        )
    q = attribute.qualifiers
    risks = []
    if family == "Строка":
        config = q.get("string_length", 0)
        if not isinstance(config, int) or isinstance(config, bool) or config < 0:
            return Compatibility(False, "Неизвестны квалификаторы строки", "conversion_required")
        length = limits.get("length", limits.get("maxLength"))
        source, destination = (
            (config or None, length) if direction == "send" else (length, config or None)
        )
        if destination is not None and (source is None or Decimal(source) > Decimal(destination)):
            risks.append(
                f"длина источника={source if source is not None else 'unbounded'}, "
                f"приёмника={destination}"
            )
        minimum = limits.get("length", limits.get("minLength", Decimal(0)))
        if direction == "send" and minimum > 0:
            risks.append(
                f"минимальная длина формата={minimum}; пустое значение реквизита допустимо"
            )
        if q.get("string_fixed"):
            risks.append("реквизит имеет фиксированную длину")
    elif family == "Дата":
        parts = str(q.get("date_parts", "ДатаВремя")).casefold()
        expected = {
            "date": {"дата", "date"},
            "time": {"время", "time"},
            "dateTime": {"датавремя", "datetime"},
        }[local or "dateTime"]
        if parts not in expected:
            return Compatibility(
                False, "Компоненты даты реквизита и свойства различаются", "type_incompatible"
            )
        if limits:
            return Compatibility(
                False, "Ограничения даты не поддержаны прямой ПКС", "conversion_required"
            )
    elif family == "Число":
        digits, fraction = q.get("number_length"), q.get("number_precision")
        if (
            not isinstance(digits, int)
            or not isinstance(fraction, int)
            or not 0 <= fraction < digits <= 38
        ):
            return Compatibility(False, "Неизвестны квалификаторы числа", "conversion_required")
        with localcontext() as context:
            context.prec = 80
            maximum = Decimal(10) ** (digits - fraction) - Decimal(10) ** (-fraction)
        config_low = Decimal(0) if q.get("number_nonnegative") else -maximum
        format_low = limits.get("minInclusive", limits.get("minExclusive"))
        format_high = limits.get("maxInclusive", limits.get("maxExclusive"))
        total = limits.get("totalDigits")
        decimals = limits.get("fractionDigits")
        if total is not None:
            bound = Decimal(10) ** total - 1
            format_low = max(format_low, -bound) if format_low is not None else -bound
            format_high = min(format_high, bound) if format_high is not None else bound
        cfg = (config_low, maximum, Decimal(fraction))
        fmt = (format_low, format_high, decimals)
        source, destination = (cfg, fmt) if direction == "send" else (fmt, cfg)
        for label, src, dst, lower in zip(
            ("min", "max", "fraction"), source, destination, (True, False, False), strict=True
        ):
            if dst is not None and (src is None or (src < dst if lower else src > dst)):
                risks.append(
                    f"{label}: источник={src if src is not None else 'unbounded'}, приёмник={dst}"
                )
        if direction == "send" and any(k.endswith("Exclusive") for k in limits):
            risks.append("граница формата исключительная")
    return Compatibility(
        True, "Одиночный совместимый примитив", value_range="; ".join(risks) or None
    )


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
                attributes.add(prop.configuration_property.casefold())
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
