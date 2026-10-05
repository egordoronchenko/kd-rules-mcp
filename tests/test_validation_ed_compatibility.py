"""Направление ограничений: отправка строки блокирует выгрузку, число остаётся сведениями."""

from dataclasses import replace

import pytest

from kd2_rules_mcp.ed.schema import load_schema
from kd2_rules_mcp.ed.schema.profile import ValidationProfile
from kd2_rules_mcp.validation.ed_compatibility import compatibility
from tests.test_ed_profile import DATA
from tests.test_validation_ed_structure import snapshot


@pytest.mark.parametrize(
    "family,qualifiers,facets,direction,warning,consequence",
    [
        (
            "Строка",
            {"string_length": 5000},
            "<maxLength>1024</maxLength>",
            "send",
            True,
            "значение длиннее 1024 символов не выгрузится",
        ),
        (
            "Строка",
            {"string_length": 0},
            "<maxLength>1024</maxLength>",
            "send",
            True,
            "значение длиннее 1024 символов не выгрузится",
        ),
        (
            "Строка",
            {"string_length": 10},
            "<minLength>2</minLength>",
            "send",
            True,
            "значение короче 2 символов не выгрузится",
        ),
        (
            "Строка",
            {"string_length": 10},
            "<maxLength>1024</maxLength>",
            "receive",
            False,
            "при загрузке возможны усечение строки или отказ записи",
        ),
        (
            "Строка",
            {"string_length": 10, "string_fixed": True},
            "",
            "send",
            False,
            "фиксированная длина реквизита требует проверки перед отправкой",
        ),
        (
            "Число",
            {"number_length": 5, "number_precision": 2},
            "<maxInclusive>99</maxInclusive><fractionDigits>1</fractionDigits>",
            "send",
            False,
            "значение больше 99 не выгрузится",
        ),
        (
            "Число",
            {"number_length": 5, "number_precision": 2},
            "<fractionDigits>1</fractionDigits>",
            "send",
            False,
            "значение с дробной частью более 1 знаков не выгрузится",
        ),
        (
            "Число",
            {"number_length": 5, "number_precision": 2},
            "<maxInclusive>9999</maxInclusive><fractionDigits>3</fractionDigits>",
            "receive",
            False,
            "при загрузке возможны потеря точности числа или отказ записи",
        ),
    ],
)
def test_r4_directional_ranges(
    tmp_path, family, qualifiers, facets, direction, warning, consequence
):
    primitive = "string" if family == "Строка" else "decimal"
    path = tmp_path / "ranges.bin"
    text = (DATA / "validation.bin").read_text(encoding="utf-8")
    path.write_text(
        text.replace('name="Код" type="xs:string"', 'name="Код" type="t:Ограниченный"').replace(
            "</package>",
            f'<valueType name="Ограниченный" base="xs:{primitive}">{facets}</valueType></package>',
        ),
        encoding="utf-8",
    )
    profile = ValidationProfile.build(load_schema(path), "1.2", direction)
    typ, _ = profile.find_type("Справочник.Тест")
    assert typ is not None
    prop = profile.properties[profile.resolve(typ, "Код").property_ids[0]]
    attribute = replace(
        snapshot().objects[("справочник", "тест")].property("Код")[0],
        types=(family,),
        qualifiers=qualifiers,
    )
    result = compatibility(profile, prop, attribute, direction)
    assert result.compatible and result.value_range
    assert result.value_range_warning is warning
    assert consequence in result.value_range_consequence
    assert "реквизита=" in result.value_range_consequence
    assert "формата=" in result.value_range_consequence
    if warning:
        assert "XDTO:6103–6109" in result.value_range_consequence
