"""Одна операция доступа: выполнить код в песочнице и получить текст."""

import http.client
import ipaddress
import json
import os
import re
import ssl
import urllib.request
from dataclasses import dataclass, field
from typing import Protocol
from urllib.parse import parse_qsl, urlsplit, urlunsplit

from kd_rules_mcp.errors import (
    LiveBaseRefusedError,
    LiveEmptyError,
    LiveProtocolError,
    LiveTransportError,
)
from kd_rules_mcp.live.response import short_error
from kd_rules_mcp.projects import (
    Base,
    Catalog,
    LocalSettings,
    ProjectConfigError,
    base_login,
    data_endpoint,
)


class LiveAccess(Protocol):
    """Минимальный контракт адаптера, без знания сценариев и повторов."""

    def execute(self, code: str) -> str: ...


def mapped_url(url: str) -> str:
    """Явная карта заменяет только хост; ошибочная настройка не печатает её значение."""
    raw = os.environ.get("KD2_HOST_MAP", "")
    if not raw:
        return url

    def host(value: str) -> str:
        try:
            return str(ipaddress.ip_address(value.strip("[]")))
        except ValueError:
            if re.fullmatch(r"[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)*", value):
                return value.lower()
            raise ProjectConfigError("неверный формат KD2_HOST_MAP: нужен хост=хост;…") from None

    mapping = {}
    for entry in raw.split(";"):
        source, sep, target = entry.partition("=")
        if not sep:
            raise ProjectConfigError("неверный формат KD2_HOST_MAP: нужен хост=хост;…")
        source, target = host(source), host(target)
        if source in mapping:
            raise ProjectConfigError("повторный хост в KD2_HOST_MAP")
        mapping[source] = target
    parsed = urlsplit(url)
    try:
        source = str(ipaddress.ip_address(parsed.hostname or ""))
    except ValueError:
        source = (parsed.hostname or "").lower()
    target = mapping.get(source)
    if target is None:
        return url
    target = f"[{target}]" if ":" in target else target
    credentials, sep, _ = parsed.netloc.rpartition("@")
    netloc = (credentials + sep if sep else "") + target
    if parsed.port is not None:
        netloc += f":{parsed.port}"
    return urlunsplit(parsed._replace(netloc=netloc))


class _HTTPConnection(http.client.HTTPConnection):
    def connect(self) -> None:
        read_timeout = self.timeout
        self.timeout = 10
        try:
            super().connect()
        finally:
            self.timeout = read_timeout
        if self.sock is not None:
            self.sock.settimeout(read_timeout)


class _HTTPSConnection(http.client.HTTPSConnection):
    def connect(self) -> None:
        read_timeout = self.timeout
        self.timeout = 10
        try:
            super().connect()
        finally:
            self.timeout = read_timeout
        if self.sock is not None:
            self.sock.settimeout(read_timeout)


class _HTTPHandler(urllib.request.HTTPHandler):
    def http_open(self, req):
        return self.do_open(_HTTPConnection, req)


class _HTTPSHandler(urllib.request.HTTPSHandler):
    _context: ssl.SSLContext | None

    def https_open(self, req):
        return self.do_open(_HTTPSConnection, req, context=self._context)


def open_data_request(request: urllib.request.Request, timeout: int):
    """Соединение и TLS — до 10 с, заголовки и тело — с таймаутом чтения."""
    return urllib.request.build_opener(_HTTPHandler(), _HTTPSHandler()).open(
        request, timeout=timeout
    )


@dataclass
class DataMcpAccess:
    """HTTP JSON-RPC/SSE, Basic из разрешения базы; ни одного повтора."""

    url: str = field(repr=False)
    headers: dict[str, str] = field(repr=False)
    tool: str = "vcexecutecode"
    argument: str = "bslcode"
    timeout: int = 900
    secrets: tuple[str, ...] = field(default=(), repr=False)

    def __post_init__(self) -> None:
        # Ошибка настройки должна возникнуть до запуска сценария и его стадий.
        mapped_url(self.url)

    def clean(self, text: str) -> str:
        """Только ошибки: целые значения длиной от четырёх символов, без эха URL/заголовков."""
        text = re.sub(r"https?://[^\s\"'<>]+", "<скрыто>", text)
        text = re.sub(r"(?i)Authorization\s*[:=]\s*[^\r\n]*", "<скрыто>", text)
        url = urlsplit(self.url)
        secrets = (
            self.url,
            url.username or "",
            url.password or "",
            *(value for _, value in parse_qsl(url.query)),
            *self.headers.values(),
            *self.headers.keys(),
            *(value.split(" ", 1)[-1] for value in self.headers.values()),
            *self.secrets,
        )
        for secret in sorted(secrets, key=len, reverse=True):
            if len(secret) >= 4:
                text = re.sub(r"(?<!\w)" + re.escape(secret) + r"(?!\w)", "<скрыто>", text)
        return text

    def execute(self, code: str) -> str:
        url = mapped_url(self.url)
        endpoint = urlsplit(url)
        host = endpoint.hostname or "неизвестный хост"
        host = f"[{host}]" if ":" in host else host
        port = (
            endpoint.port
            if endpoint.port is not None
            else (443 if endpoint.scheme == "https" else 80)
        )
        destination = f"{host}:{port}"
        body = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": self.tool, "arguments": {self.argument: code}},
        }
        try:
            request = urllib.request.Request(
                url,
                json.dumps(body).encode(),
                headers={
                    **self.headers,
                    "Content-Type": "application/json",
                    "Accept": "application/json, text/event-stream",
                },
            )
            with open_data_request(request, timeout=self.timeout) as response:
                raw = response.read().decode("utf-8")
            events: list[str] = []
            if any(line.startswith("data:") for line in raw.splitlines()):
                pending: list[str] = []
                for line in [*raw.splitlines(), ""]:
                    if line.startswith("data:"):
                        pending.append(line[5:].removeprefix(" "))
                    elif not line and pending:
                        events.append("\n".join(pending))
                        pending = []
            else:
                events = [raw]
            answers = [json.loads(event) for event in events]
        except (OSError, ValueError, http.client.HTTPException):
            raise LiveTransportError(
                f"сервер данных {destination} недоступен или ответ оборван (не-JSON)"
            ) from None
        answer = next(
            (item for item in answers if isinstance(item, dict) and item.get("id") == body["id"]),
            None,
        )
        if answer is None:
            raise LiveProtocolError("нет ответа JSON-RPC с id запроса")
        if "error" in answer:
            raise LiveProtocolError(self.clean(short_error(str(answer["error"]))))
        result = answer.get("result")
        if not isinstance(result, dict) or not isinstance(result.get("content"), list):
            raise LiveProtocolError("нет результата инструмента")
        content = result["content"]
        if any(not isinstance(p, dict) or not isinstance(p.get("text", ""), str) for p in content):
            raise LiveProtocolError("неверное содержимое результата")
        text = "".join(p.get("text", "") for p in content)
        if result.get("isError"):
            raise LiveProtocolError(
                self.clean(short_error(text)) or "ошибка инструмента сервера данных"
            )
        if not text.strip():
            raise LiveEmptyError("пустой ответ сервера данных: код не выполнился")
        return text


def resolve_base(catalog: Catalog, local: LocalSettings, ref: str) -> tuple[Base, DataMcpAccess]:
    """Все отказы до создания сетевого вызова; секреты конфигурации не печатаются."""
    project, sep, base_id = ref.partition(".")
    if not sep or not project or not base_id:
        raise LiveBaseRefusedError("база задаётся как <проект>.<база>")
    if project not in catalog.projects:
        raise LiveBaseRefusedError(f"нет проекта {project}")
    if base_id not in catalog.projects[project].bases:
        raise LiveBaseRefusedError(f"в проекте {project} нет базы {base_id}")
    try:
        base = catalog.base(project, base_id)
        if not base.is_sandbox:
            raise LiveBaseRefusedError("база не песочница")
        if not base.data_mcp:
            raise LiveBaseRefusedError("у базы нет способа доступа: не задан data_mcp")
        login = base_login(local, project, base)
        if login is None or not login[0]:
            raise LiveBaseRefusedError("нет логина базы")
        url, headers = data_endpoint(catalog, local, project, base_id)
    except LiveBaseRefusedError:
        raise
    except (ProjectConfigError, OSError, ValueError):
        raise LiveBaseRefusedError(
            "проект/база не найдены или нет доступа к настройкам сервера"
        ) from None
    return base, DataMcpAccess(
        url, headers, base.data_access.tool, base.data_access.argument, secrets=login
    )
