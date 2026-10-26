"""Сценарии live_registration_check: код разбирается, ответ сводится явно."""

import json
import re
from pathlib import Path

import pytest

from kd_rules_mcp.errors import LiveParseError, LiveStageError
from kd_rules_mcp.live.codegen.registration import (
    lookup_code,
    registration_code,
    validate_registration_args,
)
from kd_rules_mcp.live.registration import parse_registration, summarize_registration
from kd_rules_mcp.live.response import literal
from kd_rules_mcp.service import Kd2Service, Settings

REF = "11111111-1111-1111-1111-111111111111"
HOSTILE = 'Имя"|;\r\nСимвол(10)//'
ROLLBACK = "Пока ТранзакцияАктивна() Цикл ОтменитьТранзакцию(); КонецЦикла;"
VERSION_HINT = "учтена версия объекта в базе (ПроверятьСсылку)"
SUBSCRIPTION_HINT = "подписка или модуль объекта"
DIRECT_HINT = "зарегистрировано напрямую (подписка или модуль объекта)"
NOT_MODIFIED_HINT = "объект не изменён для плана или запись отклонена"
OUTSIDE = "план вне обмена БСП"
AUTO = "авторегистрация: регистрирует платформа"


class _Missing:
    """Отличие «параметр не передан» от явного None."""


_MISSING = _Missing()


def check_registration_code(code: str) -> None:
    """Подставная база читает текст, а не только подставляет ответ."""
    assert "\r" not in code and "\n" not in code
    assert "//" not in code
    assert code.startswith("Попытка ") and code.endswith("КонецПопытки;")
    assert "ЗаписатьJSON(" in code
    assert "ЗафиксироватьТранзакцию" not in code
    # Формат(0, "ЧГ=0") в 1С — пустая строка: без ЧН=0 путь был "" вместо "0" (живой прогон).
    assert not re.search(r'Формат\([^()]*, "(?![^"]*ЧН=0)[^"]*Ч[ГДН][^"]*"\)', code)
    cache = code.index("ОбменДаннымиСлужебный.ОбновитьКэшМеханизмаРегистрацииОбъектов()")
    assert cache < code.index("ОпределитьПолучателей(")
    assert ".Изменения КАК" in code and "Ссылка = &Ссылка" in code
    if "НачатьТранзакцию()" in code:
        assert code.index("НачатьТранзакцию()") < code.index(".Записать(")
        assert "РежимЗаписиДокумента.Запись" in code
        loops = [match.start() for match in re.finditer(re.escape(ROLLBACK), code)]
        assert len(loops) >= 2
        assert code.count("КонецПопытки; " + ROLLBACK) == 1
        assert code.index("ОбновитьПовторноИспользуемыеЗначения()", loops[0]) > loops[0]
    else:
        assert "НачатьТранзакцию" not in code and ".Записать(" not in code


def check_lookup_code(code: str) -> None:
    """Код поиска — одна строка и только запрос. Дата дня видна по конструктору Дата(…235959)."""
    assert "\r" not in code and "\n" not in code
    assert "//" not in code
    assert code.startswith("Попытка ")
    assert "ЗаписатьJSON(" in code
    assert "ПЕРВЫЕ 21" in code
    assert "МО.СтандартныеРеквизиты" in code
    assert "ПривестиЗначение(" in code
    assert "ПолучитьОбъект" not in code
    assert ".Записать(" not in code
    assert "НачатьТранзакцию" not in code
    # Привилегированно, как основной код: иначе RLS отказал бы поиску там, где проверка прошла бы.
    assert code.index("УстановитьПривилегированныйРежим(Истина)") < code.index("ЗапросПоиска")
    assert "УстановитьПривилегированныйРежим(Ложь)" not in code
    # Значение кандидата — без пробелов фиксированной строки и без разделителей групп числа.
    assert "СокрП(ВыборкаПоиска.Значение)" in code
    assert "XMLСтрока(ВыборкаПоиска.Значение)" in code
    assert "Строка(ВыборкаПоиска.Значение)" not in code.replace("XMLСтрока(ВыборкаПоиска", "")
    # Обратное приведение до запроса: длинный код не обрезается молча до чужого.
    check = code.index("Если Не Совпало Тогда ВызватьИсключение")
    assert code.index("СокрП(Значение) = Исходное") < check
    assert code.index("XMLСтрока(Значение) = Исходное") < check
    assert check < code.index("ЗапросПоиска.Выполнить()")
    if re.search(r'Дата\(\("\d{8}235959"\)\)', code):
        assert "МЕЖДУ &Начало И &Конец" in code


def _is_lookup_code(code: str) -> bool:
    return "ПЕРВЫЕ 21" in code and "ПривестиЗначение(" in code


class RegistrationBase:
    def __init__(
        self,
        payload: dict | list[dict] | None = None,
        error: str = "",
        *,
        lookup: dict | None = None,
        lookup_error: str = "",
        expect_value: str | None = None,
    ):
        # Список — ответы по порядку вызовов; lookup — отдельный ответ на код поиска.
        if isinstance(payload, list):
            self.queue: list[dict] | None = payload
            self.payload: dict = {}
        else:
            self.queue = None
            self.payload = payload or {}
        self.error = error
        self.lookup = lookup
        self.lookup_error = lookup_error
        self.expect_value = expect_value
        self.codes: list[str] = []

    def execute(self, code: str) -> str:
        self.codes.append(code)
        if _is_lookup_code(code):
            check_lookup_code(code)
            if self.expect_value is not None:
                assert f"Исходное = {literal(self.expect_value)};" in code
            if self.lookup_error:
                return "ОШИБКА " + self.lookup_error
            body = self._next_body(self.lookup)
        else:
            check_registration_code(code)
            body = None if self.error else self._next_body(self.payload)
        if self.error and not _is_lookup_code(code):
            reply = "ОШИБКА " + self.error
        else:
            reply = "OK " + json.dumps(body, ensure_ascii=False)
        assert reply.startswith("OK ") or reply.startswith("ОШИБКА ")
        if reply.startswith("OK "):
            assert isinstance(json.loads(reply[3:]), dict)
        return reply

    def _next_body(self, fallback: dict | None) -> dict:
        if self.queue is not None:
            return self.queue.pop(0)
        return {} if fallback is None else fallback


@pytest.fixture
def service(tmp_path: Path):
    folder = tmp_path / "source"
    folder.mkdir()
    (folder / ".dev.env").write_text("IB_USER=login\nIB_PASSWORD=pwd\n", encoding="utf-8")
    (folder / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"data": {"url": "http://source/base?token=secret"}}}),
        encoding="utf-8",
    )
    catalog = tmp_path / "projects.yaml"
    catalog.write_text(
        "projects:\n"
        "  source:\n"
        "    mcp_config: .mcp.json\n"
        "    configurations: {full: {dump: main}}\n"
        "    bases:\n"
        "      sandbox:\n"
        "        role: песочница\n"
        "        configuration: full\n"
        "        connection: 'File=\"source\";'\n"
        "        data_mcp: data\n"
        "        dev_env: .dev.env\n",
        encoding="utf-8",
    )
    return Kd2Service(
        Settings(
            cache_dir=tmp_path / "cache",
            workspace=tmp_path / "workspace",
            projects_file=catalog,
            project_dirs={"source": folder},
        )
    )


def node(code: str, *, deleted: bool = False) -> dict:
    return {"code": code, "name": code, "deleted": deleted}


def reg(plan: str, code: str, message: int | None) -> dict:
    return {"plan": plan, "code": code, "name": code, "message": message}


def obj_leaf(
    path: str,
    result: bool | None,
    *,
    dropped: bool = False,
    kind: str = "Значение",
    algorithm: str = "",
    error: str | None = None,
) -> dict:
    return {
        "path": path,
        "group": False,
        "and": True,
        "property": "Наименование",
        "comparison": "Равно",
        "kind": kind,
        "expected": "1",
        "actual": "1",
        "result": result,
        "dropped": dropped,
        "algorithm": algorithm,
        "error": error,
    }


def obj_group(path: str, *, conjunction: bool) -> dict:
    return {
        "path": path,
        "group": True,
        "and": conjunction,
        "property": "",
        "comparison": "",
        "kind": "",
        "expected": "",
        "actual": "",
        "result": None,
        "dropped": False,
        "algorithm": "",
        "error": None,
    }


def node_leaf(
    path: str,
    nodes: list[str] | None,
    *,
    error: str | None = None,
    op: str = "И",
    comparison: str = "Равно",
    query_operator: str = "=",
) -> dict:
    return {
        "path": path,
        "group": False,
        "op": op,
        "attribute": "Код",
        "table": "",
        "comparison": comparison,
        "query_operator": query_operator,
        "operand": "1",
        "nodes": nodes,
        "error": error,
    }


def node_group(path: str, op: str) -> dict:
    return {
        "path": path,
        "group": True,
        "op": op,
        "attribute": "",
        "table": "",
        "comparison": "",
        "query_operator": "",
        "operand": "",
        "nodes": None,
        "error": None,
    }


def rule(
    *,
    object_filter: list[dict],
    node_filter: list[dict],
    bsp: list[str] | None,
    condition: list[str] | None,
    modes: list[dict] | None = None,
    handlers: list[str] | None = None,
    manager: str = "",
    error: str | None = None,
    flag: str = "",
    if_needed: list[str] | _Missing | None = _MISSING,
) -> dict:
    stored_if_needed: list[str] | None = [] if isinstance(if_needed, _Missing) else if_needed
    return {
        "flag_attribute": flag,
        "handlers": handlers or [],
        "manager": manager,
        "object_filter": object_filter,
        "node_filter": node_filter,
        "nodes_by_bsp": bsp,
        "nodes_by_condition": condition,
        "modes": modes or [],
        "if_needed": stored_if_needed,
        "error": error,
    }


def plan(
    name: str,
    nodes: list[dict],
    *,
    bsp: bool = True,
    auto: bool = False,
    rules_count: int = 1,
    recipients: list[dict] | None = None,
    error: str | None = None,
    detail: list[dict] | None = None,
) -> dict:
    return {
        "plan": name,
        "bsp": bsp,
        "autoregistration": auto,
        "rules": rules_count,
        "nodes": nodes,
        "recipients": recipients,
        "recipients_error": error,
        "detail": detail,
    }


def raw(
    plans: list[dict],
    *,
    new: bool = False,
    before: list[dict] | None = None,
    changes: dict | None = None,
    write: dict | None = None,
    after: list[dict] | None = None,
    without: list[str] | None = None,
) -> dict:
    return {
        "object": {
            "name": "Справочник.Контрагенты",
            "ref": REF,
            "presentation": "Представление",
            "new": new,
            "modified": False,
        },
        "changes": {} if changes is None else changes,
        "before": [] if before is None else before,
        "plans_without_nodes": [] if without is None else without,
        "plans": plans,
        "write": write,
        "after": after,
        "cache": {"mode": "refreshed", "checked": False},
    }


def matching(codes: tuple[str, ...] = ("N1",), name: str = "План") -> dict:
    nodes = [node(code) for code in codes]
    recipients = [{"code": item["code"], "name": item["name"]} for item in nodes]
    detail = [
        rule(
            object_filter=[],
            node_filter=[],
            bsp=[item["code"] for item in nodes],
            condition=[item["code"] for item in nodes],
        )
    ]
    return plan(name, nodes, recipients=recipients, detail=detail)


def folded(object_filter: list[dict], node_filter: list[dict] | None = None, **rule_kw) -> dict:
    nodes = [node("N1"), node("N2")]
    # Пустой отбор по узлу сводится ко всем узлам плана: значения по умолчанию с ним совпадают.
    codes = ["N1", "N2"]
    bsp = rule_kw.pop("bsp", codes)
    condition = rule_kw.pop("condition", codes)
    recipients = rule_kw.pop("recipients", [{"code": code, "name": code} for code in codes])
    item = plan(
        "План",
        nodes,
        recipients=recipients,
        detail=[
            rule(
                object_filter=object_filter,
                node_filter=[] if node_filter is None else node_filter,
                bsp=bsp,
                condition=condition,
                **rule_kw,
            )
        ],
    )
    summary = summarize_registration(parse_registration(raw([item]), wrote=False))
    return summary["plans"][0]


def invoke(service, payload: dict, **kwargs):
    base = RegistrationBase(payload, lookup=kwargs.pop("lookup", None))
    service.live_access_factory = lambda access: base
    result = service.live_registration_check("source.sandbox", **_registration_kwargs(kwargs))
    return result, base


def _registration_kwargs(kwargs: dict) -> dict:
    """Позиции number/date/code не должны забирать plan, changes и write."""
    params = {
        "object": kwargs.pop("object", "Справочник.Контрагенты"),
        "plan": kwargs.pop("plan", None),
        "changes": kwargs.pop("changes", None),
        "write": kwargs.pop("write", False),
    }
    if "ref" in kwargs:
        params["ref"] = kwargs.pop("ref")
    elif "number" not in kwargs and "code" not in kwargs:
        params["ref"] = REF
    params.update(kwargs)
    return params


def refuse(service, **kwargs):
    called = []

    def factory(access):
        called.append(access)
        raise AssertionError("адаптер вызван")

    service.live_access_factory = factory
    with pytest.raises(ValueError) as caught:
        service.live_registration_check("source.sandbox", **_registration_kwargs(kwargs))
    assert called == []
    return str(caught.value)


def test_scenario_только_чтение_при_write_false(service) -> None:
    payload = raw([matching()], before=[reg("План", "N1", 4)], without=["Пустой"])
    result, base = invoke(service, payload)
    code = base.codes[0]
    assert ".Записать(" not in code
    assert "НачатьТранзакцию" not in code
    assert "ЗарегистрироватьИзменения" not in code
    assert "УдалитьРегистрациюИзменений" not in code
    plan_row = result["plans"][0]
    assert plan_row["registered"] == [{"code": "N1", "name": "N1", "message": 4}]
    assert plan_row["recipients"] == [{"code": "N1", "name": "N1"}]
    assert result["plans_without_nodes"] == ["Пустой"]
    assert result["write"] is None
    saved = Path(result["run_path"]) / "registration.json"
    text = saved.read_bytes()
    assert text.endswith(b"\n") and b"\r" not in text
    assert "Представление" in text.decode("utf-8")
    assert json.loads(text)["before"][0]["message"] == 4


def test_scenario_запись_по_умолчанию(service) -> None:
    payload = raw([matching()], before=[reg("План", "N1", 4)])
    payload["write"] = {"error": None, "recipients": [], "after_write": []}
    payload["after"] = [reg("План", "N1", 4)]
    base = RegistrationBase(payload)
    service.live_access_factory = lambda access: base
    result = service.live_registration_check("source.sandbox", "Справочник.Контрагенты", REF)
    code = base.codes[0]
    assert "НачатьТранзакцию()" in code and ".Записать(" in code
    assert result["write"] == {"error": None, "rollback_verified": True}
    assert [stage["stage"] for stage in result["stages"]] == ["РЕГИСТРАЦИЯ", "ОТКАТ"]


def test_scenario_регистр_отказ_до_сети(service) -> None:
    for name in ("РегистрСведений.Курсы", "Константа.Валюта"):
        assert refuse(service, object=name) == "поддерживаются только ссылочные объекты"


def test_scenario_объекта_нет(service) -> None:
    base = RegistrationBase(error="объект не найден")
    service.live_access_factory = lambda access: base
    with pytest.raises(LiveStageError) as caught:
        service.live_registration_check("source.sandbox", "Справочник.Контрагенты", REF)
    code = base.codes[0]
    assert code.index("объект не найден") < code.index("ОпределитьПолучателей(")
    # Запись по умолчанию включена, но исключение «объект не найден» стоит раньше транзакции.
    assert code.index("объект не найден") < code.index("НачатьТранзакцию()")
    assert caught.value.details["stages"][0]["code"] == "live_code"
    assert "объект не найден" in caught.value.details["stages"][0]["message"]
    assert len(caught.value.details["stages"]) == 1


def test_scenario_запись_показана_и_откачена(service) -> None:
    before = [reg("План", "N1", 3)]
    after = [{"plan": "План", "code": "N1", "name": "Другое", "message": 3}]
    payload = raw(
        [matching()],
        before=before,
        write={
            "error": None,
            "recipients": [{"plan": "План", "code": "N1", "name": "N1"}],
            "after_write": [reg("План", "N1", 8)],
        },
        after=after,
    )
    result, base = invoke(service, payload, object="Документ.Поступление", write=True)
    code = base.codes[0]
    assert "ЗафиксироватьТранзакцию" not in code
    assert code.count(ROLLBACK) >= 2
    assert result["plans"][0]["written"] == [{"code": "N1", "name": "N1"}]
    assert result["write"] == {"error": None, "rollback_verified": True}
    assert result["stages"][1]["stage"] == "ОТКАТ"


def test_scenario_регистрация_после_отката_не_совпала(service) -> None:
    before = [reg("План", "N1", 1)]
    after = [reg("План", "N1", 2)]
    payload = raw(
        [matching()],
        before=before,
        write={"error": None, "recipients": [], "after_write": []},
        after=after,
    )
    base = RegistrationBase(payload)
    service.live_access_factory = lambda access: base
    with pytest.raises(LiveStageError) as caught:
        service.live_registration_check(
            "source.sandbox",
            "Справочник.Контрагенты",
            REF,
            write=True,
        )
    stage = next(item for item in caught.value.details["stages"] if item["stage"] == "ОТКАТ")
    assert stage["code"] == "live_stage"
    assert stage["before"] == before
    assert stage["after"] == after
    assert stage["message"] == "регистрация после отката не совпала"


def test_scenario_запись_отказала(service) -> None:
    payload = raw(
        [matching()],
        before=[reg("План", "N1", None)],
        write={"error": "блокировка", "recipients": [], "after_write": []},
        after=[reg("План", "N1", None)],
    )
    result, _base = invoke(service, payload, write=True)
    assert result["status"] == "OK"
    assert result["write"] == {"error": "блокировка", "rollback_verified": True}
    assert result["plans"][0]["written"] is None
    assert result["plans"][0]["recipients"] == [{"code": "N1", "name": "N1"}]


def test_scenario_после_установки_правил(service) -> None:
    _result, base = invoke(service, raw([matching()]))
    code = base.codes[0]
    assert code.index(
        "ОбменДаннымиСлужебный.ОбновитьКэшМеханизмаРегистрацииОбъектов()"
    ) < code.index("ОпределитьПолучателей(")


def test_scenario_план_вне_бсп(service) -> None:
    item = plan("Чужой", [node("N1")], bsp=False, rules_count=0, detail=None)
    payload = raw(
        [item],
        before=[reg("Чужой", "N1", 2)],
        write={
            "error": None,
            "recipients": [{"plan": "Чужой", "code": "N1", "name": "N1"}],
            "after_write": [],
        },
        after=[reg("Чужой", "N1", 2)],
    )
    result, _base = invoke(service, payload, write=True)
    row = result["plans"][0]
    assert row["recipients"] is None
    assert row["recipients_skipped"] == OUTSIDE
    assert row["registered"] == [{"code": "N1", "name": "N1", "message": 2}]
    assert row["written"] == [{"code": "N1", "name": "N1"}]
    assert row["explanation"] is None


def test_autoregistration_skips_recipients(service) -> None:
    item = plan("План", [node("N1")], auto=True, rules_count=2, detail=None)
    result, _base = invoke(service, raw([item]))
    row = result["plans"][0]
    assert row["recipients"] is None
    assert row["recipients_skipped"] == AUTO
    assert row["no_rules"] is False
    assert row["explanation"] is None


def test_scenario_нет_про_все_узлы(service) -> None:
    nodes = [node("N1"), node("N2")]
    item = plan(
        "План",
        nodes,
        rules_count=0,
        recipients=[{"code": "N1", "name": "N1"}, {"code": "N2", "name": "N2"}],
        detail=[],
    )
    result, _base = invoke(service, raw([item]))
    row = result["plans"][0]
    assert row["no_rules"] is True
    assert row["recipients"] == [{"code": "N1", "name": "N1"}, {"code": "N2", "name": "N2"}]
    assert row["explanation"] is None


def test_scenario_запись_добавила_узел(service) -> None:
    item = plan(
        "План",
        [node("N1"), node("N2")],
        recipients=[{"code": "N1", "name": "N1"}],
        detail=[
            rule(
                object_filter=[],
                node_filter=[node_leaf("0", ["N1"])],
                bsp=["N1"],
                condition=["N1"],
            )
        ],
    )
    payload = raw(
        [item],
        new=False,
        write={
            "error": None,
            "recipients": [
                {"plan": "План", "code": "N1", "name": "N1"},
                {"plan": "План", "code": "N2", "name": "N2"},
            ],
            "after_write": [],
        },
        after=[],
    )
    result, _base = invoke(service, payload, write=True)
    assert result["plans"][0]["only_written"] == [
        {"code": "N2", "name": "N2", "hint": VERSION_HINT}
    ]


def test_only_written_new_object_hint() -> None:
    item = plan(
        "План",
        [node("N1"), node("N2")],
        recipients=[{"code": "N1", "name": "N1"}],
        detail=[
            rule(
                object_filter=[],
                node_filter=[node_leaf("0", ["N1"])],
                bsp=["N1"],
                condition=["N1"],
            )
        ],
    )
    payload = raw(
        [item],
        new=True,
        write={
            "error": None,
            "recipients": [{"plan": "План", "code": "N2", "name": "N2"}],
            "after_write": [],
        },
        after=[],
    )
    row = summarize_registration(parse_registration(payload, wrote=True))["plans"][0]
    assert row["only_written"] == [{"code": "N2", "name": "N2", "hint": SUBSCRIPTION_HINT}]


def test_only_rules() -> None:
    item = plan(
        "План",
        [node("N1"), node("N2")],
        recipients=[{"code": "N1", "name": "N1"}],
        detail=[
            rule(
                object_filter=[],
                node_filter=[node_leaf("0", ["N1"])],
                bsp=["N1"],
                condition=["N1"],
            )
        ],
    )
    payload = raw(
        [item],
        write={
            "error": None,
            "recipients": [{"plan": "План", "code": "N2", "name": "N2"}],
            "after_write": [],
        },
        after=[],
    )
    row = summarize_registration(parse_registration(payload, wrote=True))["plans"][0]
    assert row["only_rules"] == [{"code": "N1", "name": "N1", "hint": NOT_MODIFIED_HINT}]
    assert row["only_written"] == [{"code": "N2", "name": "N2", "hint": VERSION_HINT}]


def test_scenario_правка_меняет_получателей(service) -> None:
    payload = raw([matching()], changes={"Сумма": "10"})
    result, base = invoke(service, payload, changes={"Сумма": True})
    code = base.codes[0]
    assert 'XMLЗначение(Тип(("Булево")), ("true"))' in code
    assert 'XMLЗначение(Тип(("Число")), ("1"))' not in code
    assert code.index("Сумма") < code.index("ОпределитьПолучателей(")
    assert result["changes"] == {"Сумма": "10"}
    assert result["plans"][0]["recipients"] == [{"code": "N1", "name": "N1"}]


def test_scenario_нет_реквизита(service) -> None:
    base = RegistrationBase(error="нет реквизита: Комментарий")
    service.live_access_factory = lambda access: base
    with pytest.raises(LiveStageError) as caught:
        service.live_registration_check(
            "source.sandbox",
            "Документ.Поступление",
            REF,
            changes={"Комментарий": "текст"},
            write=True,
        )
    code = base.codes[0]
    missing = code.index("нет реквизита")
    assert missing < code.index("ОпределитьПолучателей(")
    assert missing < code.index("НачатьТранзакцию()")
    assert caught.value.details["stages"][0]["code"] == "live_code"
    assert "Комментарий" in caught.value.details["stages"][0]["message"]


def test_scenario_узел_отсечён_условием() -> None:
    row = folded(
        [],
        [node_leaf("0", ["N1"])],
        bsp=["N1"],
        condition=["N1"],
        recipients=[{"code": "N1", "name": "N1"}],
    )
    explanation = row["explanation"]
    assert explanation["status"] == "ok"
    assert explanation["reason"] is None
    condition = explanation["rules"][0]["node_filter"]
    assert condition["consistent"] is True
    assert condition["conditions"][0]["nodes"] == ["N1"]
    assert "N2" not in condition["conditions"][0]["nodes"]
    second = explanation["rules"][0]["nodes"][1]
    assert second["code"] == "N2"
    assert second["reason"] == "узел не прошёл условия"
    assert second["failed"] == ["0"]
    assert second["deleted"] is False
    assert second["by_rule"] is False
    assert explanation["rules"][0]["nodes"][0]["by_rule"] is True
    assert row["recipients"] == [{"code": "N1", "name": "N1"}]


def test_scenario_объяснение_разошлось_с_бсп() -> None:
    row = folded(
        [],
        [node_leaf("0", ["N1"])],
        bsp=["N1", "N2"],
        condition=["N1"],
        recipients=[{"code": "N1", "name": "N1"}],
    )
    assert row["explanation"]["status"] == "mismatch"
    assert row["explanation"]["reason"] == "свод условий узла не совпал с расчётом БСП"
    assert row["explanation"]["rules"][0]["node_filter"]["consistent"] is False
    assert row["recipients"] == [{"code": "N1", "name": "N1"}]


def test_scenario_алгоритм_исполнен_как_в_бсп() -> None:
    row = folded([obj_leaf("0", True, kind="АлгоритмЗначения", algorithm="Значение = 1;")])
    explanation = row["explanation"]
    condition = explanation["rules"][0]["object_filter"]["conditions"][0]
    assert condition["result"] is True and condition["algorithm"] == "Значение = 1;"
    assert condition["error"] is None
    assert explanation["rules"][0]["object_filter"]["result"] is True
    assert explanation["status"] == "ok"


def test_algorithm_code_follows_bsp() -> None:
    code = registration_code("Справочник.Контрагенты", REF, None, None, False)
    copy = code.index("ПРО = Новый Структура;")
    run = code.index("Выполнить(ТекстАлгоритма);")
    traversal = code.index("ПРО.ОтборПоСвойствамОбъекта.Строки.Количество() - 1", run)
    # Копия правила (ОДСоб:2296–2327) → исполнение алгоритмов (2446–2505) → отбор по объекту.
    assert copy < run < traversal
    assert "ЗначениеКолонки.Скопировать()" in code
    assert "СтандартныеПодсистемыСервер.ПрофильБезопасностиДляВыполнитьВычислить()" in code
    assert code.index("ПрофильБезопасностиДляВыполнитьВычислить", copy) < run
    assert "Значение = Неопределено; Попытка Выполнить(ТекстАлгоритма);" in code
    assert "СтрокаОтбора.ЗначениеКонстанты = Значение;" in code
    # Привилегированный режим кода не снимается ради алгоритма.
    assert "УстановитьПривилегированныйРежим(Ложь)" not in code
    # Алгоритм исполняется только при чтении правил, до записи и вне транзакции.
    written = registration_code("Справочник.Контрагенты", REF, None, None, True)
    assert written.index("Выполнить(ТекстАлгоритма);") < written.index("НачатьТранзакцию()")


def test_scenario_алгоритм_не_исполнен() -> None:
    row = folded(
        [
            obj_leaf(
                "0",
                None,
                kind="АлгоритмЗначения",
                algorithm="ВызватьИсключение 1;",
                error="ошибка алгоритма",
            )
        ]
    )
    explanation = row["explanation"]
    assert explanation["rules"][0]["object_filter"]["conditions"][0]["result"] is None
    assert explanation["rules"][0]["object_filter"]["result"] is None
    assert explanation["status"] == "partial"
    assert explanation["reason"] == "итог отбора по объекту неизвестен и решает исход"


def test_refused_bad_uuid(service) -> None:
    assert refuse(service, ref="не-uuid") == "неверная ссылка"


def test_refused_bad_change_name(service) -> None:
    assert refuse(service, changes={"Имя|": "x"}) == "неверное имя реквизита: Имя|"


def test_refused_nan(service) -> None:
    assert (
        refuse(service, changes={"Сумма": float("nan")})
        == "число с экспонентой или не конечное не принимается"
    )


def test_refused_scientific_float(service) -> None:
    assert (
        refuse(service, changes={"Сумма": 1e-7})
        == "число с экспонентой или не конечное не принимается"
    )


def test_refused_type_without_value(service) -> None:
    assert refuse(service, changes={"Текст": {"type": "Строка"}}) == (
        "changes: нужна пара type и value"
    )


def test_change_reference_type_is_literal() -> None:
    changes: dict[str, str | int | float | bool | dict[str, str]] = {
        "Контрагент": {"type": "СправочникСсылка.Контрагенты", "value": "x"}
    }
    validate_registration_args("Справочник.Контрагенты", REF, None, changes, False)
    code = registration_code("Справочник.Контрагенты", REF, None, changes, False)
    assert literal("СправочникСсылка.Контрагенты") in code
    assert literal("x") in code


def test_parse_missing_field() -> None:
    payload = raw([matching()])
    del payload["after"]
    with pytest.raises(LiveParseError) as caught:
        parse_registration(payload, wrote=False)
    assert caught.value.code == "live_parse"


def test_parse_message_string() -> None:
    payload = raw([matching()], before=[reg("План", "N1", 1)])
    payload["before"][0]["message"] = "1"
    with pytest.raises(LiveParseError) as caught:
        parse_registration(payload, wrote=False)
    assert caught.value.code == "live_parse"
    assert "message" in str(caught.value)


def test_parse_result_string() -> None:
    item = matching()
    item["detail"][0]["object_filter"] = [obj_leaf("0", True)]
    item["detail"][0]["object_filter"][0]["result"] = "true"
    with pytest.raises(LiveParseError) as caught:
        parse_registration(raw([item]), wrote=False)
    assert caught.value.code == "live_parse"
    assert "result" in str(caught.value)


def test_parse_nodes_not_list() -> None:
    item = matching()
    item["detail"][0]["node_filter"] = [node_leaf("0", ["N1"])]
    item["detail"][0]["node_filter"][0]["nodes"] = "N1"
    with pytest.raises(LiveParseError) as caught:
        parse_registration(raw([item]), wrote=False)
    assert caught.value.code == "live_parse"
    assert "nodes" in str(caught.value)


def test_object_fold_and_false_wins_over_null() -> None:
    row = folded([obj_leaf("0", False), obj_leaf("1", None)])
    assert row["explanation"]["rules"][0]["object_filter"]["result"] is False


def test_object_fold_or_null_and_false_is_unknown() -> None:
    row = folded([obj_group("0", conjunction=False), obj_leaf("0.0", None), obj_leaf("0.1", False)])
    assert row["explanation"]["rules"][0]["object_filter"]["result"] is None


def test_object_fold_nested_groups() -> None:
    row = folded(
        [
            obj_group("0", conjunction=True),
            obj_group("0.0", conjunction=False),
            obj_leaf("0.0.0", True),
            obj_leaf("0.0.1", False),
            obj_leaf("0.1", False),
        ]
    )
    assert row["explanation"]["rules"][0]["object_filter"]["result"] is False


def test_object_fold_empty_groups() -> None:
    conjunction = folded([obj_group("0", conjunction=True)])
    disjunction = folded([obj_group("0", conjunction=False)])
    assert conjunction["explanation"]["rules"][0]["object_filter"]["result"] is True
    assert disjunction["explanation"]["rules"][0]["object_filter"]["result"] is False


def test_object_fold_dropped_is_excluded() -> None:
    row = folded([obj_leaf("0", True, dropped=True), obj_leaf("1", False)])
    only = folded([obj_leaf("0", False, dropped=True)])
    assert row["explanation"]["rules"][0]["object_filter"]["result"] is False
    assert only["explanation"]["rules"][0]["object_filter"]["result"] is True


def test_node_fold_self_check_ok() -> None:
    row = folded(
        [],
        [node_group("0", "И"), node_leaf("0.0", ["N1", "N2"]), node_leaf("0.1", ["N1"])],
        bsp=["N1"],
        condition=["N1"],
        recipients=[{"code": "N1", "name": "N1"}],
    )
    assert row["explanation"]["status"] == "ok"
    assert row["explanation"]["rules"][0]["node_filter"]["consistent"] is True


def test_node_leaf_error_unavailable() -> None:
    row = folded(
        [],
        [node_leaf("0", None, error="нет строки ГДЕ в тексте запроса")],
        bsp=None,
        condition=None,
        recipients=[{"code": "N1", "name": "N1"}],
    )
    explanation = row["explanation"]
    assert explanation["status"] == "unavailable"
    assert explanation["reason"] == "нет строки ГДЕ в тексте запроса"
    assert explanation["rules"][0]["node_filter"]["consistent"] is None
    assert row["recipients"] == [{"code": "N1", "name": "N1"}]


def test_handlers_mark_reference() -> None:
    row = folded([], handlers=["ПередОбработкой"], manager="")
    assert row["explanation"]["reference"] is True
    assert row["explanation"]["status"] == "ok"
    managed = folded([], manager="МенеджерРегистрации")
    assert managed["explanation"]["reference"] is True


def test_mode_always_explains_node_without_conditions() -> None:
    row = folded(
        [],
        [],
        bsp=["N1", "N2"],
        condition=[],
        modes=[{"code": "N1", "mode": "ВыгружатьВсегда"}],
        flag="РежимВыгрузки",
        recipients=[{"code": "N1", "name": "N1"}],
    )
    explained = row["explanation"]["rules"][0]["nodes"][0]
    assert explained["code"] == "N1"
    assert explained["reason"] == "режим ВыгружатьВсегда"
    assert explained["mode"] == "ВыгружатьВсегда"
    assert explained["failed"] == []
    assert row["explanation"]["status"] == "ok"


def test_lists_truncated_at_100_nodes() -> None:
    codes = [f"N{index}" for index in range(101)]
    nodes = [node(code) for code in codes]
    recipients = [{"code": code, "name": code} for code in codes]
    item = plan(
        "План",
        nodes,
        recipients=recipients,
        detail=[
            rule(
                object_filter=[],
                node_filter=[node_leaf("0", codes)],
                bsp=codes,
                condition=codes,
            )
        ],
    )
    payload = raw([item], before=[reg("План", code, 1) for code in codes])
    row = summarize_registration(parse_registration(payload, wrote=False))["plans"][0]
    assert row["nodes"] == 101
    assert len(row["registered"]) == 100
    assert len(row["recipients"]) == 100
    assert row["truncated"] is True
    rule_row = row["explanation"]["rules"][0]
    assert len(rule_row["nodes"]) == 100
    assert rule_row["truncated"] is True
    assert len(rule_row["node_filter"]["conditions"][0]["nodes"]) == 100
    assert rule_row["node_filter"]["conditions"][0]["truncated"] is True


def _summary(payload: dict, *, wrote: bool = False) -> dict:
    return summarize_registration(parse_registration(payload, wrote=wrote))


def _column_reads_are_guarded(code: str, name: str) -> None:
    guard = f'Колонки.Найти("{name}") <> Неопределено'
    access = f"СтрокаПравила.{name}"
    assert guard in code
    assert access in code
    start = 0
    while True:
        found = code.find(access, start)
        if found < 0:
            break
        guard_at = code.rfind(f'Колонки.Найти("{name}")', 0, found)
        assert guard_at != -1
        assert "КонецЕсли" not in code[guard_at:found]
        start = found + len(access)


def test_failed_leaves_skip_satisfied_or() -> None:
    # Проба 1 / run-2: лист 2.0 внутри выполненной «ИЛИ» исход не решает.
    payload = raw(
        [
            plan(
                "П",
                [node("Z")],
                recipients=[],
                detail=[
                    rule(
                        object_filter=[obj_leaf("0", True)],
                        node_filter=[
                            node_leaf("0", []),
                            node_leaf("1", ["Z"]),
                            node_group("2", "ИЛИ"),
                            node_leaf("2.0", [], op="ИЛИ"),
                            node_leaf("2.1", ["Z"], op="ИЛИ"),
                        ],
                        bsp=[],
                        condition=[],
                    )
                ],
            )
        ]
    )
    row = _summary(payload)["plans"][0]["explanation"]["rules"][0]["nodes"][0]
    assert row["failed"] == ["0"]
    assert row["reason"] == "узел не прошёл условия"


def test_failed_leaves_skip_nested_or() -> None:
    # Проба 9: группа 1 «ИЛИ» выполнена листом 1.1, виновник — лист 0.
    payload = raw(
        [
            plan(
                "П",
                [node("Z")],
                recipients=[],
                detail=[
                    rule(
                        object_filter=[],
                        node_filter=[
                            node_leaf("0", []),
                            node_group("1", "ИЛИ"),
                            node_group("1.0", "И"),
                            node_leaf("1.0.0", [], op="И"),
                            node_leaf("1.0.1", ["Z"], op="И"),
                            node_leaf("1.1", ["Z"], op="ИЛИ"),
                        ],
                        bsp=[],
                        condition=[],
                    )
                ],
            )
        ]
    )
    row = _summary(payload)["plans"][0]["explanation"]["rules"][0]["nodes"][0]
    assert row["failed"] == ["0"]


def test_deleted_node_is_not_a_failed_condition() -> None:
    cases = [
        rule(
            object_filter=[],
            node_filter=[node_leaf("0", ["A"]), node_leaf("1", ["A"])],
            bsp=["A"],
            condition=["A"],
        ),
        rule(object_filter=[], node_filter=[], bsp=["A"], condition=["A"]),
        rule(
            object_filter=[],
            node_filter=[node_leaf("0", ["A"])],
            bsp=["A"],
            condition=["A"],
            flag="Режим",
            modes=[{"code": "A", "mode": ""}, {"code": "D", "mode": "ВыгружатьВсегда"}],
        ),
    ]
    for detail in cases:
        payload = raw(
            [
                plan(
                    "П",
                    [node("A"), node("D", deleted=True)],
                    recipients=[{"code": "A", "name": "A"}],
                    detail=[detail],
                )
            ]
        )
        explanation = _summary(payload)["plans"][0]["explanation"]
        deleted = explanation["rules"][0]["nodes"][1]
        assert explanation["status"] == "ok"
        assert deleted["code"] == "D"
        assert deleted["deleted"] is True
        assert deleted["reason"] == "узел помечен на удаление"
        assert deleted["failed"] == []
        assert deleted["by_rule"] is False


def test_if_needed_without_export_explains_exclusion() -> None:
    payload = raw(
        [
            plan(
                "П",
                [node("A"), node("N")],
                recipients=[{"code": "A", "name": "A"}],
                detail=[
                    rule(
                        object_filter=[],
                        node_filter=[node_leaf("0", ["A", "N"])],
                        bsp=["A", "N"],
                        condition=["A"],
                        flag="Режим",
                        modes=[
                            {"code": "A", "mode": "ВыгружатьПоУсловию"},
                            {"code": "N", "mode": "ВыгружатьПриНеобходимости"},
                        ],
                        if_needed=[],
                    )
                ],
            )
        ]
    )
    explanation = _summary(payload)["plans"][0]["explanation"]
    node_n = explanation["rules"][0]["nodes"][1]
    assert explanation["status"] == "ok"
    assert node_n["reason"] == "режим ВыгружатьПриНеобходимости, объект в узел не выгружался"
    assert node_n["failed"] == []
    assert node_n["by_rule"] is False


def test_if_needed_does_not_explain_recipient_without_export() -> None:
    payload = raw(
        [
            plan(
                "П",
                [node("N")],
                recipients=[{"code": "N", "name": "N"}],
                detail=[
                    rule(
                        object_filter=[obj_leaf("0", False)],
                        node_filter=[node_leaf("0", ["N"])],
                        bsp=["N"],
                        condition=[],
                        flag="Режим",
                        modes=[{"code": "N", "mode": "ВыгружатьПриНеобходимости"}],
                        if_needed=[],
                    )
                ],
            )
        ]
    )
    explanation = _summary(payload)["plans"][0]["explanation"]
    assert explanation["status"] == "mismatch"
    assert explanation["reason"] == "узел без причины"
    node_n = explanation["rules"][0]["nodes"][0]
    assert node_n["recipient"] is True
    assert node_n["by_rule"] is False
    assert "объект выгружался" not in node_n["reason"]


def test_if_needed_unknown_is_partial() -> None:
    payload = raw(
        [
            plan(
                "П",
                [node("N")],
                recipients=[],
                detail=[
                    rule(
                        object_filter=[obj_leaf("0", None, kind="АлгоритмЗначения")],
                        node_filter=[node_leaf("0", ["N"])],
                        bsp=["N"],
                        condition=[],
                        flag="Режим",
                        modes=[{"code": "N", "mode": "ВыгружатьПриНеобходимости"}],
                        if_needed=None,
                    )
                ],
            )
        ]
    )
    explanation = _summary(payload)["plans"][0]["explanation"]
    node_n = explanation["rules"][0]["nodes"][0]
    assert explanation["status"] == "partial"
    assert explanation["reason"] == "неизвестно, выгружался ли объект в узел"
    assert node_n["reason"] == "неизвестно, выгружался ли объект в узел"
    assert node_n["by_rule"] is None
    assert node_n["failed"] == []


def test_if_needed_export_explains_inclusion() -> None:
    payload = raw(
        [
            plan(
                "П",
                [node("N")],
                recipients=[{"code": "N", "name": "N"}],
                detail=[
                    rule(
                        object_filter=[obj_leaf("0", False)],
                        node_filter=[node_leaf("0", [])],
                        bsp=[],
                        condition=[],
                        flag="Режим",
                        modes=[{"code": "N", "mode": "ВыгружатьПриНеобходимости"}],
                        if_needed=["N"],
                    )
                ],
            )
        ]
    )
    explanation = _summary(payload)["plans"][0]["explanation"]
    node_n = explanation["rules"][0]["nodes"][0]
    assert explanation["status"] == "ok"
    assert node_n["reason"] == "режим ВыгружатьПриНеобходимости, объект выгружался"
    assert node_n["by_rule"] is True


def test_direct_registration_is_only_written() -> None:
    payload = raw(
        [
            plan(
                "П",
                [node("A"), node("S")],
                recipients=[{"code": "A", "name": "A"}],
                detail=[
                    rule(
                        object_filter=[],
                        node_filter=[node_leaf("0", ["A"])],
                        bsp=["A"],
                        condition=["A"],
                    )
                ],
            )
        ],
        write={
            "error": None,
            "recipients": [{"plan": "П", "code": "A", "name": "A"}],
            "after_write": [reg("П", "A", None), reg("П", "S", None)],
        },
        after=[],
    )
    row = _summary(payload, wrote=True)["plans"][0]
    assert [item["code"] for item in row["written"]] == ["A", "S"]
    assert row["only_written"] == [{"code": "S", "name": "S", "hint": DIRECT_HINT}]


def test_changed_message_is_written_unchanged_pair_is_not() -> None:
    nodes = [node("S")]
    item = plan(
        "П",
        nodes,
        recipients=[],
        detail=[rule(object_filter=[], node_filter=[], bsp=[], condition=[])],
    )
    changed = raw(
        [item],
        before=[reg("П", "S", 5)],
        write={
            "error": None,
            "recipients": [],
            "after_write": [reg("П", "S", None)],
        },
        after=[reg("П", "S", 5)],
    )
    same = raw(
        [item],
        before=[reg("П", "S", None)],
        write={
            "error": None,
            "recipients": [],
            "after_write": [reg("П", "S", None)],
        },
        after=[reg("П", "S", None)],
    )
    assert _summary(changed, wrote=True)["plans"][0]["only_written"] == [
        {"code": "S", "name": "S", "hint": DIRECT_HINT}
    ]
    assert _summary(same, wrote=True)["plans"][0]["written"] == []


def test_recipient_outside_plan_nodes_is_mismatch() -> None:
    payload = raw(
        [
            plan(
                "П",
                [node("A")],
                recipients=[{"code": "A", "name": "A"}, {"code": "X", "name": "X"}],
                detail=[rule(object_filter=[], node_filter=[], bsp=["A"], condition=["A"])],
            )
        ]
    )
    explanation = _summary(payload)["plans"][0]["explanation"]
    assert explanation["status"] == "mismatch"
    assert explanation["reason"] == "получатель вне узлов плана"


def test_blank_node_codes_do_not_reject_other_plans() -> None:
    blank = plan(
        "П",
        [node(""), node("")],
        recipients=[],
        detail=[rule(object_filter=[], node_filter=[], bsp=[], condition=[])],
    )
    single = plan(
        "Один",
        [node("")],
        recipients=[],
        detail=[rule(object_filter=[], node_filter=[], bsp=[], condition=[])],
    )
    duplicate = plan(
        "Дубль",
        [node("A"), node("A")],
        recipients=[],
        detail=[rule(object_filter=[], node_filter=[], bsp=["A"], condition=["A"])],
    )
    summary = _summary(raw([blank, single, duplicate, matching()]))
    assert summary["plans"][0]["explanation"]["status"] == "unavailable"
    assert summary["plans"][0]["explanation"]["reason"] == "коды узлов плана не уникальны"
    assert summary["plans"][1]["explanation"]["reason"] == "коды узлов плана не уникальны"
    assert summary["plans"][2]["explanation"]["reason"] == "коды узлов плана не уникальны"
    assert summary["plans"][3]["explanation"]["status"] == "ok"


def test_group_operator_ignores_case() -> None:
    payload = raw(
        [
            plan(
                "П",
                [node("A")],
                recipients=[{"code": "A", "name": "A"}],
                detail=[
                    rule(
                        object_filter=[],
                        node_filter=[node_group("0", "или"), node_leaf("0.0", ["A"], op="или")],
                        bsp=["A"],
                        condition=["A"],
                    )
                ],
            )
        ]
    )
    parsed = parse_registration(payload, wrote=False)
    rows = parsed["plans"][0]["detail"][0]["node_filter"]
    assert rows[0]["op"] == "ИЛИ"
    assert rows[1]["op"] == "ИЛИ"
    assert _summary(payload)["plans"][0]["explanation"]["status"] == "ok"


def test_reference_prefix_and_names_ignore_case() -> None:
    validate_registration_args(
        "справочник.Контрагенты", REF, "обменЗарплата3Бухгалтерия3", None, False
    )
    code = registration_code(
        "справочник.Контрагенты",
        REF,
        "обменЗарплата3Бухгалтерия3",
        {"наименование": "x"},
        False,
    )
    assert literal("Справочник.Контрагенты") in code
    assert literal("справочник.Контрагенты") not in code
    assert "ВРег(ОписаниеРеквизита.Имя) = ВРег(" in code
    assert "ВРег(ПланМета.Имя) <> ВРег(ИмяПланаОтбор)" in code
    assert literal("наименование") in code
    assert literal("обменЗарплата3Бухгалтерия3") in code
    assert "ОписаниеРеквизита.Имя = " not in code


def test_object_outside_plans_is_rejected_before_changes_table() -> None:
    code = registration_code("Справочник.Контрагенты", REF, None, None, False)
    marker = 'ВызватьИсключение "объект не входит ни в один план обмена"'
    assert marker in code
    assert code.index(marker) < code.index(".Изменения КАК")
    assert "ОписанияПланов.Количество() = 0" in code
    assert "ПланыБезУзлов.Количество() = 0" in code


def test_read_after_write_is_not_a_write_failure() -> None:
    code = registration_code("Документ.Поступление", REF, None, None, True)
    done = code.index("ЗаписьВыполнена = Истина;")
    assert code.index("Объект.Записать();") < done
    assert done < code.index("ВыборкаРегистрации", done)
    assert 'ОшибкаЗаписи = "после записи: "' in code


def test_bsp_312_pro_columns_are_guarded() -> None:
    code = registration_code("Справочник.Контрагенты", REF, None, None, False)
    _column_reads_are_guarded(code, "ИмяМенеджераРегистрации")
    _column_reads_are_guarded(code, "ОтборПоСвойствамПланаОбмена")


def test_if_needed_query_is_read_only_and_skipped_for_new() -> None:
    code = registration_code("Справочник.Контрагенты", REF, None, None, False)
    assert "ОбменДаннымиСервер.ЭтоПланОбменаXDTO" in code
    assert "МассивУзловДляРегистрацииВыгружатьПриНеобходимости" in code
    assert "РегистрСведений.СоответствияОбъектовИнформационныхБаз" in code
    assert "УникальныйИдентификаторИсточника = &Объект" in code
    assert "ВыгружатьПриНеобходимости" in code
    assert "НЕ ШапкаПланаОбмена.ПометкаУдаления" in code
    assert "НЕ ШапкаПланаОбмена.ЭтотУзел" in code
    assert code.index("Не ПризнакНового") < code.index("СоответствияОбъектовИнформационныхБаз")
    assert "Не ПустаяСтрока(ИмяФлага)" in code


def test_query_operator_is_the_inverted_comparison() -> None:
    code = registration_code("Справочник.Контрагенты", REF, None, None, False)
    assert "comparison,query_operator,operand" in code
    row = folded([], [node_leaf("0", ["N1"], comparison="БольшеИлиРавно", query_operator=">=")])
    condition = row["explanation"]["rules"][0]["node_filter"]["conditions"][0]
    assert condition["comparison"] == "БольшеИлиРавно"
    assert condition["query_operator"] == ">="


def test_by_rule_is_not_the_plan_recipient() -> None:
    payload = raw(
        [
            plan(
                "П",
                [node("A")],
                rules_count=2,
                recipients=[{"code": "A", "name": "A"}],
                detail=[
                    rule(
                        object_filter=[obj_leaf("0", False)],
                        node_filter=[],
                        bsp=["A"],
                        condition=["A"],
                    ),
                    rule(
                        object_filter=[obj_leaf("0", True)],
                        node_filter=[],
                        bsp=["A"],
                        condition=["A"],
                    ),
                ],
            )
        ]
    )
    rules = _summary(payload)["plans"][0]["explanation"]["rules"]
    assert rules[0]["nodes"][0]["recipient"] is True
    assert rules[0]["nodes"][0]["by_rule"] is False
    assert rules[0]["nodes"][0]["reason"] == "отбор по объекту не выполнен"
    assert rules[1]["nodes"][0]["recipient"] is True
    assert rules[1]["nodes"][0]["by_rule"] is True
    assert rules[1]["nodes"][0]["reason"] == "условия выполнены"


def test_always_not_recipient_is_mismatch() -> None:
    payload = raw(
        [
            plan(
                "П",
                [node("A")],
                recipients=[],
                detail=[
                    rule(
                        object_filter=[obj_leaf("0", False)],
                        node_filter=[],
                        bsp=["A"],
                        condition=[],
                        flag="Режим",
                        modes=[{"code": "A", "mode": "ВыгружатьВсегда"}],
                    )
                ],
            )
        ]
    )
    explanation = _summary(payload)["plans"][0]["explanation"]
    assert explanation["status"] == "mismatch"
    assert explanation["reason"] == "узел без причины"


def test_hostile_values_stay_strings() -> None:
    code = registration_code(HOSTILE, HOSTILE, HOSTILE, {HOSTILE: HOSTILE}, False)
    assert code.count(literal(HOSTILE)) >= 5
    assert HOSTILE not in code
    assert "\n" not in code and "\r" not in code and "//" not in code
    assert code.startswith("Попытка ")


def test_cache_refreshed_path_in_code() -> None:
    code = registration_code("Справочник.Контрагенты", REF, None, None, False)
    refresh = code.index(
        'Выполнить("ОбменДаннымиСлужебный.ОбновитьКэшМеханизмаРегистрацииОбъектов();");'
    )
    assert refresh < code.index("ОпределитьПолучателей(")
    # Прямого вызова нет: отсутствие метода в старой БСП не должно ронять компиляцию всего кода.
    assert "; ОбменДаннымиСлужебный.ОбновитьКэшМеханизмаРегистрацииОбъектов();" not in code
    assert 'РежимКэша = "refreshed";' in code
    assert (
        'Данные.Вставить("cache", Новый Структура("mode,checked", РежимКэша, ПроверкаКэша));'
        in code
    )


def test_cache_fallback_path_in_code() -> None:
    code = registration_code("Справочник.Контрагенты", REF, None, None, False)
    handler = code.index("Исключение ОписаниеОшибкиКэша = ИнформацияОбОшибке().Описание;")
    fallback = code.index('РежимКэша = "fallback";', handler)
    # Запасной путь — только при «метод не обнаружен» с именем процедуры, иначе ошибка наружу.
    guard = code[handler:fallback]
    missing = "Метод объекта не обнаружен (ОбновитьКэшМеханизмаРегистрацииОбъектов)"
    assert f'ОписаниеОшибкиКэша = "{missing}"' in guard
    assert '"Object method not found (ОбновитьКэшМеханизмаРегистрацииОбъектов)"' in guard
    # Подробное представление содержит строку кода с именем процедуры — по нему сравнивать нельзя.
    assert "ПодробноеПредставлениеОшибки" not in guard
    assert "Тогда ВызватьИсключение; КонецЕсли;" in guard
    after = code[fallback:]
    assert after.index("ОбновитьПовторноИспользуемыеЗначения();") < after.index(
        'Выполнить("ОбменДаннымиСлужебный.ПроверитьКэшМеханизмаРегистрацииОбъектов();");'
    )
    assert '"Метод объекта не обнаружен (ПроверитьКэшМеханизмаРегистрацииОбъектов)"' in after
    assert after.index("ПроверкаКэша = Истина;") < after.index("ОпределитьПолучателей(")


def test_cache_fallback_reported_with_warning() -> None:
    payload = raw([matching()])
    payload["cache"] = {"mode": "fallback", "checked": True}
    summary = summarize_registration(parse_registration(payload, wrote=False))
    assert summary["cache"] == "fallback"
    assert "может опираться на старые правила" in summary["cache_warning"]
    payload["cache"] = {"mode": "fallback", "checked": False}
    summary = summarize_registration(parse_registration(payload, wrote=False))
    assert "правила регистрации сеанса могут быть старыми" in summary["cache_warning"]
    payload["cache"] = {"mode": "refreshed", "checked": False}
    summary = summarize_registration(parse_registration(payload, wrote=False))
    assert summary["cache"] == "refreshed" and "cache_warning" not in summary


@pytest.mark.parametrize(
    "cache",
    [{"mode": "unknown", "checked": False}, {"mode": "refreshed", "checked": True}, None],
)
def test_cache_bad_shape(cache) -> None:
    payload = raw([matching()])
    payload["cache"] = cache
    with pytest.raises(LiveParseError):
        parse_registration(payload, wrote=False)


def _candidate(ref: str, value: str, day: str | None, presentation: str = "Объект") -> dict:
    return {"ref": ref, "value": value, "date": day, "presentation": presentation}


def test_scenario_документ_по_номеру_и_дате(service) -> None:
    found = "22222222-2222-2222-2222-222222222222"
    payload = raw([matching()])
    payload["object"]["name"] = "Документ.Счет"
    payload["object"]["ref"] = found
    lookup = {
        "candidates": [_candidate(found, "0000-000123", "2026-10-01", "Счет")],
        "more": False,
    }
    result, base = invoke(
        service,
        payload,
        lookup=lookup,
        object="Документ.Счет",
        ref=None,
        number="0000-000123",
        date="2026-10-01",
    )
    search, main = base.codes
    assert "Т.Номер" in search
    assert "Т.Дата КАК Дата" in search
    assert "МЕЖДУ &Начало И &Конец" in search
    assert literal("0000-000123") in search
    assert literal("20261001000000") in search
    assert literal("20261001235959") in search
    assert "ПолучитьОбъект" not in search and "НачатьТранзакцию" not in search
    assert main == registration_code("Документ.Счет", found, None, None, False)
    assert result["lookup"] == {
        "by": "number",
        "value": "0000-000123",
        "date": "2026-10-01",
        "ref": found,
        "match": {"value": "0000-000123", "date": "2026-10-01", "presentation": "Счет"},
    }
    assert [stage["stage"] for stage in result["stages"]] == ["ПОИСК", "РЕГИСТРАЦИЯ"]
    saved = json.loads((Path(result["run_path"]) / "lookup.json").read_text(encoding="utf-8"))
    assert saved["candidates"][0]["ref"] == found


def test_scenario_справочник_по_коду(service) -> None:
    found = "22222222-2222-2222-2222-222222222222"
    payload = raw([matching()])
    payload["object"]["ref"] = found
    lookup = {
        "candidates": [_candidate(found, "000000001", None, "Контрагент")],
        "more": False,
    }
    result, base = invoke(
        service,
        payload,
        lookup=lookup,
        object="Справочник.Контрагенты",
        ref=None,
        code="000000001",
    )
    search, main = base.codes
    assert "Т.Код" in search
    assert "Т.Дата" not in search
    assert "МЕЖДУ" not in search
    assert literal("000000001") in search
    assert main == registration_code("Справочник.Контрагенты", found, None, None, False)
    assert result["lookup"] == {
        "by": "code",
        "value": "000000001",
        "ref": found,
        "match": {"value": "000000001", "date": None, "presentation": "Контрагент"},
    }


def test_code_found_in_other_case_shows_stored_value(service) -> None:
    # Ревью F1: запрос сравнивает строки без учёта регистра — в match код как в базе.
    found = "22222222-2222-2222-2222-222222222222"
    payload = raw([matching()])
    payload["object"]["ref"] = found
    lookup = {"candidates": [_candidate(found, "АБ-1", None, "Контрагент")], "more": False}
    base = RegistrationBase(payload, lookup=lookup, expect_value="аб-1")
    service.live_access_factory = lambda access: base
    result = service.live_registration_check(
        "source.sandbox", "Справочник.Контрагенты", code="аб-1", write=False
    )
    assert result["lookup"]["value"] == "аб-1"
    assert result["lookup"]["match"]["value"] == "АБ-1"


def test_lookup_platform_error_stops_before_main_code(service) -> None:
    # Ревью F7: отказ обратного приведения приходит ошибкой платформы стадии ПОИСК.
    message = "значение не подходит к типу реквизита Код: 00-000002"
    base = RegistrationBase(lookup_error=message, expect_value="00-0000021")
    service.live_access_factory = lambda access: base
    with pytest.raises(LiveStageError) as caught:
        service.live_registration_check(
            "source.sandbox", "Справочник.Контрагенты", code="00-0000021", write=False
        )
    stages = caught.value.details["stages"]
    assert [(stage["stage"], stage["code"]) for stage in stages] == [("ПОИСК", "live_code")]
    assert message in stages[0]["message"]
    assert len(base.codes) == 1


def test_scenario_несколько_объектов(service) -> None:
    rows = [
        _candidate("22222222-2222-2222-2222-222222222222", "1", "2026-10-01", "первый"),
        _candidate("33333333-3333-3333-3333-333333333333", "1", "2026-10-02", "второй"),
    ]
    base = RegistrationBase(lookup={"candidates": rows, "more": False})
    service.live_access_factory = lambda access: base
    with pytest.raises(LiveStageError) as caught:
        service.live_registration_check(
            "source.sandbox", "Документ.Счет", ref=None, number="1", write=False
        )
    assert len(base.codes) == 1
    assert "МЕЖДУ" not in base.codes[0]
    assert "Т.Дата КАК Дата" in base.codes[0]
    stage = caught.value.details["stages"][0]
    assert stage["stage"] == "ПОИСК" and stage["code"] == "live_stage"
    assert stage["message"] == "найдено объектов: 2; уточните номер датой или передайте ref"
    assert stage["candidates"] == rows and stage["more"] is False
    assert len(caught.value.details["stages"]) == 1


def test_несколько_по_коду_советуют_ref(service) -> None:
    # Живой прогон 26.10.2026: в песочнице два контрагента с одним кодом; дата коду не поможет.
    rows = [
        _candidate("22222222-2222-2222-2222-222222222222", "00-1", None, "первый"),
        _candidate("33333333-3333-3333-3333-333333333333", "00-1", None, "второй"),
    ]
    base = RegistrationBase(lookup={"candidates": rows, "more": False})
    service.live_access_factory = lambda access: base
    with pytest.raises(LiveStageError) as caught:
        service.live_registration_check(
            "source.sandbox", "Справочник.Контрагенты", code="00-1", write=False
        )
    stage = caught.value.details["stages"][0]
    assert stage["message"] == "найдено объектов: 2; передайте ref из candidates"


def test_scenario_объект_не_найден_по_номеру(service) -> None:
    base = RegistrationBase(lookup={"candidates": [], "more": False})
    service.live_access_factory = lambda access: base
    with pytest.raises(LiveStageError) as caught:
        service.live_registration_check(
            "source.sandbox", "Документ.Счет", ref=None, number="9", write=False
        )
    assert len(base.codes) == 1
    stage = caught.value.details["stages"][0]
    assert stage["stage"] == "ПОИСК" and stage["code"] == "live_stage"
    assert stage["message"] == "объект не найден"
    assert stage["lookup"] == {"by": "number", "value": "9"}
    saved = json.loads(
        (Path(caught.value.details["run_path"]) / "lookup.json").read_text(encoding="utf-8")
    )
    assert saved == {"candidates": [], "more": False}


def test_scenario_номер_у_справочника(service) -> None:
    assert "номер" in refuse(service, object="Справочник.Контрагенты", ref=None, number="1")


def test_scenario_код_у_документа(service) -> None:
    assert "код" in refuse(service, object="Документ.Счет", ref=None, code="1")


def test_scenario_ни_ссылки_ни_номера_ни_кода(service) -> None:
    assert "ссылка" in refuse(service, ref=None)


def test_scenario_два_способа(service) -> None:
    assert "один" in refuse(service, object="Документ.Счет", ref=REF, number="1")


def test_scenario_дата_без_номера(service) -> None:
    assert "дат" in refuse(service, ref=None, date="2026-10-01")


def test_scenario_неверная_дата(service) -> None:
    assert (
        refuse(service, object="Документ.Счет", ref=None, number="1", date="2026-02-30")
        == "неверная дата"
    )


def test_scenario_пустой_номер(service) -> None:
    assert refuse(service, object="Документ.Счет", ref=None, number="   ") == "пустой номер"
    assert refuse(service, object="Документ.Счет", ref=None, number="") == "пустой номер"


def test_scenario_номер_с_переводом_строки(service) -> None:
    assert "управляющ" in refuse(service, object="Документ.Счет", ref=None, number="1\n2")


def test_hostile_number_stays_literal() -> None:
    code = lookup_code("Документ.Счет", "Номер", HOSTILE, "2026-10-01")
    check_lookup_code(code)
    assert literal(HOSTILE) in code
    assert HOSTILE not in code
    assert "МЕЖДУ &Начало И &Конец" in code


def test_lookup_more_when_twenty_first_row(service) -> None:
    rows = [
        _candidate(f"00000000-0000-4000-8000-{index:012d}", "1", "2026-10-01", str(index))
        for index in range(1, 21)
    ]
    base = RegistrationBase(lookup={"candidates": rows, "more": True})
    service.live_access_factory = lambda access: base
    with pytest.raises(LiveStageError) as caught:
        service.live_registration_check(
            "source.sandbox", "Документ.Счет", ref=None, number="1", write=False
        )
    assert len(base.codes) == 1
    assert "ПЕРВЫЕ 21" in base.codes[0]
    assert "Кандидаты.Количество() = 20" in base.codes[0]
    stage = caught.value.details["stages"][0]
    assert stage["message"] == "найдено объектов: 20+; уточните номер датой или передайте ref"
    assert stage["more"] is True and stage["candidates"] == rows


def test_lookup_bad_payload_is_parse_error(service) -> None:
    base = RegistrationBase(
        lookup={
            "candidates": [{"ref": "не-uuid", "value": "1", "date": None, "presentation": "x"}],
            "more": False,
        }
    )
    service.live_access_factory = lambda access: base
    with pytest.raises(LiveStageError) as caught:
        service.live_registration_check(
            "source.sandbox", "Справочник.Контрагенты", ref=None, code="1", write=False
        )
    assert len(base.codes) == 1
    assert caught.value.details["stages"][0]["code"] == "live_parse"


def test_lookup_kinds_and_limits() -> None:
    for name in ("Документ.Счет", "БизнесПроцесс.Заявка", "Задача.ЗадачаИсполнителя"):
        validate_registration_args(name, None, None, None, False, number="1" * 50)
    for name in (
        "Справочник.Контрагенты",
        "ПланВидовХарактеристик.Виды",
        "ПланСчетов.Хозрасчетный",
        "ПланВидовРасчета.Начисления",
    ):
        validate_registration_args(name, None, None, None, False, code="A")
    with pytest.raises(ValueError, match="длиннее 50"):
        validate_registration_args("Документ.Счет", None, None, None, False, number="1" * 51)


def test_lookup_strips_edge_spaces(service) -> None:
    found = "44444444-4444-4444-4444-444444444444"
    lookup = {"candidates": [_candidate(found, "15", "2026-01-02")], "more": False}
    result, base = invoke(
        service,
        raw([matching()]),
        lookup=lookup,
        object="Документ.Счет",
        ref=None,
        number=" \t15\t ",
    )
    assert literal("15") in base.codes[0]
    assert result["lookup"]["value"] == "15"
