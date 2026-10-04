"""Профильные проверки ED: несовпадения — предупреждения, BSL не исполняется.

Основания: XDTO:4147–4155 (тип), 933–993 (свойства), 5681–5703 (ТЧ),
6753–6768 (получение), 6863–6880 (поиск), 5563–5578 (ПКПД).
"""

from collections import Counter

from kd2_rules_mcp.ed import model as ed
from kd2_rules_mcp.ed.address import AddressIndex
from kd2_rules_mcp.ed.layer_model import EffectiveContext, LayeredManager
from kd2_rules_mcp.ed.schema.model import EdSchema, SchemaProperty, SchemaType
from kd2_rules_mcp.ed.schema.profile import Applicability, ValidationProfile
from kd2_rules_mcp.ed.schema.resolver import is_reference, property_type, table_row
from kd2_rules_mcp.ed.schema.xdto import XS
from kd2_rules_mcp.validation.ed_structure_snapshot import (
    CheckContext,
    StructureSnapshot,
    metadata_key,
)
from kd2_rules_mcp.validation.report import ValidationReport

SEND_EVENTS = frozenset({"ПриОтправкеДанных"})
RECEIVE_EVENTS = frozenset(
    {"ПриКонвертацииДанныхXDTO", "ПередЗаписьюПолученныхДанных", "АлгоритмПоиска"}
)
ATOMIC = {
    "decimal": "Число",
    "integer": "Число",
    "int": "Число",
    "long": "Число",
    "short": "Число",
    "byte": "Число",
    "double": "Число",
    "float": "Число",
    "nonNegativeInteger": "Число",
    "positiveInteger": "Число",
    "nonPositiveInteger": "Число",
    "negativeInteger": "Число",
    "unsignedLong": "Число",
    "unsignedInt": "Число",
    "unsignedShort": "Число",
    "unsignedByte": "Число",
    "date": "Дата",
    "dateTime": "Дата",
    "time": "Дата",
    "boolean": "Булево",
}


def has_handler(
    rule: ed.ObjectRule, direction: str, applicability: Applicability | None = None
) -> bool:
    events = SEND_EVENTS if direction == "send" else RECEIVE_EVENTS
    return any(
        e.event in events
        and e.target_name
        and (applicability is None or applicability.evaluate(e, direction, rule) is not False)
        for e in rule.events
    )


def enum_values(schema: EdSchema, typ: SchemaType) -> tuple[frozenset[str], bool]:
    """Наследованные enumeration; неполное наследование не даёт отрицательного вывода."""
    seen: set[str] = set()
    while typ.id not in seen:
        seen.add(typ.id)
        if typ.status != "complete":
            return frozenset(), False
        values = frozenset(f.lexical for f in typ.facets if f.kind == "enumeration")
        if values:
            return values, True
        if typ.base is None or typ.base.namespace == XS:
            return frozenset(), True
        parent = schema.types.get(typ.base)
        if parent is None:
            return frozenset(), False
        typ = parent
    return frozenset(), False


def atomic_family(schema: EdSchema, prop: SchemaProperty) -> str | None:
    """Только доказанное атомарное семейство; enum/union/ref/any исключены."""
    if prop.status != "complete":
        return None
    target = property_type(schema, prop)
    if target is None:
        return (
            ATOMIC.get(prop.type_ref.local)
            if prop.type_ref and prop.type_ref.namespace == XS
            else None
        )
    seen: set[str] = set()
    while target.id not in seen:
        seen.add(target.id)
        if (
            target.status != "complete"
            or target.kind != "value"
            or target.members
            or target.variety.lower() in ("union", "list")
            or any(f.kind == "enumeration" for f in target.facets)
            or is_reference(schema, target)
        ):
            return None
        if target.base is None:
            return None
        if target.base.namespace == XS:
            return ATOMIC.get(target.base.local)
        parent = schema.types.get(target.base)
        if parent is None:
            return None
        target = parent
    return None


def required_properties(
    profile: ValidationProfile,
    typ: SchemaType,
    prefix: tuple[str, ...] = (),
    seen: frozenset[str] = frozenset(),
) -> tuple[tuple[SchemaProperty, str], ...]:
    """Обязательные листья только обязательных оболочек (XDTO:981–993).

    Optional контейнер не требует детей. Обычные вложенные объекты не раскрываются.
    """
    schema = profile.schema
    assert schema is not None
    if typ.id in seen or typ.status != "complete":
        return ()
    result: list[tuple[SchemaProperty, str]] = []
    for prop in schema.inherited[typ.id]:
        if prop.lower <= 0 or prop.nillable or prop.status != "complete":
            continue
        name = prop.name.local
        if name in ("Ссылка", "AdditionalInfo"):
            continue
        target = property_type(schema, prop)
        if name == "КлючевыеСвойства" or (
            target and target.qname and "ОбщиеСвойства" in target.qname.local
        ):
            if target:
                result.extend(
                    required_properties(profile, target, (*prefix, name), seen | {typ.id})
                )
        else:
            result.append((prop, ".".join((*prefix, name))))
    return tuple(result)


def validate_schema(
    document: ed.EdDocument | LayeredManager,
    schema: EdSchema,
    index: AddressIndex,
    profile: ValidationProfile,
    snapshot: StructureSnapshot | None = None,
    coverage: Counter[str] | None = None,
    *,
    context: EffectiveContext | None = None,
) -> ValidationReport:
    """Проверяет выбранную схему и прямые типы при наличии структуры."""
    if isinstance(document, LayeredManager):
        from .ed_layers import validate_effective_schema

        if context is None:
            raise ValueError("Для действующего представления нужен контекст направления")
        return validate_effective_schema(document, context, schema, profile, snapshot, coverage)
    checking = CheckContext(document, index, profile)
    by_name: dict[str, list[ed.ObjectRule]] = {}
    for rule in document.pko:
        by_name.setdefault(rule.name.casefold(), []).append(rule)

    def owner_type(rule: ed.ObjectRule, direction: str) -> tuple[SchemaType | None, str]:
        return profile.owner_type(rule, direction, checking.applicability)

    # Признак «выгрузка всего объекта доказана ПОД» не зависит от правила и направления:
    # иначе каждый ПКО заново обходит все ПОД.
    send_targets = {
        ref.target_id
        for pod in document.pod
        if checking.applicability.evaluate(pod, "send") is True
        for ref in pod.used_pko
    }

    for direction in profile.directions:
        for rule in document.pko:
            typ, type_status = owner_type(rule, direction)
            check = "ed.schema.type_missing"
            if checking.start(check, rule, direction):
                if type_status == "missing":
                    checking.checked()
                    checking.report.warning(
                        check,
                        checking.address(rule),
                        f"Тип формата «{rule.format_object.value}» отсутствует в выбранной схеме; "
                        "ПКО исключается исполнителем.",
                    )
                elif type_status == "resolved":
                    checking.checked()
                elif type_status == "not_applicable":
                    checking.count("not_applicable")
                else:
                    checking.skip(check, type_status, rule)

            full_send = rule.entity_id in send_targets
            for group, properties in [
                (None, rule.properties),
                *((g, g.properties) for g in rule.groups),
            ]:
                group_type = typ
                group_type_status = type_status
                if (
                    type_status == "resolved"
                    and group
                    and group.namespace
                    and rule.format_object.value
                ):
                    group_type, group_type_status = profile.find_type(
                        rule.format_object.value, group.namespace
                    )
                row_type = typ
                group_status = type_status
                if group:
                    check = "ed.schema.table_missing"
                    if checking.start(check, group, direction, rule):
                        if group_type is None or group_type_status != "resolved":
                            checking.skip(check, "owner_type_unavailable", group)
                            group_status = "owner_type_unavailable"
                        elif not group.format_property:
                            checking.skip(check, "empty_format_side", group)
                            group_status = "empty_format_side"
                        else:
                            resolved = profile.resolve(group_type, group.format_property)
                            group_status = resolved.status
                            if group_status == "resolved":
                                row_type = table_row(
                                    schema, profile.properties[resolved.property_ids[0]]
                                )
                                group_status = "resolved" if row_type else "not_table"
                            if group_status in ("missing", "not_table"):
                                checking.checked()
                                checking.report.warning(
                                    check,
                                    checking.address(group),
                                    f"Группа «{group.format_property}» "
                                    "не разрешается как табличная часть формата.",
                                )
                            elif group_status == "resolved":
                                checking.checked()
                            else:
                                checking.skip(check, resolved.reason or group_status, group)
                supplied: set[str] = set()
                possible_supplied: set[str] = set()
                for prop in properties:
                    property_owner = group_type
                    property_owner_status = group_type_status
                    if type_status == "resolved" and prop.namespace and rule.format_object.value:
                        property_owner, property_owner_status = profile.find_type(
                            rule.format_object.value, prop.namespace
                        )
                    check = "ed.schema.property_missing"
                    resolved_prop = None
                    state = checking.applicability.evaluate(prop, direction, group or rule)
                    if state is None and property_owner and prop.format_property:
                        possible = profile.resolve(
                            property_owner,
                            prop.format_property,
                            group.format_property if group and group.format_property else None,
                        )
                        if possible.status == "resolved":
                            possible_supplied.update(possible.property_ids)
                    if checking.start(check, prop, direction, group or rule):
                        if (
                            property_owner is None
                            or property_owner_status != "resolved"
                            or group_status != "resolved"
                        ):
                            checking.skip(check, "owner_type_unavailable", prop)
                        elif not prop.format_property:
                            checking.skip(check, "empty_format_side", prop)
                        else:
                            resolved = profile.resolve(
                                property_owner,
                                prop.format_property,
                                group.format_property if group else None,
                            )
                            if resolved.status == "missing":
                                checking.checked()
                                checking.report.warning(
                                    check,
                                    checking.address(prop),
                                    f"Свойство формата «{prop.format_property}» "
                                    "отсутствует в выбранном профиле; "
                                    "проверьте версию и обработчик.",
                                )
                            elif resolved.status == "resolved":
                                checking.checked()
                                resolved_prop = profile.properties[resolved.property_ids[0]]
                                supplied.update(resolved.property_ids)
                            else:
                                checking.skip(check, resolved.reason or resolved.status, prop)
                    check = "ed.schema.pko_unavailable"
                    if prop.conversion_rule and checking.start(
                        check, prop, direction, group or rule
                    ):
                        targets = by_name.get(prop.conversion_rule.casefold(), [])
                        if type_status != "resolved":
                            checking.skip(check, "owner_type_unavailable", prop)
                        elif len(targets) > 1:
                            checking.skip(check, "ambiguous", prop)
                        elif targets:
                            target = targets[0]
                            _, status = owner_type(target, direction)
                            if status == "missing":
                                checking.checked()
                                checking.report.warning(
                                    check,
                                    checking.address(prop),
                                    f"ПКО «{prop.conversion_rule}» "
                                    "недоступен в выбранной схеме формата.",
                                )
                            elif status in ("resolved", "empty_format_side"):
                                checking.checked()
                            else:
                                checking.skip(check, status, prop)
                    check = "ed.schema.type_incompatible"
                    if (
                        snapshot is not None
                        and not prop.algorithm_flag
                        and not prop.conversion_rule
                        and prop.format_property
                        and checking.start(check, prop, direction, group or rule)
                    ):
                        if resolved_prop is None:
                            checking.skip(check, "owner_type_unavailable", prop)
                            continue
                        key, _ = metadata_key(rule.configuration_object.value)
                        obj = snapshot.objects.get(key) if key else None
                        configuration_state = checking.applicability.field(
                            rule.configuration_object, direction
                        )
                        if configuration_state is None:
                            checking.skip(check, "opaque_condition", prop)
                            continue
                        if configuration_state is False:
                            checking.count("not_applicable")
                            continue
                        path = (
                            f"{group.configuration_property}.{prop.configuration_property}"
                            if group
                            else prop.configuration_property
                        )
                        actual = (
                            obj.property(path, group is not None)
                            if obj
                            and prop.configuration_property
                            and (not group or group.configuration_property)
                            else ()
                        )
                        if not actual or len(actual) != 1 or actual[0].unresolved:
                            checking.skip(check, "unresolved_configuration_type", prop)
                        elif has_handler(rule, direction, checking.applicability):
                            checking.skip(check, "handler_may_supply", prop)
                        else:
                            expected = atomic_family(schema, resolved_prop)
                            types = set(actual[0].types)
                            if (
                                not expected
                                or not types
                                or not types <= {"Число", "Дата", "Булево"}
                            ):
                                checking.skip(check, "non_atomic_type", prop)
                            else:
                                checking.checked()
                                if expected not in types:
                                    checking.report.warning(
                                        check,
                                        checking.address(prop),
                                        f"Типы свойства «{prop.format_property}» "
                                        "требуют преобразования, "
                                        "не описанного прямым ПКС.",
                                    )

                if direction != "send":
                    continue
                check = "ed.schema.required_source"
                owner = group or rule
                if not checking.start(check, owner, direction, rule if group else None):
                    continue
                if row_type is None or group_status != "resolved":
                    checking.skip(check, "owner_type_unavailable", owner)
                    continue
                if not group:
                    for child_group in rule.groups:
                        state = checking.applicability.evaluate(child_group, direction, rule)
                        if child_group.format_property and state is not False:
                            resolved = profile.resolve(row_type, child_group.format_property)
                            if resolved.status == "resolved":
                                (supplied if state is True else possible_supplied).update(
                                    resolved.property_ids
                                )
                for required, path in required_properties(profile, row_type):
                    if required.id in supplied:
                        checking.checked()
                        continue
                    if required.id in possible_supplied:
                        checking.skip(check, "opaque_condition", owner)
                    elif has_handler(rule, "send", checking.applicability):
                        checking.skip(check, "handler_may_supply", owner)
                    elif not full_send or (group is not None and not properties):
                        checking.skip(check, "full_object_not_proven", owner)
                    else:
                        checking.checked()
                        checking.report.warning(
                            check,
                            checking.address(owner),
                            f"Для обязательного свойства «{path}» "
                            "не подтверждён источник значения.",
                        )
            if direction != "receive":
                continue
            for search in rule.search_sets:
                for name in search.fields:
                    check = "ed.schema.search_source"
                    if not checking.start(check, search, direction, rule):
                        continue
                    if typ is None or type_status != "resolved":
                        checking.skip(check, "owner_type_unavailable", search)
                        continue
                    key, _ = metadata_key(rule.configuration_object.value)
                    if name.casefold() == "этогруппа" or (
                        name.casefold() == "родитель"
                        and key
                        and key[0] in ("справочник", "планвидовхарактеристик")
                    ):
                        checking.checked()
                        continue
                    candidates = [
                        p
                        for p in rule.properties
                        if p.configuration_property.casefold() == name.casefold()
                    ]
                    states = [
                        checking.applicability.evaluate(p, direction, rule) for p in candidates
                    ]
                    resolutions = []
                    for candidate in candidates:
                        source_type, status = (
                            profile.find_type(rule.format_object.value or "", candidate.namespace)
                            if candidate.namespace
                            else (typ, type_status)
                        )
                        if source_type and status == "resolved" and candidate.format_property:
                            resolved = profile.resolve(source_type, candidate.format_property)
                            resolutions.append((resolved.status, resolved.reason))
                        else:
                            resolutions.append((status, None))
                    direct = any(
                        state is True
                        and p.format_property
                        and not p.algorithm_flag
                        and resolved[0] == "resolved"
                        for p, state, resolved in zip(candidates, states, resolutions, strict=True)
                    )
                    if direct:
                        checking.checked()
                    elif has_handler(rule, direction, checking.applicability) or any(
                        p.algorithm_flag and state is not False
                        for p, state in zip(candidates, states, strict=True)
                    ):
                        checking.skip(check, "handler_may_supply", search)
                    elif None in states:
                        checking.skip(check, "opaque_condition", search)
                    elif opaque := next(
                        (
                            reason or status
                            for state, (status, reason) in zip(states, resolutions, strict=True)
                            if state is True and status not in ("resolved", "missing")
                        ),
                        None,
                    ):
                        checking.skip(check, opaque, search)
                    else:
                        checking.checked()
                        checking.report.warning(
                            check,
                            checking.address(search),
                            f"Для поля поиска «{name}» не подтверждён источник в формате.",
                        )

        for rule in document.pkpd:
            check = "ed.schema.pkpd_type_missing"
            if not checking.start(check, rule, direction):
                continue
            name = rule.format_type.value
            typ, status = (
                profile.find_type(name, dependency=True) if name else (None, "empty_format_side")
            )
            if checking.applicability.field(rule.format_type, direction) is not True:
                typ, status = None, "opaque_condition"
            elif rule.format_type.presence not in ("literal", "absent"):
                typ, status = None, "dynamic_format_type"
            if status == "missing":
                checking.checked()
                checking.report.warning(
                    check,
                    checking.address(rule),
                    f"Тип формата ПКПД «{name}» отсутствует в выбранной схеме.",
                )
            elif status == "resolved":
                checking.checked()
            else:
                checking.skip(check, status, rule)
            for pair in rule.mappings:
                check = "ed.schema.pkpd_value_missing"
                if not checking.start(check, pair, direction, rule):
                    continue
                if typ is None or status != "resolved":
                    checking.skip(check, "owner_type_unavailable", pair)
                    continue
                values, complete = enum_values(schema, typ)
                if not complete:
                    checking.skip(check, "partial_schema", pair)
                elif pair.format_value.literal_type != "string":
                    checking.skip(check, "dynamic_format_value", pair)
                else:
                    checking.checked()
                    value = pair.format_value.literal_value
                    if values and value not in values:
                        checking.report.warning(
                            check,
                            checking.address(pair),
                            f"Значение «{value}» не входит в перечисление формата «{name}».",
                        )
    if coverage is not None:
        coverage.update(checking.coverage)
    return checking.finish()
