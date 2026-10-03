"""Ожидания заданы независимо от читателя, на вымышленных правилах."""

import hashlib
import io
from dataclasses import FrozenInstanceError
from pathlib import Path
from unittest.mock import patch

import pytest

from kd2_rules_mcp.ed import (
    EdFormatError,
    EdReadError,
    EdResourceLimitError,
    forms,
    read_manager,
    read_manager_text,
    reader,
)
from kd2_rules_mcp.ed.address import locate

DATA = Path(__file__).parent / "data/ed"


def fixture_text(version=2):
    return (DATA / f"manager_v{version}.bsl").read_text(encoding="utf-8")


def test_minimal_v2():
    doc = read_manager(DATA / "manager_v2.bsl")
    expected = {
        "pko": 2,
        "pod": 2,
        "pkpd": 1,
        "pks": 4,
        "pktch": 1,
        "values": 2,
        "search_sets": 1,
        "parameters": 1,
        "algorithms": 1,
        "handlers": 5,
        "dispatchers": 2,
        "unknown": 1,
    }
    assert {key: doc.counts[key] for key in expected} == expected
    assert doc.manager_version == 2
    product, order = doc.pko
    assert product.name == "Товар"
    assert product.configuration_object.value is not None
    assert product.configuration_object.value.reference_parts == (
        "Метаданные",
        "Справочники",
        "Товары",
    )
    assert product.format_object.value == "Catalog.Product"
    assert product.group_flag.value is True
    assert product.identification.value == "ПоПолямПоиска"
    assert order.identification.presence == "absent"
    assert product.properties[1].namespace == "urn:example:extra"
    assert product.properties[1].argument_presence == (True, True, True, True, False, True)
    assert product.properties[1].algorithm_flag == 1
    assert product.groups[0].properties[0].group_id == product.groups[0].entity_id
    assert product.search_sets[0].fields == ("Код", "Наименование")
    assert product.extensions == ("urn:example:extra",)
    assert doc.pod[0].clear_data.value is False
    assert doc.pod[0].used_pko[0].target_id == product.entity_id
    assert doc.parameters[0].default_source == "implicit"
    sending, receiving = doc.pkpd[0].mappings
    assert sending.configuration_value.raw == receiving.configuration_value.raw
    assert sending.format_value.literal_value == receiving.format_value.literal_value == "New"
    assert [sending.direction, receiving.direction] == ["send", "receive"]
    assert [u.direction for u in doc.rule_uses] == ["send", "receive", "both", "send"]
    assert len(doc.conversion.events) == 4
    assert not doc.diagnostics


def test_minimal_v3():
    doc = read_manager(DATA / "manager_v3.bsl")
    assert doc.manager_version == 3
    assert (doc.counts["pko"], doc.counts["pks"], doc.counts["pktch"], doc.counts["pkpd"]) == (
        1,
        2,
        1,
        1,
    )
    assert doc.parse_status == "complete"
    rule = doc.pko[0]
    assert rule.properties[0].condition_name == "Версия120"
    assert rule.groups[0].condition_name == "Версия120"
    assert rule.groups[0].properties[0].namespace == "urn:example:extra"
    routine = next(r for r in doc.routines if r.name == rule.procedure_name)
    assert routine.parameters[1].default is not None
    assert routine.parameters[2].default is not None
    assert routine.parameters[1].default.literal_value == ""
    assert routine.parameters[2].default.literal_value is False
    assert {g.guard_kind for g in doc.guards} >= {"direction", "condition_name", "headers_only"}
    assert doc.pkpd[0].mappings[0].guards
    assert [v.value for v in doc.conversion.format_version_mentions] == ["1.20.2"]


def test_interface_one_uses_old_rule_signature():
    doc = read_manager_text(fixture_text().replace('Возврат "2";', 'Возврат "1";', 1))
    assert doc.manager_version == 1
    assert len(doc.pko) == 2
    assert "unsupported_manager_version" not in {d.code for d in doc.diagnostics}


def test_deferred_algorithm_custom_arguments_and_direct_handler():
    doc = read_manager_text(fixture_text())
    routine = next(r for r in doc.routines if r.name == "ЗавершитьТовар")
    assert routine.roles == {"algorithm", "callback"}
    binding = next(b for b in doc.pko[0].events if b.event == "ПослеЗагрузкиВсехДанных")
    assert binding.target_id == routine.entity_id and binding.resolution == "resolved"
    case = doc.dispatcher_cases[0]
    assert [arg.raw for arg in case.arguments] == [
        "Параметры.Объект",
        "Параметры.КомпонентыОбмена.ПараметрыКонвертации",
    ]
    assert doc.dispatcher_cases[-1].returns is True
    assert routine.parameters[0].by_value is True
    assert routine.parameters[1].default is not None
    assert routine.parameters[1].default.literal_type == "undefined"
    direct = read_manager_text(fixture_text(3))
    assert direct.pko[0].events[0].resolution == "resolved"


def test_unknown_field_does_not_hide_next_statement_or_raw():
    text = fixture_text().replace(
        "ПравилоКонвертации.РучнаяВставка = ВычислитьЗначение();",
        "ПравилоКонвертации.РучнаяВставка = ВычислитьЗначение(); "
        'ДобавитьПКС(СвойстваШапки, "Вес", "Weight");',
    )
    doc = read_manager_text(text)
    assert doc.pko[0].properties[-1].format_property == "Weight"
    (unknown,) = doc.unknown
    assert unknown.reason == "unsupported_pko_statement"
    assert doc.coverage.classify_line(unknown.span.line_start) == "unknown"
    assert text[unknown.span.char_start : unknown.span.char_end] == unknown.raw_text
    assert doc.parse_status == "partial"


def test_repeated_fields_preserve_guards_and_unknown_expression():
    text = fixture_text().replace(
        'ПравилоКонвертации.ОбъектФормата = "Catalog.Product";',
        """
Если Флаг Тогда
ПравилоКонвертации.ОбъектФормата = "Первый";
Иначе
ПравилоКонвертации.ОбъектФормата = "Второй";
КонецЕсли;""",
    )
    doc = read_manager_text(text)
    field = doc.pko[0].format_object
    assert field.presence == "ambiguous" and field.value is None
    assert [a.literal_value for a in field.assignments] == ["Первый", "Второй"]
    assert field.assignments[0].guards != field.assignments[1].guards
    text = fixture_text().replace('"Catalog.Product"', "ПолучитьИмя()")
    field = read_manager_text(text).pko[0].format_object
    assert field.presence == "expression" and field.assignments[0].raw == "ПолучитьИмя()"


def test_group_does_not_leak_across_sibling_branches():
    text = fixture_text().replace(
        'ДобавитьПКС(СвойстваТЧ, "Цена", "Price", 0);',
        """
Если Флаг Тогда
СвойстваТЧ = ДобавитьПКТЧ(ПравилоКонвертации, "А", "A");
Иначе
ДобавитьПКС(СвойстваТЧ, "Цена", "Price", 0);
КонецЕсли;""",
    )
    doc = read_manager_text(text)
    assert "ambiguous_property_parent" in {u.reason for u in doc.unknown}


@pytest.mark.parametrize(
    "call",
    [
        "ДругойМодуль.ДобавитьПКО_Товар(ПравилаКонвертации);",
        "ДругойМодуль.ЗаполнитьПравилаКонвертацииОбъектов(НаправлениеОбмена, ПравилаКонвертации);",
    ],
)
def test_split_module_diagnostic(call):
    doc = read_manager_text(
        fixture_text().replace("ДобавитьПКО_Товар(ПравилаКонвертации);", call, 1)
    )
    assert "split_module_required" in {d.code for d in doc.diagnostics}
    assert doc.parse_status == "partial"


def test_generator_column_statement_is_a_known_form():
    """Писатель КД 3 сам добавляет колонку перед ПОД отправки — это не неизвестный фрагмент."""
    call = "        ДобавитьПОД_Товары(ПравилаОбработкиДанных);"
    preamble = (
        '        Если ПравилаОбработкиДанных.Колонки.Найти("ОчисткаДанных") = Неопределено Тогда\n'
        '            ПравилаОбработкиДанных.Колонки.Добавить("ОчисткаДанных");\n'
        "        КонецЕсли;\n"
    )
    base = read_manager_text(fixture_text())
    doc = read_manager_text(fixture_text().replace(call, preamble + call, 1))
    assert len(doc.unknown) == len(base.unknown)
    assert "unsupported_entrypoint_statement" not in {u.reason for u in doc.unknown}
    use = next(u for u in doc.rule_uses if u.target_name == "ДобавитьПОД_Товары")
    assert use.direction == "send"
    changed = fixture_text().replace(
        call, preamble.replace('Добавить("ОчисткаДанных")', 'Добавить("Другая")') + call, 1
    )
    assert "unsupported_entrypoint_statement" in {
        u.reason for u in read_manager_text(changed).unknown
    }


def test_custom_callback_and_unknown_routine():
    text = fixture_text().replace('"ПКО_Товар_ПриОтправкеДанных"', '"СвойМетод"')
    text += "\nПроцедура СвойМетод()\nКонецПроцедуры\nПроцедура Чужой()\nКонецПроцедуры"
    doc = read_manager_text(text)
    assert next(r for r in doc.routines if r.name == "СвойМетод").roles == {"handler", "callback"}
    assert [u.raw_text for u in doc.unknown if u.reason == "unknown_routine"] == [
        "Процедура Чужой()\nКонецПроцедуры"
    ]


@pytest.mark.parametrize("version", [2, 3])
def test_helper_body_compared_not_only_signature(version):
    helper = forms.helper_forms("ДобавитьПКС", version)[-1]
    text = fixture_text(version) + "\n" + helper
    assert "helper_semantics_unverified" not in {
        d.code for d in read_manager_text(text).diagnostics
    }
    changed = text.replace(
        "НоваяСтрока = РодительПКС.Добавить();", "НоваяСтрока = ДругаяТаблица.Добавить();"
    )
    assert "helper_semantics_unverified" in {d.code for d in read_manager_text(changed).diagnostics}


def test_immutable_raw_bom_crlf_and_coverage():
    text = "\ufeff" + fixture_text().replace("\n", "\r\n")
    doc = read_manager_text(text, file_id="source")
    source = doc.files[0]
    assert source.bom and source.newline == "\r\n"
    assert source.sha256 == hashlib.sha256(text.encode()).hexdigest()
    assert doc == read_manager_text(text, file_id="source")
    with pytest.raises(FrozenInstanceError):
        doc.__setattr__("manager_version", 999)
    cursor = 0
    for segment in doc.coverage.segments:
        assert segment.span.char_start == cursor
        cursor = segment.span.char_end
    assert cursor == len(source.text)
    assert sum(doc.coverage.counts.values()) == source.lines
    for entity in doc.entities():
        assert source.text[entity.span.char_start : entity.span.char_end] == entity.raw_text
    assert doc.coverage.coverage_ratio < doc.coverage.classified_ratio < 1
    algorithm = next(r for r in doc.routines if "algorithm" in r.roles)
    assert doc.coverage.classify_line(algorithm.span.line_start + 1) == "opaque_code"
    assert algorithm in locate(doc, algorithm.span.line_start + 1)


@pytest.mark.parametrize(
    "text",
    [
        "",
        "Текст = 1;",
        '"незакрытая',
        "Процедура ЗаполнитьПараметрыКонвертации(ПараметрыКонвертации)",
        "Процедура ЗаполнитьПараметрыКонвертации(ПараметрыКонвертации) КонецФункции",
    ],
)
def test_format_errors(text):
    with pytest.raises(EdFormatError):
        read_manager_text(text)


def test_unknown_version_and_missing_version():
    for value in ['"99"', "ПолучитьВерсию()"]:
        doc = read_manager_text(fixture_text().replace('"2";', value + ";", 1))
        assert doc.parse_status == "partial"
        assert "unsupported_manager_version" in {d.code for d in doc.diagnostics}


def test_io_encoding_and_limits():
    with patch.object(Path, "open", side_effect=PermissionError), pytest.raises(EdReadError):
        read_manager("missing.bsl")
    with patch.object(Path, "open", return_value=io.BytesIO(b"\xff")), pytest.raises(EdReadError):
        read_manager("not_utf8.bsl")
    with patch.object(reader, "MAX_BYTES", 5), pytest.raises(EdResourceLimitError):
        read_manager_text(fixture_text())


def test_computed_call_arguments_and_composite_expression_stay_unknown():
    text = (
        fixture_text()
        .replace('"Code");', "ВычислитьИмя());")
        .replace("ВычислитьЗначение();", "Первое() + Второе();")
    )
    doc = read_manager_text(text)
    assert {u.reason for u in doc.unknown} == {
        "unsupported_property_arguments",
        "unsupported_pko_statement",
    }
    assert doc.counts["pko"] == 2 and doc.counts["pks"] == 3
    assert doc.pko[0].status == "partial"


def test_direction_guard_does_not_claim_opaque_nested_condition():
    text = fixture_text().replace(
        "ДобавитьПКО_Заказ(ПравилаКонвертации);",
        "Если НепонятныйФлаг Тогда ДобавитьПКО_Заказ(ПравилаКонвертации); КонецЕсли;",
        1,
    )
    doc = read_manager_text(text)
    assert doc.rule_uses[-1].direction is None
    assert len(doc.rule_uses[-1].guards) == 2


def test_dispatcher_alias_resolves_deferred_callback():
    text = (
        fixture_text()
        .replace(
            'ПослеЗагрузкиВсехДанных = "ЗавершитьТовар"', 'ПослеЗагрузкиВсехДанных = "Псевдоним"'
        )
        .replace('ИмяПроцедуры = "ЗавершитьТовар"', 'ИмяПроцедуры = "Псевдоним"')
    )
    doc = read_manager_text(text)
    binding = next(b for b in doc.pko[0].events if b.event == "ПослеЗагрузкиВсехДанных")
    assert binding.target_name == "Псевдоним" and binding.resolution == "resolved"
    assert binding.target_id == next(
        r.entity_id for r in doc.routines if r.name == "ЗавершитьТовар"
    )


def test_rule_signatures_and_arguments_are_not_guessed():
    text = (
        fixture_text()
        .replace(
            "Процедура ДобавитьПКО_Товар(ПравилаКонвертации)",
            "Процедура ДобавитьПКО_Товар(ДругойПараметр)",
        )
        .replace("ДобавитьПКО_Товар(ПравилаКонвертации);", "ДобавитьПКО_Товар(ДругаяТаблица);", 1)
    )
    doc = read_manager_text(text)
    assert {u.reason for u in doc.unknown} >= {
        "unsupported_rule_signature",
        "unsupported_rule_arguments",
    }


def test_reordered_independent_fields_and_routines_mixed_case():
    text = (
        fixture_text().replace("Процедура", "пРОЦЕДУРА").replace("КонецПроцедуры", "КонецпРОЦЕДУРЫ")
    )
    text = text.replace('ПравилоКонвертации.ИмяПКО = "Товар";', "").replace(
        'ПравилоКонвертации.ОбъектФормата = "Catalog.Product";',
        'ПравилоКонвертации.ОбъектФормата = "Catalog.Product"; '
        'ПравилоКонвертации.ИмяПКО = "Товар";',
    )
    doc = read_manager_text(text)
    assert doc.pko[0].name == "Товар"
    assert doc.counts["handlers"] == 5


def test_link_gaps_keep_resolution_without_read_diagnostics():
    """Обрыв связи больше не диагностика чтения: модуль без иных проблем — complete."""
    text = fixture_text().replace(
        'ПравилоКонвертации.ПриОтправкеДанных = "ПКО_Товар_ПриОтправкеДанных";',
        'ПравилоКонвертации.ПриОтправкеДанных = "НетМетода";',
        1,
    )
    text = text.replace(
        'ПравилоОбработки.ИспользуемыеПКО.Добавить("Товар");',
        'ПравилоОбработки.ИспользуемыеПКО.Добавить("НетПКО");',
        1,
    )
    text = text.replace(
        "ДобавитьПКО_Заказ(ПравилаКонвертации);",
        "ДобавитьПКО_НетПроцедуры(ПравилаКонвертации);",
        1,
    )
    text += """
#Область Алгоритмы
Процедура Двойной()
КонецПроцедуры
Процедура Двойной()
КонецПроцедуры
#КонецОбласти
"""
    text = text.replace(
        'ПравилоКонвертации.ПриКонвертацииДанныхXDTO = "ПКО_Товар_ПриКонвертацииДанныхXDTO";',
        'ПравилоКонвертации.ПриКонвертацииДанныхXDTO = "Двойной";',
        1,
    )
    doc = read_manager_text(text)
    missing = next(item for item in doc.pko[0].events if item.target_name == "НетМетода")
    ambiguous = next(item for item in doc.pko[0].events if item.target_name == "Двойной")
    assert missing.resolution == "missing" and missing.target_id is None
    assert ambiguous.resolution == "ambiguous" and ambiguous.target_id is None
    ref = doc.pod[0].used_pko[0]
    assert ref.name == "НетПКО" and ref.resolution == "missing" and ref.target_id is None
    use = next(item for item in doc.rule_uses if item.target_name == "ДобавитьПКО_НетПроцедуры")
    assert use.rule_id is None
    assert not {
        "handler_missing",
        "handler_ambiguous",
        "pko_reference_unresolved",
        "rule_reference_unresolved",
    } & {item.code for item in doc.diagnostics}
    # В v2 остаётся неизвестный оператор РучнаяВставка; сами связи чтение не портят.
    without_unknown = text.replace(
        "    ПравилоКонвертации.РучнаяВставка = ВычислитьЗначение();\n", ""
    )
    clean = read_manager_text(without_unknown)
    assert clean.unknown == ()
    assert clean.parse_status == "complete"
    assert not {
        "handler_missing",
        "handler_ambiguous",
        "pko_reference_unresolved",
        "rule_reference_unresolved",
    } & {item.code for item in clean.diagnostics}


def test_tag_ranges_and_conversion_header():
    text = "// Менеджер обмена через универсальный формат (Пример от 03.10.2026)\n" + fixture_text()
    doc = read_manager_text(text)
    assert doc.conversion.title == "Пример"
    assert doc.conversion.generated_at_raw == "03.10.2026"
    (tag,) = doc.tags
    assert tag.name == "Товар"
    assert text[tag.span.char_start : tag.span.char_end] == tag.raw_text
    broken = read_manager_text(text.replace("//-- Товар", "//-- Другое"))
    assert {d.code for d in broken.diagnostics} >= {"unbalanced_tag", "unclosed_context"}
    with patch.object(reader, "MAX_LINES", 1), pytest.raises(EdResourceLimitError):
        read_manager_text(fixture_text())


def test_empty_predefined_procedure_is_a_valid_form():
    """Процедура ПКПД без единого правила читается: раньше падала на пустом списке блоков."""
    text = (DATA / "checks_base.bsl").read_text(encoding="utf-8")
    assert "ЗаполнитьПравилаКонвертацииПредопределенныхДанных" not in text
    text += (
        "\nПроцедура ЗаполнитьПравилаКонвертацииПредопределенныхДанных("
        "НаправлениеОбмена, ПравилаКонвертации) Экспорт\nКонецПроцедуры\n"
    )
    doc = read_manager_text(text)
    assert doc.pkpd == ()
    assert doc.counts["pko"] == 1
