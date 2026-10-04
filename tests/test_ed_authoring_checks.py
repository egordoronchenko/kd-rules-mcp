"""Критерий выбранных профилей, другие версии, baseline и границы неизвестного."""

from dataclasses import replace
from types import MappingProxyType

import pytest

from kd2_rules_mcp.authoring.ed.model import (
    AuthoringPreconditionError,
    ExtensionIdentity,
    ProfileReport,
    SourceSet,
    ValidationDelta,
)
from kd2_rules_mcp.ed.model import Field, HandlerBinding, PropertyRule, UnknownFragment
from kd2_rules_mcp.ed.route_model import RouteEntry, RouteSkip
from kd2_rules_mcp.ed.schema.model import EdSchema, QName
from kd2_rules_mcp.ed.schema.xdto import XS
from kd2_rules_mcp.validation.ed_authoring import (
    check_profile,
    compare_reports,
    enforce_delta,
    prepare_authoring,
)
from kd2_rules_mcp.validation.report import Issue, Level, Skipped
from tests.test_ed_authoring_model import (
    DATA,
    IDENTITY,
    OPERATION,
    TARGET,
    inputs,
    refreshed,
    rule_changed,
)


def report(issues=(), skipped=(), version="1.20", direction="send"):
    return ProfileReport(version, direction, tuple(issues), tuple(skipped))


def test_baseline_same_counts_changed_warning_disappeared_and_multiset():
    old = Issue(
        Level.WARNING, "ed.schema.property_missing", "ПКО/Товар/ПКС/Старое", "Старое замечание"
    )
    new = Issue(
        Level.WARNING, "ed.schema.property_missing", "ПКО/Товар/ПКС/Новое", "Новое замечание"
    )
    delta = compare_reports(report((old, old)), report((old, new)))
    assert delta.new == (new,) and delta.disappeared == (old,)
    assert not delta.no_new_issues
    assert compare_reports(report((old,)), report((old,))).no_new_issues
    duplicate = compare_reports(report((old,)), report((old, old)))
    assert duplicate.new == (old,)
    assert compare_reports(report((old,)), report()).disappeared == (old,)


def test_new_issues_bad_and_clean():
    value = inputs()
    bad = ValidationDelta(
        (
            Issue(
                Level.WARNING,
                "ed.schema.property_missing",
                "ПКО/Товар/ПКС/Комментарий",
                "Новый дефект",
            ),
        ),
        (),
        (),
    )
    with pytest.raises(AuthoringPreconditionError) as caught:
        enforce_delta(bad, (OPERATION,), value.document)
    failure = caught.value.failures[0]
    assert (failure.id, failure.address, failure.message) == (
        "ed.author.new_issues",
        "ПКО/Товар",
        "Новое замечание ed.schema.property_missing; адрес «ПКО/Товар/ПКС/Комментарий»; "
        f"Новый дефект; операция {OPERATION.operation_id}",
    )
    assert failure.file == "fiction/CommonModules/Менеджер2/Ext/Module.bsl" and failure.line > 0
    enforce_delta(ValidationDelta((), (), ()), (OPERATION,), value.document)


def test_relevant_skipped_blocks_and_unrelated_does_not():
    old = Skipped("ed.schema.property_missing", "opaque_condition: 1; ПКО/Заказ/ПКС/Старое")
    new = Skipped(
        "ed.schema.property_missing",
        "opaque_condition: 2; ПКО/Заказ/ПКС/Старое, ПКО/Товар/ПКС/Комментарий",
    )
    delta = compare_reports(
        report(skipped=(old,)), report(skipped=(new,)), relevant_addresses=("ПКО/Товар",)
    )
    assert delta.new_relevant_skipped == (
        Skipped("ed.schema.property_missing", "opaque_condition: ПКО/Товар/ПКС/Комментарий"),
    )
    assert not delta.no_new_issues
    unrelated = Skipped("ed.schema.property_missing", "opaque_condition: 1; ПКО/Заказ/ПКС/Новое")
    assert compare_reports(
        report(), report(skipped=(unrelated,)), relevant_addresses=("ПКО/Товар",)
    ).no_new_issues
    assert compare_reports(
        report(skipped=(old,)), report(skipped=(old,)), relevant_addresses=("ПКО/Товар",)
    ).no_new_issues


def test_logical_address_renumbering_does_not_create_defect():
    old = Issue(
        Level.WARNING, "ed.schema.property_missing", "ПКО/Товар/ПКС/Код", "Независимое замечание"
    )
    new = replace(old, address="ПКО/Товар/ПКС/Код#1")
    delta = compare_reports(
        report((old,)), report((new,)), logical_addresses={new.address: old.address}
    )
    assert delta.new == delta.disappeared == ()
    assert not compare_reports(
        report((old,), version="1.20"), report((old,), version="1.21")
    ).no_new_issues


@pytest.mark.parametrize(
    "direction,expected",
    [
        ("send", "В версиях 1.21 свойства «Комментарий» или типа ПКО нет: значение не передаётся"),
        (
            "receive",
            "В версиях 1.21 свойства «Комментарий» или типа ПКО нет: "
            "реквизит найденного объекта может очищаться при каждом получении",
        ),
    ],
)
def test_other_version_one_notice_selected_profile_passes(direction, expected):
    value = inputs()
    op = replace(OPERATION, target=replace(TARGET, direction=direction))
    prepared = prepare_authoring(value, (op,), IDENTITY, version_scope="manager")
    notices = [n for n in prepared.notices if n.id == "ed.author.other_version_incompatible"]
    assert len(notices) == 1
    assert (notices[0].version_keys, notices[0].message) == (("1.21",), expected)
    assert notices[0].notice_id == "ed.author.other_version_incompatible:" + op.operation_id
    assert len(prepared.selected_profiles) == len(prepared.other_profiles) == 1
    assert prepared.selected_profiles[0].delta.no_new_issues
    assert prepared.other_profiles[0].delta.new[0].check == "ed.schema.property_missing"
    assert prepared.runtime_verified is False
    assert (
        prepared.build_hash
        == prepare_authoring(value, (op, op), IDENTITY, version_scope="manager").build_hash
    )
    assert "runtime.object_conversion" in {s.check for s in prepared.skipped}


def test_other_version_keys_aggregated_and_unverified():
    value = inputs()
    plan = value.routes.plans[0]
    entry = replace(plan.entries[1], key="1.22", key_raw="1.22")
    unknown = replace(entry, key="1.23", key_raw="1.23")
    schema = value.schemas["1.21"]
    value = refreshed(
        value,
        schemas={**value.schemas, "1.22": schema, "1.23": "Нет доступного пакета"},
        routes=replace(
            value.routes, plans=(replace(plan, entries=(*plan.entries, entry, unknown)),)
        ),
    )
    prepared = prepare_authoring(value, (OPERATION,), IDENTITY, version_scope="manager")
    notices = {n.id: n for n in prepared.notices}
    assert notices["ed.author.other_version_incompatible"].version_keys == ("1.21", "1.22")
    assert notices["ed.author.other_version_unverified"].version_keys == ("1.23",)
    assert prepared.selected_profiles[0].delta.no_new_issues


def test_old_defect_cannot_explain_bad_new_operation():
    value = inputs()
    # Уже отсутствующий тип даёт старое предупреждение; предусловие остаётся отказом.
    rule = value.document.pko[0]
    value = rule_changed(value, format_object=Field("literal", "НетТипа"))
    schema = value.schemas["1.20"]
    assert isinstance(schema, EdSchema)
    before = check_profile(value.document, schema, value.structure, "1.20", "send")
    assert any(i.check == "ed.schema.type_missing" for i in before.issues)
    with pytest.raises(AuthoringPreconditionError) as caught:
        prepare_authoring(value, (OPERATION,), IDENTITY, version_scope="manager")
    assert any(
        f.id == "ed.author.pko_removed" and f.address == "ПКО/Товар" for f in caught.value.failures
    )
    assert rule.span == value.document.pko[0].span


def test_existing_baseline_warning_preserved_and_unrelated_unknown_allowed():
    value = inputs()
    other = value.document.pko[1]
    bad_property = replace(other.properties[0], format_property="ПостороннееСвойство")
    value = refreshed(
        value,
        document=replace(
            value.document, pko=(value.document.pko[0], replace(other, properties=(bad_property,)))
        ),
    )
    prepared = prepare_authoring(value, (OPERATION,), IDENTITY, version_scope="manager")
    selected = prepared.selected_profiles[0]
    assert selected.delta.no_new_issues
    assert any(i.address == "ПКО/Заказ/ПКС/ПостороннееСвойство" for i in selected.before.issues)
    text = (DATA / "base/manager-v2.bsl").read_text(
        "utf-8"
    ) + "\nПроцедура Посторонняя()\n ВычислитьНеизвестное();\nКонецПроцедуры\n"
    opaque = inputs(text=text)
    prepared = prepare_authoring(opaque, (OPERATION,), IDENTITY, version_scope="manager")
    assert prepared.projection_after.base_parse_status == "partial"
    assert prepared.selected_profiles[0].delta.no_new_issues


def test_handler_notice_lists_methods_and_keeps_runtime_unknown():
    value = inputs()
    rule = value.document.pko[0]
    binding = HandlerBinding(
        entity_id="binding",
        kind="binding",
        name="ПередЗаписьюПолученныхДанных",
        span=rule.span,
        raw_text="",
        owner_id=rule.entity_id,
        event="ПередЗаписьюПолученныхДанных",
        target_name="ПКО_Товар_ПередЗаписьюПолученныхДанных",
    )
    value = rule_changed(value, events=(binding,))
    receive = replace(OPERATION, target=replace(TARGET, direction="receive"))
    prepared = prepare_authoring(value, (receive,), IDENTITY, version_scope="manager")
    notice = next(n for n in prepared.notices if n.id == "ed.author.handler_effect_unknown")
    assert notice.methods == ("Обработчик/ПКО_Товар_ПередЗаписьюПолученныхДанных",)
    assert notice.address == "ПКО/Товар"
    assert not prepared.runtime_verified
    assert any(
        "handler_may_supply" in s.reason for s in prepared.selected_profiles[0].after.skipped
    )


def test_new_unknown_not_allowed_even_with_unchanged_baseline():
    value = inputs()
    rule = value.document.pko[0]
    unknown = UnknownFragment(
        entity_id="unknown",
        kind="unknown",
        name="unknown",
        span=rule.span,
        raw_text="",
        owner_id=rule.entity_id,
        reason="unknown",
    )
    value = refreshed(value, document=replace(value.document, unknown=(unknown,)))
    with pytest.raises(AuthoringPreconditionError) as caught:
        prepare_authoring(value, (OPERATION,), IDENTITY, version_scope="manager")
    assert any(f.id == "ed.author.target_partial" for f in caught.value.failures)


def test_build_hash_tracks_normalized_decisions_input_hashes_and_prefix():
    value = inputs()
    prepared = prepare_authoring(value, (OPERATION,), IDENTITY, version_scope="manager")
    changed = prepare_authoring(
        value, (OPERATION,), replace(IDENTITY, version="0.2"), version_scope="manager"
    )
    assert prepared.build_hash != changed.build_hash
    text = (DATA / "base/manager-v2.bsl").read_text("utf-8") + "\n// Изменение входа\n"
    assert (
        prepared.build_hash
        != prepare_authoring(
            inputs(text=text), (OPERATION,), IDENTITY, version_scope="manager"
        ).build_hash
    )


def test_projection_rechecks_physical_id_conflict_and_direction():
    value = inputs()
    prepared = prepare_authoring(value, (OPERATION,), IDENTITY, version_scope="manager")
    first = prepared.projection_after.document.pko[0].properties[-1]
    assert isinstance(first, PropertyRule)
    schema = value.schemas["1.20"]
    assert isinstance(schema, EdSchema)
    opposite = check_profile(
        prepared.projection_after.document, schema, prepared.structure_after, "1.20", "receive"
    )
    assert not any(i.address.endswith("/Комментарий") for i in opposite.issues)


def test_one_hundred_valid_operations_do_not_turn_truncated_skips_into_failure():
    value = inputs()
    schema = value.schemas["1.20"]
    assert isinstance(schema, EdSchema) and OPERATION.new_attribute is not None
    owner = schema.types[QName(schema.base_namespace, "Справочник.Товары")]
    primitive = next(p for p in owner.properties if p.name.local == "ВнешнийКод")
    added = tuple(
        replace(primitive, id=f"extra-{i}", name=QName(schema.base_namespace, f"Поле{i:03}"))
        for i in range(100)
    )
    updated = replace(owner, properties=(*owner.properties, *added))
    schema = replace(
        schema,
        types=MappingProxyType({**schema.types, owner.qname: updated}),
        by_id=MappingProxyType({**schema.by_id, owner.id: updated}),
        inherited=MappingProxyType(
            {**schema.inherited, owner.id: (*schema.inherited[owner.id], *added)}
        ),
    )
    value = refreshed(value, schemas={**value.schemas, "1.20": schema})
    operations = tuple(
        replace(
            OPERATION,
            configuration_attribute=f"доп_Поле{i:03}",
            format_property=f"Поле{i:03}",
            new_attribute=replace(OPERATION.new_attribute, name=f"доп_Поле{i:03}"),
        )
        for i in range(100)
    )
    prepared = prepare_authoring(value, operations, IDENTITY, version_scope="manager")
    assert prepared.projection_after.generated_operations_applied == 100
    assert prepared.selected_profiles[0].delta.no_new_issues


def test_other_version_with_changed_primitive_is_incompatible():
    value = inputs()
    schema = value.schemas["1.21"]
    assert isinstance(schema, EdSchema)
    owner = schema.types[QName(schema.base_namespace, "Справочник.Товары")]
    changed = tuple(
        replace(p, type_ref=QName(XS, "boolean")) if p.name.local == "ВнешнийКод" else p
        for p in owner.properties
    )
    updated = replace(owner, properties=changed)
    schema = replace(
        schema,
        types=MappingProxyType({**schema.types, owner.qname: updated}),
        by_id=MappingProxyType({**schema.by_id, owner.id: updated}),
        inherited=MappingProxyType({**schema.inherited, owner.id: changed}),
    )
    value = refreshed(value, schemas={**value.schemas, "1.21": schema})
    op = replace(OPERATION, format_property="ВнешнийКод")
    prepared = prepare_authoring(value, (op,), IDENTITY, version_scope="manager")
    assert prepared.selected_profiles[0].delta.no_new_issues
    notice = next(n for n in prepared.notices if n.id == "ed.author.other_version_incompatible")
    assert notice.version_keys == ("1.21",)
    assert (
        notice.message
        == "В версиях 1.21 тип свойства не совпадает: передача прямой ПКС несовместима со схемой"
    )


@pytest.mark.parametrize("property_name", ["комментарий", " Комментарий ", " КОММЕНТАРИЙ "])
def test_canonical_names_hashes_projection_and_duplicate_noop(property_name):
    value = inputs()
    normal = replace(OPERATION, configuration_attribute="Заметка", new_attribute=None)
    variant = replace(
        normal,
        target=replace(
            TARGET,
            pko_address="пко/ТОВАР",
            plan="планформата",
            project="пример",
            configuration="main",
        ),
        configuration_attribute="ЗАМЕТКА",
        format_property=property_name,
    )
    first = prepare_authoring(value, (normal,), IDENTITY, version_scope="manager")
    second = prepare_authoring(value, (variant, normal), IDENTITY, version_scope="manager")
    assert len(second.operations) == second.projection_after.generated_operations_applied == 1
    canonical = second.operations[0]
    assert (
        canonical.target.pko_address,
        canonical.configuration_attribute,
        canonical.format_property,
    ) == ("ПКО/Товар", "Заметка", "Комментарий")
    assert canonical.operation_id == first.operations[0].operation_id == normal.operation_id
    assert second.build_hash == first.build_hash
    assert second.generated_hook.source.text == first.generated_hook.source.text
    assert (
        'ДобавитьПКС(Правило.Свойства, "Заметка", "Комментарий");'
        in second.generated_hook.source.text
    )
    prop = second.projection_after.document.pko[0].properties[-1]
    assert (prop.configuration_property, prop.format_property) == ("Заметка", "Комментарий")
    assert all(n.address == "ПКО/Товар" for n in second.notices)


def test_canonical_full_path_preserves_agent_path_form():
    op = replace(OPERATION, format_property=" общиесвойстваобъектовформата.КОММЕНТАРИЙ ")
    prepared = prepare_authoring(inputs(), (op,), IDENTITY, version_scope="manager")
    assert prepared.operations[0].format_property == "ОбщиеСвойстваОбъектовФормата.Комментарий"
    assert '"ОбщиеСвойстваОбъектовФормата.Комментарий"' in prepared.generated_hook.source.text


@pytest.mark.parametrize("name", ["Заметка", " ЗАМЕТКА "])
def test_attribute_edge_spaces_are_canonical(name):
    op = replace(OPERATION, configuration_attribute=name, new_attribute=None)
    result = prepare_authoring(inputs(), (op,), IDENTITY, version_scope="manager")
    assert result.operations[0].configuration_attribute == "Заметка"


def test_invalid_attribute_reports_original_input():
    bad = " Зам етка "
    with pytest.raises(AuthoringPreconditionError) as caught:
        prepare_authoring(
            inputs(),
            (replace(OPERATION, configuration_attribute=bad, new_attribute=None),),
            IDENTITY,
            version_scope="manager",
        )
    assert caught.value.failures[0].message == (
        f"Недопустимый идентификатор «{bad}» или префикс нового реквизита"
    )


@pytest.mark.parametrize("full", [False, True])
def test_missing_value_notice_reason_matches_executor_branch(full):
    name = "ОбщиеСвойстваОбъектовФормата.Комментарий" if full else "Комментарий"
    op = replace(OPERATION, target=replace(TARGET, direction="receive"), format_property=name)
    result = prepare_authoring(inputs(), (op,), IDENTITY, version_scope="manager")
    notice = next(n for n in result.notices if n.id == "ed.author.missing_value_clears")
    reason = "Для полного пути с точкой" if full else "Защита сравнивает имена разных сторон"
    assert reason in notice.message


def test_exact_property_name_wins_case_ambiguity():
    from kd2_rules_mcp.authoring.ed.canonical import canonical_property
    from kd2_rules_mcp.ed.schema.profile import ValidationProfile

    value = inputs()
    schema = value.schemas["1.20"]
    assert isinstance(schema, EdSchema)
    profile = ValidationProfile.build(schema, "1.20", "send")
    assert profile.schema is not None
    typ = next(
        t for t in profile.schema.types.values() if t.qname and t.qname.local == "Справочник.Товары"
    )
    rows = profile.effective[typ.id]
    original, path = next((p, path) for p, path in rows if p.name.local == "Комментарий")
    lower = replace(original, id="lower", name=replace(original.name, local="комментарий"))
    profile = replace(
        profile,
        properties={**profile.properties, "lower": lower},
        effective={
            **profile.effective,
            typ.id: (*rows, (lower, (*path[:-1], replace(path[-1], local="комментарий")))),
        },
    )
    assert canonical_property(profile, typ, "Комментарий") == "Комментарий"
    assert canonical_property(profile, typ, "комментарий") == "комментарий"
    assert canonical_property(profile, typ, "КОММЕНТАРИЙ") is None


@pytest.mark.parametrize(
    "bad",
    [
        "Комм ентарий",
        "Комментарий\n",
        "Комм\tентарий",
        "Комм\u2028ентарий",
        "Комм\u0000ентарий",
        "Отсутствует",
        "ОбщиеСвойстваОбъектовФормата. Комментарий",
    ],
)
def test_unresolved_spaces_and_controls_are_refused(bad):
    op = replace(OPERATION, format_property=bad)
    with pytest.raises(AuthoringPreconditionError) as caught:
        prepare_authoring(inputs(), (op,), IDENTITY, version_scope="manager")
    assert [(f.id, f.address, f.message) for f in caught.value.failures] == [
        (
            "ed.author.schema_property_missing",
            "ПКО/Товар",
            f"Свойство формата «{bad}» не разрешено однозначно",
        )
    ]


@pytest.mark.parametrize("name,expected", [("КОММЕНТАРИЙ", True), ("Комментарий", False)])
def test_receive_names_are_compared_exactly_after_resolution(name, expected):
    assert OPERATION.new_attribute is not None
    identity = ExtensionIdentity("ДоработкаОбмена", "Ком")
    op = replace(
        OPERATION,
        target=replace(TARGET, direction="receive"),
        configuration_attribute=name,
        format_property=" КОММЕНТАРИЙ ",
        new_attribute=replace(OPERATION.new_attribute, name=name),
    )
    prepared = prepare_authoring(inputs(), (op,), identity, version_scope="manager")
    assert any(n.id == "ed.author.missing_value_clears" for n in prepared.notices) is expected
    incompatible = next(
        n for n in prepared.notices if n.id == "ed.author.other_version_incompatible"
    )
    assert ("может очищаться" in incompatible.message) is expected
    assert incompatible.operation_id == prepared.operations[0].operation_id


def test_relevant_and_proven_skips_use_canonical_case():
    item = Skipped("ed.structure.pks_target", "standard_attribute: 1; ПКО/Товар/ПКС/Комментарий")
    assert compare_reports(
        report(), report(skipped=(item,)), relevant_addresses=("пко/товар",)
    ).new_relevant_skipped == (
        Skipped("ed.structure.pks_target", "standard_attribute: ПКО/Товар/ПКС/Комментарий"),
    )
    narrow = Skipped("ed.schema.type_incompatible", "non_atomic_type: 1; ПКО/Товар/ПКС/Комментарий")
    assert compare_reports(
        report(),
        report(skipped=(narrow,)),
        relevant_addresses=("пко/товар",),
        proven_type_addresses=("пко/товар/пкс/комментарий",),
    ).no_new_issues


@pytest.mark.parametrize("defect", ["partial", "manager_missing", "unparsed"])
def test_selected_route_must_be_complete(defect):
    value = inputs()
    plan = value.routes.plans[0]
    if defect == "partial":
        plan = replace(plan, status="partial")
    elif defect == "manager_missing":
        plan = replace(
            plan, entries=(replace(plan.entries[0], manager_name=None), *plan.entries[1:])
        )
    routes = replace(value.routes, plans=(plan, *value.routes.plans[1:]))
    if defect == "unparsed":
        routes = replace(
            routes,
            reading=replace(routes.reading, unparsed_map_operations=1),
            skipped=(
                RouteSkip(
                    "route.unparsed_map_operation",
                    "Вставить(Ключ, Модуль)",
                    plan.entries[0].source.relative_file,
                    7,
                ),
            ),
        )
    value = refreshed(value, routes=routes)
    with pytest.raises(AuthoringPreconditionError) as caught:
        prepare_authoring(value, (OPERATION,), IDENTITY, version_scope="manager")
    assert [(f.id, f.address, f.message) for f in caught.value.failures] == [
        (
            "ed.author.route_unresolved",
            "ПКО/Товар",
            "Маршрут ED не разрешён однозначно на прочитанный менеджер",
        )
    ]


@pytest.mark.parametrize("defect", ["partial", "manager_missing", "unparsed"])
def test_other_route_unknown_includes_reason_and_place(defect):
    value = inputs()
    selected, other = value.routes.plans
    source = replace(
        other.entries[0].source,
        relative_file="ExchangePlans/Другой/Ext/ManagerModule.bsl",
        line_start=17,
    )
    entry = replace(
        other.entries[0],
        key="1.22",
        key_raw="1.22",
        source=source,
        manager_name=None if defect == "manager_missing" else "Менеджер2",
    )
    other = replace(
        other,
        entries=(entry,),
        settings_source=source,
        status="partial" if defect != "manager_missing" else "complete",
    )
    routes = replace(value.routes, plans=(selected, other))
    if defect == "unparsed":
        routes = replace(
            routes,
            reading=replace(routes.reading, unparsed_map_operations=1),
            skipped=(
                RouteSkip(
                    "route.unparsed_map_operation", "Не прочитана вставка", source.relative_file, 17
                ),
            ),
        )
    value = refreshed(
        value, routes=routes, schemas={**value.schemas, "1.22": value.schemas["1.20"]}
    )
    prepared = prepare_authoring(value, (OPERATION,), IDENTITY, version_scope="manager")
    notice = next(n for n in prepared.notices if n.id == "ed.author.other_version_unverified")
    assert notice.version_keys == ("1.22",)
    assert "ExchangePlans/Другой/Ext/ManagerModule.bsl:17" in notice.message
    assert (
        "Менеджер не определён" if defect == "manager_missing" else "Неполная карта"
    ) in notice.message
    if defect == "unparsed":
        assert "Не прочитана вставка" in notice.message
    assert prepared.selected_profiles[0].delta.no_new_issues


def test_route_interface_mismatch_is_snapshot_mismatch():
    value = inputs()
    value = refreshed(
        value,
        routes=replace(
            value.routes,
            managers=(
                replace(value.routes.managers[0], interface_version=3),
                *value.routes.managers[1:],
            ),
        ),
    )
    with pytest.raises(AuthoringPreconditionError) as caught:
        prepare_authoring(value, (OPERATION,), IDENTITY, version_scope="manager")
    assert [(f.id, f.address, f.message) for f in caught.value.failures] == [
        (
            "ed.author.snapshot_mismatch",
            "ПКО/Товар",
            "Версия интерфейса маршрута не совпадает с прочитанным документом",
        )
    ]


def test_unknown_manager_for_other_key_in_selected_plan_is_a_notice():
    value = inputs()
    plan = value.routes.plans[0]
    entry = RouteEntry("1.22", "1.22", None, plan.entries[0].source)
    value = refreshed(
        value, routes=replace(value.routes, plans=(replace(plan, entries=(*plan.entries, entry)),))
    )
    prepared = prepare_authoring(value, (OPERATION,), IDENTITY, version_scope="manager")
    notice = next(n for n in prepared.notices if n.id == "ed.author.other_version_unverified")
    assert notice.version_keys == ("1.22",)
    assert "Менеджер не определён" in notice.message
    assert "ExchangePlans/ПланФормата/Ext/ManagerModule.bsl:1" in notice.message
    assert prepared.selected_profiles[0].delta.no_new_issues


def test_route_manager_identifier_uses_metadata_case():
    value = inputs()
    plan = value.routes.plans[0]
    value = refreshed(
        value,
        routes=replace(
            value.routes,
            plans=(
                replace(
                    plan, entries=tuple(replace(e, manager_name="менеджер2") for e in plan.entries)
                ),
            ),
        ),
    )
    prepared = prepare_authoring(value, (OPERATION,), IDENTITY, version_scope="manager")
    assert prepared.generated_hook.source.path == "modules/CommonModules/Менеджер2/Ext/Module.bsl"
    assert prepared.selected_profiles[0].delta.no_new_issues


def test_partial_other_plan_without_keys_is_not_silently_ignored():
    value = inputs()
    selected, other = value.routes.plans
    value = refreshed(
        value,
        routes=replace(
            value.routes, plans=(selected, replace(other, status="partial", entries=()))
        ),
    )
    prepared = prepare_authoring(value, (OPERATION,), IDENTITY, version_scope="manager")
    notice = next(n for n in prepared.notices if n.id == "ed.author.other_version_unverified")
    assert notice.version_keys == ("<неизвестный ключ: ВторойПлан>",)
    assert "Неполная карта без прочитанных ключей" in notice.message
    assert "ExchangePlans/ПланФормата/Ext/ManagerModule.bsl:1" in notice.message
    assert prepared.selected_profiles[0].delta.no_new_issues


def test_two_selected_versions_have_clear_scope_refusal():
    second = replace(
        OPERATION, target=replace(TARGET, format_version="1.21"), format_property="ВнешнийКод"
    )
    with pytest.raises(AuthoringPreconditionError) as caught:
        prepare_authoring(inputs(), (OPERATION, second), IDENTITY, version_scope="manager")
    assert any(
        (f.id, f.address, f.message)
        == (
            "ed.author.scope_required",
            "ПКО/Товар",
            "операции одного менеджера проверяются по одной выбранной версии; "
            "остальные версии показываются как другие",
        )
        for f in caught.value.failures
    )


def schema_property_changed(
    schema: EdSchema, name: str, *, type_ref: QName | None = None, remove: bool = False
) -> EdSchema:
    """Меняет одну декларацию тестовой схемы, согласуя её неизменяемые индексы."""
    owner = schema.types[QName(schema.base_namespace, "Справочник.Товары")]
    properties = tuple(
        replace(p, type_ref=type_ref) if p.name.local == name and type_ref else p
        for p in owner.properties
        if not (remove and p.name.local == name)
    )
    changed = replace(owner, properties=properties)
    return replace(
        schema,
        types=MappingProxyType({**schema.types, owner.qname: changed}),
        by_id=MappingProxyType({**schema.by_id, owner.id: changed}),
        inherited=MappingProxyType({**schema.inherited, owner.id: properties}),
    )


@pytest.mark.parametrize("selected", ["1.20", "1.21"])
def test_value_range_lists_selected_and_other_versions_with_equal_limits(selected):
    value = inputs()
    wide = value.schemas["1.20"]
    assert isinstance(wide, EdSchema)
    narrow = schema_property_changed(
        wide, "ВнешнийКод", type_ref=QName(wide.base_namespace, "Строка50")
    )
    plan = value.routes.plans[0]
    extra = replace(plan.entries[0], key="1.22", key_raw="1.22")
    value = refreshed(
        value,
        schemas={"1.20": wide, "1.21": narrow, "1.22": narrow},
        routes=replace(value.routes, plans=(replace(plan, entries=(*plan.entries, extra)),)),
    )
    assert OPERATION.new_attribute is not None
    op = replace(
        OPERATION,
        target=replace(TARGET, format_version=selected),
        format_property="ВнешнийКод",
        new_attribute=replace(OPERATION.new_attribute, qualifiers={"string_length": 0}),
    )
    prepared = prepare_authoring(value, (op,), IDENTITY, version_scope="manager")
    notices = [n for n in prepared.notices if n.id == "ed.author.value_range"]
    assert len(notices) == 1
    assert (notices[0].version_keys, notices[0].message) == (
        ("1.21", "1.22"),
        "В версиях 1.21, 1.22: длина источника=unbounded, приёмника=50",
    )
    assert prepared.selected_profiles[0].delta.no_new_issues


def test_value_ranges_with_different_limits_have_distinct_stable_ids():
    value = inputs()
    schema = value.schemas["1.20"]
    assert isinstance(schema, EdSchema)
    narrow = schema_property_changed(
        schema, "ВнешнийКод", type_ref=QName(schema.base_namespace, "Строка50")
    )
    limited = narrow.types[QName(narrow.base_namespace, "Строка50")]
    limited = replace(limited, facets=tuple(replace(f, lexical="25") for f in limited.facets))
    narrower = replace(
        narrow,
        types=MappingProxyType({**narrow.types, limited.qname: limited}),
        by_id=MappingProxyType({**narrow.by_id, limited.id: limited}),
    )
    plan = value.routes.plans[0]
    extra = replace(plan.entries[0], key="1.22", key_raw="1.22")
    value = refreshed(
        value,
        schemas={**value.schemas, "1.21": narrow, "1.22": narrower},
        routes=replace(value.routes, plans=(replace(plan, entries=(*plan.entries, extra)),)),
    )
    assert OPERATION.new_attribute is not None
    op = replace(
        OPERATION,
        format_property="ВнешнийКод",
        new_attribute=replace(OPERATION.new_attribute, qualifiers={"string_length": 0}),
    )
    first = prepare_authoring(value, (op,), IDENTITY, version_scope="manager")
    second = prepare_authoring(value, (op,), IDENTITY, version_scope="manager")
    ranges = [n for n in first.notices if n.id == "ed.author.value_range"]
    assert len(ranges) == len({n.notice_id for n in ranges}) == 2
    assert {n.version_keys for n in ranges} == {("1.21",), ("1.22",)}
    assert [n.notice_id for n in first.notices] == [n.notice_id for n in second.notices]


def test_other_notice_distinguishes_absence_and_type_mismatch_by_key():
    value = inputs()
    schema = value.schemas["1.20"]
    assert isinstance(schema, EdSchema)
    missing = schema_property_changed(schema, "ВнешнийКод", remove=True)
    wrong = schema_property_changed(schema, "ВнешнийКод", type_ref=QName(XS, "boolean"))
    plan = value.routes.plans[0]
    extra = replace(plan.entries[0], key="1.22", key_raw="1.22")
    value = refreshed(
        value,
        schemas={"1.20": schema, "1.21": missing, "1.22": wrong},
        routes=replace(value.routes, plans=(replace(plan, entries=(*plan.entries, extra)),)),
    )
    op = replace(OPERATION, format_property="ВнешнийКод")
    prepared = prepare_authoring(value, (op,), IDENTITY, version_scope="manager")
    notice = next(n for n in prepared.notices if n.id == "ed.author.other_version_incompatible")
    assert notice.version_keys == ("1.21", "1.22")
    assert notice.message == (
        "В версиях 1.21 свойства «ВнешнийКод» или типа ПКО нет: значение не передаётся; "
        "В версиях 1.22 тип свойства не совпадает: передача прямой ПКС несовместима со схемой"
    )


def test_preparation_reuses_indices_profiles_and_does_not_rehash_inputs(monkeypatch):
    import kd2_rules_mcp.authoring.ed.context as context_module
    import kd2_rules_mcp.validation.ed_authoring as checking
    from kd2_rules_mcp.ed.schema.profile import Applicability, ValidationProfile

    value = inputs()
    counts = {"addresses": 0, "references": 0, "profile": 0, "applicability": 0}

    def addresses(document):
        counts["addresses"] += 1
        return original_addresses(document)

    def references(document):
        counts["references"] += 1
        return original_references(document)

    def profile(cls, *args, **kwargs):
        counts["profile"] += 1
        return original_profile(*args, **kwargs)

    def applicability(cls, *args, **kwargs):
        counts["applicability"] += 1
        return original_applicability(*args, **kwargs)

    def unexpected_hash(*args, **kwargs):
        raise AssertionError("Подготовка не должна пересчитывать отпечатки")

    original_addresses, original_references = (
        context_module.build_addresses,
        checking.build_references,
    )
    original_profile, original_applicability = ValidationProfile.build, Applicability.build
    monkeypatch.setattr(context_module, "build_addresses", addresses)
    monkeypatch.setattr(checking, "build_references", references)
    monkeypatch.setattr(ValidationProfile, "build", classmethod(profile))
    monkeypatch.setattr(Applicability, "build", classmethod(applicability))
    monkeypatch.setattr(SourceSet, "build", unexpected_hash)
    prepared = prepare_authoring(value, (OPERATION,), IDENTITY, version_scope="manager")
    assert counts == {"addresses": 2, "references": 2, "profile": 2, "applicability": 4}
    assert prepared.source_set is value.input_fingerprints


def test_cached_checkers_equal_original_reports_and_keep_global_dependencies():
    from kd2_rules_mcp.ed.address import build_addresses
    from kd2_rules_mcp.ed.refs import build_references
    from kd2_rules_mcp.ed.schema.profile import ValidationProfile
    from kd2_rules_mcp.validation.ed_links import validate_links
    from kd2_rules_mcp.validation.ed_schema import validate_schema
    from kd2_rules_mcp.validation.ed_structure import validate_structure
    from kd2_rules_mcp.validation.ed_structure_snapshot import CheckContext

    value = inputs()
    prepared = prepare_authoring(value, (OPERATION,), IDENTITY, version_scope="manager")
    for comparison in (*prepared.selected_profiles, *prepared.other_profiles):
        for document, snapshot, actual in [
            (value.document, value.structure, comparison.before),
            (prepared.projection_after.document, prepared.structure_after, comparison.after),
        ]:
            schema = value.schemas[actual.version]
            assert isinstance(schema, EdSchema)
            index = build_addresses(document)
            profile = ValidationProfile.build(schema, actual.version, actual.direction)
            expected = validate_links(document, index, build_references(document))
            expected.extend(validate_schema(document, schema, index, profile, snapshot))
            expected.extend(validate_structure(document, snapshot, index, profile))
            assert (actual.issues, actual.skipped) == (
                tuple(expected.issues),
                tuple(expected.skipped),
            )
    assert validate_schema.__globals__["CheckContext"] is CheckContext
    assert validate_structure.__globals__["CheckContext"] is CheckContext
