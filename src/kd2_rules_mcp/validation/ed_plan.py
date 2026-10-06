"""Состав типового плана: источники ПКО и добавления доставляемого расширения."""

from kd2_rules_mcp.ed.canonical import model_addresses
from kd2_rules_mcp.ed.writer_model import ManagerModel
from kd2_rules_mcp.structures.xmldump import KINDS
from kd2_rules_mcp.validation.ed_structure_snapshot import COLLECTIONS, StructureSnapshot
from kd2_rules_mcp.validation.report import ValidationReport


def validate_plan_content(
    model: ManagerModel, structure: StructureSnapshot | None, plan_name: str
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
    if not declared:
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
    return report, tuple(missing[key] for key in sorted(missing))
