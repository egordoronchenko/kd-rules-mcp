"""Прогон, протокол стадий и проверенная передача больших значений."""

import base64
import hashlib
import json
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from kd_rules_mcp.errors import LiveError, LiveParseError, LiveStageError, WorkspacePathError
from kd_rules_mcp.live.access import LiveAccess
from kd_rules_mcp.live.codegen import files, rules
from kd_rules_mcp.live.response import parse_response, short_error


def execute(access: LiveAccess, code: str) -> dict[str, Any]:
    return parse_response(access.execute(code))


def preview_hash(tool: str, inputs: dict, state: dict) -> str:
    raw = json.dumps(
        {"tool": tool, "inputs": inputs, "state": state},
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class LiveRun:
    """Все результаты одного вызова — только внутри рабочей папки."""

    def __init__(self, workspace: Path, tool: str, host: Callable[[Path], str]) -> None:
        if not (workspace / ".live").resolve().is_relative_to(workspace.resolve()):
            raise WorkspacePathError("папка .live указывает за пределы рабочей папки")
        self.path = (
            workspace / ".live" / (f"{tool}-{datetime.now(UTC):%Y%m%dT%H%M%S%f}-{uuid4().hex[:8]}")
        )
        self.path.mkdir(parents=True)
        self.host = host
        self.stages: list[dict[str, Any]] = []
        self.failed = False
        self.flush()

    def flush(self) -> None:
        (self.path / "protocol.txt").write_text(
            "\n".join(json.dumps(s, ensure_ascii=False) for s in self.stages) + "\n",
            encoding="utf-8",
            newline="\n",
        )

    def stage(self, label: str, action: Callable[[], Any], *, include_data: bool = False) -> Any:
        if self.failed:
            self.stages.append({"stage": label, "status": "ПРОПУЩЕНА", "duration_ms": 0})
            self.flush()
            return None
        start = time.perf_counter()
        row: dict[str, Any] = {"stage": label, "status": "OK"}
        try:
            result = action()
            if include_data:
                row["data"] = result
            return result
        except Exception as error:
            self.failed = True
            row.update(status="ОШИБКА", message=short_error(str(error)))
            if isinstance(error, LiveError):
                row.update(code=error.code, **error.details)
            return None
        finally:
            row["duration_ms"] = round((time.perf_counter() - start) * 1000)
            self.stages.append(row)
            self.flush()

    def result(self, **data: Any) -> dict[str, Any]:
        self.flush()
        with (self.path / "protocol.txt").open("a", encoding="utf-8", newline="\n") as stream:
            stream.write("ИТОГ " + ("ОШИБКА" if self.failed else "OK") + "\n")
        result = {
            "status": "ОШИБКА" if self.failed else "OK",
            "run_path": self.host(self.path),
            "protocol_path": self.host(self.path / "protocol.txt"),
            "stages": self.stages,
            **data,
        }
        if self.failed:
            raise LiveStageError("стадия остановила сценарий", result)
        return result


def validate_chunk_size(size: int) -> None:
    if isinstance(size, bool) or not 4 <= size <= 96000 or size % 4:
        raise ValueError("размер части: 4..96000 символов base64, кратно четырём")


@contextmanager
def uploaded(
    access: LiveAccess,
    data: bytes,
    run: LiveRun | None = None,
    chunk_size: int = 48000,
    cleanup: list[str] | None = None,
    extension: str = "zip",
) -> Iterator[str]:
    """Даже малый архив передаётся частями, удаление не подменяет основной итог."""
    validate_chunk_size(chunk_size)
    info = execute(access, files.begin_file_code(extension))
    path = info.get("path")
    if not isinstance(path, str) or not path:
        raise LiveParseError("сервер не вернул имя временного файла")
    try:
        payload = base64.b64encode(data).decode("ascii")
        for offset in range(0, len(payload), chunk_size):
            execute(access, files.append_chunk_code(path, payload[offset : offset + chunk_size]))
        received = execute(access, files.file_info_code(path))
        digest = base64.b64encode(hashlib.sha256(data).digest()).decode("ascii")
        if (
            received.get("is_file") is not True
            or type(received.get("size")) is not int
            or received["size"] != len(data)
            or received.get("hash") != digest
        ):
            raise LiveParseError(
                "размер или SHA256 переданного файла не совпал; загрузка запрещена"
            )
        yield path
    finally:
        try:
            execute(access, files.delete_file_code(path))
        except Exception as error:
            message = "УДАЛЕНИЕ ФАЙЛА ОШИБКА " + short_error(str(error))
            if run is not None:
                run.stages.append(
                    {
                        "stage": "УДАЛЕНИЕ ФАЙЛА",
                        "status": "ОШИБКА",
                        "message": message,
                        "duration_ms": 0,
                    }
                )
                run.flush()
            if cleanup is not None:
                cleanup.append(message)


def download_rule(
    access: LiveAccess, plan: str, kind: str, info: dict, chunk_size: int = 48000
) -> bytes:
    return download_bytes(
        access,
        info,
        lambda offset, count, digest: rules.chunk_code(plan, kind, offset, count, digest),
        chunk_size,
    )


def download_bytes(
    access: LiveAccess, info: dict, code: Callable[[int, int, str], str], chunk_size: int = 48000
) -> bytes:
    """Общий проверенный сборщик XML и снимка ресурсов регистра."""
    validate_chunk_size(chunk_size)
    length, size, digest = info.get("length"), info.get("size"), info.get("hash")
    if (
        type(length) is not int
        or type(size) is not int
        or min(length, size) < 0
        or not isinstance(digest, str)
    ):
        raise LiveParseError("некорректный размер или SHA256 правил")
    parts = []
    for offset in range(0, length, chunk_size):
        count = min(chunk_size, length - offset)
        response = execute(access, code(offset, count, digest))
        part = response.get("chunk")
        if not isinstance(part, str) or len(part) != count:
            raise LiveParseError(f"часть {offset}:{offset + count}: обрыв base64")
        if response.get("hash") != digest:
            raise LiveParseError("правила изменились во время чтения")
        parts.append(part)
    try:
        data = base64.b64decode("".join(parts), validate=True)
    except ValueError:
        raise LiveParseError("некорректный base64 правил") from None
    if len(data) != size or base64.b64encode(hashlib.sha256(data).digest()).decode() != digest:
        raise LiveParseError("размер или SHA256 снятых правил не совпал")
    return data
