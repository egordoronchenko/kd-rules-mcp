"""Подписанные ссылки на файлы: выдача, маршрут HTTP и границы путей."""

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import time
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from urllib.parse import quote, urlsplit

import anyio
import httpx2 as httpx
import pytest

from kd_rules_mcp.errors import FileLinkNotFoundError, Kd2Error, WorkspacePathError
from kd_rules_mcp.server import create_app, create_server, error_payload
from kd_rules_mcp.service import Kd2Service, PathMap, Settings
from kd_rules_mcp.service.file_links import (
    FileTokenLogFilter,
    link_signing_key,
    mcp_base_url,
    payload_bytes,
    request_base_url,
    sign_token,
    verify_token,
)

_KEY_LABEL = b"kd-rules-mcp/file-links/v1"


def _service(tmp: Path, **overrides: Any) -> Kd2Service:
    fields: dict[str, Any] = {
        "cache_dir": tmp / "cache",
        "workspace": tmp / "workspace",
        "projects_file": tmp / "no-projects.yaml",
    }
    fields.update(overrides)
    return Kd2Service(Settings(**fields))


def _write(service: Kd2Service, name: str, payload: bytes) -> Path:
    path = service.workspace.root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return path


def _route(url: str) -> str:
    return urlsplit(url).path


def _b64decode(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _b64encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _tamper(token: str, **changes: object) -> str:
    body_text, signature = token.split(".", 1)
    payload = json.loads(_b64decode(body_text))
    payload.update(changes)
    return _b64encode(payload_bytes(payload)) + "." + signature


def _http(app: Any, method: str, url: str, **kwargs: Any) -> tuple[int, dict[str, str], bytes]:
    async def _go() -> tuple[int, dict[str, str], bytes]:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://files.test") as client:
            response = await client.request(method, url, **kwargs)
            headers = {key.lower(): value for key, value in response.headers.items()}
            return response.status_code, headers, response.content

    return anyio.run(_go)


def _raw_http(app: Any, method: str, path: str) -> tuple[int, bytes]:
    """Вызов ASGI с путём как есть: клиент HTTP схлопывает `/files/..`."""

    async def _go() -> tuple[int, bytes]:
        status = 0
        chunks = bytearray()

        async def receive() -> dict[str, Any]:
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message: dict[str, Any]) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = int(message["status"])
            elif message["type"] == "http.response.body":
                chunks.extend(message.get("body", b""))

        await app(
            {
                "type": "http",
                "method": method,
                "path": path,
                "headers": [],
                "query_string": b"",
                "scheme": "http",
                "server": ("files.test", 80),
                "client": ("127.0.0.1", 123),
            },
            receive,
            send,
        )
        return status, bytes(chunks)

    return anyio.run(_go)


def _no_server_path(body: bytes, *paths: Path) -> None:
    text = body.decode("utf-8", "replace")
    for path in paths:
        assert str(path) not in text
        assert str(path.resolve()) not in text


def test_sign_hmac_tamper_and_foreign_key(tmp_path: Path) -> None:
    """Ключ из токена, compare_digest отсекает чужой ключ и подмену каждого поля."""
    own = _service(tmp_path / "own", token="one")
    same = _service(tmp_path / "same", token="one")
    foreign = _service(tmp_path / "foreign", token="other")
    random_a = _service(tmp_path / "random-a")
    random_b = _service(tmp_path / "random-b")
    expected = hmac.new(b"one", _KEY_LABEL, hashlib.sha256).digest()
    assert own._file_link_key == expected
    assert same._file_link_key == expected
    assert link_signing_key("one") == expected
    assert foreign._file_link_key != expected
    assert own._file_link_key != b"one"
    assert len(random_a._file_link_key) == 32
    assert random_a._file_link_key != random_b._file_link_key

    path = _write(own, "rules.xml", b"signed")
    token = _route(own.download_link(str(path))["url"]).rsplit("/", 1)[-1]
    assert verify_token(own._file_link_key, token) is not None
    assert verify_token(same._file_link_key, token) is not None
    assert verify_token(foreign._file_link_key, token) is None
    payload = json.loads(_b64decode(token.split(".", 1)[0]))
    replacements = {
        "a": "put",
        "p": payload["p"] + "-x",
        "e": payload["e"] + 5,
        "o": True,
        "m": payload["m"] + 1,
    }
    for key, value in replacements.items():
        assert verify_token(own._file_link_key, _tamper(token, **{key: value})) is None
    body, _signature = token.split(".", 1)
    for garbage in ("", "nodot", "a.b", ".", "..", "@@@.@@@", body + ".YQ", body + "."):
        assert verify_token(own._file_link_key, garbage) is None


def test_settings_public_url_ttl_and_upload_max(tmp_path: Path) -> None:
    """KD2_PUBLIC_URL, срок не больше часа и предел закачки читаются при старте."""
    projects = str(tmp_path / "projects.yaml")
    base = {"KD2_PROJECTS_FILE": projects}
    defaults = Settings.from_env(base)
    assert defaults.public_url is None
    assert defaults.link_ttl == 600
    assert defaults.upload_max_bytes == 64 * 1024 * 1024
    custom = Settings.from_env(
        {
            **base,
            "KD2_PUBLIC_URL": "https://files.example:8443/",
            "KD2_LINK_TTL": "3600",
            "KD2_UPLOAD_MAX_MB": "2",
        }
    )
    assert custom.public_url == "https://files.example:8443"
    assert custom.link_ttl == 3600
    assert custom.upload_max_bytes == 2 * 1024 * 1024
    with pytest.raises(Kd2Error, match="3600"):
        Settings.from_env({**base, "KD2_LINK_TTL": "3601"})
    with pytest.raises(Kd2Error):
        Settings.from_env({**base, "KD2_LINK_TTL": "0"})
    with pytest.raises(Kd2Error):
        Settings.from_env({**base, "KD2_LINK_TTL": "нет"})
    with pytest.raises(Kd2Error):
        Settings.from_env({**base, "KD2_UPLOAD_MAX_MB": "0"})
    with pytest.raises(Kd2Error, match="http"):
        Settings.from_env({**base, "KD2_PUBLIC_URL": "files.example"})


def test_outside_source_refused_and_rules_dir_downloads(tmp_path: Path) -> None:
    """Файл rules_dir скачивается; исходники проекта вне rules_dir — path_outside_workspace."""
    project = tmp_path / "project"
    rules = project / "rules"
    rules.mkdir(parents=True)
    payload = b"rules-bytes"
    expected = hashlib.sha256(payload).hexdigest()
    (rules / "live.xml").write_bytes(payload)
    source = project / "src" / "config.xml"
    source.parent.mkdir()
    source.write_bytes(b"source-bytes")
    agent = r"D:\Agent\project"
    service = _service(
        tmp_path,
        workspace=tmp_path / "ws",
        path_map=PathMap.parse(f"{agent}={project.resolve()}"),
        rules_dirs={"demo": rules},
        project_dirs={"demo": project},
    )
    link = service.download_link(agent + r"\rules\live.xml")
    assert link["sha256"] == expected
    assert link["size"] == len(payload)
    status, headers, body = _http(create_app(service), "GET", _route(link["url"]))
    assert status == 200
    assert body == payload
    assert headers["x-content-sha256"] == expected
    with pytest.raises(WorkspacePathError) as caught:
        service.download_link(agent + r"\src\config.xml")
    error = error_payload(caught.value, service)
    assert error["code"] == "path_outside_workspace"
    assert agent + r"\src\config.xml" in error["message"]
    assert any(item.replace("/", "\\").startswith(agent) for item in error["writable"])


def test_symlink_to_outside_is_refused(tmp_path: Path) -> None:
    """Симлинк внутри рабочей папки на файл снаружи не выдаётся и не скачивается."""
    service = _service(tmp_path)
    outside = tmp_path / "secret.bin"
    secret = b"secret-bytes"
    outside.write_bytes(secret)
    inside = _write(service, "real.bin", b"real-bytes")
    alias = service.workspace.root / "alias.bin"
    try:
        alias.symlink_to(inside)
    except OSError as error:
        pytest.skip(f"ОС не даёт создать символическую ссылку: {error}")
    allowed = service.download_link(str(alias))
    assert allowed["sha256"] == hashlib.sha256(b"real-bytes").hexdigest()
    leaked = service.workspace.root / "leak.bin"
    leaked.symlink_to(outside)
    with pytest.raises(WorkspacePathError):
        service.download_link(str(leaked))
    issued = service.download_link(str(inside))
    inside.unlink()
    inside.symlink_to(outside)
    status, _, body = _http(create_app(service), "GET", _route(issued["url"]))
    assert status == 403
    assert secret not in body
    _no_server_path(body, service.workspace.root, outside)


def test_not_found_missing_file_uses_agent_path(tmp_path: Path) -> None:
    """Нет файла — file_not_found, в тексте путь агента, а не путь сервера."""
    workspace = tmp_path / "ws"
    agent = r"D:\Agent\ws"
    service = _service(
        tmp_path,
        workspace=workspace,
        path_map=PathMap.parse(f"{agent}={workspace.resolve()}"),
    )
    missing = agent + r"\missing.xml"
    with pytest.raises(FileLinkNotFoundError) as caught:
        service.download_link(missing)
    assert error_payload(caught.value, service)["code"] == "file_not_found"
    assert missing in str(caught.value)
    assert str(workspace.resolve()) not in str(caught.value)
    present = _write(service, "gone.xml", b"gone")
    link = service.download_link(str(present))
    present.unlink()
    status, _, body = _http(create_app(service), "GET", _route(link["url"]))
    assert status == 404
    assert json.loads(body) == {"error": "not_found"}
    _no_server_path(body, workspace)


def test_folder_instead_of_file_suggests_rules_pack(tmp_path: Path) -> None:
    """Папка — file_not_found с подсказкой собрать ZIP через rules_pack."""
    service = _service(tmp_path)
    folder = service.workspace.root / "kit"
    folder.mkdir()
    with pytest.raises(FileLinkNotFoundError) as caught:
        service.download_link(str(folder))
    error = error_payload(caught.value, service)
    assert error["code"] == "file_not_found"
    assert "rules_pack" in error["message"]


@pytest.mark.parametrize(
    "name",
    [
        r"..\ПравилаОбмена.xml",
        "папка/файл.xml",
        "папка\\файл.xml",
        "..",
        ".",
        "",
        "CON",
        "con.txt",
        "aux",
        "NUL",
        "COM1",
        "lpt9.dat",
        "a:b.xml",
        "bad\nname.xml",
        "file?.xml",
        "file.xml ",
        "file.xml.",
        "x" * 201,
    ],
)
def test_upload_name_rejected(tmp_path: Path, name: str) -> None:
    """Имя с папкой, `..`, управляющим символом или резервом Windows — invalid_argument."""
    service = _service(tmp_path)
    with pytest.raises(ValueError):
        service.upload_link(name)
    assert not (service.workspace.root / "inbox").exists()
    assert error_payload(ValueError("имя"), service)["code"] == "invalid_argument"


def test_upload_name_accepts_single_segment(tmp_path: Path) -> None:
    service = _service(tmp_path)
    long_name = "а" * 196 + ".xml"
    for name in ("ПравилаОбмена.xml", "file..xml", "my file.xml", long_name):
        issued = service.upload_link(name)
        assert Path(issued["path"]).name == name
    assert (service.workspace.root / "inbox").is_dir()


def test_base_url_public_host_fallback_and_forwarded(tmp_path: Path) -> None:
    """Публичный адрес важнее Host; X-Forwarded-* не читаются; 0.0.0.0 становится петлёй."""
    service = _service(tmp_path)
    path = _write(service, "rules.xml", b"base")
    service.settings.public_url = "https://public.example:9443"
    public = service.download_link(str(path), None, "http://files.example:9090")
    assert public["url"].startswith("https://public.example:9443/files/")
    service.settings.public_url = None
    requested = service.download_link(str(path), None, "http://files.example:9090")
    assert requested["url"].startswith("http://files.example:9090/files/")
    service.settings.host = "0.0.0.0"
    service.settings.port = 8123
    fallback = service.download_link(str(path))
    assert fallback["url"].startswith("http://127.0.0.1:8123/files/")
    headers = {
        "host": "files.example:9090",
        "x-forwarded-host": "evil.example",
        "x-forwarded-proto": "https",
    }
    assert request_base_url(headers, tls=False) == "http://files.example:9090"
    ctx = SimpleNamespace(
        headers=headers,
        request_context=SimpleNamespace(
            request=SimpleNamespace(url=SimpleNamespace(scheme="http"))
        ),
    )
    assert mcp_base_url(ctx) == "http://files.example:9090"
    tls = SimpleNamespace(
        headers={"Host": "files.example:9090", "X-Forwarded-Proto": "http"},
        request_context=SimpleNamespace(
            request=SimpleNamespace(url=SimpleNamespace(scheme="https"))
        ),
    )
    assert mcp_base_url(tls) == "https://files.example:9090"
    tool = create_server(service)._tool_manager.get_tool("download_link")
    assert tool is not None
    from mcp.server.mcpserver import Context

    request = SimpleNamespace(
        headers={"host": "files.example:9090", "x-forwarded-proto": "https"},
        url=SimpleNamespace(scheme="http"),
    )
    context = Context(request_context=cast(Any, SimpleNamespace(request=request)))
    result = anyio.run(tool.run, {"path": str(path)}, context)
    assert result["url"].startswith("http://files.example:9090/files/")


def test_response_download_and_upload_commands(tmp_path: Path) -> None:
    """Ответы инструментов: хеш исходных байтов, срок с Z и команды curl.exe."""
    service = _service(tmp_path)
    payload = b"abc"
    expected = hashlib.sha256(payload).hexdigest()
    path = _write(service, "rules.xml", payload)
    body = service.download_link(str(path))
    assert set(body) == {
        "url",
        "path",
        "file_name",
        "size",
        "sha256",
        "expires_at",
        "command",
    }
    assert body["sha256"] == expected
    assert body["size"] == len(payload)
    assert body["file_name"] == "rules.xml"
    assert body["command"] == {
        "windows": f'curl.exe -fsS -o "rules.xml" "{body["url"]}"',
        "posix": f'curl -fsS -o "rules.xml" "{body["url"]}"',
    }
    stamp = datetime.strptime(body["expires_at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    assert abs(stamp.timestamp() - (time.time() + 600)) < 3
    assert not (service.workspace.root / "inbox").exists()
    uploaded = service.upload_link("in.xml", overwrite=True)
    assert set(uploaded) == {"url", "path", "max_bytes", "expires_at", "overwrite", "command"}
    assert uploaded["overwrite"] is True
    assert uploaded["max_bytes"] == 64 * 1024 * 1024
    assert uploaded["command"] == {
        "windows": f'curl.exe -fsS -T "in.xml" "{uploaded["url"]}"',
        "posix": f'curl -fsS -T "in.xml" "{uploaded["url"]}"',
    }
    assert (service.workspace.root / "inbox" / "in.xml") == Path(uploaded["path"])
    with pytest.raises(ValueError):
        service.download_link(str(path), 0)
    with pytest.raises(ValueError):
        service.upload_link("in.xml", ttl_seconds=3601)


def test_get_route_returns_exact_bytes(tmp_path: Path) -> None:
    """Кириллическое имя и байты с BOM и CRLF отдаются как есть."""
    service = _service(tmp_path)
    name = "ПравилаОбмена.xml"
    payload = b"\xef\xbb\xbf" + "строка\r\nещё\r\n".encode()
    expected = hashlib.sha256(payload).hexdigest()
    path = _write(service, name, payload)
    link = service.download_link(str(path))
    assert link["sha256"] == expected
    status, headers, body = _http(create_app(service), "GET", _route(link["url"]))
    assert status == 200
    assert body == payload
    assert headers["x-content-sha256"] == expected
    assert headers["content-type"].startswith("application/octet-stream")
    disposition = headers["content-disposition"]
    assert disposition == (
        "attachment; filename=\"_____________.xml\"; filename*=UTF-8''" + quote(name, safe="")
    )


def test_get_route_expired_link_is_forbidden(tmp_path: Path) -> None:
    service = _service(tmp_path, token="secret")
    path = _write(service, "rules.xml", b"still-here")
    token = sign_token(
        service._file_link_key,
        {"a": "get", "p": str(path.resolve()), "e": int(time.time()) - 5, "o": False, "m": 10},
    )
    status, _, body = _http(create_app(service, "secret"), "GET", f"/files/{token}")
    assert status == 403
    assert json.loads(body) == {"error": "forbidden"}
    assert b"still-here" not in body
    _no_server_path(body, service.workspace.root)


def test_get_route_forged_fields_foreign_key_and_garbage(tmp_path: Path) -> None:
    """Подмена любого поля, чужой ключ и мусор вместо токена — 403, не 500."""
    service = _service(tmp_path, token="secret")
    payload = b"original"
    path = _write(service, "rules.xml", payload)
    link = service.download_link(str(path))
    token = _route(link["url"]).rsplit("/", 1)[-1]
    signed = json.loads(_b64decode(token.split(".", 1)[0]))
    app = create_app(service, "secret")
    replacements = {
        "a": "put",
        "p": signed["p"] + "-x",
        "e": signed["e"] + 5,
        "o": True,
        "m": signed["m"] + 1,
    }
    for key, value in replacements.items():
        status, _, body = _http(app, "GET", "/files/" + _tamper(token, **{key: value}))
        assert status == 403
        assert json.loads(body) == {"error": "forbidden"}
        assert payload not in body
        _no_server_path(body, service.workspace.root)
    foreign = sign_token(
        link_signing_key("other"),
        {"a": "get", "p": str(path.resolve()), "e": int(time.time()) + 60, "o": False, "m": 8},
    )
    status, _, body = _http(app, "GET", f"/files/{foreign}")
    assert status == 403
    assert payload not in body
    for raw in ("nodot", "a.b", "@@@.@@@", ".", "..", "ab.", ".ab"):
        status, body = _raw_http(app, "GET", f"/files/{raw}")
        assert status == 403, raw
        assert json.loads(body) == {"error": "forbidden"}
    for empty in ("/files", "/files/"):
        status, _, body = _http(app, "GET", empty)
        assert status == 403
        assert json.loads(body) == {"error": "forbidden"}


def test_get_route_changed_after_issue_serves_current_hash(tmp_path: Path) -> None:
    service = _service(tmp_path)
    path = _write(service, "rules.xml", b"one")
    link = service.download_link(str(path))
    assert link["sha256"] == hashlib.sha256(b"one").hexdigest()
    updated = b"two-two"
    path.write_bytes(updated)
    expected = hashlib.sha256(updated).hexdigest()
    status, headers, body = _http(create_app(service), "GET", _route(link["url"]))
    assert status == 200
    assert body == updated
    assert headers["x-content-sha256"] == expected
    assert headers["x-content-sha256"] != link["sha256"]


def test_put_route_stores_bytes_and_json(tmp_path: Path) -> None:
    service = _service(tmp_path)
    payload = b"\xef\xbb\xbfrules\r\n"
    expected = hashlib.sha256(payload).hexdigest()
    link = service.upload_link("ПравилаОбмена.xml")
    status, _, body = _http(create_app(service), "PUT", _route(link["url"]), content=payload)
    assert status == 200
    parsed = json.loads(body)
    assert parsed["size"] == len(payload)
    assert parsed["sha256"] == expected
    assert Path(link["path"]).read_bytes() == payload
    assert parsed["path"] == link["path"]


def test_put_route_then_rules_open(tmp_path: Path) -> None:
    """Сценарий «Закачать правила и открыть»: PUT по ссылке, затем rules_open по пути из ответа."""
    service = _service(tmp_path)
    payload = (Path(__file__).parent / "data" / "exchange_rules.xml").read_bytes()
    link = service.upload_link("ПравилаОбмена.xml")
    status, _, body = _http(create_app(service), "PUT", _route(link["url"]), content=payload)
    assert status == 200
    parsed = json.loads(body)
    assert parsed["sha256"] == hashlib.sha256(payload).hexdigest()
    opened = service.rules_open(parsed["path"])
    assert opened["project_id"]
    service.rules_close(opened["project_id"])


def test_put_route_too_large_content_length_skips_body_and_chunked(tmp_path: Path) -> None:
    """Content-Length больше предела не читает тело; chunked не оставляет временный файл."""
    service = _service(tmp_path, upload_max_bytes=8)
    link = service.upload_link("big.xml")
    app = create_app(service)
    reads = {"n": 0}

    async def spy(scope: dict[str, Any], receive: Any, send: Any) -> None:
        async def counting() -> dict[str, Any]:
            reads["n"] += 1
            return await receive()

        await app(scope, counting, send)

    status, _, body = _http(spy, "PUT", _route(link["url"]), content=b"x" * 20)
    assert status == 413
    assert json.loads(body) == {"error": "payload_too_large"}
    assert reads["n"] == 0
    _no_server_path(body, service.workspace.root)
    inbox = service.workspace.root / "inbox"
    assert not (inbox / "big.xml").exists()

    async def chunks() -> Any:
        yield b"01234"
        yield b"56789"

    status, _, body = _http(app, "PUT", _route(link["url"]), content=chunks())
    assert status == 413
    assert json.loads(body) == {"error": "payload_too_large"}
    assert not (inbox / "big.xml").exists()
    assert [path for path in inbox.iterdir() if ".part" in path.name] == []
    _no_server_path(body, service.workspace.root)


def test_put_route_conflict_leaves_file_and_overwrite_replaces(tmp_path: Path) -> None:
    service = _service(tmp_path)
    kept = b"keep-me"
    first = service.upload_link("a.xml")
    target = Path(first["path"])
    target.write_bytes(kept)
    status, _, body = _http(create_app(service), "PUT", _route(first["url"]), content=b"nope")
    assert status == 409
    assert json.loads(body) == {"error": "conflict"}
    assert target.read_bytes() == kept
    _no_server_path(body, service.workspace.root)
    replaced = b"new-bytes"
    second = service.upload_link("a.xml", overwrite=True)
    status, _, body = _http(create_app(service), "PUT", _route(second["url"]), content=replaced)
    assert status == 200
    assert target.read_bytes() == replaced
    assert json.loads(body)["sha256"] == hashlib.sha256(replaced).hexdigest()

    fresh = service.upload_link("race.xml")
    app = create_app(service)
    url = _route(fresh["url"])

    async def _put(payload: bytes) -> int:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://files.test") as client:
            response = await client.put(url, content=payload)
            return response.status_code

    async def _both() -> tuple[int, int]:
        return await asyncio.gather(_put(b"aaaa"), _put(b"bbbb"))

    codes = tuple(sorted(anyio.run(_both)))
    assert codes == (200, 409)
    assert Path(fresh["path"]).read_bytes() in {b"aaaa", b"bbbb"}


def test_put_route_wrong_action_does_not_change_file(tmp_path: Path) -> None:
    service = _service(tmp_path)
    payload = b"stable"
    path = _write(service, "rules.xml", payload)
    download = service.download_link(str(path))
    app = create_app(service)
    status, _, body = _http(app, "PUT", _route(download["url"]), content=b"changed")
    assert status == 403
    assert json.loads(body) == {"error": "forbidden"}
    assert path.read_bytes() == payload
    _no_server_path(body, service.workspace.root)
    upload = service.upload_link("fresh.xml")
    status, _, body = _http(app, "GET", _route(upload["url"]))
    assert status == 403
    assert not Path(upload["path"]).exists()
    _no_server_path(body, service.workspace.root)


def test_bearer_file_route_without_authorization(tmp_path: Path) -> None:
    """Токен MCP закрывает /mcp и не требуется действующей ссылке на файл."""
    service = _service(tmp_path, token="secret")
    payload = b"open"
    path = _write(service, "rules.xml", payload)
    link = service.download_link(str(path))
    app = create_app(service, "secret")
    status, _, body = _http(app, "GET", _route(link["url"]))
    assert status == 200
    assert body == payload
    status, _, body = _http(
        app, "GET", _route(link["url"]), headers={"Authorization": "Bearer wrong"}
    )
    assert status == 200
    assert body == payload
    status, _, body = _http(app, "POST", "/mcp")
    assert status == 401
    assert json.loads(body) == {"error": "unauthorized"}


def test_log_records_action_without_link(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """В лог попадают действие, имя, статус и размер; ссылка и подпись — нет."""
    caplog.set_level(logging.INFO)
    service = _service(tmp_path)
    payload = b"logged"
    path = _write(service, "rules.xml", payload)
    link = service.download_link(str(path))
    token = _route(link["url"]).rsplit("/", 1)[-1]
    status, _, _body = _http(create_app(service), "GET", _route(link["url"]))
    assert status == 200
    text = "\n".join(
        record.getMessage() for record in caplog.records if record.name == "kd_rules_mcp.files"
    )
    assert token not in text
    assert link["url"] not in text
    assert "action=get" in text
    assert "name=rules.xml" in text
    assert "status=200" in text
    assert f"size={len(payload)}" in text


def test_log_access_log_masks_link_token(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """Журнал доступа uvicorn пишет путь целиком — токен ссылки в нём маскируется.

    Живой прогон 16.11.2026: `docker logs` отдавал действующие ссылки `/files/<токен>`.
    """
    caplog.set_level(logging.INFO)
    service = _service(tmp_path)
    link = service.download_link(str(_write(service, "rules.xml", b"x")))
    token = _route(link["url"]).rsplit("/", 1)[-1]
    create_app(service)
    access = logging.getLogger("uvicorn.access")
    # Форма записи журнала доступа uvicorn: адрес, метод, путь с запросом, версия, статус.
    access.info(
        '%s - "%s %s HTTP/%s" %d', "10.0.0.5:5000", "GET", f"/files/{token}?x=1", "1.1", 200
    )
    access.info(f"GET /files/{token} 403")
    access.info('%s - "%s %s HTTP/%s" %d', "10.0.0.5:5000", "POST", "/mcp", "1.1", 200)
    messages = [record.getMessage() for record in caplog.records if record.name == "uvicorn.access"]
    assert messages == [
        '10.0.0.5:5000 - "GET /files/*** HTTP/1.1" 200',
        "GET /files/*** 403",
        '10.0.0.5:5000 - "POST /mcp HTTP/1.1" 200',
    ]
    create_app(service)
    assert sum(isinstance(item, FileTokenLogFilter) for item in access.filters) == 1


def test_get_route_ascii_filename_fallback(tmp_path: Path) -> None:
    """Рядом с `filename*` — ASCII `filename`: клиенты без RFC 5987 не называют файл токеном."""
    service = _service(tmp_path)
    name = "Правила БП-1.xml"
    link = service.download_link(str(_write(service, name, b"x")))
    status, headers, _ = _http(create_app(service), "GET", _route(link["url"]))
    assert status == 200
    assert headers["content-disposition"] == (
        "attachment; filename=\"_______ __-1.xml\"; filename*=UTF-8''" + quote(name, safe="")
    )
