"""Входы независимого ревью p1–p6 и регрессии БСП; никаких живых вызовов."""

import base64
import copy
import io
import json
import random
from pathlib import Path

import pytest

from kd_rules_mcp.errors import LiveParseError, LiveProtocolError, LiveStageError
from kd_rules_mcp.live.access import DataMcpAccess
from kd_rules_mcp.live.codegen import exchange, plans, rules
from kd_rules_mcp.live.response import literal, one_line
from kd_rules_mcp.live.runs import uploaded
from tests.test_live_tools import exchange_args, ok
from tests.test_live_tools import setup as setup


def http_reply(text: str):
    return io.BytesIO(
        json.dumps(
            {"jsonrpc": "2.0", "id": 1, "result": {"content": [{"type": "text", "text": text}]}},
            ensure_ascii=False,
        ).encode()
    )


@pytest.mark.parametrize(
    "sep", ["\x00", "\x0b", "\x0c", "\x1c", "\x1d", "\x1e", "\x85", "\u2028", "\u2029"]
)
def test_p1_literal_preserves_control_separators(sep):
    value = f"a  {sep}  b"
    assert one_line(f"Х = {literal(value)};") == f'Х = ("a  " + Символ({ord(sep)}) + "  b");'
    query = f'ВЫБРАТЬ 1 ГДЕ "x  {sep}  y" = "x  {sep}  y"'
    assert f"Символ({ord(sep)})" in exchange.query_rows(query)


@pytest.mark.parametrize(
    "data",
    [
        {"count": 2, "columns": ["Код"], "rows": [["1"], ["Агент"]]},
        {"enabled": True, "nodes": [{"code": "1", "this": True}]},
        {"path": r"C:\Users\Агент\AppData\Local\Temp\v8_1.zip"},
        {"loaded": False, "rules": [], "messages": ["Пользователь Агент: ошибка в строке 1"]},
        {"plans": ["Authorization", "Basic"]},
    ],
)
def test_p2_success_values_are_unchanged(monkeypatch, data):
    text = "OK " + json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    monkeypatch.setattr(
        "kd_rules_mcp.live.access.open_data_request", lambda *a, **kw: http_reply(text)
    )
    access = DataMcpAccess(
        "http://host/mcp", {"Authorization": "Basic QWdlbnQ6MQ=="}, secrets=("Агент", "1")
    )
    assert access.execute("x") == text


def test_p6_legacy_base64_is_unchanged(monkeypatch):
    from kdbase.bsp_load import DataServer

    payload = random.Random(1).randbytes(300_000)
    text64 = base64.b64encode(payload).decode()
    monkeypatch.setattr(
        "kd_rules_mcp.live.access.open_data_request", lambda *a, **kw: http_reply("OK " + text64)
    )
    server = DataServer(
        "x.y", "http://host/mcp", {"Authorization": "Basic Zm9vOjE="}, secrets=("Агент", "1")
    )
    assert server.run("x") == text64
    assert base64.b64decode(server.run("x"), validate=True) == payload


@pytest.mark.parametrize("has_reply", [True, False])
def test_p5_sse_notification_before_response(monkeypatch, has_reply):
    note = {"jsonrpc": "2.0", "method": "notifications/message", "params": {"data": "x"}}
    wrong = {"jsonrpc": "2.0", "id": 99, "result": {"content": [{"text": "wrong"}]}}
    reply = {"jsonrpc": "2.0", "id": 1, "result": {"content": [{"text": "OK {}"}]}}
    raw = "".join(
        "event: message\ndata: " + json.dumps(item) + "\n\n"
        for item in [note, wrong, *([reply] if has_reply else [])]
    )
    monkeypatch.setattr(
        "kd_rules_mcp.live.access.open_data_request", lambda *a, **kw: io.BytesIO(raw.encode())
    )
    access = DataMcpAccess("http://host/mcp", {})
    if has_reply:
        assert access.execute("x") == "OK {}"
    else:
        with pytest.raises(LiveProtocolError):
            access.execute("x")


def test_compatibility_description_and_cache_reset_codegen():
    quiet = rules.load_file_code("План", "file", "rules.zip", write=False)
    assert "Описание = Неопределено;" in quiet
    assert "Описание.ТекстОшибки" in quiet
    assert "Сообщения.Добавить(Описание);" not in quiet
    assert "Отказ = Истина" in quiet
    written = rules.load_file_code("План", "file", "rules.zip", write=True)
    reset = "ОбменДаннымиСлужебный.СброситьКэшМеханизмаРегистрацииОбъектов()"
    assert written.index("З.Записать();") < written.index(reset)
    assert reset not in quiet


def test_compatibility_error_is_reported_without_write(setup):
    service, source, _, inputs = setup
    original = source.execute

    def execute(code):
        # Моделируется структура БСП с несериализуемой Картинкой: наружу только текст.
        if (
            "ЗагрузитьКомплектПравил" in code
            and "Описание = Неопределено;" in code
            and "Описание.ТекстОшибки" in code
        ):
            return ok(
                {
                    "loaded": False,
                    "rules": [
                        {"kind": kind, "loaded": False, "info": ""}
                        for kind in ("exchange", "correspondent", "registration")
                    ],
                    "messages": ["Правила предназначены для другой программы"],
                }
            )
        return original(code)

    source.execute = execute
    with pytest.raises(LiveStageError) as caught:
        service.live_rules_check("source.sandbox", "План", inputs)
    assert caught.value.details["stages"][0]["report"]["messages"] == [
        "Правила предназначены для другой программы"
    ]
    assert not any("З.Записать();" in c for c in source.codes)


def test_p3_rollback_deletes_previously_absent_registration(setup):
    service, source, _, inputs = setup
    del source.rules["registration"]
    preview = service.live_rules_install("source.sandbox", "План", inputs)
    result = service.live_rules_install(
        "source.sandbox", "План", inputs, "apply", preview["preview_hash"]
    )
    folder = Path(result["backup"]["folder"])
    assert (folder / "manifest.json").is_file()
    manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["records"]["registration"]["present"] is False
    assert result["backup"]["complete"] is True
    preview = service.live_rules_install("source.sandbox", "План", str(folder))
    service.live_rules_install(
        "source.sandbox", "План", str(folder), "apply", preview["preview_hash"]
    )
    assert "registration" not in source.rules


def test_p3_incomplete_snapshot_is_irreversible(setup):
    service, source, _, inputs = setup
    original = source.execute

    def execute(code):
        response = original(code)
        if (
            "З.Прочитать();" in code
            and "З.ПравилаXMLКорреспондента" not in code
            and ".ПравилаРегистрацииОбъектов;" not in code
        ):
            info = json.loads(response[3:])
            info["binary"] = False
            return ok(info)
        return response

    source.execute = execute
    preview = service.live_rules_install("source.sandbox", "План", inputs)
    assert preview["effects"][0]["irreversible"] is True
    assert preview["effects"][0]["reason"]


def test_p3_rollback_preserves_template_source_and_all_resources(setup):
    service, source, _, inputs = setup
    source.record_resources = {
        "exchange": {
            "ИсточникПравил": "МакетКонфигурации",
            "ИмяФайлаПравил": "old.zip",
            "ИмяМакетаПравил": "ПравилаОбмена",
            "ИмяМакетаПравилКорреспондента": "Корреспондент",
            "РежимОтладки": True,
            "РежимОтладкиВыгрузки": True,
            "РежимОтладкиЗагрузки": True,
            "ИмяФайлаПротоколаОбмена": "old-log",
            "НеОстанавливатьПоОшибке": True,
            "ПараметрыВыборочнойРегистрации": {"Параметр": "исходный"},
            "ИмяМенеджераРегистрации": "Менеджер",
            "ПрочийРесурс": "сохранён",
        }
    }
    before = copy.deepcopy(source.record_resources)
    preview = service.live_rules_install("source.sandbox", "План", inputs)
    result = service.live_rules_install(
        "source.sandbox", "План", inputs, "apply", preview["preview_hash"]
    )
    folder = Path(result["backup"]["folder"])
    assert (folder / "manifest.json").is_file()
    manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["records"]["exchange"]["resources"]["ИсточникПравил"] == '"МакетКонфигурации"'
    assert manifest["records"]["exchange"]["resources"]["РежимОтладки"] == "true"
    # После установки сведения базы изменены, откат обязан восстановить весь снимок.
    source.record_resources["exchange"] = {"ИсточникПравил": "Файл", "РежимОтладки": False}
    preview = service.live_rules_install("source.sandbox", "План", str(folder))
    service.live_rules_install(
        "source.sandbox", "План", str(folder), "apply", preview["preview_hash"]
    )
    assert source.record_resources["exchange"] == before["exchange"]


def test_p4_expect_rows_requires_query_before_access(setup):
    service, source, target, _ = setup
    with pytest.raises(ValueError, match="query"):
        service.live_exchange_run(**exchange_args(), expect_rows=5)
    assert source.codes == target.codes == []


def test_p4_load_messages_and_warning_are_stage_data(setup):
    service, _, target, _ = setup
    original = target.execute

    def execute(code):
        if "ВыполнитьОбменДаннымиДляУзла" in code:
            return ok(
                {
                    "messages": ["загружено", "предупреждение"],
                    "result": "ВыполненоСПредупреждениями",
                    "warnings": True,
                }
            )
        return original(code)

    target.execute = execute
    preview = service.live_exchange_run(**exchange_args())
    result = service.live_exchange_run(
        **exchange_args(), mode="apply", expected_preview_hash=preview["preview_hash"]
    )
    load = next(s for s in result["stages"] if s["stage"] == "ЗАГРУЗКА")
    assert result["status"] == "OK"
    assert load["data"] == {
        "messages": ["загружено", "предупреждение"],
        "result": "ВыполненоСПредупреждениями",
        "warnings": True,
    }


@pytest.mark.parametrize("flag", ["enabled", "ready"])
def test_exchange_unready_nodes_stop_before_rules(setup, flag):
    service, source, _, inputs = setup
    if flag == "enabled":
        source.nodes["enabled"] = False
    else:
        source.nodes["nodes"][1]["ready"] = False
    args = exchange_args() | {"source_rules": inputs}
    preview = service.live_exchange_run(**args)
    with pytest.raises(LiveStageError) as caught:
        service.live_exchange_run(
            **args, mode="apply", expected_preview_hash=preview["preview_hash"]
        )
    assert caught.value.details["stages"][0]["status"] == "ОШИБКА"
    assert not any(
        "ЗагрузитьКомплектПравил" in c or "ЗарегистрироватьИзменения" in c for c in source.codes
    )


def test_text_template_is_measured_via_file():
    code = plans.overview_code("План")
    assert "Макет.Размер()" not in code
    assert "Макет.Записать(Файл);" in code
    assert "Новый ДвоичныеДанные(Файл)" in code
    assert code.count("УдалитьФайлы(Файл)") == 2


@pytest.mark.parametrize("is_file,size", [(True, 0), (True, 3), (False, 3)])
def test_upload_lost_chunk_or_directory_never_reaches_load(is_file, size):
    class Access:
        def __init__(self):
            self.codes = []

        def execute(self, code):
            self.codes.append(code)
            if "РежимОткрытияФайла.Создать" in code:
                return ok({"path": "server-path"})
            if '"is_file,size,hash"' in code:
                return ok({"is_file": is_file, "size": size, "hash": "wrong"})
            return ok({})

    access = Access()
    with pytest.raises(LiveParseError), uploaded(access, b"abc"):
        pytest.fail("повреждённый файл нельзя загружать")
    deletion = access.codes[-1]
    assert "ЭтоФайл()" in deletion
    assert deletion.index("ЭтоФайл()") < deletion.index("УдалитьФайлы")


def test_message_upload_uses_xml_suffix(setup):
    service, _, target, _ = setup
    preview = service.live_exchange_run(**exchange_args())
    service.live_exchange_run(
        **exchange_args(), mode="apply", expected_preview_hash=preview["preview_hash"]
    )
    begin = next(c for c in target.codes if "РежимОткрытияФайла.Создать" in c)
    assert 'ПолучитьИмяВременногоФайла("xml")' in begin


def test_import_result_is_read_from_bsp_state():
    code = exchange.import_file("План", "SRC", "message.xml")
    assert "СостоянияОбменовДанными" in code
    assert "ВыполненоСПредупреждениями" in code
    assert '"messages,result,warnings"' in code


def test_p4_password_one_does_not_change_created_node_code(setup, monkeypatch):
    service, source, target, _ = setup
    for folder in service.settings.project_dirs.values():
        (folder / ".dev.env").write_text("IB_USER=Агент\nIB_PASSWORD=1", encoding="utf-8")
    source.nodes["nodes"][0]["code"] = "1"
    target.nodes["nodes"][1].update(code="OLD", ready=False)
    service.live_access_factory = lambda access: access

    def routed(request, timeout):
        code = json.loads(request.data)["params"]["arguments"]["bslcode"]
        db = source if "//source/" in request.full_url else target
        return http_reply(db.execute(code))

    monkeypatch.setattr("kd_rules_mcp.live.access.open_data_request", routed)
    preview = service.live_nodes_setup("source.sandbox", "target.sandbox", "План")
    service.live_nodes_setup(
        "source.sandbox",
        "target.sandbox",
        "План",
        mode="apply",
        expected_preview_hash=preview["preview_hash"],
    )
    code = next(c for c in target.codes if "СоздатьУзел" in c)
    assert 'НайтиПоКоду(("1"))' in code and 'О.Код = ("1")' in code


def test_cli_load_layer_error_keeps_main_query_protocol(tmp_path, monkeypatch):
    from contextlib import contextmanager

    from tests.test_exchange_check import _Scripted, ec

    source = _Scripted("a.src", ["OK SRC", "OK", "OK PG1lc3NhZ2UvPg=="])
    target = _Scripted("a.dst", ["OK DST", "OK 1\nН\nx"])
    args = ec.parse_args(
        [
            "run",
            "--source",
            "a.src",
            "--target",
            "a.dst",
            "--plan",
            "План",
            "--object",
            "Документ.Д",
            "--ref",
            "uuid",
            "--query",
            "ВЫБРАТЬ 1",
        ]
    )

    @contextmanager
    def broken_upload(*a, **kw):
        raise LiveProtocolError("ошибка передачи")
        yield  # pragma: no cover — форма контекстного менеджера

    monkeypatch.setattr(ec, "uploaded", broken_upload)
    monkeypatch.setattr(ec, "RUNS", tmp_path)
    lines = []
    assert ec.run(args, source, target, lines) is False
    assert "ЗАГРУЗКА ОШИБКА ошибка передачи" in lines
    assert lines[-3:] == ["ЗАПРОС 1 строк", "  Н", "  x"]


def test_cli_multiline_messages_and_cells_keep_first_line(tmp_path, monkeypatch):
    from tests.test_exchange_check import _Scripted, ec

    source = _Scripted("a.src", ["OK SRC", "OK", "OK PG1lc3NhZ2UvPg=="])
    target = _Scripted("a.dst", ["OK DST", "OK\nСООБЩЕНИЕ первая\nпродолжение", "OK 1\nН\nдо"])
    call = target.call

    def reply(code):
        if "ВыполнитьОбменДаннымиДляУзла" in code:
            return ok({"messages": ["первая\nпродолжение"]})
        if '"count,columns,rows"' in code:
            return ok({"count": 1, "columns": ["Н"], "rows": [["до\nпосле"]]})
        return call(code)

    object.__setattr__(target, "call", reply)
    args = ec.parse_args(
        [
            "run",
            "--source",
            "a.src",
            "--target",
            "a.dst",
            "--plan",
            "План",
            "--object",
            "Документ.Д",
            "--ref",
            "uuid",
            "--query",
            "ВЫБРАТЬ 1",
        ]
    )
    monkeypatch.setattr(ec, "RUNS", tmp_path)
    lines = []
    assert ec.run(args, source, target, lines) is True
    assert "СООБЩЕНИЕ первая" in lines and "  до" in lines
    assert "продолжение" not in "\n".join(lines) and "после" not in "\n".join(lines)


def test_cli_bsp_description_not_added_to_messages(tmp_path, capsys):
    from kdbase.bsp_load import DataServer
    from tests.test_kdbase import _rules_zip, bsp_check

    archive = _rules_zip(tmp_path / "rules.zip")

    class Base(DataServer):
        def call(self, code):
            if "РежимОткрытияФайла.Создать" in code:
                return ok({"path": "server-file"})
            if "ЗагрузитьКомплектПравил" in code:
                added = ["Описание совместимости"] if "Сообщения.Добавить(Описание" in code else []
                return ok(
                    {
                        "loaded": False,
                        "rules": [{"loaded": False, "info": ""}],
                        "messages": ["Сообщение БСП", *added],
                    }
                )
            if '"is_file,size,hash"' in code:
                import hashlib

                data = archive.read_bytes()
                return ok(
                    {
                        "is_file": True,
                        "size": len(data),
                        "hash": base64.b64encode(hashlib.sha256(data).digest()).decode(),
                    }
                )
            return ok({})

    assert bsp_check.check_data(archive, "План", Base("a.b", "", {})) == 1
    output = capsys.readouterr().out
    assert "СООБЩЕНИЕ Сообщение БСП\nИТОГ ОШИБКА\n" in output
    assert "Описание совместимости" not in output


def test_snapshot_unread_record_is_irreversible_and_apply_refuses(setup):
    service, source, _, inputs = setup
    original = source.execute

    def execute(code):
        if ".ПравилаРегистрацииОбъектов; З.Прочитать();" in code:
            return "ОШИБКА нет доступа к записи"
        return original(code)

    source.execute = execute
    preview = service.live_rules_install("source.sandbox", "План", inputs)
    assert preview["effects"][0]["irreversible"] is True
    with pytest.raises(LiveStageError) as caught:
        service.live_rules_install(
            "source.sandbox", "План", inputs, "apply", preview["preview_hash"]
        )
    assert any("не прочитано" in s.get("message", "") for s in caught.value.details["stages"])
    assert not any("З.Записать();" in c for c in source.codes)


def test_snapshot_absent_both_records_can_be_restored(setup):
    service, source, _, inputs = setup
    source.rules.clear()
    preview = service.live_rules_install("source.sandbox", "План", inputs)
    result = service.live_rules_install(
        "source.sandbox", "План", inputs, "apply", preview["preview_hash"]
    )
    assert result["backup"]["complete"] is True and result["backup"]["files"] == []
    folder = result["backup"]["folder"]
    preview = service.live_rules_install("source.sandbox", "План", folder)
    assert preview["effects"][0]["new"]["records"]["exchange"]["present"] is False
    service.live_rules_install("source.sandbox", "План", folder, "apply", preview["preview_hash"])
    assert source.rules == {}


def test_failed_import_keeps_user_messages_in_stage(setup):
    service, _, target, _ = setup
    original = target.execute

    def execute(code):
        if "ВыполнитьОбменДаннымиДляУзла" in code:
            return ok(
                {
                    "messages": ["ПКО: объект не загружен"],
                    "result": "Ошибка",
                    "warnings": False,
                    "error": "ОШИБКА отказ загрузки\n{стек}",
                }
            )
        return original(code)

    target.execute = execute
    preview = service.live_exchange_run(**exchange_args())
    with pytest.raises(LiveStageError) as caught:
        service.live_exchange_run(
            **exchange_args(), mode="apply", expected_preview_hash=preview["preview_hash"]
        )
    load = next(s for s in caught.value.details["stages"] if s["stage"] == "ЗАГРУЗКА")
    assert load["status"] == "ОШИБКА"
    assert load["data"]["messages"] == ["ПКО: объект не загружен"]
    assert load["data"]["result"] == "Ошибка"
    assert load["message"] == "отказ загрузки"


def test_cli_structured_import_error_keeps_main_query(tmp_path, monkeypatch):
    from tests.test_exchange_check import _Scripted, ec

    source = _Scripted("a.src", ["OK SRC", "OK", "OK PG1lc3NhZ2UvPg=="])
    target = _Scripted("a.dst", ["OK DST", "OK 1\nН\nx"])
    call = target.call

    def reply(code):
        if "ВыполнитьОбменДаннымиДляУзла" in code:
            return ok(
                {
                    "messages": ["не загружено"],
                    "result": "Ошибка",
                    "warnings": False,
                    "error": "ОШИБКА отказ загрузки\n{стек}",
                }
            )
        return call(code)

    object.__setattr__(target, "call", reply)
    args = ec.parse_args(
        [
            "run",
            "--source",
            "a.src",
            "--target",
            "a.dst",
            "--plan",
            "План",
            "--object",
            "Документ.Д",
            "--ref",
            "uuid",
            "--query",
            "ВЫБРАТЬ 1",
        ]
    )
    monkeypatch.setattr(ec, "RUNS", tmp_path)
    lines = []
    assert ec.run(args, source, target, lines) is False
    assert "ЗАГРУЗКА ОШИБКА a.dst: отказ загрузки" in lines
    assert lines[-3:] == ["ЗАПРОС 1 строк", "  Н", "  x"]
