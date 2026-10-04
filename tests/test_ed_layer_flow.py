"""Условия пути, выбор менеджера и определённость действующего представления."""

import shutil
from dataclasses import replace
from pathlib import Path

import pytest

from kd2_rules_mcp.ed.errors import EdReadError
from kd2_rules_mcp.ed.layer_model import (
    SKIP_REASONS,
    Certainty,
    LayerDescriptor,
    LayerSkip,
    LayerStatus,
    rule_view,
)
from kd2_rules_mcp.ed.layer_reader import read_extension_text
from kd2_rules_mcp.ed.layers import compose_manager, read_layers
from kd2_rules_mcp.ed.reader import read_manager_text

ROOT = Path(__file__).resolve().parent / "data" / "ed" / "layers"
HELPERS = frozenset({"добавитьпкс", "добавитьпктч"})
MODULE = (ROOT / "base" / "CommonModules" / "МенеджерДемо" / "Ext" / "Module.bsl").read_text(
    encoding="utf-8"
)
MD = 'xmlns="http://v8.1c.ru/8.3/MDClasses"'
HEAD = (
    '&После("ЗаполнитьПравилаКонвертацииОбъектов")\n'
    "Процедура Доп(НаправлениеОбмена, ПравилаКонвертации)\n"
)
FIND = '    Правило = ПравилаКонвертации.Найти("Товар", "ИмяПКО");\n'
TAIL = "КонецПроцедуры\n"


def _add(format_name: str = "Extra") -> str:
    return f'    ДобавитьПКС(Правило.Свойства, "Х", "{format_name}");\n'


@pytest.mark.parametrize(
    "statement",
    [
        'Выполнить("Правило.Свойства.Очистить()");',
        'Выполнить "ПравилаКонвертации.Очистить()";',
        'Код = "ПравилаКонвертации.Очистить()"; Выполнить(Код);',
        'Х = Вычислить("ПравилаКонвертации.Очистить()");',
        "Сообщить(Вычислить(Код));",
        'Пока Вычислить("ПравилаКонвертации.Очистить()") Цикл\nКонецЦикла;',
    ],
)
def test_dynamic_code_denies_its_procedure_scope_only_on_its_path(statement: str):
    result = _overlay(
        _demo(),
        HEAD
        + FIND
        + 'Если НаправлениеОбмена = "Получение" Тогда\n'
        + statement
        + "\nКонецЕсли;\n"
        + TAIL,
    )
    assert _certainty(result, "send") == "known"
    assert _certainty(result, "receive") == "unknown"
    assert "dynamic_code" in {skip.reason for skip in result.skipped}
    clean = _overlay(
        _demo(), HEAD + FIND + "Если Ложь Тогда\n" + statement + "\nКонецЕсли;\n" + _add() + TAIL
    )
    assert clean.status == "complete"
    assert _certainty(clean, "send") == "known"


@pytest.mark.parametrize(
    "statement,prefix",
    [
        ('Выполнить("ПравилаКонвертации.Очистить()");', ""),
        ("ПравилаКонвертации = Неопределено;", ""),
        ("Глоб = ПравилаКонвертации;", "Перем Глоб;\n"),
    ],
)
def test_base_filler_obeys_dynamic_escape_and_byref_deny(statement: str, prefix: str):
    changed = prefix + MODULE.replace(
        "    ДобавитьПКО_Товар(ПравилаКонвертации);",
        statement + "\n    ДобавитьПКО_Товар(ПравилаКонвертации);",
    )
    result = compose_manager(read_manager_text(changed))
    assert result.status == "partial"
    assert result.skipped
    assert _certainty(result, "send") == "unknown"
    assert compose_manager(_demo()).status == "complete"


@pytest.mark.parametrize(
    "declaration,assignment",
    [
        ("Перем Глоб Экспорт;\n", "Глоб = Правило;"),
        ("Перем Первый, Глоб;\n", "глоб = Правило.Свойства;"),
        ("Перем Т;\n", "Т = ПравилаКонвертации;"),
    ],
)
def test_module_variable_escape_and_explicit_local_shadow(declaration: str, assignment: str):
    result = _overlay(_demo(), declaration + HEAD + FIND + assignment + "\n" + TAIL)
    assert result.status == "partial"
    assert _certainty(result, "send") == "unknown"
    assert "module_variable_escape" in {skip.reason for skip in result.skipped}
    local = assignment.partition("=")[0].strip()
    clean = _overlay(
        _demo(), declaration + HEAD + f"Перем {local};\n" + FIND + assignment + "\n" + _add() + TAIL
    )
    assert clean.status == "complete"
    assert _certainty(clean, "send") == "known"
    scalar = _overlay(_demo(), declaration + HEAD + FIND + f"{local} = Правило.ИмяПКО;\n" + TAIL)
    assert scalar.status == "complete"


@pytest.mark.parametrize(
    "value",
    ["Неопределено", "Истина", "Ложь", "ПравилаКонвертации", "Другая", "Новый ТаблицаЗначений"],
)
def test_collection_parameter_rebind_differs_from_a_local_alias(value: str):
    result = _overlay(_demo(), HEAD + FIND + f"ПравилаКонвертации = {value};\n" + _add() + TAIL)
    assert result.status == "partial"
    assert _certainty(result, "send") == "unknown"
    assert "collection_rebind" in {skip.reason for skip in result.skipped}
    clean = _overlay(_demo(), HEAD + FIND + f"Локальная = {value};\n" + _add() + TAIL)
    assert clean.status == "complete"


@pytest.mark.parametrize(
    "statement",
    [
        "Сообщить(Правило.ИмяПКО);",
        'Журнал.Метод("Обмен", Правило.ИмяПКО);',
        "Имя = Правило.ОбъектФормата;",
        'Текст = "Имя: " + Правило.ИмяПКО;',
        'Если Правило.ИмяПКО = "Товар" Тогда\n Сообщить(1);\n КонецЕсли;',
        "Сообщить(ПравилаКонвертации.Количество());",
    ],
)
def test_scalar_field_reads_leave_the_rule_known(statement: str):
    clean = _overlay(_demo(), HEAD + FIND + statement + "\n" + _add() + TAIL)
    assert clean.status == "complete"
    assert _certainty(clean, "send") == "known"
    bad = _overlay(_demo(), HEAD + FIND + "М.Доработать(Правило.Свойства);\n" + TAIL)
    assert bad.status == "partial"
    assert _certainty(bad, "send") == "unknown"


def test_scalar_field_condition_only_taints_its_mutating_body():
    result = _overlay(
        _demo(),
        HEAD + FIND + 'Если Правило.ИмяПКО = "Товар" Тогда\n' + _add() + "КонецЕсли;\n" + TAIL,
    )
    assert result.status == "partial"
    assert result.skipped
    assert all(
        item.certainty == "known"
        for item in _context(result, "send").entities
        if item.collection != "pko"
    )


@pytest.mark.parametrize("name,expected", [("Description", False), ("Extra", True)])
def test_inline_property_find_has_the_same_snapshot_guard(name: str, expected: bool):
    result = _overlay(
        _demo(),
        HEAD
        + FIND
        + f'Если Правило.Свойства.Найти("{name}", "СвойствоФормата") = Неопределено Тогда\n'
        + _add()
        + "КонецЕсли;\n"
        + TAIL,
    )
    assert result.status == "complete"
    assert ("Extra" in _formats(result, "send")) == expected


def test_try_catches_raise_preserves_prior_changes_and_continues():
    body = (
        "Попытка\n"
        + _add("Before")
        + 'ВызватьИсключение "x";\n'
        + _add("Dead")
        + "Исключение\n"
        + _add("Caught")
        + "КонецПопытки;\n"
        + _add("After")
    )
    result = _overlay(_demo(), HEAD + FIND + body + TAIL)
    assert result.status == "complete"
    assert _formats(result, "send") == ["Description", "Before", "Caught", "After"]
    bad = _overlay(_demo(), HEAD + FIND + 'ВызватьИсключение "x";\n' + TAIL)
    assert "filler_error" in {skip.reason for skip in bad.skipped}


def test_try_return_and_unknown_exception_paths_are_distinct():
    returned = _overlay(
        _demo(),
        HEAD
        + FIND
        + "Попытка\nВозврат;\nИсключение\n"
        + _add("Caught")
        + "КонецПопытки;\n"
        + _add()
        + TAIL,
    )
    assert returned.status == "complete"
    assert _formats(returned, "send") == ["Description"]
    uncertain = _overlay(
        _demo(),
        HEAD
        + FIND
        + "Попытка\nХ = М.Получить();\n"
        + _add()
        + "Исключение\nКонецПопытки;\n"
        + TAIL,
    )
    assert uncertain.status == "partial"
    assert uncertain.skipped
    assert _certainty(uncertain, "send") == "unknown"


def test_try_catches_a_missing_rule_dereference():
    result = _overlay(
        _demo(),
        HEAD
        + 'Правило = ПравилаКонвертации.Найти("Нет", "ИмяПКО");\nПопытка\n'
        + _add("Dead")
        + "Исключение\n"
        + FIND
        + _add("Caught")
        + "КонецПопытки;\n"
        + _add("After")
        + TAIL,
    )
    assert result.status == "complete"
    assert _formats(result, "send") == ["Description", "Caught", "After"]


@pytest.mark.parametrize(
    "statement,property_row",
    [
        ("Имя = П.ИмяПКО;", False),
        ("Сообщить(П.ИмяПКО);", False),
        ("Имя = П.СвойствоФормата;", True),
        ('С = П.Свойства.Найти("Extra", "СвойствоФормата");', False),
        ('Если П.ИмяПКО = "Товар" Тогда\nСообщить(1);\nКонецЕсли;', False),
    ],
)
@pytest.mark.parametrize("caught", [False, True])
def test_scalar_dereference_requires_a_found_row(statement: str, property_row: bool, caught: bool):
    def source(name: str) -> str:
        find = (
            f'П = Правило.Свойства.Найти("{name}", "СвойствоФормата");\n'
            if property_row
            else f'П = ПравилаКонвертации.Найти("{name}", "ИмяПКО");\n'
        )
        body = statement + "\n" + _add("Inside")
        if caught:
            body = "Попытка\n" + body + "Исключение\n" + _add("Caught") + "КонецПопытки;\n"
        return HEAD + FIND + find + body + _add("After") + TAIL

    missing = _overlay(_demo(), source("Нет"))
    if caught:
        assert missing.status == "complete"
        assert _formats(missing, "send") == ["Description", "Caught", "After"]
    else:
        assert missing.status == "partial"
        assert "filler_error" in {skip.reason for skip in missing.skipped}
        assert "After" not in _formats(missing, "send")
    clean = _overlay(_demo(), source("Description" if property_row else "Товар"))
    assert clean.status == "complete"
    assert _formats(clean, "send") == ["Description", "Inside", "After"]


@pytest.mark.parametrize("raises", [True, False])
def test_try_local_helper_throw_stops_the_body_but_helper_return_does_not(raises: bool):
    action = 'ВызватьИсключение "x";' if raises else "Возврат;"
    source = (
        HEAD
        + FIND
        + "Попытка\nУпасть();\n"
        + _add("Dead")
        + "Исключение\n"
        + _add("Caught")
        + "КонецПопытки;\n"
        + _add("After")
        + TAIL
        + "Процедура Упасть()\n"
        + action
        + "\n"
        + TAIL
    )
    result = _overlay(_demo(), source)
    assert result.status == "complete"
    assert _formats(result, "send") == ["Description", "Caught" if raises else "Dead", "After"]


@pytest.mark.parametrize("name,known", [("Товар", True), ("Нет", False)])
def test_unreachable_filler_error_has_no_operation_status_or_unknown_coverage(
    name: str, known: bool
):
    result = _overlay(
        _demo(),
        HEAD
        + f'Правило = ПравилаКонвертации.Найти("{name}", "ИмяПКО");\n'
        + 'Если Правило = Неопределено Тогда\nВызватьИсключение "нет";\nКонецЕсли;\n'
        + _add()
        + TAIL,
    )
    assert (result.status == "complete") == known
    assert bool(result.skipped) != known
    assert any(op.resolution == "filler_error" for op in result.operations) != known
    if known:
        assert result.coverage[0][1].line_classes[4] != "unknown"


@pytest.mark.parametrize("delete_after", [False, True])
def test_false_skip_uses_the_state_at_its_statement(delete_after: bool):
    body = (
        'Если Правило = Неопределено Тогда\nВыполнить("ПравилаКонвертации.Очистить()");\n'
        "КонецЕсли;\n"
    )
    suffix = "ПравилаКонвертации.Удалить(Правило);\n" if delete_after else _add()
    result = _overlay(_demo(), HEAD + FIND + body + suffix + TAIL)
    assert result.status == "complete"
    assert result.skipped == ()
    assert not any(op.resolution == "unknown" for op in result.operations)
    assert _certainty(result, "send") == "known"
    bad = _overlay(_demo(), HEAD + FIND.replace('"Товар"', '"Нет"') + body + TAIL)
    assert bad.status == "partial"
    assert "dynamic_code" in {skip.reason for skip in bad.skipped}


@pytest.mark.parametrize("find_name,known", [("Новый", True), ("Нет", False)])
def test_find_new_rule_sees_the_immediately_created_row(find_name: str, known: bool):
    body = (
        "Р = ОбменДаннымиXDTOСервер.ИнициализироватьПравилоКонвертацииОбъекта("
        'ПравилаКонвертации);\nР.ИмяПКО = "Новый";\n'
        + f'Правило = ПравилаКонвертации.Найти("{find_name}", "ИмяПКО");\n'
        + _add()
    )
    result = _overlay(_demo(), HEAD + body + TAIL)
    assert (result.status == "complete") == known
    if known:
        rule = next(
            item for item in rule_view(_context(result, "send")).pko if item.name == "Новый"
        )
        assert [prop.format_property for prop in rule.properties] == ["Extra"]
    else:
        assert "filler_error" in {skip.reason for skip in result.skipped}


@pytest.mark.parametrize("newline", ["\n", "\r", "\r\n"])
def test_simple_reads_guards_and_cr_newlines(newline: str):
    body = (
        'Если Не ЗначениеЗаполнено(Правило) Тогда\nВызватьИсключение "нет";\nКонецЕсли;\n'
        "Если ПравилаКонвертации.Количество() > 0 Тогда\n" + _add() + "КонецЕсли;\n"
    )
    result = _overlay(_demo(), (HEAD + FIND + body + TAIL).replace("\n", newline))
    assert result.status == "complete"
    assert "Extra" in _formats(result, "send")
    absent = _overlay(
        _demo(), (HEAD + FIND.replace('"Товар"', '"Нет"') + body + TAIL).replace("\n", newline)
    )
    assert absent.status == "partial"
    assert "filler_error" in {skip.reason for skip in absent.skipped}


@pytest.mark.parametrize("property_find", [False, True])
def test_branch_bindings_reach_the_join(property_find: bool):
    prefix = FIND
    assignment = 'Правило = ПравилаКонвертации.Найти("Нет", "ИмяПКО");'
    guard = "Правило <> Неопределено"
    if property_find:
        prefix += 'Свойство = Правило.Свойства.Найти("Description", "СвойствоФормата");\n'
        assignment = 'Свойство = Правило.Свойства.Найти("Нет", "СвойствоФормата");'
        guard = "Свойство = Неопределено"
    layered = _overlay(
        _demo(),
        HEAD
        + prefix
        + 'Если НаправлениеОбмена = "Отправка" Тогда\n'
        + assignment
        + "\nКонецЕсли;\nЕсли "
        + guard
        + " Тогда\n"
        + _add()
        + "КонецЕсли;\n"
        + TAIL,
    )
    expected = "send" if property_find else "receive"
    for direction in ("send", "receive"):
        assert ("Extra" in _formats(layered, direction)) == (direction == expected)
        assert rule_view(_context(layered, direction)).certainty == Certainty.KNOWN


@pytest.mark.parametrize("raised", [True, False])
def test_filler_error_is_limited_to_its_direction(raised: bool):
    if raised:
        body = (
            FIND
            + 'Если НаправлениеОбмена = "Отправка" Тогда\nВызватьИсключение "стоп";\nКонецЕсли;\n'
        )
        bad, good = "send", "receive"
    else:
        body = 'Правило = ПравилаКонвертации.Найти("Нет", "ИмяПКО");\n'
        body += 'Если НаправлениеОбмена = "Отправка" Тогда\n' + FIND + "КонецЕсли;\n"
        bad, good = "receive", "send"
    layered = _overlay(_demo(), HEAD + body + _add() + TAIL)
    assert "filler_error" in {skip.reason for skip in layered.skipped}
    assert rule_view(_context(layered, bad)).certainty == Certainty.UNKNOWN
    assert rule_view(_context(layered, good)).certainty == Certainty.KNOWN
    assert "Extra" in _formats(layered, good)


@pytest.mark.parametrize(
    "statement,collection",
    [
        ("Правило.Свойства.Очистить();", False),
        ("Правило.Свойства.Удалить(0);", False),
        ("Правило.Свойства = Новый ТаблицаЗначений;", False),
        ("С = Правило.Свойства; С.Очистить();", False),
        ("Модуль.Доработать(Правило.Свойства);", False),
        ("Модуль.Доработать(1 + Правило);", False),
        ("ПравилаКонвертации.Удалить(0);", True),
        ("ПравилаКонвертации.Очистить();", True),
        ("Новое = ПравилаКонвертации.Вставить(0);", True),
        ("Т = ПравилаКонвертации; Т.Очистить();", True),
        ('Ст = Новый Структура("Т", ПравилаКонвертации); Ст.Т.Очистить();', True),
        ("ПравилаКонвертации = Новый ТаблицаЗначений;", True),
    ],
)
def test_default_deny_preserves_unaffected_rules(statement: str, collection: bool):
    document = _demo()
    extra = replace(document.pko[0], entity_id="other", name="Услуга")
    document = replace(document, pko=(*document.pko, extra))
    layered = _overlay(document, HEAD + FIND + statement + "\n" + TAIL)
    assert layered.status == LayerStatus.PARTIAL
    for context in layered.contexts:
        ranks = {item.payload.name: item.certainty for item in context.entities if item.payload}
        assert ranks["Товар"] == Certainty.UNKNOWN
        assert ranks["Услуга"] == (Certainty.UNKNOWN if collection else Certainty.KNOWN)
        assert ranks["Товары"] == Certainty.KNOWN
        assert ranks["РежимДемо"] == Certainty.KNOWN
    clean = _overlay(document, HEAD + FIND + "Модуль.Прочее(1); Сообщить(1);\n" + TAIL)
    assert clean.status == LayerStatus.COMPLETE
    assert all(rule_view(context).certainty == Certainty.KNOWN for context in clean.contexts)


@pytest.mark.parametrize("reason", sorted(SKIP_REASONS | {"future_reason"}))
@pytest.mark.parametrize("scope", ["entity", "collection", "filler"])
def test_every_skip_reason_lowers_its_scope(reason: str, scope: str):
    document = _demo()
    clean = compose_manager(document)
    origin = clean.contexts[0].entities[0].origins[0]
    ref = {"entity": "pko\x1fИмяПКО\x1fТовар", "collection": "pko", "filler": "filler"}[scope]
    skip = LayerSkip(reason, origin, (ref,), "", (ref,))
    layered = compose_manager(document, skips=(skip,))
    for context in layered.contexts:
        assert rule_view(context).certainty == Certainty.UNKNOWN
        for item in context.entities:
            assert item.certainty == (
                Certainty.UNKNOWN
                if item.collection == "pko" or scope == "filler"
                else Certainty.KNOWN
            )
    with pytest.raises(ValueError, match="область"):
        LayerSkip(reason, origin, (), "", ())
    with pytest.raises(ValueError, match="область"):
        LayerSkip(reason, origin, ("",), "", ("",))


def test_opaque_branch_without_exit_does_not_stop_following_calls():
    text = HEAD + FIND + "Если Флаг() Тогда\nСообщить(1);\nКонецЕсли;\n" + _add() + TAIL
    layered = _overlay(_demo(), text)
    assert layered.status == LayerStatus.COMPLETE
    assert all("Extra" in _formats(layered, direction) for direction in ("send", "receive"))
    old = "    ДобавитьПОД_Товары(ПравилаОбработкиДанных);"
    body = 'Если ПравилаОбработкиДанных.Колонки.Найти("ОчисткаДанных") = Неопределено Тогда\n'
    body += 'ПравилаОбработкиДанных.Колонки.Добавить("ОчисткаДанных");\nКонецЕсли;\n' + old
    base = compose_manager(_patched(old, body))
    assert base.status == LayerStatus.COMPLETE
    assert all(rule_view(context).certainty == Certainty.KNOWN for context in base.contexts)
    hook = (
        '&После("ЗаполнитьПравилаОбработкиДанных")\n'
        "Процедура Доп(НаправлениеОбмена, ПравилаОбработкиДанных)\n"
    )
    clean = _overlay(_demo(), hook + body.replace(old, "") + TAIL)
    assert clean.status == LayerStatus.COMPLETE


def test_parenthesized_boolean_terms_and_external_connection():
    for condition, expected in [
        ('Не (НаправлениеОбмена = "Отправка" И Правило <> Неопределено)', "receive"),
        (
            '(НаправлениеОбмена = "Отправка" Или НаправлениеОбмена = "Получение") '
            "И Правило <> Неопределено",
            "both",
        ),
    ]:
        layered = _overlay(
            _demo(), HEAD + FIND + f"Если {condition} Тогда\n" + _add() + "КонецЕсли;\n" + TAIL
        )
        assert layered.status == LayerStatus.COMPLETE
        for direction in ("send", "receive"):
            assert ("Extra" in _formats(layered, direction)) == (expected in ("both", direction))
    unknown = _overlay(
        _demo(), HEAD + FIND + "#Если ВнешнееСоединение Тогда\n" + _add() + "#КонецЕсли\n" + TAIL
    )
    assert unknown.status == LayerStatus.PARTIAL
    assert all(
        _certainty(unknown, direction) == Certainty.UNKNOWN for direction in ("send", "receive")
    )


def test_dnf_limit_has_its_own_reason():
    finds = "".join(f'Р{i} = ПравилаКонвертации.Найти("Нет{i}", "ИмяПКО");\n' for i in range(14))
    condition = " И ".join(
        f"(Р{i} = Неопределено Или Р{i + 1} = Неопределено)" for i in range(0, 14, 2)
    )
    layered = _overlay(
        _demo(), HEAD + FIND + finds + f"Если {condition} Тогда\n" + _add() + "КонецЕсли;\n" + TAIL
    )
    assert "condition_limit" in {skip.reason for skip in layered.skipped}
    assert all(
        _certainty(layered, direction) == Certainty.UNKNOWN for direction in ("send", "receive")
    )


def _layer(ordinal: int = 1, name: str = "ДемоB") -> LayerDescriptor:
    return LayerDescriptor(f"L0{ordinal}-{name}", ordinal, name, "mem", None, "")


def _overlay(document, text: str):
    reading = read_extension_text(
        text,
        layer=_layer(),
        version=document.manager_version,
        helpers=HELPERS,
        targets={item.name.casefold(): item for item in document.routines},
    )
    base_layer = LayerDescriptor("base", 0, "Демо", "base", None, "")
    return compose_manager(document, readings=[reading], layers=(base_layer, _layer()))


def _demo():
    return read_manager_text(MODULE, file_id="base", path="base.bsl")


def _patched(old: str, new: str):
    assert old in MODULE
    return read_manager_text(MODULE.replace(old, new, 1), file_id="base", path="base.bsl")


def _context(layered, direction: str):
    return next(
        item for item in layered.contexts if item.direction == direction and not item.headers_only
    )


def _formats(layered, direction: str) -> list[str]:
    view = rule_view(_context(layered, direction))
    return [
        prop.format_property
        for rule in view.pko
        if rule.name == "Товар"
        for prop in rule.properties
    ]


def _certainty(layered, direction: str):
    return next(
        item.certainty
        for item in _context(layered, direction).entities
        if getattr(item.payload, "name", None) == "Товар"
    )


def test_new_rule_creation_and_field_write_keep_branch_paths():
    init = (
        "Р = ОбменДаннымиXDTOСервер.ИнициализироватьПравилоКонвертацииОбъекта"
        '(ПравилаКонвертации);\nР.ИмяПКО = "Новый";\n'
    )
    created = _overlay(
        _demo(), HEAD + 'Если НаправлениеОбмена = "Отправка" Тогда\n' + init + "КонецЕсли;\n" + TAIL
    )
    assert any(rule.name == "Новый" for rule in rule_view(_context(created, "send")).pko)
    assert all(rule.name != "Новый" for rule in rule_view(_context(created, "receive")).pko)
    edited = _overlay(
        _demo(),
        HEAD
        + init
        + 'Если НаправлениеОбмена = "Отправка" Тогда\n'
        + 'Р.ОбъектФормата = "Отправка";\nИначе\n'
        + 'Р.ОбъектФормата = "Получение";\nКонецЕсли;\n'
        + TAIL,
    )
    for direction, expected in (("send", "Отправка"), ("receive", "Получение")):
        rule = next(
            rule for rule in rule_view(_context(edited, direction)).pko if rule.name == "Новый"
        )
        assert rule.format_object.value == expected
        assert rule_view(_context(edited, direction)).certainty == "known"


def test_local_helper_rebinding_is_by_reference_and_has_local_scope():
    for modifier, expected in (("", "receive"), ("Знач ", "both")):
        source = HEAD + FIND + "Помощник(Правило, НаправлениеОбмена, ПравилаКонвертации);\n"
        source += "Если Правило <> Неопределено Тогда\n" + _add() + "КонецЕсли;\n" + TAIL
        source += (
            f"Процедура Помощник({modifier}Р, Направление, Таблица)\n"
            'Если Направление = "Отправка" Тогда\nР = Таблица.Найти("Нет", "ИмяПКО");\n'
            "КонецЕсли;\n" + TAIL
        )
        result = _overlay(_demo(), source)
        assert "Extra" in _formats(result, "receive")
        assert ("Extra" in _formats(result, "send")) == (expected == "both")
    source = HEAD + FIND + "Помощник();\n" + TAIL
    source += "Процедура Помощник()\nПравило.Свойства.Очистить();\n" + TAIL
    assert rule_view(_context(_overlay(_demo(), source), "send")).certainty == "known"


def test_local_properties_argument_and_invalid_arity():
    helper = 'Процедура Помощник(С)\nДобавитьПКС(С, "Х", "Extra");\n' + TAIL
    valid = _overlay(_demo(), HEAD + FIND + "Помощник(Правило.Свойства);\n" + TAIL + helper)
    assert _formats(valid, "send") == ["Description", "Extra"]
    assert valid.status == "complete"
    invalid = _overlay(_demo(), HEAD + FIND + "Помощник(Правило.Свойства, 0);\n" + TAIL + helper)
    assert _certainty(invalid, "send") == "unknown"
    assert "Extra" not in _formats(invalid, "send")


@pytest.mark.parametrize(
    "statement",
    [
        'ДобавитьПКС(Правило.Свойства, "Х", "Extra", 0, М.Доработать(Правило));',
        'ДобавитьПКС(Правило.Свойства, "Х", "Extra", 0, ИмяИзНастроек);',
        "Если М.Доработать(Правило.Свойства) Тогда\nКонецЕсли;",
        "Правило.Свойства = Правило.Свойства;",
        "Правило.ОбъектДанныхФормат = Правило.ОбъектДанныхФормат;",
    ],
)
def test_default_deny_inside_recognized_shapes(statement: str):
    bad = _overlay(_demo(), HEAD + FIND + statement + "\n" + TAIL)
    assert _certainty(bad, "send") == "unknown"
    clean = _overlay(_demo(), HEAD + FIND + 'М.Доработать("текст");\n' + _add() + TAIL)
    assert _certainty(clean, "send") == "known"
    assert "Extra" in _formats(clean, "send")


def test_foreign_call_cannot_preserve_a_passed_direction_variable():
    result = _overlay(
        _demo(),
        HEAD + FIND + "М.Доработать(НаправлениеОбмена);\n"
        'Если НаправлениеОбмена = "Отправка" Тогда\n' + _add() + "КонецЕсли;\n" + TAIL,
    )
    assert _certainty(result, "send") == "unknown"
    assert _certainty(result, "receive") == "unknown"


def test_repeated_byref_actual_requires_unknown_semantics():
    source = HEAD + FIND + "Сменить(Правило, Правило);\n" + TAIL
    source += (
        'Процедура Сменить(P, Q)\nP = Неопределено;\nДобавитьПКС(Q.Свойства, "Х", "Extra");\n'
        + TAIL
    )
    result = _overlay(_demo(), source)
    assert _certainty(result, "send") == "unknown"
    assert "Extra" not in _formats(result, "send")


@pytest.mark.parametrize("control", ["loop", "try"])
def test_context_assignment_in_opaque_control_invalidates_the_old_binding(control: str):
    bounds = {
        "loop": ("Для Сч = 1 По 1 Цикл\n", "КонецЦикла;\n"),
        "try": ("Попытка\n", "Исключение\nКонецПопытки;\n"),
    }
    start, end = bounds[control]
    result = _overlay(
        _demo(),
        HEAD
        + FIND
        + start
        + 'НаправлениеОбмена = "Получение";\n'
        + end
        + 'Если НаправлениеОбмена = "Отправка" Тогда\n'
        + _add()
        + "КонецЕсли;\n"
        + TAIL,
    )
    assert _certainty(result, "send") == ("unknown" if control == "loop" else "known")
    assert "Extra" not in _formats(result, "send")


@pytest.mark.parametrize(
    "body,known",
    [
        ("ТолькоЗаголовки = Ложь;\n", True),
        ("ТолькоЗаголовки = Неопределено;\n", False),
        ("М.Доработать(ТолькоЗаголовки);\n", False),
    ],
)
def test_overwritten_headers_are_not_recovered_by_their_name(body: str, known: bool):
    base = MODULE.replace('Возврат "2";', 'Возврат "3";').replace(
        "ЗаполнитьПравилаКонвертацииОбъектов(НаправлениеОбмена, ПравилаКонвертации)",
        "ЗаполнитьПравилаКонвертацииОбъектов(КомпонентыОбмена, ПравилаКонвертации, "
        "ТолькоЗаголовки)",
    )
    document = read_manager_text(base, file_id="base", path="base.bsl")
    head = (
        '&После("ЗаполнитьПравилаКонвертацииОбъектов")\n'
        "Процедура Доп(КомпонентыОбмена, ПравилаКонвертации, ТолькоЗаголовки)\n"
    )
    result = _overlay(
        document,
        head + FIND + body + "Если ТолькоЗаголовки Тогда\n" + _add() + "КонецЕсли;\n" + TAIL,
    )
    assert all(
        ("Extra" not in [p.format_property for r in rule_view(c).pko for p in r.properties])
        for c in result.contexts
    )
    assert result.status == ("complete" if known else "partial")


def test_opaque_elseif_preserves_the_known_excluded_direction():
    result = _overlay(
        _demo(),
        HEAD
        + FIND
        + 'Если НаправлениеОбмена = "Отправка" Тогда\n'
        + _add("Send")
        + "ИначеЕсли Настройка Тогда\n"
        + _add("Maybe")
        + "КонецЕсли;\n"
        + TAIL,
    )
    assert _certainty(result, "send") == "known"
    assert _formats(result, "send") == ["Description", "Send"]
    assert _certainty(result, "receive") == "unknown"


def test_opaque_raise_keeps_the_known_part_of_its_path():
    result = _overlay(
        _demo(),
        HEAD + FIND + 'Если НаправлениеОбмена = "Отправка" И Настройка Тогда\n'
        'ВызватьИсключение "стоп";\nКонецЕсли;\n' + _add() + TAIL,
    )
    assert rule_view(_context(result, "send")).certainty == "unknown"
    assert rule_view(_context(result, "receive")).certainty == "known"
    assert "Extra" in _formats(result, "receive")


def test_exhaustive_opaque_returns_prove_no_following_operation():
    result = _overlay(
        _demo(),
        HEAD
        + FIND
        + "Если Настройка Тогда\nВозврат;\nИначе\nВозврат;\nКонецЕсли;\n"
        + _add()
        + TAIL,
    )
    assert result.status == "complete"
    assert "Extra" not in _formats(result, "send")


@pytest.mark.parametrize("control", ["loop", "try"])
def test_unknown_control_mutations_keep_their_direction(control: str):
    body = {
        "loop": "Для Сч = 1 По 1 Цикл\nПравилаКонвертации.Очистить();\nКонецЦикла;\n",
        "try": "Попытка\nПравилаКонвертации.Очистить();\nИсключение\nКонецПопытки;\n",
    }[control]
    result = _overlay(
        _demo(),
        HEAD
        + 'Если НаправлениеОбмена = "Отправка" Тогда\n'
        + body
        + "Иначе\n"
        + FIND
        + _add()
        + "КонецЕсли;\n"
        + TAIL,
    )
    assert _certainty(result, "send") == "unknown"
    assert _certainty(result, "receive") == "known"
    assert "Extra" in _formats(result, "receive")


def test_opaque_or_does_not_hide_a_proven_true_direction():
    result = _overlay(
        _demo(),
        HEAD
        + FIND
        + 'Если НаправлениеОбмена = "Отправка" Или Настройка Тогда\n'
        + _add()
        + "КонецЕсли;\n"
        + TAIL,
    )
    assert _formats(result, "send") == ["Description", "Extra"]
    assert _certainty(result, "send") == "known"
    assert _certainty(result, "receive") == "unknown"
    call = "    ДобавитьПКО_Товар(ПравилаКонвертации);\n"
    base = compose_manager(
        _patched(
            call,
            'Если НаправлениеОбмена = "Отправка" Или Настройка Тогда\n' + call + "КонецЕсли;\n",
        )
    )
    assert _certainty(base, "send") == "known"
    assert _certainty(base, "receive") == "unknown"


@pytest.mark.parametrize("property_row", [False, True])
@pytest.mark.parametrize("guarded", [False, True])
def test_delete_missing_row_is_a_filler_error_unless_guarded(property_row: bool, guarded: bool):
    parent = "Правило.Свойства" if property_row else "ПравилаКонвертации"
    column = "СвойствоФормата" if property_row else "ИмяПКО"
    body = FIND + f'Р = {parent}.Найти("Нет", "{column}");\n'
    if guarded:
        body += "Если Р <> Неопределено Тогда\n"
    body += f"{parent}.Удалить(Р);\n"
    if guarded:
        body += "КонецЕсли;\n"
    result = _overlay(_demo(), HEAD + body + TAIL)
    assert ("filler_error" in {skip.reason for skip in result.skipped}) == (not guarded)
    assert _certainty(result, "send") == ("known" if guarded else "unknown")


def test_property_delete_requires_its_actual_owner():
    document = _demo()
    other = replace(document.pko[0], entity_id="other", name="Услуга")
    document = replace(document, pko=(*document.pko, other))
    result = _overlay(
        document,
        HEAD
        + FIND
        + 'Б = ПравилаКонвертации.Найти("Услуга", "ИмяПКО");\n'
        + 'П = Б.Свойства.Найти("Description", "СвойствоФормата");\n'
        + "Правило.Свойства.Удалить(П);\n"
        + TAIL,
    )
    assert result.status == "partial"
    for context in result.contexts:
        assert all(
            item.certainty == "unknown" for item in context.entities if item.collection == "pko"
        )
        assert all(
            item.certainty == "known" for item in context.entities if item.collection != "pko"
        )


def test_found_property_keeps_its_row_identity_after_rename():
    result = _overlay(
        _demo(),
        HEAD
        + FIND
        + 'П = Правило.Свойства.Найти("Наименование", "СвойствоКонфигурации");\n'
        + 'П.СвойствоФормата = "Renamed";\nП.СвойствоКонфигурации = "Изменённое";\n'
        + "Правило.Свойства.Удалить(П);\n"
        + TAIL,
    )
    assert result.status == "complete"
    assert _formats(result, "send") == []


def test_ambiguous_property_find_remains_unknown():
    result = _overlay(
        _demo(),
        HEAD
        + FIND
        + _add("Description")
        + 'П = Правило.Свойства.Найти("Description", "СвойствоФормата");\n'
        + "Если П <> Неопределено Тогда\n"
        + _add("Maybe")
        + "КонецЕсли;\n"
        + TAIL,
    )
    assert _certainty(result, "send") == "unknown"
    assert "Maybe" not in _formats(result, "send")


@pytest.mark.parametrize("op,expected", [("=", False), ("<>", True)])
def test_undefined_is_not_equal_to_a_string(op: str, expected: bool):
    result = _overlay(
        _demo(),
        HEAD
        + FIND
        + "Х = Неопределено;\nЕсли Х "
        + op
        + ' "текст" Тогда\n'
        + _add()
        + "КонецЕсли;\n"
        + TAIL,
    )
    assert ("Extra" in _formats(result, "send")) == expected
    assert result.status == "complete"


def test_base_direction_overwrite_and_collection_mutation():
    call = "    ДобавитьПКО_Товар(ПравилаКонвертации);\n"
    fixed = _patched(
        call,
        '    НаправлениеОбмена = "Отправка";\n'
        '    Если НаправлениеОбмена = "Отправка" Тогда\n' + call + "    КонецЕсли;\n",
    )
    result = compose_manager(fixed)
    assert len(rule_view(_context(result, "receive")).pko) == 1
    assert result.status == "complete"
    mutated = compose_manager(_patched(call, call + "ПравилаКонвертации.Очистить();\n"))
    assert _certainty(mutated, "send") == "unknown"
    assert {item.reason for item in mutated.skipped} == {"unknown_call"}
    missing = _overlay(
        _demo(),
        HEAD + 'Р = ПравилаКонвертации.Найти("Нет", "ИмяПКО");\n'
        'Р.ОбъектФормата = "Новый";\n' + TAIL,
    )
    assert "filler_error" in {item.reason for item in missing.skipped}


def test_elseif_keeps_each_branch():
    both = _overlay(
        _demo(),
        HEAD
        + FIND
        + '    Если НаправлениеОбмена = "Отправка" Тогда\n'
        + '        ДобавитьПКС(Правило.Свойства, "С", "S");\n'
        + '    ИначеЕсли НаправлениеОбмена = "Получение" Тогда\n'
        + '        ДобавитьПКС(Правило.Свойства, "П", "R");\n'
        + "    Иначе\n"
        + '        ДобавитьПКС(Правило.Свойства, "Н", "Never");\n'
        + "    КонецЕсли;\n"
        + TAIL,
    )
    assert both.status == LayerStatus.COMPLETE
    assert _formats(both, "send") == ["Description", "S"]
    assert _formats(both, "receive") == ["Description", "R"]
    only = _overlay(
        _demo(),
        HEAD
        + FIND
        + "    Если Правило = Неопределено Тогда\n"
        + '        ДобавитьПКС(Правило.Свойства, "Z", "Z0");\n'
        + '    ИначеЕсли НаправлениеОбмена = "Отправка" Тогда\n'
        + '        ДобавитьПКС(Правило.Свойства, "П", "OnlySend");\n'
        + "    КонецЕсли;\n"
        + TAIL,
    )
    assert _formats(only, "send") == ["Description", "OnlySend"]
    assert _formats(only, "receive") == ["Description"]
    assert only.status == LayerStatus.COMPLETE


def test_composite_negation():
    early = _overlay(
        _demo(),
        HEAD
        + FIND
        + '    Если НаправлениеОбмена = "Отправка" И Правило <> Неопределено Тогда\n'
        + "        Возврат;\n"
        + "    КонецЕсли;\n"
        + '    ДобавитьПКС(Правило.Свойства, "Х", "AfterAndReturn");\n'
        + TAIL,
    )
    assert "AfterAndReturn" not in _formats(early, "send")
    assert "AfterAndReturn" in _formats(early, "receive")
    other = _overlay(
        _demo(),
        HEAD
        + FIND
        + '    Если НаправлениеОбмена = "Отправка" ИЛИ НаправлениеОбмена = "Получение" Тогда\n'
        + "    Иначе\n"
        + '        ДобавитьПКС(Правило.Свойства, "Н", "OrElseNever");\n'
        + "    КонецЕсли;\n"
        + TAIL,
    )
    assert "OrElseNever" not in _formats(other, "send")
    assert "OrElseNever" not in _formats(other, "receive")
    ander = _overlay(
        _demo(),
        HEAD
        + FIND
        + '    Если НаправлениеОбмена = "Отправка" И Правило <> Неопределено Тогда\n'
        + "    Иначе\n"
        + '        ДобавитьПКС(Правило.Свойства, "Н", "AndElseOnlyReceive");\n'
        + "    КонецЕсли;\n"
        + TAIL,
    )
    assert "AndElseOnlyReceive" not in _formats(ander, "send")
    assert "AndElseOnlyReceive" in _formats(ander, "receive")


def test_return_inside_branch():
    plain = _overlay(
        _demo(),
        HEAD
        + FIND
        + '    Если НаправлениеОбмена = "Отправка" Тогда\n'
        + '        ДобавитьПКС(Правило.Свойства, "С", "SendOnly");\n'
        + "        Возврат;\n"
        + "    КонецЕсли;\n"
        + '    ДобавитьПКС(Правило.Свойства, "П", "ReceiveOnly");\n'
        + TAIL,
    )
    assert _formats(plain, "send") == ["Description", "SendOnly"]
    assert _formats(plain, "receive") == ["Description", "ReceiveOnly"]
    nested = _overlay(
        _demo(),
        HEAD
        + FIND
        + '    Если НаправлениеОбмена = "Отправка" Тогда\n'
        + "        Если Правило <> Неопределено Тогда\n"
        + "            Возврат;\n"
        + "        КонецЕсли;\n"
        + "    КонецЕсли;\n"
        + '    ДобавитьПКС(Правило.Свойства, "П", "ReceiveOnlyNested");\n'
        + TAIL,
    )
    assert "ReceiveOnlyNested" not in _formats(nested, "send")
    assert "ReceiveOnlyNested" in _formats(nested, "receive")
    assert nested.status == LayerStatus.COMPLETE


def test_property_guard_is_fixed_at_find():
    twice = _overlay(
        _demo(),
        HEAD
        + FIND
        + '    Свойство = Правило.Свойства.Найти("Code", "СвойствоФормата");\n'
        + "    Если Свойство = Неопределено Тогда\n"
        + '        ДобавитьПКС(Правило.Свойства, "Код", "Code");\n'
        + "    КонецЕсли;\n"
        + "    Если Свойство = Неопределено Тогда\n"
        + '        ДобавитьПКС(Правило.Свойства, "Код", "Code");\n'
        + "    КонецЕсли;\n"
        + TAIL,
    )
    assert _formats(twice, "send").count("Code") == 2
    assert twice.status == LayerStatus.COMPLETE
    once = _overlay(
        _demo(),
        HEAD
        + FIND
        + '    Свойство = Правило.Свойства.Найти("Code", "СвойствоФормата");\n'
        + "    Если Свойство = Неопределено Тогда\n"
        + '        ДобавитьПКС(Правило.Свойства, "Код", "Code");\n'
        + "    КонецЕсли;\n"
        + TAIL,
    )
    assert _formats(once, "send").count("Code") == 1


def test_unguarded_missing_target_is_filler_error():
    wrong = _overlay(
        _demo(),
        HEAD + '    Правило = ПравилаКонвертации.Найти("товар", "ИмяПКО");\n'
        '    ДобавитьПКС(Правило.Свойства, "А", "WrongCase");\n' + TAIL,
    )
    assert "filler_error" in {skip.reason for skip in wrong.skipped}
    assert rule_view(_context(wrong, "send")).certainty == Certainty.UNKNOWN
    assert "WrongCase" not in _formats(wrong, "send")
    guarded = _overlay(
        _demo(),
        HEAD
        + '    Правило = ПравилаКонвертации.Найти("товар", "ИмяПКО");\n'
        + "    Если Правило <> Неопределено Тогда\n"
        + '        ДобавитьПКС(Правило.Свойства, "А", "WrongCase");\n'
        + "    КонецЕсли;\n"
        + TAIL,
    )
    assert guarded.status == LayerStatus.COMPLETE
    assert "WrongCase" not in _formats(guarded, "send")
    assert "filler_error" not in {skip.reason for skip in guarded.skipped}


def test_instead_without_continue_taints_the_area():
    helper = _overlay(
        _demo(),
        '&Вместо("ДобавитьПКС")\n'
        "Процедура Подмена(РодительПКС, СвойствоКонфигурации, СвойствоФормата, "
        'ИспользуетсяАлгоритмКонвертации = 0, ПравилоКонвертацииСвойства = "", '
        'ПространствоИмен = "")\n'
        "    Возврат;\n"
        "КонецПроцедуры\n",
    )
    assert "helper_replaced" in {skip.reason for skip in helper.skipped}
    assert rule_view(_context(helper, "send")).certainty == Certainty.UNKNOWN
    procedure = _overlay(
        _demo(),
        '&Вместо("ДобавитьПКО_Товар")\nПроцедура Подмена(ПравилаКонвертации)\nКонецПроцедуры\n',
    )
    assert "rule_procedure_replaced" in {skip.reason for skip in procedure.skipped}
    assert _certainty(procedure, "send") == Certainty.UNKNOWN
    kept = _overlay(
        _demo(),
        '&Вместо("ЗаполнитьПравилаКонвертацииОбъектов")\n'
        "Процедура Подмена(НаправлениеОбмена, ПравилаКонвертации)\n"
        "    ПродолжитьВызов(НаправлениеОбмена, ПравилаКонвертации);\n"
        + FIND
        + '    ДобавитьПКС(Правило.Свойства, "Х", "Added");\n'
        + TAIL,
    )
    assert "Added" in _formats(kept, "send")
    assert kept.status == LayerStatus.COMPLETE


def test_annotation_comment_and_unrecognized_form():
    commented = _overlay(
        _demo(),
        '&После("ЗаполнитьПравилаКонвертацииОбъектов") // хвост\n'
        "Процедура Доп(НаправлениеОбмена, ПравилаКонвертации)\n"
        + FIND
        + '    ДобавитьПКС(Правило.Свойства, "Х", "Added");\n'
        + TAIL,
    )
    assert commented.hooks
    assert "Added" in _formats(commented, "send")
    spaced = _overlay(
        _demo(),
        '&После ("ЗаполнитьПравилаКонвертацииОбъектов")\n'
        "Процедура Доп(НаправлениеОбмена, ПравилаКонвертации)\n"
        + FIND
        + '    ДобавитьПКС(Правило.Свойства, "Х", "Added");\n'
        + TAIL,
    )
    assert "Added" in _formats(spaced, "send")
    for text in (
        '& После("ЗаполнитьПравилаКонвертацииОбъектов")\nПроцедура Доп()\nКонецПроцедуры\n',
        "&После(ИмяБезКавычек)\nПроцедура Доп()\nКонецПроцедуры\n",
    ):
        broken = _overlay(_demo(), text)
        assert "unrecognized_annotation" in {skip.reason for skip in broken.skipped}
        assert rule_view(_context(broken, "send")).certainty == Certainty.UNKNOWN


def test_parameters_bind_by_position():
    renamed = _overlay(
        _demo(),
        '&После("ЗаполнитьПравилаКонвертацииОбъектов")\n'
        "Процедура Доп(Направление, Правила)\n"
        '    Если Направление = "Отправка" Тогда\n'
        '        Правило = Правила.Найти("Товар", "ИмяПКО");\n'
        '        ДобавитьПКС(Правило.Свойства, "Х", "Renamed");\n'
        "    КонецЕсли;\n"
        "КонецПроцедуры\n",
    )
    assert "Renamed" in _formats(renamed, "send")
    assert "Renamed" not in _formats(renamed, "receive")
    assert renamed.status == LayerStatus.COMPLETE
    valued = _overlay(
        _demo(),
        '&После("ЗаполнитьПравилаКонвертацииОбъектов")\n'
        "Процедура Доп(Знач НаправлениеОбмена, ПравилаКонвертации)\n"
        "КонецПроцедуры\n",
    )
    assert "manager_signature" in {skip.reason for skip in valued.skipped}
    assert rule_view(_context(valued, "send")).certainty == Certainty.UNKNOWN


def test_skip_lowers_view_certainty():
    before = _overlay(
        _demo(),
        '&Перед("ЗаполнитьПравилаКонвертацииОбъектов")\n'
        "Процедура Доп(НаправлениеОбмена, ПравилаКонвертации)\nКонецПроцедуры\n",
    )
    assert "before_filler" in {skip.reason for skip in before.skipped}
    assert rule_view(_context(before, "send")).certainty == Certainty.UNKNOWN
    assert before.status == LayerStatus.PARTIAL


def test_journal_calls_are_not_edges_and_rule_edges_are():
    logs = _overlay(
        _demo(),
        HEAD
        + "    ЗаписьЖурналаРегистрации.Записать(1);\n" * 5
        + FIND
        + "    Если Правило <> Неопределено Тогда\n"
        + '        ДобавитьПКС(Правило.Свойства, "Х", "AfterFiveDottedCalls");\n'
        + "    КонецЕсли;\n"
        + TAIL,
    )
    assert "AfterFiveDottedCalls" in _formats(logs, "send")
    assert logs.status == LayerStatus.COMPLETE
    edge = (
        "    ОбменДаннымиXDTOСервер.ИнициализироватьПравилоКонвертацииОбъекта"
        "(ПравилаКонвертации);\n"
    )
    limited = _overlay(
        _demo(),
        HEAD + edge * 5 + FIND + '    ДобавитьПКС(Правило.Свойства, "Х", "AfterLimit");\n' + TAIL,
    )
    assert "resource_limit" in {skip.reason for skip in limited.skipped}
    assert "AfterLimit" not in _formats(limited, "send")
    assert rule_view(_context(limited, "send")).certainty == Certainty.UNKNOWN
    assert limited.status == LayerStatus.PARTIAL


def test_dispatcher_elseif_and_dead_call():
    text = (
        HEAD
        + FIND
        + '    Правило.ПриОтправкеДанных = "Б_Обработчик";\n'
        + TAIL
        + '&Вместо("ВыполнитьПроцедуруМодуляМенеджера")\n'
        + "Процедура Доп_Диспетчер(ИмяПроцедуры, Параметры)\n"
        + '    Если ИмяПроцедуры = "А_Обработчик" Тогда\n'
        + "        А_Обработчик(Параметры);\n"
        + '    ИначеЕсли ИмяПроцедуры = "Б_Обработчик" Тогда\n'
        + "        Б_Обработчик(Параметры);\n"
        + "    Иначе\n"
        + "        ПродолжитьВызов(ИмяПроцедуры, Параметры);\n"
        + "    КонецЕсли;\n"
        + "КонецПроцедуры\n"
        + "Процедура А_Обработчик(П)\nКонецПроцедуры\n"
        + "Процедура Б_Обработчик(П)\nКонецПроцедуры\n"
    )
    layered = _overlay(_demo(), text)
    chains = {
        item.target_name: item.resolution for item in _context(layered, "send").dispatch_chains
    }
    assert chains["А_Обработчик"] == "call"
    assert chains["Б_Обработчик"] == "call"
    dead = (
        '&Вместо("ВыполнитьПроцедуруМодуляМенеджера")\n'
        "Процедура Доп_Диспетчер(ИмяПроцедуры, Параметры)\n"
        '    Если ИмяПроцедуры = "Х" Тогда\n'
        "        Возврат;\n"
        "        Х(Параметры);\n"
        "    КонецЕсли;\n"
        "КонецПроцедуры\n"
    )
    result = _overlay(_demo(), dead)
    assert result.status == LayerStatus.COMPLETE


def test_base_condition_survives_a_layer_edit():
    call = "    ДобавитьПКО_Товар(ПравилаКонвертации);\n"
    base = _patched(call, "    Если ОбщегоНазначения.Флаг() Тогда\n" + call + "    КонецЕсли;\n")
    plain = compose_manager(base)
    assert _certainty(plain, "send") == Certainty.UNKNOWN
    edited = _overlay(
        base,
        HEAD
        + FIND
        + "    Если Правило <> Неопределено Тогда\n"
        + '        ДобавитьПКС(Правило.Свойства, "Х", "Added");\n'
        + "    КонецЕсли;\n"
        + TAIL,
    )
    assert _certainty(edited, "send") == Certainty.UNKNOWN
    assert edited.status == LayerStatus.PARTIAL
    direct = compose_manager(_demo())
    assert _certainty(direct, "send") == Certainty.KNOWN
    assert direct.status == LayerStatus.COMPLETE


def test_indirect_base_rule_stays_visible():
    call = "    ДобавитьПКО_Товар(ПравилаКонвертации);\n"
    indirect = _patched(call, "    ЗаполнитьВсе(ПравилаКонвертации);\n")
    layered = compose_manager(indirect)
    assert any(rule.name == "Товар" for rule in rule_view(_context(layered, "send")).pko)
    assert _certainty(layered, "send") == Certainty.UNKNOWN
    assert "call_not_found" in {skip.reason for skip in layered.skipped}
    loop = _patched(call, "    Для Сч = 1 По 1 Цикл\n" + call + "    КонецЦикла;\n")
    looped = compose_manager(loop)
    assert any(rule.name == "Товар" for rule in rule_view(_context(looped, "send")).pko)
    assert _certainty(looped, "send") == Certainty.UNKNOWN


def test_base_property_direction_is_the_guard_direction():
    old = '    ДобавитьПКС(СвойстваШапки, "Наименование", "Description");\n'
    new = '    Если НаправлениеОбмена = "Получение" Тогда\n' + old + "    КонецЕсли;\n"
    base = _patched(old, new)
    plain = compose_manager(base)
    assert "Description" not in _formats(plain, "send")
    assert "Description" in _formats(plain, "receive")
    guarded = _overlay(
        base,
        HEAD
        + FIND
        + "    Если Правило <> Неопределено Тогда\n"
        + '        Свойство = Правило.Свойства.Найти("Description", "СвойствоФормата");\n'
        + "        Если Свойство = Неопределено Тогда\n"
        + '            ДобавитьПКС(Правило.Свойства, "Х", "Description");\n'
        + "        КонецЕсли;\n"
        + "    КонецЕсли;\n"
        + TAIL,
    )
    assert _formats(guarded, "send").count("Description") == 1
    assert _formats(guarded, "receive") == ["Description"]


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def _config(root: Path, name: str, modules: list[str], plans: list[str], ext: bool) -> None:
    purpose = "<ConfigurationExtensionPurpose>Customization</ConfigurationExtensionPurpose>"
    kids = "".join(f"<CommonModule>{item}</CommonModule>" for item in modules)
    kids += "".join(f"<ExchangePlan>{item}</ExchangePlan>" for item in plans)
    _write(
        root / "Configuration.xml",
        f"<MetaDataObject {MD}><Configuration><Properties><Name>{name}</Name>"
        f"{purpose if ext else ''}</Properties><ChildObjects>{kids}</ChildObjects>"
        "</Configuration></MetaDataObject>",
    )


def _module(root: Path, name: str, text: str, uuid: str | None, adopted: str | None) -> None:
    props = f"<Name>{name}</Name><Server>true</Server>"
    if adopted is not None:
        props = (
            "<ObjectBelonging>Adopted</ObjectBelonging>"
            + props
            + f"<ExtendedConfigurationObject>{adopted}</ExtendedConfigurationObject>"
        )
    attr = f' uuid="{uuid}"' if uuid else ""
    _write(
        root / "CommonModules" / f"{name}.xml",
        f"<MetaDataObject {MD}><CommonModule{attr}><Properties>{props}</Properties>"
        "</CommonModule></MetaDataObject>",
    )
    _write(root / "CommonModules" / name / "Ext" / "Module.bsl", text)


def _plan(root: Path, body: str, adopted: bool) -> None:
    props = "<Name>ДемоОбмен</Name>"
    attr = ""
    if adopted:
        props = (
            "<ObjectBelonging>Adopted</ObjectBelonging>"
            + props
            + "<ExtendedConfigurationObject>00000000-0000-0000-0000-000000000002"
            + "</ExtendedConfigurationObject>"
        )
    else:
        attr = ' uuid="00000000-0000-0000-0000-000000000002"'
    _write(
        root / "ExchangePlans" / "ДемоОбмен.xml",
        f"<MetaDataObject {MD}><ExchangePlan{attr}><Properties>{props}</Properties>"
        "</ExchangePlan></MetaDataObject>",
    )
    _write(root / "ExchangePlans" / "ДемоОбмен" / "Ext" / "ManagerModule.bsl", body)


def _two(root: Path, left: str, right: str, text_a: str = MODULE) -> None:
    _config(root, "Демо2", ["МенеджерА", "МенеджерБ"], ["ДемоОбмен"], False)
    _module(root, "МенеджерА", text_a, "00000000-0000-0000-0000-00000000000a", None)
    text_b = MODULE.replace('"Наименование", "Description"', '"Код", "Code"')
    _module(root, "МенеджерБ", text_b, "00000000-0000-0000-0000-00000000000b", None)
    _plan(
        root,
        "Процедура ПриПолученииНастроек(Настройки) Экспорт\n"
        "    ВерсииФормата = Новый Соответствие;\n"
        f'    ВерсииФормата.Вставить("1.20", {left});\n'
        f'    ВерсииФормата.Вставить("1.21", {right});\n'
        "    Настройки.ВерсииФорматаОбмена = ВерсииФормата;\n"
        "КонецПроцедуры\n",
        False,
    )


def _adopt(root: Path, name: str, uuid: str) -> None:
    _config(root, "Доп", [name], [], True)
    hook = (
        '&После("ЗаполнитьПравилаКонвертацииОбъектов")\n'
        "Процедура Доп(НаправлениеОбмена, ПравилаКонвертации)\n"
        + FIND
        + "    Если Правило <> Неопределено Тогда\n"
        + '        ДобавитьПКС(Правило.Свойства, "ДопКод", "Code");\n'
        + "    КонецЕсли;\n"
        + TAIL
    )
    _module(root, name, hook, None, uuid)


def test_foreign_adopted_hook_is_not_this_manager(tmp_path: Path):
    base = tmp_path / "base"
    ext = tmp_path / "ext"
    _two(base, "МенеджерА", "МенеджерА")
    _adopt(ext, "МенеджерБ", "00000000-0000-0000-0000-00000000000b")
    layered = read_layers(base, [ext])
    assert layered.contexts[0].manager_name == "МенеджерА"
    assert "Code" not in _formats(layered, "send")
    assert "manager" not in {scope for skip in layered.skipped for scope in skip.check_scope}
    same = tmp_path / "same"
    _adopt(same, "МенеджерА", "00000000-0000-0000-0000-00000000000a")
    applied = read_layers(base, [same])
    assert "Code" in _formats(applied, "send")


def test_manager_is_not_guessed(tmp_path: Path):
    base = tmp_path / "base"
    _two(base, "МенеджерА", "МенеджерБ")
    ext = tmp_path / "ext"
    _adopt(ext, "МенеджерБ", "00000000-0000-0000-0000-00000000000b")
    refused = read_layers(base, [ext])
    assert refused.status == LayerStatus.PARTIAL
    assert "ambiguous_manager" in {skip.reason for skip in refused.skipped}
    assert refused.base.pko == ()
    with pytest.raises(EdReadError, match="МенеджерА"):
        read_layers(base)
    chosen = read_layers(base, manager="МенеджерБ")
    assert "Code" in _formats(chosen, "send")
    assert "Description" not in _formats(chosen, "send")
    keyed = read_layers(base, version_key="1.20")
    assert keyed.contexts[0].manager_name == "МенеджерА"
    switched = tmp_path / "switch"
    _config(switched, "Карта", [], ["ДемоОбмен"], True)
    _plan(
        switched,
        '&После("ПриПолученииНастроек")\n'
        "Процедура Доп(Настройки)\n"
        '    Настройки.ВерсииФорматаОбмена.Вставить("1.20", МенеджерБ);\n'
        '    Настройки.ВерсииФорматаОбмена.Вставить("1.21", МенеджерБ);\n'
        "КонецПроцедуры\n",
        True,
    )
    moved = read_layers(base, [switched])
    assert moved.contexts[0].manager_name == "МенеджерБ"
    assert "Code" in _formats(moved, "send")
    shutil.rmtree(tmp_path / "lower", ignore_errors=True)


def test_explicit_manager_precedes_maps_and_requires_a_declaration(tmp_path: Path):
    base = tmp_path / "base"
    _two(base, "МенеджерА", "МенеджерА")
    for key in (None, "1.20", "несуществующая"):
        chosen = read_layers(base, manager="менеджерб", version_key=key)
        assert chosen.contexts[0].manager_name == "МенеджерБ"
        assert "Code" in _formats(chosen, "send")
    with pytest.raises(EdReadError):
        read_layers(base, manager="Нет")
    _module(
        base,
        "МенеджерБ",
        "// ЗаполнитьПравилаКонвертацииОбъектов\n"
        "Процедура Вызов()\nЗаполнитьПравилаКонвертацииОбъектов();\nКонецПроцедуры",
        "00000000-0000-0000-0000-00000000000b",
        None,
    )
    with pytest.raises(EdReadError):
        read_layers(base, manager="МенеджерБ")
    _plan(base, "", False)
    chosen = read_layers(base)
    assert chosen.contexts[0].manager_name == "МенеджерА"


def test_no_ed_manager_is_a_partial_empty_baseline(tmp_path: Path):
    _config(tmp_path, "БезОбмена", [], [], False)
    result = read_layers(tmp_path)
    assert result.status == "partial"
    assert {item.reason for item in result.skipped} == {"ambiguous_manager"}
    assert result.base.pko == ()
    assert all(rule_view(context).certainty == "unknown" for context in result.contexts)


@pytest.mark.parametrize("target,missing", [("ПриПолученииНастроек", False), ("НетЦели", True)])
def test_route_hook_target_belongs_to_the_adopted_route_module(
    tmp_path: Path, target: str, missing: bool
):
    base, ext = tmp_path / "base", tmp_path / "ext"
    _two(base, "МенеджерА", "МенеджерА")
    _config(ext, "Карта", [], ["ДемоОбмен"], True)
    _plan(
        ext, f'&После("{target}")\nПроцедура Доп(Настройки)\nЗаписатьЛог("готово");\n' + TAIL, True
    )
    result = read_layers(base, [ext], manager="МенеджерА")
    assert ("missing_target" in {skip.reason for skip in result.skipped}) == missing
    assert result.status == ("partial" if missing else "complete")


@pytest.mark.parametrize(
    "body",
    [
        'Для Сч = 1 По 1 Цикл\nЗаписатьЛог("текст");\nКонецЦикла;\n',
        'Попытка\nЗаписатьЛог("текст");\nИсключение\nКонецПопытки;\n',
    ],
)
def test_unrelated_control_blocks_have_no_rule_effect(body: str):
    result = _overlay(_demo(), HEAD + FIND + body + _add() + TAIL)
    assert result.status == "complete"
    assert _certainty(result, "send") == "known"
    assert "Extra" in _formats(result, "send")


def test_scoped_pod_try_and_parameter_condition_leave_pko_known():
    for head, body in [
        (
            '&После("ЗаполнитьПравилаОбработкиДанных")\n'
            "Процедура Доп(НаправлениеОбмена, ПравилаОбработкиДанных)\n",
            "Попытка\nПравилаОбработкиДанных.Очистить();\nИсключение\nКонецПопытки;\n",
        ),
        (
            '&После("ЗаполнитьПараметрыКонвертации")\nПроцедура Доп(ПараметрыКонвертации)\n',
            'Если Настройка Тогда\nПараметрыКонвертации.Вставить("Х", 1);\nКонецЕсли;\n',
        ),
    ]:
        result = _overlay(_demo(), head + body + TAIL)
        assert result.status == "partial"
        assert _certainty(result, "send") == "known"


def test_map_name_is_case_insensitive_and_not_replaced(tmp_path: Path):
    base = tmp_path / "base"
    own = MODULE.replace('"Наименование", "Description"', '"Свой", "Own"')
    _config(base, "Демо2", ["МенеджерА", "ДопМенеджер"], ["ДемоОбмен"], False)
    _module(base, "МенеджерА", MODULE, "00000000-0000-0000-0000-00000000000a", None)
    _module(base, "ДопМенеджер", own, "00000000-0000-0000-0000-00000000000c", None)
    _plan(
        base,
        "Процедура ПриПолученииНастроек(Настройки) Экспорт\n"
        "    ВерсииФормата = Новый Соответствие;\n"
        '    ВерсииФормата.Вставить("1.20", допменеджер);\n'
        '    ВерсииФормата.Вставить("1.21", допменеджер);\n'
        "    Настройки.ВерсииФорматаОбмена = ВерсииФормата;\n"
        "КонецПроцедуры\n",
        False,
    )
    # Карта лежит в базе, не в расширении: имя модуля — собственный модуль базы.
    layered = read_layers(base)
    assert layered.contexts[0].manager_name == "ДопМенеджер"
    assert "Own" in _formats(layered, "send")
    typo = MODULE.replace(
        "ЗаполнитьПравилаКонвертацииОбъектов", "ЗаполнитьПравилаКонвертацииОбъектовОпечатка"
    )
    broken = tmp_path / "typo"
    _two(broken, "МенеджерА", "МенеджерА", text_a=typo)
    read = read_layers(broken)
    assert read.contexts[0].manager_name == "МенеджерА"
    assert "Code" not in _formats(read, "send")
    assert "Description" in _formats(read, "send")
