"""`kdbase/exchange_check.py` без баз 1С: код для сервера данных, разбор ответов, выбор базы."""

import argparse
import base64
import json
import re
import sys
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import ClassVar

import pytest

from kd2_rules_mcp.projects import ProjectConfigError

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "kdbase"))

import exchange_check as ec  # noqa: E402 — скрипт из kdbase/, не пакет

PLAN = "ОбменАльфаБета"


def _all_snippets() -> dict[str, str]:
    return {
        "this_node_code": ec.this_node_code(PLAN, "KD2S"),
        "correspondent_node": ec.correspondent_node(PLAN, 'K"1'),
        "load_rules": ec.load_rules(PLAN, b"PK\x03\x04", "Правила.zip"),
        "export_object": ec.export_object(PLAN, "KD2T", "Документ.Заказ", "0a-1b"),
        "import_message": ec.import_message(PLAN, "KD2S", "<ФайлОбмена/>".encode()),
        "query_rows": ec.query_rows('ВЫБРАТЬ\n  К.Ссылка\nИЗ Справочник.К КАК К ГДЕ К.ИНН = "1"'),
    }


@pytest.mark.parametrize("name", list(_all_snippets()))
def test_snippets_are_one_guarded_line(name: str) -> None:
    code = _all_snippets()[name]
    assert "\n" not in code, "многострочный код сервер данных не выполняет"
    assert code.startswith("Попытка ") and code.endswith("КонецПопытки;")
    assert 'Результат = "ОШИБКА " + ПодробноеПредставлениеОшибки' in code
    # Метод у выражения «Новый Тип(…)» — ошибка компиляции 1С («Неопознанный оператор»).
    assert not re.search(r"Новый \w+\([^;]*?\)\s*\.", code)


def test_snippets_quote_values_and_payloads() -> None:
    snippets = _all_snippets()
    assert 'НайтиПоКоду("K""1")' in snippets["correspondent_node"]
    assert '"ВЫБРАТЬ К.Ссылка ИЗ Справочник.К КАК К ГДЕ К.ИНН = ""1"""' in snippets["query_rows"]
    payload = base64.b64encode("<ФайлОбмена/>".encode()).decode()
    assert f'Base64Значение("{payload}")' in snippets["import_message"]
    assert "ЗагрузитьКомплектПравил(Отказ, Данные, Описание, Адрес, " in snippets["load_rules"]
    assert '"Правила.zip"' in snippets["load_rules"]
    assert "ВоВременноеХранилище( " in snippets["export_object"]
    assert 'МенеджерОбъектаПоПолномуИмени("Документ.Заказ")' in snippets["export_object"]


def test_short_error_drops_code_echo_and_stack() -> None:
    text = (
        "ОШИБКА Ошибка в обработчике события ПослеЗагрузкиОбъекта\n"
        "\tИмяПКО = Заказ\n"
        "{Обработка.КонвертацияОбъектовИнформационныхБаз.МодульОбъекта(4462)}:ВызватьИсключение\n"
        '{(1)}:Попытка Base64Значение("' + "A" * 300 + '")'
    )
    assert (
        ec.short_error(text) == "Ошибка в обработчике события ПослеЗагрузкиОбъекта ИмяПКО = Заказ"
    )


class _Stub(BaseHTTPRequestHandler):
    """Сервер данных: отвечает заранее заданным текстом, запоминает запрос."""

    reply: str = "OK"
    sse: bool = False
    seen: ClassVar[list[tuple[dict[str, str], dict[str, object]]]] = []

    def do_POST(self) -> None:
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        type(self).seen.append((dict(self.headers), body))
        answer = {
            "jsonrpc": "2.0",
            "id": 1,
            "result": {"content": [{"type": "text", "text": self.reply}]},
        }
        data = json.dumps(answer, ensure_ascii=False)
        payload = (f"event: message\ndata: {data}\n\n" if self.sse else data).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream" if self.sse else "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, format: str, *args: object) -> None:
        return


@pytest.fixture
def stub() -> Iterator[ec.DataServer]:
    _Stub.seen = []
    server = HTTPServer(("127.0.0.1", 0), _Stub)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        url = f"http://127.0.0.1:{server.server_port}/base/hs/mcp"
        yield ec.DataServer("alpha.sandbox", url, {"Authorization": "Basic eA=="})
    finally:
        server.shutdown()


@pytest.mark.parametrize("sse", [False, True], ids=["json", "sse"])
def test_data_server_calls_tool_and_parses_answer(stub: ec.DataServer, sse: bool) -> None:
    _Stub.reply, _Stub.sse = "OK 3\nшапка", sse
    assert stub.run("Результат = 1;") == "3\nшапка"
    headers, body = _Stub.seen[-1]
    assert headers["Authorization"] == "Basic eA=="
    assert body["method"] == "tools/call"
    assert body["params"] == {"name": "vcexecutecode", "arguments": {"bslcode": "Результат = 1;"}}


def test_data_server_raises_on_error_and_empty_answer(stub: ec.DataServer) -> None:
    _Stub.reply, _Stub.sse = "ОШИБКА Нет узла KD2T: выполните setup\n{(1)}:Попытка …", False
    with pytest.raises(
        ec.ExchangeCheckError, match=r"^alpha\.sandbox: Нет узла KD2T: выполните setup$"
    ):
        stub.run("…")
    _Stub.reply = ""
    with pytest.raises(ec.ExchangeCheckError, match="пустой ответ"):
        stub.run("…")


def _machine(tmp_path: Path) -> Path:
    """Каталог проектов, личный файл и .mcp.json проекта во временной папке."""
    (tmp_path / "projects.yaml").write_text(
        """
projects:
  alpha:
    name: Альфа
    mcp_config: .mcp.json
    configurations: {full: {dump: main}}
    bases:
      sandbox:
        {role: песочница, configuration: full, connection: 'Srvr="s";Ref="a";', data_mcp: data-a}
      nodata: {role: песочница, configuration: full, connection: 'Srvr="s";Ref="n";'}
      prod: {role: боевая, configuration: full, connection: 'Srvr="s";Ref="p";', data_mcp: data-a}
""",
        encoding="utf-8",
    )
    project = tmp_path / "alpha"
    project.mkdir()
    (project / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"data-a": {"type": "http", "url": "http://srv/a/hs/mcp"}}}),
        encoding="utf-8",
    )
    (tmp_path / "projects.local.yaml").write_text(
        f"projects:\n  alpha: {project}\n"
        "logins:\n  alpha.sandbox: {user: Агент, password: '1'}\n",
        encoding="utf-8",
    )
    return tmp_path


def test_server_for_takes_sandbox_endpoint_and_login(tmp_path: Path) -> None:
    server = ec.server_for("alpha.sandbox", _machine(tmp_path))
    assert server.url == "http://srv/a/hs/mcp"
    token = base64.b64encode("Агент:1".encode()).decode()
    assert server.headers == {"Authorization": f"Basic {token}"}


@pytest.mark.parametrize(
    ("ref", "error", "message"),
    [
        ("alpha.prod", ProjectConfigError, "только с песочницами"),
        ("alpha.nodata", ProjectConfigError, "не задан data_mcp"),
        ("alpha", ec.ExchangeCheckError, r"<проект>\.<база>"),
    ],
)
def test_server_for_refuses(tmp_path: Path, ref: str, error: type[Exception], message: str) -> None:
    with pytest.raises(error, match=message):
        ec.server_for(ref, _machine(tmp_path))


def test_arguments() -> None:
    args = ec.parse_args(
        [
            "run",
            "--plan",
            PLAN,
            "--source",
            "a.b",
            "--target",
            "c.d",
            "--object",
            "Документ.Заказ",
            "--ref",
            "0a",
            "--query",
            "ВЫБРАТЬ 1",
            "--expect-rows",
            "1",
        ]
    )
    assert isinstance(args, argparse.Namespace)
    assert (args.source_code, args.target_code, args.expect_rows) == ("KD2S", "KD2T", 1)
    assert args.source_rules is None


class _Scripted(ec.DataServer):
    """Сервер данных с ответами по очереди (без HTTP)."""

    def __init__(self, label: str, replies: list[str]) -> None:
        super().__init__(label, "", {})
        object.__setattr__(self, "_replies", replies)

    def call(self, code: str) -> str:
        return self._replies.pop(0)  # type: ignore[attr-defined]  # задан в __init__


@pytest.mark.parametrize(
    ("imported", "found", "ok", "expected"),
    [
        (
            "OK\nСООБЩЕНИЕ загружено",
            "OK 1\nН\nручной",
            True,
            ["ЗАГРУЗКА OK", "СООБЩЕНИЕ загружено"],
        ),
        ("OK", "OK 2\nН\nручной\nдубль", False, ["ОШИБКА ожидалось строк: 1, получено: 2"]),
        ("ОШИБКА Ошибка в обработчике\n{стек}", "OK 1\nН\nx", False, ["ЗАГРУЗКА ОШИБКА"]),
    ],
    ids=["найден", "дубль", "ошибка-загрузки"],
)
def test_run_protocol(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    imported: str,
    found: str,
    ok: bool,
    expected: list[str],
) -> None:
    monkeypatch.setattr(ec, "RUNS", tmp_path)
    message = base64.b64encode("<ФайлОбмена/>".encode()).decode()
    source = _Scripted("a.src", ["OK KD2S", f"OK {message}"])
    target = _Scripted("a.dst", ["OK KD2T", imported, found])
    args = ec.parse_args(
        [
            "run",
            "--plan",
            PLAN,
            "--source",
            "a.src",
            "--target",
            "a.dst",
            "--object",
            "Документ.З",
            "--ref",
            "0a",
            "--query",
            "ВЫБРАТЬ 1",
            "--expect-rows",
            "1",
        ]
    )
    lines: list[str] = []
    assert ec.run(args, source, target, lines) is ok
    text = "\n".join(lines)
    assert "ИСТОЧНИК a.src (KD2S) → ПРИЕМНИК a.dst (KD2T)" in text
    assert all(item in text for item in expected), text
    assert (next(tmp_path.glob("exchange-*")) / "message.xml").read_text("utf-8") == "<ФайлОбмена/>"
