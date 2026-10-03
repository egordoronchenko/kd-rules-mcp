"""Модель, все предусловия §2.3, кандидаты и неизменяемая структура автора."""

import hashlib
import sqlite3
from dataclasses import FrozenInstanceError, fields, replace
from pathlib import Path
from typing import Any, cast

import pytest

from kd2_rules_mcp.authoring.ed.candidates import candidates, compatibility, target_objects
from kd2_rules_mcp.authoring.ed.model import (
    FILLER,
    AddHeaderProperty,
    AttributeDraft,
    AuthoringInputs,
    AuthoringPreconditionError,
    AuthoringTarget,
    ExtensionIdentity,
    MetadataProfile,
    SourceSet,
    digest,
    order_operations,
    runtime_verified,
)
from kd2_rules_mcp.authoring.ed.operations import (
    copy_structure_with_attributes,
    draft_property,
    validate_owned_content,
    validate_preconditions,
)
from kd2_rules_mcp.ed import read_manager_text
from kd2_rules_mcp.ed.model import Expr, Field, UnknownFragment
from kd2_rules_mcp.ed.route_model import (
    ManagerInfo,
    PlanRoute,
    RegistrationProfile,
    RouteEntry,
    RouteProfile,
    RouteReading,
    RouteSource,
)
from kd2_rules_mcp.ed.schema import load_schema
from kd2_rules_mcp.structures import db, md83exp
from kd2_rules_mcp.validation.ed_structure_snapshot import StructureSnapshot

DATA = Path(__file__).parent / "data/ed/authoring"
IDENTITY = ExtensionIdentity("ДоработкаОбмена", "доп_")
TARGET = AuthoringTarget("Пример", "Main", "ПланФормата", None, "1.20", "send", "ПКО/Товар")
OPERATION = AddHeaderProperty(
    TARGET,
    "доп_Заметка",
    "Комментарий",
    AttributeDraft("доп_Заметка", "Заметка", "string", {"string_length": 150}),
)


def inputs(version=2, *, text=None):
    path = f"fiction/CommonModules/Менеджер{version}/Ext/Module.bsl"
    doc = read_manager_text(
        text or (DATA / f"base/manager-v{version}.bsl").read_text("utf-8"), path=path
    )
    schemas = {
        key: load_schema(
            DATA / f"base/format-{key}.bin",
            locate_import=lambda uri: (
                DATA / "base/common.bin" if uri == "urn:fiction:common" else None
            ),
        )
        for key in ("1.20", "1.21")
    }
    with sqlite3.connect(":memory:") as connection:
        connection.executescript(db.SCHEMA)
        md83exp.load(DATA / "base/structure.xml", connection)
        structure = StructureSnapshot.load(connection)
    source = RouteSource("ExchangePlans/ПланФормата/Ext/ManagerModule.bsl", 1, 1, "Настройки")
    entries = tuple(RouteEntry(key, key, f"Менеджер{version}", source) for key in schemas)
    plan = PlanRoute(
        "ПланФормата",
        "ExchangePlans/ПланФормата.xml",
        True,
        "urn:fiction:1.20",
        source,
        entries,
        RegistrationProfile("none"),
        (),
        (),
        "complete",
    )
    reading = RouteReading(0, 0, 0, 0, 0, 0, 0, 0, 0, 0, (), 0, 0, 0, 0, 0)
    routes = RouteProfile(
        "fiction",
        "fiction",
        "Пример",
        "Main",
        "Вымышленная",
        "routes-hash",
        (plan, replace(plan, plan_name="ВторойПлан")),
        entries,
        (),
        (),
        (
            ManagerInfo(
                f"Менеджер{version}", True, True, path, version, "declared", ("send", "receive")
            ),
            ManagerInfo("ИнойМенеджер", True, True, "fiction/Other.bsl", 2, "declared", ("send",)),
        ),
        (),
        "complete",
        reading,
    )
    return AuthoringInputs(
        doc, schemas, structure, routes, SourceSet.build(doc, schemas, structure, routes)
    )


def refreshed(value, **changes):
    value = replace(value, **changes)
    fingerprints = SourceSet.build(
        value.document,
        value.schemas,
        value.structure,
        value.routes,
        value.extension_sources,
        value.source_set.extensions,
    )
    return replace(value, source_set=fingerprints, input_fingerprints=fingerprints)


def rule_changed(value, **changes):
    return refreshed(
        value,
        document=replace(
            value.document, pko=(replace(value.document.pko[0], **changes), *value.document.pko[1:])
        ),
    )


def violation(name, value, op):
    doc = value.document
    rule = doc.pko[0]
    scope = "manager"
    identity = IDENTITY
    if name == "pko_missing":
        op = replace(op, target=replace(op.target, pko_address="ПКО/Нет"))
    elif name == "pko_ambiguous":
        cloned = replace(rule, entity_id="duplicate")
        use = replace(doc.rule_uses[0], entity_id="duplicate-use", rule_id="duplicate")
        value = refreshed(
            value, document=replace(doc, pko=(*doc.pko, cloned), rule_uses=(*doc.rule_uses, use))
        )
    elif name == "snapshot_mismatch":
        value = replace(value, source_set=replace(value.source_set, structure_hash="changed"))
    elif name == "route_unresolved":
        plan = value.routes.plans[0]
        value = refreshed(
            value,
            routes=replace(
                value.routes,
                plans=(
                    replace(
                        plan,
                        entries=(replace(plan.entries[0], state="conditional"), *plan.entries[1:]),
                    ),
                    *value.routes.plans[1:],
                ),
            ),
        )
    elif name == "scope_required":
        scope = None
    elif name == "pko_inactive":
        value = refreshed(
            value,
            document=replace(
                doc,
                rule_uses=tuple(
                    replace(u, direction="receive") if u.rule_id == rule.entity_id else u
                    for u in doc.rule_uses
                ),
            ),
        )
    elif name == "pko_removed":
        value = rule_changed(value, format_object=replace(rule.format_object, value="НетТипа"))
    elif name == "applicability_unknown":
        value = rule_changed(value, format_object=replace(rule.format_object, presence="ambiguous"))
    elif name == "schema_property_missing":
        op = replace(op, format_property="Нет")
    elif name == "format_property_occupied":
        op = replace(op, format_property="Код")
    elif name == "configuration_attribute_missing":
        op = replace(op, new_attribute=None)
    elif name == "configuration_attribute_occupied":
        op = replace(op, configuration_attribute="Код", new_attribute=None)
    elif name == "type_incompatible":
        op = replace(
            op,
            format_property="Дата",
            new_attribute=replace(
                op.new_attribute,
                primitive="number",
                qualifiers={
                    "number_length": 10,
                    "number_precision": 2,
                    "number_nonnegative": False,
                },
            ),
        )
    elif name == "conversion_required":
        op = replace(op, format_property="Повторы")
    elif name == "header_ineffective":
        value = rule_changed(value, group_flag=Field("literal", True))
    elif name == "target_partial":
        unknown = UnknownFragment(
            entity_id="unknown",
            kind="unknown",
            name="unknown",
            span=rule.span,
            raw_text="ИзменитьКакУгодно(Правило.Свойства);",
            owner_id=rule.entity_id,
            reason="unknown",
        )
        value = refreshed(value, document=replace(doc, unknown=(unknown,)))
    elif name == "helper_unverified":
        value = refreshed(
            value,
            document=replace(
                doc, routines=tuple(r for r in doc.routines if r.name != "ДобавитьПКС")
            ),
        )
    elif name == "manager_signature":
        value = refreshed(value, document=replace(doc, manager_version=4))
    elif name == "metadata_profile_unsupported":
        value = replace(value, metadata_profile=MetadataProfile(dump_version="2.99"))
    elif name == "extension_conflict":
        value = refreshed(
            value,
            extension_sources={
                "CommonModules/Менеджер2/Ext/Module.bsl": (
                    DATA / "cases/foreign-hook.bsl"
                ).read_text("utf-8")
            },
        )
    elif name == "identifier_conflict":
        routine = replace(doc.routines[0], entity_id="collision", name="доп_" + FILLER)
        value = refreshed(value, document=replace(doc, routines=(*doc.routines, routine)))
    return value, op, identity, scope


@pytest.mark.parametrize(
    "case,expected_id,address,message",
    [
        ("pko_missing", "ed.author.pko_missing", "ПКО/Нет", "ПКО «ПКО/Нет» отсутствует"),
        ("pko_ambiguous", "ed.author.pko_ambiguous", "ПКО/Товар", "Поиск «Товар» неоднозначен"),
        (
            "snapshot_mismatch",
            "ed.author.snapshot_mismatch",
            "ПКО/Товар",
            "Снимки ED, схемы, структуры и маршрутов не совпадают с SourceSet",
        ),
        (
            "route_unresolved",
            "ed.author.route_unresolved",
            "ПКО/Товар",
            "Маршрут ED не разрешён однозначно на прочитанный менеджер",
        ),
        (
            "scope_required",
            "ed.author.scope_required",
            "ПКО/Товар",
            "Требуется явная область действия version_scope=manager",
        ),
        (
            "pko_inactive",
            "ed.author.pko_inactive",
            "ПКО/Товар",
            "ПКО не применим в выбранном направлении и версии",
        ),
        (
            "pko_removed",
            "ed.author.pko_removed",
            "ПКО/Товар",
            "Тип «НетТипа» отсутствует; исполнитель удалит ПКО",
        ),
        (
            "applicability_unknown",
            "ed.author.applicability_unknown",
            "ПКО/Товар",
            "Применимость ПКО, его полей или новой ПКС не доказана",
        ),
        (
            "schema_property_missing",
            "ed.author.schema_property_missing",
            "ПКО/Товар",
            "Свойство формата «Нет» не разрешено однозначно",
        ),
        (
            "format_property_occupied",
            "ed.author.format_property_occupied",
            "ПКО/Товар",
            "Свойство формата «Код» уже занято",
        ),
        (
            "configuration_attribute_missing",
            "ed.author.configuration_attribute_missing",
            "ПКО/Товар",
            "Обычный записываемый реквизит «доп_Заметка» отсутствует",
        ),
        (
            "configuration_attribute_occupied",
            "ed.author.configuration_attribute_occupied",
            "ПКО/Товар",
            "Реквизит «Код» уже занят",
        ),
        (
            "type_incompatible",
            "ed.author.type_incompatible",
            "ПКО/Товар",
            "Примитивные семьи реквизита и свойства различаются",
        ),
        (
            "conversion_required",
            "ed.author.conversion_required",
            "ПКО/Товар",
            "Требуется конвертация: не одиночный подтверждённый примитив",
        ),
        (
            "header_ineffective",
            "ed.author.header_ineffective",
            "ПКО/Товар",
            "Владелец не обычный объект с применимой шапкой",
        ),
        (
            "target_partial",
            "ed.author.target_partial",
            "ПКО/Товар",
            "Unknown влияет на целевой ПКО, helper или путь заполнения",
        ),
        (
            "helper_unverified",
            "ed.author.helper_unverified",
            "ПКО/Товар",
            "Семантика ДобавитьПКС не подтверждена эталоном",
        ),
        (
            "manager_signature",
            "ed.author.manager_signature",
            "ПКО/Товар",
            "Сигнатура заполнителя не соответствует интерфейсу 1/2/3",
        ),
        (
            "metadata_profile_unsupported",
            "ed.author.metadata_profile_unsupported",
            "ПКО/Товар",
            "Профиль метаданных или нового реквизита не поддержан",
        ),
        (
            "extension_conflict",
            "ed.author.extension_conflict",
            "ПКО/Товар",
            "Чужое расширение влияет на операцию: ЗаполнитьПравилаКонвертацииОбъектов",
        ),
        (
            "identifier_conflict",
            "ed.author.identifier_conflict",
            "ПКО/Товар",
            "Имя перехватчика «доп_ЗаполнитьПравилаКонвертацииОбъектов» занято",
        ),
    ],
)
def test_precondition_bad_and_clean(case, expected_id, address, message):
    base = inputs()
    value, op, identity, scope = violation(case, base, OPERATION)
    with pytest.raises(AuthoringPreconditionError) as caught:
        validate_preconditions(value, (op,), identity, version_scope=scope)
    assert caught.value.code == "ed_authoring_precondition"
    matching = [f for f in caught.value.failures if f.id == expected_id]
    assert len(matching) == 1
    assert (matching[0].address, matching[0].message) == (address, message)
    assert validate_preconditions(base, (OPERATION,), IDENTITY, version_scope="manager") == ()


def test_owned_content_bad_and_clean():
    text = "Проверенный текст\n"
    expected = {"Module.bsl": hashlib.sha256(text.encode()).hexdigest()}
    validate_owned_content(expected, {"Module.bsl": text}, "ПКО/Товар")
    for actual in ({"Module.bsl": text + "// Правка"}, {"Module.bsl": text, "extra": ""}, {}):
        with pytest.raises(AuthoringPreconditionError) as caught:
            validate_owned_content(expected, actual, "ПКО/Товар")
        failure = caught.value.failures[0]
        assert (failure.id, failure.address, failure.message) == (
            "ed.author.owned_content_changed",
            "ПКО/Товар",
            "Содержимое прежнего результата изменено или содержит неизвестные файлы",
        )


def test_model_normalized_immutable_ids_and_runtime():
    assert OPERATION.new_attribute is not None
    assert len(OPERATION.operation_id) == 64
    value = inputs()
    reordered = dict(reversed(tuple(value.schemas.items())))
    assert (
        SourceSet.build(value.document, reordered, value.structure, value.routes)
        == value.source_set
    )
    assert (
        OPERATION.operation_id
        != replace(
            OPERATION,
            configuration_attribute="ДОП_ЗАМЕТКА",
            new_attribute=replace(OPERATION.new_attribute, name="ДОП_ЗАМЕТКА"),
        ).operation_id
    )
    assert (
        OPERATION.operation_id
        != replace(OPERATION, target=replace(TARGET, direction="receive")).operation_id
    )
    assert order_operations((OPERATION, OPERATION)) == (OPERATION,)
    assert order_operations((OPERATION,))[0].operation_id == OPERATION.operation_id
    with pytest.raises(FrozenInstanceError):
        cast(Any, OPERATION).format_property = "Нет"
    with pytest.raises(TypeError):
        cast(Any, OPERATION.new_attribute.qualifiers)["string_length"] = 5
    assert "missing_value_policy" not in {f.name for f in fields(AddHeaderProperty)}
    assert not any(runtime_verified(v) for v in (None, 1, 2, 3, 4))


@pytest.mark.parametrize("annotation", ["Перед", "После", "Вместо", "ИзменениеИКонтроль"])
@pytest.mark.parametrize("procedure", [FILLER, "ДобавитьПКС", "ДобавитьПКО_Товар"])
def test_extension_intercepts_all_three_targets(annotation, procedure):
    value = inputs()
    file = "foreign/CommonModules/Менеджер2/Ext/Module.bsl"
    value = refreshed(
        value,
        extension_sources={
            file: f'&{annotation}("{procedure}")\nПроцедура Чужая()\nКонецПроцедуры\n'
        },
    )
    with pytest.raises(AuthoringPreconditionError) as caught:
        validate_preconditions(value, (OPERATION,), IDENTITY, version_scope="manager")
    failure = next(f for f in caught.value.failures if f.id == "ed.author.extension_conflict")
    assert (failure.address, failure.message, failure.file, failure.line) == (
        "ПКО/Товар",
        f"Чужое расширение влияет на операцию: {procedure}",
        file,
        1,
    )


@pytest.mark.parametrize("annotation", ["НаСервере", "НаКлиенте", 'Перед("Посторонняя")'])
def test_extension_non_intercepts_are_clean(annotation):
    value = refreshed(
        inputs(),
        extension_sources={
            "foreign/CommonModules/Менеджер2/Ext/Module.bsl": (
                f"&{annotation}\nПроцедура Чужая()\nКонецПроцедуры\n"
            )
        },
    )
    validate_preconditions(value, (OPERATION,), IDENTITY, version_scope="manager")


@pytest.mark.parametrize(
    "xml,reason",
    [
        ("<bad", "Описание расширения не является корректным XML"),
        (
            '<!DOCTYPE x SYSTEM "http://invalid.example/dtd"><x/>',
            "DTD и сущности в описании расширения запрещены",
        ),
        (
            '<!DOCTYPE x [<!ENTITY secret SYSTEM "file:///unavailable">]><x>&secret;</x>',
            "DTD и сущности в описании расширения запрещены",
        ),
    ],
)
def test_extension_xml_is_safe_and_refuses_invalid_input(xml, reason):
    file = "foreign/Catalogs/Товары.xml"
    value = refreshed(inputs(), extension_sources={file: xml})
    with pytest.raises(AuthoringPreconditionError) as caught:
        validate_preconditions(value, (OPERATION,), IDENTITY, version_scope="manager")
    assert [(f.id, f.address, f.message, f.file, f.line) for f in caught.value.failures] == [
        (
            "ed.author.extension_conflict",
            "ПКО/Товар",
            f"Чужое расширение влияет на операцию: {reason}",
            file,
            1,
        )
    ]


@pytest.mark.parametrize("encoding", ["utf-8", "windows-1251"])
def test_extension_xml_received_as_text_uses_text_encoding(encoding):
    text = f'<?xml version="1.0" encoding="{encoding}"?>\n' + (
        DATA / "cases/foreign-attribute.xml"
    ).read_text("utf-8")
    file = "foreign/Catalogs/Товары.xml"
    value = refreshed(inputs(), extension_sources={file: text})
    with pytest.raises(AuthoringPreconditionError) as caught:
        validate_preconditions(value, (OPERATION,), IDENTITY, version_scope="manager")
    assert [(f.id, f.address, f.message, f.file, f.line) for f in caught.value.failures] == [
        (
            "ed.author.extension_conflict",
            "ПКО/Товар",
            "Чужое расширение влияет на операцию: доп_Заметка",
            file,
            4,
        )
    ]


@pytest.mark.parametrize(
    "primitive,qualifiers,property_name,keys",
    [
        ("string", {"Length": 150}, "Комментарий", ("Length", "string_length")),
        ("string", {}, "Комментарий", ("string_length",)),
        ("string", {"string_length": "150"}, "Комментарий", ("string_length",)),
        ("string", {"string_length": True}, "Комментарий", ("string_length",)),
        ("date", {}, "Дата", ("date_parts",)),
        ("date", {}, "День", ("date_parts",)),
        ("date", {"date_parts": 0}, "Дата", ("date_parts",)),
        ("boolean", {"string_length": 150}, "Булево", ("string_length",)),
        ("number", {"number_length": 10, "number_precision": 2}, "Число", ("number_nonnegative",)),
        (
            "number",
            {"number_length": True, "number_precision": 2, "number_nonnegative": False},
            "Число",
            ("number_length",),
        ),
        (
            "number",
            {"number_length": 10, "number_precision": 2, "number_nonnegative": "false"},
            "Число",
            ("number_nonnegative",),
        ),
    ],
)
def test_draft_qualifiers_are_closed_explicit_and_typed(primitive, qualifiers, property_name, keys):
    assert OPERATION.new_attribute is not None
    op = replace(
        OPERATION,
        format_property=property_name,
        new_attribute=replace(OPERATION.new_attribute, primitive=primitive, qualifiers=qualifiers),
    )
    with pytest.raises(AuthoringPreconditionError) as caught:
        validate_preconditions(inputs(), (op,), IDENTITY, version_scope="manager")
    assert [(f.id, f.address, f.message) for f in caught.value.failures] == [
        (
            "ed.author.metadata_profile_unsupported",
            "ПКО/Товар",
            f"Квалификатор «{key}» нового реквизита не поддержан, "
            "отсутствует или имеет неверный тип",
        )
        for key in keys
    ]


def test_reference_to_primitive_is_type_incompatible():
    value = inputs()
    owner = value.structure.objects[("справочник", "товары")]
    prop = replace(owner.property("Заметка")[0], types=("СправочникСсылка.Товары",))
    owner = replace(owner, properties={**owner.properties, ("заметка", ""): (prop,)})
    value = refreshed(
        value,
        structure=StructureSnapshot(
            {**value.structure.objects, ("справочник", "товары"): owner}, value.structure.by_type
        ),
    )
    op = replace(OPERATION, configuration_attribute="Заметка", new_attribute=None)
    with pytest.raises(AuthoringPreconditionError) as caught:
        validate_preconditions(value, (op,), IDENTITY, version_scope="manager")
    assert [(f.id, f.address, f.message) for f in caught.value.failures] == [
        (
            "ed.author.type_incompatible",
            "ПКО/Товар",
            "Тип реквизита и примитив свойства различаются",
        )
    ]


def test_snapshot_copy_negative_ids_both_owners_and_no_database_write():
    value = inputs()
    before = digest(value.structure)
    order = replace(OPERATION, target=replace(TARGET, pko_address="ПКО/Заказ"))
    snapshot = copy_structure_with_attributes(value, (order, OPERATION))
    assert digest(value.structure) == before
    assert snapshot is not value.structure
    assert snapshot.objects[("справочник", "товары")].property("доп_Заметка")[0].id == -2
    assert snapshot.objects[("документ", "заказ")].property("доп_Заметка")[0].id == -1
    assert (
        snapshot.objects[("справочник", "товары")]
        .property("доп_Заметка")[0]
        .qualifiers["string_length"]
        == 150
    )
    existing = replace(OPERATION, configuration_attribute="Заметка", new_attribute=None)
    assert digest(copy_structure_with_attributes(value, (existing,))) == before


def test_candidates_order_filter_no_auto_and_unavailable_pairs():
    value = inputs()
    result = candidates(value, TARGET, "format")
    assert result == tuple(sorted(result, key=lambda c: (c.name, c.path)))
    assert all(c.auto is False and c.compatible is None for c in result)
    assert "Код" not in [c.name for c in result]
    assert "Повторы" not in [c.name for c in result]
    note = candidates(value, TARGET, "format", text="КОММЕНТ", configuration_attribute="Заметка")
    assert [(c.path, c.compatible) for c in note] == [
        ("ОбщиеСвойстваОбъектовФормата.Комментарий", True)
    ]
    attrs = candidates(value, TARGET, "configuration", format_property="Комментарий")
    assert [(c.name, c.compatible) for c in attrs] == [("Заметка", True)]
    missing_attribute = candidates(
        value, TARGET, "format", text="Комментарий", configuration_attribute="Нет"
    )
    assert missing_attribute[0].compatible is False
    assert missing_attribute[0].reason == "Реквизит конфигурации отсутствует или неоднозначен"
    missing_property = candidates(value, TARGET, "configuration", format_property="Нет")
    assert missing_property[0].compatible is False
    assert missing_property[0].reason == "Свойство формата отсутствует или неоднозначно"
    limited_string = candidates(value, TARGET, "format", text="Короткий")[0]
    assert limited_string.qualifiers == {"maxLength": "50"}


def test_two_versions_cannot_add_two_attributes_to_the_same_hook_property():
    value = inputs()
    assert OPERATION.new_attribute is not None
    first = replace(OPERATION, format_property="ВнешнийКод")
    second = replace(
        first,
        target=replace(TARGET, format_version="1.21"),
        configuration_attribute="доп_ДругойКод",
        new_attribute=replace(OPERATION.new_attribute, name="доп_ДругойКод"),
    )
    with pytest.raises(AuthoringPreconditionError) as caught:
        validate_preconditions(value, (first, second), IDENTITY, version_scope="manager")
    assert "ed.author.scope_required" in [f.id for f in caught.value.failures]
    assert caught.value.failures[0].address == "ПКО/Товар"
    assert caught.value.failures[0].message == (
        "операции одного менеджера проверяются по одной выбранной версии; "
        "остальные версии показываются как другие"
    )


@pytest.mark.parametrize(
    "primitive,qualifiers,prop,expected",
    [
        ("string", {"string_length": -1}, "Комментарий", "ed.author.metadata_profile_unsupported"),
        ("date", {"date_parts": "Дата"}, "Время", "ed.author.type_incompatible"),
        (
            "number",
            {"number_length": 2, "number_precision": 2, "number_nonnegative": False},
            "Число",
            "ed.author.metadata_profile_unsupported",
        ),
    ],
)
def test_unknown_qualifiers_and_date_parts_are_not_assumed_compatible(
    primitive, qualifiers, prop, expected
):
    value = inputs()
    assert OPERATION.new_attribute is not None
    op = replace(
        OPERATION,
        format_property=prop,
        new_attribute=replace(OPERATION.new_attribute, primitive=primitive, qualifiers=qualifiers),
    )
    with pytest.raises(AuthoringPreconditionError) as caught:
        validate_preconditions(value, (op,), IDENTITY, version_scope="manager")
    assert [f.id for f in caught.value.failures] == [expected]


@pytest.mark.parametrize(
    "primitive,qualifiers,prop",
    [
        ("string", {"string_length": 150}, "Комментарий"),
        ("boolean", {}, "Булево"),
        (
            "number",
            {"number_length": 10, "number_precision": 2, "number_nonnegative": False},
            "Число",
        ),
        ("date", {"date_parts": "ДатаВремя"}, "Дата"),
        ("date", {"date_parts": "Дата"}, "День"),
        ("date", {"date_parts": "Время"}, "Время"),
    ],
)
@pytest.mark.parametrize("direction", ["send", "receive"])
def test_primitive_families_and_qualifiers(primitive, qualifiers, prop, direction):
    assert OPERATION.new_attribute is not None
    value = inputs()
    op = replace(
        OPERATION,
        target=replace(TARGET, direction=direction),
        format_property=prop,
        new_attribute=replace(OPERATION.new_attribute, primitive=primitive, qualifiers=qualifiers),
    )
    validate_preconditions(value, (op,), IDENTITY, version_scope="manager")
    _, profile, _, typ, _ = target_objects(value, op.target)
    assert typ is not None and op.new_attribute is not None
    resolved = profile.resolve(typ, prop)
    checked = compatibility(
        profile,
        profile.properties[resolved.property_ids[0]],
        draft_property(op.new_attribute),
        direction,
    )
    assert checked.compatible
    if direction == "receive" and primitive == "string":
        assert checked.value_range == "длина источника=unbounded, приёмника=150"


def test_alias_occupied_and_different_owner_free():
    value = inputs()
    rule = value.document.pko[0]
    alias = replace(rule.properties[0], format_property="ОбщиеСвойстваОбъектовФормата.Комментарий")
    value = rule_changed(value, properties=(alias,))
    with pytest.raises(AuthoringPreconditionError) as caught:
        validate_preconditions(value, (OPERATION,), IDENTITY, version_scope="manager")
    assert any(f.id == "ed.author.format_property_occupied" for f in caught.value.failures)
    order = replace(OPERATION, target=replace(TARGET, pko_address="ПКО/Заказ"))
    validate_preconditions(value, (order,), IDENTITY, version_scope="manager")


def test_unknown_unrelated_allowed_target_and_helper_block():
    value = inputs()
    base = (DATA / "base/manager-v2.bsl").read_text("utf-8")
    unrelated = inputs(
        text=base + "\nПроцедура Посторонняя()\n ВыполнитьЧтоУгодно();\nКонецПроцедуры\n"
    )
    assert unrelated.document.parse_status == "partial"
    validate_preconditions(unrelated, (OPERATION,), IDENTITY, version_scope="manager")
    bad = inputs(
        text=base.replace(
            "НоваяСтрока.СвойствоФормата = СвойствоФормата;", "НоваяСтрока.Иное = СвойствоФормата;"
        )
    )
    with pytest.raises(AuthoringPreconditionError) as caught:
        validate_preconditions(bad, (OPERATION,), IDENTITY, version_scope="manager")
    assert {"ed.author.helper_unverified", "ed.author.target_partial"} <= {
        f.id for f in caught.value.failures
    }
    unknown = Expr("ФлагИзБазы", value.document.pko[0].span)
    conditional = replace(value.document.pko[0].properties[0], condition_name=unknown.raw)
    with pytest.raises(AuthoringPreconditionError) as caught:
        validate_preconditions(
            rule_changed(value, properties=(conditional,)),
            (OPERATION,),
            IDENTITY,
            version_scope="manager",
        )
    assert any(f.id == "ed.author.applicability_unknown" for f in caught.value.failures)


def test_other_pko_unknown_may_taint_the_shared_collection():
    base = (DATA / "base/manager-v2.bsl").read_text("utf-8")
    prefix = "Процедура ДобавитьПКО_Заказ(ПравилаКонвертации)\n"
    contaminated = inputs(
        text=base.replace(prefix, prefix + "\tИзменитьКакУгодно(ПравилаКонвертации);\n")
    )
    with pytest.raises(AuthoringPreconditionError) as caught:
        validate_preconditions(contaminated, (OPERATION,), IDENTITY, version_scope="manager")
    assert any(f.id == "ed.author.target_partial" for f in caught.value.failures)
    local = inputs(
        text=base.replace(
            'ПравилоКонвертации.ИмяПКО = "Заказ";',
            'ПравилоКонвертации.ИмяПКО = "Заказ";\n\tПостороннее(ПравилоКонвертации.Свойства);',
        )
    )
    validate_preconditions(local, (OPERATION,), IDENTITY, version_scope="manager")


@pytest.mark.parametrize("path", ["cases/foreign-hook.bsl", "cases/foreign-attribute.xml"])
def test_narrow_extension_scanner(path):
    value = inputs()
    filename = (
        "CommonModules/Менеджер2/Ext/Module.bsl" if path.endswith("bsl") else "Catalogs/Товары.xml"
    )
    value = refreshed(value, extension_sources={filename: (DATA / path).read_text("utf-8")})
    with pytest.raises(AuthoringPreconditionError) as caught:
        validate_preconditions(value, (OPERATION,), IDENTITY, version_scope="manager")
    conflict = next(f for f in caught.value.failures if f.id == "ed.author.extension_conflict")
    assert conflict.file == filename and conflict.line > 0
    if path.endswith("bsl"):
        independent = refreshed(
            value,
            extension_sources={
                "CommonModules/Иной/Ext/Module.bsl": (DATA / path).read_text("utf-8")
            },
        )
        validate_preconditions(independent, (OPERATION,), IDENTITY, version_scope="manager")
