"""Контракт A для B: DTO, предусловия и границы body, без записи комплекта и ИБ."""

import hashlib
import importlib
import json
from collections import Counter
from dataclasses import asdict, replace
from pathlib import Path
from types import MappingProxyType

import pytest

from kd2_rules_mcp.authoring.ed.canonical import canonicalize_operations
from kd2_rules_mcp.authoring.ed.context import AuthoringContext
from kd2_rules_mcp.authoring.ed.handlers import (
    DISPATCHER,
    EVENT_PARAMETERS,
    canonical_operations_bytes,
    handler_name,
    merge_operations,
    operation_from_input,
    preservation_marker,
)
from kd2_rules_mcp.authoring.ed.model import (
    AddAlgorithmicHeaderProperty,
    AddHeaderProperty,
    AttributeDraft,
    AuthoringInputs,
    AuthoringPreconditionError,
    AuthoringTarget,
    ExtensionIdentity,
    HandlerChain,
    HandlerEvent,
    PreserveMissingHeaderProperty,
    SetObjectHandler,
    SourceSet,
    normalize_body,
)
from kd2_rules_mcp.authoring.ed.operations import validate_preconditions
from kd2_rules_mcp.ed import read_manager
from kd2_rules_mcp.ed.routes import read_routes
from kd2_rules_mcp.ed.schema import load_schema
from kd2_rules_mcp.ed.schema.xdto import metadata
from kd2_rules_mcp.errors import EdAuthoringResourceLimitError
from kd2_rules_mcp.validation.ed_authoring import prepare_handler_operations
from kd2_rules_mcp.validation.ed_authoring_handlers import (
    check_body,
    enforce_handler_result,
    preset_procedure_text,
)
from kd2_rules_mcp.validation.ed_structure_snapshot import StructureSnapshot
from tests.session_inputs import corpus_structure
from tests.test_ed_authoring_model import DATA, IDENTITY, TARGET, inputs, refreshed

HANDLERS = DATA / "handlers"


def handler_inputs(version=2, *, event=None, previous="", dispatch_extra="", signature=None):
    text = (DATA / f"base/manager-v{version}.bsl").read_text("utf-8")
    if event and previous:
        text = text.replace(
            'ПравилоКонвертации.ИмяПКО = "Товар";',
            f'ПравилоКонвертации.ИмяПКО = "Товар";\nПравилоКонвертации.{event} = "{previous}";',
        )
    dispatch = f"Процедура {DISPATCHER}(ИмяПроцедуры, Параметры) Экспорт\n"
    if previous:
        assert event is not None
        parameters = EVENT_PARAMETERS[event]
        dispatch += (
            f'Если ИмяПроцедуры = "{previous}" Тогда\n{previous}('
            + ", ".join("Параметры." + p for p in parameters)
            + ");\n"
            + dispatch_extra
            + "КонецЕсли;\n"
        )
    dispatch += "КонецПроцедуры\n"
    if previous:
        assert event is not None
        dispatch += (
            f"Процедура {previous}("
            + (signature or ", ".join(EVENT_PARAMETERS[event]))
            + ")\nВозврат;\nКонецПроцедуры\n"
        )
    return inputs(version, text=text + "\n" + dispatch)


def handler(
    event: HandlerEvent = "ПриОтправкеДанных",
    body="Возврат;",
    previous="",
    chain: HandlerChain = "none",
):
    direction = "send" if event == "ПриОтправкеДанных" else "receive"
    return SetObjectHandler(replace(TARGET, direction=direction), event, body, previous, chain)


def assert_refusal(value, operations, check, *, context=None):
    with pytest.raises(AuthoringPreconditionError) as caught:
        validate_preconditions(
            value, operations, IDENTITY, version_scope="manager", context=context
        )
    assert "ed.author." + check in {f.id for f in caught.value.failures}
    return caught.value


@pytest.mark.parametrize("version", [1, 2, 3])
@pytest.mark.parametrize("event", tuple(EVENT_PARAMETERS))
def test_events_and_interfaces(version, event):
    plan = prepare_handler_operations(
        handler_inputs(version), (handler(event),), IDENTITY, version_scope="manager"
    )
    assert plan.bindings[0].parameters == EVENT_PARAMETERS[event]
    assert plan.bindings[0].manager_interface == version
    assert plan.manager_interface == version
    assert plan.dispatcher_name == IDENTITY.prefix + "Диспетчер"
    assert plan.dispatcher_order == (plan.bindings[0].handler_name,)
    assert not plan.runtime_verified and not plan.bindings[0].runtime_verified
    assert any(n.detail_key == "agent_body" for n in plan.notices)
    notice = next(n for n in plan.notices if n.id == "ed.author.handler_runtime_unverified")
    assert "Объектный путь доказан кодом, не обменом" in notice.message
    if event == "ПриОтправкеДанных":
        assert "/отправка " in notice.message
    else:
        assert "обычный путь и объектный путь" in notice.message


@pytest.mark.parametrize("event", tuple(EVENT_PARAMETERS))
def test_existing_handler_exact_chain_and_signature(event):
    op = handler(event, previous="Типовой", chain="after_existing")
    value = handler_inputs(event=event, previous="Типовой")
    plan = prepare_handler_operations(value, (op,), IDENTITY, version_scope="manager")
    binding = plan.bindings[0]
    assert binding.previous_name == "Типовой"
    assert binding.previous_arguments == EVENT_PARAMETERS[event]
    assert len(binding.previous_branch_hash) == len(binding.previous_routine_hash) == 64
    assert "ed.author.handler_chained" in {n.id for n in plan.notices}
    if event == "ПриОтправкеДанных":
        assert binding.runtime_verified and plan.runtime_verified
        assert "ed.author.handler_runtime_unverified" not in {n.id for n in plan.notices}
    else:
        assert not binding.runtime_verified
        unverified = next(n for n in plan.notices if n.id == "ed.author.handler_runtime_unverified")
        assert "обычный путь и объектный путь" in unverified.message
    assert_refusal(value, (replace(op, chain="none"),), "handler_chain_required")
    assert_refusal(value, (replace(op, expected_previous=""),), "handler_previous_mismatch")
    assert_refusal(handler_inputs(), (replace(op, expected_previous=""),), "handler_chain_required")
    assert_refusal(
        handler_inputs(
            event=event, previous="Типовой", signature="Знач " + ", ".join(EVENT_PARAMETERS[event])
        ),
        (op,),
        "handler_signature",
    )
    assert_refusal(
        handler_inputs(event=event, previous="Типовой", dispatch_extra="Посторонний();\n"),
        (op,),
        "handler_dispatch_unknown",
    )


@pytest.mark.parametrize(
    "body,check",
    [
        ('ДанныеXDTO.Вставить("X', "body_lexical"),
        ("Если Истина Тогда\nВозврат;", "body_lexical"),
        ("КонецЕсли;", "body_lexical"),
        ("X = (1];", "body_lexical"),
        ("КонецПроцедуры\nПроцедура Чужая()\n", "body_form_unsupported"),
        ('&После("X")\nВозврат;', "body_form_unsupported"),
        ("#Область X\nВозврат;", "body_form_unsupported"),
        ("~Метка: Перейти ~Метка;", "body_form_unsupported"),
        ('Выполнить("Код");', "body_form_unsupported"),
        ('X = Вычислить("Код");', "body_form_unsupported"),
        ("ПродолжитьВызов();", "body_form_unsupported"),
        (f"{DISPATCHER}();", "body_form_unsupported"),
        ("ПолученныеДанные = Неопределено;", "body_parameter_unavailable"),
        ("Перем ПолученныеДанные;", "body_parameter_unavailable"),
        ("Параметры.ДанныеXDTO = Неопределено;", "body_parameter_unavailable"),
        ("ДанныеXDTO.Вставить(Имя, 1);", "body_dynamic_reference"),
        ('ДанныеXDTO.Вставить("НеСуществует", 1);', "body_reference_unresolved"),
        ("ДанныеXDTO.ОбщиеСвойстваОбъектовФормата.НетПоля = 1;", "body_reference_unresolved"),
        ("X = ДанныеИБ.НетРеквизита;", "body_reference_unresolved"),
        (
            "А = КомпонентыОбмена; А.ПравилаКонвертацииОбъектов.Очистить();",
            "body_form_unsupported",
        ),
        ("X = КомпонентыОбмена.ПравилаКонвертацииОбъектов;", "body_form_unsupported"),
        ("X = КомпонентыОбмена.ПравилаОбработкиДанных;", "body_form_unsupported"),
        ('X = КомпонентыОбмена["ПравилаКонвертацииОбъектов"];', "body_form_unsupported"),
        ('А = КомпонентыОбмена; А.Свойство("ПравилаОбработкиДанных", X);', "body_form_unsupported"),
        ('КомпонентыОбмена.Вставить("ПравилаКонвертацииОбъектов", X);', "body_form_unsupported"),
        ('КомпонентыОбмена.Вставить("X", 1);', "body_form_unsupported"),
        ("КомпонентыОбмена = Новый Структура;", "body_form_unsupported"),
        ("КомпонентыОбмена.Очистить();", "body_form_unsupported"),
        ("X = КомпонентыОбмена[Имя];", "body_dynamic_reference"),
        ("X = КомпонентыОбмена.ПравилаКонвертацииПредопределенныхДанных;", "body_form_unsupported"),
        (
            'Р = ОбменДаннымиXDTOСервер.ПКОПоИмени(КомпонентыОбмена, "Товар"); '
            'Р.ПриОтправкеДанных = "X";',
            "body_form_unsupported",
        ),
        (
            'Р = ОбменДаннымиXDTOСервер.ПКОПоИмени(КомпонентыОбмена, "Товар"); Р.Очистить();',
            "body_form_unsupported",
        ),
        (
            'Р = ОбменДаннымиXDTOСервер.ПКОПоИмени(КомпонентыОбмена, "Товар"); '
            'Р["ПриОтправкеДанных"] = "X";',
            "body_form_unsupported",
        ),
        (
            'Р = ОбменДаннымиXDTOСервер.ПКОПоИмени(КомпонентыОбмена, "Товар"); '
            'А = Р; А.ПриОтправкеДанных = "X";',
            "body_form_unsupported",
        ),
        (
            'Р = ОбменДаннымиXDTOСервер.ПКОПоИмени(КомпонентыОбмена, "Товар"); Р[Имя] = "X";',
            "body_dynamic_reference",
        ),
    ],
)
def test_body_rejected(body, check):
    value = handler_inputs()
    result = check_body(value, handler(body=body), AuthoringContext(value))
    assert "ed.author." + check in {f.id for f in result.failures}
    assert all(body not in f.message for f in result.failures)


@pytest.mark.parametrize(
    "body",
    [
        'ДанныеXDTO.Вставить("Комментарий", "~");',
        'ДанныеXDTO.Вставить("Комментарий", "Текст ""цитата""\n\t| продолжение");',
        "// ПолученныеДанные, Выполнить, КомпонентыОбмена\n"
        'ДанныеXDTO.Вставить("Комментарий", "СтекВыгрузки");',
        "Локальная.ПолученныеДанные = 1; Запрос.Выполнить();",
        "Локальная.ПолученныеДанные.НеРеквизит = 1; X = Локальная.ДанныеXDTO.НеПоле;",
        "Локальная.ДанныеXDTO.Вставить(Имя, 1);",
        "Если КомпонентыОбмена.НаправлениеОбмена = 1 Тогда\nВозврат;\nКонецЕсли;",
        "А = ОбменДаннымиXDTOСервер.РазрешенаЗаписьОбъекта(ДанныеИБ, КомпонентыОбмена);",
        'Р = ОбменДаннымиXDTOСервер.ПКОПоИмени(КомпонентыОбмена, "Товар"); X = Р.ИмяПКО;',
        'Посторонний(ОбменДаннымиXDTOСервер.ПКОПоИмени(КомпонентыОбмена, "Товар"));',
        "X = КомпонентыОбмена.НеИзПеречня;",
        'X = КомпонентыОбмена["НаправлениеОбмена"];',
        "КомпонентыОбмена.НаправлениеОбмена = 1;",
        "Посторонний(КомпонентыОбмена);",
        'ЗаполнитьЗначенияСвойств(КомпонентыОбмена, Новый Структура("НаправлениеОбмена", 1));',
        "ДанныеXDTO = Неопределено;",
        'ИмяПКО = "";',
        "#Область Имя\nВозврат;\n#КонецОбласти",
        "If True Then\nX = 1;\nEndIf;",
        "Пока Истина Цикл\nПрервать;\nКонецЦикла;",
        "Для Каждого Элемент Из Массив Цикл\nПродолжить;\nКонецЦикла;",
        'ДанныеИБ.ДополнительныеСвойства.Вставить("СлужебныйКлюч", 1);',
        "ДанныеИБ.ОбменДанными.Загрузка = Истина;",
        "Для Н = 1 По 2 Цикл\nX = Н;\nКонецЦикла;",
    ],
)
def test_body_clean_boundary(body):
    value = handler_inputs()
    assert not check_body(value, handler(body=body), AuthoringContext(value)).failures


def test_event_data_root_may_be_cleared():
    value = handler_inputs()
    context = AuthoringContext(value)
    assert not check_body(
        value, handler("ПриОтправкеДанных", body="ДанныеXDTO = Неопределено;"), context
    ).failures
    assert not check_body(
        value,
        handler("ПриКонвертацииДанныхXDTO", body="ПолученныеДанные = Неопределено;"),
        context,
    ).failures


@pytest.mark.parametrize(
    ("event", "body"),
    [
        ("ПриОтправкеДанных", "КомпонентыОбмена = Неопределено;"),
        ("ПриОтправкеДанных", "КомпонентыОбмена = Новый Структура;"),
        ("ПриОтправкеДанных", "КомпонентыОбмена.Clear();"),
        ("ПриОтправкеДанных", "А = КомпонентыОбмена; А.Очистить();"),
        ("ПриОтправкеДанных", 'КомпонентыОбмена.Удалить("ПравилаКонвертацииОбъектов");'),
        (
            "ПередЗаписьюПолученныхДанных",
            "Если Истина Тогда\nКонвертацияСвойств = Неопределено;\nКонецЕсли;",
        ),
        (
            "ПередЗаписьюПолученныхДанных",
            "If True Then КонвертацияСвойств = Undefined; EndIf;",
        ),
        (
            "ПриОтправкеДанных",
            'ОбменДаннымиXDTOСервер.ПКОПоИмени(КомпонентыОбмена, "Товар").ПриОтправкеДанных = "X";',
        ),
        (
            "ПриОтправкеДанных",
            'Р = ОбменДаннымиXDTOСервер.ПКОПоИмени(КомпонентыОбмена, "Товар"); '
            "Р.Свойства = Неопределено;",
        ),
        (
            "ПриОтправкеДанных",
            'Р = ОбменДаннымиXDTOСервер.ПКОПоИмени(КомпонентыОбмена, "Товар"); '
            "Р.Свойства.Очистить();",
        ),
        (
            "ПриОтправкеДанных",
            'Р = ОбменДаннымиXDTOСервер.ПКОПоИмени(КомпонентыОбмена, "Товар"); '
            'Р.ИмяПКО = "Другое";',
        ),
        (
            "ПередЗаписьюПолученныхДанных",
            'Р = КонвертацияСвойств[0]; Р.СвойствоФормата = "X";',
        ),
        (
            "ПередЗаписьюПолученныхДанных",
            'Р = КонвертацияСвойств.Получить(0); Р.СвойствоФормата = "X";',
        ),
        (
            "ПередЗаписьюПолученныхДанных",
            'КонвертацияСвойств.Получить(0).СвойствоФормата = "X";',
        ),
        (
            "ПередЗаписьюПолученныхДанных",
            "For Each Р In КонвертацияСвойств Do\nР.СвойствоФормата = 1;\nEndDo;",
        ),
        ("ПриОтправкеДанных", "If True Then\nReturn;"),
        ("ПриОтправкеДанных", "EndIf;"),
        ("ПриОтправкеДанных", "Возврат 1;"),
        ("ПриОтправкеДанных", "Return 1;"),
        ("ПриОтправкеДанных", "Прервать;"),
        ("ПриОтправкеДанных", "Continue;"),
        ("ПриОтправкеДанных", "#Если Сервер Тогда\nX = 1;\n#КонецЕсли"),
        ("ПриОтправкеДанных", 'Менеджер.ВыполнитьПроцедуруМодуляМенеджера("X", П);'),
        ("ПриОтправкеДанных", "ОбменДаннымиXDTOСервер.ДобавитьПКС(П, 1, 2);"),
        (
            "ПриОтправкеДанных",
            "МенеджерОбменаЧерезУниверсальныйФормат.ЗаполнитьПравилаКонвертацииОбъектов(1, 2);",
        ),
        ("ПриОтправкеДанных", "ДобавитьПКТЧ(П, 1, 2);"),
    ],
)
def test_review_probe_rejects_forbidden_form(event, body):
    value = handler_inputs()
    result = check_body(value, handler(event, body=body), AuthoringContext(value))
    assert result.failures
    assert all(body not in failure.message for failure in result.failures)


def test_review_probe_rule_builders_and_generated_names_are_refusals_not_opaque():
    value = handler_inputs()
    own = handler_name(IDENTITY.prefix, "Товар", "send", "ПриОтправкеДанных")
    other = handler_name(IDENTITY.prefix, "Товар", "receive", "ПередЗаписьюПолученныхДанных")
    for body in (
        'доп_Диспетчер("X", П);',
        other + "(1, 2, 3, 4);",
        own + "(ДанныеИБ, ДанныеXDTO, КомпонентыОбмена, СтекВыгрузки);",
        "ОбменДаннымиXDTOСервер.ДобавитьПКС(П, 1, 2);",
    ):
        result = check_body(value, handler(body=body), AuthoringContext(value), wrapper_name=own)
        assert result.failures
        assert not result.opaque_calls
    clean = check_body(
        value,
        handler(
            body="А = ОбменДаннымиXDTOСервер.РазрешенаЗаписьОбъекта(ДанныеИБ, КомпонентыОбмена);"
        ),
        AuthoringContext(value),
        wrapper_name=own,
    )
    assert not clean.failures


def test_review_probe_references_keep_name_and_line_and_skip_empty_instruction():
    value = handler_inputs()
    context = AuthoringContext(value)
    unresolved = check_body(
        value,
        handler(body='ДанныеXDTO.Вставить("НетПоля", 1);\nДанныеXDTO.Вставить("ДругоеПоле", 1);'),
        context,
    )
    misses = [f for f in unresolved.failures if f.id == "ed.author.body_reference_unresolved"]
    assert len(misses) >= 2
    assert {f.line for f in misses} >= {1, 2}
    assert any("НетПоля" in f.message for f in misses)
    assert any("ДругоеПоле" in f.message for f in misses)
    dynamic = check_body(
        value,
        handler(body="ДанныеXDTO.Вставить(Имя, 1);\nДанныеXDTO.Вставить(Другое, 1);"),
        context,
    )
    computed = [f for f in dynamic.failures if f.id == "ed.author.body_dynamic_reference"]
    assert len(computed) >= 2
    assert any("Имя" in f.message for f in computed)
    assert any("Другое" in f.message for f in computed)
    empty = check_body(value, handler(body='ИмяПКО = "";'), context)
    assert not empty.failures
    table = check_body(
        value,
        handler(body="X = КомпонентыОбмена.ПравилаКонвертацииОбъектов;"),
        context,
    )
    assert any("ПКОПоИмени" in f.message for f in table.failures)


def test_guarded_missing_format_property_is_notice_and_write_stays_refusal():
    value = handler_inputs()
    read = 'Если ДанныеXDTO.Свойство("НетПоля") Тогда\n\tX = ДанныеXDTO.НетПоля;\nКонецЕсли;'
    checked = check_body(value, handler(body=read), AuthoringContext(value))
    assert not any(f.id == "ed.author.body_reference_unresolved" for f in checked.failures)
    assert checked.guarded_reads and checked.guarded_reads[0][0] == "НетПоля"
    plan = prepare_handler_operations(
        value, (handler(body=read),), IDENTITY, version_scope="manager"
    )
    assert any(
        n.id == "ed.author.format_property_guarded" and "НетПоля" in n.message for n in plan.notices
    )
    write = (
        'Если ДанныеXDTO.Свойство("НетПоля") Тогда\n'
        '\tДанныеXDTO.Вставить("НетПоля", 1);\n'
        "КонецЕсли;"
    )
    refused = check_body(value, handler(body=write), AuthoringContext(value))
    assert any(f.id == "ed.author.body_reference_unresolved" for f in refused.failures)


def test_register_filter_and_dynamic_additional_key_are_not_refusals():
    value = handler_inputs()
    owner = value.structure.objects[("справочник", "товары")]
    catalog = check_body(
        value, handler(body="X = ДанныеИБ.Отбор.Организация;"), AuthoringContext(value)
    )
    assert any(f.id == "ed.author.body_reference_unresolved" for f in catalog.failures)
    register = replace(
        value,
        structure=replace(
            value.structure,
            objects=MappingProxyType(
                {
                    **value.structure.objects,
                    ("справочник", "товары"): replace(owner, kind="РегистрСведений"),
                }
            ),
        ),
    )
    context = AuthoringContext(register)
    assert not check_body(
        register, handler(body="X = ДанныеИБ.Отбор.Организация;"), context
    ).failures
    received = handler("ПриКонвертацииДанныхXDTO", body="X = ПолученныеДанные.Отбор.Организация;")
    assert not check_body(register, received, context).failures
    dynamic_key = handler(
        "ПриКонвертацииДанныхXDTO",
        body="ПолученныеДанные.ДополнительныеСвойства.Вставить(Ключ, 1);",
    )
    assert not any(
        f.id == "ed.author.body_dynamic_reference"
        for f in check_body(value, dynamic_key, AuthoringContext(value)).failures
    )


def test_preset_body_matches_spec_exemplar_and_sorts_pairs():
    exemplar = preset_procedure_text("доп_Сохранить", (("Комментарий", "доп_Заметка"),))
    assert exemplar == (HANDLERS / "preset-body.bsl").read_text(encoding="utf-8")
    assert "СвойстваОтсутствующиеВПолученныхДанных" in exemplar
    several = preset_procedure_text(
        "доп_Сохранить", (("Яблоко", "РеквизитЯ"), ("Банан", "РеквизитБ"))
    )
    assert several.index('Найти("Банан")') < several.index('Найти("Яблоко")')
    assert several.count("СтрРазделить") == 1
    repeated = preset_procedure_text(
        "доп_Сохранить", (("Комментарий", "доп_Заметка"), ("Код", "доп_Заметка"))
    )
    assert repeated.count('Добавить("доп_Заметка")') == 1


def test_noncanonical_dependency_names_the_id_the_server_expected():
    value = handler_inputs()
    raw = AddHeaderProperty(replace(TARGET, direction="receive"), "заметка", "комментарий")
    canonical = canonicalize_operations(value, (raw,))[0]
    refused = assert_refusal(
        value,
        (raw, PreserveMissingHeaderProperty(raw.target, raw.operation_id)),
        "handler_property_dependency",
    )
    assert canonical.operation_id in refused.failures[0].message
    assert "ожидался канонический идентификатор" in refused.failures[0].message
    prepare_handler_operations(
        value,
        (raw, PreserveMissingHeaderProperty(raw.target, canonical.operation_id)),
        IDENTITY,
        version_scope="manager",
    )
    chained = handler_inputs(event="ПриОтправкеДанных", previous="Типовой")
    rough = handler(
        body='ДанныеXDTO.Вставить("Комментарий", ДанныеИБ.Заметка);',
        previous="типовой",
        chain="after_existing",
    )
    canonical_handler = canonicalize_operations(chained, (rough,))[0]
    algorithmic = AddAlgorithmicHeaderProperty(TARGET, "Заметка", "Комментарий", rough.operation_id)
    refused = assert_refusal(chained, (rough, algorithmic), "handler_property_dependency")
    assert canonical_handler.operation_id in refused.failures[0].message
    prepare_handler_operations(
        chained,
        (
            rough,
            AddAlgorithmicHeaderProperty(
                TARGET, "Заметка", "Комментарий", canonical_handler.operation_id
            ),
        ),
        IDENTITY,
        version_scope="manager",
    )


def test_several_bindings_fix_dispatcher_order_names_and_interface():
    value = handler_inputs()
    first = handler()
    second = replace(first, target=replace(TARGET, pko_address="ПКО/Заказ"))
    plan = prepare_handler_operations(value, (first, second), IDENTITY, version_scope="manager")
    assert len(plan.bindings) == 2
    assert plan.dispatcher_name == "доп_Диспетчер"
    assert plan.dispatcher_order == tuple(sorted(b.handler_name for b in plan.bindings))
    assert plan.manager_interface == 2
    assert {b.manager_interface for b in plan.bindings} == {2}
    chained = handler_inputs(event="ПриОтправкеДанных", previous="Типовой")
    linked = handler(
        body='ДанныеXDTO.Вставить("Комментарий", ДанныеИБ.Заметка);',
        previous="Типовой",
        chain="after_existing",
    )
    algorithmic = AddAlgorithmicHeaderProperty(
        TARGET, "Заметка", "Комментарий", linked.operation_id
    )
    mixed = prepare_handler_operations(
        chained, (linked, algorithmic), IDENTITY, version_scope="manager"
    )
    assert mixed.runtime_verified and mixed.bindings[0].previous_name == "Типовой"
    prop, preset = preset_operations()
    neighbour = SetObjectHandler(
        replace(TARGET, pko_address="ПКО/Заказ"), "ПриОтправкеДанных", "Возврат;", "", "none"
    )
    split = prepare_handler_operations(
        value, (prop, preset, neighbour), IDENTITY, version_scope="manager"
    )
    assert {b.target.pko_address for b in split.bindings} == {"ПКО/Товар", "ПКО/Заказ"}
    assert {b.event for b in split.bindings} == {
        "ПередЗаписьюПолученныхДанных",
        "ПриОтправкеДанных",
    }


@pytest.mark.parametrize(
    "body",
    [
        "КонвертацияСвойств.Очистить();",
        "КонвертацияСвойств = Неопределено;",
        "А = КонвертацияСвойств; А.Очистить();",
        'Р = КонвертацияСвойств.Найти("X"); А = Р; А.СвойствоФормата = "X";',
        'КонвертацияСвойств[0].СвойствоФормата = "X";',
        'КонвертацияСвойств.Найти("X").СвойствоФормата = "X";',
        'Р = КонвертацияСвойств.Найти("X"); Р.Очистить();',
        'Для Каждого Р Из КонвертацияСвойств Цикл\nР.СвойствоФормата = "X";\nКонецЦикла;',
    ],
)
def test_property_table_stops(body):
    op = handler("ПередЗаписьюПолученныхДанных", body=body)
    value = handler_inputs()
    checks = {f.id for f in check_body(value, op, AuthoringContext(value)).failures}
    assert "ed.author.body_form_unsupported" in checks


def test_property_table_read_is_allowed():
    value = handler_inputs()
    op = handler(
        "ПередЗаписьюПолученныхДанных",
        body='Р = КонвертацияСвойств.Найти("X"); X = Р.СвойствоФормата;',
    )
    assert not check_body(value, op, AuthoringContext(value)).failures


def test_table_alias_read_and_nested_opaque_arguments_are_not_mutation():
    value = handler_inputs()
    op = handler(
        "ПередЗаписьюПолученныхДанных",
        body='А = КонвертацияСвойств; Р = А.Найти("X"); Р.Прикладной(ЛокальныйОбъект.Очистить());',
    )
    result = check_body(value, op, AuthoringContext(value))
    assert not result.failures
    assert result.opaque_calls


def test_parenthesized_condition_is_not_an_opaque_call():
    value = handler_inputs()
    op = handler(body="Если (Истина) И Не (Ложь) Тогда\nВозврат;\nКонецЕсли;")
    result = check_body(value, op, AuthoringContext(value))
    assert not result.failures and not result.opaque_calls


def test_body_limits():
    value = handler_inputs()
    for body in ("//" + "x" * (64 * 1024), "Возврат;\n" * 2001):
        with pytest.raises(EdAuthoringResourceLimitError):
            check_body(value, handler(body=body), AuthoringContext(value))


def test_opaque_calls_are_not_refusals_and_notice_lists_names_and_body_lines():
    value = handler_inputs(event="ПриОтправкеДанных", previous="Типовой")
    op = handler(
        body="// Комментарий\nТиповой(ДанныеИБ, ДанныеXDTO, КомпонентыОбмена, СтекВыгрузки);\n"
        "Запрос.Выполнить();\nПрикладнойМетод(КомпонентыОбмена);",
        previous="Типовой",
        chain="after_existing",
    )
    result = check_body(value, op, AuthoringContext(value))
    assert not result.failures
    assert [(c.name, c.line) for c in result.opaque_calls] == [
        ("Типовой", 2),
        ("Запрос.Выполнить", 3),
        ("ПрикладнойМетод", 4),
    ]
    plan = prepare_handler_operations(value, (op,), IDENTITY, version_scope="manager")
    notice = next(n for n in plan.notices if n.detail_key == "agent_body")
    for call in result.opaque_calls:
        assert f"{call.name}, строка тела {call.line}" in notice.message
    assert notice.methods == tuple(c.name for c in result.opaque_calls)


def test_event_parameters_can_be_passed_to_arbitrary_static_call():
    value = handler_inputs()
    for event, parameters in EVENT_PARAMETERS.items():
        op = handler(event, body="ПрикладнойМетод(" + ", ".join(parameters) + ");")
        result = check_body(value, op, AuthoringContext(value))
        assert not result.failures
        assert [(c.name, c.line) for c in result.opaque_calls] == [("ПрикладнойМетод", 1)]


def preset_operations():
    prop = AddHeaderProperty(replace(TARGET, direction="receive"), "Заметка", "Комментарий")
    return prop, PreserveMissingHeaderProperty(prop.target, prop.operation_id)


def test_preset_marker_model_matches_cases_for_b():
    fixture = json.loads((HANDLERS / "dto/preserve-two-properties.json").read_text("utf-8"))
    props = tuple(
        op
        for row in fixture["input"]
        if isinstance(op := operation_from_input(row), AddHeaderProperty)
    )
    cases = fixture["marker_cases"]
    assert len(cases) >= 10
    for case in cases:
        result = preservation_marker(
            props, case["marker"], has_received_data=case["has_received_data"]
        )
        assert result == case["expected"], case["name"]
        assert (
            preservation_marker(props, result, has_received_data=case["has_received_data"])
            == result
        )


@pytest.mark.parametrize("version", [1, 2, 3])
def test_preset_supported_and_does_not_remove_other_notices(version):
    value = handler_inputs(version)
    prop, preset = preset_operations()
    plan = prepare_handler_operations(value, (preset, prop), IDENTITY, version_scope="manager")
    assert plan.bindings[0].event == "ПередЗаписьюПолученныхДанных"
    assert "ed.author.missing_value_clears" not in {n.id for n in plan.notices}
    assert "ed.author.preserve_absence_keeps_value" in {n.id for n in plan.notices}
    notice = next(n for n in plan.notices if n.id == "ed.author.preserve_absence_keeps_value")
    assert plan.bindings[0].runtime_verified is (version == 2)
    assert plan.runtime_verified is (version == 2)
    if version == 2:
        assert "ed.author.handler_runtime_unverified" not in {n.id for n in plan.notices}
    assert "Явный пустой элемент очищает реквизит обычного пути" in notice.message
    assert "при отсутствующем или пустом значении реквизит не меняет" in notice.message
    assert "ed.author.value_range" in {n.id for n in plan.notices}
    assert [o.operation_id for o in plan.operations] == [prop.operation_id, preset.operation_id]


def test_preset_conflicts_and_wrong_dependencies():
    value = handler_inputs()
    prop, preset = preset_operations()
    assert_refusal(
        value, (prop, preset, handler("ПриКонвертацииДанныхXDTO")), "preserve_handler_conflict"
    )
    assert_refusal(
        handler_inputs(event="ПередЗаписьюПолученныхДанных", previous="Типовой"),
        (prop, preset),
        "preserve_handler_conflict",
    )
    bad = replace(prop, target=TARGET)
    assert_refusal(
        value,
        (bad, replace(preset, property_operation_id=bad.operation_id)),
        "handler_property_dependency",
    )
    bad = replace(prop, format_property="ОбщиеСвойстваОбъектовФормата.Комментарий")
    assert_refusal(
        value,
        (bad, replace(preset, property_operation_id=bad.operation_id)),
        "preserve_property_unsupported",
    )
    assert_refusal(
        value, (prop, replace(preset, property_operation_id="нет")), "handler_property_dependency"
    )


def test_preset_multiple_properties_share_one_binding_and_reject_composite():
    value = handler_inputs()
    prop, preset = preset_operations()
    other = AddHeaderProperty(
        prop.target,
        "доп_ВтораяЗаметка",
        "Короткий",
        AttributeDraft("доп_ВтораяЗаметка", "Вторая заметка", "string", {"string_length": 50}),
    )
    other_preset = PreserveMissingHeaderProperty(other.target, other.operation_id)
    plan = prepare_handler_operations(
        value, (preset, other_preset, other, prop), IDENTITY, version_scope="manager"
    )
    assert len(plan.bindings) == 1
    assert set(plan.bindings[0].operation_ids) == {preset.operation_id, other_preset.operation_id}
    owner = value.structure.objects[("справочник", "товары")]
    attr = replace(owner.property("Заметка")[0], types=("Строка", "Число"))
    owner = replace(
        owner, properties=MappingProxyType({**owner.properties, ("заметка", ""): (attr,)})
    )
    composite = refreshed(
        value,
        structure=replace(
            value.structure,
            objects=MappingProxyType({**value.structure.objects, ("справочник", "товары"): owner}),
        ),
    )
    assert_refusal(composite, (prop, preset), "preserve_property_unsupported")


def test_algorithmic_send_written_existing_attribute_and_receive_not_supported():
    value = handler_inputs()
    op = handler(body='ДанныеXDTO.Вставить("Комментарий", ДанныеИБ.Заметка);')
    prop = AddAlgorithmicHeaderProperty(TARGET, "Заметка", "Комментарий", op.operation_id)
    plan = prepare_handler_operations(value, (prop, op), IDENTITY, version_scope="manager")
    assert plan.operations[0] == op
    assert plan.runtime_verified and plan.bindings[0].runtime_verified
    assert "ed.author.handler_type_unproven" in {n.id for n in plan.notices}
    assert "ed.author.handler_runtime_unverified" not in {n.id for n in plan.notices}
    wrong = replace(op, body="Возврат;")
    assert_refusal(
        value,
        (wrong, replace(prop, handler_operation_id=wrong.operation_id)),
        "handler_property_unwritten",
    )
    assert_refusal(
        value, (op, replace(prop, configuration_attribute="Нет")), "configuration_attribute_missing"
    )
    recv = handler("ПриКонвертацииДанныхXDTO")
    refused = assert_refusal(
        value,
        (recv, replace(prop, target=recv.target, handler_operation_id=recv.operation_id)),
        "not_supported",
    )
    assert "ed.author.handler_property_dependency" not in {f.id for f in refused.failures}
    assert_refusal(value, (op, replace(prop, conversion_rule="Нет")), "handler_property_rule")


def test_algorithmic_computed_primitive_and_physical_property_path():
    value = handler_inputs()
    for body, field in (
        ('ДанныеXDTO.Вставить("Число", 42);', "Число"),
        ('ДанныеXDTO.ОбщиеСвойстваОбъектовФормата.Комментарий = "x";', "Комментарий"),
    ):
        op = handler(body=body)
        prop = AddAlgorithmicHeaderProperty(TARGET, "Заметка", field, op.operation_id)
        plan = prepare_handler_operations(value, (op, prop), IDENTITY, version_scope="manager")
        assert "ed.author.handler_type_unproven" in {n.id for n in plan.notices}
    op = handler(body='ДанныеXDTO.Вставить("Комментарий", "x");')
    prop = AddAlgorithmicHeaderProperty(TARGET, "Заметка", "Комментарий", op.operation_id)
    assert_refusal(
        value,
        (op, prop, AddHeaderProperty(TARGET, "Заметка", "Комментарий")),
        "handler_slot_conflict",
    )
    occupied = handler(body='ДанныеXDTO.Вставить("Код", "x");')
    assert_refusal(
        value,
        (
            occupied,
            replace(prop, format_property="Код", handler_operation_id=occupied.operation_id),
        ),
        "format_property_occupied",
    )


def reference_inputs():
    value = handler_inputs()
    schema = load_schema(
        HANDLERS / "reference/format-1.20.bin", locate_import=lambda _: DATA / "base/common.bin"
    )
    owner = value.structure.objects[("справочник", "товары")]
    attr = replace(owner.property("Заметка")[0], types=(owner.type_name,))
    owner = replace(
        owner, properties=MappingProxyType({**owner.properties, ("заметка", ""): (attr,)})
    )
    structure = replace(
        value.structure,
        objects=MappingProxyType({**value.structure.objects, ("справочник", "товары"): owner}),
        by_type=MappingProxyType({**value.structure.by_type, owner.type_name.casefold(): owner}),
    )
    return refreshed(value, schemas={**value.schemas, "1.20": schema}, structure=structure)


def test_algorithmic_reference_requires_matching_literal_existing_rule():
    value = reference_inputs()
    op = handler(
        body='ДанныеXDTO.Вставить("Владелец", Новый Структура("ИмяПКО, Значение", "Товар", '
        "ДанныеИБ.Заметка));"
    )
    prop = AddAlgorithmicHeaderProperty(TARGET, "Заметка", "Владелец", op.operation_id, "Товар")
    plan = prepare_handler_operations(value, (prop, op), IDENTITY, version_scope="manager")
    assert plan.operations[-1] == prop
    for conversion in ("", "Нет", "Заказ"):
        assert_refusal(
            value, (op, replace(prop, conversion_rule=conversion)), "handler_property_rule"
        )
    for body, check in (
        (
            'ДанныеXDTO.Вставить("Владелец", Новый Структура("ИмяПКО", Имя));',
            "body_dynamic_reference",
        ),
        (
            'ДанныеXDTO.Вставить("Владелец", Новый Структура("ИмяПКО", "Нет"));',
            "body_reference_unresolved",
        ),
    ):
        bad = replace(op, body=body)
        assert_refusal(value, (bad, replace(prop, handler_operation_id=bad.operation_id)), check)


def test_format_property_runtime_instruction_and_anytype_fields_are_allowed():
    value = reference_inputs()
    op = handler(
        body="X = ДанныеXDTO.Владелец.Значение; X = ДанныеXDTO.AdditionalInfo.СлужебноеПоле;"
    )
    result = check_body(value, op, AuthoringContext(value))
    assert not result.failures
    assert {r.name for r in result.references} == {"Владелец", "AdditionalInfo"}


def test_operation_input_rejects_unknown_kind_event_and_chain():
    value = {"kind": "set_object_handler", **asdict(handler())}
    for changed in ({"kind": "other"}, {"event": "other"}, {"chain": "before_existing"}):
        with pytest.raises(ValueError):
            operation_from_input({**value, **changed})


def test_identity_order_drop_and_body_changes():
    one = handler(body='ДанныеXDTO.Вставить("Комментарий", "x");')
    two = replace(one, body='ДанныеXDTO.Вставить("Комментарий", "y");')
    prop = AddAlgorithmicHeaderProperty(TARGET, "Заметка", "Комментарий", one.operation_id)
    assert one.operation_id != two.operation_id
    assert handler_name("доп_", "Товар", "send", one.event) == handler_name(
        "доп_", "Товар", "send", two.event
    )
    assert canonical_operations_bytes((one, prop)) == canonical_operations_bytes((prop, one, one))
    with pytest.raises(AuthoringPreconditionError):
        merge_operations((one, prop), (two,), drop_operations=(one.operation_id,))
    assert merge_operations(
        (one, prop), (two,), drop_operations=(one.operation_id, prop.operation_id)
    ) == (two,)
    with pytest.raises(AuthoringPreconditionError):
        merge_operations((one,), (two,))
    assert normalize_body('  "текст"\r\n\t| продолжение\r\n\r\n') == '  "текст"\n\t| продолжение\n'
    assert replace(one, body=one.body.replace("\n", "\r\n")).operation_id == one.operation_id


def test_generated_name_collision_and_duplicate_algorithmic_attributes(monkeypatch):
    value = handler_inputs()
    monkeypatch.setattr(
        "kd2_rules_mcp.validation.ed_authoring_handlers.handler_name", lambda *args: "доп_Коллизия"
    )
    other = replace(handler(), target=replace(TARGET, pko_address="ПКО/Заказ"))
    assert_refusal(value, (handler(), other), "handler_name_occupied")
    op = handler(body='ДанныеXDTO.Вставить("Комментарий", "x"); ДанныеXDTO.Вставить("Число", 42);')
    prop = AddAlgorithmicHeaderProperty(TARGET, "Заметка", "Комментарий", op.operation_id)
    assert_refusal(
        value, (op, prop, replace(prop, format_property="Число")), "handler_slot_conflict"
    )


def test_total_body_and_operation_limits():
    value = handler_inputs()
    with pytest.raises(EdAuthoringResourceLimitError):
        prepare_handler_operations(value, (handler(),) * 101, IDENTITY, version_scope="manager")
    bodies = tuple(handler(body="//" + "x" * 63000 + str(i)) for i in range(17))
    with pytest.raises(EdAuthoringResourceLimitError):
        prepare_handler_operations(value, bodies, IDENTITY, version_scope="manager")


def test_direction_names_foreign_hooks_and_stale_snapshot():
    value = handler_inputs()
    op = handler()
    assert_refusal(
        value,
        (replace(op, target=replace(TARGET, direction="receive")),),
        "handler_event_direction",
    )
    name = handler_name(IDENTITY.prefix, "Товар", "send", op.event)
    text = value.document.files[0].text + f"\nПроцедура {name}()\nКонецПроцедуры\n"
    assert_refusal(
        inputs(text=text),
        (op,),
        "handler_name_occupied",
    )
    for target in (DISPATCHER, "ЗаполнитьПравилаКонвертацииОбъектов"):
        foreign = refreshed(
            value,
            extension_sources={
                "CommonModules/Менеджер2/Ext/Module.bsl": (
                    f'&После("{target}")\nПроцедура Чужая()\nКонецПроцедуры'
                )
            },
        )
        assert_refusal(foreign, (op,), "handler_foreign_hook")
    for source in ('X = "не закрыто', '&После("не закрыто'):
        foreign = refreshed(
            value, extension_sources={"CommonModules/Менеджер2/Ext/Module.bsl": source}
        )
        assert_refusal(foreign, (op,), "handler_foreign_hook")
    assert_refusal(
        replace(value, source_set=replace(value.source_set, document_hash="старый")),
        (op,),
        "snapshot_mismatch",
    )


def test_layer_result_has_unbypassable_refusal():
    enforce_handler_result(certain=True, unknown_lines=0, unresolved_dispatch=0)
    with pytest.raises(AuthoringPreconditionError) as caught:
        enforce_handler_result(certain=True, unknown_lines=1, unresolved_dispatch=0)
    assert caught.value.failures[0].id == "ed.author.layer_not_certain"


@pytest.mark.parametrize("path", sorted((HANDLERS / "dto").glob("*.json")), ids=lambda p: p.stem)
def test_canonical_dto_for_writer_b(path):
    fixture = json.loads(path.read_text("utf-8"))
    ops = tuple(operation_from_input(op) for op in fixture["input"])
    previous = next(
        (op for op in ops if isinstance(op, SetObjectHandler) and op.expected_previous), None
    )
    value = (
        reference_inputs()
        if fixture.get("context") == "reference"
        else handler_inputs(
            event=previous.event if previous else None,
            previous=previous.expected_previous if previous else "",
        )
    )
    canonical = canonicalize_operations(value, ops)
    if "previous_input" in fixture:
        previous = tuple(operation_from_input(op) for op in fixture["previous_input"])
        canonical = merge_operations(
            previous, canonical, drop_operations=tuple(fixture["drop_operations"])
        )
    assert canonical_operations_bytes(canonical).decode() == fixture["canonical_utf8"]
    assert [op.operation_id for op in canonical] == fixture["operation_ids"]
    assert canonical_operations_bytes(tuple(reversed(canonical))) == canonical_operations_bytes(
        canonical
    )
    for op in canonical:
        if isinstance(op, SetObjectHandler):
            pko = op.target.pko_address.removeprefix("ПКО/")
            assert fixture["handler_names"][op.operation_id] == handler_name(
                IDENTITY.prefix, pko, op.target.direction, op.event
            )
    if fixture.get("expected_failure"):
        assert_refusal(value, canonical, fixture["expected_failure"].removeprefix("ed.author."))
    else:
        plan = prepare_handler_operations(value, canonical, IDENTITY, version_scope="manager")
        assert json.loads(json.dumps([asdict(b) for b in plan.bindings])) == fixture["bindings"]
        assert plan.dispatcher_name == fixture["dispatcher_name"]
        assert list(plan.dispatcher_order) == fixture["dispatcher_order"]
        assert plan.manager_interface == fixture["manager_interface"]
        assert plan.runtime_verified is fixture["plan_runtime_verified"]
        if "preset_body_file" in fixture:
            exemplar = (HANDLERS / fixture["preset_body_file"]).read_text(encoding="utf-8")
            assert exemplar == preset_procedure_text(
                "доп_Сохранить", (("Комментарий", "доп_Заметка"),)
            )


@pytest.mark.slow
def test_closed_corpus_pilot_preconditions():
    """Настройки закрытого корпуса импортируются только при его наличии."""
    try:
        private = importlib.import_module("tests.private.corpus_private")
    except ImportError:
        pytest.skip("Закрытый корпус отсутствует")
    pilot = Path(__file__).parents[1] / "docs/plans/evals/2026-10-04-ed-handlers-pilot"
    tested = 0
    for side in ("bp", "zup", "zup-h8"):
        modules = list((pilot / side / "extension/CommonModules").glob("*/Ext/Module.bsl"))
        assert len(modules) == 1
        manager = modules[0].parents[1].name
        root = next(
            (
                private.project_dir(p)
                for p in private.PROJECTS
                if (private.project_dir(p) / "CommonModules" / manager / "Ext/Module.bsl").is_file()
            ),
            None,
        )
        if root is None:
            pytest.skip("Выгрузки менеджеров пилота недоступны")
        document_path = root / "CommonModules" / manager / "Ext/Module.bsl"
        before_hash = hashlib.sha256(document_path.read_bytes()).hexdigest()
        document = read_manager(document_path)
        routes = replace(read_routes(root), project="Корпус", configuration="Main")
        plan = next(p for p in routes.plans if p.effective_map().get("1.20") == manager)
        packages = {metadata(p)[1]: p for p in (root / "XDTOPackages").glob("*.xml")}
        schema = load_schema(
            packages["http://v8.1c.ru/edi/edi_stnd/EnterpriseData/1.20"], locate_import=packages.get
        )
        with corpus_structure(root) as connection:
            structure = StructureSnapshot.load(connection)
        value = AuthoringInputs(
            document,
            {"1.20": schema},
            structure,
            routes,
            SourceSet.build(document, {"1.20": schema}, structure, routes),
        )
        context = AuthoringContext(value)
        # Имена ПКО берутся из поиска в исходнике пилота; его BSL не исполняется.
        from kd2_rules_mcp.ed.lexer import tokenize

        tokens = tokenize(modules[0].read_text("utf-8"))
        names = [
            tokens[i + 4].value
            for i in range(len(tokens) - 4)
            if tokens[i].value == "ПравилаКонвертации" and tokens[i + 2].value == "Найти"
        ]
        for name in names:
            rule = next(r for r in document.pko if r.declared_name == name)
            for event in tuple(EVENT_PARAMETERS) if side != "zup-h8" else ("ПриОтправкеДанных",):
                direction = "send" if event == "ПриОтправкеДанных" else "receive"
                target = AuthoringTarget(
                    "Корпус", "Main", plan.plan_name, None, "1.20", direction, "ПКО/" + name
                )
                previous = next((b.target_name for b in rule.events if b.event == event), "")
                is_chained = side == "zup-h8" and "Пользователи" in name
                assert bool(previous) == is_chained
                op = SetObjectHandler(
                    target, event, "Возврат;", previous, "after_existing" if previous else "none"
                )
                if side == "zup" and direction == "receive":
                    # Пилотный ПКО этого менеджера включён только в ветке отправки.
                    assert_refusal(value, (op,), "pko_inactive", context=context)
                else:
                    validate_preconditions(
                        value,
                        (op,),
                        ExtensionIdentity("Демо", "доп_"),
                        version_scope="manager",
                        context=context,
                    )
                if is_chained:
                    assert_refusal(
                        value,
                        (replace(op, chain="none"),),
                        "handler_chain_required",
                        context=context,
                    )
                tested += 1
        assert hashlib.sha256(document_path.read_bytes()).hexdigest() == before_hash
    assert tested >= 3


@pytest.mark.slow
def test_closed_corpus_all_bound_handler_bodies():
    """Каждая базовая привязка трёх событий проверяется как тело агента своего ПКО."""
    try:
        private = importlib.import_module("tests.private.corpus_private")
    except ImportError:
        pytest.skip("Закрытый корпус отсутствует")
    pilot = Path(__file__).parents[1] / "docs/plans/evals/2026-10-04-ed-handlers-pilot"
    managers = []
    for side in ("bp", "zup"):
        module = next((pilot / side / "extension/CommonModules").glob("*/Ext/Module.bsl"))
        manager = module.parents[1].name
        root = next(
            (
                private.project_dir(p)
                for p in private.PROJECTS
                if (private.project_dir(p) / "CommonModules" / manager / "Ext/Module.bsl").is_file()
            ),
            None,
        )
        if root is None:
            pytest.skip("Выгрузки менеджеров пилота недоступны")
        managers.append((side, root, manager))
        if side == "zup":
            managers.append(("zup13", root, "МенеджерОбменаЧерезУниверсальныйФормат13"))
    totals = Counter()
    refusals = Counter()
    reasons = Counter()
    examples = []
    unique = set()
    for side, root, manager in managers:
        document = read_manager(root / "CommonModules" / manager / "Ext/Module.bsl")
        routes = replace(read_routes(root), project="Корпус", configuration="Main")
        packages = {metadata(p)[1]: p for p in (root / "XDTOPackages").glob("*.xml")}
        version = max(
            (v for p in routes.plans for v, name in p.effective_map().items() if name == manager),
            key=lambda v: tuple(map(int, v.split("."))),
        )
        schema = load_schema(
            packages[f"http://v8.1c.ru/edi/edi_stnd/EnterpriseData/{version}"],
            locate_import=packages.get,
        )
        with corpus_structure(root) as connection:
            structure = StructureSnapshot.load(connection)
        value = AuthoringInputs(
            document,
            {version: schema},
            structure,
            routes,
            SourceSet.build(document, {version: schema}, structure, routes),
        )
        context = AuthoringContext(value)
        routines = {r.entity_id: r for r in document.routines}
        files = {f.file_id: f for f in document.files}
        for rule in document.pko:
            for binding in rule.events:
                if binding.event not in EVENT_PARAMETERS or not binding.target_name:
                    continue
                assert binding.target_id is not None
                routine = routines[binding.target_id]
                source = files[routine.body_span.file_id]
                body = source.text[routine.body_span.char_start : routine.body_span.char_end]
                event: HandlerEvent = binding.event
                direction = "send" if event == "ПриОтправкеДанных" else "receive"
                target = AuthoringTarget(
                    "Корпус",
                    "Main",
                    "Пилот",
                    None,
                    version,
                    direction,
                    "ПКО/" + (rule.declared_name or rule.name),
                )
                checked = check_body(
                    value, SetObjectHandler(target, event, body, "", "none"), context
                )
                unique.add((side, routine.entity_id, event))
                totals[side] += 1
                if checked.failures:
                    totals["rejected"] += 1
                    for check in {f.id for f in checked.failures}:
                        refusals[check] += 1
                    for reason in {
                        f.message.split(": ", 1)[1].split("; ПКО", 1)[0] for f in checked.failures
                    }:
                        reasons[reason] += 1
                    examples.append((side, routine.name, tuple(f.id for f in checked.failures)))
                elif checked.opaque_calls:
                    totals["accepted_opaque"] += 1
                else:
                    totals["accepted_plain"] += 1
    count = sum(totals[side] for side, _, _ in managers)
    print(f"BODY_CORPUS: bindings={count}, unique_bodies={len(unique)}, counts={dict(totals)}")
    print(f"BODY_REFUSALS: {dict(refusals)}; reasons={dict(reasons)}")
    assert count > 500
    assert set(refusals) <= {
        "ed.author.body_lexical",
        "ed.author.body_form_unsupported",
        "ed.author.body_parameter_unavailable",
        "ed.author.body_dynamic_reference",
        "ed.author.body_reference_unresolved",
    }
    assert totals["rejected"] <= count * 0.2, examples[:20]
