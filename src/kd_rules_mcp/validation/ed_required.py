"""Риски пустых обязательных свойств: статическая проверка, без чтения данных ИБ."""

from collections.abc import Sequence
from dataclasses import dataclass

from kd_rules_mcp.ed import model as ed
from kd_rules_mcp.ed.schema.model import EdSchema, SchemaProperty, SchemaType
from kd_rules_mcp.ed.schema.profile import ValidationProfile
from kd_rules_mcp.ed.schema.resolver import is_reference, property_type
from kd_rules_mcp.validation.report import ValidationReport

from .ed_structure_snapshot import CheckContext, StructureSnapshot, metadata_key

CHECK = "ed.schema.required_unfilled"
PARTIAL = "ed.schema.required_unfilled.partial"
_FILL_UNAVAILABLE = "fill_checking_unavailable (reload XML structure)"


@dataclass(frozen=True, slots=True)
class RequiredUnfilled:
    """Источник запроса и адрес ПКС; в комплект не попадают данные информационной базы."""

    address: str
    object_kind: str
    object_name: str
    property_path: str
    source_types: tuple[str, ...]
    format_property: str
    reference_key: bool


def check_required_unfilled(
    checking: CheckContext,
    profile: ValidationProfile,
    schema: EdSchema,
    snapshot: StructureSnapshot | None,
    rule: ed.ObjectRule,
    group: ed.PropertyGroup | None,
    prop: ed.PropertyRule,
    owner: SchemaType | None,
    target: SchemaProperty | None,
) -> RequiredUnfilled | None:
    """ЗначениеЗаполнено проверяется до ПКПД/ПКО (XDTO:1539–1573), затем XDTO:985–993.

    Число 0 и пустая дата также пропускаются; валидность их лексического представления
    по XSD не означает, что исполнитель установит свойство (XDTO:6078–6086).
    Булево не имеет пустого значения в смысле ЗначениеЗаполнено.
    """
    if prop.algorithm_flag or not checking.start(CHECK, prop, "send", group or rule):
        return None
    if snapshot is None:
        checking.skip(CHECK, "structure_unavailable (supply structure_id)", prop)
        return None
    if target is None or owner is None:
        checking.skip(CHECK, "owner_type_unavailable", prop)
        return None
    if target.lower < 1 or target.nillable:
        checking.checked()
        return None
    state = checking.applicability.field(rule.configuration_object, "send")
    if state is not True:
        checking.skip(
            CHECK, "opaque_condition" if state is None else "empty_configuration_side", prop
        )
        return None
    key, reason = metadata_key(rule.configuration_object.value)
    obj = snapshot.objects.get(key) if key else None
    path = (
        f"{group.configuration_property}.{prop.configuration_property}"
        if group
        else prop.configuration_property
    )
    actual = (
        obj.property(path, group is not None)
        if obj and prop.configuration_property and (not group or group.configuration_property)
        else ()
    )
    if obj is None or len(actual) != 1 or actual[0].unresolved:
        checking.skip(CHECK, reason if key is None else "unresolved_configuration_type", prop)
        return None
    source = actual[0]
    if not source.fill_checking:
        checking.fill_checking_missing[prop.entity_id] = obj.kind + "." + obj.name
        checking.skip(CHECK, _FILL_UNAVAILABLE, prop)
        return None
    if source.fill_checking == "ShowError":
        checking.checked()
        return None
    if not source.types or any(
        name not in {"Строка", "Число", "Дата", "Булево"} and "Ссылка." not in name
        for name in source.types
    ):
        checking.skip(CHECK, "empty_value_semantics_unavailable", prop)
        return None
    checking.checked()
    if set(source.types) == {"Булево"}:
        return None
    resolved = profile.resolve(
        owner, prop.format_property, group.format_property if group else None
    )
    reference = profile.resolve(owner, "Ссылка")
    reference_type = (
        property_type(schema, profile.properties[reference.property_ids[0]])
        if reference.status == "resolved"
        else None
    )
    reference_key = (
        group is None
        and reference_type is not None
        and is_reference(schema, reference_type)
        and any(
            part.local == "КлючевыеСвойства"
            for physical in resolved.physical_paths
            for part in physical[:-1]
        )
    )
    if group is None:
        # Optional обычная оболочка может отсутствовать целиком. Ключ ссылки исполнитель
        # собирает отдельно, даже когда полный объект не отправляется (XDTO:1575–1579).
        current = owner
        for part in resolved.physical_paths[0][:-1]:
            wrapper = next((p for p in schema.inherited[current.id] if p.name == part), None)
            if wrapper is None:
                checking.skip(CHECK, "partial_schema", prop)
                return None
            if (wrapper.lower < 1 or wrapper.nillable) and not reference_key:
                return None
            following = property_type(schema, wrapper)
            if following is None:
                checking.skip(CHECK, "partial_schema", prop)
                return None
            current = following
    risk = RequiredUnfilled(
        checking.address(prop),
        obj.kind,
        obj.name,
        source.path,
        source.types,
        prop.format_property,
        reference_key,
    )
    message = (
        f"{risk.address}: свойство формата «{prop.format_property}» обязательно в схеме; "
        f"источник «{obj.kind}.{obj.name}.{source.path}» — реквизит без обязательного "
        f"заполнения (FillChecking={source.fill_checking}). Пустое значение остановит выгрузку."
    )
    if reference_key:
        message += (
            " Это ключевое свойство ссылочного типа: пустое значение остановит выгрузку "
            "всех объектов со ссылкой на этот, включая документы, а не только справочник."
        )
    checking.report.warning(CHECK, risk.address, message)
    return risk


def partial_message(names: Sequence[str]) -> str:
    """Не больше трёх имён объектов (ответ ed_validate держится в бюджете); хвост — «и ещё N»."""
    shown = list(names[:3])
    hidden = len(names) - len(shown)
    tail = f" и ещё {hidden}" if hidden else ""
    return "Нет FillChecking (перечитайте XML-структуру): " + ", ".join(shown) + tail + "."


def settle_required_unfilled(checking: CheckContext, report: ValidationReport) -> None:
    """Пропуск fill_checking не отменяет уже найденные замечания той же проверки.

    `skipped` остаётся, только если замечаний нет. Иначе сведение `.partial`
    называет пропущенные объекты, а проверка не числится невыполненной.
    """
    recorded = checking.fill_checking_missing
    instances: dict[str, str] = {}
    for (check, reason), items in checking.skips.items():
        if check == CHECK and reason == _FILL_UNAVAILABLE:
            instances.update(items)
    if not instances or not any(issue.check == CHECK for issue in report.issues):
        return
    names: list[str] = []
    seen: set[str] = set()
    for entity_id in instances:
        name = recorded.get(entity_id)
        if name and name not in seen:
            seen.add(name)
            names.append(name)
    names.sort()
    if not names:
        return
    report.skipped = [
        item
        for item in report.skipped
        if not (item.check == CHECK and item.reason.startswith(_FILL_UNAVAILABLE))
    ]
    report.info(PARTIAL, "Конвертация", partial_message(names))


# Завершающий шаг обоих путей (ed_validate и сборка): явная подписка на CheckContext.finish.
if settle_required_unfilled not in CheckContext.finalizers:
    CheckContext.finalizers.append(settle_required_unfilled)
