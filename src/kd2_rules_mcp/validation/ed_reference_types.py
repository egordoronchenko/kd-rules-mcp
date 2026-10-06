"""Жёсткий ПКО не выбирает тип составной ссылки: XDTO:1199–1218,1250–1257."""

from kd2_rules_mcp.ed import model as ed
from kd2_rules_mcp.ed.schema.profile import ValidationProfile
from kd2_rules_mcp.validation.ed_structure_snapshot import (
    CheckContext,
    StructureSnapshot,
    metadata_key,
)

CHECK = "ed.schema.reference_type_partial"


def check_reference_types(
    checking: CheckContext,
    profile: ValidationProfile,
    snapshot: StructureSnapshot | None,
    rule: ed.ObjectRule,
    group: ed.PropertyGroup | None,
    prop: ed.PropertyRule,
    targets: list[ed.ObjectRule],
) -> None:
    """Только прямая ПКС отправки на единственный ПКО с известными обеими сторонами."""
    if not checking.start(CHECK, prop, "send", group or rule):
        return
    if snapshot is None:
        checking.skip(CHECK, "structure_required", prop)
        return
    if len(targets) != 1:
        return
    target = targets[0]
    if checking.applicability.evaluate(target, "send") is not True:
        return
    if any(
        checking.applicability.field(owner.configuration_object, "send") is not True
        for owner in (rule, target)
    ):
        return
    key, _ = metadata_key(rule.configuration_object.value)
    target_key, _ = metadata_key(target.configuration_object.value)
    owner = snapshot.objects.get(key) if key else None
    covered = snapshot.objects.get(target_key) if target_key else None
    path = (
        f"{group.configuration_property}.{prop.configuration_property}"
        if group
        else prop.configuration_property
    )
    actual = owner.property(path, group is not None) if owner else ()
    if len(actual) != 1 or actual[0].unresolved or not covered or not covered.type_name:
        return
    checking.checked()
    types = {t.casefold(): t for t in actual[0].types}
    if len(types) <= 1:
        return
    extra = [name for key, name in types.items() if key != covered.type_name.casefold()]
    owner_type, status = profile.owner_type(rule, "send", checking.applicability)
    resolved = (
        profile.resolve(owner_type, prop.format_property, group.format_property if group else None)
        if owner_type and status == "resolved" and prop.format_property
        else None
    )
    key_property = bool(
        resolved
        and resolved.status == "resolved"
        and any(
            p and p[0].local.casefold().startswith("ключевыесвойства")
            for p in resolved.physical_paths
        )
    )
    checking.report.warning(
        CHECK,
        checking.address(prop),
        ("Ключевая ПКС-ссылка" if key_property else "ПКС-ссылка")
        + f" отправки {checking.address(prop)}: реквизит «{path}» допускает типы "
        + ", ".join(sorted(types.values()))
        + f"; ПКО «{target.name}» покрывает только {covered.type_name}. Значение типа "
        + ", ".join(sorted(extra))
        + " уронит выгрузку объекта и всех ссылающихся объектов (XDTO:1199–1218). "
        "Задайте алгоритмическую ПКС с выбором ПКО по типу значения; "
        "обработчик отправки вызывается после конвертации ключевых ссылок (XDTO:1250–1257).",
    )
