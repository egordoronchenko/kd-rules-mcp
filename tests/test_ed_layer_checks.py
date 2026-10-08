"""Каждая строка §4: явный оракул нарушения и чистая граница комплектов A/B."""

from dataclasses import asdict, replace
from pathlib import Path
from shutil import copytree

import pytest

from kd_rules_mcp.ed.address import build_addresses
from kd_rules_mcp.ed.layer_model import LayerDescriptor
from kd_rules_mcp.ed.layers import compose_manager, read_layers
from kd_rules_mcp.ed.model import PropertyGroup
from kd_rules_mcp.ed.reader import read_manager_text
from kd_rules_mcp.ed.refs import build_references
from kd_rules_mcp.ed.route_model import FormatExtension, RouteSource
from kd_rules_mcp.ed.routes import apply_route_layers
from kd_rules_mcp.ed.schema import load_schema
from kd_rules_mcp.ed.schema.profile import ValidationProfile
from kd_rules_mcp.validation.ed_layers import (
    LAYER_CHECKS,
    effective_document,
    select_context,
    validate_effective_links,
    validate_layers,
)
from kd_rules_mcp.validation.ed_links import validate_links
from kd_rules_mcp.validation.ed_routes import compare_routes, select_route
from kd_rules_mcp.validation.ed_schema import validate_schema
from kd_rules_mcp.validation.ed_structure import validate_structure
from tests import session_inputs
from tests.test_ed_layers import _reading
from tests.test_ed_profile import DATA, document
from tests.test_validation_ed_structure import snapshot

ROOT = Path(__file__).parent / "data" / "ed" / "layers"
B_PATH = ROOT / "b" / "CommonModules" / "МенеджерДемо" / "Ext" / "Module.bsl"


def read_routes(root, **kwargs):
    # Общий кэш корпуса сохраняет старую сигнатуру. Проверяем публичную функцию,
    # сохранённую обвязкой, без изменения общей инфраструктуры тестов.
    assert session_inputs._orig_read_routes is not None
    return session_inputs._orig_read_routes(root, **kwargs)


@pytest.fixture(scope="module")
def kits():
    return {
        name: read_layers(ROOT / "base", [ROOT / name] if name else [], version_key="1.20")
        for name in ("", "a", "b")
    }


def overlay(base, text, *, name="ДемоB", ordinal=1):
    reading = _reading(base.base, text, name=name, ordinal=ordinal)
    return compose_manager(
        base.base,
        readings=[reading],
        layers=(
            LayerDescriptor("base", 0, "Демо", "base", None, ""),
            LayerDescriptor(reading.layer_id, ordinal, name, "mem", None, ""),
        ),
    )


def report(layered, *, routes=None, direction="send"):
    context = replace(select_context(layered, direction), version_key="1.20")
    return validate_layers(layered, routes or read_routes(ROOT / "base"), context=context)


def issues(result, suffix):
    return [issue for issue in result.issues if issue.check == "ed.layer." + suffix]


def oracle(result, suffix, level, address, message):
    found = issues(result, suffix)
    assert len(found) == 1, result
    issue = found[0]
    assert (issue.check, issue.level.value, issue.address) == ("ed.layer." + suffix, level, address)
    assert issue.message.startswith(message + "; ")
    assert issue.message.rsplit(":", 1)[-1].isdigit()


def test_route_single_version_and_all_keys(kits):
    a = kits["a"]
    readings = tuple(
        replace(
            reading, operations=tuple(op for op in reading.operations if op.field_path != ("1.21",))
        )
        for reading in a.readings
    )
    bad = replace(
        a,
        readings=readings,
        map_entries=tuple(
            entry
            for entry in a.map_entries
            if entry.origin.layer_id == "base" or entry.key == "1.20"
        ),
    )
    routes = read_routes(ROOT / "base", layers=bad)
    found = issues(report(bad, routes=routes), "route.single_version")
    assert len(found) == 2  # одно на карту с узлом и без него
    for issue in found:
        assert issue.level.value == "предупреждение"
        assert issue.address in {
            "Слой/L01-ДемоA/Маршрут/ДемоОбмен",
            "Слой/L01-ДемоA/Маршрут/БезУзла",
        }
        assert issue.message.startswith(
            "Подмена менеджера ДопМенеджер покрывает только 1.20; "
            "при выборе 1.21 действует МенеджерДемо; "
        )
    assert not issues(
        report(a, routes=read_routes(ROOT / "base", layers=a)), "route.single_version"
    )
    one = replace(bad, map_entries=tuple(entry for entry in bad.map_entries if entry.key == "1.20"))
    one_routes = replace(
        routes,
        without_node_entries=tuple(
            entry for entry in routes.without_node_entries if entry.key == "1.20"
        ),
        plans=tuple(
            replace(plan, entries=tuple(entry for entry in plan.entries if entry.key == "1.20"))
            for plan in routes.plans
        ),
    )
    assert not issues(report(one, routes=one_routes), "route.single_version")


def test_route_context_mismatch_and_both_maps(kits):
    a = kits["a"]
    bad = replace(
        a,
        readings=tuple(
            reading
            for reading in a.readings
            if all(op.target_ref != "without_node" for op in reading.operations)
        ),
        map_entries=tuple(
            entry
            for entry in a.map_entries
            if entry.origin.layer_id == "base" or entry.role == "plan"
        ),
    )
    found = issues(
        report(bad, routes=read_routes(ROOT / "base", layers=bad)), "route.context_mismatch"
    )
    assert [
        (issue.level.value, issue.address, issue.message.split("; ")[0]) for issue in found
    ] == [
        (
            "предупреждение",
            f"Слой/L01-ДемоA/Маршрут/ДемоОбмен/{key}",
            f"Подмена ключа {key} различается с узлом (ДопМенеджер) и без узла (МенеджерДемо)",
        )
        for key in ("1.20", "1.21")
    ]
    assert not issues(
        report(a, routes=read_routes(ROOT / "base", layers=a)), "route.context_mismatch"
    )
    opaque = replace(read_routes(ROOT / "base", layers=bad), without_node_status="partial")
    skipped = report(bad, routes=opaque)
    assert not issues(skipped, "route.context_mismatch")
    assert any(skip.check == "ed.layer.route.context_mismatch" for skip in skipped.skipped)


def test_handler_unreachable_and_dispatcher(kits):
    text = B_PATH.read_text(encoding="utf-8")
    start = text.index('&Вместо("ВыполнитьПроцедуруМодуляМенеджера")')
    end = text.index("КонецПроцедуры", start) + len("КонецПроцедуры")
    bad = overlay(kits[""], text[:start] + text[end:])
    oracle(
        report(bad),
        "handler.unreachable",
        "ошибка",
        "Слой/L01-ДемоB/ПКО/ДопЗаказ",
        "Обработчик Доп_Заказ_Отправка назначен правилу ДопЗаказ, "
        "но цепочка диспетчера его не вызывает",
    )
    assert not issues(report(kits["b"]), "handler.unreachable")
    links = validate_effective_links(bad, select_context(bad))
    assert not any(
        issue.check == "ed.handler.missing" and "Доп_Заказ_Отправка" in issue.message
        for issue in links.issues
    )


def test_target_missing_guard_and_existing(kits):
    text = B_PATH.read_text(encoding="utf-8").replace('Найти("Товар",', 'Найти("Несуществующий",')
    bad = overlay(kits[""], text)
    oracle(
        report(bad),
        "target.missing",
        "предупреждение",
        "Слой/L01-ДемоB/Перехват/Доп_ПКО",
        "Правка properties.Code не имеет цели Несуществующий в контексте send",
    )
    assert not issues(report(kits["b"]), "target.missing")


def test_rule_conflict_and_one_layer(kits):
    template = """&После("ЗаполнитьПравилаКонвертацииОбъектов")
Процедура Доп(НаправлениеОбмена, ПравилаКонвертации)
    Правило = ПравилаКонвертации.Найти("Товар", "ИмяПКО");
    Если Правило <> Неопределено Тогда
        Правило.ПриОтправкеДанных = "{name}";
    КонецЕсли;
КонецПроцедуры
"""
    readings = [
        _reading(kits[""].base, template.format(name="H1"), name="Первый", ordinal=1),
        _reading(kits[""].base, template.format(name="H2"), name="Второй", ordinal=2),
    ]
    bad = compose_manager(kits[""].base, readings=readings)
    oracle(
        report(bad),
        "rule.conflict",
        "предупреждение",
        "Слой/L02-Второй/ПКО/Товар",
        "Слои L01-Первый, L02-Второй изменяют Товар; "
        "пересекающиеся поля: ПриОтправкеДанных; итог в этом порядке: L02-Второй",
    )
    assert not issues(
        report(compose_manager(kits[""].base, readings=readings[:1])), "rule.conflict"
    )


def test_format_undeclared_and_both_declarations(kits):
    text = """&После("ЗаполнитьПравилаКонвертацииОбъектов")
Процедура Доп(НаправлениеОбмена, ПравилаКонвертации)
    Правило = ПравилаКонвертации.Найти("Товар", "ИмяПКО");
    Если Правило <> Неопределено Тогда
        ДобавитьПКС(Правило.Свойства, "ДопКод", "Code", 0, "", "urn:demo");
        // объявление
    КонецЕсли;
КонецПроцедуры
"""
    bad = overlay(kits[""], text)
    oracle(
        report(bad),
        "format.undeclared",
        "предупреждение",
        "Слой/L01-ДемоB/ПКО/Товар/ПКС/Code",
        "Пространство urn:demo свойства Code не объявлено: "
        "инициализация ПКО, глобальное объявление URI; "
        "проверьте расширение формата для 1.20",
    )
    clean = overlay(
        kits[""],
        text.replace(
            "// объявление",
            "ОбменДаннымиXDTOСервер.ИнициализироватьРасширениеПравилаКонвертацииОбъекта("
            'Правило, "urn:demo");',
        ),
    )
    routes = replace(
        read_routes(ROOT / "base"),
        format_extensions=(FormatExtension("urn:demo", "1.20", RouteSource("demo", 1, 1, "Доп")),),
    )
    assert not issues(report(clean, routes=routes), "format.undeclared")
    assert not any(
        issue.check == "ed.extension.uninitialized"
        for issue in validate_effective_links(bad, select_context(bad)).issues
    )
    unknown = validate_layers(clean, context=select_context(clean))
    assert any(skip.check == "ed.layer.format.undeclared" for skip in unknown.skipped)


def test_duplicate_add_and_distinct_name(kits):
    text = B_PATH.read_text(encoding="utf-8")
    start = text.index("    Правило = ПравилаКонвертации.Найти")
    end = text.index('    Если НаправлениеОбмена = "Отправка"', start)
    text = text[:start] + text[end:]
    bad = overlay(
        kits[""],
        text.replace('ИмяПКО = "ДопЗаказ"', 'ИмяПКО = "Товар"'),
    )
    oracle(
        report(bad),
        "rule.duplicate",
        "предупреждение",
        "Слой/L01-ДемоB/ПКО/Товар",
        "Добавленное имя Товар уже используется: Слой/base/ПКО/Товар, Слой/L01-ДемоB/ПКО/Товар",
    )
    assert not issues(report(kits["b"]), "rule.duplicate")
    assert not any(
        issue.check == "ed.rule.duplicate"
        for issue in validate_effective_links(bad, select_context(bad)).issues
    )


def test_hook_signature_and_local_names(kits):
    bad = overlay(
        kits[""],
        """&После("ЗаполнитьПравилаКонвертацииОбъектов")
Процедура Доп(ПравилаКонвертации)
КонецПроцедуры
""",
    )
    oracle(
        report(bad),
        "hook.signature",
        "ошибка",
        "Слой/L01-ДемоB/Перехват/Доп",
        "Сигнатура перехвата Доп не соответствует "
        "ЗаполнитьПравилаКонвертацииОбъектов: число параметров",
    )
    clean = overlay(
        kits[""],
        """&После("ЗаполнитьПравилаКонвертацииОбъектов")
Процедура Доп(Первый, Второй)
КонецПроцедуры
""",
    )
    assert not issues(report(clean), "hook.signature")


def test_hook_target_missing_and_unannotated(kits):
    bad = overlay(
        kits[""],
        """&После("НетЗаполнителя")
Процедура Доп(НаправлениеОбмена, ПравилаКонвертации)
КонецПроцедуры
""",
    )
    oracle(
        report(bad),
        "hook.target_missing",
        "ошибка",
        "Слой/L01-ДемоB/Перехват/Доп",
        "Цель перехвата НетЗаполнителя отсутствует в ДемоB",
    )
    assert not issues(report(kits["b"]), "hook.target_missing")
    own = overlay(kits[""], "Процедура Своя()\nКонецПроцедуры\n")
    assert not issues(report(own), "hook.target_missing")


def test_dispatcher_suppressed_and_fallback(kits):
    bad = overlay(
        kits[""],
        B_PATH.read_text(encoding="utf-8").replace("ПродолжитьВызов(ИмяПроцедуры, Параметры);", ""),
    )
    result = report(bad)
    assert issues(result, "dispatcher.suppressed")
    assert not any(skip.check == "ed.layer.dispatcher.suppressed" for skip in result.skipped)
    assert not issues(report(kits["b"]), "dispatcher.suppressed")


def test_control_conflict_and_single(kits):
    text = """&ИзменениеИКонтроль("ЗаполнитьПравилаКонвертацииОбъектов")
Процедура Доп(НаправлениеОбмена, ПравилаКонвертации)
    НепрозрачныйКод();
КонецПроцедуры
"""
    first = _reading(kits[""].base, text, name="Первый", ordinal=1)
    second = _reading(kits[""].base, text, name="Второй", ordinal=2)
    bad = compose_manager(kits[""].base, readings=[first, second])
    oracle(
        report(bad),
        "hook.control_conflict",
        "ошибка",
        "Слой/L02-Второй/Перехват/Доп",
        "Для ЗаполнитьПравилаКонвертацииОбъектов заданы несовместимые перехваты "
        "ИзменениеИКонтроль: L01-Первый, L02-Второй",
    )
    assert not issues(
        report(compose_manager(kits[""].base, readings=[first])), "hook.control_conflict"
    )


def test_uncertain_rules_skip_exact_checks_and_keep_known(kits):
    unknown = overlay(
        kits[""],
        """&После("ЗаполнитьПравилаКонвертацииОбъектов")
Процедура Доп(НаправлениеОбмена, ПравилаКонвертации)
    Правило = ПравилаКонвертации.Найти("Товар", "ИмяПКО");
    Если Правило <> Неопределено Тогда
        Правило.Свойства.Очистить();
    КонецЕсли;
КонецПроцедуры
""",
    )
    ctx = select_context(unknown)
    result = validate_links(unknown, context=ctx)
    assert not result.issues
    assert any(
        skip.check == "ed.handler.missing" and "Действующее/ПКО/Товар" in skip.reason
        for skip in result.skipped
    )
    assert effective_document(unknown, ctx).pko == ()
    assert len(effective_document(unknown, ctx).pod) == 1
    layer = report(unknown)
    assert any(
        skip.check == "ed.layer.reading" and "unknown_call" in skip.reason for skip in layer.skipped
    )
    assert any(skip.check == "ed.layer.runtime" for skip in layer.skipped)


def test_baseline_contexts_union_preserves_existing_issues():
    base = document()
    layered = compose_manager(base)
    ctx = select_context(layered)
    index = build_addresses(base)
    schema = load_schema(DATA / "validation.bin")
    profile = ValidationProfile.build(schema, "1.2", "send")
    snap = snapshot()

    def rows(result):
        return {(i.level, i.check, i.address, i.message) for i in result.issues}

    combined = set()
    for direction in ("send", "receive"):
        current = select_context(layered, direction)
        combined.update(rows(validate_links(layered, context=current)))
    assert combined == rows(validate_links(base, index, build_references(base)))
    assert asdict(validate_schema(base, schema, index, profile, snap)) == asdict(
        validate_schema(layered, schema, index, profile, snap, context=ctx)
    )
    assert asdict(validate_structure(base, snap, index, profile)) == asdict(
        validate_structure(layered, snap, index, profile, context=ctx)
    )


@pytest.mark.parametrize("case", ["D4", "D4b", "H8"])
def test_unknown_dispatcher_skips_unreachable_and_suppressed(kits, case):
    assignment = """&После("ЗаполнитьПравилаКонвертацииОбъектов")
Процедура Доп(НаправлениеОбмена, ПравилаКонвертации)
    Правило = ПравилаКонвертации.Найти("Товар", "ИмяПКО");
    Если Правило <> Неопределено Тогда
        Правило.ПриОтправкеДанных = "Мой";
    КонецЕсли;
КонецПроцедуры
"""
    dispatcher = """&Вместо("ВыполнитьПроцедуруМодуляМенеджера")
Процедура Дисп(ИмяПроцедуры, Параметры)
    Если ИмяПроцедуры = ОбщийМодуль.Имя() Тогда
        Мой(Параметры);
    КонецЕсли;
КонецПроцедуры
"""
    base = kits[""]
    if case == "H8":
        text = base.base.files[0].text.replace(
            '    Если ИмяПроцедуры = "ПКО_Товар_ПриОтправкеДанных" Тогда',
            "    Если ОбщийМодуль.Разрешено(ИмяПроцедуры) Тогда\n"
            '        Выполнить(ИмяПроцедуры + "(Параметры)");\n    КонецЕсли;\n'
            '    Если ИмяПроцедуры = "ПКО_Товар_ПриОтправкеДанных" Тогда',
        )
        base = replace(base, base=read_manager_text(text))
        extension = assignment
    else:
        extension = dispatcher if case == "D4b" else assignment + dispatcher
    layered = overlay(base, extension)
    check = "ed.layer.dispatcher.suppressed" if case == "D4b" else "ed.layer.handler.unreachable"
    result = report(layered)
    assert not any(i.check == check for i in result.issues)
    assert any(
        s.check == check and "L01-ДемоB" in s.reason and ":" in s.reason for s in result.skipped
    )
    assert any(chain.resolution == "unknown" for chain in select_context(layered).dispatch_chains)
    if case != "H8":
        # Литеральное условие само по себе не доказывает полный dispatcher:
        # нужны собственная цель, правильные аргументы и fallback (§2.3 запуска R).
        # Комплект B содержит привязанную ветку с точной сигнатурой события.
        lines = B_PATH.read_text(encoding="utf-8").splitlines(keepends=True)
        clean = overlay(
            base,
            "".join(lines[:2])
            + "\n".join(assignment.splitlines()[2:-1])
            + "\n"
            + "".join(lines[2:]),
        )
        assert issues(report(clean), "handler.unreachable")
        assert not issues(report(clean), "dispatcher.suppressed")
    else:
        clean = overlay(kits[""], assignment)
        assert issues(report(clean), "handler.unreachable")


@pytest.mark.parametrize("second_name", ["Нов", "Другой"])
def test_duplicate_within_one_hook_is_not_overwritten(kits, second_name):
    add = (
        "    П = ОбменДаннымиXDTOСервер.ИнициализироватьПравилоКонвертацииОбъекта"
        "(ПравилаКонвертации);\n"
        '    П.ИмяПКО = "{name}";\n    П.ОбъектФормата = "Catalog.X";\n'
    )
    text = (
        '&После("ЗаполнитьПравилаКонвертацииОбъектов")\n'
        "Процедура Доп(НаправлениеОбмена, ПравилаКонвертации)\n"
        + add.format(name="Нов")
        + add.format(name=second_name)
        + "КонецПроцедуры\n"
    )
    layered = overlay(kits[""], text)
    result = issues(report(layered), "rule.duplicate")
    assert len(result) == (1 if second_name == "Нов" else 0)
    if result:
        assert result[0].level.value == "предупреждение"
        assert result[0].address == "Слой/L01-ДемоB/ПКО/Нов#2"
        assert "Добавленное имя Нов уже используется" in result[0].message
    assert (
        len(
            [
                r
                for r in effective_document(layered, select_context(layered)).pko
                if r.name in {"Нов", "Другой"}
            ]
        )
        == 2
    )
    assert not any(
        i.check == "ed.rule.duplicate"
        for i in validate_effective_links(layered, select_context(layered)).issues
    )


@pytest.mark.parametrize("event", ["ПередЗаписьюПолученныхДанных", "ПриКонвертацииДанныхXDTO"])
def test_suppression_key_preserves_missing_binding_for_another_event(kits, event):
    source = kits[""].base
    text = source.files[0].text.replace(
        'ПравилоКонвертации.ПриОтправкеДанных = "ПКО_Товар_ПриОтправкеДанных";',
        'ПравилоКонвертации.ПриОтправкеДанных = "НетОбработчика";',
    )
    base = replace(kits[""], base=read_manager_text(text))
    layered = overlay(
        base,
        f"""&После("ЗаполнитьПравилаКонвертацииОбъектов")
Процедура Доп(НаправлениеОбмена, ПравилаКонвертации)
    П = ПравилаКонвертации.Найти("Товар", "ИмяПКО");
    Если П <> Неопределено Тогда
        П.{event} = "НетОбработчика";
    КонецЕсли;
КонецПроцедуры
""",
    )
    sent = validate_effective_links(layered, select_context(layered, "send"))
    old = [
        i for i in sent.issues if i.check == "ed.handler.missing" and "НетОбработчика" in i.message
    ]
    assert len(old) == 1 and "ПриОтправкеДанных" in old[0].message
    received = validate_effective_links(layered, select_context(layered, "receive"))
    assert not any(
        i.check == "ed.handler.missing" and "НетОбработчика" in i.message for i in received.issues
    )
    assert len(issues(report(layered, direction="receive"), "handler.unreachable")) == 1
    index = build_addresses(base.base)
    refs = build_references(base.base)
    rule = base.base.pko[0]
    wrong_event = frozenset(
        {("ed.handler.missing", rule.entity_id, "нетобработчика", event.casefold())}
    )
    assert any(
        i.check == "ed.handler.missing"
        for i in validate_links(base.base, index, refs, explained=wrong_event).issues
    )
    same_event = frozenset(
        {("ed.handler.missing", rule.entity_id, "нетобработчика", "приотправкеданных")}
    )
    assert not any(
        i.check == "ed.handler.missing"
        for i in validate_links(base.base, index, refs, explained=same_event).issues
    )


def test_event_bindings_of_other_direction_are_not_checked(kits):
    text = B_PATH.read_text(encoding="utf-8")
    start = text.index('&Вместо("ВыполнитьПроцедуруМодуляМенеджера")')
    end = text.index("КонецПроцедуры", start) + len("КонецПроцедуры")
    layered = overlay(kits[""], text[:start] + text[end:])
    received = validate_effective_links(layered, select_context(layered, "receive"))
    assert not any(
        i.check == "ed.handler.missing" and "Доп_Заказ_Отправка" in i.message
        for i in received.issues
    )
    assert issues(report(layered, direction="send"), "handler.unreachable")


def test_select_context_reports_missing_direction_and_headers(kits):
    assert select_context(kits[""]).direction == "send"
    with pytest.raises(ValueError, match=r"Контекст направления 'other'.*отсутствует"):
        select_context(kits[""], "other")
    absent = replace(kits[""], contexts=tuple(c for c in kits[""].contexts if not c.headers_only))
    with pytest.raises(ValueError, match="headers_only=True отсутствует"):
        select_context(absent, headers_only=True)


def test_unchanged_context_is_directional_and_union_keeps_receive_issue(kits):
    text = kits[""].base.files[0].text
    text = text.replace(
        "    ДобавитьПКО_Товар(ПравилаКонвертации);",
        '    Если НаправлениеОбмена = "Отправка" Тогда\n'
        "        ДобавитьПКО_Товар(ПравилаКонвертации);\n"
        "    Иначе\n        ДобавитьПКО_Получ(ПравилаКонвертации);\n    КонецЕсли;",
        1,
    )
    start = text.index("Процедура ДобавитьПКО_Товар(")
    end = text.index("КонецПроцедуры", start) + len("КонецПроцедуры")
    receive = (
        text[start:end]
        .replace("Товар", "Получ")
        .replace("ПриОтправкеДанных", "ПриКонвертацииДанныхXDTO")
    )
    base = compose_manager(read_manager_text(text + "\n" + receive))
    changed = overlay(
        base,
        """&После("ЗаполнитьПравилаКонвертацииОбъектов")
Процедура Доп(НаправлениеОбмена, ПравилаКонвертации)
    Если НаправлениеОбмена = "Отправка" Тогда
        П = ПравилаКонвертации.Найти("Товар", "ИмяПКО");
        П.ОбъектФормата = "Catalog.Other";
    КонецЕсли;
КонецПроцедуры
""",
    )

    def rows(result):
        return {(i.check, i.address, i.message) for i in result.issues}

    combined = set()
    for direction, names in (("send", {"Товар"}), ("receive", {"Получ"})):
        context = select_context(base, direction)
        assert {r.name for r in effective_document(base, context).pko} == names
        assert {
            r.name for r in effective_document(changed, select_context(changed, direction)).pko
        } == names
        combined.update(rows(validate_effective_links(base, context)))
    raw = base.base
    assert combined == rows(validate_links(raw, build_addresses(raw), build_references(raw)))
    received = validate_effective_links(changed, select_context(changed, "receive"))
    assert any(
        i.check == "ed.handler.missing" and i.address == "ПКО/Получ" for i in received.issues
    )


def test_directional_event_filter_does_not_invent_algorithm_handler_missing(kits):
    text = (
        kits[""]
        .base.files[0]
        .text.replace(
            'ДобавитьПКС(СвойстваШапки, "Наименование", "Description");',
            'ДобавитьПКС(СвойстваШапки, "Наименование", "Description", 1);',
        )
    )
    base = compose_manager(read_manager_text(text))
    for direction in ("send", "receive"):
        result = validate_effective_links(base, select_context(base, direction))
        assert not any(i.check == "ed.algorithm.handler_missing" for i in result.issues)


def test_routes_overlay_history_own_manager_and_no_effect_contract(kits):
    base = read_routes(ROOT / "base")
    assert apply_route_layers(base, kits[""]) is base
    assert asdict(read_routes(ROOT / "base", layers=kits[""])) == asdict(base)
    effective = read_routes(ROOT / "base", layers=kits["a"])
    assert effective.effective_without_node() == {"1.20": "ДопМенеджер", "1.21": "ДопМенеджер"}
    assert {
        (entry.key, entry.state, entry.source.layer) for entry in effective.without_node_entries
    } == {
        (key, state, layer)
        for key in ("1.20", "1.21")
        for state, layer in (("overwritten", "base"), ("effective", "L01-ДемоA"))
    }
    manager = next(manager for manager in effective.managers if manager.name == "ДопМенеджер")
    assert manager.metadata_exists and manager.source_exists and manager.interface_version == 2
    assert not issues(report(kits["a"], routes=effective), "hook.target_missing")


def test_route_documents_observation_and_layers_work_together(kits, monkeypatch):
    from kd_rules_mcp.ed import routes as route_module

    base = kits[""].base
    original = route_module.read_manager
    path = Path(base.files[0].path).resolve()

    def read_manager(file):
        assert Path(file).resolve() != path, "Переданный снимок менеджера должен переиспользоваться"
        return original(file)

    monkeypatch.setattr(route_module, "read_manager", read_manager)
    observed = []
    result = read_routes(
        ROOT / "base", layers=kits["a"], documents={path: base}, observe=observed.append
    )
    monkeypatch.setattr(route_module, "read_manager", original)
    assert asdict(result) == asdict(read_routes(ROOT / "base", layers=kits["a"]))
    hashes = {item.path for item in observed if item.sha256 is not None}
    assert path in hashes
    assert (ROOT / "a" / "CommonModules" / "ДопМенеджер" / "Ext" / "Module.bsl").resolve() in hashes
    assert (
        ROOT / "a" / "ExchangePlans" / "ДемоОбмен" / "Ext" / "ManagerModule.bsl"
    ).resolve() in hashes


def test_table_has_eleven_composition_checks_and_one_body_hint():
    assert len(LAYER_CHECKS) == len(set(LAYER_CHECKS)) == 12
    assert LAYER_CHECKS[-1] == "ed.layer.handler.touches_rules"


def test_layer_report_has_existing_public_shape(kits):
    assert set(asdict(report(kits["b"]))) == {"issues", "skipped"}


def test_effective_schema_structure_violation_clean_and_unknown():
    base = compose_manager(document())
    text = """&После("ЗаполнитьПравилаКонвертацииОбъектов")
Процедура Доп(НаправлениеОбмена, ПравилаКонвертации)
    Правило = ПравилаКонвертации.Найти("Тест", "ИмяПКО");
    Если Правило <> Неопределено Тогда
        {body}
    КонецЕсли;
КонецПроцедуры
"""
    schema = load_schema(DATA / "validation.bin")
    profile = ValidationProfile.build(schema, "1.2", "send")
    snap = snapshot()
    results = []
    for body in (
        'ДобавитьПКС(Правило.Свойства, "НетРеквизита", "НетСвойства");',
        'ДобавитьПКС(Правило.Свойства, "Дата", "Дата");',
        "Правило.Свойства.Очистить();",
    ):
        layer = overlay(base, text.format(body=body))
        ctx = select_context(layer)
        index = build_addresses(layer.base)
        results.append(
            (
                validate_schema(layer, schema, index, profile, snap, context=ctx),
                validate_structure(layer, snap, index, profile, context=ctx),
            )
        )
    assert [
        (issue.check, issue.level.value, issue.address, issue.message)
        for issue in results[0][0].issues
    ] == [
        (
            "ed.schema.property_missing",
            "предупреждение",
            "ПКО/Тест/ПКС/НетСвойства",
            "Свойство формата «НетСвойства» отсутствует в выбранном профиле; "
            "проверьте версию и обработчик.",
        )
    ]
    assert [
        (issue.check, issue.level.value, issue.address, issue.message)
        for issue in results[0][1].issues
    ] == [
        (
            "ed.structure.property_missing",
            "предупреждение",
            "ПКО/Тест/ПКС/НетСвойства",
            "Свойство конфигурации «НетРеквизита» отсутствует в структуре.",
        )
    ]
    assert not results[1][0].issues and not results[1][1].issues
    assert not results[2][0].issues and not results[2][1].issues
    for family, result in zip(("schema", "structure"), results[2], strict=True):
        assert any(
            skip.check == "ed." + family + ".property_missing"
            and "Действующее/ПКО/Тест" in skip.reason
            for skip in result.skipped
        )


def test_route_compare_accepts_layer_and_baseline_contract(kits):
    base = read_routes(ROOT / "base")
    plain = apply_route_layers(base, kits[""])
    assert asdict(compare_routes(base, base, select_route(base, base), {})) == asdict(
        compare_routes(plain, plain, select_route(plain, plain), {})
    )
    effective = apply_route_layers(base, kits["a"])
    choice = select_route(effective, base)
    result = compare_routes(effective, base, choice, {})
    assert choice.selected_key == "1.21"
    assert any(
        skip.check == "ed.route.extensions"
        and skip.reason
        == "Статические слои учтены; активность и порядок подключения в базе не проверены."
        for skip in result.report.skipped
    )


def test_route_format_declaration_uses_existing_grammar():
    layered = read_layers(ROOT / "base", [ROOT / "format"], version_key="1.20")
    routes = read_routes(ROOT / "base", layers=layered)
    assert [
        (item.uri, item.version, item.state, item.source.layer) for item in routes.format_extensions
    ] == [("urn:demo", "1.20", "effective", "L01-ДемоФормат")]
    assert not any(skip.code == "ed.route.format_extensions" for skip in routes.skipped)


def test_routes_before_history_and_opaque_hook(kits):
    a = kits["a"]
    before = replace(a, hooks=tuple(replace(hook, kind="before") for hook in a.hooks))
    routes = apply_route_layers(read_routes(ROOT / "base"), before)
    assert routes.effective_without_node() == {"1.20": "МенеджерДемо", "1.21": "МенеджерДемо"}
    assert all(
        entry.state == "overwritten"
        for entry in routes.without_node_entries
        if entry.source.layer != "base"
    )
    opaque = replace(
        a, hooks=tuple(replace(hook, kind="around", continuation="unknown") for hook in a.hooks)
    )
    uncertain = apply_route_layers(read_routes(ROOT / "base"), opaque)
    assert uncertain.without_node_status == "partial"
    assert uncertain.effective_without_node() == {}
    assert not issues(report(opaque, routes=uncertain), "route.context_mismatch")


def test_delete_existing_target_is_not_missing(kits):
    text = """&После("ЗаполнитьПравилаКонвертацииОбъектов")
Процедура Доп(НаправлениеОбмена, ПравилаКонвертации)
    Правило = ПравилаКонвертации.Найти("Товар", "ИмяПКО");
    Если Правило <> Неопределено Тогда
        ПравилаКонвертации.Удалить(Правило);
    КонецЕсли;
КонецПроцедуры
"""
    assert not issues(report(overlay(kits[""], text)), "target.missing")


@pytest.mark.parametrize("member", ["property", "group"])
def test_changed_property_and_added_group_uri(kits, member):
    mutation = (
        'С = Правило.Свойства.Найти("Description", "СвойствоФормата");\n'
        '        С.ПространствоИмен = "urn:demo";'
        if member == "property"
        else 'ДобавитьПКТЧ(Правило.Свойства, "Items", "Rows", "urn:demo");'
    )
    text = f"""&После("ЗаполнитьПравилаКонвертацииОбъектов")
Процедура Доп(НаправлениеОбмена, ПравилаКонвертации)
    Правило = ПравилаКонвертации.Найти("Товар", "ИмяПКО");
    Если Правило <> Неопределено Тогда
        {mutation}
        ОбменДаннымиXDTOСервер.ИнициализироватьРасширениеПравилаКонвертацииОбъекта(
            Правило, "urn:demo");
    КонецЕсли;
КонецПроцедуры
"""
    layer = overlay(kits[""], text)
    oracle(
        report(layer),
        "format.undeclared",
        "предупреждение",
        "Слой/L01-ДемоB/ПКО/Товар/" + ("ПКС/Description" if member == "property" else "ПКТЧ/Rows"),
        "Пространство urn:demo свойства "
        + ("Description" if member == "property" else "Rows")
        + " не объявлено: глобальное объявление URI; проверьте расширение формата для 1.20",
    )
    routes = replace(
        read_routes(ROOT / "base"),
        format_extensions=(FormatExtension("urn:demo", "1.20", RouteSource("demo", 1, 1, "Доп")),),
    )
    assert not issues(report(layer, routes=routes), "format.undeclared")
    rule = next(
        rule
        for rule in effective_document(layer, select_context(layer)).pko
        if rule.name == "Товар"
    )
    if member == "group":
        assert [(group.format_property, group.namespace) for group in rule.groups] == [
            ("Rows", "urn:demo")
        ]
    else:
        assert (
            next(
                prop for prop in rule.properties if prop.format_property == "Description"
            ).namespace
            == "urn:demo"
        )


def test_baseline_namespace_issue_survives_new_property_same_uri(kits):
    original = kits[""].base
    base = replace(
        original,
        pko=tuple(
            replace(
                rule,
                properties=tuple(
                    replace(prop, namespace="urn:missing")
                    if prop.format_property == "Description"
                    else prop
                    for prop in rule.properties
                ),
            )
            if rule.name == "Товар"
            else rule
            for rule in original.pko
        ),
    )
    layer = overlay(
        compose_manager(base),
        """&После("ЗаполнитьПравилаКонвертацииОбъектов")
Процедура Доп(НаправлениеОбмена, ПравилаКонвертации)
    Правило = ПравилаКонвертации.Найти("Товар", "ИмяПКО");
    Если Правило <> Неопределено Тогда
        ДобавитьПКС(Правило.Свойства, "Extra", "Extra", 0, "", "urn:missing");
    КонецЕсли;
КонецПроцедуры
""",
    )
    old = validate_links(base, build_addresses(base), build_references(base))
    old_issues = [issue for issue in old.issues if issue.check == "ed.extension.uninitialized"]
    effective = validate_effective_links(layer, select_context(layer))
    assert old_issues
    assert [
        issue for issue in effective.issues if issue.check == "ed.extension.uninitialized"
    ] == old_issues
    assert len(issues(report(layer), "format.undeclared")) == 1


def test_route_manager_name_case_is_not_substitution(kits):
    a = kits["a"]
    layer = replace(
        a,
        map_entries=tuple(
            replace(entry, manager_name="менеджердемо")
            if entry.origin.layer_id != "base"
            else entry
            for entry in a.map_entries
        ),
    )
    # Операции с двумя ключами и с одним ключом одинаково сохраняют прежний менеджер.
    for entries in (
        layer.map_entries,
        tuple(
            entry
            for entry in layer.map_entries
            if entry.origin.layer_id == "base" or entry.key == "1.20"
        ),
    ):
        result = report(replace(layer, map_entries=entries))
        assert not issues(result, "route.single_version")
        assert not issues(result, "route.context_mismatch")


def test_route_unknown_hook_without_insert_has_check_skips(kits):
    a = kits["a"]
    unknown = replace(
        a,
        readings=tuple(replace(reading, operations=()) for reading in a.readings),
        map_entries=tuple(entry for entry in a.map_entries if entry.origin.layer_id == "base"),
        hooks=tuple(replace(hook, kind="around", continuation="unknown") for hook in a.hooks),
    )
    result = report(unknown, routes=apply_route_layers(read_routes(ROOT / "base"), unknown))
    for check in LAYER_CHECKS[:2]:
        assert not any(issue.check == check for issue in result.issues)
        assert any(skip.check == check and "/Перехват/" in skip.reason for skip in result.skipped)
    assert not any(skip.check in LAYER_CHECKS[:2] for skip in report(kits["a"]).skipped)


def test_route_format_before_and_unknown(kits):
    layer = read_layers(ROOT / "base", [ROOT / "format"], version_key="1.20")
    profile = replace(
        read_routes(ROOT / "base"),
        format_extensions=(FormatExtension("urn:demo", "1.21", RouteSource("demo", 1, 1, "База")),),
    )
    before = replace(layer, hooks=tuple(replace(hook, kind="before") for hook in layer.hooks))
    result = apply_route_layers(profile, before)
    assert [(entry.version, entry.state) for entry in result.format_extensions] == [
        ("1.20", "overwritten"),
        ("1.21", "effective"),
    ]
    opaque = replace(layer, hooks=tuple(replace(hook, kind="around") for hook in layer.hooks))
    result = apply_route_layers(profile, opaque)
    assert any(skip.code == "ed.route.format_extensions" for skip in result.skipped)
    assert not any(entry.state == "effective" for entry in result.format_extensions)


def test_handler_issue_origin_is_assignment_layer(kits):
    text = """&После("ЗаполнитьПравилаКонвертацииОбъектов")
Процедура Доп(НаправлениеОбмена, ПравилаКонвертации)
    Правило = ПравилаКонвертации.Найти("Товар", "ИмяПКО");
    Если Правило <> Неопределено Тогда
        {operation}
    КонецЕсли;
КонецПроцедуры
"""
    readings = [
        _reading(
            kits[""].base,
            text.format(operation='Правило.ПриОтправкеДанных = "Missing";'),
            name="Первый",
            ordinal=1,
        ),
        _reading(
            kits[""].base,
            text.format(operation='ДобавитьПКС(Правило.Свойства, "Extra", "Extra");'),
            name="Второй",
            ordinal=2,
        ),
    ]
    result = report(compose_manager(kits[""].base, readings=readings))
    found = issues(result, "handler.unreachable")
    assert len(found) == 1
    assert found[0].address == "Слой/L01-Первый/ПКО/Товар"
    assert found[0].message.endswith("; <memory>:5")


@pytest.mark.parametrize(
    "body",
    [
        "ВерсииФормата.Очистить();",
        'X = ВерсииФормата.Удалить("1.20");',
        'Если ВерсииФормата.Удалить("1.20") = Неопределено Тогда X = 1; КонецЕсли;',
        'Если Вычислить("ВерсииФормата.Очистить()") Тогда X = 1; КонецЕсли;',
        "ВерсииФормата = Новый Соответствие;",
        'Если НеизвестныйФлаг Тогда Выполнить("ВерсииФормата.Очистить();"); КонецЕсли;',
        'Пока Вычислить("ВерсииФормата.Очистить();") Цикл X = 1; КонецЦикла;',
        'Попытка Выполнить("ВерсииФормата.Очистить();"); Исключение X = 1; КонецПопытки;',
    ],
)
def test_route_unsupported_mutation_without_insert(tmp_path, body):
    ext = tmp_path / "extension"
    copytree(ROOT / "format", ext)
    path = ext / "CommonModules" / "ОбменДаннымиПереопределяемый" / "Ext" / "Module.bsl"
    template = """&После("ПриПолученииДоступныхВерсийФормата")
Процедура Доп(ВерсииФормата)
    {body}
КонецПроцедуры
"""
    path.write_text(template.format(body=body), encoding="utf-8")
    layered = read_layers(ROOT / "base", [ext], manager="МенеджерДемо", version_key="1.20")
    routes = read_routes(ROOT / "base", layers=layered)
    assert routes.without_node_status == "partial"
    assert routes.effective_without_node() == {}
    result = report(layered, routes=routes)
    for check in LAYER_CHECKS[:2]:
        assert any(skip.check == check for skip in result.skipped)
        assert not any(issue.check == check for issue in result.issues)
    path.write_text(template.format(body="X = ВерсииФормата.Количество();"), encoding="utf-8")
    clean = read_layers(ROOT / "base", [ext], manager="МенеджерДемо", version_key="1.20")
    routes = read_routes(ROOT / "base", layers=clean)
    assert routes.without_node_status == "complete"
    assert routes.effective_without_node() == {"1.20": "МенеджерДемо", "1.21": "МенеджерДемо"}
    assert not any(skip.check in LAYER_CHECKS[:2] for skip in report(clean, routes=routes).skipped)


@pytest.mark.parametrize(
    "body",
    [
        "Настройки.Очистить();",
        'Настройки.Удалить("ВерсииФорматаОбмена");',
        'НоваяКарта = Новый Соответствие; НоваяКарта.Вставить("1.20", ДопМенеджер); '
        "Настройки.ВерсииФорматаОбмена = НоваяКарта;",
    ],
)
def test_route_settings_mutation_is_not_silently_ignored(tmp_path, body):
    ext = tmp_path / "extension"
    copytree(ROOT / "a", ext)
    path = ext / "ExchangePlans" / "ДемоОбмен" / "Ext" / "ManagerModule.bsl"
    template = """&После("ПриПолученииНастроек")
Процедура Доп(Настройки)
    {body}
КонецПроцедуры
"""
    path.write_text(template.format(body=body), encoding="utf-8")
    layered = read_layers(ROOT / "base", [ext], manager="МенеджерДемо", version_key="1.20")
    routes = read_routes(ROOT / "base", layers=layered)
    assert routes.plans[0].status == "partial"
    assert routes.plans[0].effective_map() == {}
    result = report(layered, routes=routes)
    assert any(skip.check == LAYER_CHECKS[1] for skip in result.skipped)
    path.write_text(template.format(body="X = Настройки.Количество();"), encoding="utf-8")
    clean = read_layers(ROOT / "base", [ext], manager="МенеджерДемо", version_key="1.20")
    routes = read_routes(ROOT / "base", layers=clean)
    assert routes.plans[0].status == "complete"


def test_added_group_child_uri_is_checked(kits):
    text = """&После("ЗаполнитьПравилаКонвертацииОбъектов")
Процедура Доп(НаправлениеОбмена, ПравилаКонвертации)
    Правило = ПравилаКонвертации.Найти("Товар", "ИмяПКО");
    Если Правило <> Неопределено Тогда
        ДобавитьПКТЧ(Правило.Свойства, "Items", "Rows");
        ОбменДаннымиXDTOСервер.ИнициализироватьРасширениеПравилаКонвертацииОбъекта(
            Правило, "urn:demo");
    КонецЕсли;
КонецПроцедуры
"""
    reading = _reading(kits[""].base, text)
    prototype = next(rule for rule in kits[""].base.pko if rule.name == "Товар").properties[0]
    operations = []
    for operation in reading.operations:
        if isinstance(operation.value, PropertyGroup):
            group = operation.value
            child = replace(
                prototype,
                entity_id="child",
                group_id=group.entity_id,
                format_property="Child",
                namespace="urn:demo",
                span=group.span,
            )
            operation = replace(operation, value=replace(group, properties=(child,)))
        operations.append(operation)
    layer = compose_manager(
        kits[""].base, readings=[replace(reading, operations=tuple(operations))]
    )
    found = issues(report(layer), "format.undeclared")
    assert len(found) == 1
    assert found[0].address == "Слой/L01-ДемоB/ПКО/Товар/ПКТЧ/Rows/ПКС/Child"
    routes = replace(
        read_routes(ROOT / "base"),
        format_extensions=(FormatExtension("urn:demo", "1.20", RouteSource("demo", 1, 1, "Доп")),),
    )
    assert not issues(report(layer, routes=routes), "format.undeclared")


def test_route_hook_signature_uses_borrowed_module(tmp_path):
    ext = tmp_path / "extension"
    copytree(ROOT / "format", ext)
    path = ext / "CommonModules" / "ОбменДаннымиПереопределяемый" / "Ext" / "Module.bsl"
    text = path.read_text(encoding="utf-8")
    path.write_text(
        text.replace("(РасширенияФормата)", "(РасширенияФормата, Лишний)"), encoding="utf-8"
    )
    layer = read_layers(ROOT / "base", [ext], version_key="1.20")
    oracle(
        report(layer),
        "hook.signature",
        "ошибка",
        "Слой/L01-ДемоФормат/Перехват/Доп_РасширенияФормата",
        "Сигнатура перехвата Доп_РасширенияФормата не соответствует "
        "ПриПолученииДоступныхРасширенийФормата",
    )
    path.write_text(text, encoding="utf-8")
    clean = read_layers(ROOT / "base", [ext], version_key="1.20")
    assert not issues(report(clean), "hook.signature")
