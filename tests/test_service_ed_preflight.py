"""Все обязательные ключи попадают в проверку данных, даже при ShowError."""

from dataclasses import replace

import pytest

from kd_rules_mcp.ed.schema import load_schema
from kd_rules_mcp.ed.schema.profile import ValidationProfile
from kd_rules_mcp.service.ed_preflight import key_data_instruction
from tests.test_ed_profile import DATA, document
from tests.test_validation_ed_structure import snapshot


@pytest.mark.parametrize("fill_check", ["ShowError", "DontCheck", ""])
def test_required_key_queries_ignore_fill_check(fill_check):
    snap = snapshot()
    owner = snap.objects[("справочник", "тест")]
    prop = owner.property("Код")[0]
    changed = replace(prop, qualifiers={**prop.qualifiers, "fill_check": fill_check})
    owner = replace(owner, properties={**owner.properties, ("код", ""): (changed,)})
    snap = replace(snap, objects={**snap.objects, ("справочник", "тест"): owner})
    schema = load_schema(DATA / "validation.bin")
    text = key_data_instruction(document(), ValidationProfile.build(schema, "1.2"), snap)
    assert "Проверьте данные перед первым обменом" in text
    assert "ShowError" in text and "ОбменДанными.Загрузка = Истина" in text
    assert "ИЗ Справочник.Тест" in text and "Данные.Код" in text
    assert "ЗначениеЗаполнено(Выборка.ПроверяемоеЗначение)" in text
    assert "Записать(" not in text


def test_optional_key_and_unmapped_source_are_not_inferred(tmp_path):
    schema_path = tmp_path / "optional.bin"
    schema_path.write_text(
        (DATA / "validation.bin")
        .read_text("utf-8")
        .replace('name="Код" type="xs:string"', 'name="Код" type="xs:string" lowerBound="0"'),
        "utf-8",
    )
    schema = load_schema(schema_path)
    assert (
        key_data_instruction(document(), ValidationProfile.build(schema, "1.2"), snapshot()) == ""
    )
