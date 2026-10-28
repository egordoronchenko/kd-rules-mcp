"""Каждое условие контракта: нарушение, чистый вход и явные адреса."""

from dataclasses import replace

import pytest

from kd_rules_mcp.authoring.ed.manager_operations import parse_operation, preview
from kd_rules_mcp.ed.format_model import FormatProperty, FormatType
from kd_rules_mcp.ed.schema import QName
from kd_rules_mcp.ed.schema.xdto import XS
from kd_rules_mcp.ed.writer_model import (
    Guard,
    Identification,
    ObjectRule,
    PredefinedRule,
    ProcessingRule,
    Property,
    PropertyGroup,
    Value,
)
from kd_rules_mcp.validation.ed_format import validate_format
from kd_rules_mcp.validation.ed_pair import validate_format_pair
from tests.data.ed.format_extension.model import OBJECT, URI, package_c
from tests.test_ed_format_model import manager, operation


def pks(key="field", *, namespace=URI, name="Состояние", guards=()):
    values = (Value("string", key), Value("string", name))
    presence = (True, True, True)
    if namespace:
        values += (Value(), Value(), Value("string", namespace))
        presence += (False, False, True)
    return Property(
        logical_id=key,
        name=key,
        configuration_property=key,
        format_property=name,
        namespace=namespace,
        guards=guards,
        argument_values=values,
        argument_presence=presence,
    )


def pko(*, name=OBJECT, properties=(), groups=(), extensions=(URI,), directions=("send",)):
    return ObjectRule(
        logical_id="pko",
        name="Задача",
        procedure_name="ДобавитьПКО_Задача",
        identification=Identification(logical_id="ident", name="Идентификация"),
        format_object=Value("string", name),
        directions=directions,
        properties=properties,
        groups=groups,
        extensions=extensions,
    )


def pod(name=OBJECT, *, directions=("receive",)):
    return ProcessingRule(
        logical_id="pod",
        name="Задачи",
        procedure_name="ДобавитьПОД_Задачи",
        format_selection=Value("string", name),
        directions=directions,
    )


def issues(model, schema=True):
    return {
        (i.check, i.address)
        for i in validate_format(model, package_c().base_schema if schema else None).issues
    }


@pytest.mark.parametrize(
    "kind,address",
    [
        ("header", "ПКО/Задача/ПКС/field"),
        ("group", "ПКО/Задача/ПКТЧ/Шаги"),
        ("row", "ПКО/Задача/ПКТЧ/Шаги/ПКС/field"),
    ],
)
def test_uri_unknown_and_init_missing_violation_and_clean(kind, address):
    def model(uri, extensions):
        field = pks(namespace=uri)
        group = PropertyGroup(
            logical_id="group",
            name="Шаги",
            configuration_property="Шаги",
            format_property="Шаги",
            namespace=uri if kind == "group" else "",
            properties=(field,) if kind == "row" else (),
        )
        return replace(
            manager(),
            pko=(
                pko(
                    properties=(field,) if kind == "header" else (),
                    groups=(group,) if kind != "header" else (),
                    extensions=extensions,
                ),
            ),
        )

    unknown = issues(model("urn:fiction:unknown", ()))
    assert ("ed.format.uri_unknown", address) in unknown
    assert ("ed.format.init_missing", address) not in unknown
    assert ("ed.format.init_missing", address) in issues(model(URI, ()))
    clean = issues(model(URI, (URI,)))
    assert ("ed.format.uri_unknown", address) not in clean
    assert ("ed.format.init_missing", address) not in clean


def test_init_type_missing_base_owner_and_schema_package():
    model = replace(manager(), pko=(pko(name="Object"),))
    assert ("ed.format.init_type_missing", "ПКО/Задача") in issues(model)
    package = model.format_packages[0]
    typ = FormatType(
        QName(URI, "Object"), "object", (FormatProperty(QName(URI, "Extra"), QName(XS, "string")),)
    )
    clean = replace(model, format_packages=(replace(package, types=(*package.types, typ)),))
    assert ("ed.format.init_type_missing", "ПКО/Задача") not in issues(clean)


def test_type_missing_pko_init_and_receive_pod_send_exemption():
    model = replace(manager(), pko=(pko(extensions=()),), pod=(pod("НетТипа"),))
    assert ("ed.format.type_missing", "ПКО/Задача") in issues(model)
    assert ("ed.format.type_missing", "ПОД/Задачи") in issues(model)
    clean = replace(model, pko=(pko(),), pod=(pod(),))
    assert ("ed.format.type_missing", "ПКО/Задача") not in issues(clean)
    assert ("ed.format.type_missing", "ПОД/Задачи") not in issues(clean)
    assert ("ed.format.type_missing", "ПОД/Задачи") not in issues(
        replace(model, pod=(pod("НетТипа", directions=("send",)),))
    )


@pytest.mark.parametrize("rows", [False, True])
def test_name_collision_directions_and_levels(rows):
    first = pks("first", namespace=URI)
    second = pks("second", namespace="", name="состояние")
    # Собственный тип ПКО разрешается в URI, поэтому второй URI задаём базовым.
    second = replace(second, namespace=package_c().base_namespace)
    group = PropertyGroup(
        logical_id="group",
        name="Шаги",
        configuration_property="Шаги",
        format_property="Шаги",
        properties=(first, second),
    )
    model = replace(
        manager(),
        pko=(
            pko(
                properties=() if rows else (first, second),
                groups=(group,) if rows else (),
                directions=("both",),
            ),
        ),
    )
    prefix = "ПКО/Задача/ПКТЧ/Шаги/ПКС/" if rows else "ПКО/Задача/ПКС/"
    expected = {
        ("ed.format.name_collision", prefix + "first"),
        ("ed.format.name_collision", prefix + "second"),
    }
    assert expected <= issues(model)
    clean_props = (first, replace(second, namespace=URI))
    clean = replace(
        model,
        pko=(
            replace(
                model.pko[0],
                properties=() if rows else clean_props,
                groups=(replace(group, properties=clean_props),) if rows else (),
            ),
        ),
    )
    assert not expected & issues(clean)
    guards = (
        Guard(
            logical_id="send",
            name="send",
            expression="",
            branch="if",
            guard_kind="direction",
            direction="send",
        ),
        Guard(
            logical_id="receive",
            name="receive",
            expression="",
            branch="if",
            guard_kind="direction",
            direction="receive",
        ),
    )
    props = (replace(first, guards=("send",)), replace(second, guards=("receive",)))
    disjoint = replace(
        model,
        guards=guards,
        pko=(
            replace(
                model.pko[0],
                properties=() if rows else props,
                groups=(replace(group, properties=props),) if rows else (),
            ),
        ),
    )
    assert not expected & issues(disjoint)


def test_receive_pod_and_deletion_route_violation_and_clean():
    model = replace(manager(), pko=(pko(directions=("both",)),))
    assert ("ed.format.pod_missing", "ПКО/Задача") in issues(model)
    assert ("ed.format.deletion_route_unresolved", "ПКО/Задача") in issues(model)
    clean = replace(model, pko=(pko(directions=("receive",)),), pod=(pod(),))
    assert ("ed.format.pod_missing", "ПКО/Задача") not in issues(clean)
    assert ("ed.format.deletion_route_unresolved", "ПКО/Задача") not in issues(clean)
    package = model.format_packages[0]
    types = tuple(
        replace(t, properties=tuple(p for p in t.properties if p.name.local != "Ссылка"))
        if t.role == "key"
        else t
        for t in package.types
    )
    assert ("ed.format.deletion_route_unresolved", "ПКО/Задача") not in issues(
        replace(model, format_packages=(replace(package, types=types),))
    )


def test_pkpd_unsupported_and_unknown_type_is_not_this_check():
    rule = PredefinedRule(
        logical_id="pkpd", name="Состояния", format_type=Value("string", "СостояниеЗадачи")
    )
    model = replace(manager(), pkpd=(rule,))
    assert ("ed.format.pkpd_unsupported", "ПКПД/Состояния") in issues(model)
    for name in ("Ref", "НетТипа"):
        assert ("ed.format.pkpd_unsupported", "ПКПД/Состояния") not in issues(
            replace(model, pkpd=(replace(rule, format_type=Value("string", name)),))
        )


def test_version_mismatch_and_clean():
    model = manager()
    expected = ("ed.format.version_mismatch", "Пакет/fmt_УчебныйПакетC")
    assert expected not in issues(model)
    assert expected in issues(
        replace(model, format_packages=(replace(model.format_packages[0], base_version="9.99"),))
    )
    assert expected in issues(
        replace(
            model,
            format_packages=(
                replace(model.format_packages[0], base_namespace="urn:fiction:other-base"),
            ),
        )
    )


def test_schema_unavailable_and_not_literal_are_explicit():
    model = replace(
        manager(),
        pko=(pko(properties=(pks(namespace="urn:unknown"),)),),
        pod=(pod(),),
        pkpd=(
            PredefinedRule(
                logical_id="pkpd", name="Состояния", format_type=Value("string", "СостояниеЗадачи")
            ),
        ),
    )
    report = validate_format(model, None)
    assert not report.errors

    # pod_missing неприменим: ПКО только отправки; пропуск давал бы ложное «не проверено».
    assert {
        "ed.format.type_missing",
        "ed.format.init_type_missing",
        "ed.format.deletion_route_unresolved",
        "ed.format.uri_unknown",
        "ed.format.pkpd_unsupported",
    } <= {s.check for s in report.skipped if s.reason.startswith("schema_unavailable")}
    assert "ed.format.pod_missing" not in {s.check for s in report.skipped}
    model = replace(
        model, pko=(replace(model.pko[0], format_object=Value("unknown", raw="ИмяТипа()")),)
    )
    report = validate_format(model, package_c().base_schema)
    assert {
        "ed.format.type_missing",
        "ed.format.init_type_missing",
        "ed.format.pod_missing",
        "ed.format.deletion_route_unresolved",
    } <= {s.check for s in report.skipped if s.reason.startswith("not_literal")}


def test_namespace_not_literal_is_explicit():
    prop = pks()
    prop = replace(prop, argument_values=(*prop.argument_values[:4], Value("unknown", raw="URI()")))
    model = replace(manager(), pko=(pko(properties=(prop,)),))
    report = validate_format(model, package_c().base_schema)
    assert {"ed.format.uri_unknown", "ed.format.init_missing", "ed.format.name_collision"} <= {
        s.check for s in report.skipped if s.reason == "not_literal: ПКО/Задача/ПКС/field"
    }
    assert not report.errors
    pair = validate_format_pair(model, manager(packages=False), package_c().base_schema)
    assert not pair.issues
    assert [(s.check, s.reason) for s in pair.skipped] == [
        ("ed.format.package_one_sided", "not_literal: ПКО/Задача/ПКС/field")
    ]


def test_consumers_unknown_pko_and_row_without_type_name_template():
    from kd_rules_mcp.authoring.ed.manager_operations import format_consumers
    from kd_rules_mcp.ed.writer_model import model_addresses

    group = PropertyGroup(
        logical_id="group",
        name="Шаги",
        configuration_property="Шаги",
        format_property="Шаги",
        properties=(pks(name="Текст"),),
    )
    model = replace(manager(), pko=(pko(groups=(group,)),))
    addresses = model_addresses(model)
    target = next(
        key
        for key, address in addresses.items()
        if address == "Пакет/fmt_УчебныйПакетC/Тип/УчебнаяСтрока/Свойство/Текст"
    )
    assert format_consumers(model, target) == ("ПКО/Задача/ПКТЧ/Шаги/ПКС/field",)
    unknown = replace(
        model, pko=(replace(model.pko[0], format_object=Value("unknown", raw="Тип()")),)
    )
    assert format_consumers(unknown, target) == ("ПКО/Задача/ПКТЧ/Шаги/ПКС/field",)


def test_pair_hash_mismatch_one_sided_and_clean():
    source = replace(manager(), pko=(pko(properties=(pks(),)),))
    target = manager()
    assert not validate_format_pair(source, target).issues
    target = replace(
        target,
        format_packages=(
            replace(target.format_packages[0], types=target.format_packages[0].types[:-1]),
        ),
    )
    report = validate_format_pair(source, target)
    assert {(i.check, i.address) for i in report.issues} == {
        ("ed.format.package_hash_mismatch", "Пакет/fmt_УчебныйПакетC")
    }
    report = validate_format_pair(source, manager(packages=False), package_c().base_schema)
    assert {(i.check, i.address) for i in report.issues} == {
        ("ed.format.package_one_sided", "ПКО/Задача/ПКС/field")
    }
    report = validate_format_pair(
        replace(source, format_packages=()), manager(packages=False), package_c().base_schema
    )
    assert {(i.check, i.address) for i in report.issues} == {
        ("ed.format.package_one_sided", "ПКО/Задача/ПКС/field")
    }
    report = validate_format_pair(source, None)
    assert {s.reason for s in report.skipped} == {"not_manager_documents"}
    assert not validate_format_pair(None, None).skipped


@pytest.mark.parametrize(
    "kind,address",
    [
        ("format_package", "Пакет/fmt_УчебныйПакетC"),
        ("format_type", "Пакет/fmt_УчебныйПакетC/Тип/" + OBJECT),
        ("format_property", "Пакет/fmt_УчебныйПакетC/Тип/" + OBJECT + "/Свойство/Состояние"),
    ],
)
@pytest.mark.parametrize("action", ["delete", "update"])
def test_consumers_prevent_destructive_change(kind, address, action):
    model = replace(manager(), pko=(pko(properties=(pks(),)),)).with_revision()
    patch = (
        {}
        if action == "delete"
        else {
            # Смена URI — новый контракт; имя метаданных правила не адресуют.
            "patch": {"namespace": "urn:other:format"}
            if kind == "format_package"
            else {"name": "Другое"}
        }
    )
    planned = preview(
        model,
        (parse_operation(operation(kind, action, address=address, **patch)),),
        expected_revision=model.revision,
        format_schema=package_c().base_schema,
    )
    assert [f.reason for f in planned.failures] == ["format_in_use"]
    assert planned.failures[0].references == (
        ("ПКО/Задача/ПКС/field",)
        if kind == "format_property"
        else ("ПКО/Задача", "ПКО/Задача/ПКС/field")
    )
    assert planned.model is model


# Находки ревью 28.10.2026 (docs/plans/evals/2026-10-28-format-extension.md, раздел 3).


def test_review_empty_format_object_is_not_type_missing():
    """XDTO:4146: пустой ОбъектФормата исполнитель не ищет и правило не удаляет."""
    model = replace(manager(), pko=(pko(name="", properties=(pks(),)),))
    assert not {c for c, _ in issues(model) if c == "ed.format.type_missing"}
    model = replace(manager(packages=False), pod=(pod(""),))
    assert not issues(model)


def test_review_rule_without_format_extension_is_silent():
    """Тип вне версии без init и своего типа — вопрос версии схемы, не пакета."""
    model = replace(manager(packages=False), pko=(pko(name="НетВВерсии", extensions=()),))
    report = validate_format(model, package_c().base_schema)
    assert not report.issues and not report.skipped
    report = validate_format(model, None)
    assert not report.issues and not report.skipped


def test_review_unparsed_init_is_skipped_not_error():
    """Init не литералом не попадает в extensions: ПКО retained, проверки init — пропуск."""
    rule = replace(pko(properties=(pks(),), extensions=()), state="retained")
    report = validate_format(replace(manager(), pko=(rule,)), package_c().base_schema)
    checks = {i.check for i in report.issues}
    assert not checks & {"ed.format.init_missing", "ed.format.type_missing"}
    assert {"ed.format.init_missing", "ed.format.type_missing"} <= {
        s.check for s in report.skipped if s.reason.startswith("rule_not_editable")
    }
    # Тот же ПКО, но редактируемый — ошибки есть (отрицательный случай).
    rule = replace(rule, state="editable")
    checks = {
        i.check
        for i in validate_format(replace(manager(), pko=(rule,)), package_c().base_schema).issues
    }
    assert {"ed.format.init_missing", "ed.format.type_missing"} <= checks


def test_review_consumer_without_namespace_on_own_type():
    """ПКС без URI у ПКО своего типа пишет свойство своего типа — это потребитель."""
    model = replace(manager(), pko=(pko(properties=(pks(namespace=""),)),)).with_revision()
    planned = preview(
        model,
        (
            parse_operation(
                operation(
                    "format_property",
                    "delete",
                    address="Пакет/fmt_УчебныйПакетC/Тип/" + OBJECT + "/Свойство/Состояние",
                )
            ),
        ),
        expected_revision=model.revision,
        format_schema=package_c().base_schema,
    )
    assert [f.reason for f in planned.failures] == ["format_in_use"]
    assert planned.failures[0].references == ("ПКО/Задача/ПКС/field",)


def test_review_build_packages_count_as_own_uris():
    """Пакет из файла сборки — свой URI: точное сравнение, опечатка в init остаётся ошибкой."""
    package = package_c()
    model = replace(manager(packages=False), pko=(pko(properties=(pks(),)),))
    report = validate_format(model, package.base_schema, (package,))
    assert not report.errors  # предупреждение об удалении своей ссылки здесь верно
    typo = replace(model, pko=(pko(properties=(pks(),), extensions=(URI + "-typo",)),))
    checks = {i.check for i in validate_format(typo, package.base_schema, (package,)).issues}
    assert "ed.format.uri_unknown" in checks


def test_review_pair_ignores_metadata_name_and_reads_type_uri():
    source = replace(manager(), pko=(pko(properties=(pks(),)),))
    target = manager()
    renamed = replace(
        target,
        format_packages=(
            replace(target.format_packages[0], metadata_name="fmt_Другое", name="fmt_Другое"),
        ),
    )
    assert not validate_format_pair(source, renamed).issues
    implicit = replace(manager(), pko=(pko(properties=(pks(namespace=""),)),))
    report = validate_format_pair(implicit, manager(packages=False), package_c().base_schema)
    assert {(i.check, i.address) for i in report.issues} == {
        ("ed.format.package_one_sided", "ПКО/Задача/ПКС/field")
    }


def test_review_package_namespace_change_rewrites_qnames():
    model = manager().with_revision()
    planned = preview(
        model,
        (
            parse_operation(
                operation(
                    "format_package",
                    "update",
                    address="Пакет/fmt_УчебныйПакетC",
                    patch={"namespace": "urn:other:format", "metadata_name": "fmt_Новое"},
                )
            ),
        ),
        expected_revision=model.revision,
        format_schema=package_c().base_schema,
    )
    assert not planned.failures
    package = planned.model.format_packages[0]
    assert package.namespace == "urn:other:format" and package.metadata_name == "fmt_Новое"
    names = {t.name.namespace for t in package.types} | {
        p.name.namespace for t in package.types for p in t.properties
    }
    assert names == {"urn:other:format"}
