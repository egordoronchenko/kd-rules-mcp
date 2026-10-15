"""Ревью снимка правил: строки обёрток, трасса, поиск, запросы и консервативные отказы."""

from pathlib import Path
from time import perf_counter

import anyio
import pytest
from mcp import Client

from kd_rules_mcp.errors import Kd2Error
from kd_rules_mcp.kd2.model import ExchangeRules
from kd_rules_mcp.kd2.queries import parse_queries, split_top
from kd_rules_mcp.kd2.rules_io import load_exchange_rules
from kd_rules_mcp.server import create_server
from kd_rules_mcp.service import Kd2Service, Settings
from kd_rules_mcp.validation.call_graph import build_call_graph, call_graph, validation_evidence
from kd_rules_mcp.validation.structure import unreachable_pko_codes


def review_rules() -> ExchangeRules:
    """Небольшой обмен с алгоритмом, циклом, отключением и двумя ветками одного типа."""
    return load_exchange_rules(
        """<ПравилаОбмена><ВерсияФормата>2.01</ВерсияФормата>
<ПередКонвертациейОбъекта>Маркер = Истина;</ПередКонвертациейОбъекта>
<ПравилаКонвертацииОбъектов>
 <Правило><Код>Док</Код><Источник>ДокументСсылка.Пример</Источник>
  <Приемник>ДокументСсылка.Назначение</Приемник><Свойства>
   <Свойство><Код>1</Код><Приемник Имя="Владелец"/>
    <КодПравилаКонвертации>Спр</КодПравилаКонвертации></Свойство>
   <Свойство Отключить="true"><Код>2</Код><Приемник Имя="Отключено"/>
    <КодПравилаКонвертации>Скрытый</КодПравилаКонвертации>
    <ПередВыгрузкой>ВыгрузитьПоПравилу(Объект,,,, "Скрытый");</ПередВыгрузкой>
   </Свойство>
  </Свойства>
 </Правило>
 <Правило><Код>Спр</Код><Источник>СправочникСсылка.Пример</Источник><Свойства>
  <Свойство Поиск="true"><Код>1</Код><Приемник Имя="Код"/></Свойство>
  </Свойства></Правило>
 <Правило><Код>Вызванный</Код><Источник>СправочникСсылка.Другой</Источник></Правило>
 <Правило><Код>Скрытый</Код><Источник>СправочникСсылка.Скрытый</Источник></Правило>
</ПравилаКонвертацииОбъектов>
<ПравилаВыгрузкиДанных>
 <Правило><Код>Первая</Код><ОбъектВыборки>ДокументСсылка.Пример</ОбъектВыборки>
  <КодПравилаКонвертации>Док</КодПравилаКонвертации>
  <СпособОтбораДанных>ПроизвольныйАлгоритм</СпособОтбораДанных>
  <ПередОбработкойПравила>// ВыгрузитьПоПравилу(Объект,,,, "Скрытый");
Выполнить(Алгоритмы.Первый);</ПередОбработкойПравила></Правило>
 <Правило><Код>Вторая</Код><ОбъектВыборки>ДокументСсылка.Пример</ОбъектВыборки></Правило>
</ПравилаВыгрузкиДанных>
<Алгоритмы><Алгоритм Имя="Первый"><Текст>
ВыгрузитьПоПравилу(Объект,,,, ?(Условие, "Вызванный", "Док"));
Выполнить(Алгоритмы.Первый);
</Текст></Алгоритм></Алгоритмы>
</ПравилаОбмена>""".encode()
    )


@pytest.fixture
def service(tmp_path: Path) -> Kd2Service:
    service = Kd2Service(Settings(cache_dir=tmp_path / "cache", workspace=tmp_path / "work"))
    source = tmp_path / "ExchangeRules.xml"
    from kd_rules_mcp.kd2.rules_io import dump_rules

    source.write_bytes(dump_rules(review_rules()))
    service.rules_open(str(source))
    return service


def project_id(service: Kd2Service) -> str:
    return service.workspace.ids()[0]


def test_query_batch_and_result_indices() -> None:
    text = (Path(__file__).parent / "data" / "review-handler.bsl").read_text(encoding="utf-8")
    batch = parse_queries(text, 10)["batches"][0]
    assert batch["complete"]
    assert len(batch["queries"]) == 2
    assert batch["queries"][0]["temporary_tables"] == ["ВТОбъекты"]
    assert batch["queries"][1]["dependencies"] == [{"name": "ВТОбъекты", "query_index": 0}]
    assert batch["parameters"] == [
        {"name": "Начало", "expression": "НачалоДня(ДатаНачала)", "line": 21}
    ]
    assert batch["executions"][0]["query_indices"] == [0, 1]
    assert [r["query_index"] for r in batch["results"]] == [1, "undefined"]


@pytest.mark.parametrize(
    ("expression", "reason"),
    [
        ('"ВЫБРАТЬ " + Фрагмент', "concatenation"),
        ('СтрЗаменить("ВЫБРАТЬ &Поле", "&Поле", Поле)', "СтрЗаменить"),
        ('СтрШаблон("ВЫБРАТЬ %1", Поле)', "СтрШаблон"),
        ("ТекстЗапроса", "dynamic_expression"),
    ],
)
def test_query_dynamic_text(expression: str, reason: str) -> None:
    result = parse_queries(f"Запрос.Текст = {expression}; Р = Запрос.ВыполнитьПакет(); Р[0];")
    batch = result["batches"][0]
    assert not batch["complete"]
    assert batch["reason"] == reason
    assert batch["executions"][0]["query_indices"] == "undefined"
    assert batch["results"][0]["query_index"] == "undefined"


@pytest.mark.parametrize(
    "text", ["", 'Запрос.Текст = "ВЫБРАТЬ', "Запрос.Выполнить(", '// Запрос.Текст = "ВЫБРАТЬ 1";']
)
def test_query_malformed_does_not_raise(text: str) -> None:
    assert isinstance(parse_queries(text)["diagnostics"], list)


def test_query_constructor_execute_and_multiple_variables() -> None:
    result = parse_queries("""А = Новый Запрос("ВЫБРАТЬ 1; ВЫБРАТЬ 2");
Б = Новый Запрос("ВЫБРАТЬ 3"); А.Выполнить(); Б.ВыполнитьПакет();""")
    assert result["batches"][0]["executions"][0]["query_indices"] == [1]
    assert result["batches"][1]["executions"][0]["query_indices"] == [0]
    assert len(split_top('ВЫБРАТЬ "a;"; // ;\nВЫБРАТЬ Ф(1, "b;")')) == 2


def test_graph_calls_and_algorithm_locations() -> None:
    edges = call_graph(review_rules())
    assert "Скрытый" not in {edge.target for edge in edges}
    chained = next(e for e in edges if e.target == "Вызванный" and len(e.chain) == 2)
    assert chained.address == "алгоритм «Первый»"
    assert "ПВД «Первая»" in chained.callers
    assert chained.chain[0]["handler_line"] == 2
    assert chained.chain[1]["handler_line"] == 2


def test_trace_branches_cascades_and_checks(service: Kd2Service) -> None:
    trace = service.rules_trace(project_id(service), "Документ.Пример")
    assert [b["first_for_type"] for b in trace["branches"]["items"]] == [True, False]
    assert trace["branches"]["items"][0]["checks"][0]["check"] == "handlers.pvd_arbitrary"
    assert {r["code"] for r in trace["pko"]["items"]} >= {"Док", "Спр", "Вызванный"}
    assert trace["cascades"]["items"][0]["search_fields"] == ["Код"]
    assert trace["events"]["total"] == 1
    assert not trace["registration"]["available"]
    assert service.rules_trace(project_id(service), "Док", depth=0)["cascades"]["total"] == 0
    assert (
        service.rules_trace(project_id(service), "Документ.Назначение", "import")["pko"]["total"]
        > 0
    )


def test_read_search_export_locations_and_live_edits(service: Kd2Service, tmp_path: Path) -> None:
    pid = project_id(service)
    address = "ПВД «Первая»"
    handler = "ПередОбработкойПравила"
    read = service.handlers_read(pid, address, handler, limit=1)
    assert read["has_more"] and read["items"][0]["handler_line"] == 1
    found = service.handlers_search(pid, "Алгоритмы", rule=address, handler=handler)["items"][0]
    export = service.handlers_export(pid, "handlers", 200)
    assert export["count"] > 0
    located = service.handlers_locate(pid, found["file"], found["line"])
    assert located["address"] == address and located["handler_line"] == 2
    body = (Path(export["folder"]) / found["file"]).read_text(encoding="utf-8-sig").splitlines()
    assert body[found["line"] - 1] == found["text"]
    assert service.handlers_search(pid, "Алго.*", "regex")["total"] > 0
    assert service.handlers_search(pid, "Алгоритмы", offset=100)["items"] == []
    service.rule_update(pid, "pvd", "Первая", fields={handler: "НовыйМаркер = 1;"})
    assert service.handlers_search(pid, "НовыйМаркер")["total"] == 1
    assert service.handlers_search(pid, "Алгоритмы", rule=address)["total"] == 0
    # Восстановление снимка не требует сохранения исходного XML.
    restored = Kd2Service(service.settings)
    assert restored.handlers_search(pid, "НовыйМаркер")["total"] == 1


def test_queries_by_address_and_file(service: Kd2Service) -> None:
    pid = project_id(service)
    text = (Path(__file__).parent / "data" / "review-handler.bsl").read_text(encoding="utf-8")
    service.rule_update(pid, "pko", "Док", fields={"ПередВыгрузкой": text})
    read = service.handlers_read(pid, "ПКО «Док»", "ПередВыгрузкой")
    by_address = service.handlers_queries(pid, address="ПКО «Док»", handler="ПередВыгрузкой")
    by_file = service.handlers_queries(pid, file=read["file"])
    assert by_file == by_address
    assert by_file["items"][0]["parameters"][0]["line"] == read["items"][10]["line"]


def test_registration_sibling_and_open_snapshot(service: Kd2Service, tmp_path: Path) -> None:
    pid = project_id(service)
    registration = tmp_path / "RegistrationRules.xml"
    registration.write_text(
        """<ПравилаРегистрации><ВерсияФормата>2.01</ВерсияФормата>
<ПравилаРегистрацииОбъектов><Правило Валидное="true" Отключить="true">
<Код>ПРО1</Код><ОбъектМетаданныхИмя>Документ.Пример</ОбъектМетаданныхИмя>
<РеквизитРежимаВыгрузки>ВыгружатьПример</РеквизитРежимаВыгрузки>
<ПередОбработкой>Маркер = 1;</ПередОбработкой>
</Правило></ПравилаРегистрацииОбъектов></ПравилаРегистрации>""",
        encoding="utf-8",
    )
    row = service.rules_trace(pid, "Док")["registration"]["items"][0]
    assert row["disabled"] and row["Валидное"]
    assert row["node_mode_value"] == "unknown"
    assert row["РеквизитРежимаВыгрузки"] == "ВыгружатьПример"
    reg_id = service.rules_open(str(registration))["project_id"]
    document = service.workspace.get(reg_id).document
    assert document is not None
    pro_rules = document.root.child("ПравилаРегистрацииОбъектов")
    assert pro_rules is not None
    pro_rules.items[0].attrs["Отключить"] = False
    row = service.rules_trace(pid, "Док")["registration"]["items"][0]
    assert not row["disabled"]
    hit = service.handlers_search(reg_id, "Маркер")["items"][0]
    service.handlers_export(reg_id, "registration-handlers", 50)
    located = service.handlers_locate(reg_id, hit["file"], hit["line"])
    assert located["address"] == hit["address"]


def test_trace_cache_tracks_unsaved_edits(service: Kd2Service) -> None:
    pid = project_id(service)
    first = service.rules_trace(pid, "Док")
    assert any(c["target"] == "Вызванный" for c in first["calls"]["items"])
    service.rule_update(pid, "pvd", "Первая", fields={"ПередОбработкойПравила": "БезВызова = 1;"})
    changed = service.rules_trace(pid, "Док")
    assert not any(
        c["target"] == "Вызванный" and c["address"] == "ПВД «Первая»"
        for c in changed["calls"]["items"]
    )


def test_queries_reassignment_parameters_and_non_numeric_indices() -> None:
    result = parse_queries("""Запрос = Новый Запрос;
Запрос.УстановитьПараметр("П", 1);
Запрос.Текст = "ВЫБРАТЬ 1; ВЫБРАТЬ 2";
Р = Запрос.ВыполнитьПакет(); Р["1"]; Р[99999999999999999999999999999];
Р = ДругойРезультат; Р[0]; Запрос = ДругойЗапрос; Запрос.Выполнить();""")
    batch = result["batches"][0]
    assert batch["parameters"][0]["expression"] == "1"
    assert len(batch["executions"]) == 1
    assert [r["query_index"] for r in batch["results"]] == ["undefined", "undefined"]


def test_execute_string_and_ordinary_string_are_distinct() -> None:
    rules = review_rules()
    rules.pvd()[0].values["ПередОбработкойПравила"] = """
Сообщить("ВыгрузитьПоПравилу(Объект,,,, ""Скрытый"")");
Выполнить("ВыгрузитьПоПравилу(Объект,,,, ""Вызванный"")");"""
    edges = call_graph(rules)
    assert "Скрытый" not in {edge.target for edge in edges}
    assert any(e.target == "Вызванный" and e.kind == "execute_string" for e in edges)


def test_graph_conditional_targets_and_dynamic_names() -> None:
    rules = review_rules()
    rules.pvd()[0].values["ПередОбработкойПравила"] = """
ВыгрузитьПоПравилу(Объект,,,, ?(Имя = "Скрытый", "Док", "Вызванный"));
ВыгрузитьПоПравилу(Объект,,,, "Док" + Суффикс);"""
    edges = call_graph(rules)
    assert "Скрытый" not in {e.target for e in edges}
    assert any(e.kind == "dynamic_call" and e.chain[-1]["target_expression"] for e in edges)


@pytest.mark.parametrize(
    "operation",
    [
        lambda s, p: s.handlers_search(p, "[", "regex"),
        lambda s, p: s.handlers_search(p, "", "substring"),
        lambda s, p: s.handlers_search(p, "x", "other"),
        lambda s, p: s.handlers_read(p, "missing", "event"),
        lambda s, p: s.handlers_queries(p),
        lambda s, p: s.handlers_queries(p, address="x", file="x"),
        lambda s, p: s.rules_trace(p, "Док", direction="other"),
        lambda s, p: s.rules_trace(p, "Док", depth=11),
        lambda s, p: s.handlers_search(p, "x", offset=-1),
    ],
)
def test_review_rejections(service: Kd2Service, operation) -> None:
    with pytest.raises(Kd2Error):
        operation(service, project_id(service))


def test_review_mcp_tools(service: Kd2Service) -> None:
    async def exercise() -> None:
        async with Client(create_server(service)) as client:
            for name, arguments in (
                ("rules_trace", {"object": "Док"}),
                ("handlers_search", {"query": "Алгоритмы"}),
                ("handlers_read", {"address": "ПВД «Первая»", "handler": "ПередОбработкойПравила"}),
                ("handlers_queries", {"address": "ПВД «Первая»"}),
            ):
                result = await client.call_tool(
                    name, {"project_id": project_id(service), **arguments}
                )
                assert not result.is_error, result

    anyio.run(exercise)


@pytest.mark.parametrize(
    ("metadata", "value_type"),
    [
        ("РегистрСведений", "РегистрСведенийЗапись"),
        ("РегистрНакопления", "РегистрНакопленияЗапись"),
        ("РегистрБухгалтерии", "РегистрБухгалтерииЗапись"),
        ("РегистрРасчета", "РегистрРасчетаЗапись"),
        ("Константа", "КонстантыНабор"),
    ],
)
def test_b1_trace_registration_type_aliases(
    service: Kd2Service, tmp_path: Path, metadata: str, value_type: str
) -> None:
    pid = project_id(service)
    rules = service.workspace.get(pid).document
    assert isinstance(rules, ExchangeRules)
    rules.pko()[0].values["Источник"] = f"{value_type}.Пример"
    for pvd in rules.pvd():
        pvd.values["ОбъектВыборки"] = f"{value_type}.Пример"
    (tmp_path / "RegistrationRules.xml").write_text(
        f"""<ПравилаРегистрации>
<ВерсияФормата>2.01</ВерсияФормата><ПравилаРегистрацииОбъектов>
<Правило Валидное="true"><Код>Р</Код><ОбъектМетаданныхИмя>{metadata}.Пример</ОбъектМетаданныхИмя>
</Правило></ПравилаРегистрацииОбъектов></ПравилаРегистрации>""",
        encoding="utf-8",
    )
    responses = [
        service.rules_trace(pid, name)
        for name in (
            f"{metadata}.Пример",
            f"{value_type}.Пример",
            f"{value_type}.Пример".lower(),
            "док",
        )
    ]
    assert all(response == responses[0] for response in responses)
    assert responses[0]["registration"]["total"] == 1
    assert responses[0]["branches"]["total"] == 2


def test_b2_default_pko_is_last_loaded_even_with_disabled_attribute(service: Kd2Service) -> None:
    from kd_rules_mcp.kd2.model import Node

    pid = project_id(service)
    rules = service.workspace.get(pid).document
    assert isinstance(rules, ExchangeRules)
    last = Node.new("pko", "Правило")
    last.values.update({"Код": "Последний", "Источник": "ДокументСсылка.Пример"})
    last.attrs["Отключить"] = True
    rules.section("ПравилаКонвертацииОбъектов").items.append(last)
    for key in ("Документ.Пример", "Док", "Первая", "Последний"):
        trace = service.rules_trace(pid, key)
        implicit = [c for c in trace["calls"]["items"] if c["kind"] == "branch_properties"]
        assert {c["target"] for c in implicit} == {"Последний"}
        assert any(row["code"] == "Последний" for row in trace["pko"]["items"])


def test_b3_named_query_is_read_from_model(service: Kd2Service) -> None:
    from kd_rules_mcp.kd2.model import Node

    pid = project_id(service)
    rules = service.workspace.get(pid).document
    assert isinstance(rules, ExchangeRules)
    query = Node.new("query", "Запрос")
    query.attrs["Имя"] = "Пример"
    query.values["Текст"] = "ВЫБРАТЬ 1 ПОМЕСТИТЬ ВТ; ВЫБРАТЬ * ИЗ ВТ"
    rules.section("Запросы").items.append(query)
    rules.pvd()[0].values["ПередОбработкойПравила"] = """
Запросы.Пример.УстановитьПараметр("П", 1);
Р = Запросы.Пример.ВыполнитьПакет(); Р[1];"""
    batch = service.handlers_queries(pid, address="ПВД «Первая»")["items"][0]
    assert batch["query_name"] == "Пример" and batch["complete"]
    assert batch["executions"][0]["query_indices"] == [0, 1]
    assert batch["results"][0]["query_index"] == 1
    assert batch["parameters"][0]["name"] == "П"


def test_named_query_mutation_retains_name_and_marks_incomplete() -> None:
    batch = parse_queries(
        """Запросы.Пример.Текст = СтрЗаменить(ИсходныйТекст, "До", "После");
Запросы.Пример.УстановитьПараметр("П", 1);
Р = Запросы.Пример.ВыполнитьПакет();""",
        named_queries={"Пример": "ВЫБРАТЬ 1"},
    )["batches"][0]
    assert batch["query_name"] == "Пример"
    assert not batch["complete"] and batch["reason"] == "СтрЗаменить"
    assert batch["queries"] == []
    assert batch["executions"][0]["query_indices"] == "undefined"
    assert batch["parameters"][0]["name"] == "П"


def test_b4_branch_texts_share_common_execution_and_parameters() -> None:
    parsed = parse_queries("""Запрос = Новый Запрос;
Если А Тогда Запрос.Текст = "ВЫБРАТЬ 1; ВЫБРАТЬ 2";
ИначеЕсли Б Тогда Запрос.Текст = "ВЫБРАТЬ 3";
Иначе Запрос.Текст = "ВЫБРАТЬ 4"; КонецЕсли;
Запрос.УстановитьПараметр("П", 1); Р = Запрос.ВыполнитьПакет(); Р[0];""")
    assert len(parsed["batches"]) == 3
    for batch in parsed["batches"]:
        assert not batch["complete"] and batch["reason"] == "branching_text"
        assert batch["executions"][0]["query_indices"] == "undefined"
        assert batch["parameters"][0]["name"] == "П"
        assert batch["results"][0]["query_index"] == "undefined"


def test_b5_execute_continuations_and_literal_on_next_line() -> None:
    rules = review_rules()
    rules.pvd()[0].values["ПередОбработкойПравила"] = """Выполнить(
"ВыгрузитьПоПравилу(Объект,,,,
|""Вызванный"");");"""
    edge = next(e for e in call_graph(rules) if e.kind == "execute_string")
    assert edge.target == "Вызванный"
    assert edge.chain[-1]["handler_line"] == 2
    assert edge.chain[-1]["line"] == 9


def test_b6_property_evidence_contains_owner_and_path() -> None:
    assert validation_evidence(review_rules(), "Спр") == [
        {"kind": "field", "address": "ПКО «Док» / ПКС Владелец"}
    ]


def test_b7_group_disable_is_not_inherited() -> None:
    from kd_rules_mcp.kd2.model import Node

    rules = review_rules()
    for section, kind in (
        ("ПравилаКонвертацииОбъектов", "pko_group"),
        ("ПравилаВыгрузкиДанных", "pvd_group"),
    ):
        group = Node.new(kind, "Группа")
        group.attrs["Отключить"] = True
        group.items = rules.section(section).items
        rules.section(section).items = [group]
    graph = build_call_graph(rules)
    assert graph.targets >= {"док", "спр", "вызванный"}
    assert graph.branches[0].first_for_type
    assert "Док" not in unreachable_pko_codes(rules, graph)


def test_b8_comparison_does_not_create_assignment_edge() -> None:
    rules = review_rules()
    rules.pvd()[0].values["ПередОбработкойПравила"] = """Если ИмяПКО = "Скрытый" Тогда
ИмяПКО = "Вызванный"; КонецЕсли;"""
    assignments = [e for e in call_graph(rules) if e.kind == "pko_assignment"]
    assert {e.target for e in assignments} == {"Вызванный"}


def test_b9_message_text_is_not_query() -> None:
    assert (
        parse_queries("""X = Новый СообщениеПользователю;
X.Текст = "Сообщение; проверьте настройки"; X.Сообщить();""")["batches"]
        == []
    )
    assert len(parse_queries('X.Текст = "SELECT 1";')["batches"]) == 1


def test_query_english_subquery_dependencies_and_comparison() -> None:
    parsed = parse_queries("""Q = New Query("SELECT 1 INTO TT1; SELECT 2 INTO TT2;
|SELECT * FROM (SELECT * FROM TT1) AS T, TT2 AS U");
If Q = Undefined Then Return; EndIf;
R = Q.ExecuteBatchWithIntermediateData(); R[2];""")
    batch = parsed["batches"][0]
    assert {d["name"] for d in batch["queries"][2]["dependencies"]} == {"TT1", "TT2"}
    assert batch["results"][0]["query_index"] == 2


def test_algorithm_diamond_is_linear() -> None:
    depth = 16
    algorithms = []
    for level in range(depth):
        for name in ("Л", "П"):
            algorithms.append(f'''<Алгоритм Имя="{name}{level}"><Текст>
Выполнить(Алгоритмы.Л{level + 1}); Выполнить(Алгоритмы.П{level + 1});</Текст></Алгоритм>''')
    algorithms.append(
        f"""<Алгоритм Имя="Л{depth}"><Текст>
ВыгрузитьПоПравилу(Объект,,,, "Цель");</Текст></Алгоритм>"""
    )
    xml = f"""<ПравилаОбмена><ВерсияФормата>2.01</ВерсияФормата>
<Алгоритмы>{"".join(algorithms)}</Алгоритмы>
<ПравилаВыгрузкиДанных><Правило><Код>В</Код><ПередВыгрузкойОбъекта>Выполнить(Алгоритмы.Л0);</ПередВыгрузкойОбъекта></Правило></ПравилаВыгрузкиДанных></ПравилаОбмена>"""
    start = perf_counter()
    edges = call_graph(load_exchange_rules(xml.encode()))
    elapsed = perf_counter() - start
    assert elapsed < 1.0, elapsed
    assert len(edges) == 1
    assert len(edges[0].chain) <= 4 * depth + 2


def test_search_case_and_length_and_trace_not_found(service: Kd2Service) -> None:
    pid = project_id(service)
    assert service.handlers_search(pid, "алгоритмы")["total"] > 0
    with pytest.raises(Kd2Error):
        service.handlers_search(pid, "x" * 257, "regex")
    assert service.rules_trace(pid, "Несуществующий")["status"] == "not_found"


@pytest.mark.parametrize("disabled", [False, True])
def test_n1_property_type_lookup_uses_last_pko(
    service: Kd2Service, tmp_path: Path, disabled: bool
) -> None:
    source = tmp_path / "typed.xml"
    source.write_text(
        f'''<ПравилаОбмена><ВерсияФормата>2.01</ВерсияФормата>
<ПравилаКонвертацииОбъектов>
<Правило><Код>Владелец</Код><Источник>ДокументСсылка.Пример</Источник><Свойства>
<Свойство><Код>1</Код><Источник Имя="Ссылка" Тип="СправочникСсылка.Другой"/></Свойство>
<Свойство Отключить="true"><Код>2</Код>
<Источник Имя="Отключен" Тип="СправочникСсылка.Другой"/></Свойство>
<Свойство><Код>3</Код><Источник Имя="Явный" Тип="СправочникСсылка.Другой"/>
<КодПравилаКонвертации>Первый</КодПравилаКонвертации></Свойство>
</Свойства></Правило>
<Правило><Код>Первый</Код><Источник>СправочникСсылка.Другой</Источник></Правило>
<Правило Отключить="{str(disabled).lower()}"><Код>Последний</Код>
<Источник>СправочникСсылка.Другой</Источник></Правило>
</ПравилаКонвертацииОбъектов></ПравилаОбмена>''',
        encoding="utf-8",
    )
    pid = service.rules_open(str(source))["project_id"]
    rules = service.workspace.get(pid).document
    assert isinstance(rules, ExchangeRules)
    graph = build_call_graph(rules)
    lookups = [edge for edge in graph.edges if edge.kind == "type_lookup"]
    assert [(edge.address, edge.target, edge.whole) for edge in lookups] == [
        ("ПКО «Владелец» / ПКС Ссылка", "Последний", False)
    ]
    assert "Последний" not in unreachable_pko_codes(rules, graph)
    trace = service.rules_trace(pid, "Владелец", depth=1)
    assert any(
        c["target"] == "Последний" and c["kind"] == "type_lookup" for c in trace["calls"]["items"]
    )
    assert any(c["kind"] == "type_lookup" for c in trace["cascades"]["items"])
    if not disabled:
        row = next(p for p in trace["pko"]["items"] if p["code"] == "Последний")
        assert row["reference_only"]
        assert any("ПКС Ссылка" in issue["message"] for issue in row["checks"])


def test_n2_result_bindings_merge_after_if() -> None:
    batches = parse_queries("""А = Новый Запрос("ВЫБРАТЬ 1; ВЫБРАТЬ 2");
Б = Новый Запрос("ВЫБРАТЬ 3; ВЫБРАТЬ 4");
Если У Тогда Р = А.ВыполнитьПакет();
Иначе Р = Б.ВыполнитьПакет(); КонецЕсли; Х = Р[1];""")["batches"]
    assert len(batches) == 2
    assert all(b["complete"] and b["results"][0]["query_index"] == 1 for b in batches)


@pytest.mark.parametrize(
    ("start", "middle", "end"),
    [("Попытка", "Исключение", "КонецПопытки"), ("Try", "Except", "EndTry")],
)
def test_n3_try_except_texts_and_results(start: str, middle: str, end: str) -> None:
    batches = parse_queries(f"""Запрос = Новый Запрос;
{start}
Запрос.Текст = "ВЫБРАТЬ 1; ВЫБРАТЬ 2"; Р = Запрос.ВыполнитьПакет();
{middle}
Запрос.Текст = "ВЫБРАТЬ 3"; Р = Запрос.ВыполнитьПакет();
{end}; Запрос.УстановитьПараметр("А", 1); Х = Р[0];""")["batches"]
    assert len(batches) == 2
    for batch in batches:
        assert batch["reason"] == "branching_text"
        assert batch["executions"] and batch["results"]
        assert batch["parameters"][0]["name"] == "А"


@pytest.mark.parametrize("directive", ["Область", "Region"])
def test_n3_region_assignment_and_single_text_in_if(directive: str) -> None:
    batch = parse_queries(f"""#{directive} Пример
Если У Тогда
Запрос = Новый Запрос("ВЫБРАТЬ 1; ВЫБРАТЬ 2");
Р = Запрос.ВыполнитьПакет(); Х = Р[1]; КонецЕсли;""")["batches"][0]
    assert batch["complete"]
    assert batch["executions"][0]["query_indices"] == [0, 1]
    assert batch["results"][0]["query_index"] == 1


@pytest.mark.parametrize("prefix", ["Попытка А = 1; Исключение", "#Область Пример"])
def test_n3_statement_boundaries_in_call_graph(prefix: str) -> None:
    rules = review_rules()
    prop = rules.pko()[0].child("Свойства")
    assert prop is not None
    prop.items[0].values["ПередВыгрузкой"] = f"{prefix}\nВыгрузитьОбъект = Истина;"
    rules.pvd()[0].values["ПередОбработкойПравила"] = f"""{prefix}
ИмяПКО = ?(У, "Док", "Вызванный"); ИмяПКО = Вычисленный;"""
    graph = build_call_graph(rules)
    assert any(edge.target == "Спр" and edge.whole for edge in graph.edges)
    assert {e.target for e in graph.edges if e.kind == "pko_assignment"} == {"Док", "Вызванный"}
    assert any(
        e.kind == "dynamic_call" and e.chain[-1]["target_expression"] == "Вычисленный"
        for e in graph.edges
    )


@pytest.mark.parametrize("reference", [False, True])
def test_explicit_property_call_exports_whole_by_default(reference: bool) -> None:
    rules = review_rules()
    props = rules.pko()[0].child("Свойства")
    assert props is not None
    seventh = ",, Истина" if reference else ""
    props.items[0].values["ПередВыгрузкой"] = (
        f'ВыгрузитьПоПравилу(Объект,,,, "Вызванный"{seventh});'
    )
    edge = next(
        e
        for e in build_call_graph(rules).edges
        if e.kind == "call" and e.target == "Вызванный" and "/ ПКС " in e.address
    )
    assert edge.whole is not reference


def test_trace_shared_algorithm_has_only_current_callers(
    service: Kd2Service, tmp_path: Path
) -> None:
    source = tmp_path / "dag.xml"
    source.write_text(
        """<ПравилаОбмена><ВерсияФормата>2.01</ВерсияФормата>
<ПравилаКонвертацииОбъектов>
<Правило><Код>П1</Код><Источник>СправочникСсылка.А</Источник>
<ПослеВыгрузки>Выполнить(Алгоритмы.Общий);</ПослеВыгрузки></Правило>
<Правило><Код>П2</Код><Источник>СправочникСсылка.Б</Источник>
<ПослеВыгрузки>Выполнить(Алгоритмы.Промежуточный);</ПослеВыгрузки></Правило>
<Правило><Код>К</Код><Источник>СправочникСсылка.К</Источник></Правило>
</ПравилаКонвертацииОбъектов><Алгоритмы>
<Алгоритм Имя="Промежуточный"><Текст>Выполнить(Алгоритмы.Общий);</Текст></Алгоритм>
<Алгоритм Имя="Общий"><Текст>ВыгрузитьПоПравилу(Объект,,,, "К");</Текст></Алгоритм>
</Алгоритмы></ПравилаОбмена>""",
        encoding="utf-8",
    )
    pid = service.rules_open(str(source))["project_id"]
    for code, other in (("П1", "П2"), ("П2", "П1")):
        calls = service.rules_trace(pid, code, depth=2)["calls"]["items"]
        edge = next(call for call in calls if call["target"] == "К")
        addresses = {site["address"] for site in edge["chain"]}
        assert f"ПКО «{code}»" in addresses
        assert f"ПКО «{other}»" not in addresses
        assert ("алгоритм «Промежуточный»" in addresses) is (code == "П2")
