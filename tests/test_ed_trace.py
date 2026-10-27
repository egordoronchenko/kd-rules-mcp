"""Контракт трассировки: синтетические нарушения и доказанные чистые пути."""

from dataclasses import replace
from pathlib import Path
from shutil import copytree

import pytest

from kd_rules_mcp.ed import build_references, read_manager_text
from kd_rules_mcp.ed.address import build_addresses
from kd_rules_mcp.ed.model import Field, ParseStatus
from kd_rules_mcp.ed.schema import load_schema
from kd_rules_mcp.ed.schema.profile import ValidationProfile
from kd_rules_mcp.errors import EdSchemaNotFoundError
from kd_rules_mcp.service import Kd2Service, Settings
from kd_rules_mcp.service import ed_routes as routes_service
from kd_rules_mcp.service.ed_chain import chain_view
from kd_rules_mcp.validation.ed_chain import ChainView
from kd_rules_mcp.validation.ed_trace import trace
from tests import session_inputs
from tests.test_ed_profile import BASE, DATA, document

SEND = BASE.replace(
    '    ПравилоОбработки.Имя = "Тест";',
    '    ПравилоОбработки.Имя = "Тест";\n'
    "    ПравилоОбработки.ОбъектВыборкиМетаданные = Метаданные.Справочники.Тест;",
)
POD_CALL = "    ДобавитьПОД_Тест(ПравилаОбработкиДанных);"
PKO_CALL = "    ДобавитьПКО_Тест(ПравилаКонвертации);"
USE = '    ПравилоОбработки.ИспользуемыеПКО.Добавить("Тест");'


def view(text=SEND, direction="send", schema=True, doc=None):
    doc = doc or document(text)
    profile = ValidationProfile.build(
        load_schema(DATA / "validation.bin") if schema else None, "1.2", direction
    )
    return ChainView.build(
        "p", doc, build_addresses(doc), direction, profile, references=build_references(doc)
    )


def run(text=SEND, direction="send", schema=True, property=None, depth=1, doc=None):
    return trace(view(text, direction, schema, doc), "Тест", property=property, depth=depth)


def step(result, name, path=0):
    return next(s for s in result["paths"][path]["steps"] if s["step"] == name)


def reasons(result):
    return {r["reason"] for r in result["breaks"]}


def two_pods(text=SEND):
    start = text.index("Процедура ДобавитьПОД_Тест(")
    end = text.index("КонецПроцедуры", start) + len("КонецПроцедуры")
    extra = (
        text[start:end]
        .replace("ДобавитьПОД_Тест", "ДобавитьПОД_Второй")
        .replace('Имя = "Тест"', 'Имя = "Второй"')
    )
    return (
        text.replace(POD_CALL, POD_CALL + "\n    ДобавитьПОД_Второй(ПравилаОбработкиДанных);", 1)
        + "\n"
        + extra
    )


def handler_text(text=SEND, direction="send", literal=False):
    event = "ПриОтправкеДанных" if direction == "send" else "ПриКонвертацииДанныхXDTO"
    params = (
        "ДанныеИБ, ДанныеXDTO, КомпонентыОбмена, СтекВыгрузки"
        if direction == "send"
        else "ДанныеXDTO, ПолученныеДанные, КомпонентыОбмена"
    )
    args = ", ".join("Параметры." + p for p in params.split(", "))
    return (
        text.replace(
            "    СвойстваШапки = ПравилоКонвертации.Свойства;",
            f'    ПравилоКонвертации.{event} = "ТестОбработчик";\n'
            "    СвойстваШапки = ПравилоКонвертации.Свойства;",
        ).replace(
            "Процедура ВыполнитьПроцедуруМодуляМенеджера(ИмяПроцедуры, Параметры) Экспорт",
            "Процедура ВыполнитьПроцедуруМодуляМенеджера(ИмяПроцедуры, Параметры) Экспорт\n"
            '    Если ИмяПроцедуры = "ТестОбработчик" Тогда\n'
            f"        ТестОбработчик({args});\n    КонецЕсли;",
        )
        + f"\nПроцедура ТестОбработчик({params})\n"
        + ("    ДанныеXDTO.Дополнение = 1;\n" if literal else "")
        + "КонецПроцедуры\n"
    )


def searching(fields="Код", text=SEND):
    return text.replace(
        "    // <properties>",
        '    ПравилоКонвертации.ВариантИдентификации = "ПоПолямПоиска";\n'
        + f'    ПравилоКонвертации.ПоляПоиска.Добавить("{fields}");',
    )


def test_complete_send_and_without_schema():
    result = run()
    assert result["found"] is True and result["status"] == "found"
    assert result["paths"][0]["verdict"] == "complete"
    assert [(s["step"], s["status"]) for s in result["paths"][0]["steps"]] == [
        ("object", "ok"),
        ("pod", "ok"),
        ("use", "ok"),
        ("pko", "ok"),
        ("type", "ok"),
        ("correspondent", "unknown"),
    ]
    assert step(result, "correspondent")["reasons"] == ["correspondent_declaration"]
    assert step(result, "correspondent")["hint"] == "ed_pair_validate, live_format_support"
    assert result["breaks"] == []
    missing_schema = run(schema=False)
    assert step(missing_schema, "type")["status"] == "unknown"
    assert step(missing_schema, "type")["reasons"] == ["schema_unavailable"]
    assert missing_schema["paths"][0]["verdict"] == "unknown"


def test_roots_exact_names_metadata_and_format_fallback():
    chain = view()
    assert trace(chain, "ПКО/Тест", property=None, depth=1)["matched"] == [
        {"by": "pko", "address": "ПКО/Тест"}
    ]
    assert trace(chain, "тест", property=None, depth=1)["found"] is False
    meta = trace(chain, "справочник.тест", property=None, depth=1)
    assert meta["matched"] == [
        {"by": "configuration", "address": "ПОД/Тест"},
        {"by": "configuration", "address": "ПКО/Тест"},
    ]
    # Точное совпадение метаданных исключает приблизительный поиск по типу.
    format_only = view(SEND.replace("Метаданные.Справочники.Тест", "Метаданные.Справочники.Другой"))
    approximate = trace(format_only, "справочник.тест", property=None, depth=1)
    assert approximate["matched"] == [
        {"by": "format", "address": "ПОД/Тест", "inexact": True},
        {"by": "format", "address": "ПКО/Тест", "inexact": True},
    ]
    exact = trace(format_only, "Справочник.Тест", property=None, depth=1)
    assert all("inexact" not in r for r in exact["matched"])


def test_orphan_and_no_pod_and_wrong_direction():
    orphan = run(SEND.replace(USE, ""))
    assert "pko_not_in_pod" in reasons(orphan)
    absent = run(
        SEND.replace(
            "ОбъектВыборкиМетаданные = Метаданные.Справочники.Тест",
            "ОбъектВыборкиМетаданные = Метаданные.Справочники.Другой",
        )
    )
    assert step(absent, "pod")["reasons"] == ["no_pod"]
    assert absent["paths"][0]["break"]["evidence"] == ["XDTO:8344-8357"]
    other = run(
        SEND.replace(
            POD_CALL,
            '    Если НаправлениеОбмена = "Получение" Тогда\n' + POD_CALL + "\n    КонецЕсли;",
        )
    )
    assert step(other, "pod")["status"] == "broken"
    assert step(other, "pod")["reasons"] == ["not_in_direction"]


def test_receive_orphan_pko_is_a_break():
    # ПОД получения типа ПКО использует другое имя: сам ПКО-корень не выпадает из ответа.
    orphan = run(
        SEND.replace(USE, '    ПравилоОбработки.ИспользуемыеПКО.Добавить("Другой");'),
        direction="receive",
    )
    broken = [p for p in orphan["paths"] if p["break"]["reason"] == "pko_not_in_pod"]
    assert broken and broken[0]["break"]["evidence"] == ["XDTO:6820-6826"]
    assert step(orphan, "format", orphan["paths"].index(broken[0]))["name"] == "Справочник.Тест"


@pytest.mark.parametrize("name", ["НетТакого", "тест"])
@pytest.mark.parametrize(
    "direction,evidence", [("send", "XDTO:794-799"), ("receive", "XDTO:7455-7475")]
)
def test_used_name_is_exact(name, direction, evidence):
    result = run(
        SEND.replace(USE, f'    ПравилоОбработки.ИспользуемыеПКО.Добавить("{name}");'), direction
    )
    assert step(result, "use")["status"] == "broken"
    assert step(result, "use")["reasons"] == ["pod_pko_missing"]
    assert step(result, "use")["evidence"] == [evidence]
    assert step(run(direction=direction), "use")["status"] == "ok"


def test_pko_not_added_in_direction():
    text = SEND.replace(
        PKO_CALL, '    Если НаправлениеОбмена = "Получение" Тогда\n' + PKO_CALL + "\n    КонецЕсли;"
    )
    result = run(text)
    assert step(result, "use")["status"] == "broken"
    assert step(result, "use")["reasons"] == ["not_in_direction"]
    assert "молча пропускается" in step(result, "use")["message"]
    assert result["paths"][0]["verdict"] == "broken"


def test_type_missing_and_receive_pod_type_missing():
    text = SEND.replace('ОбъектФормата = "Справочник.Тест"', 'ОбъектФормата = "НетТипа"')
    result = run(text)
    assert step(result, "type")["reasons"] == ["type_missing"]
    assert result["paths"][0]["break"]["step"] == "type"
    receive = run(
        SEND.replace('ОбъектВыборкиФормат = "Справочник.Тест"', 'ОбъектВыборкиФормат = "НетТипа"'),
        "receive",
    )
    # Корень ПКО даёт тип Справочник.Тест, у которого ПОД больше нет.
    assert "no_pod" in reasons(receive)
    by_type = trace(
        view(
            text.replace(
                'ОбъектВыборкиФормат = "Справочник.Тест"', 'ОбъектВыборкиФормат = "НетТипа"'
            ),
            "receive",
        ),
        "НетТипа",
        property=None,
        depth=1,
    )
    assert step(by_type, "pod")["status"] == "broken"
    assert step(by_type, "pod")["reasons"] == ["type_missing"]
    assert step(by_type, "pod")["evidence"] == ["XDTO:4519", "XDTO:3939-3950"]


def test_opaque_and_shadowing():
    opaque = SEND.replace(POD_CALL, "    Если Неизвестно() Тогда\n" + POD_CALL + "\n    КонецЕсли;")
    result = run(opaque)
    assert step(result, "pod")["status"] == "partial"
    assert step(result, "pod")["reasons"] == ["opaque_condition"]
    assert result["paths"][0]["verdict"] == "partial"
    shadowed = run(two_pods())
    assert step(shadowed, "pod", 1)["reasons"] == ["shadowed_pod"]
    assert shadowed["paths"][1]["verdict"] == "broken"
    maybe = run(
        two_pods().replace(
            POD_CALL, "    Если Неизвестно() Тогда\n" + POD_CALL + "\n    КонецЕсли;", 1
        )
    )
    assert step(maybe, "pod", 1)["status"] == "partial"
    assert step(maybe, "pod", 1)["reasons"] == ["shadowed_maybe"]


def test_receive_pko_type_declaration_and_complete():
    clean = run(direction="receive")
    assert clean["paths"][0]["verdict"] == "complete"
    assert step(clean, "object")["name"] == "Справочник.Тест"
    text = SEND.replace('ОбъектФормата = "Справочник.Тест"', 'ОбъектФормата = "ДругойТип"')
    result = trace(view(text, "receive", False), "Справочник.Тест", property=None, depth=1)
    assert "pko_type_not_declared" in step(result, "pko")["reasons"]
    text = two_pods(text)
    start = text.index("Процедура ДобавитьПОД_Второй(")
    text = text[:start] + text[start:].replace(
        'ОбъектВыборкиФормат = "Справочник.Тест"', 'ОбъектВыборкиФормат = "ДругойТип"'
    )
    declared = trace(view(text, "receive", False), "Справочник.Тест", property=None, depth=1)
    assert "pko_type_not_declared" not in reasons(declared)


@pytest.mark.parametrize(
    "addition,reason,status",
    [
        ('ДобавитьПКС(СвойстваШапки, "Нет", "НетТакого", 0);', "property_missing", "broken"),
        ('ДобавитьПКС(СвойстваШапки, "Номер", "Номер", 0, "НетПКО");', "rule_missing", "broken"),
        ('ДобавитьПКС(СвойстваШапки, "Номер", "Номер", 1);', "handler_fills", "partial"),
        ('ДобавитьПКС(СвойстваШапки, "Номер", "", 0);', "not_in_direction", "broken"),
    ],
)
def test_property_states(addition, reason, status):
    result = run(SEND.replace("// <properties>", addition))
    row = result["properties"][1]
    assert row["status"] == status
    assert reason in row["reasons"]
    assert result["properties"][0] == {
        "pko": "ПКО/Тест",
        "address": "ПКО/Тест/ПКС/Код",
        "configuration_property": "Код",
        "format_property": "Код",
        "status": "ok",
        "key": True,
        "schema_path": "Код",
    }


def test_receive_empty_configuration_and_pkpd():
    result = run(SEND.replace('"Код", "Код", 0', '"", "Код", 0'), "receive")
    assert result["properties"][0]["status"] == "broken"
    assert "not_in_direction" in result["properties"][0]["reasons"]
    pkpd = run(SEND.replace('"Код", "Код", 0', '"Код", "Код", 0, "Выбор"'))
    assert pkpd["properties"][0]["status"] == "ok"
    assert pkpd["cascades"][0]["kind"] == "pkpd"
    assert pkpd["cascades"][0]["status"] == "ok"


def test_property_filter_and_handler_evidence():
    text = SEND.replace('"Код", "Код", 0', '"Артикул", "Код", 0')
    for name in ("артикул", "кОД"):
        result = run(text, property=name)
        assert len(result["properties"]) == 1
        assert result["breaks"] == []
    absent = run(property="Дополнение")
    assert "property_not_in_rules" in reasons(absent)
    handler = run(handler_text(), property="Дополнение")
    assert handler["breaks"][0]["status"] == "partial"
    assert handler["breaks"][0]["reason"] == "handler_may_supply"
    literal_text = handler_text(literal=True)
    literal = run(literal_text, property="дополнение")
    assert literal["breaks"][0]["reason"] == "handler_literal"
    assert (
        literal["breaks"][0]["line"]
        == literal_text[: literal_text.index("    ДанныеXDTO.Дополнение")].count("\n") + 1
    )
    assert literal["handlers"][0]["status"] == "ok"


def test_missing_dispatcher_from_manager_v2():
    text = (DATA.parent / "manager_v2.bsl").read_text("utf-8")
    result = trace(
        view(direction="receive", schema=False, doc=read_manager_text(text)),
        "Товар",
        property=None,
        depth=1,
    )
    # ПКО не стоит в ПОД получения; для проверки привязки используем ПОД Заказы.
    text = text.replace('ИспользуемыеПКО.Добавить("Заказ")', 'ИспользуемыеПКО.Добавить("Товар")')
    result = trace(
        view(direction="receive", schema=False, doc=read_manager_text(text)),
        "Document.Order",
        property=None,
        depth=1,
    )
    handler = next(h for h in result["handlers"] if h["event"] == "ПриКонвертацииДанныхXDTO")
    assert handler["status"] == "broken"
    assert handler["reasons"][0] == "handler_missing"
    assert handler["evidence"] == ["XDTO:8597-8638"]


def test_search_fields_handler_parent_key_and_identification():
    clean = run(searching(), "receive")["search"][0]
    assert clean["identification"] == "ПоПолямПоиска"
    assert clean["variants"][0]["items"] == [
        {"field": "Код", "status": "ok", "format_property": "Код", "key": True}
    ]
    missing = run(searching("Наименование"), "receive")
    assert "search_field_unfilled" in reasons(missing)
    supplied = run(handler_text(searching("Наименование"), "receive"), "receive")
    assert supplied["search"][0]["variants"][0]["items"][0]["reason"] == "handler_may_supply"
    assert supplied["search"][0]["variants"][0]["status"] == "partial"
    parent = run(searching("Родитель"), "receive")
    assert parent["search"][0]["variants"][0]["items"] == [
        {"field": "Родитель", "status": "ok", "reason": "parent_optional"}
    ]
    assert run(direction="receive")["search"][0]["reasons"] == ["identification_unknown"]
    # Признак группы ставит исполнитель: это не поле без ПКС, но и не доказанное значение.
    group = run(searching("ЭтоГруппа"), "receive")["search"][0]["variants"][0]
    assert group["status"] == "partial"
    assert group["items"][0]["reason"] == "executor_fills"
    # Ключ «Родитель» исполнитель сравнивает точно (XDTO:6876).
    lower = run(searching("родитель"), "receive")
    assert "search_field_unfilled" in reasons(lower)


@pytest.mark.parametrize(
    "resolution,status,reason",
    [
        ("call", "ok", None),
        ("no_call", "broken", "handler_missing"),
        ("throws", "broken", "handler_throws"),
        ("unknown", "unknown", "unknown"),
    ],
)
def test_layer_dispatch_resolutions(resolution, status, reason):
    text = handler_text(SEND, "send")
    doc = document(text)
    pko = doc.pko[0]
    events = tuple(replace(e, resolution=resolution) for e in pko.events)
    doc = replace(doc, pko=(replace(pko, events=events),))
    [row] = run(doc=doc)["handlers"]
    assert row["status"] == status
    assert row["address"] == "ПКО/Тест"
    if reason:
        assert row["reasons"][0] == reason


def test_cascade_depth_missing_and_cycles():
    text = SEND.replace('"Код", "Код", 0', '"Код", "Код", 0, "Тест"')
    assert run(text, depth=0)["cascades"] == []
    assert run(text, depth=1)["cascades"] == [
        {
            "from": "ПКО/Тест/ПКС/Код",
            "rule": "Тест",
            "depth": 1,
            "status": "ok",
            "address": "ПКО/Тест",
            "type_status": "resolved",
        }
    ]
    assert len(run(text, depth=3)["cascades"]) == 1
    original = document(text)
    rule = original.pko[0]
    second = replace(
        rule,
        entity_id="second",
        name="Второй",
        properties=(
            replace(
                rule.properties[0], entity_id="second-p", owner_id="second", conversion_rule="Тест"
            ),
        ),
    )
    first = replace(rule, properties=(replace(rule.properties[0], conversion_rule="Второй"),))
    doc = replace(
        original,
        pko=(first, second),
        rule_uses=(
            *original.rule_uses,
            replace(original.rule_uses[0], entity_id="second-u", rule_id="second"),
        ),
    )
    assert [c["rule"] for c in run(doc=doc, depth=3)["cascades"]] == ["Второй", "Тест"]
    missing = run(SEND.replace('"Код", "Код", 0', '"Код", "Код", 0, "Нет"'))
    assert missing["cascades"][0]["reasons"] == ["rule_missing"]
    assert "rule_missing" in reasons(missing)


@pytest.fixture
def service(tmp_path, monkeypatch):
    assert session_inputs._orig_read_routes is not None
    monkeypatch.setattr(routes_service, "read_routes", session_inputs._orig_read_routes)
    return Kd2Service(Settings(cache_dir=tmp_path / "cache", workspace=tmp_path / "workspace"))


def test_service_equals_pure_function_and_pagination(service, tmp_path):
    path = tmp_path / "manager.bsl"
    path.write_text(
        SEND
        + "\n"
        + "\n".join(
            r.raw_text for r in document().routines if r.name in ("ДобавитьПКС", "ДобавитьПКТЧ")
        ),
        encoding="utf-8",
    )
    ident = service.ed_open(path=str(path))["project_id"]
    schema_id = service.ed_schema_open("1.2", path=str(DATA / "validation.bin"))["schema_id"]
    snapshot = service._ed_project(ident)
    pure = trace(
        chain_view(
            ident, snapshot, "send", service._ed_schemas[schema_id], "1.2", with_references=True
        ),
        "Тест",
        property=None,
        depth=1,
    )
    result = service.ed_trace(ident, "Тест", schema_id=schema_id)
    assert result["schema_id"] == schema_id and result["version"] == "1.2"
    for section in ("matched", "paths", "properties", "handlers", "cascades", "breaks"):
        assert result[section]["items"] == pure[section]
    assert "search" not in result
    page = service.ed_trace(ident, "Тест", schema_id=schema_id, offset=1, limit=1)
    assert page["properties"]["items"] == []
    assert page["matched"]["has_more"] is False
    assert service.ed_trace(ident, "Справочник.Тест", limit=1)["matched"]["has_more"] is True
    for kwargs in (
        {"direction": "both"},
        {"depth": 4},
        {"object": "  "},
        {"offset": -1},
        {"limit": 0},
        {"version": "1.9", "schema_id": schema_id},
    ):
        with pytest.raises(ValueError):
            service.ed_trace(ident, **({"object": "Тест"} | kwargs))
    with pytest.raises(EdSchemaNotFoundError):
        service.ed_trace(ident, "Тест", schema_id="missing")


def test_not_found():
    result = trace(view(), "Нет", property=None, depth=1)
    assert result["found"] is False and result["status"] == "not_found"
    assert all(
        result[s] == []
        for s in ("matched", "paths", "properties", "handlers", "search", "cascades", "breaks")
    )
    assert 'ed_list(kind="pko", text="Нет")' in result["notes"][-1]


def test_layers_origin_and_unknown(service, tmp_path):
    root = Path(__file__).parent / "data/ed/layers"
    module = Path("CommonModules/МенеджерДемо/Ext/Module.bsl")
    ident = service.ed_open(
        path=str(root / "base" / module),
        configuration_path=str(root / "base"),
        extensions=[str(root / "b")],
    )["project_id"]
    result = service.ed_trace(ident, "Товар")
    pko = step({"paths": result["paths"]["items"]}, "pko")
    assert pko["origin"]["layer_id"] != "base"
    assert pko["origin"]["changed_by"] == [pko["origin"]["layer_id"]]
    copytree(root, tmp_path / "kits")
    ext = tmp_path / "kits/b" / module
    ext.write_text(
        ext.read_text("utf-8").replace(
            'ДобавитьПКС(Свойства, "ДопКод", "Code");', "НеизвестнаяФункция(ПравилаКонвертации);"
        ),
        encoding="utf-8",
    )
    opened = service.ed_open(
        path=str(tmp_path / "kits/base" / module),
        configuration_path=str(tmp_path / "kits/base"),
        extensions=[str(tmp_path / "kits/b")],
    )["project_id"]
    unknown = service.ed_trace(opened, "Товар")
    link = step({"paths": unknown["paths"]["items"]}, "pko")
    assert link["status"] == "unknown" and "layer_unknown" in link["reasons"]


def test_reader_partial_and_dynamic_type_are_not_complete():
    doc = document(SEND)
    partial = run(doc=replace(doc, parse_status=ParseStatus.PARTIAL))
    assert partial["paths"][0]["verdict"] == "partial"
    assert "reader_partial" in step(partial, "pko")["reasons"]
    dynamic = replace(doc.pko[0], format_object=Field(presence="dynamic"))
    result = run(doc=replace(doc, pko=(dynamic,)))
    assert step(result, "type")["status"] == "partial"
    assert step(result, "type")["reasons"] == ["dynamic_format_type"]


def test_search_partial_and_handler_resolution_states():
    doc = document(handler_text(searching(), "receive"))
    rule = doc.pko[0]
    search = replace(rule.search_sets[0], status=ParseStatus.PARTIAL)
    result = run(direction="receive", doc=replace(doc, pko=(replace(rule, search_sets=(search,)),)))
    assert result["search"][0]["variants"][0]["status"] == "partial"
    assert "reader_partial" in result["search"][0]["variants"][0]["reasons"]
    for resolution, status in (
        ("unknown", "unknown"),
        ("ambiguous", "broken"),
        ("invalid_signature", "broken"),
        ("resolved", "ok"),
    ):
        binding = replace(rule.events[0], resolution=resolution)
        changed = replace(doc, pko=(replace(rule, events=(binding,)),))
        assert run(direction="receive", doc=changed)["handlers"][0]["status"] == status


def test_layer_order_unknown_and_ambiguous_pko():
    doc = document(two_pods())
    second = replace(doc.pod[1], span=replace(doc.pod[1].span, file_id="extension"))
    doc = replace(doc, pod=(doc.pod[0], second))
    chain = replace(view(doc=doc), layered=True)
    result = trace(chain, "Тест", property=None, depth=1)
    assert [step(result, "pod", n)["reasons"] for n in (0, 1)] == [
        ["layer_order_unknown"],
        ["layer_order_unknown"],
    ]
    original = document(SEND)
    duplicate = replace(original.pko[0], entity_id="duplicate")
    result = run(doc=replace(original, pko=(*original.pko, duplicate)))
    # Исполнитель берёт первую строку с именем (XDTO:504, 795): второй ПКО не используется.
    uses = [step(result, "use", n) for n in range(len(result["paths"]))]
    assert uses[0]["status"] == "ok"
    assert "duplicate_name" in uses[1]["reasons"]


def test_all_service_sections_share_pagination(service, tmp_path):
    text = handler_text(searching("НеЗаполняется", two_pods()), "receive")
    text = text.replace('"Код", "Код", 0', '"Код", "Код", 0, "НетПравила"')
    doc = document(text)
    path = tmp_path / "pages.bsl"
    path.write_text(
        text
        + "\n"
        + "\n".join(r.raw_text for r in doc.routines if r.name in ("ДобавитьПКС", "ДобавитьПКТЧ")),
        encoding="utf-8",
    )
    ident = service.ed_open(path=str(path))["project_id"]
    full = service.ed_trace(ident, "Справочник.Тест", direction="receive")
    second = service.ed_trace(ident, "Справочник.Тест", direction="receive", offset=1, limit=1)
    first = service.ed_trace(ident, "Справочник.Тест", direction="receive", offset=0, limit=1)
    for section in ("matched", "paths", "properties", "handlers", "search", "cascades", "breaks"):
        rows = full[section]["items"]
        assert rows, section
        assert second[section]["items"] == rows[1:2], section
        assert second[section]["has_more"] is (len(rows) > 2), section
        assert first[section]["items"] == rows[:1], section
        assert first[section]["has_more"] is (len(rows) > 1), section
    absent = service.ed_trace(ident, "НетОбъекта", direction="receive")
    assert absent["status"] == "not_found"
    assert absent["breaks"]["items"] == []


@pytest.mark.parametrize(
    "presence,value,reason",
    [
        ("literal", "", "empty_format_side"),
        ("dynamic", None, "dynamic_format_type"),
        ("ambiguous", "Справочник.Тест", "opaque_condition"),
    ],
)
def test_unresolved_type_states(presence, value, reason):
    doc = document(SEND)
    rule = replace(doc.pko[0], format_object=Field(presence=presence, value=value))
    result = run(doc=replace(doc, pko=(rule,)))
    assert step(result, "type")["status"] == "partial"
    assert step(result, "type")["reasons"] == [reason]
    assert result["paths"][0]["verdict"] == "partial"
    if not value:
        receive = trace(
            view(direction="receive", doc=replace(doc, pko=(rule,))), "Тест", property=None, depth=1
        )
        assert receive["paths"] and receive["paths"][0]["verdict"] == "partial"


def test_partial_schema_does_not_prove_missing_type_or_property():
    chain = view(SEND.replace('ОбъектФормата = "Справочник.Тест"', 'ОбъектФормата = "НетТипа"'))
    assert chain.profile.schema is not None
    schema = replace(chain.profile.schema, status="partial")
    chain = replace(chain, profile=ValidationProfile.build(schema, "1.2", "send"))
    result = trace(chain, "Тест", property=None, depth=1)
    assert step(result, "type")["status"] == "partial"
    assert "partial_schema" in step(result, "type")["reasons"]
    chain = view(SEND.replace('"Код", "Код", 0', '"Код", "Нет", 1'))
    chain = replace(chain, profile=ValidationProfile.build(schema, "1.2", "send"))
    result = trace(chain, "Тест", property=None, depth=1)
    assert result["properties"][0]["status"] == "partial"
    assert "property_missing" in result["properties"][0]["reasons"]


def test_table_properties_filter_and_schema_path():
    text = SEND.replace(
        "// <properties>",
        'СвойстваТЧ = ДобавитьПКТЧ(ПравилоКонвертации, "Товары", "Товары");\n'
        'ДобавитьПКС(СвойстваТЧ, "Количество", "Количество", 0);',
    )
    result = run(text, property="ТОВАРЫ.КОЛИЧЕСТВО")
    assert len(result["properties"]) == 1
    row = result["properties"][0]
    assert row["table"] == "Товары" and row["status"] == "ok"
    assert row["key"] is False and row["schema_path"] == "Количество"


def test_partial_reader_does_not_claim_absence_and_first_break_is_broken_reason():
    doc = document(SEND.replace(USE, '    ПравилоОбработки.ИспользуемыеПКО.Добавить("Нет");'))
    result = run(doc=replace(doc, parse_status=ParseStatus.PARTIAL))
    assert step(result, "use")["status"] == "partial"
    assert step(result, "use")["reasons"] == ["pod_pko_missing", "reader_partial"]
    assert "pod_pko_missing" not in reasons(result)
    doc = document(SEND.replace("// <properties>", 'ДобавитьПКС(СвойстваШапки, "Код", "Нет", 1);'))
    result = run(doc=doc)
    row = result["properties"][1]
    assert row["reasons"] == ["property_missing", "handler_fills"]
    broken = next(b for b in result["breaks"] if b["section"] == "properties")
    assert broken["reason"] == "property_missing"
    assert broken["message"] == "Свойства нет в типе формата: ПКС молча теряется"


POD_HANDLER = (
    "\nПроцедура ПОД_Тест_ПриОбработке(ДанныеИБ, ИспользованиеПКО, КомпонентыОбмена)\n"
    "    ИспользованиеПКО.Тест = Ложь;\nКонецПроцедуры\n"
)


def test_pod_handler_may_switch_pko_off():
    text = SEND.replace(
        USE, USE + '\n    ПравилоОбработки.ПриОбработке = "ПОД_Тест_ПриОбработке";'
    ).replace(
        "Процедура ВыполнитьПроцедуруМодуляМенеджера(ИмяПроцедуры, Параметры) Экспорт",
        "Процедура ВыполнитьПроцедуруМодуляМенеджера(ИмяПроцедуры, Параметры) Экспорт\n"
        '    Если ИмяПроцедуры = "ПОД_Тест_ПриОбработке" Тогда\n'
        "        ПОД_Тест_ПриОбработке(Параметры.ДанныеИБ, Параметры.ИспользованиеПКО, "
        "Параметры.КомпонентыОбмена);\n    КонецЕсли;",
    )
    for direction in ("send", "receive"):
        result = run(text + POD_HANDLER, direction)
        assert "pod_handler_may_switch" in step(result, "use")["reasons"]
        assert result["paths"][0]["verdict"] == "partial"


def test_root_used_by_pod_of_other_object_has_path():
    text = SEND.replace(
        "ОбъектВыборкиМетаданные = Метаданные.Справочники.Тест",
        "ОбъектВыборкиМетаданные = Метаданные.Справочники.Другой",
    )
    result = trace(view(text), "ПКО/Тест", property=None, depth=1)
    via = [p for p in result["paths"] if any(s["step"] == "use" for s in p["steps"])]
    assert via and step(result, "pod", result["paths"].index(via[0]))["status"] == "ok"


def test_root_without_metadata_follows_its_pod():
    text = SEND.replace(
        "ПравилоКонвертации.ОбъектДанных = Метаданные.Справочники.Тест;",
        "ПравилоКонвертации.ОбъектДанных = Метаданные.НайтиПоПолномуИмени(Имя());",
    )
    result = trace(view(text), "ПКО/Тест", property=None, depth=1)
    assert any(s["step"] == "pod" for p in result["paths"] for s in p["steps"])
    assert all(p["verdict"] != "complete" or len(p["steps"]) > 2 for p in result["paths"])


def test_pod_with_unknown_key_earlier_shadows_maybe():
    dynamic = (
        "\nПроцедура ДобавитьПОД_Дин(ПравилаОбработкиДанных)\n"
        "    ПравилоОбработки = ПравилаОбработкиДанных.Добавить();\n"
        '    ПравилоОбработки.Имя = "Дин";\n'
        "    ПравилоОбработки.ОбъектВыборкиМетаданные = ОбъектПоИмени();\n"
        "КонецПроцедуры\n"
    )
    text = SEND.replace(POD_CALL, "    ДобавитьПОД_Дин(ПравилаОбработкиДанных);\n" + POD_CALL)
    result = run(text + dynamic)
    assert "shadowed_unknown" in step(result, "pod")["reasons"]


def test_property_rule_must_act_in_direction():
    text = SEND.replace(
        "    // <properties>", '    ДобавитьПКС(СвойстваШапки, "Код", "Код", , "Тест2");'
    ).replace(
        PKO_CALL,
        PKO_CALL + '\n    Если НаправлениеОбмена = "Получение" Тогда\n'
        "        ДобавитьПКО_Тест2(ПравилаКонвертации);\n    КонецЕсли;",
    ) + (
        "\nПроцедура ДобавитьПКО_Тест2(ПравилаКонвертации)\n"
        "    ПравилоКонвертации = ОбменДаннымиXDTOСервер."
        "ИнициализироватьПравилоКонвертацииОбъекта(ПравилаКонвертации);\n"
        '    ПравилоКонвертации.ИмяПКО = "Тест2";\n'
        "    ПравилоКонвертации.ОбъектДанных = Метаданные.Справочники.Тест;\n"
        '    ПравилоКонвертации.ОбъектФормата = "Справочник.Тест";\n'
        "КонецПроцедуры\n"
    )
    result = run(text, depth=0)
    assert "rule_missing" in reasons(result)


def test_external_dispatcher_target_is_unknown():
    text = handler_text().replace(
        "        ТестОбработчик(Параметры.", "        ОбщийМодульОбмена.ТестОбработчик(Параметры."
    )
    [row] = run(text)["handlers"]
    assert row["status"] == "unknown"


def test_property_filter_by_table_name():
    text = SEND.replace(
        "    // <properties>",
        '    СвойстваТЧ = ДобавитьПКТЧ(ПравилоКонвертации, "Товары", "Товары");\n'
        '    ДобавитьПКС(СвойстваТЧ, "Количество", "Количество", 0);',
    )
    result = run(text, property="Товары")
    assert "property_not_in_rules" not in reasons(result)
    assert [r["table"] for r in result["properties"]] == ["Товары"]
