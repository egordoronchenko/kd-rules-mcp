"""Живые сценарии через подставной сервер данных: ни одного подключения к базе."""

import base64
import hashlib
import json
import sys
import zipfile
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "kdbase"))

import bsp_load  # noqa: E402 — скрипты kdbase, не пакет
import exchange_check as exchange  # noqa: E402 — скрипты kdbase, не пакет
import live_checks as live  # noqa: E402 — скрипты kdbase, не пакет
import plans_overview as plans  # noqa: E402 — скрипты kdbase, не пакет
import rules_dump as rules  # noqa: E402 — скрипты kdbase, не пакет


class Scripted(bsp_load.DataServer):
    """Полный протокол DataServer.run, подменён только HTTP-вызов."""

    replies: list[str | Exception]
    codes: list[str]

    def __init__(self, replies: Sequence[str | Exception], label: str = "alpha.sandbox") -> None:
        super().__init__(label, "", {})
        object.__setattr__(self, "replies", list(replies))
        object.__setattr__(self, "codes", [])

    def call(self, code: str) -> str:
        assert "\n" not in code and "\r" not in code
        self.codes.append(code)
        assert self.replies, "лишний вызов после последней стадии"
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


def answer(value: Any) -> str:
    return "OK " + json.dumps(value, ensure_ascii=False)


def info(present: bool = True) -> dict[str, Any]:
    if not present:
        return {"present": False}
    return {
        "present": True,
        "source": "Файл",
        "template": "ПравилаОбмена",
        "file": "rules.zip",
        "loaded": True,
        "info": "Версия: 2.01\nДата: 2026-01-01",
    }


def rules_replies(data: bytes = b"<rules/>", chunk_size: int = 4) -> list[str | Exception]:
    encoded = base64.b64encode(data).decode("ascii")
    result: list[str | Exception] = []
    for _ in rules.FILES:
        details = info()
        details.update(
            binary=True,
            size=len(data),
            length=len(encoded),
            hash=base64.b64encode(hashlib.sha256(data).digest()).decode("ascii"),
        )
        result.append(answer(details))
        result.extend(
            "OK " + encoded[p : p + chunk_size] for p in range(0, len(encoded), chunk_size)
        )
    return result


def dump_args(out: Path, chunk_size: int = 4):
    return rules.parse_args(
        [
            "--base",
            "alpha.sandbox",
            "--plan",
            "План",
            "--out",
            str(out),
            "--chunk-size",
            str(chunk_size),
        ]
    )


def test_rules_dump_reassembles_zip(tmp_path: Path) -> None:
    server = Scripted(rules_replies())
    lines: list[str] = []
    archive = rules.dump(dump_args(tmp_path), server, lines)
    with zipfile.ZipFile(archive) as package:
        assert package.namelist() == list(rules.FILES.values())
        assert all(package.read(n) == b"<rules/>" for n in package.namelist())
    assert [line for line in lines if line.startswith("ЧАСТИ")] == [
        "ЧАСТИ exchange: 3",
        "ЧАСТИ correspondent: 3",
        "ЧАСТИ registration: 3",
    ]
    assert "ПРАВИЛА exchange: источник=Файл" in "\n".join(lines)
    assert "8 байт" in "\n".join(lines) and "ZIP OK" in lines
    assert "полный комплект" in lines[-1]
    assert not server.replies
    assert all(".Записать(" not in c and "ОбновлениеПравил" not in c for c in server.codes)


@pytest.mark.parametrize("failed", range(12))
@pytest.mark.parametrize(
    "failure", ["", "ОШИБКА сбой стадии", bsp_load.ExchangeCheckError("нет HTTP")]
)
def test_rules_stops_at_each_server_failure(tmp_path: Path, failed: int, failure) -> None:
    replies = rules_replies()
    replies[failed] = failure
    server = Scripted(replies)
    lines: list[str] = []
    with pytest.raises(bsp_load.ExchangeCheckError):
        rules.dump(dump_args(tmp_path), server, lines)
    assert len(server.codes) == failed + 1
    assert "ZIP OK" not in lines and any("ОШИБКА" in line for line in lines)


@pytest.mark.parametrize("failure", ["OK xyz", "OK AAAA", "OK AA", "OK !!!!"])
def test_rules_rejects_bad_or_changed_chunks(tmp_path: Path, failure: str) -> None:
    replies = rules_replies()
    replies[1] = failure
    lines: list[str] = []
    with pytest.raises((ValueError, bsp_load.ExchangeCheckError)):
        rules.dump(dump_args(tmp_path), Scripted(replies), lines)
    assert "ZIP OK" not in lines


def test_rules_absent_records_and_xml(tmp_path: Path) -> None:
    present = info()
    present["binary"] = False
    server = Scripted([answer(info(False)), answer(info(False)), answer(present)])
    lines: list[str] = []
    archive = rules.dump(dump_args(tmp_path), server, lines)
    assert "ПРАВИЛА exchange: нет записи в регистре, 0 байт" in lines
    assert "XML отсутствует" in "\n".join(lines)
    with zipfile.ZipFile(archive) as package:
        assert package.namelist() == []
    assert "неполный комплект" in lines[-1]


@pytest.mark.parametrize("operation", ["mkdir", "write_bytes", "zip"])
def test_dump_filesystem_stage_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    def fail(*args, **kwargs):
        raise OSError("сбой файловой стадии")

    if operation == "zip":
        monkeypatch.setattr(rules.zipfile, "ZipFile", fail)
    else:
        monkeypatch.setattr(Path, operation, fail)
    lines: list[str] = []
    with pytest.raises(OSError, match="сбой файловой"):
        rules.dump(dump_args(tmp_path), Scripted(rules_replies()), lines)
    assert "ОШИБКА" in lines[-1]


def plan_info(bsp: bool = True, rib: bool = False) -> dict[str, Any]:
    return {
        "bsp": bsp,
        "rib": rib,
        "technology": "XML" if bsp else "не определена (план вне БСП)",
        "nodes": 2,
        "templates": [
            {"name": "ПравилаОбмена", "present": True, "size": 8},
            {"name": "ПравилаОбменаКорреспондента", "present": True, "size": 0},
            {"name": "ПравилаРегистрации", "present": False},
        ],
        "rules": [{"kind": "exchange", **info()}, {"kind": "registration", **info(False)}],
    }


def test_plans_overview_success() -> None:
    universal = plan_info()
    universal["technology"] = "универсальный формат"
    server = Scripted(
        [
            answer(["План", "Другой", "Универсальный"]),
            answer(plan_info()),
            answer(plan_info(False, True)),
            answer(universal),
        ]
    )
    lines: list[str] = []
    plans.overview(server, lines)
    out = "\n".join(lines)
    assert "ПЛАН План: БСП=да, технология=XML, РИБ=нет, узлов кроме этого=2" in out
    assert "РИБ=да" in out and "универсальный формат" in out
    assert "МАКЕТ ПравилаОбменаКорреспондента: пустой" in lines
    assert "ЗАПИСЬ registration: нет записи в регистре" in lines
    assert all(".Записать();" not in c for c in server.codes)


@pytest.mark.parametrize("failed", [0, 1, 2])
@pytest.mark.parametrize("failure", ["", "ОШИБКА стадия", "OK {"])
def test_plans_failure_stops(failed: int, failure: str) -> None:
    replies = [answer(["План", "Другой"]), answer(plan_info()), answer(plan_info())]
    replies[failed] = failure
    server = Scripted(replies)
    lines: list[str] = []
    with pytest.raises((ValueError, bsp_load.ExchangeCheckError)):
        plans.overview(server, lines)
    assert len(server.codes) == failed + 1
    assert "ОШИБКА" in lines[-1]


def test_plans_empty() -> None:
    lines: list[str] = []
    plans.overview(Scripted([answer([])]), lines)
    assert lines == ["СПИСОК ПЛАНОВ OK", "ПЛАНЫ отсутствуют"]


OLD = {"ref": "11111111-1111-1111-1111-111111111111", "code": "OLD"}
NEW = {"ref": "22222222-2222-2222-2222-222222222222", "code": "NEW"}
REF = "33333333-3333-3333-3333-333333333333"


def register_args(restore: bool = True):
    argv = ["register", "--base", "alpha.sandbox", "--object", "Справочник.Элемент", "--ref", REF]
    if restore:
        argv.append("--restore")
    return exchange.parse_args(argv)


def register_replies() -> list[str | Exception]:
    return [
        answer({"План": [OLD], "Другой": []}),
        "OK",
        answer({"План": [OLD, NEW], "Другой": []}),
        "OK",
        answer({"План": [OLD], "Другой": []}),
    ]


def test_register_success_and_restore() -> None:
    server = Scripted(register_replies())
    lines: list[str] = []
    assert exchange.register(register_args(), server, lines)
    assert f"РЕГИСТРАЦИЯ План добавлено: NEW ({NEW['ref']})" in lines
    assert "РЕГИСТРАЦИЯ планов без регистрации объекта до и после: 1" in lines
    assert not any(line.startswith("РЕГИСТРАЦИЯ Другой") for line in lines)
    assert "ПРОВЕРКА ВОССТАНОВЛЕНИЯ OK" in lines
    assert OLD["ref"] not in server.codes[3] and NEW["ref"] in server.codes[3]
    assert all("ЗарегистрироватьИзменения" not in c for c in server.codes)
    assert all("ОбменДанными.Загрузка" not in c for c in server.codes)


def test_register_without_restore_changes_no_registration() -> None:
    server = Scripted(register_replies()[:3])
    assert exchange.register(register_args(False), server, [])
    assert all("УдалитьРегистрацию" not in c for c in server.codes)


@pytest.mark.parametrize("failed", range(5))
@pytest.mark.parametrize("failure", ["", "ОШИБКА стадия", bsp_load.ExchangeCheckError("нет HTTP")])
def test_register_stops_at_each_stage(failed: int, failure) -> None:
    replies = register_replies()
    replies[failed] = failure
    server = Scripted(replies)
    lines: list[str] = []
    with pytest.raises(bsp_load.ExchangeCheckError):
        exchange.register(register_args(), server, lines)
    assert len(server.codes) == failed + 1
    assert "ОШИБКА" in lines[-1]


def test_restore_checks_actual_result() -> None:
    replies = register_replies()
    replies[-1] = answer({"План": [OLD, NEW], "Другой": []})
    with pytest.raises(bsp_load.ExchangeCheckError, match="не снята"):
        exchange.register(register_args(), Scripted(replies), [])


def delete_args(mode: str = "delete", expect: str = "deleted", confirm: bool = True, extra=()):
    argv = [
        "delete",
        "--plan",
        "План",
        "--source",
        "alpha.sandbox",
        "--target",
        "beta.sandbox",
        "--object",
        "Справочник.Элемент",
        "--ref",
        REF,
        "--mode",
        mode,
        "--expect",
        expect,
        *extra,
    ]
    if confirm:
        argv.append("--confirm")
    return exchange.parse_args(argv)


def delete_servers(state: str = "deleted", rules_files: bool = False) -> tuple[Scripted, Scripted]:
    source = [
        "OK SRC",
        "OK",
        *(["OK"] if rules_files else []),
        "OK",
        "OK",
        "OK " + base64.b64encode(b"<message/>").decode("ascii"),
    ]
    target = ["OK DST", "OK kept", *(["OK"] if rules_files else []), "OK", "OK " + state]
    return Scripted(source), Scripted(target, "beta.sandbox")


@pytest.mark.parametrize(
    "mode,state",
    [("mark", "marked"), ("delete", "deleted"), ("delete", "marked"), ("delete", "kept")],
)
def test_delete_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str, state: str
) -> None:
    monkeypatch.setattr(exchange, "RUNS", tmp_path)
    source, target = delete_servers(state)
    lines: list[str] = []
    assert exchange.delete(delete_args(mode, state), source, target, lines)
    assert f"УДАЛЕНИЕ факт={state}, ожидалось={state}" in lines
    assert "СРАВНЕНИЕ OK" in lines
    mutation = source.codes[3]
    assert (
        REF in mutation and "О.Удалить();" in mutation if mode == "delete" else "Ложь);" in mutation
    )
    assert "НачатьТранзакцию" in mutation and "ОтменитьТранзакцию" in mutation
    assert (next(tmp_path.glob("delete-*/message.xml"))).read_bytes() == b"<message/>"


@pytest.mark.parametrize(
    "side,index", [("source", i) for i in range(7)] + [("target", i) for i in range(5)]
)
@pytest.mark.parametrize("failure", ["", "ОШИБКА объект имеет ссылки"])
def test_delete_each_stage_stops(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, side: str, index: int, failure: str
) -> None:
    monkeypatch.setattr(exchange, "RUNS", tmp_path)
    archive = tmp_path / "rules.zip"
    archive.write_bytes(b"PK")
    source, target = delete_servers(rules_files=True)
    # source: код, состав, правила, очистка, изменение, выгрузка — шесть вызовов.
    if side == "source" and index == 6:
        source.replies[-1] = "OK !!!"
        index = 5
    else:
        (source if side == "source" else target).replies[index] = failure
    lines: list[str] = []
    with pytest.raises((ValueError, bsp_load.ExchangeCheckError)):
        exchange.delete(
            delete_args(extra=("--source-rules", str(archive), "--target-rules", str(archive))),
            source,
            target,
            lines,
        )
    assert "СРАВНЕНИЕ OK" not in lines
    if side == "source" and index <= 4:
        assert not any("ВыполнитьОбменДаннымиДляУзла" in c for c in target.codes)


def test_delete_mismatch_and_missing_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(exchange, "RUNS", tmp_path)
    source, target = delete_servers("kept")
    lines: list[str] = []
    assert not exchange.delete(delete_args(), source, target, lines)
    assert "СРАВНЕНИЕ ОШИБКА" in lines
    source, target = delete_servers()
    target.replies[1] = "OK deleted"
    with pytest.raises(bsp_load.ExchangeCheckError, match="не найден"):
        exchange.delete(delete_args(), source, target, [])
    assert len(source.codes) == 2


def test_delete_no_confirm_does_not_open_servers(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def forbidden(*args):
        pytest.fail("без confirm сервер не должен открываться")

    monkeypatch.setattr(exchange, "server_for", forbidden)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "exchange_check",
            "delete",
            "--source",
            "alpha.sandbox",
            "--target",
            "beta.sandbox",
            "--plan",
            "План",
            "--object",
            "Справочник.Элемент",
            "--ref",
            REF,
            "--mode",
            "delete",
        ],
    )
    with pytest.raises(SystemExit) as caught:
        exchange.main()
    assert caught.value.code == 2
    out = capsys.readouterr().out
    assert "ПРЕДПРОСМОТР" in out and "--confirm" in out and "ИТОГ OK" not in out


@pytest.mark.parametrize("scenario", ["rules", "plans", "register", "delete"])
def test_non_sandbox_refused_before_http(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    scenario: str,
) -> None:
    (tmp_path / "projects.yaml").write_text(
        """projects:
  alpha:
    name: Альфа
    configurations: {full: {dump: main}}
    bases:
      prod: {role: боевая, configuration: full, connection: 'File="x";', data_mcp: data}
""",
        encoding="utf-8",
    )
    (tmp_path / "projects.local.yaml").write_text("{}", encoding="utf-8")
    module = rules if scenario == "rules" else plans if scenario == "plans" else exchange
    monkeypatch.setattr(module, "server_for", lambda ref: bsp_load.server_for(ref, tmp_path))
    argv = ["--base", "alpha.prod"]
    if scenario == "rules":
        argv += ["--plan", "План", "--out", str(tmp_path)]
    elif scenario == "register":
        argv = ["register", *argv, "--object", "Справочник.Элемент", "--ref", REF]
    elif scenario == "delete":
        argv = [
            "delete",
            "--source",
            "alpha.prod",
            "--target",
            "alpha.prod",
            "--plan",
            "План",
            "--object",
            "Справочник.Элемент",
            "--ref",
            REF,
            "--mode",
            "mark",
            "--confirm",
        ]
    monkeypatch.setattr(sys, "argv", [module.__name__, *argv])
    with pytest.raises(SystemExit) as caught:
        module.main()
    assert caught.value.code == 1
    out = capsys.readouterr().out
    assert "песочниц" in out and "ИТОГ ОШИБКА" in out and "ИТОГ OK" not in out


def test_all_new_builders_escape_arguments() -> None:
    value = 'Имя"; Выполнить("опасно");\r\nследующая'
    escaped = live.literal(value)
    assert '""' in escaped and "Символ(13)" in escaped and "Символ(10)" in escaped
    for code in [
        rules.metadata_code(value, "exchange"),
        rules.chunk_code(value, "registration", 0, 4, value),
        plans.overview_code(value),
        exchange.registration_code(value, value, value),
        exchange.write_object_code(value, value),
        exchange.state_code(value, value),
        exchange._safe_code(exchange.this_node_code, value, value),
        exchange.mutation_code(value, value, value, value, "mark"),
        exchange.export_registered_code(value, value),
        exchange._safe_code(exchange.plan_content_check, value, value),
        exchange.restore_code(value, value, {value: [{"ref": value, "code": value}]}),
    ]:
        assert "\n" not in code and "\r" not in code
        assert escaped in code
        assert 'Выполнить("опасно")' not in code


@pytest.mark.parametrize("size", [0, 3, 5, 96004])
def test_chunk_size_boundaries(size: int) -> None:
    with pytest.raises(SystemExit) as caught:
        rules.parse_args(["--base", "alpha.sandbox", "--plan", "План", "--chunk-size", str(size)])
    assert caught.value.code == 2


def test_success_protocols_from_main(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    cases = [
        (
            rules,
            [
                "--base",
                "alpha.sandbox",
                "--plan",
                "План",
                "--out",
                str(tmp_path),
                "--chunk-size",
                "4",
            ],
            Scripted(rules_replies()),
        ),
        (plans, ["--base", "alpha.sandbox"], Scripted([answer(["План"]), answer(plan_info())])),
        (
            exchange,
            [
                "register",
                "--base",
                "alpha.sandbox",
                "--object",
                "Справочник.Элемент",
                "--ref",
                REF,
                "--restore",
            ],
            Scripted(register_replies()),
        ),
    ]
    for module, argv, server in cases:
        monkeypatch.setattr(module, "server_for", lambda ref, s=server: s)
        monkeypatch.setattr(sys, "argv", [module.__name__, *argv])
        with pytest.raises(SystemExit) as caught:
            module.main()
        assert caught.value.code == 0
        out = capsys.readouterr().out
        assert "ИТОГ OK\nКОНЕЦ\n" in out


def test_delete_main_protocol(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(exchange, "RUNS", tmp_path)
    source, target = delete_servers()
    monkeypatch.setattr(
        exchange, "server_for", lambda ref: source if ref == source.label else target
    )
    args = delete_args()
    monkeypatch.setattr(exchange, "parse_args", lambda: args)
    with pytest.raises(SystemExit) as caught:
        exchange.main()
    assert caught.value.code == 0
    out = capsys.readouterr().out
    assert "ИЗМЕНЕНИЕ ИСТОЧНИКА OK\nВЫГРУЗКА OK\nЗАГРУЗКА OK" in out
    assert "УДАЛЕНИЕ факт=deleted, ожидалось=deleted\nСРАВНЕНИЕ OK\nИТОГ OK\nКОНЕЦ" in out


@pytest.mark.parametrize("operation", ["mkdir", "write_bytes"])
def test_delete_message_file_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    monkeypatch.setattr(exchange, "RUNS", tmp_path)
    source, target = delete_servers()

    def fail(*args, **kwargs):
        raise OSError("сбой сохранения сообщения")

    monkeypatch.setattr(Path, operation, fail)
    lines: list[str] = []
    with pytest.raises(OSError, match="сбой сохранения"):
        exchange.delete(delete_args(), source, target, lines)
    assert "ВЫГРУЗКА ОШИБКА сбой сохранения сообщения" in lines
    assert "ЗАГРУЗКА OK" not in lines and len(target.codes) == 2


@pytest.mark.parametrize(
    "reply,expected",
    [("ОШИБКА запрос", False), ("OK 1\nСсылка\nстрока", False), ("OK 0\nСсылка", True)],
)
def test_delete_optional_query(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, reply: str, expected: bool
) -> None:
    monkeypatch.setattr(exchange, "RUNS", tmp_path)
    source, target = delete_servers()
    target.replies.append(reply)
    args = delete_args(extra=("--query", 'ВЫБРАТЬ "кавычки" КАК Ссылка', "--expect-rows", "0"))
    lines: list[str] = []
    if reply.startswith("ОШИБКА"):
        with pytest.raises(bsp_load.ExchangeCheckError):
            exchange.delete(args, source, target, lines)
    else:
        assert exchange.delete(args, source, target, lines) == expected


def test_delete_checks_confirmation_and_expectation() -> None:
    source, target = delete_servers()
    args = delete_args(confirm=False)
    with pytest.raises(bsp_load.ExchangeCheckError, match="confirm"):
        exchange.delete(args, source, target, [])
    args.confirm, args.expect = True, None
    with pytest.raises(bsp_load.ExchangeCheckError, match="expect"):
        exchange.delete(args, source, target, [])
    assert not source.codes and not target.codes


def test_register_specific_plan_and_no_new_registration() -> None:
    args = register_args()
    args.plan = "План"
    server = Scripted(
        [answer({"План": [OLD]}), "OK", answer({"План": [OLD]}), "OK", answer({"План": [OLD]})]
    )
    lines: list[str] = []
    assert exchange.register(args, server, lines)
    assert "РЕГИСТРАЦИЯ План добавлено: —" in lines
    assert "УдалитьРегистрациюИзменений" not in server.codes[3]


@pytest.mark.parametrize("reply", ["OK {", answer([]), answer({"План": ["узел"]})])
def test_register_invalid_snapshot_stops_before_write(reply: str) -> None:
    server = Scripted([reply])
    with pytest.raises((ValueError, bsp_load.ExchangeCheckError)):
        exchange.register(register_args(), server, [])
    assert len(server.codes) == 1


def test_dump_rule_sources_and_empty_binary(tmp_path: Path) -> None:
    details = info()
    details.update(
        source="МакетКонфигурации",
        file="",
        binary=True,
        size=0,
        length=0,
        hash=base64.b64encode(hashlib.sha256(b"").digest()).decode("ascii"),
    )
    manager = info()
    manager.update(source="МенеджерТиповой", binary=False, loaded=False)
    lines: list[str] = []
    archive = rules.dump(
        dump_args(tmp_path),
        Scripted([answer(details), answer(info(False)), answer(manager)]),
        lines,
    )
    with zipfile.ZipFile(archive) as package:
        assert package.namelist() == ["ExchangeRules.xml"]
        assert package.read("ExchangeRules.xml") == b""
    assert "источник=МакетКонфигурации" in "\n".join(lines)
    assert "источник=МенеджерТиповой" in "\n".join(lines)


def test_delete_escapes_rules_and_import(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(exchange, "RUNS", tmp_path)
    archive = tmp_path / "rules.zip"
    archive.write_bytes(b"zip")
    args = delete_args(
        extra=(
            "--target-object",
            "Справочник.Другой",
            "--target-ref",
            NEW["ref"],
            "--source-rules",
            str(archive),
            "--target-rules",
            str(archive),
        )
    )
    args.plan = 'План"\nдругая строка'
    source, target = delete_servers(rules_files=True)
    assert exchange.delete(args, source, target, [])
    assert live.literal(args.plan) in source.codes[2]
    assert live.literal(args.plan) in target.codes[3]
    assert NEW["ref"] in target.codes[1]
    assert all(REF not in c for c in (target.codes[1], target.codes[-1]))


@pytest.mark.parametrize("scenario", ["rules", "plans", "register", "delete"])
def test_failed_main_has_nonzero_exit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    scenario: str,
) -> None:
    module = rules if scenario == "rules" else plans if scenario == "plans" else exchange
    args = (
        dump_args(tmp_path)
        if scenario == "rules"
        else plans.parse_args(["--base", "alpha.sandbox"])
        if scenario == "plans"
        else register_args()
        if scenario == "register"
        else delete_args()
    )
    monkeypatch.setattr(module, "parse_args", lambda: args)
    source, target = Scripted(["ОШИБКА сбой"]), Scripted(["ОШИБКА сбой"], "beta.sandbox")
    monkeypatch.setattr(module, "server_for", lambda ref: source if ref == source.label else target)
    with pytest.raises(SystemExit) as caught:
        module.main()
    assert caught.value.code == 1
    out = capsys.readouterr().out
    assert "ИТОГ ОШИБКА\nКОНЕЦ" in out and "ИТОГ OK" not in out
