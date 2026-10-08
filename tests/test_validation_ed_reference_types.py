"""Ключевая ссылка составного типа с одним ПКО, как в дефекте живой выгрузки."""

from dataclasses import replace
from types import MappingProxyType

import pytest

from kd_rules_mcp.ed.address import build_addresses
from kd_rules_mcp.ed.schema import load_schema
from kd_rules_mcp.ed.schema.profile import ValidationProfile
from kd_rules_mcp.validation.ed_schema import validate_schema
from tests.test_ed_profile import BASE, DATA, document
from tests.test_validation_ed_structure import snapshot


def reference_case(*, algorithm=0, target="ТолькоФизлицо", types=None, handler=False):
    text = BASE.replace(
        'ДобавитьПКС(СвойстваШапки, "Код", "Код", 0);',
        f'ДобавитьПКС(СвойстваШапки, "Код", "Код", {algorithm}, "{target}");'
        + ('\nПравилоКонвертации.ПриОтправкеДанных = "Фильтр";' if handler else ""),
    )
    doc = document(text)
    source = doc.pko[0]
    target_rule = replace(source, entity_id="physical-person", name="ТолькоФизлицо")
    snap = snapshot()
    owner = snap.objects[("справочник", "тест")]
    prop = owner.property("Код")[0]
    updated = replace(
        prop,
        types=types or ("СправочникСсылка.ФизическиеЛица", "СправочникСсылка.Организации"),
    )
    owner = replace(owner, properties={**owner.properties, ("код", ""): (updated,)})
    person = replace(owner, id=100, type_name="СправочникСсылка.ФизическиеЛица")
    # Цель использует тот же адрес метаданных, но отдельный ПКО и тип снимка цели.
    from kd_rules_mcp.ed.model import Expr, Field

    target_rule = replace(
        target_rule,
        configuration_object=Field(
            value=Expr(
                "Метаданные.Справочники.ФизическиеЛица",
                source.span,
                reference_parts=("Метаданные", "Справочники", "ФизическиеЛица"),
            )
        ),
        properties=(),
        events=(),
    )
    doc = replace(
        doc,
        pko=(source, target_rule),
        rule_uses=(
            *doc.rule_uses,
            *(
                replace(u, entity_id=u.entity_id + "-target", rule_id=target_rule.entity_id)
                for u in doc.rule_uses
                if u.rule_id == source.entity_id
            ),
        ),
    )
    snap = replace(
        snap,
        objects=MappingProxyType(
            {
                **snap.objects,
                ("справочник", "тест"): owner,
                ("справочник", "физическиелица"): person,
            }
        ),
    )
    return doc, snap


def report_case(**kwargs):
    doc, snap = reference_case(**kwargs)
    schema = load_schema(DATA / "validation.bin")
    return validate_schema(
        doc, schema, build_addresses(doc), ValidationProfile.build(schema, "1.2"), snap
    )


@pytest.mark.parametrize("handler", [False, True])
def test_live_composite_key_reference_warns_even_with_send_handler(handler):
    report = report_case(handler=handler)
    issues = [i for i in report.issues if i.check == "ed.schema.reference_type_partial"]
    assert len(issues) == 1
    issue = issues[0]
    assert issue.level.value == "предупреждение"
    assert issue.address == "ПКО/Тест/ПКС/Код"
    assert "Ключевая" in issue.message
    assert "ФизическиеЛица" in issue.message and "Организации" in issue.message
    assert "алгоритмическую ПКС" in issue.message and "XDTO:1199–1218" in issue.message


@pytest.mark.parametrize(
    "kwargs",
    [{"algorithm": 1}, {"target": "Выбор"}, {"types": ("СправочникСсылка.ФизическиеЛица",)}],
)
def test_algorithm_predefined_rule_and_single_type_are_not_partial(kwargs):
    assert not any(
        i.check == "ed.schema.reference_type_partial" for i in report_case(**kwargs).issues
    )
