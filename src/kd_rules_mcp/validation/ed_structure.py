"""Проверки конфигурационной стороны ED по неизменяемому снимку."""

from collections import Counter

from kd_rules_mcp.ed import model as ed
from kd_rules_mcp.ed.address import AddressIndex
from kd_rules_mcp.ed.layer_model import EffectiveContext, LayeredManager
from kd_rules_mcp.ed.schema.profile import ValidationProfile
from kd_rules_mcp.validation.ed_structure_snapshot import (
    COLLECTIONS,
    CheckContext,
    StructureSnapshot,
    metadata_key,
    standard_attribute,
)
from kd_rules_mcp.validation.report import ValidationReport


def validate_structure(
    document: ed.EdDocument | LayeredManager,
    snapshot: StructureSnapshot,
    index: AddressIndex,
    profile: ValidationProfile,
    coverage: Counter[str] | None = None,
    *,
    context: EffectiveContext | None = None,
) -> ValidationReport:
    """Отсутствие объекта доказано статической ссылкой; свойства — предупреждения."""
    if isinstance(document, LayeredManager):
        from .ed_layers import validate_effective_structure

        if context is None:
            raise ValueError("Для действующего представления нужен контекст направления")
        return validate_effective_structure(document, context, snapshot, profile, coverage)
    checking = CheckContext(document, index, profile)
    for direction in profile.directions:
        for rule in document.pko:
            check = "ed.structure.object_missing"
            active = checking.start(check, rule, direction)
            key, reason = metadata_key(rule.configuration_object.value)
            if checking.applicability.field(rule.configuration_object, direction) is not True:
                key, reason = None, "opaque_condition"
            owner = snapshot.objects.get(key) if key and active else None
            removed = (
                profile.schema is not None
                and profile.owner_type(rule, direction, checking.applicability)[1] == "missing"
            )
            if active and key is None:
                checking.skip(check, reason, rule)
            elif active:
                checking.checked()
                if owner is None:
                    name = (
                        rule.configuration_object.value.raw
                        if rule.configuration_object.value
                        else ""
                    )
                    checking.report.error(
                        check,
                        checking.address(rule),
                        f"Объект конфигурации «{name}» отсутствует в структуре.",
                    )
            for group, properties in [
                (None, rule.properties),
                *((g, g.properties) for g in rule.groups),
            ]:
                group_reason = None
                if group:
                    check = "ed.structure.property_missing"
                    group_active = checking.start(check, group, direction, rule)
                    if group_active and removed:
                        group_reason = "owner_type_unavailable"
                        checking.skip(check, group_reason, group)
                    elif group_active and owner is None:
                        group_reason = "owner_object_unavailable"
                        checking.skip(check, group_reason, group)
                    elif group_active and not group.configuration_property:
                        group_reason = "empty_configuration_side"
                        checking.skip(check, group_reason, group)
                    elif group_active and owner:
                        checking.checked()
                        group_found = any(
                            p.kind == "ТабличнаяЧасть"
                            for p in owner.property(group.configuration_property)
                        )
                        if not group_found:
                            group_reason = "owner_object_unavailable"
                            checking.report.warning(
                                check,
                                checking.address(group),
                                f"Свойство конфигурации «{group.configuration_property}» "
                                "отсутствует в структуре.",
                            )
                for prop in properties:
                    check = "ed.structure.property_missing"
                    if not checking.start(check, prop, direction, group or rule):
                        continue
                    if removed:
                        checking.skip(check, "owner_type_unavailable", prop)
                        continue
                    if not owner or group_reason or not prop.configuration_property:
                        checking.skip(
                            check,
                            "owner_object_unavailable"
                            if not owner
                            else group_reason or "empty_configuration_side",
                            prop,
                        )
                        continue
                    path = (
                        f"{group.configuration_property}.{prop.configuration_property}"
                        if group
                        else prop.configuration_property
                    )
                    found = owner.property(path, group is not None)
                    if not found and standard_attribute(owner, path, group is not None):
                        checking.skip(check, "standard_attribute", prop)
                        continue
                    checking.checked()
                    if not found:
                        checking.report.warning(
                            check,
                            checking.address(prop),
                            f"Свойство конфигурации «{path}» отсутствует в структуре.",
                        )
            for search in rule.search_sets:
                for name in search.fields:
                    check = "ed.structure.search_missing"
                    if not checking.start(check, search, direction, rule):
                        continue
                    if removed:
                        checking.skip(check, "owner_type_unavailable", search)
                        continue
                    if owner is None:
                        checking.skip(check, "owner_object_unavailable", search)
                        continue
                    _, reason = snapshot.search(owner, name)
                    if reason not in ("resolved", "missing"):
                        checking.skip(check, reason, search)
                    else:
                        checking.checked()
                        if reason == "missing":
                            checking.report.warning(
                                check,
                                checking.address(search),
                                f"Поле поиска «{name}» отсутствует в структуре объекта.",
                            )
        for rule in document.pkpd:
            # ПКПД выбирается данными; направление известно у нормализованных пар.
            check = "ed.structure.pkpd_type_missing"
            active = checking.start(check, rule, direction)
            key, reason = metadata_key(rule.configuration_type.value)
            if checking.applicability.field(rule.configuration_type, direction) is not True:
                key, reason = None, "opaque_condition"
            owner = snapshot.objects.get(key) if key and active else None
            if active and key is None:
                checking.skip(check, reason, rule)
            elif active:
                checking.checked()
                if owner is None:
                    name = (
                        rule.configuration_type.value.raw if rule.configuration_type.value else ""
                    )
                    checking.report.error(
                        check,
                        checking.address(rule),
                        f"Тип конфигурации ПКПД «{name}» отсутствует в структуре.",
                    )
            for pair in rule.mappings:
                check = "ed.structure.pkpd_value_missing"
                if not checking.start(check, pair, direction, rule):
                    continue
                parts = pair.configuration_value.reference_parts
                if owner is None:
                    checking.skip(check, "owner_object_unavailable", pair)
                elif len(parts) != 3 or parts[0].casefold() not in COLLECTIONS:
                    checking.skip(check, "dynamic_configuration_value", pair)
                else:
                    checking.checked()
                    if (
                        COLLECTIONS[parts[0].casefold()].casefold(),
                        parts[1].casefold(),
                    ) != key or parts[2].casefold() not in owner.values:
                        checking.report.warning(
                            check,
                            checking.address(pair),
                            f"Значение конфигурации «{pair.configuration_value.raw}» "
                            "отсутствует у указанного типа ПКПД.",
                        )
    if coverage is not None:
        coverage.update(checking.coverage)
    return checking.finish()
