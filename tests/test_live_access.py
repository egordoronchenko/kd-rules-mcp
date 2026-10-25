"""Транспорт подменён на уровне HTTP: настоящей сети нет."""

import io
import json
import socket
import time
from pathlib import Path
from typing import Any

import pytest

from kd_rules_mcp.errors import (
    LiveBaseRefusedError,
    LiveConnectError,
    LiveEmptyError,
    LiveProtocolError,
    LiveTransportError,
)
from kd_rules_mcp.live.access import DataMcpAccess, resolve_base
from kd_rules_mcp.projects import LocalSettings, load_catalog


def answer(text: str, **fields: Any) -> dict:
    return {
        "jsonrpc": "2.0",
        "id": 1,
        "result": {"content": [{"type": "text", "text": text}], **fields},
    }


@pytest.mark.parametrize("sse", [False, True])
def test_transport_json_sse_other_tool(monkeypatch: pytest.MonkeyPatch, sse: bool) -> None:
    seen = []
    raw = json.dumps(answer('OK {"value": 7}'))
    if sse:
        raw = f"event: message\ndata: {raw}\n\n"

    def open_stub(request, timeout):
        seen.append((json.loads(request.data), timeout, request.headers))
        return io.BytesIO(raw.encode())

    monkeypatch.setattr("kd_rules_mcp.live.access.open_data_request", open_stub)
    access = DataMcpAccess(
        "http://server/base", {"Authorization": "Basic test"}, "run_code", "code"
    )
    assert access.execute("код") == 'OK {"value": 7}'
    assert len(seen) == 1
    assert seen[0][0]["params"] == {"name": "run_code", "arguments": {"code": "код"}}
    assert seen[0][1] == 900
    assert seen[0][2]["Authorization"] == "Basic test"


@pytest.mark.parametrize(
    "reply,kind",
    [
        ({"error": {"message": "ошибка RPC"}}, LiveProtocolError),
        (answer('OK {"success":true}', isError=True), LiveProtocolError),
        (answer(""), LiveEmptyError),
        ([], LiveProtocolError),
        ({"result": {}}, LiveProtocolError),
        ("обрыв", LiveTransportError),
    ],
)
def test_error_flag_empty_and_broken(monkeypatch: pytest.MonkeyPatch, reply, kind) -> None:
    calls = []

    def stub(request, timeout):
        calls.append(request)
        return io.BytesIO((reply if isinstance(reply, str) else json.dumps(reply)).encode())

    monkeypatch.setattr("kd_rules_mcp.live.access.open_data_request", stub)
    with pytest.raises(kind):
        DataMcpAccess("http://server", {}).execute("запись")
    assert len(calls) == 1


@pytest.mark.parametrize("failure", [OSError("обрыв"), ValueError("нет URL")])
def test_transport_no_retry(monkeypatch: pytest.MonkeyPatch, failure: Exception) -> None:
    calls = []

    def stub(*args, **kwargs):
        calls.append(1)
        raise failure

    monkeypatch.setattr("kd_rules_mcp.live.access.open_data_request", stub)
    with pytest.raises(LiveTransportError):
        DataMcpAccess("http://server", {}).execute("запись")
    assert calls == [1]


def test_secrets_never_echo(monkeypatch: pytest.MonkeyPatch) -> None:
    url, header, user, password = (
        "http://server?token=url-secret",
        "Basic header-secret",
        "login-secret",
        "pwd-secret",
    )
    echoed = " ".join(
        (url, "Authorization:", header, "url-secret", "header-secret", user, password)
    )
    for reply in [answer(echoed, isError=True), {"error": echoed}, "обрыв"]:
        monkeypatch.setattr(
            "kd_rules_mcp.live.access.open_data_request",
            lambda *a, r=reply, **k: io.BytesIO(json.dumps(r).encode()),
        )
        with pytest.raises((LiveProtocolError, LiveTransportError)) as caught:
            DataMcpAccess(url, {"Authorization": header}, secrets=(user, password)).execute("код")
        assert all(secret not in str(caught.value) for secret in (url, header, user, password))
        assert "Authorization" not in str(caught.value)
        assert "url-secret" not in str(caught.value) and "header-secret" not in str(caught.value)


def machine(tmp_path: Path, role: str = "песочница", data: str = "data", login: bool = True):
    (tmp_path / "projects.yaml").write_text(
        f"""projects:
  alpha:
    mcp_config: .mcp.json
    configurations: {{full: {{dump: main}}}}
    bases:
      sandbox:
        role: {role}
        configuration: full
        connection: 'File="base";'
        data_mcp: '{data}'
        dev_env: .dev.env
        data_access: {{tool: run_code, argument: code}}
""",
        encoding="utf-8",
    )
    (tmp_path / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"data": {"url": "http://server"}}}), encoding="utf-8"
    )
    if login:
        (tmp_path / ".dev.env").write_text(
            "IB_USER=test-user\nIB_PASSWORD=secret", encoding="utf-8"
        )
    return load_catalog(tmp_path / "projects.yaml"), LocalSettings(project_dirs={"alpha": tmp_path})


@pytest.mark.parametrize(
    "role,data,login,ref,message",
    [
        ("боевая", "data", True, "alpha.sandbox", "не песочница"),
        ("песочница", "", True, "alpha.sandbox", "нет способа доступа"),
        ("песочница", "data", False, "alpha.sandbox", "нет логина"),
        ("песочница", "data", True, "unknown.sandbox", "нет проекта unknown"),
        ("песочница", "data", True, "alpha.unknown", "в проекте alpha нет базы unknown"),
    ],
)
def test_refuse_before_network(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, role, data, login, ref, message
):
    monkeypatch.setattr(
        "kd_rules_mcp.live.access.open_data_request", lambda *a, **k: pytest.fail("сеть запрещена")
    )
    catalog, local = machine(tmp_path, role, data, login)
    with pytest.raises(LiveBaseRefusedError, match=message):
        resolve_base(catalog, local, ref)


def test_setting(tmp_path: Path) -> None:
    catalog, local = machine(tmp_path)
    _, access = resolve_base(catalog, local, "alpha.sandbox")
    assert (access.tool, access.argument) == ("run_code", "code")
    assert access.headers == {"Authorization": "Basic dGVzdC11c2VyOnNlY3JldA=="}


def test_default_setting(tmp_path: Path) -> None:
    _, local = machine(tmp_path)
    path = tmp_path / "projects.yaml"
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            "        data_access: {tool: run_code, argument: code}\n", ""
        ),
        encoding="utf-8",
    )
    _, access = resolve_base(load_catalog(path), local, "alpha.sandbox")
    assert (access.tool, access.argument) == ("vcexecutecode", "bslcode")


@pytest.mark.parametrize(
    "mapping,url,expected",
    [
        (
            "2001:db8::1=host.docker.internal",
            "http://[2001:0db8::1]:8081/base?token=x",
            "http://host.docker.internal:8081/base?token=x",
        ),
        (
            "server=host.docker.internal",
            "http://server:8081/base/hs/mcp?token=x",
            "http://host.docker.internal:8081/base/hs/mcp?token=x",
        ),
        ("", "http://server:8081/base?token=x", "http://server:8081/base?token=x"),
        ("other=host.docker.internal", "http://server/base", "http://server/base"),
    ],
)
def test_b1_host_map_preserves_port_path_query(monkeypatch, mapping, url, expected):
    monkeypatch.setenv("KD2_HOST_MAP", mapping)
    seen = []

    def open_stub(request, timeout):
        seen.append(request.full_url)
        return io.BytesIO(json.dumps(answer("OK {}")).encode())

    monkeypatch.setattr("kd_rules_mcp.live.access.open_data_request", open_stub)
    DataMcpAccess(url, {}).execute("код")
    assert seen == [expected]


@pytest.mark.parametrize(
    "mapping",
    ["server", "=target", "server=", "server=target/path", "server=target;", "server=a;server=b"],
)
def test_b1_invalid_host_map_is_configuration_error(monkeypatch, mapping):
    from kd_rules_mcp.projects import ProjectConfigError

    monkeypatch.setenv("KD2_HOST_MAP", mapping)
    with pytest.raises(ProjectConfigError, match="KD2_HOST_MAP"):
        DataMcpAccess("http://server/base", {}).execute("код")


def test_b1_transport_error_names_endpoint_without_secrets(monkeypatch):
    monkeypatch.setenv("KD2_HOST_MAP", "server=host.docker.internal")
    calls = []

    def fail(*args, **kwargs):
        calls.append(1)
        raise TimeoutError(
            "login-secret pwd-secret /base?token=url-secret Authorization: header-secret"
        )

    monkeypatch.setattr("kd_rules_mcp.live.access.open_data_request", fail)
    with pytest.raises(LiveTransportError) as caught:
        DataMcpAccess(
            "http://login-secret:pwd-secret@server:8081/base?token=url-secret",
            {"Authorization": "header-secret"},
            secrets=("login-secret", "pwd-secret"),
        ).execute("код")
    assert "host.docker.internal:8081" in str(caught.value)
    assert all(
        secret not in str(caught.value)
        for secret in (
            "login-secret",
            "pwd-secret",
            "/base",
            "url-secret",
            "header-secret",
            "Authorization",
        )
    )
    assert calls == [1]


def test_connect_phase_retried_then_reported_as_not_sent(monkeypatch):
    import urllib.request

    from kd_rules_mcp.live import access as module

    attempts = []

    def unresponsive(address, timeout, *args, **kwargs):
        attempts.append((address, timeout))
        raise TimeoutError("нет соединения")

    monkeypatch.setattr(socket, "create_connection", unresponsive)
    monkeypatch.setattr(urllib.request, "getproxies", lambda: {})
    monkeypatch.setattr(module, "CONNECT_PAUSE", 0)
    started = time.monotonic()
    with pytest.raises(
        LiveConnectError, match=r"127\.0\.0\.1:8081.*запрос не отправлялся"
    ) as caught:
        DataMcpAccess("http://127.0.0.1:8081/base", {}).execute("запись")
    assert caught.value.code == "live_connect"
    assert time.monotonic() - started < 10
    assert attempts == [(("127.0.0.1", 8081), 10)] * 3


class _Stub:
    """Настоящий HTTP-сервер на петле: считает принятые запросы."""

    def __init__(self, reply: bytes | None):
        import http.server
        import threading

        stub = self
        self.requests = 0

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                self.rfile.read(int(self.headers["Content-Length"]))
                stub.requests += 1
                if reply is None:
                    # Ответ обрывается после приёма запроса: база могла выполнить код.
                    self.wfile.write(b"HTTP/1.1 200 OK\r\nContent-Length: 100\r\n\r\n{")
                    self.close_connection = True
                    return
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(reply)))
                self.end_headers()
                self.wfile.write(reply)

            def log_message(self, format, *args):
                pass

        self.server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def test_connect_retry_succeeds_and_request_is_sent_once(monkeypatch):
    import urllib.request

    from kd_rules_mcp.live import access as module

    stub = _Stub(json.dumps(answer('OK {"value": 1}')).encode())
    real = socket.create_connection
    failures = []

    def flaky(address, timeout, *args, **kwargs):
        if len(failures) < 2:
            failures.append(address)
            raise TimeoutError("нет соединения")
        return real(address, timeout, *args, **kwargs)

    monkeypatch.setattr(socket, "create_connection", flaky)
    monkeypatch.setattr(urllib.request, "getproxies", lambda: {})
    monkeypatch.setattr(module, "CONNECT_PAUSE", 0)
    try:
        text = DataMcpAccess(f"http://127.0.0.1:{stub.port}/base", {}).execute("запись")
    finally:
        stub.close()
    assert text == 'OK {"value": 1}'
    assert len(failures) == 2
    assert stub.requests == 1


def test_broken_response_after_send_is_not_retried(monkeypatch):
    import urllib.request

    monkeypatch.setattr(urllib.request, "getproxies", lambda: {})
    stub = _Stub(None)
    try:
        with pytest.raises(LiveTransportError, match="запрос мог выполниться") as caught:
            DataMcpAccess(f"http://127.0.0.1:{stub.port}/base", {}).execute("запись")
    finally:
        stub.close()
    assert not isinstance(caught.value, LiveConnectError)
    assert caught.value.code == "live_transport"
    assert stub.requests == 1


@pytest.mark.parametrize("secure", [False, True])
def test_b1_read_timeout_restored_after_connect(monkeypatch, secure):
    import http.client

    from kd_rules_mcp.live.access import _HTTPConnection, _HTTPSConnection

    seen = []

    class Socket:
        def settimeout(self, value):
            seen.append(("read", value))

    def connect(connection):
        seen.append(("connect", connection.timeout))
        connection.sock = Socket()

    parent = http.client.HTTPSConnection if secure else http.client.HTTPConnection
    monkeypatch.setattr(parent, "connect", connect)
    connection = (_HTTPSConnection if secure else _HTTPConnection)("localhost", timeout=900)
    connection.connect()
    assert seen == [("connect", 10), ("read", 900)]
