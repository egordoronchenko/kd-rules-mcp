"""Паспорт базы: подставная платформа проверяет код и отдаёт текст OK JSON / ОШИБКА."""

import copy
import json
import re
from pathlib import Path

import pytest
import yaml

from kd_rules_mcp.errors import LiveBaseRefusedError, LiveStageError
from kd_rules_mcp.live.codegen.base import base_check_code
from kd_rules_mcp.live.response import literal
from kd_rules_mcp.projects import ProjectConfigError
from kd_rules_mcp.service import Kd2Service, Settings

DATA = Path(__file__).parent / "data" / "xmldump"


class Database:
    """Проверяет вызванные выражения, наличие изолированных разделов и ответ платформы."""

    def __init__(self):
        self.codes: list[str] = []
        self.error = False
        self.payload = {
            "connection": 'Srvr="server";Ref="bp_dev";',
            "configuration": {
                "name": "Тестовая",
                "synonym": "Тестовая конфигурация",
                "version": "1.0.0.1",
            },
            "platform": "8.3.27.1",
            "user": {"name": "Агент", "full_name": "Агент обмена"},
            "universal_xml": True,
            "bsp": "3.1.12",
            "extensions": [
                {"name": "Расширение", "version": "0.1.0.1", "active": True},
                {"name": "Отключённое", "version": "1", "active": False},
            ],
            "plans": [{"name": "План", "exists": True, "bsp": True, "xdto": False, "rib": False}],
        }

    def execute(self, code: str) -> str:
        assert not any(marker in code for marker in ("\r", "\n", "//"))
        # Формы, которых платформа не принимает (живая проба 25.10.2026): метода
        # Метаданные.РасширенияКонфигурации() нет, «Новый X().Свойство» не компилируется.
        assert "Метаданные.РасширенияКонфигурации" not in code
        assert not re.search(r"Новый [А-Яа-яЁё]+\(\)\.", code)
        assert not re.search(
            r"\b(?:Записать|Удалить|НачатьТранзакцию|УстановитьПривилегированныйРежим)\b", code
        )
        assert code.startswith("Попытка ") and "ЗаписатьJSON(Запись, Данные)" in code
        for expression in (
            "СтрокаСоединенияИнформационнойБазы()",
            "Метаданные.Имя",
            "Метаданные.Синоним",
            "Метаданные.Версия",
            "СведенияСистемы = Новый СистемнаяИнформация;",
            "СведенияСистемы.ВерсияПриложения",
            "СтандартныеПодсистемыСервер.ВерсияБиблиотеки()",
            'Новый Структура("name,full_name", ИмяПользователя(), ПолноеИмяПользователя())',
            'Метаданные.Обработки.Найти("УниверсальныйОбменДаннымиXML") <> Неопределено',
            "РасширенияКонфигурации.Получить()",
            "Р.Активно",
            "Метаданные.ПланыОбмена.Найти(Имя) <> Неопределено",
            "ОбменДаннымиПовтИсп.ПланыОбменаБСП().Найти(Имя) <> Неопределено",
            "ОбменДаннымиПовтИсп.ЭтоПланОбменаXDTO(Имя)",
            "ОбменДаннымиПовтИсп.ЭтоПланОбменаРаспределеннойИнформационнойБазы(Имя)",
        ):
            assert expression in code
        sections = ("connection", "configuration", "platform", "user", "universal_xml", "bsp")
        for section in (*sections, "extensions", "plans"):
            assert (
                f'Данные.Вставить("{section}_error", '
                "КраткоеПредставлениеОшибки(ИнформацияОбОшибке()))" in code
            )
        assert code.count("Попытка") >= 10
        all_plans = "Имена = ОбменДаннымиПовтИсп.ПланыОбменаБСП();" in code
        if all_plans:
            assert 'Данные.Вставить("all", Истина)' in code
            assert "ВерсииФорматаОбмена" not in code
        else:
            assert (
                'ОбменДаннымиСервер.ЗначениеНастройкиПланаОбмена(Имя, "ВерсииФорматаОбмена")'
                in code
            )
            assert 'ОбменДаннымиСервер.ЗначениеНастройкиПланаОбмена(Имя, "ФорматОбмена")' in code
            assert "Версии.Добавить(Строка(КЗ.Ключ))" in code
            for plan in self.payload["plans"]:
                assert f"Имена.Добавить({literal(plan['name'])});" in code
        self.codes.append(code)
        if self.error:
            return "ОШИБКА Не удалось прочитать базу"
        payload = copy.deepcopy(self.payload)
        if all_plans:
            payload["all"] = True
        return "OK " + json.dumps(payload, ensure_ascii=False)


@pytest.fixture
def setup(tmp_path: Path):
    folder = tmp_path / "bp"
    folder.mkdir()
    (folder / ".dev.env").write_text(
        "IB_USER=test-agent\nIB_PASSWORD=test-only\n", encoding="utf-8"
    )
    (folder / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"data": {"url": "http://unused/data"}}}), encoding="utf-8"
    )
    base = {
        "role": "песочница",
        "configuration": "full",
        "connection": 'Srvr="server";Ref="bp_dev";',
        "data_mcp": "data",
        "dev_env": ".dev.env",
    }
    projects = {
        "bp": {
            "name": "bp",
            "mcp_config": ".mcp.json",
            "configurations": {"full": {"dump": "main"}},
            "bases": {"dev": base, "prod": {**base, "role": "боевая"}},
        },
        "do": {"name": "do", "configurations": {"full": {"dump": "main"}}},
    }
    profile = {
        "id": "bp-do",
        "plan": "План",
        "technology": "kd2",
        "sides": [{"project": "bp", "base": "dev"}, {"project": "do"}],
    }
    path = tmp_path / "projects.yaml"
    path.write_text(
        yaml.safe_dump({"projects": projects, "exchanges": [profile]}, allow_unicode=True),
        encoding="utf-8",
    )
    service = Kd2Service(
        Settings(
            projects_file=path,
            project_dirs={"bp": folder},
            cache_dir=tmp_path / "cache",
            workspace=tmp_path / "ws",
        )
    )
    service.store.load_xml("bp-full", DATA / "main", [DATA / "ext"])
    database = Database()
    service.live_access_factory = lambda access: database
    return service, database


def check(result: dict, name: str) -> dict:
    return next(c for c in result["checks"] if c["check"] == name)


def rewrite(service: Kd2Service, mutate) -> None:
    path = service.settings.projects_file
    body = yaml.safe_load(path.read_text(encoding="utf-8"))
    mutate(body)
    path.write_text(yaml.safe_dump(body, allow_unicode=True), encoding="utf-8")


def test_same_base_fresh_structure(setup) -> None:
    service, database = setup
    result = service.live_base_check("bp.dev")
    assert result["status"] == "OK"
    assert result["base"] == "bp.dev"
    assert result["connection"] == {"srvr": "server", "ref": "bp_dev"}
    assert result["configuration"]["version"] == "1.0.0.1"
    assert result["bsp"] == "3.1.12" and result["platform"] == "8.3.27.1"
    assert result["user"] == {"name": "Агент", "full_name": "Агент обмена"}
    assert result["universal_xml"] is True
    assert all(c["status"] == "ok" for c in result["checks"])
    assert result["problems"] == result["warnings"] == 0
    assert result["stages"][0]["stage"] == "БАЗА"
    assert len(database.codes) == 1
    assert Path(result["protocol_path"]).is_file()


def test_other_ref_is_error_but_run_ok(setup) -> None:
    service, database = setup
    database.payload["connection"] = 'Srvr="server";Ref="other";'
    result = service.live_base_check("bp.dev")
    identity = check(result, "base.identity")
    assert identity["status"] == "error"
    assert "bp_dev" in identity["message"] and "other" in identity["message"]
    assert result["status"] == "OK" and result["problems"] == 1


@pytest.mark.parametrize(
    "field,value,fragments",
    [
        ("version", "2.0", ["1.0.0.1", "2.0"]),
        ("name", "Другая", ["Тестовая", "Другая"]),
    ],
)
def test_stale_structure(setup, field, value, fragments) -> None:
    service, database = setup
    database.payload["configuration"][field] = value
    result = service.live_base_check("bp.dev")
    stale = check(result, "base.structure")
    assert stale["status"] == "warning" and "устарела" in stale["message"]
    assert all(s in stale["message"] for s in fragments)
    assert result["warnings"] == 1


def test_extension_difference_is_warning(setup) -> None:
    service, database = setup
    database.payload["extensions"] = [{"name": "Другое", "version": "1", "active": True}]
    result = service.live_base_check("bp.dev")
    stale = check(result, "base.structure")
    assert stale["status"] == "warning"
    assert "расширение" in stale["message"] and "другое" in stale["message"]


def test_missing_structure_warns_and_proposes_load(setup) -> None:
    service, _ = setup
    service.store.path("bp-full").unlink()
    result = service.live_base_check("bp.dev")
    assert check(result, "base.structure")["status"] == "warning"
    assert result["steps"] == [
        {"tool": "structure_load_project", "arguments": {"project": "bp", "configuration": "full"}}
    ]


def test_wrong_technology_reports_versions(setup) -> None:
    service, database = setup
    database.payload["plans"][0].update(
        xdto=True, versions=["1.18", "1.19"], format="EnterpriseData"
    )
    result = service.live_base_check("bp.dev", exchange="bp-do")
    assert check(result, "base.technology")["status"] == "error"
    assert result["plans"][0]["technology"] == "ed"
    assert result["plans"][0]["versions"] == ["1.18", "1.19"]
    assert result["plans"][0]["format"] == "EnterpriseData"


def test_bsp_section_error_preserves_other_sections(setup) -> None:
    service, database = setup
    del database.payload["bsp"]
    database.payload["bsp_error"] = "Нет функции библиотеки"
    result = service.live_base_check("bp.dev")
    assert result["status"] == "OK"
    assert result["bsp"] is None and result["bsp_error"] == "Нет функции библиотеки"
    assert result["configuration"] == database.payload["configuration"]
    assert result["plans"][0]["exists"] is True


def test_code_error_fails_base_stage(setup) -> None:
    service, database = setup
    database.error = True
    with pytest.raises(LiveStageError) as caught:
        service.live_base_check("bp.dev")
    stage = caught.value.details["stages"][0]
    assert stage["stage"] == "БАЗА" and stage["code"] == "live_code"
    assert caught.value.details["status"] == "ОШИБКА"


def test_production_refused_before_adapter(setup) -> None:
    service, database = setup
    created = []
    service.live_access_factory = lambda access: created.append(access) or database
    with pytest.raises(LiveBaseRefusedError, match="не песочница"):
        service.live_base_check("bp.prod")
    assert created == [] and database.codes == []


def test_no_plan_or_profile_reads_all_bsp_plans(setup) -> None:
    service, database = setup
    rewrite(service, lambda data: data.update(exchanges=[]))
    result = service.live_base_check("bp.dev")
    assert "Имена = ОбменДаннымиПовтИсп.ПланыОбменаБСП();" in database.codes[0]
    # Все планы БСП — обзор: сверять план и технологию не с чем, технология видна в plans.
    assert not any(c["check"] in ("base.plan", "base.technology") for c in result["checks"])
    assert result["plans"][0]["technology"] == "kd2"


def test_unknown_exchange_refused_before_adapter(setup) -> None:
    service, database = setup
    created = []
    service.live_access_factory = lambda access: created.append(access) or database
    with pytest.raises(ProjectConfigError, match="bp-do"):
        service.live_base_check("bp.dev", exchange="absent")
    assert created == [] and database.codes == []


def test_connection_never_exposes_credentials(setup) -> None:
    service, database = setup
    database.payload["connection"] += 'Usr="private-user";Pwd="private-password";Other="hidden";'
    result = service.live_base_check("bp.dev")
    assert set(result["connection"]) == {"srvr", "ref"}
    serialized = json.dumps(result, ensure_ascii=False)
    assert all(
        s not in serialized for s in ("Usr", "Pwd", "private-user", "private-password", "hidden")
    )
    assert "private-password" not in Path(result["protocol_path"]).read_text(encoding="utf-8")


def test_plan_literal_escapes_quotes_newline_and_comment(setup) -> None:
    service, database = setup
    name = 'План";\n// строка'
    database.payload["plans"][0]["name"] = name
    result = service.live_base_check("bp.dev", plan=name)
    assert f"Имена.Добавить({literal(name)});" in database.codes[0]
    assert result["plans"][0]["name"] == name
    assert check(result, "base.technology")["status"] == "skipped"


@pytest.mark.parametrize("connection", ["srvr=SERVER; ref=BP_DEV;", 'SrVr="SERVER";ReF="BP_DEV";'])
def test_identity_is_case_insensitive(setup, connection) -> None:
    service, database = setup
    database.payload["connection"] = connection
    database.payload["configuration"]["name"] = "тестовая"
    database.payload["extensions"][0]["name"] = "РАСШИРЕНИЕ"
    result = service.live_base_check("bp.dev")
    assert check(result, "base.identity")["status"] == "ok"
    assert check(result, "base.structure")["status"] == "ok"


def test_file_identity_normalizes_separators_and_trailing_slash(setup) -> None:
    service, database = setup
    rewrite(
        service,
        lambda data: data["projects"]["bp"]["bases"]["dev"].update(
            connection='File="C:/Bases/BP/";'
        ),
    )
    database.payload["connection"] = 'file="c:\\bases\\bp";'
    result = service.live_base_check("bp.dev")
    assert check(result, "base.identity")["status"] == "ok"


def test_other_kind_of_base_is_error(setup) -> None:
    """Файловая база против серверной в настройке — другая база, а не «не с чем сверить»."""
    service, database = setup
    database.payload["connection"] = 'File="C:/Bases/BP";'
    result = service.live_base_check("bp.dev")
    assert check(result, "base.identity")["status"] == "error"
    assert result["problems"] == 1


def test_partial_identity_is_warning(setup) -> None:
    """В настройке только Ref, в базе ещё и Srvr — сверена часть адреса, не `ok`."""
    service, _ = setup
    rewrite(
        service,
        lambda data: data["projects"]["bp"]["bases"]["dev"].update(connection='Ref="bp_dev";'),
    )
    identity = check(service.live_base_check("bp.dev"), "base.identity")
    assert identity["status"] == "warning" and "ref" in identity["message"]


def test_exchange_without_this_base_refused_before_adapter(setup) -> None:
    service, database = setup
    rewrite(service, lambda data: data["exchanges"][0]["sides"][0].update(base="prod"))
    with pytest.raises(ProjectConfigError, match="не сторона обмена"):
        service.live_base_check("bp.dev", exchange="bp-do")
    assert database.codes == []


def test_rib_plan_is_not_kd2(setup) -> None:
    service, database = setup
    database.payload["plans"][0]["rib"] = True
    result = service.live_base_check("bp.dev", exchange="bp-do")
    assert result["plans"][0]["technology"] == "rib"
    assert check(result, "base.technology")["status"] == "error"


def test_unread_rib_flag_claims_no_technology(setup) -> None:
    service, database = setup
    del database.payload["plans"][0]["rib"]
    database.payload["plans"][0]["rib_error"] = "Метод не найден"
    result = service.live_base_check("bp.dev", exchange="bp-do")
    assert result["plans"][0]["technology"] is None
    assert result["plans"][0]["rib_error"] == "Метод не найден"
    assert check(result, "base.technology")["status"] == "error"


@pytest.mark.parametrize(
    "patch", [{"exists": False, "bsp": False}, {"bsp": False}, {"error": "Не прочитан"}]
)
def test_missing_or_unread_plan_is_error(setup, patch) -> None:
    service, database = setup
    database.payload["plans"][0].update(patch)
    result = service.live_base_check("bp.dev")
    assert check(result, "base.plan")["status"] == "error"
    assert result["status"] == "OK"


def test_codegen_all_plans_has_no_format_version_calls() -> None:
    code = base_check_code(None)
    assert "ВерсииФорматаОбмена" not in code and "ФорматОбмена" not in code


@pytest.mark.parametrize("section", ["user", "universal_xml"])
def test_user_and_universal_xml_section_errors_are_isolated(setup, section: str) -> None:
    """Ошибка раздела пользователя или обработки — в `<раздел>_error`, остальное прочитано."""
    service, database = setup
    del database.payload[section]
    database.payload[f"{section}_error"] = "Нет доступа"
    result = service.live_base_check("bp.dev")
    assert result["status"] == "OK"
    assert result[section] is None and result[f"{section}_error"] == "Нет доступа"
    assert result["bsp"] == "3.1.12" and result["plans"][0]["exists"] is True


def test_missing_universal_xml_is_false_not_error(setup) -> None:
    service, database = setup
    database.payload["universal_xml"] = False
    database.payload["user"] = {"name": "", "full_name": ""}
    result = service.live_base_check("bp.dev")
    assert result["universal_xml"] is False
    assert result["user"] == {"name": "", "full_name": ""}
    assert all(c["status"] == "ok" for c in result["checks"])


@pytest.mark.parametrize(
    "patch",
    [{"user": {"name": "Агент"}}, {"user": "Агент"}, {"universal_xml": "да"}],
)
def test_malformed_user_or_universal_xml_is_parse_error(setup, patch: dict) -> None:
    service, database = setup
    database.payload.update(patch)
    with pytest.raises(LiveStageError) as caught:
        service.live_base_check("bp.dev")
    assert caught.value.details["stages"][0]["code"] == "live_parse"
