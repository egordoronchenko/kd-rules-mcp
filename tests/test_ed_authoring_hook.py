"""BSL: независимый whitelist оракул, шаблоны, направления и реальные SourceSpan."""

from dataclasses import replace
from types import MappingProxyType

import pytest

from kd2_rules_mcp.authoring.ed.hook import generate_hook, validate_hook_forms
from kd2_rules_mcp.authoring.ed.model import AuthoringPreconditionError, digest
from kd2_rules_mcp.authoring.ed.operations import apply_header_properties, validate_preconditions
from kd2_rules_mcp.ed.lexer import lex, split_arguments
from kd2_rules_mcp.ed.model import SourceFile
from kd2_rules_mcp.ed.schema.model import QName
from kd2_rules_mcp.validation.ed_authoring import prepare_authoring
from kd2_rules_mcp.validation.ed_structure_snapshot import StructureSnapshot
from tests.test_ed_authoring_model import DATA, IDENTITY, OPERATION, TARGET, inputs, refreshed


def with_test_fields(value, operations):
    """Явно добавляет вымышленные свободные поля для тестов шаблона BSL."""
    schema = value.schemas["1.20"]
    owner = schema.types[QName(schema.base_namespace, "Справочник.Товары")]
    template = next(p for p in owner.properties if p.name.local == "ВнешнийКод")
    existing = {p.name.local for p in owner.properties}
    additions = tuple(
        replace(template, id=f"test-{name}", name=replace(template.name, local=name))
        for name in sorted({op.format_property for op in operations} - existing)
    )
    changed = replace(owner, properties=(*owner.properties, *additions))
    schema = replace(
        schema,
        types=MappingProxyType({**schema.types, owner.qname: changed}),
        by_id=MappingProxyType({**schema.by_id, owner.id: changed}),
        inherited=MappingProxyType({**schema.inherited, owner.id: changed.properties}),
    )
    metadata = value.structure.objects[("справочник", "товары")]
    attribute = metadata.property("Заметка")[0]
    props = dict(metadata.properties)
    for op in operations:
        if op.new_attribute is None and not metadata.property(op.configuration_attribute):
            props[(op.configuration_attribute.casefold(), "")] = (
                replace(attribute, path=op.configuration_attribute),
            )
    metadata = replace(metadata, properties=MappingProxyType(props))
    structure = StructureSnapshot(
        MappingProxyType({**value.structure.objects, ("справочник", "товары"): metadata}),
        MappingProxyType({**value.structure.by_type, metadata.type_name.casefold(): metadata}),
    )
    return refreshed(value, schemas={**value.schemas, "1.20": schema}, structure=structure)


def whitelist(source: SourceFile, direction="send", headers=False, tables=None):
    """Оракул ограниченных форм §3.3: произвольные присваивания/вызовы не принимаются."""
    parsed = lex(source)
    assert parsed.warnings == ()
    active = [True]
    conditions = []
    tables = {"Товар": []} if tables is None else tables
    rule = None
    prop = None
    calls = []
    headers_seen = False
    for statement in parsed.statements:
        tokens = statement.tokens
        values = tuple(t.folded if t.kind == "identifier" else t.value for t in tokens)
        if tokens[0].kind == "directive":
            assert values == ('&После("ЗаполнитьПравилаКонвертацииОбъектов")',)
        elif values[0] == "процедура":
            assert values in (
                (
                    "процедура",
                    "доп_заполнитьправилаконвертацииобъектов",
                    "(",
                    "направлениеобмена",
                    ",",
                    "правилаконвертации",
                    ")",
                ),
                (
                    "процедура",
                    "доп_заполнитьправилаконвертацииобъектов",
                    "(",
                    "компонентыобмена",
                    ",",
                    "правилаконвертации",
                    ",",
                    "толькозаголовки",
                    ")",
                ),
            )
        elif values[0] == "если":
            if values == ("если", "не", "толькозаголовки", "тогда"):
                condition = not headers
                label = "headers"
                headers_seen = True
            elif values in (
                ("если", "направлениеобмена", "=", "Отправка", "тогда"),
                ("если", "направлениеобмена", "=", "Получение", "тогда"),
            ) or (
                len(values) == 7
                and values[:4] == ("если", "компонентыобмена", ".", "направлениеобмена")
                and values[4] == "="
                and values[5] in ("Отправка", "Получение")
                and values[6] == "тогда"
            ):
                condition = values[-2] == ("Отправка" if direction == "send" else "Получение")
                label = "direction"
            elif values == ("если", "правило", "<>", "неопределено", "тогда"):
                condition, label = rule is not None, "rule"
            elif values == ("если", "свойство", "=", "неопределено", "тогда"):
                condition, label = prop is None, "property"
            else:
                raise AssertionError(f"Условие вне whitelist: {values}")
            active.append(active[-1] and condition)
            conditions.append(label)
        elif values[0] == "конецесли":
            assert values == ("конецесли", ";")
            assert len(active) > 1
            active.pop()
            conditions.pop()
        elif values[:5] == ("правило", "=", "правилаконвертации", ".", "найти"):
            assert (
                len(values) == 11
                and values[5] == "("
                and tokens[6].kind == "string"
                and values[7:] == (",", "ИмяПКО", ")", ";")
            )
            assert conditions[-1] == "direction"
            if active[-1]:
                rule = tables.get(tokens[6].value)
        elif values[:7] == ("свойство", "=", "правило", ".", "свойства", ".", "найти"):
            assert (
                len(values) == 13
                and values[7] == "("
                and tokens[8].kind == "string"
                and values[9:] == (",", "СвойствоФормата", ")", ";")
            )
            assert conditions[-1] == "rule"
            if active[-1]:
                assert rule is not None
                prop = next((p for p in rule if p[1] == tokens[8].value), None)
        elif values[0] == "добавитьпкс":
            args = split_arguments(tokens[2:-2])
            assert tuple(t.folded for t in args[0]) == ("правило", ".", "свойства")
            assert len(args) == 3 and all(len(a) == 1 and a[0].kind == "string" for a in args[1:])
            assert conditions[-3:] == ["direction", "rule", "property"]
            if headers_seen:
                assert conditions[0] == "headers"
            if active[-1]:
                assert rule is not None
                rule.append((args[1][0].value, args[2][0].value))
                calls.append(statement.span)
        elif values[0] == "конецпроцедуры":
            assert values == ("конецпроцедуры",)
            assert active == [True]
        else:
            raise AssertionError(f"Форма вне whitelist: {values}")
    return tables, tuple(calls)


@pytest.mark.parametrize("version", [1, 2, 3])
@pytest.mark.parametrize("direction", ["send", "receive"])
@pytest.mark.parametrize("count", [1, 2, 100])
def test_hook_count_guards_directions_and_repeat(version, direction, count):
    value = inputs(version)
    operations = tuple(
        replace(
            OPERATION,
            target=replace(TARGET, direction=direction),
            format_property=f"Поле{i:03}",
            configuration_attribute=f"доп_Поле{i:03}",
            new_attribute=None,
        )
        for i in range(count)
    )
    value = with_test_fields(value, operations)
    hook = generate_hook(value.document, operations, IDENTITY, inputs=value)
    repeated = generate_hook(value.document, tuple(reversed(operations)), IDENTITY, inputs=value)
    assert hook.source.text.encode() == repeated.source.text.encode()
    assert hook.source.text.count("&После") == 1
    assert "\r" not in hook.source.text and hook.source.text.endswith("\n")
    tables, calls = whitelist(hook.source, direction)
    assert len(tables["Товар"]) == len(calls) == count
    assert whitelist(hook.source, direction, tables=tables)[1] == ()
    assert whitelist(hook.source, "receive" if direction == "send" else "send")[1] == ()
    assert len(whitelist(hook.source, direction, headers=True)[1]) == (0 if version == 3 else count)
    for op in operations:
        span = hook.calls[op.operation_id]
        assert span in calls
        assert hook.source.text[span.char_start : span.char_end].startswith("ДобавитьПКС(")
        assert len(hook.arguments[op.operation_id]) == 3


def test_v3_exact_template():
    value = inputs(3)
    op = replace(OPERATION, target=replace(TARGET, direction="receive"))
    hook = generate_hook(value.document, (op,), IDENTITY, inputs=value)
    assert hook.source.text.encode("utf-8") == (DATA / "cases/hook-v3.bsl").read_bytes()


def test_v2_template_with_normative_unicode_sort():
    assert OPERATION.new_attribute is not None
    # §3.1 перечисляет поля в обратном порядке относительно §3.3.1.
    # Применяется нормативная сортировка; остальные байты шаблона сохранены.
    value = inputs(
        text=(DATA / "base/manager-v2.bsl")
        .read_text("utf-8")
        .replace('"Товар"', '"Товар_Отправка"')
    )
    target = replace(TARGET, pko_address="ПКО/Товар_Отправка")
    note = replace(OPERATION, target=target)
    code = replace(
        note,
        configuration_attribute="доп_Код",
        format_property="ВнешнийКод",
        new_attribute=replace(OPERATION.new_attribute, name="доп_Код"),
    )
    hook = generate_hook(value.document, (note, code), IDENTITY, inputs=value)
    assert hook.source.text.encode("utf-8") == (DATA / "cases/hook-v2-sorted.bsl").read_bytes()


def test_string_escaping_and_whitelist_rejects_arbitrary_code():
    value = inputs(
        text=(DATA / "base/manager-v2.bsl").read_text("utf-8").replace('"Товар"', '"Тов""ар"')
    )
    op = replace(
        OPERATION, target=replace(TARGET, pko_address='ПКО/Тов"ар'), format_property='Поле"Формата'
    )
    value = with_test_fields(value, (op,))
    hook = generate_hook(value.document, (op,), IDENTITY, inputs=value)
    assert 'Найти("Тов""ар", "ИмяПКО")' in hook.source.text
    tables, calls = whitelist(hook.source, tables={'Тов"ар': []})
    assert tables['Тов"ар'] == [("доп_Заметка", 'Поле"Формата')]
    assert len(calls) == 1
    dirty = replace(
        hook.source,
        text=hook.source.text.replace("КонецПроцедуры", "Выполнить(Код);\nКонецПроцедуры"),
    )
    with pytest.raises(AssertionError, match="Форма вне whitelist"):
        whitelist(dirty)
    with pytest.raises(ValueError, match="белого списка"):
        validate_hook_forms(dirty, 2, IDENTITY.prefix)


@pytest.mark.parametrize(
    "bad", ["Если", "1Реквизит", "доп_Имя/Иное", "доп_Имя\nИное", "доп_Имя\\Иное"]
)
def test_invalid_identifier(bad):
    value = inputs()
    op = replace(OPERATION, configuration_attribute=bad, new_attribute=None)
    with pytest.raises(AuthoringPreconditionError) as caught:
        generate_hook(value.document, (op,), IDENTITY, inputs=value)
    assert caught.value.failures[0].id == "ed.author.identifier_conflict"


@pytest.mark.parametrize("version", [1, 2, 3])
def test_projection_count_source_spans_and_original_reading(version):
    assert OPERATION.new_attribute is not None
    value = inputs(version)
    base_hash = digest(value.document)
    struct_hash = digest(value.structure)
    second = replace(
        OPERATION,
        configuration_attribute="доп_Код",
        format_property="ВнешнийКод",
        new_attribute=replace(OPERATION.new_attribute, name="доп_Код"),
    )
    validate_preconditions(value, (OPERATION, second), IDENTITY, version_scope="manager")
    prepared = prepare_authoring(value, (OPERATION, second), IDENTITY, version_scope="manager")
    projected = prepared.projection_after
    assert len(projected.document.pko[0].properties) == len(value.document.pko[0].properties) + 2
    assert projected.document.pko[1] is value.document.pko[1]
    assert projected.document.pko[0].span == value.document.pko[0].span
    assert projected.document.pko[0].raw_text == value.document.pko[0].raw_text
    assert projected.document.coverage is value.document.coverage
    assert projected.document.parse_status == value.document.parse_status
    assert projected.document.files[:-1] == value.document.files
    assert digest(value.document) == base_hash and digest(value.structure) == struct_hash
    assert projected.generated_operations_applied == 2
    source = prepared.generated_hook.source
    for guard in projected.document.guards[len(value.document.guards) :]:
        assert guard.raw_text == source.text[guard.span.char_start : guard.span.char_end]
        assert guard.raw_text.startswith("Если ") and guard.raw_text.endswith(" Тогда")
    for prop in projected.document.pko[0].properties[-2:]:
        assert prop.raw_text == source.text[prop.span.char_start : prop.span.char_end]
        assert prop.raw_text in source.text.splitlines()[prop.span.line_start - 1]
        assert prop.argument_presence == (True, True, True)
        assert len(prop.raw_arguments) == 3
        assert prop.group_id is None and prop.algorithm_flag == 0
        assert prop.namespace == prop.conversion_rule == prop.condition_name == ""
    if version == 3:
        headers = apply_header_properties(
            value.document,
            (OPERATION, second),
            prepared.generated_hook,
            schemas=value.schemas,
            headers_only=True,
        )
        assert headers.document is value.document and headers.generated_operations_applied == 0


def test_mixed_direction_grouping_and_no_auto_mirroring():
    value = inputs()
    receive = replace(OPERATION, target=replace(TARGET, direction="receive"))
    hook = generate_hook(value.document, (receive, OPERATION), IDENTITY, inputs=value)
    assert hook.source.text.index('= "Отправка"') < hook.source.text.index('= "Получение"')
    assert len(whitelist(hook.source, "send")[1]) == 1
    assert len(whitelist(hook.source, "receive")[1]) == 1
    prepared = prepare_authoring(value, (receive, OPERATION), IDENTITY, version_scope="manager")
    assert len(prepared.selected_profiles) == 2
    assert all(c.delta.no_new_issues for c in prepared.selected_profiles)
    assert not prepared.runtime_verified
    assert isinstance(hook.calls, MappingProxyType)


def test_whitelist_is_enforced_by_production_generation(monkeypatch):
    import kd2_rules_mcp.authoring.ed.hook as hook_module

    original = hook_module.lex

    def injected(source):
        return original(
            replace(
                source,
                text=source.text.replace("КонецПроцедуры", "Выполнить(Код);\nКонецПроцедуры"),
            )
        )

    monkeypatch.setattr(hook_module, "lex", injected)
    value = inputs()
    with pytest.raises(ValueError, match="белого списка"):
        generate_hook(value.document, (OPERATION,), IDENTITY, inputs=value)


def test_raw_generation_requires_read_inputs_for_canonical_names():
    with pytest.raises(ValueError, match="AuthoringInputs"):
        generate_hook(inputs().document, (OPERATION,), IDENTITY)
