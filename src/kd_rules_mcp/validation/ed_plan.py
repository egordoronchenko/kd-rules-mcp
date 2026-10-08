"""Состав типового плана: источники ПКО и добавления доставляемого расширения."""

from kd_rules_mcp.ed.canonical import model_addresses
from kd_rules_mcp.ed.model import EdDocument
from kd_rules_mcp.ed.writer_model import ManagerModel
from kd_rules_mcp.structures.xmldump import (
    KINDS,
    EventSubscription,
    plan_subscriptions,
    registration_events,
    registration_source_object,
    subscription_accepts,
)
from kd_rules_mcp.validation.ed_plan_stubs import CHECK, missing_plan_pods
from kd_rules_mcp.validation.ed_structure_snapshot import COLLECTIONS, StructureSnapshot
from kd_rules_mcp.validation.report import ValidationReport


def validate_plan_content(
    model: ManagerModel,
    structure: StructureSnapshot | None,
    plan_name: str,
    registration_objects: tuple[str, ...] = (),
) -> tuple[ValidationReport, tuple[tuple[str, str, str], ...]]:
    """Возвращает предупреждения по ПКО и уникальный состав для заимствования."""
    report = ValidationReport()
    declared = []
    for rule in model.pko:
        parts = rule.configuration_object.reference_parts
        if len(parts) == 3 and parts[0].casefold() == "метаданные":
            kind = COLLECTIONS.get(parts[1].casefold())
            if kind:
                declared.append((rule, kind, parts[2]))
    if not declared and not registration_objects:
        return report, ()
    plan = structure.objects.get(("планобмена", plan_name.casefold())) if structure else None
    if plan is None or not any(
        p.kind == "СоставПланаОбмена" for rows in plan.properties.values() for p in rows
    ):
        report.info(
            "ed.plan.content_unchecked",
            "ПланОбмена/" + plan_name,
            "Состав плана недоступен в структуре; проверьте его в конфигураторе",
        )
        return report, ()
    included = {
        name.casefold()
        for rows in plan.properties.values()
        for p in rows
        if p.kind == "ЭлементСоставаПланаОбмена"
        for name in p.types
    }
    addresses = model_addresses(model)
    kinds = {value[1].casefold(): (key, value[0]) for key, value in KINDS.items()}
    missing = {}
    for rule, kind, name in declared:
        obj = structure.objects.get((kind.casefold(), name.casefold())) if structure else None
        if obj is None:
            report.info(
                "ed.plan.content_unchecked",
                addresses[rule.logical_id],
                f"Объект {kind}.{name} недоступен в структуре; состав не проверен",
            )
            continue
        if obj.type_name.casefold() in included:
            continue
        report.warning(
            "ed.plan.content_missing",
            addresses[rule.logical_id],
            f"{obj.kind}.{obj.name} вне состава плана {plan_name}: изменения объекта "
            "не регистрируются подпиской БСП, правило отправки сработает только по ссылке; "
            "комплект добавляет объект в состав плана расширением. "
            "Авторегистрация запрещена: регистрацию ведут ПРО",
        )
        xml_kind, folder = kinds[obj.kind.casefold()]
        missing[(xml_kind, obj.name)] = (xml_kind, obj.name, folder)
    for full_name in registration_objects:
        kind, _, name = full_name.partition(".")
        obj = structure.objects[(kind.casefold(), name.casefold())] if structure else None
        if obj is not None and obj.type_name.casefold() not in included:
            report.warning(
                "ed.plan.content_missing",
                full_name,
                f"{full_name} вне состава плана {plan_name}; комплект добавляет объект "
                "только для регистрации, AutoRecord=Deny",
            )
            xml_kind, folder = kinds[obj.kind.casefold()]
            missing[(xml_kind, obj.name)] = (xml_kind, obj.name, folder)
    return report, tuple(missing[key] for key in sorted(missing))


def validate_plan_registration(
    structure: StructureSnapshot | None,
    plan_name: str,
    additions: tuple[tuple[str, str, str], ...] = (),
    registration_objects: tuple[str, ...] = (),
) -> tuple[ValidationReport, tuple[tuple[EventSubscription, tuple[str, ...]], ...]]:
    """Проверяет весь состав и добавления; предупреждение и дополнение Source вычисляются вместе."""
    report = ValidationReport()
    if structure is None or structure.subscriptions is None:
        report.info(
            "ed.plan.registration_unchecked",
            "ПланОбмена/" + plan_name,
            "Подписки недоступны в структуре; проверьте источники событий в конфигураторе",
        )
        return report, ()
    subscriptions = plan_subscriptions(structure.subscriptions, plan_name)
    plan = structure.objects.get(("планобмена", plan_name.casefold()))
    objects = {}
    if plan is not None:
        for rows in plan.properties.values():
            for prop in rows:
                if prop.kind == "ЭлементСоставаПланаОбмена":
                    for name in (*prop.types, *prop.unresolved):
                        obj = structure.by_type.get(name.casefold())
                        if obj:
                            objects[(obj.kind.casefold(), obj.name.casefold())] = obj
    for tag, name, _ in additions:
        key = KINDS[tag][1].casefold(), name.casefold()
        if key in structure.objects:
            objects[key] = structure.objects[key]
    for full_name in registration_objects:
        kind, _, name = full_name.partition(".")
        key = kind.casefold(), name.casefold()
        if key in structure.objects:
            objects[key] = structure.objects[key]
    missing: dict[EventSubscription, set[str]] = {}
    available = ", ".join(s.name + " (" + s.event + ")" for s in subscriptions) or "нет"
    for _, obj in sorted(objects.items()):
        for event, source in registration_events(obj.kind, obj.name):
            if any(s.event == event and s.covers(source) for s in subscriptions):
                continue
            candidates = [s for s in subscriptions if subscription_accepts(s, event, source)]
            report.warning(
                "ed.plan.registration_unsubscribed",
                obj.kind + "." + obj.name,
                f"{obj.kind}.{obj.name}: {event} не покрыт подписками плана {plan_name}. "
                f"Подписки плана: {available}. "
                + (
                    "Комплект дополнит источники типовой подписки; регистрацию ведут ПРО"
                    if candidates
                    else "Нет подходящей типовой подписки: требуется настройка плана"
                ),
            )
            for subscription in candidates:
                missing.setdefault(subscription, set()).add(source)
    return report, tuple(
        (s, tuple(sorted(missing[s]))) for s in sorted(missing, key=lambda s: s.name)
    )


def validate_plan_pods(
    model: ManagerModel | EdDocument,
    structure: StructureSnapshot | None,
    plan_name: str,
    additions: tuple[tuple[str, str, str], ...] = (),
    registration_objects: tuple[str, ...] = (),
    registered_objects: tuple[str, ...] = (),
    *,
    plan_stubs: bool = True,
):
    """Без ПОД отправки падает всё сообщение (XDTO:5323,774,8384,8354–8357)."""
    report = ValidationReport()
    missing = (
        missing_plan_pods(model, structure, plan_name, additions, registration_objects)
        if structure
        else None
    )
    if missing is None:
        report.skip(CHECK, "Структура или состав плана маршрута недоступны")
        return report, ()
    registered = {name.casefold() for name in (*registered_objects, *registration_objects)}
    for obj in missing:
        full_name = obj.kind + "." + obj.name
        has_registration_rule = full_name.casefold() in registered
        message = (
            f"{full_name}: нет ПОД отправки в плане {plan_name}; всего без ПОД: {len(missing)}. "
            + (
                "Есть ПРО; заглушка нужна и при ручной регистрации. "
                if has_registration_rule
                else "Регистрация объекта без ПРО может остановить всё сообщение. "
            )
            + (
                "Комплект добавит ПОД-заглушку без ПКО: объект не отправляется"
                if plan_stubs
                else "ПОД-заглушки отключены (plan_stubs=false); комплект не добавит ПОД"
            )
        )
        (report.info if has_registration_rule else report.warning)(CHECK, full_name, message)
    return report, missing


def subscription_source_objects(
    subscriptions: tuple[tuple[EventSubscription, tuple[str, ...]], ...],
    additions: tuple[tuple[str, str, str], ...] = (),
) -> tuple[tuple[str, str, str], ...]:
    """Объекты добавляемых источников подписок вне добавлений состава: их заимствует расширение.

    Расширение может указать в Source заимствованной подписки только заимствованный объект:
    эталон — заимствованная подписка расширения из корпуса и её объект-источник
    (docs/plans/ed-writer-plan-content-2026-10.md, раздел о подписках).
    """
    added = {(tag, name.casefold()) for tag, name, _ in additions}
    result = {}
    for _, sources in subscriptions:
        for source in sources:
            found = registration_source_object(source)
            if found is None:
                raise ValueError("Неизвестный тип источника подписки: " + source)
            tag, name = found
            if (tag, name.casefold()) not in added:
                result[(tag, name)] = (tag, name, KINDS[tag][0])
    return tuple(result[key] for key in sorted(result))
