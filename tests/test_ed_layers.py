"""Чтение слоя расширений: комплекты §2.3, грамматика и честные пропуски."""

from pathlib import Path

import pytest

from kd2_rules_mcp.ed.errors import EdFormatError
from kd2_rules_mcp.ed.layer_model import (
    Certainty,
    EffectiveRuleView,
    EntityState,
    LayerDescriptor,
    LayerStatus,
    OperationKind,
    rule_view,
)
from kd2_rules_mcp.ed.layer_reader import read_extension_text
from kd2_rules_mcp.ed.layers import compose_manager, read_layers
from kd2_rules_mcp.ed.model import ObjectRule, Parameter, ParseStatus

ROOT = Path(__file__).resolve().parent / "data" / "ed" / "layers"
HELPERS = frozenset({"добавитьпкс", "добавитьпктч"})

AUTHORING_V2 = """\
// Сформировано из проверенных операций прямых ПКС. Изменять через повторное порождение комплекта.
// Область действия: этот менеджер, указанные направления; карта версий не изменяется.
&После("ЗаполнитьПравилаКонвертацииОбъектов")
Процедура доп_ЗаполнитьПравилаКонвертацииОбъектов(НаправлениеОбмена, ПравилаКонвертации)

	Если НаправлениеОбмена = "Отправка" Тогда
		Правило = ПравилаКонвертации.Найти("Товар_Отправка", "ИмяПКО");
		Если Правило <> Неопределено Тогда
			Свойство = Правило.Свойства.Найти("Комментарий", "СвойствоФормата");
			Если Свойство = Неопределено Тогда
				ДобавитьПКС(Правило.Свойства, "доп_Заметка", "Комментарий");
			КонецЕсли;
			Свойство = Правило.Свойства.Найти("ВнешнийКод", "СвойствоФормата");
			Если Свойство = Неопределено Тогда
				ДобавитьПКС(Правило.Свойства, "доп_Код", "ВнешнийКод");
			КонецЕсли;
		КонецЕсли;
	КонецЕсли;

КонецПроцедуры
"""

AUTHORING_V3 = """\
// Сформировано из проверенных операций прямых ПКС. Изменять через повторное порождение комплекта.
// Заголовочный проход не изменяет свойства правил.
&После("ЗаполнитьПравилаКонвертацииОбъектов")
Процедура доп_ЗаполнитьПравилаКонвертацииОбъектов(\
КомпонентыОбмена, ПравилаКонвертации, ТолькоЗаголовки)

	Если Не ТолькоЗаголовки Тогда
		Если КомпонентыОбмена.НаправлениеОбмена = "Получение" Тогда
			Правило = ПравилаКонвертации.Найти("Товар", "ИмяПКО");
			Если Правило <> Неопределено Тогда
				Свойство = Правило.Свойства.Найти("Комментарий", "СвойствоФормата");
				Если Свойство = Неопределено Тогда
					ДобавитьПКС(Правило.Свойства, "доп_Заметка", "Комментарий");
				КонецЕсли;
			КонецЕсли;
		КонецЕсли;
	КонецЕсли;

КонецПроцедуры
"""

PILOT_SHAPE = """\
&После("ЗаполнитьПравилаКонвертацииОбъектов")
Процедура доп_ЗаполнитьПравилаКонвертацииОбъектов(НаправлениеОбмена, ПравилаКонвертации)
	Если НаправлениеОбмена <> "Отправка" Тогда
		Возврат;
	КонецЕсли;
	Правило = ПравилаКонвертации.Найти("Изделие", "ИмяПКО");
	Если Правило = Неопределено Тогда
		Возврат;
	КонецЕсли;
	ДобавитьПКС(Правило.Свойства, "доп_Метка", "Примечание");
КонецПроцедуры
"""


@pytest.fixture(scope="module")
def demo():
    return read_layers(ROOT / "base")


@pytest.fixture(scope="module")
def kit_a():
    return read_layers(ROOT / "base", [ROOT / "a"])


@pytest.fixture(scope="module")
def kit_b():
    return read_layers(ROOT / "base", [ROOT / "b"])


@pytest.fixture(scope="module")
def kit_v3():
    return read_layers(ROOT / "v3" / "base", [ROOT / "v3" / "ext"])


def _layer(ordinal: int, name: str) -> LayerDescriptor:
    return LayerDescriptor(f"L0{ordinal}-{name}", ordinal, name, "mem", None, "")


def _reading(
    document, text: str, *, ordinal: int = 1, name: str = "ДемоB", version: int | None = None
):
    return read_extension_text(
        text,
        layer=_layer(ordinal, name),
        version=document.manager_version if version is None else version,
        helpers=HELPERS,
        targets={item.name.casefold(): item for item in document.routines},
    )


def _overlay(document, text: str, **kwargs):
    reading = _reading(document, text, **kwargs)
    base_layer = LayerDescriptor("base", 0, "Демо", "base", None, "")
    return compose_manager(
        document,
        readings=[reading],
        layers=(
            base_layer,
            reading and _layer(kwargs.get("ordinal", 1), kwargs.get("name", "ДемоB")),
        ),
    )


def _send(layered, headers_only: bool = False):
    return next(
        context
        for context in layered.contexts
        if context.direction == "send" and context.headers_only is headers_only
    )


def _receive(layered, headers_only: bool = False):
    return next(
        context
        for context in layered.contexts
        if context.direction == "receive" and context.headers_only is headers_only
    )


def _pks(context) -> list[tuple[str, str]]:
    return [
        (rule.name, prop.format_property)
        for rule in rule_view(context).pko
        for prop in rule.properties
    ]


def _reasons(layered) -> set[str]:
    return {skip.reason for skip in layered.skipped}


def test_metadata_adopted_native_and_plan_without_module(demo, kit_a):
    assert demo.status == LayerStatus.COMPLETE
    assert {item.name for item in demo.layers} >= {"Демо"}
    native = ROOT / "a" / "CommonModules" / "ДопМенеджер.xml"
    assert "ObjectBelonging" not in native.read_text(encoding="utf-8")
    plan = read_layers(ROOT / "base", [ROOT / "v3" / "meta" / "plan-gap"])
    assert plan.status == LayerStatus.COMPLETE
    assert "missing_module" not in _reasons(plan)
    assert kit_a.contexts[0].manager_name == "ДопМенеджер"


def test_metadata_uuid_mismatch_does_not_apply_hooks(demo):
    layered = read_layers(ROOT / "base", [ROOT / "v3" / "meta" / "mismatch"])
    assert layered.status == LayerStatus.PARTIAL
    assert "identity_mismatch" in _reasons(layered)
    assert ("Товар", "Extra") not in _pks(_send(layered))
    assert [prop.format_property for prop in demo.base.pko[0].properties] == ["Description"]


def test_metadata_missing_child_and_missing_module_are_distinct():
    missing_child = read_layers(ROOT / "base", [ROOT / "v3" / "meta" / "missing-child"])
    missing_module = read_layers(ROOT / "base", [ROOT / "v3" / "meta" / "missing-module"])
    assert missing_child.status == LayerStatus.PARTIAL
    assert "missing_metadata" in _reasons(missing_child)
    assert "missing_module" not in _reasons(missing_child)
    assert missing_module.status == LayerStatus.PARTIAL
    assert "missing_module" in _reasons(missing_module)
    assert "missing_metadata" not in _reasons(missing_module)


def test_metadata_corrupt_xml_is_a_skip_not_an_exception():
    broken_object = read_layers(ROOT / "base", [ROOT / "v3" / "meta" / "bad-object"])
    assert broken_object.status == LayerStatus.PARTIAL
    assert "metadata_xml" in _reasons(broken_object)
    broken_config = read_layers(ROOT / "base", [ROOT / "v3" / "meta" / "bad-config"])
    assert broken_config.status == LayerStatus.PARTIAL
    assert "metadata_xml" in _reasons(broken_config)
    view = rule_view(_send(broken_config))
    assert view.certainty == Certainty.UNKNOWN
    with pytest.raises(EdFormatError):
        read_layers(ROOT / "v3" / "meta" / "absent-base")


def test_metadata_bom_is_read(demo):
    layered = read_layers(ROOT / "base", [ROOT / "v3" / "meta" / "bom"])
    source = next(item for item in layered.source_files if item.bom)
    assert source.text.startswith("&После")
    assert ("Товар", "Extra") in _pks(_send(layered))
    assert demo.base.files[0].bom is False


def test_module_without_hooks_adds_nothing():
    layered = read_layers(ROOT / "base", [ROOT / "v3" / "meta" / "plain"])
    assert layered.status == LayerStatus.COMPLETE
    assert layered.hooks == ()
    assert ("Товар", "Extra") not in _pks(_send(layered))


def test_kit_a_replaces_manager_and_keeps_base_document(demo, kit_a):
    assert kit_a.status == LayerStatus.COMPLETE
    overwritten = [entry for entry in kit_a.map_entries if entry.state == "overwritten"]
    effective = [entry for entry in kit_a.map_entries if entry.state == "effective"]
    assert len(overwritten) == 4
    assert {entry.manager_name for entry in overwritten} == {"МенеджерДемо"}
    assert len(effective) == 4
    assert {entry.manager_name for entry in effective} == {"ДопМенеджер"}
    assert {(entry.role, entry.key) for entry in effective} == {
        ("plan", "1.20"),
        ("plan", "1.21"),
        ("without_node", "1.20"),
        ("without_node", "1.21"),
    }
    send = _send(kit_a)
    view = rule_view(send)
    assert send.manager_name == "ДопМенеджер"
    assert send.manager_layer_id == "L01-ДемоA"
    assert len(view.pko) == 1
    assert len(view.pod) == 1
    assert sum(len(rule.properties) for rule in view.pko) == 1
    assert len(view.parameters) == 1
    assert "ДопМенеджер" in send.entities[0].origins[0].path
    assert "МенеджерДемо" in kit_a.base.files[0].path
    assert kit_a.base.files[0].sha256 == demo.base.files[0].sha256
    assert kit_a.base.files[0].text == demo.base.files[0].text
    assert kit_a.base.coverage == demo.base.coverage
    assert kit_a.base.parse_status == ParseStatus.PARTIAL
    assert [prop.format_property for prop in kit_a.base.pko[0].properties] == ["Description"]


def test_kit_b_counts_origins_and_base_are_unchanged(demo, kit_b):
    assert kit_b.status == LayerStatus.COMPLETE
    assert kit_b.skipped == ()
    send = rule_view(_send(kit_b))
    receive = rule_view(_receive(kit_b))
    assert len(send.pko) == 2
    assert len(send.pod) == 2
    assert sum(len(rule.properties) for rule in send.pko) == 3
    assert len(receive.pko) == 1
    assert len(receive.pod) == 1
    assert sum(len(rule.properties) for rule in receive.pko) == 2
    assert len(send.pkpd) == 1
    assert len(send.pkpd[0].mappings) == 2
    assert {item.direction for item in send.pkpd[0].mappings} == {"send", "receive"}
    parameter = send.parameters[0]
    assert parameter.name == "РежимДемо"
    assert parameter.default is not None
    assert parameter.default.literal_value is True
    tip = next(
        item
        for item in _send(kit_b).entities
        if isinstance(item.payload, ObjectRule) and item.payload.name == "Товар"
    )
    assert tip.state == EntityState.CHANGED
    assert tip.payload is not None
    assert tip.payload.span == kit_b.base.pko[0].span
    assert tip.payload.raw_text == kit_b.base.pko[0].raw_text
    old = next(prop for prop in tip.payload.properties if prop.format_property == "Description")
    new = next(prop for prop in tip.payload.properties if prop.format_property == "Code")
    assert old.span.file_id == "module"
    assert "ДемоB" in new.span.file_id or "layers" in new.span.file_id.replace("\\", "/")
    comment = next(
        prop
        for rule in send.pko
        if rule.name == "ДопЗаказ"
        for prop in rule.properties
        if prop.format_property == "Comment"
    )
    assert comment.algorithm_flag == 1
    assert comment.configuration_property == ""
    names = (
        {rule.name for rule in send.pko}
        | {rule.name for rule in send.pod}
        | {rule.name for rule in send.pkpd}
    )
    assert names == {"Товар", "ДопЗаказ", "Товары", "ДопЗаказы", "ДопСостояния"}
    chains = {item.target_name: item.resolution for item in _send(kit_b).dispatch_chains}
    assert chains["Доп_Заказ_Отправка"] == "call"
    assert chains["ПКО_Товар_ПриОтправкеДанных"] == "call"
    changed = next(item for item in _send(kit_b).entities if isinstance(item.payload, Parameter))
    assert changed.state == EntityState.CHANGED
    assert changed.changes[0].before is not None
    assert changed.changes[0].before.value is False
    assert changed.changes[0].after is not None
    assert changed.changes[0].after.value is True
    assert kit_b.base.files[0].sha256 == demo.base.files[0].sha256
    assert kit_b.base.coverage == demo.base.coverage
    assert len(kit_b.base.parameters) == 0


def test_authoring_forms_are_fully_read(demo, kit_v3):
    v2 = _reading(demo.base, AUTHORING_V2)
    v3 = _reading(kit_v3.base, AUTHORING_V3, version=3)
    assert v2.skips == ()
    assert v3.skips == ()
    assert all(op.resolution != "unknown" for op in (*v2.operations, *v3.operations))
    kinds = {pred.kind for op in v2.operations if op.kind == OperationKind.ADD for pred in op.preds}
    assert {"direction_is", "rule_found", "property_absent"} <= kinds
    kinds_v3 = {
        pred.kind for op in v3.operations if op.kind == OperationKind.ADD for pred in op.preds
    }
    assert {"not_headers", "direction_is", "rule_found", "property_absent"} <= kinds_v3
    overlaid = _overlay(demo.base, AUTHORING_V2)
    assert overlaid.status == LayerStatus.COMPLETE
    assert ("Товар", "Комментарий") not in _pks(_send(overlaid))
    pilot = _overlay(demo.base, PILOT_SHAPE)
    assert pilot.skipped == ()
    assert pilot.status == LayerStatus.COMPLETE


def test_pilot_shape_adds_only_on_understood_branch(demo):
    text = PILOT_SHAPE.replace('"Изделие"', '"Товар"').replace('"Примечание"', '"Примечание"')
    layered = _overlay(demo.base, text)
    assert layered.status == LayerStatus.COMPLETE
    assert ("Товар", "Примечание") in _pks(_send(layered))
    assert ("Товар", "Примечание") not in _pks(_receive(layered))


def test_property_absent_skips_occupied_format_property(demo):
    text = """\
&После("ЗаполнитьПравилаКонвертацииОбъектов")
Процедура Доп(НаправлениеОбмена, ПравилаКонвертации)
    Правило = ПравилаКонвертации.Найти("Товар", "ИмяПКО");
    Если Правило <> Неопределено Тогда
        Свойство = Правило.Свойства.Найти("Description", "СвойствоФормата");
        Если Свойство = Неопределено Тогда
            ДобавитьПКС(Правило.Свойства, "Ещё", "Description");
        КонецЕсли;
    КонецЕсли;
КонецПроцедуры
"""
    layered = _overlay(demo.base, text)
    assert layered.status == LayerStatus.COMPLETE
    assert _pks(_send(layered)).count(("Товар", "Description")) == 1


def test_v3_guard_applies_only_to_full_send(kit_v3):
    send = rule_view(_send(kit_v3, False))
    headers = rule_view(_send(kit_v3, True))
    receive = rule_view(_receive(kit_v3, False))
    assert [prop.format_property for prop in send.pko[0].properties] == [
        "Description",
        "Комментарий",
    ]
    assert headers.pko == ()
    assert receive.pko == ()
    assert send.certainty == Certainty.KNOWN


def test_unguarded_v3_add_is_not_headers_only(kit_v3):
    text = """\
&После("ЗаполнитьПравилаКонвертацииОбъектов")
Процедура Доп(КомпонентыОбмена, ПравилаКонвертации, ТолькоЗаголовки = Ложь)
    Правило = ПравилаКонвертации.Найти("Товар", "ИмяПКО");
    Если Правило <> Неопределено Тогда
        ДобавитьПКС(Правило.Свойства, "Явно", "Extra");
    КонецЕсли;
КонецПроцедуры
"""
    reading = _reading(kit_v3.base, text, version=3)
    added = next(op for op in reading.operations if op.kind == OperationKind.ADD)
    assert not any(pred.kind in ("headers", "not_headers") for pred in added.preds)
    layered = compose_manager(kit_v3.base, readings=[reading], version=3)
    assert ("Товар", "Extra") in _pks(_send(layered, False))


def test_nested_direction_is_known_and_data_condition_is_not(demo):
    nested = """\
&После("ЗаполнитьПравилаКонвертацииОбъектов")
Процедура Доп(НаправлениеОбмена, ПравилаКонвертации)
    Если НаправлениеОбмена = "Отправка" Тогда
        Если НаправлениеОбмена <> "Получение" Тогда
            Правило = ПравилаКонвертации.Найти("Товар", "ИмяПКО");
            Если Правило <> Неопределено Тогда
                ДобавитьПКС(Правило.Свойства, "Вложенный", "Nested");
            КонецЕсли;
        КонецЕсли;
    КонецЕсли;
КонецПроцедуры
"""
    clean = _overlay(demo.base, nested)
    assert clean.status == LayerStatus.COMPLETE
    assert ("Товар", "Nested") in _pks(_send(clean))
    assert ("Товар", "Nested") not in _pks(_receive(clean))
    opaque = """\
&После("ЗаполнитьПравилаКонвертацииОбъектов")
Процедура Доп(НаправлениеОбмена, ПравилаКонвертации)
    Если НаправлениеОбмена = "Отправка" Тогда
        Если Данные.Сумма > 0 Тогда
            Правило = ПравилаКонвертации.Найти("Товар", "ИмяПКО");
            Если Правило <> Неопределено Тогда
                ДобавитьПКС(Правило.Свойства, "Скрытый", "Hidden");
            КонецЕсли;
        КонецЕсли;
    КонецЕсли;
КонецПроцедуры
"""
    broken = _overlay(demo.base, opaque)
    assert broken.status == LayerStatus.PARTIAL
    assert "opaque_condition" in _reasons(broken)
    assert ("Товар", "Hidden") not in _pks(_send(broken))
    assert ("Товар", "Description") in _pks(_send(broken))


def test_computed_right_hand_side_does_not_invent_a_rule(demo):
    text = """\
&После("ЗаполнитьПравилаКонвертацииОбъектов")
Процедура Доп(НаправлениеОбмена, ПравилаКонвертации)
    Имя = Сложить("То", "вар");
    Правило = ПравилаКонвертации.Найти(Имя, "ИмяПКО");
    Если Правило <> Неопределено Тогда
        ДобавитьПКС(Правило.Свойства, "Вычисленный", "Computed");
    КонецЕсли;
КонецПроцедуры
"""
    layered = _overlay(demo.base, text)
    assert layered.status == LayerStatus.PARTIAL
    assert "computed_name" in _reasons(layered)
    assert ("Товар", "Computed") not in _pks(_send(layered))
    assert ("Товар", "Description") in _pks(_send(layered))
    clean = _overlay(
        demo.base,
        """\
&После("ЗаполнитьПравилаКонвертацииОбъектов")
Процедура Доп(НаправлениеОбмена, ПравилаКонвертации)
    Правило = ПравилаКонвертации.Найти("Товар", "ИмяПКО");
    Если Правило <> Неопределено Тогда
        ДобавитьПКС(Правило.Свойства, "Явный", "Literal");
    КонецЕсли;
КонецПроцедуры
""",
    )
    assert ("Товар", "Literal") in _pks(_send(clean))


def test_early_return_before_the_branch_blocks_the_add(demo):
    text = """\
&После("ЗаполнитьПравилаКонвертацииОбъектов")
Процедура Доп(НаправлениеОбмена, ПравилаКонвертации)
    Если Данные.Готово Тогда
        Возврат;
    КонецЕсли;
    Правило = ПравилаКонвертации.Найти("Товар", "ИмяПКО");
    Если Правило <> Неопределено Тогда
        ДобавитьПКС(Правило.Свойства, "ПослеВозврата", "Late");
    КонецЕсли;
КонецПроцедуры
"""
    layered = _overlay(demo.base, text)
    assert layered.status == LayerStatus.PARTIAL
    assert "opaque_return" in _reasons(layered)
    assert ("Товар", "Late") not in _pks(_send(layered))


def test_loop_and_foreign_collection_taint_only_that_collection(demo):
    loop = _overlay(
        demo.base,
        """\
&После("ЗаполнитьПравилаКонвертацииОбъектов")
Процедура Доп(НаправлениеОбмена, ПравилаКонвертации)
    Для Каждого Правило Из ПравилаКонвертации Цикл
        ДобавитьПКС(Правило.Свойства, "ИзЦикла", "Looped");
    КонецЦикла;
КонецПроцедуры
""",
    )
    assert loop.status == LayerStatus.PARTIAL
    assert "loop" in _reasons(loop)
    assert ("Товар", "Looped") not in _pks(_send(loop))
    send = _send(loop)
    assert "pko" in send.taints
    assert "pod" not in send.taints
    pod = next(item for item in send.entities if item.collection == "pod")
    assert pod.certainty == Certainty.KNOWN
    foreign = _overlay(
        demo.base,
        """\
&После("ЗаполнитьПравилаКонвертацииОбъектов")
Процедура Доп(НаправлениеОбмена, ПравилаКонвертации)
    ЧужаяОбработка(ПравилаКонвертации);
КонецПроцедуры
""",
    )
    assert foreign.status == LayerStatus.PARTIAL
    assert "unknown_call" in _reasons(foreign)
    assert "pko" in _send(foreign).taints
    assert "pod" not in _send(foreign).taints
    assert ("Товар", "Description") in _pks(_send(foreign))


def test_identifier_casefold_and_exact_string_literal(demo):
    folded = _overlay(
        demo.base,
        """\
&после("ЗАПОЛНИТЬПРАВИЛАКОНВЕРТАЦИИОБЪЕКТОВ")
Процедура доп(направлениеобмена, правилаконвертации)
    Правило = правилаконвертации.Найти("Товар", "ИмяПКО");
    Если Правило <> Неопределено Тогда
        ДобавитьПКС(Правило.Свойства, "Регистр", "CaseOk");
    КонецЕсли;
КонецПроцедуры
""",
    )
    assert folded.status == LayerStatus.COMPLETE
    assert ("Товар", "CaseOk") in _pks(_send(folded))
    missed = _overlay(
        demo.base,
        """\
&После("ЗаполнитьПравилаКонвертацииОбъектов")
Процедура Доп(НаправлениеОбмена, ПравилаКонвертации)
    Правило = ПравилаКонвертации.Найти("товар", "ИмяПКО");
    Если Правило <> Неопределено Тогда
        ДобавитьПКС(Правило.Свойства, "НеТо", "CaseMiss");
    КонецЕсли;
КонецПроцедуры
""",
    )
    assert missed.status == LayerStatus.COMPLETE
    assert ("Товар", "CaseMiss") not in _pks(_send(missed))
    column = _overlay(
        demo.base,
        """\
&После("ЗаполнитьПравилаКонвертацииОбъектов")
Процедура Доп(НаправлениеОбмена, ПравилаКонвертации)
    Правило = ПравилаКонвертации.Найти("Товар", "имяпко");
    Если Правило <> Неопределено Тогда
        ДобавитьПКС(Правило.Свойства, "Колонка", "Column");
    КонецЕсли;
КонецПроцедуры
""",
    )
    assert column.status == LayerStatus.PARTIAL
    assert "unknown_find" in _reasons(column)
    assert ("Товар", "Column") not in _pks(_send(column))


def test_default_and_explicit_property_arguments(demo):
    layered = _overlay(
        demo.base,
        """\
&После("ЗаполнитьПравилаКонвертацииОбъектов")
Процедура Доп(НаправлениеОбмена, ПравилаКонвертации)
    Правило = ПравилаКонвертации.Найти("Товар", "ИмяПКО");
    Если Правило <> Неопределено Тогда
        ДобавитьПКС(Правило.Свойства, "Реквизит", "Short");
        ДобавитьПКС(Правило.Свойства, "Реквизит2", "Long", 1, "ПравилоСвойства", "http://demo");
    КонецЕсли;
КонецПроцедуры
""",
    )
    props = {
        prop.format_property: prop
        for rule in rule_view(_send(layered)).pko
        for prop in rule.properties
    }
    assert props["Short"].algorithm_flag == 0
    assert props["Short"].conversion_rule == ""
    assert props["Short"].namespace == ""
    assert props["Short"].argument_presence == (True, True, True)
    assert props["Long"].algorithm_flag == 1
    assert props["Long"].conversion_rule == "ПравилоСвойства"
    assert props["Long"].namespace == "http://demo"
    assert props["Long"].argument_presence == (True, True, True, True, True, True)


def test_server_annotation_without_argument_is_not_a_hook(demo):
    reading = _reading(
        demo.base,
        """\
&НаСервере
Процедура Служебная()
КонецПроцедуры
&После("ЗаполнитьПравилаКонвертацииОбъектов")
Процедура Доп(НаправлениеОбмена, ПравилаКонвертации)
КонецПроцедуры
""",
    )
    assert [hook.routine.name for hook in reading.hooks] == ["Доп"]
    assert reading.skips == ()


def test_two_hooks_in_one_extension_are_conditional(demo):
    layered = _overlay(
        demo.base,
        """\
&После("ЗаполнитьПравилаКонвертацииОбъектов")
Процедура Первая(НаправлениеОбмена, ПравилаКонвертации)
    Правило = ПравилаКонвертации.Найти("Товар", "ИмяПКО");
    Если Правило <> Неопределено Тогда
        ДобавитьПКС(Правило.Свойства, "Первый", "One");
    КонецЕсли;
КонецПроцедуры
&После("ЗаполнитьПравилаКонвертацииОбъектов")
Процедура Вторая(НаправлениеОбмена, ПравилаКонвертации)
    Правило = ПравилаКонвертации.Найти("Товар", "ИмяПКО");
    Если Правило <> Неопределено Тогда
        ДобавитьПКС(Правило.Свойства, "Второй", "Two");
    КонецЕсли;
КонецПроцедуры
""",
    )
    assert layered.status == LayerStatus.PARTIAL
    assert "duplicate_hook" in _reasons(layered)
    assert ("Товар", "One") in _pks(_send(layered))
    assert ("Товар", "Two") in _pks(_send(layered))
    assert rule_view(_send(layered)).certainty == Certainty.CONDITIONAL


def test_same_hook_in_two_extensions_keeps_both_origins(demo):
    first = _reading(
        demo.base,
        """\
&После("ЗаполнитьПараметрыКонвертации")
Процедура Первая(ПараметрыКонвертации)
    ПараметрыКонвертации.Вставить("РежимДемо", "А");
КонецПроцедуры
""",
        ordinal=1,
        name="Слой1",
    )
    second = _reading(
        demo.base,
        """\
&После("ЗаполнитьПараметрыКонвертации")
Процедура Вторая(ПараметрыКонвертации)
    ПараметрыКонвертации.Вставить("РежимДемо", "Б");
КонецПроцедуры
""",
        ordinal=2,
        name="Слой2",
    )
    layered = compose_manager(demo.base, readings=[first, second])
    assert layered.status == LayerStatus.COMPLETE
    parameter = next(
        item for item in _send(layered).entities if isinstance(item.payload, Parameter)
    )
    payload = parameter.payload
    assert isinstance(payload, Parameter)
    assert payload.default is not None
    assert payload.default.literal_value == "Б"
    layers = {
        item.layer_id
        for item in layered.revisions
        if item.collection == "parameters" and item.state != EntityState.BASE
    }
    assert layers == {"L01-Слой1", "L02-Слой2"}


def test_around_continue_keeps_base_and_adds(demo):
    layered = _overlay(
        demo.base,
        """\
&Вместо("ЗаполнитьПравилаКонвертацииОбъектов")
Процедура Замена(НаправлениеОбмена, ПравилаКонвертации)
    ПродолжитьВызов(НаправлениеОбмена, ПравилаКонвертации);
    Правило = ПравилаКонвертации.Найти("Товар", "ИмяПКО");
    Если Правило <> Неопределено Тогда
        ДобавитьПКС(Правило.Свойства, "ПослеВызова", "After");
    КонецЕсли;
КонецПроцедуры
""",
    )
    assert layered.status == LayerStatus.COMPLETE
    assert ("Товар", "Description") in _pks(_send(layered))
    assert ("Товар", "After") in _pks(_send(layered))
    assert layered.hooks[0].continuation == "once"


def test_function_continue_and_procedure_on_function_target(demo):
    from dataclasses import replace

    dispatcher = next(
        item for item in demo.base.routines if item.name == "ВыполнитьПроцедуруМодуляМенеджера"
    )
    function_target = replace(dispatcher, routine_kind="function")
    targets = {function_target.name.casefold(): function_target}
    reading = read_extension_text(
        """\
&Вместо("ВыполнитьПроцедуруМодуляМенеджера")
Функция Замена(ИмяПроцедуры, Параметры)
    Если ИмяПроцедуры = "НовыйОбработчик" Тогда
        Возврат НовыйОбработчик(Параметры);
    Иначе
        Возврат ПродолжитьВызов(ИмяПроцедуры, Параметры);
    КонецЕсли;
КонецФункции
Функция НовыйОбработчик(Параметры)
    Возврат Истина;
КонецФункции
""",
        layer=_layer(1, "Функция"),
        version=2,
        helpers=HELPERS,
        targets=targets,
    )
    assert reading.skips == ()
    assert reading.hooks[0].continuation == "once"
    procedure = _reading(
        demo.base,
        """\
&Вместо("ВыполнитьПроцедуруМодуляМенеджера")
Функция НеТа(ИмяПроцедуры, Параметры)
    Возврат ПродолжитьВызов(ИмяПроцедуры, Параметры);
КонецФункции
""",
    )
    assert "manager_signature" in {skip.reason for skip in procedure.skips}
    assert procedure.operations == () or all(
        op.kind != OperationKind.ADD for op in procedure.operations
    )


def test_bad_signature_and_missing_target_do_not_apply(demo):
    bad = _reading(
        demo.base,
        """\
&После("ЗаполнитьПравилаКонвертацииОбъектов")
Процедура Доп(ТолькоЗаголовки)
    ДобавитьПКС(Нечто, "А", "Б");
КонецПроцедуры
""",
    )
    assert "manager_signature" in {skip.reason for skip in bad.skips}
    missing = _reading(
        demo.base,
        """\
&После("НетТакойПроцедуры")
Процедура Доп()
КонецПроцедуры
""",
    )
    assert "missing_target" in {skip.reason for skip in missing.skips}


def test_local_helper_is_used_and_other_module_is_not(demo):
    local = _overlay(
        demo.base,
        """\
Процедура ДобавитьПКС(РодительПКС, СвойствоКонфигурации, СвойствоФормата, \
ИспользуетсяАлгоритмКонвертации = 0, ПравилоКонвертацииСвойства = "", ПространствоИмен = "")
    НоваяСтрока = РодительПКС.Добавить();
    НоваяСтрока.СвойствоКонфигурации = СвойствоКонфигурации;
    НоваяСтрока.СвойствоФормата = СвойствоФормата;
    НоваяСтрока.ИспользуетсяАлгоритмКонвертации = \
?(ИспользуетсяАлгоритмКонвертации = 0, Ложь, Истина);
    НоваяСтрока.ПравилоКонвертацииСвойства = ПравилоКонвертацииСвойства;
    НоваяСтрока.ПространствоИмен = ПространствоИмен;
КонецПроцедуры
&После("ЗаполнитьПравилаКонвертацииОбъектов")
Процедура Доп(НаправлениеОбмена, ПравилаКонвертации)
    Правило = ПравилаКонвертации.Найти("Товар", "ИмяПКО");
    Если Правило <> Неопределено Тогда
        ДобавитьПКС(Правило.Свойства, "Свой", "Local");
    КонецЕсли;
КонецПроцедуры
""",
    )
    assert local.status == LayerStatus.COMPLETE
    assert ("Товар", "Local") in _pks(_send(local))
    foreign = _overlay(
        demo.base,
        """\
Процедура ДобавитьПКО_Чужое(ПравилаКонвертации)
    Правило = ПравилаКонвертации.Найти("Товар", "ИмяПКО");
    Если Правило <> Неопределено Тогда
        ДобавитьПКС(Правило.Свойства, "Чужой", "Foreign");
    КонецЕсли;
КонецПроцедуры
&После("ЗаполнитьПравилаКонвертацииОбъектов")
Процедура Доп(НаправлениеОбмена, ПравилаКонвертации)
    ИнойМодуль.ДобавитьПКО_Чужое(ПравилаКонвертации);
КонецПроцедуры
""",
    )
    assert ("Товар", "Foreign") not in _pks(_send(foreign))
    assert foreign.status == LayerStatus.PARTIAL
    assert "pko" in _send(foreign).taints


def test_unverified_helper_does_not_make_the_add_known(demo):
    layered = _overlay(
        demo.base,
        """\
Процедура ДобавитьПКС(РодительПКС, СвойствоКонфигурации, СвойствоФормата)
    Сообщить(СвойствоФормата);
КонецПроцедуры
&После("ЗаполнитьПравилаКонвертацииОбъектов")
Процедура Доп(НаправлениеОбмена, ПравилаКонвертации)
    Правило = ПравилаКонвертации.Найти("Товар", "ИмяПКО");
    Если Правило <> Неопределено Тогда
        ДобавитьПКС(Правило.Свойства, "Сомнительный", "Nope");
    КонецЕсли;
КонецПроцедуры
""",
    )
    assert "helper_unverified" in _reasons(layered)
    assert ("Товар", "Nope") not in _pks(_send(layered))
    assert layered.status == LayerStatus.PARTIAL


def test_dispatcher_cases():
    from kd2_rules_mcp.ed.reader import read_manager_text

    base_text = """\
Функция ВерсияФорматаМенеджераОбмена() Экспорт
    Возврат "2";
КонецФункции
Процедура ЗаполнитьПравилаКонвертацииОбъектов(НаправлениеОбмена, ПравилаКонвертации) Экспорт
КонецПроцедуры
Процедура ВыполнитьПроцедуруМодуляМенеджера(ИмяПроцедуры, Параметры) Экспорт
    Если ИмяПроцедуры = "Известное" Тогда
        Известное();
    КонецЕсли;
КонецПроцедуры
Процедура Известное()
КонецПроцедуры
"""
    document = read_manager_text(base_text, path="throw-base.bsl")
    no_branch = _overlay(
        document,
        """\
&После("ЗаполнитьПравилаКонвертацииОбъектов")
Процедура Доп(НаправлениеОбмена, ПравилаКонвертации)
    ПравилоКонвертации = \
ОбменДаннымиXDTOСервер.ИнициализироватьПравилоКонвертацииОбъекта(ПравилаКонвертации);
    ПравилоКонвертации.ИмяПКО = "Новый";
    ПравилоКонвертации.ПриОтправкеДанных = "НетВетки";
КонецПроцедуры
""",
    )
    chains = {item.target_name: item.resolution for item in _send(no_branch).dispatch_chains}
    assert chains["НетВетки"] == "no_call"
    after = _overlay(
        document,
        """\
&После("ВыполнитьПроцедуруМодуляМенеджера")
Процедура После(ИмяПроцедуры, Параметры)
    Если ИмяПроцедуры = "НовоеИмя" Тогда
        НовоеИмя();
    КонецЕсли;
КонецПроцедуры
Процедура НовоеИмя()
КонецПроцедуры
""",
    )
    resolved = {item.target_name: item.resolution for item in _send(after).dispatch_chains}
    assert resolved["НовоеИмя"] == "call"
    assert resolved["Известное"] == "call"
    throwing = read_manager_text(
        base_text.replace(
            "    КонецЕсли;\nКонецПроцедуры",
            '    Иначе\n        ВызватьИсключение "нет";\n    КонецЕсли;\nКонецПроцедуры',
            1,
        ),
        path="throws.bsl",
    )
    thrown = _overlay(
        throwing,
        """\
&После("ВыполнитьПроцедуруМодуляМенеджера")
Процедура После(ИмяПроцедуры, Параметры)
    Если ИмяПроцедуры = "НовоеИмя" Тогда
        НовоеИмя();
    КонецЕсли;
КонецПроцедуры
Процедура НовоеИмя()
КонецПроцедуры
""",
    )
    thrown_map = {item.target_name: item.resolution for item in _send(thrown).dispatch_chains}
    assert thrown_map["НовоеИмя"] == "throws"
    before = _overlay(
        document,
        """\
&Перед("ВыполнитьПроцедуруМодуляМенеджера")
Процедура До(ИмяПроцедуры, Параметры)
КонецПроцедуры
""",
    )
    assert {item.target_name: item.resolution for item in _send(before).dispatch_chains}[
        "Известное"
    ] == "call"


def test_around_without_fallback_and_two_layers(demo):
    muted = _overlay(
        demo.base,
        """\
&Вместо("ВыполнитьПроцедуруМодуляМенеджера")
Процедура Замена(ИмяПроцедуры, Параметры)
    Если ИмяПроцедуры = "ТолькоНовое" Тогда
        ТолькоНовое();
    КонецЕсли;
КонецПроцедуры
Процедура ТолькоНовое()
КонецПроцедуры
""",
    )
    muted_map = {item.target_name: item.resolution for item in _send(muted).dispatch_chains}
    assert muted_map["ТолькоНовое"] == "call"
    assert muted_map["ПКО_Товар_ПриОтправкеДанных"] == "no_call"
    first = _reading(
        demo.base,
        """\
&Вместо("ВыполнитьПроцедуруМодуляМенеджера")
Процедура Слой1(ИмяПроцедуры, Параметры)
    Если ИмяПроцедуры = "ИмяОдин" Тогда
        ИмяОдин();
    Иначе
        ПродолжитьВызов(ИмяПроцедуры, Параметры);
    КонецЕсли;
КонецПроцедуры
Процедура ИмяОдин()
КонецПроцедуры
""",
        ordinal=1,
        name="Один",
    )
    second = _reading(
        demo.base,
        """\
&Вместо("ВыполнитьПроцедуруМодуляМенеджера")
Процедура Слой2(ИмяПроцедуры, Параметры)
    Если ИмяПроцедуры = "ИмяДва" Тогда
        ИмяДва();
    Иначе
        ПродолжитьВызов(ИмяПроцедуры, Параметры);
    КонецЕсли;
КонецПроцедуры
Процедура ИмяДва()
КонецПроцедуры
""",
        ordinal=2,
        name="Два",
    )
    layered = compose_manager(demo.base, readings=[first, second])
    both = {item.target_name: item.resolution for item in _send(layered).dispatch_chains}
    assert both["ИмяОдин"] == "call"
    assert both["ИмяДва"] == "call"
    assert both["ПКО_Товар_ПриОтправкеДанных"] == "call"


def test_mutations_set_delete_and_revisions(demo):
    updated = _overlay(
        demo.base,
        """\
&После("ЗаполнитьПравилаКонвертацииОбъектов")
Процедура Доп(НаправлениеОбмена, ПравилаКонвертации)
    Правило = ПравилаКонвертации.Найти("Товар", "ИмяПКО");
    Если Правило <> Неопределено Тогда
        Свойство = Правило.Свойства.Найти("Description", "СвойствоФормата");
        Если Свойство <> Неопределено Тогда
            Свойство.ПространствоИмен = "http://demo";
        КонецЕсли;
        Правило.ОбъектФормата = "Catalog.One";
        Правило.ВариантИдентификации = "ПоПолямПоиска";
    КонецЕсли;
КонецПроцедуры
""",
    )
    tip = next(
        item
        for item in _send(updated).entities
        if isinstance(item.payload, ObjectRule) and item.payload.name == "Товар"
    )
    assert tip.layer_id == "L01-ДемоB"
    assert len(tip.changes) == 3
    rule = tip.payload
    assert isinstance(rule, ObjectRule)
    assert rule.format_object.value == "Catalog.One"
    assert rule.properties[0].namespace == "http://demo"
    renamed = _overlay(
        demo.base,
        """\
&После("ЗаполнитьПравилаКонвертацииОбъектов")
Процедура Доп(НаправлениеОбмена, ПравилаКонвертации)
    Правило = ПравилаКонвертации.Найти("Товар", "ИмяПКО");
    Если Правило <> Неопределено Тогда
        Правило.ИмяПКО = "Другое";
    КонецЕсли;
КонецПроцедуры
""",
    )
    assert "name_mutation" in _reasons(renamed)
    assert renamed.status == LayerStatus.PARTIAL
    assert any(rule.name == "Товар" for rule in rule_view(_send(renamed)).pko)
    deleted = _overlay(
        demo.base,
        """\
&После("ЗаполнитьПравилаКонвертацииОбъектов")
Процедура Доп(НаправлениеОбмена, ПравилаКонвертации)
    Правило = ПравилаКонвертации.Найти("Товар", "ИмяПКО");
    Если Правило <> Неопределено Тогда
        ПравилаКонвертации.Удалить(Правило);
    КонецЕсли;
КонецПроцедуры
""",
    )
    assert all(rule.name != "Товар" for rule in rule_view(_send(deleted)).pko)
    tomb = next(
        item
        for item in _send(deleted).entities
        if item.payload is not None and item.payload.name == "Товар"
    )
    assert tomb.state == EntityState.DELETED


def test_v1_group_is_skipped_and_header_property_is_not(demo):
    group = _overlay(
        demo.base,
        """\
&После("ЗаполнитьПравилаКонвертацииОбъектов")
Процедура Доп(НаправлениеОбмена, ПравилаКонвертации)
    Правило = ПравилаКонвертации.Найти("Товар", "ИмяПКО");
    Если Правило <> Неопределено Тогда
        ДобавитьПКТЧ(Правило.СвойстваТабличныхЧастей, "", "Товары");
        ДобавитьПКС(Правило.Свойства, "Шапка", "Header");
    КонецЕсли;
КонецПроцедуры
""",
        version=1,
    )
    assert "v1_group" in _reasons(group)
    assert ("Товар", "Header") in _pks(_send(group))
    assert group.status == LayerStatus.PARTIAL


def test_change_control_recursion_and_method_limit(demo):
    control = _overlay(
        demo.base,
        """\
&ИзменениеИКонтроль("ЗаполнитьПравилаКонвертацииОбъектов")
Процедура Доп(НаправлениеОбмена, ПравилаКонвертации)
    ДобавитьПКС(ПравилаКонвертации, "А", "Б");
КонецПроцедуры
""",
    )
    assert "change_control" in _reasons(control)
    assert control.status == LayerStatus.PARTIAL
    assert ("Товар", "Б") not in _pks(_send(control))
    recursive = _overlay(
        demo.base,
        """\
Процедура Первая(ПравилаКонвертации)
    Вторая(ПравилаКонвертации);
КонецПроцедуры
Процедура Вторая(ПравилаКонвертации)
    Первая(ПравилаКонвертации);
КонецПроцедуры
&После("ЗаполнитьПравилаКонвертацииОбъектов")
Процедура Доп(НаправлениеОбмена, ПравилаКонвертации)
    Первая(ПравилаКонвертации);
    Правило = ПравилаКонвертации.Найти("Товар", "ИмяПКО");
    Если Правило <> Неопределено Тогда
        ДобавитьПКС(Правило.Свойства, "Рекурсия", "Recur");
    КонецЕсли;
КонецПроцедуры
""",
    )
    assert "recursion" in _reasons(recursive)
    assert ("Товар", "Recur") not in _pks(_send(recursive))
    methods = "\n".join(f"Процедура Метод{index}()\nКонецПроцедуры" for index in range(65))
    limited = _overlay(
        demo.base,
        methods
        + """
&После("ЗаполнитьПравилаКонвертацииОбъектов")
Процедура Доп(НаправлениеОбмена, ПравилаКонвертации)
    Правило = ПравилаКонвертации.Найти("Товар", "ИмяПКО");
    Если Правило <> Неопределено Тогда
        ДобавитьПКС(Правило.Свойства, "Лишний", "Limit");
    КонецЕсли;
КонецПроцедуры
""",
    )
    assert "resource_limit" in _reasons(limited)
    assert ("Товар", "Limit") not in _pks(_send(limited))
    assert limited.status == LayerStatus.PARTIAL


def test_skip_lowers_status(demo):
    layered = _overlay(
        demo.base,
        """\
&ИзменениеИКонтроль("ЗаполнитьПравилаКонвертацииОбъектов")
Процедура Доп(НаправлениеОбмена, ПравилаКонвертации)
КонецПроцедуры
""",
    )
    assert layered.skipped
    assert layered.status == LayerStatus.PARTIAL


def test_effective_view_protocol_keeps_original_span(kit_b):
    context = _send(kit_b)
    view = rule_view(context)
    assert isinstance(view, EffectiveRuleView)
    with pytest.raises(AttributeError):
        view.pko = ()  # pyright: ignore[reportAttributeAccessIssue]
    tip = next(rule for rule in view.pko if rule.name == "Товар")
    assert tip.span == kit_b.base.pko[0].span
    assert tip.raw_text == kit_b.base.pko[0].raw_text
    added = next(prop for prop in tip.properties if prop.format_property == "Code")
    assert "Code" in added.raw_text or "ДопКод" in added.raw_text
    root = Path(__file__).resolve().parents[1] / "src" / "kd2_rules_mcp" / "ed"
    for name in ("layers.py", "layer_reader.py", "layer_model.py", "layer_address.py"):
        text = (root / name).read_text(encoding="utf-8")
        assert "validation" not in text
        assert "kd2_rules_mcp.service" not in text
