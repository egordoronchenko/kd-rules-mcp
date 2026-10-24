"""Совместимость БСП: отказ по конфигурации и разрешённый человеком повтор по версии."""

import json
import sys
import zipfile

import pytest
from mcp.client import Client

from kd_rules_mcp.errors import LiveStageError, PreviewMismatchError
from kd_rules_mcp.live.codegen.rules import load_file_code
from kd_rules_mcp.server import create_server
from tests.test_live_tools import Database, exchange_args, ok
from tests.test_live_tools import setup as setup


class CompatibilityBase:
    """Сторона платформы проверяет код, возвращает только текст протокола."""

    def __init__(self, database, kind, *, retry_ok=True):
        self.database = database
        self.original = database.execute
        self.kind = kind
        self.retry_ok = retry_ok
        self.loads = []
        self.writes = 0

    def execute(self, code):
        if "ЗагрузитьКомплектПравил" not in code:
            return self.original(code)
        assert "\r" not in code and "\n" not in code and "//" not in code
        assert "Описание = Неопределено;" in code
        assert 'Новый Структура("kind,text"' in code
        assert "Описание.ВидОшибки" in code and "Описание.ТекстОшибки" in code
        retry = "Описание = Истина;" in code
        if retry:
            assert code.count("Рег.ЗагрузитьКомплектПравил(") == 2
            assert 'Совместимость.kind = "УстаревшиеПравила"' in code
            assert 'Совместимость.kind = "УстаревшаяВерсияКонфигурации"' in code
            assert "Если ЭтоНесовпадениеВерсии Тогда Отказ = Ложь;" in code
            assert code.index("Описание = Неопределено;") < code.index("Описание = Истина;")
        self.loads.append(code)
        loaded = (
            self.kind in ("УстаревшиеПравила", "УстаревшаяВерсияКонфигурации")
            and retry
            and self.retry_ok
        )
        if loaded:
            report = json.loads(self.original(code).removeprefix("OK "))
            if "З.Записать();" in code:
                assert code.index("Описание = Истина;") < code.index("Если Успех Тогда")
                assert code.index("Если Успех Тогда") < code.index("З.Записать();")
                self.writes += 1
        else:
            self.database.codes.append(code)
            report = {
                "loaded": False,
                "rules": [
                    {"kind": kind, "loaded": False, "info": ""}
                    for kind in ("exchange", "correspondent", "registration")
                ],
                "messages": [],
            }
        report["compatibility"] = {"kind": self.kind, "text": "Правила для другой версии"}
        return ok(report)


@pytest.mark.parametrize(
    ("kind", "accept", "success"),
    [
        ("НекорректнаяКонфигурация", True, False),
        ("НекорректнаяКонфигурация", False, False),
        ("УстаревшиеПравила", False, False),
        ("УстаревшаяВерсияКонфигурации", False, False),
        ("УстаревшиеПравила", True, True),
        ("УстаревшаяВерсияКонфигурации", True, True),
    ],
)
def test_check_compatibility(setup, kind, accept, success):
    service, source, _, files = setup
    base = CompatibilityBase(source, kind)
    source.execute = base.execute
    if success:
        result = service.live_rules_check(
            "source.sandbox", "План", files, accept_version_mismatch=accept
        )
        assert result["status"] == "OK"
        report = result["report"]
    else:
        with pytest.raises(LiveStageError) as caught:
            service.live_rules_check(
                "source.sandbox", "План", files, accept_version_mismatch=accept
            )
        stage = caught.value.details["stages"][0]
        report = stage["report"]
        if kind != "НекорректнаяКонфигурация":
            assert "accept_version_mismatch=true" in stage["message"]
    assert report["compatibility"] == {"kind": kind, "text": "Правила для другой версии"}
    assert report["loaded"] is success
    assert len(base.loads) == 1
    assert base.writes == 0
    assert not any("З.Записать();" in code for code in source.codes)


@pytest.mark.parametrize(
    ("kind", "accept", "retry_ok", "success"),
    [
        ("НекорректнаяКонфигурация", True, True, False),
        ("УстаревшиеПравила", False, True, False),
        ("УстаревшиеПравила", True, True, True),
        ("УстаревшиеПравила", True, False, False),
    ],
)
def test_install_writes_only_after_successful_retry(setup, kind, accept, retry_ok, success):
    service, source, _, files = setup
    base = CompatibilityBase(source, kind, retry_ok=retry_ok)
    source.execute = base.execute
    inputs = dict(base="source.sandbox", plan="План", files=files, accept_version_mismatch=accept)
    preview = service.live_rules_install(**inputs)
    if success:
        result = service.live_rules_install(
            **inputs, mode="apply", expected_preview_hash=preview["preview_hash"]
        )
        assert result["report"]["compatibility"] == {
            "kind": "УстаревшиеПравила",
            "text": "Правила для другой версии",
        }
        assert base.writes == 1
        assert len(base.loads) == 2
    else:
        with pytest.raises(LiveStageError) as caught:
            service.live_rules_install(
                **inputs, mode="apply", expected_preview_hash=preview["preview_hash"]
            )
        assert caught.value.details["stages"][-1]["status"] == "ПРОПУЩЕНА"
        assert base.writes == 0
        assert not any("З.Записать();" in code for code in source.codes)


@pytest.mark.parametrize("tool", ["install", "exchange"])
def test_accept_flag_binds_preview_hash(setup, tool):
    service, source, _, files = setup
    if tool == "install":
        call = service.live_rules_install
        inputs = dict(base="source.sandbox", plan="План", files=files)
    else:
        call = service.live_exchange_run
        inputs = {**exchange_args(), "source_rules": files}
    refused = call(**inputs)
    accepted = call(**inputs, accept_version_mismatch=True)
    assert refused["preview_hash"] != accepted["preview_hash"]
    with pytest.raises(PreviewMismatchError):
        call(**inputs, mode="apply", expected_preview_hash=accepted["preview_hash"])
    assert not any("З.Записать();" in code for code in source.codes)


@pytest.mark.parametrize("accept", [False, True])
@pytest.mark.parametrize("side", ["source", "target"])
def test_exchange_rule_stage_exposes_compatibility(setup, accept, side):
    service, source, target, files = setup
    database = source if side == "source" else target
    base = CompatibilityBase(database, "УстаревшиеПравила")
    database.execute = base.execute
    inputs = {**exchange_args(), f"{side}_rules": files, "accept_version_mismatch": accept}
    preview = service.live_exchange_run(**inputs)
    if accept:
        result = service.live_exchange_run(
            **inputs, mode="apply", expected_preview_hash=preview["preview_hash"]
        )
        stage = next(s for s in result["stages"] if s["stage"].startswith("ПРАВИЛА"))
        report = stage["data"]
        assert base.writes == 1
    else:
        with pytest.raises(LiveStageError) as caught:
            service.live_exchange_run(
                **inputs, mode="apply", expected_preview_hash=preview["preview_hash"]
            )
        stage = next(s for s in caught.value.details["stages"] if s["stage"].startswith("ПРАВИЛА"))
        report = stage["report"]
        assert base.writes == 0
        assert not any("ЗарегистрироватьИзменения" in code for code in source.codes)
    assert report["compatibility"] == {
        "kind": "УстаревшиеПравила",
        "text": "Правила для другой версии",
    }


def test_codegen_first_call_retry_and_serializable_warning():
    for write in (False, True):
        code = load_file_code(
            "План", "file", "rules.zip", write=write, accept_version_mismatch=True
        )
        assert "Описание = Неопределено;" in code
        assert "Описание = Истина;" in code
        assert code.count("Рег.ЗагрузитьКомплектПравил(") == 2
        assert 'Новый Структура("loaded,rules,messages,compatibility"' in code
        assert "Сообщения.Добавить(Описание)" not in code
    strict = load_file_code("План", "file", "rules.zip", write=False)
    assert "Описание = Истина;" not in strict
    assert "Совместимость = Неопределено;" in strict
    assert 'Совместимость.kind = "УстаревшиеПравила"' in strict


@pytest.mark.parametrize("full_set", [False, True])
def test_no_compatibility_warning_is_null(setup, full_set):
    service, _, _, files = setup
    if not full_set:
        del files["registration"]
    result = service.live_rules_check("source.sandbox", "План", files)
    assert result["report"]["compatibility"] is None
    code = load_file_code("План", "file", "rules.zip", write=False, full_set=full_set)
    assert "Совместимость = Неопределено;" in code
    assert 'Новый Структура("loaded,rules,messages,compatibility"' in code


@pytest.mark.anyio
@pytest.mark.parametrize("tool", ["live_rules_check", "live_rules_install", "live_exchange_run"])
async def test_mcp_accept_flag_forwarding(setup, tool):
    service, source, _, files = setup
    base = CompatibilityBase(source, "УстаревшиеПравила")
    source.execute = base.execute
    async with Client(create_server(service)) as client:
        inputs = (
            {**exchange_args(), "source_rules": files}
            if tool == "live_exchange_run"
            else {"base": "source.sandbox", "plan": "План", "files": files}
        )
        inputs["accept_version_mismatch"] = True
        result = await client.call_tool(tool, inputs)
        if tool != "live_rules_check":
            assert not result.is_error
            result = await client.call_tool(
                tool,
                {
                    **inputs,
                    "mode": "apply",
                    "expected_preview_hash": result.structured_content["preview_hash"],
                },
            )
        assert not result.is_error
        data = result.structured_content
        report = (
            next(s["data"] for s in data["stages"] if s["stage"] == "ПРАВИЛА ИСТОЧНИКА")
            if tool == "live_exchange_run"
            else data["report"]
        )
        assert report["compatibility"]["kind"] == "УстаревшиеПравила"


@pytest.mark.parametrize("accept", [False, True])
def test_bsp_cli_accept_flag(tmp_path, monkeypatch, capsys, accept):
    from tests.test_kdbase import bsp_check

    database = Database("SRC", "DST")
    base = CompatibilityBase(database, "УстаревшиеПравила")
    archive = tmp_path / "rules.zip"
    with zipfile.ZipFile(archive, "w") as package:
        for name in (
            "ExchangeRules.xml",
            "CorrespondentExchangeRules.xml",
            "RegistrationRules.xml",
        ):
            package.writestr(name, "<Правила/>")
    monkeypatch.setattr(bsp_check, "_bind", lambda *args: ("", "a.b"))
    monkeypatch.setattr(bsp_check, "server_for", lambda *args: base)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "bsp_check",
            "--archive",
            str(archive),
            "--plan",
            "План",
            *(["--accept-version-mismatch"] if accept else []),
        ],
    )
    with pytest.raises(SystemExit) as caught:
        bsp_check.main()
    assert caught.value.code == (0 if accept else 1)
    lines = capsys.readouterr().out.splitlines()
    assert lines[-2:] == [
        "СОВМЕСТИМОСТЬ УстаревшиеПравила: Правила для другой версии",
        "ИТОГ " + ("OK" if accept else "ОШИБКА"),
    ]


def test_bsp_cli_accept_flag_requires_data_transport(monkeypatch, capsys):
    from tests.test_kdbase import bsp_check

    monkeypatch.setattr(bsp_check, "_bind", lambda *args: ("", None))
    monkeypatch.setattr(sys, "argv", ["bsp_check", "--plan", "План", "--accept-version-mismatch"])
    with pytest.raises(SystemExit) as caught:
        bsp_check.main()
    assert caught.value.code == 2
    assert "доступен только через сервер данных" in capsys.readouterr().err


@pytest.mark.parametrize("accept", [False, True])
def test_exchange_cli_accept_flag(tmp_path, monkeypatch, capsys, accept):
    from tests.test_exchange_check import ec

    class CliBase(ec.DataServer):
        database: Database

        def __init__(self, label, database):
            super().__init__(label, "", {})
            object.__setattr__(self, "database", database)

        def execute(self, code):
            if "ВыполнитьВыгрузкуДляУзла" in code:
                assert "\n" not in code and "\r" not in code and "//" not in code
                self.database.codes.append(code)
                return ok({"message": "PG1lc3NhZ2UvPg=="})
            return self.database.execute(code)

    source, target = Database("SRC", "DST"), Database("DST", "SRC")
    base = CompatibilityBase(source, "УстаревшиеПравила")
    source.execute = base.execute
    source_cli, target_cli = CliBase("a.src", source), CliBase("a.dst", target)
    archive = tmp_path / "rules.zip"
    with zipfile.ZipFile(archive, "w") as package:
        for name in (
            "ExchangeRules.xml",
            "CorrespondentExchangeRules.xml",
            "RegistrationRules.xml",
        ):
            package.writestr(name, "<Правила/>")
    argv = [
        "exchange_check",
        "run",
        "--plan",
        "План",
        "--source",
        "a.src",
        "--target",
        "a.dst",
        "--object",
        "Справочник.Элемент",
        "--ref",
        "uuid",
        "--source-rules",
        str(archive),
        *(["--accept-version-mismatch"] if accept else []),
    ]
    monkeypatch.setattr(ec, "RUNS", tmp_path / "runs")
    monkeypatch.setattr(sys, "argv", argv)
    monkeypatch.setattr(ec, "server_for", lambda ref: source_cli if ref == "a.src" else target_cli)
    with pytest.raises(SystemExit) as caught:
        ec.main()
    assert caught.value.code == (0 if accept else 1)
    assert base.writes == (1 if accept else 0)
    lines = capsys.readouterr().out.splitlines()
    compatibility = "СОВМЕСТИМОСТЬ УстаревшиеПравила: Правила для другой версии"
    assert compatibility in lines
    assert lines.index(compatibility) < len(lines) - 2
    assert lines[-2:] == ["ИТОГ " + ("OK" if accept else "ОШИБКА"), "КОНЕЦ"]
    if not accept:
        assert any("accept_version_mismatch=true" in line for line in lines)
