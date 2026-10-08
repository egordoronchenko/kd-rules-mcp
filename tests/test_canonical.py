"""Правила канонической формы К1–К7 на ручных примерах."""

import pytest

from kd_rules_mcp.errors import RulesFormatError
from kd_rules_mcp.kd2.canonical import canonical_diff, canonical_form


def same(left: str, right: str) -> bool:
    return canonical_diff(canonical_form(left), canonical_form(right)) == []


def test_k1_attribute_order_is_ignored() -> None:
    assert same('<П><Источник Имя="А" Вид="Б"/></П>', '<П><Источник Вид="Б" Имя="А"/></П>')


def test_k1_attribute_values_still_matter() -> None:
    assert not same('<П><Источник Имя="А"/></П>', '<П><Источник Имя="Б"/></П>')


def test_k2_indentation_between_elements_is_ignored() -> None:
    assert same("<П>\r\n\t<Код>1</Код>\r\n</П>", "<П><Код>1</Код></П>")


def test_k2_leaf_text_whitespace_is_significant() -> None:
    assert not same("<П><Ид>abc    </Ид></П>", "<П><Ид>abc</Ид></П>")


def test_k2_text_markers_between_elements_are_significant() -> None:
    marked = "<С>\n\t<П><Код>1</Код></П>\n\t//bt_N\n\t<П><Код>2</Код></П>\n\t//bt_K\n</С>"
    same_marks = "<С><П><Код>1</Код></П>//bt_N<П><Код>2</Код></П>//bt_K</С>"
    moved = "<С><П><Код>1</Код></П><П><Код>2</Код></П>//bt_N//bt_K</С>"
    plain = "<С><П><Код>1</Код></П><П><Код>2</Код></П></С>"
    assert same(marked, same_marks)
    assert not same(marked, moved)
    assert not same(marked, plain)


def test_k3_newlines_in_text_are_normalized() -> None:
    crlf = "<П><Текст>А = 1;\r\nБ = 2;</Текст></П>"
    lf = "<П><Текст>А = 1;\nБ = 2;</Текст></П>"
    char_ref = "<П><Текст>А = 1;&#13;\nБ = 2;</Текст></П>"
    assert same(crlf, lf)
    assert same(char_ref, lf)


def test_k4_empty_element_equals_absent() -> None:
    assert same("<П><Код>1</Код><ПослеЗагрузки></ПослеЗагрузки></П>", "<П><Код>1</Код></П>")
    assert same("<П><Код>1</Код><Значения/></П>", "<П><Код>1</Код></П>")


def test_k4_whitespace_only_element_equals_absent() -> None:
    container = "<П><Код>1</Код><ПравилаВыгрузкиДанных>\r\n\t</ПравилаВыгрузкиДанных></П>"
    assert same(container, "<П><Код>1</Код></П>")


def test_k4_empty_element_with_attributes_is_kept() -> None:
    assert not same('<П><Источник Имя="" Вид=""/></П>', "<П/>")


def test_k5_false_flag_equals_absent() -> None:
    assert same("<П><НеЗамещать>false</НеЗамещать></П>", "<П/>")
    assert not same("<П><НеЗамещать>true</НеЗамещать></П>", "<П/>")


def test_k5_does_not_touch_flags_written_always() -> None:
    # Авторегистрация пишется через ДобавитьЭлемент и выводится и при «ложь».
    assert not same("<Э><Авторегистрация>false</Авторегистрация></Э>", "<Э/>")


def test_k6_false_flag_attribute_equals_absent() -> None:
    assert same('<Г Отключить="false"><Код>1</Код></Г>', "<Г><Код>1</Код></Г>")
    assert not same('<Г Отключить="true"><Код>1</Код></Г>', "<Г><Код>1</Код></Г>")


def test_k7_zero_number_equals_absent() -> None:
    assert same("<П><Порядок>0</Порядок></П>", "<П/>")
    assert not same("<П><Порядок>50</Порядок></П>", "<П/>")


def test_k8_field_order_is_ignored() -> None:
    kd_order = "<П><Код>1</Код><ПослеЗагрузки>А</ПослеЗагрузки><НеЗамещать>true</НеЗамещать></П>"
    other = "<П><НеЗамещать>true</НеЗамещать><Код>1</Код><ПослеЗагрузки>А</ПослеЗагрузки></П>"
    assert same(kd_order, other)


def test_k8_list_item_order_matters() -> None:
    first = "<С><Свойство><Код>1</Код></Свойство><Свойство><Код>2</Код></Свойство></С>"
    second = "<С><Свойство><Код>2</Код></Свойство><Свойство><Код>1</Код></Свойство></С>"
    assert not same(first, second)


def test_k8_rule_and_group_interleaving_matters() -> None:
    first = "<С><Правило><Код>1</Код></Правило><Группа><Код>2</Код></Группа></С>"
    second = "<С><Группа><Код>2</Код></Группа><Правило><Код>1</Код></Правило></С>"
    assert not same(first, second)


def test_comments_are_ignored() -> None:
    assert same("<П><!-- заметка --><Код>1</Код></П>", "<П><Код>1</Код></П>")


def test_diff_reports_path_with_code() -> None:
    left = canonical_form("<Р><Правило><Код>ПКО1</Код><Порядок>50</Порядок></Правило></Р>")
    right = canonical_form("<Р><Правило><Код>ПКО1</Код><Порядок>100</Порядок></Правило></Р>")
    diff = canonical_diff(left, right)
    assert len(diff) == 1
    assert "Правило[ПКО1]" in diff[0]
    assert "'50'" in diff[0] and "'100'" in diff[0]


def test_invalid_xml_raises_format_error() -> None:
    with pytest.raises(RulesFormatError, match="не является корректным XML"):
        canonical_form("<П><Код></П>")
