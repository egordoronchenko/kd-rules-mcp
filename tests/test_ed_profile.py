"""Применимость, индексы и границы поддерживаемого языка условий."""

from dataclasses import replace
from pathlib import Path
from typing import Any, cast

import pytest

from kd2_rules_mcp.ed import forms, read_manager_text
from kd2_rules_mcp.ed.schema import QName, load_schema, resolve_property
from kd2_rules_mcp.ed.schema.profile import (
    Applicability,
    ValidationProfile,
    evaluate_condition,
    numeric_helper_verified,
)

DATA = Path(__file__).parent / "data/ed/schema"
BASE = (DATA / "rules.bsl").read_text(encoding="utf-8")


def document(text=BASE):
    helpers = "\n".join(forms.helper_forms(name, 2)[0] for name in ("ДобавитьПКС", "ДобавитьПКТЧ"))
    return read_manager_text(text + "\n" + helpers)


@pytest.mark.parametrize(
    "raw,expected",
    [
        ('КомпонентыОбмена.ВерсияФорматаОбмена >= "1.10"', None),
        ('КомпонентыОбмена.ВерсияФорматаОбмена < "1.2"', None),
        ('КомпонентыОбмена.ВерсияФорматаОбмена = "1.21.0"', False),
        (
            "ВерсияФорматаЧислом(КомпонентыОбмена.ВерсияФорматаОбмена) "
            '> ВерсияФорматаЧислом("1.9")',
            True,
        ),
        ('ВерсияФорматаЧислом("1.1.2") = ВерсияФорматаЧислом("1.1.2")', None),
        ('КомпонентыОбмена.ВерсияФорматаОбмена = "1.0.beta"', False),
        ("ИспользуетсяВерсия_1_17()", None),
        ("Не ИспользуетсяВерсия_1_17()", None),
        ('ИспользуетсяВерсия_1_17() И КомпонентыОбмена.ВерсияФорматаОбмена = "1.2"', False),
        ('ИспользуетсяВерсия_1_17() Или (КомпонентыОбмена.ВерсияФорматаОбмена = "1.21")', True),
        ('Не (КомпонентыОбмена.ВерсияФорматаОбмена = "1.2")', True),
    ],
)
def test_conditions(raw, expected):
    assert evaluate_condition(raw, "1.21", "send", numeric_helper_verified=True) is expected


def test_all_previous_branches_and_direction():
    call = "    ДобавитьПКО_Тест(ПравилаКонвертации);"
    text = BASE.replace(
        call,
        """    Если КомпонентыОбмена.ВерсияФорматаОбмена = "1.21" Тогда
        ДобавитьПКО_Тест(ПравилаКонвертации);
    ИначеЕсли КомпонентыОбмена.ВерсияФорматаОбмена = "1.20" Тогда
        ДобавитьПКО_Тест(ПравилаКонвертации);
    Иначе
        ДобавитьПКО_Тест(ПравилаКонвертации);
    КонецЕсли;""",
        1,
    )
    doc = document(text)
    app = Applicability.build(doc, ValidationProfile.build(None, "1.21"))
    branches = [g for g in doc.guards if g.branch in ("if", "elseif", "else")]
    assert [app.guard(g.entity_id, "send") for g in branches[:3]] == [True, False, False]
    assert app.evaluate(doc.pko[0], "send") is True
    doc = document(
        BASE.replace(
            call, '    Если НаправлениеОбмена = "Получение" Тогда\n' + call + "\n    КонецЕсли;", 1
        )
    )
    app = Applicability.build(doc, ValidationProfile.build(None))
    assert app.evaluate(doc.pko[0], "send") is False
    assert app.evaluate(doc.pko[0], "receive") is True


def test_indices_identical_to_public_resolver():
    schema = load_schema(DATA / "base.bin", locate_import=lambda _: DATA / "message.bin")
    profile = ValidationProfile.build(schema, "1.2")
    typ = schema.types[QName(schema.base_namespace, "Item")]
    for path, table in [("Code", None), ("Title", None), ("Quantity", "Lines"), ("Missing", None)]:
        assert profile.resolve(typ, path, table) == resolve_property(
            schema, typ.id, path, table=table
        )
    with pytest.raises(TypeError):
        cast(Any, profile.properties)["x"] = None
    assert profile.find_type("Item", "urn:inactive")[1] == "inactive_namespace"


def test_numeric_helper_requires_verified_body():
    helper = (DATA / "rules-version-helper.bsl").read_text(encoding="utf-8")
    call = "    ДобавитьПКО_Тест(ПравилаКонвертации);"
    guarded = BASE.replace(
        call,
        "    Если ВерсияФорматаЧислом(КомпонентыОбмена.ВерсияФорматаОбмена) "
        '> ВерсияФорматаЧислом("1.9") Тогда\n' + call + "\n    КонецЕсли;",
        1,
    )
    for body, expected in [(helper, True), (helper.replace("10000", "10"), None), ("", None)]:
        doc = document(guarded + "\n" + body)
        assert numeric_helper_verified(doc) is (expected is True)
        app = Applicability.build(doc, ValidationProfile.build(None, "1.21"))
        assert app.evaluate(doc.pko[0], "send") is expected
    assert (
        evaluate_condition(
            'ВерсияФорматаЧислом("1.21") > ВерсияФорматаЧислом("1.9")', "1.21", "send"
        )
        is None
    )


def test_field_and_named_condition_are_unknown():
    doc = document()
    app = Applicability.build(doc, ValidationProfile.build(None))
    assert app.field(replace(doc.pko[0].format_object, presence="ambiguous"), "send") is None
    assert (
        app.evaluate(
            replace(doc.pko[0].properties[0], condition_name="Проверить"), "send", doc.pko[0]
        )
        is None
    )


@pytest.mark.parametrize(
    "raw,expected",
    [
        ('КомпонентыОбмена.ВерсияФорматаОбмена = "1.8"', True),
        ('КомпонентыОбмена.ВерсияФорматаОбмена = "1.8.0"', False),
        ('КомпонентыОбмена.ВерсияФорматаОбмена <> "1.8.0"', True),
        ('КомпонентыОбмена.ВерсияФорматаОбмена < "1.10"', None),
        ('КомпонентыОбмена.ВерсияФорматаОбмена >= ВерсияФорматаЧислом("1.8")', None),
        ('ВерсияФорматаЧислом("1.8") = КомпонентыОбмена.ВерсияФорматаОбмена', None),
        ("ВерсияФорматаЧислом(КомпонентыОбмена.ВерсияФорматаОбмена) >= 10800", None),
        (
            "ВерсияФорматаЧислом(КомпонентыОбмена.ВерсияФорматаОбмена) "
            '< ВерсияФорматаЧислом("1.10")',
            True,
        ),
    ],
)
def test_version_comparison_requires_matching_semantics(raw, expected):
    assert evaluate_condition(raw, "1.8", "send", True) is expected


def test_direction_disjunction_uses_guards_and_nested_parent():
    call = "    ДобавитьПКО_Тест(ПравилаКонвертации);"
    text = BASE.replace(
        call,
        'Если НаправлениеОбмена = "Отправка" Или '
        'КомпонентыОбмена.ВерсияФорматаОбмена = "1.2" Тогда\n' + call + "\nКонецЕсли;",
        1,
    )
    doc = document(text)
    assert doc.rule_uses[0].direction == "send"
    app = Applicability.build(doc, ValidationProfile.build(None, "1.2"))
    assert app.evaluate(doc.pko[0], "receive") is True
    app = Applicability.build(doc, ValidationProfile.build(None, "1.3"))
    assert app.evaluate(doc.pko[0], "receive") is False
    assert app.evaluate(doc.pko[0], "send") is True
    text = BASE.replace(
        call,
        'Если КомпонентыОбмена.ВерсияФорматаОбмена = "1.1" Тогда\n'
        'Если КомпонентыОбмена.ВерсияФорматаОбмена = "1.2" Тогда\n'
        + call
        + "\nКонецЕсли;\nКонецЕсли;",
        1,
    )
    doc = document(text)
    app = Applicability.build(doc, ValidationProfile.build(None, "1.2"))
    assert app.evaluate(doc.pko[0], "send") is False
    assert app.evaluate(doc.pko[0], "receive") is False
