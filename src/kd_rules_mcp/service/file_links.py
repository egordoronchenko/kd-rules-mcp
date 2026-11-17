"""Короткоживущие подписанные ссылки на один файл (design remote-file-links, Р2–Р7).

Подпись — HMAC-SHA256. Ключ выводится из `KD2_TOKEN`, чтобы сам токен MCP не был ключом;
без токена ключ случайный и живёт до остановки процесса. Маршрут `/files/{token}` не смотрит
на `Authorization`: доступ даёт только подпись.
"""

import base64
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import threading
import time
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote

import anyio
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from kd_rules_mcp.errors import FileLinkNotFoundError, WorkspacePathError
from kd_rules_mcp.service.base import ServiceBase
from kd_rules_mcp.service.paths import Settings

logger = logging.getLogger("kd_rules_mcp.files")

_KEY_LABEL = b"kd-rules-mcp/file-links/v1"
_B64_ALPHABET = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_")
_TOKEN_MAX_CHARS = 65_536
_READ_CHUNK = 64 * 1024
_NAME_MAX = 200
_RESERVED = frozenset(
    {
        "con",
        "prn",
        "aux",
        "nul",
        *(f"com{i}" for i in range(1, 10)),
        *(f"lpt{i}" for i in range(1, 10)),
    }
)
_FORBIDDEN_CHARS = set('<>:"/\\|?*')
_ACTIONS = frozenset({"get", "put"})

_ERROR_FORBIDDEN = "forbidden"
_ERROR_NOT_FOUND = "not_found"
_ERROR_CONFLICT = "conflict"
_ERROR_TOO_LARGE = "payload_too_large"
_ERROR_METHOD = "method_not_allowed"


class _HttpError(Exception):
    """Ответ маршрута: короткий код без путей сервера."""

    def __init__(
        self,
        status: int,
        error: str,
        *,
        action: str = "-",
        name: str = "-",
        size: int | None = None,
    ) -> None:
        super().__init__(error)
        self.status = status
        self.error = error
        self.action = action
        self.name = name
        self.size = size


def link_signing_key(token: str | None) -> bytes:
    """Ключ подписи: `HMAC-SHA256(KD2_TOKEN, метка)` либо 32 случайных байта."""
    if token:
        return hmac.new(token.encode("utf-8"), _KEY_LABEL, hashlib.sha256).digest()
    return secrets.token_bytes(32)


def payload_bytes(payload: Mapping[str, Any]) -> bytes:
    """Канонические байты полезной части: ключи по алфавиту, без лишних пробелов."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
        "utf-8"
    )


def sign_token(key: bytes, payload: Mapping[str, Any]) -> str:
    """`base64url(JSON).base64url(HMAC-SHA256(ключ, байты JSON))` без padding."""
    body = payload_bytes(payload)
    signature = hmac.new(key, body, hashlib.sha256).digest()
    return f"{_b64encode(body)}.{_b64encode(signature)}"


def verify_token(key: bytes, token: str) -> dict[str, Any] | None:
    """Полезная часть при верной подписи и форме либо `None`. Срок здесь не проверяется."""
    if not token or len(token) > _TOKEN_MAX_CHARS or token.count(".") != 1:
        return None
    body_text, signature_text = token.split(".", 1)
    try:
        body = _b64decode(body_text)
        signature = _b64decode(signature_text)
    except ValueError:
        return None
    expected = hmac.new(key, body, hashlib.sha256).digest()
    if len(signature) != len(expected) or not hmac.compare_digest(signature, expected):
        return None
    try:
        payload = json.loads(body)
    except (UnicodeError, json.JSONDecodeError):
        return None
    if not _payload_shape(payload):
        return None
    return payload


def validate_upload_name(name: str) -> str:
    """Одно имя файла. Папки, `..`, управляющие и зарезервированные имена Windows — отказ."""
    if (
        not isinstance(name, str)
        or not name
        or name != name.strip()
        or len(name) > _NAME_MAX
        or name in {".", ".."}
        or name.endswith(".")
        or any(ord(char) < 32 or ord(char) == 127 for char in name)
        or any(char in _FORBIDDEN_CHARS for char in name)
        or "/" in name
        or "\\" in name
        or Path(name).name != name
    ):
        raise ValueError(_name_message(name if isinstance(name, str) else ""))
    stem = name.split(".", 1)[0].rstrip(" .")
    if stem.casefold() in _RESERVED:
        raise ValueError(_name_message(name))
    return name


def request_base_url(headers: Mapping[str, str] | None, *, tls: bool) -> str | None:
    """`http(s)://Host` из запроса. `X-Forwarded-*` не читаются."""
    if not headers:
        return None
    host = _header(headers, "host")
    if host is None:
        return None
    host = host.strip()
    if not host or any(char in host for char in " \t\r\n/\\@"):
        return None
    scheme = "https" if tls else "http"
    return f"{scheme}://{host}"


def mcp_base_url(ctx: Any | None) -> str | None:
    """База из `Context` вызова MCP. Нет запроса или `Host` — `None`, сервис возьмёт настройку."""
    if ctx is None:
        return None
    try:
        headers = ctx.headers
    except (AttributeError, LookupError, RuntimeError, ValueError):
        return None
    if not isinstance(headers, Mapping):
        return None
    return request_base_url(headers, tls=_ctx_is_tls(ctx))


class FileLinksMixin(ServiceBase):
    """`download_link` и `upload_link`: выдача ссылки. Проверка пути повторяется на маршруте."""

    def __init__(self, settings: Settings) -> None:
        super().__init__(settings)
        self._file_link_key = link_signing_key(settings.token)
        self._upload_locks: dict[str, threading.Lock] = {}
        self._upload_locks_guard = threading.Lock()

    def download_link(
        self, path: str, ttl_seconds: int | None = None, base_url: str | None = None
    ) -> dict[str, Any]:
        """Ссылка на скачивание одного файла из рабочей папки или `rules_dir`."""
        ttl = self._ttl(ttl_seconds)
        local = self._download_file(path)
        digest, size = _hash_file(local)
        expiry = int(time.time()) + ttl
        token = sign_token(
            self._file_link_key,
            {"a": "get", "p": str(local), "e": expiry, "o": False, "m": self._upload_limit()},
        )
        url = self._file_url(base_url, token)
        return {
            "url": url,
            "path": self._agent_path(local),
            "file_name": local.name,
            "size": size,
            "sha256": digest,
            "expires_at": _expires_at(expiry),
            "command": _command("get", local.name, url),
        }

    def upload_link(
        self,
        name: str,
        overwrite: bool = False,
        ttl_seconds: int | None = None,
        base_url: str | None = None,
    ) -> dict[str, Any]:
        """Ссылка на закачку одного файла во входящую папку рабочей папки."""
        ttl = self._ttl(ttl_seconds)
        checked = validate_upload_name(name)
        if type(overwrite) is not bool:
            raise ValueError("overwrite должен быть логическим значением")
        target = self._inbox_target(checked)
        expiry = int(time.time()) + ttl
        limit = self._upload_limit()
        token = sign_token(
            self._file_link_key,
            {"a": "put", "p": str(target), "e": expiry, "o": overwrite, "m": limit},
        )
        url = self._file_url(base_url, token)
        return {
            "url": url,
            "path": self._agent_path(target),
            "max_bytes": limit,
            "expires_at": _expires_at(expiry),
            "overwrite": overwrite,
            "command": _command("put", checked, url),
        }

    def _ttl(self, ttl_seconds: int | None) -> int:
        value = self.settings.link_ttl if ttl_seconds is None else ttl_seconds
        if type(value) is not int or not 1 <= value <= 3600:
            raise ValueError("ttl_seconds должен быть целым числом секунд от 1 до 3600")
        return value

    def _upload_limit(self) -> int:
        limit = self.settings.upload_max_bytes
        if type(limit) is not int or limit < 1:
            raise ValueError("Предел закачки не задан")
        return limit

    def _download_file(self, path: str) -> Path:
        if not isinstance(path, str) or not path.strip():
            raise ValueError("Нужен путь к файлу")
        local = self._located(path)
        try:
            resolved = local.resolve(strict=False)
        except OSError as error:
            raise FileLinkNotFoundError(f"Файл не найден: «{path}»") from error
        if not self._under_link_roots(resolved):
            raise self._outside(path)
        if not resolved.exists():
            raise FileLinkNotFoundError(f"Файл не найден: «{path}»")
        if resolved.is_dir():
            raise FileLinkNotFoundError(
                f"«{path}» — папка, а не файл. Соберите ZIP через rules_pack"
            )
        if not resolved.is_file():
            raise FileLinkNotFoundError(f"Файл не найден: «{path}»")
        return resolved

    def _located(self, path: str) -> Path:
        local = self._local(path)
        if local is None:
            raise self._outside(path)
        if not local.is_absolute():
            local = self.workspace.root / local
        return local

    def _outside(self, path: str) -> WorkspacePathError:
        allowed = ", ".join(self.writable_dirs())
        return WorkspacePathError(
            f"Скачивание «{path}» отклонено: путь вне рабочей папки и папок правил проектов. "
            f"Разрешено: {allowed}"
        )

    def _under_link_roots(self, path: Path) -> bool:
        for root in (self.workspace.root, *self.rules_dirs().values()):
            try:
                resolved_root = root.resolve()
            except OSError:
                continue
            if _is_inside(path, resolved_root):
                return True
        return False

    def _inbox_target(self, name: str) -> Path:
        root = self.workspace.root.resolve()
        inbox = self.workspace.root / "inbox"
        inbox.mkdir(parents=True, exist_ok=True)
        resolved_inbox = inbox.resolve()
        target = resolved_inbox / name
        if not _is_inside(resolved_inbox, root) or not _is_inside(target, root):
            raise WorkspacePathError(
                "Закачка отклонена: входящая папка вне рабочей папки. "
                f"Разрешено: {', '.join(self.writable_dirs())}"
            )
        return target

    def _agent_path(self, path: Path) -> str:
        """Путь, который агент может передать в другой инструмент."""
        return self.settings.path_map.to_host(str(path))

    def _file_url(self, base_url: str | None, token: str) -> str:
        return f"{self._link_base(base_url).rstrip('/')}/files/{token}"

    def _link_base(self, base_url: str | None) -> str:
        if self.settings.public_url:
            return self.settings.public_url.rstrip("/")
        if base_url and base_url.strip():
            return base_url.strip().rstrip("/")
        host = "127.0.0.1" if self.settings.host == "0.0.0.0" else self.settings.host
        return f"http://{host}:{self.settings.port}"

    def _upload_lock(self, name: str) -> threading.Lock:
        with self._upload_locks_guard:
            lock = self._upload_locks.get(name)
            if lock is None:
                lock = threading.Lock()
                self._upload_locks[name] = lock
            return lock

    def _resolve_download(self, signed_path: str) -> Path:
        """Файл по подписанному пути. Нет файла — `FileNotFoundError`, вне корней — `None` нет,
        снаружи — `_HttpError` 403."""
        try:
            resolved = Path(signed_path).resolve(strict=False)
        except OSError as error:
            raise FileNotFoundError(signed_path) from error
        if not self._under_link_roots(resolved):
            raise _HttpError(403, _ERROR_FORBIDDEN, action="get", name=_safe_name(signed_path))
        if not resolved.is_file():
            raise FileNotFoundError(signed_path)
        return resolved

    def _resolve_upload(self, signed_path: str) -> Path:
        """Цель закачки: ровно `workspace/inbox/<проверенное имя>`."""
        try:
            name = validate_upload_name(Path(signed_path).name)
        except ValueError as error:
            raise _HttpError(403, _ERROR_FORBIDDEN, action="put") from error
        root = self.workspace.root.resolve()
        inbox = self.workspace.root / "inbox"
        try:
            inbox.mkdir(parents=True, exist_ok=True)
            resolved_inbox = inbox.resolve()
            parent = Path(signed_path).parent.resolve()
        except OSError as error:
            raise _HttpError(403, _ERROR_FORBIDDEN, action="put", name=name) from error
        target = resolved_inbox / name
        if parent != resolved_inbox or not _is_inside(target, root):
            raise _HttpError(403, _ERROR_FORBIDDEN, action="put", name=name)
        return target


_FILES_PATH_TOKEN = re.compile(r"/files/[^\s\"]+")


class FileTokenLogFilter(logging.Filter):
    """Маскирует токен в журнале доступа uvicorn: `/files/<токен>` → `/files/***`.

    Журнал доступа пишет путь запроса целиком, а токен в пути — рабочая ссылка на всё время её
    жизни (найдено живым прогоном: `docker logs` отдавал действующие ссылки).
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.args, tuple):
            record.args = tuple(
                _mask_files_path(arg) if isinstance(arg, str) else arg for arg in record.args
            )
        if isinstance(record.msg, str):
            record.msg = _mask_files_path(record.msg)
        return True


def install_access_log_filter() -> None:
    """Повесить `FileTokenLogFilter` на `uvicorn.access` один раз на процесс."""
    access = logging.getLogger("uvicorn.access")
    if not any(isinstance(item, FileTokenLogFilter) for item in access.filters):
        access.addFilter(FileTokenLogFilter())


def _mask_files_path(text: str) -> str:
    return _FILES_PATH_TOKEN.sub("/files/***", text)


class FilesASGI:
    """ASGI-маршрут `GET`/`PUT /files/{token}` на том же приложении, что и `/mcp`."""

    def __init__(self, app: ASGIApp, service: FileLinksMixin) -> None:
        self._app = app
        self._service = service
        install_access_log_filter()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") == "http" and _is_files_path(scope.get("path", "")):
            await _handle_files(self._service, scope, receive, send)
            return
        await self._app(scope, receive, send)


def _is_files_path(path: object) -> bool:
    return isinstance(path, str) and (path == "/files" or path.startswith("/files/"))


async def _handle_files(
    service: FileLinksMixin, scope: Scope, receive: Receive, send: Send
) -> None:
    started = False

    async def send_once(message: Message) -> None:
        nonlocal started
        if message.get("type") == "http.response.start":
            started = True
        await send(message)

    try:
        await _dispatch(service, scope, receive, send_once)
    except _HttpError as error:
        if started:
            return
        _log(error.action, error.name, error.status, error.size)
        await _json(send, error.status, {"error": error.error})
    except Exception:
        logger.exception("file-link failed")
        if not started:
            await _json(send, 500, {"error": "internal"})


async def _dispatch(
    service: FileLinksMixin,
    scope: Scope,
    receive: Receive,
    send: Send,
) -> None:
    method = str(scope.get("method", ""))
    token = _token_from_path(str(scope.get("path", "")))
    payload = verify_token(service._file_link_key, token)
    if payload is None:
        raise _HttpError(403, _ERROR_FORBIDDEN)
    action = str(payload["a"])
    name = _safe_name(str(payload["p"]))
    if time.time() >= int(payload["e"]):
        raise _HttpError(403, _ERROR_FORBIDDEN, action=action, name=name)
    if method not in {"GET", "PUT"}:
        raise _HttpError(405, _ERROR_METHOD, action=action, name=name)
    if (method == "GET" and action != "get") or (method == "PUT" and action != "put"):
        raise _HttpError(403, _ERROR_FORBIDDEN, action=action, name=name)
    if method == "GET":
        await _dispatch_get(service, str(payload["p"]), name, send)
        return
    await _dispatch_put(service, payload, name, scope, receive, send)


async def _dispatch_get(service: FileLinksMixin, signed_path: str, name: str, send: Send) -> None:
    try:
        target = service._resolve_download(signed_path)
    except FileNotFoundError as error:
        raise _HttpError(404, _ERROR_NOT_FOUND, action="get", name=name) from error
    digest, size = await anyio.to_thread.run_sync(_hash_file, target)
    _log("get", target.name, 200, size)
    # ASCII-имя — для клиентов без `filename*` (например, `curl -OJ`), иначе файл назовут токеном.
    fallback = "".join(
        char if 32 <= ord(char) < 127 and char not in '"\\' else "_" for char in target.name
    )
    disposition = (
        f"attachment; filename=\"{fallback}\"; filename*=UTF-8''{quote(target.name, safe='')}"
    )
    await send(
        {
            "type": "http.response.start",
            "status": 200,
            "headers": [
                (b"content-type", b"application/octet-stream"),
                (b"content-length", str(size).encode("ascii")),
                (b"content-disposition", disposition.encode("ascii")),
                (b"x-content-sha256", digest.encode("ascii")),
            ],
        }
    )
    handle = await anyio.to_thread.run_sync(target.open, "rb")
    try:
        while True:
            chunk = await anyio.to_thread.run_sync(handle.read, _READ_CHUNK)
            if not chunk:
                break
            await send({"type": "http.response.body", "body": chunk, "more_body": True})
    finally:
        await anyio.to_thread.run_sync(handle.close)
    await send({"type": "http.response.body", "body": b""})


async def _dispatch_put(
    service: FileLinksMixin,
    payload: Mapping[str, Any],
    name: str,
    scope: Scope,
    receive: Receive,
    send: Send,
) -> None:
    target = service._resolve_upload(str(payload["p"]))
    limit = int(payload["m"])
    overwrite = payload["o"] is True
    length = _content_length(scope)
    if length is not None and length > limit:
        raise _HttpError(413, _ERROR_TOO_LARGE, action="put", name=name)
    if await anyio.to_thread.run_sync(
        _upload_blocked, target, overwrite, service._upload_lock(name)
    ):
        raise _HttpError(409, _ERROR_CONFLICT, action="put", name=name)
    temp = target.with_name(f".{target.name}.{secrets.token_hex(8)}.part")
    try:
        size, digest = await _write_limited(receive, temp, limit)
        committed = await anyio.to_thread.run_sync(
            _commit_upload, temp, target, overwrite, service._upload_lock(name)
        )
    except _HttpError as error:
        await anyio.to_thread.run_sync(_unlink, temp)
        error.action = "put"
        error.name = name
        raise
    except Exception:
        await anyio.to_thread.run_sync(_unlink, temp)
        raise
    if not committed:
        await anyio.to_thread.run_sync(_unlink, temp)
        raise _HttpError(409, _ERROR_CONFLICT, action="put", name=name)
    body = json.dumps(
        {"path": service._agent_path(target), "size": size, "sha256": digest},
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    _log("put", target.name, 200, size)
    await send(
        {
            "type": "http.response.start",
            "status": 200,
            "headers": [
                (b"content-type", b"application/json; charset=utf-8"),
                (b"content-length", str(len(body)).encode("ascii")),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


def _upload_blocked(target: Path, overwrite: bool, lock: threading.Lock) -> bool:
    with lock:
        return target.exists() and not overwrite


def _commit_upload(temp: Path, target: Path, overwrite: bool, lock: threading.Lock) -> bool:
    with lock:
        if target.exists() and not overwrite:
            return False
        os.replace(temp, target)
        return True


async def _write_limited(receive: Receive, temp: Path, limit: int) -> tuple[int, str]:
    """Поток во временный файл. Предел — обрыв без хвоста на диске."""
    digest = hashlib.sha256()
    size = 0
    handle = await anyio.to_thread.run_sync(temp.open, "wb")
    too_big = False
    finished = False
    try:
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                break
            chunk = bytes(message.get("body", b""))
            if chunk and size + len(chunk) > limit:
                # Остаток тела не дочитываем: иначе клиент может слать данные бесконечно.
                too_big = True
                break
            if chunk:
                await anyio.to_thread.run_sync(handle.write, chunk)
                digest.update(chunk)
                size += len(chunk)
            if not message.get("more_body", False):
                finished = True
                break
    finally:
        await anyio.to_thread.run_sync(handle.close)
    if too_big:
        raise _HttpError(413, _ERROR_TOO_LARGE, action="put")
    if not finished:
        raise _HttpError(400, "incomplete", action="put")
    return size, digest.hexdigest()


def _hash_file(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        while chunk := handle.read(_READ_CHUNK):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def _unlink(path: Path) -> None:
    path.unlink(missing_ok=True)


def _content_length(scope: Scope) -> int | None:
    for name, value in scope.get("headers", []):
        if name == b"content-length":
            try:
                return int(bytes(value).decode("ascii"))
            except (UnicodeError, ValueError):
                return None
    return None


def _token_from_path(path: str) -> str:
    if path == "/files":
        return ""
    prefix = "/files/"
    if not path.startswith(prefix):
        return ""
    return unquote(path[len(prefix) :])


def _safe_name(signed_path: str) -> str:
    name = Path(signed_path).name
    if not name or name in {".", ".."} or any(char in name for char in "/\\"):
        return "-"
    return name


def _log(action: str, name: str, status: int, size: int | None) -> None:
    """Действие, имя файла, статус и размер. Ссылка и подпись в лог не попадают."""
    shown = "-" if size is None else str(size)
    logger.info("file-link action=%s name=%s status=%s size=%s", action, name, status, shown)


async def _json(send: Send, status: int, payload: Mapping[str, str]) -> None:
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"application/json; charset=utf-8"),
                (b"content-length", str(len(body)).encode("ascii")),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


def _payload_shape(payload: object) -> bool:
    if not isinstance(payload, dict):
        return False
    action = payload.get("a")
    path = payload.get("p")
    expiry = payload.get("e")
    overwrite = payload.get("o")
    limit = payload.get("m")
    return (
        action in _ACTIONS
        and isinstance(path, str)
        and path != ""
        and type(expiry) is int
        and type(overwrite) is bool
        and type(limit) is int
        and limit >= 0
    )


def _b64encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64decode(text: str) -> bytes:
    if not text or any(char not in _B64_ALPHABET for char in text):
        raise ValueError("bad base64")
    padding = "=" * (-len(text) % 4)
    # `binascii.Error` — подкласс `ValueError`; его ловит `verify_token`.
    return base64.urlsafe_b64decode(text + padding)


def _header(headers: Mapping[str, str], name: str) -> str | None:
    target = name.casefold()
    for key, value in headers.items():
        if str(key).casefold() == target and isinstance(value, str):
            return value
    return None


def _ctx_is_tls(ctx: Any) -> bool:
    """Схема самого соединения. `X-Forwarded-Proto` не учитывается."""
    try:
        request = ctx.request_context.request
    except (AttributeError, LookupError, RuntimeError, ValueError):
        return False
    url = getattr(request, "url", None)
    scheme = getattr(url, "scheme", None)
    if scheme is None:
        scope = getattr(request, "scope", None)
        if isinstance(scope, Mapping):
            scheme = scope.get("scheme")
    return scheme == "https"


def _expires_at(expiry: int) -> str:
    return datetime.fromtimestamp(expiry, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _command(action: str, name: str, url: str) -> dict[str, str]:
    quoted_name = _shell_quote(name)
    quoted_url = _shell_quote(url)
    if action == "get":
        return {
            "windows": f'curl.exe -fsS -o "{quoted_name}" "{quoted_url}"',
            "posix": f'curl -fsS -o "{quoted_name}" "{quoted_url}"',
        }
    return {
        "windows": f'curl.exe -fsS -T "{quoted_name}" "{quoted_url}"',
        "posix": f'curl -fsS -T "{quoted_name}" "{quoted_url}"',
    }


def _shell_quote(text: str) -> str:
    return text.replace("\\", "\\\\").replace('"', '\\"')


def _name_message(name: str) -> str:
    shown = name if name else "пустое"
    return (
        f"Имя файла «{shown}» недопустимо: нужно одно имя без папок, «..», "
        "двоеточия, управляющих символов и зарезервированных имён Windows, "
        "не длиннее 200 символов"
    )


def _is_inside(path: Path, root: Path) -> bool:
    """Путь лежит внутри корня и не совпадает с самим корнем."""
    folded_path = os.path.normcase(os.path.abspath(path))
    folded_root = os.path.normcase(os.path.abspath(root))
    if folded_path == folded_root:
        return False
    prefix = folded_root.rstrip("\\/") + os.sep
    return folded_path.startswith(prefix)
