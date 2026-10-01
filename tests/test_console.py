"""Переключение stdout и stderr скриптов на UTF-8 (`kd2_rules_mcp.console`)."""

import io
import sys

import pytest

from kd2_rules_mcp.console import utf8_stdout

SAMPLE = "→ — ✓"


def test_utf8_stdout_skips_stream_without_reconfigure(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "stdout", io.StringIO())
    monkeypatch.setattr(sys, "stderr", io.StringIO())
    utf8_stdout()


def test_utf8_stdout_reconfigures_cp1251(monkeypatch: pytest.MonkeyPatch) -> None:
    buffer = io.BytesIO()
    stream = io.TextIOWrapper(buffer, encoding="cp1251")
    monkeypatch.setattr(sys, "stdout", stream)
    monkeypatch.setattr(sys, "stderr", io.StringIO())
    utf8_stdout()
    print(SAMPLE)
    stream.flush()
    assert SAMPLE.encode() in buffer.getvalue()
    assert stream.encoding == "utf-8"


def test_cp1251_print_raises_without_utf8_stdout(monkeypatch: pytest.MonkeyPatch) -> None:
    buffer = io.BytesIO()
    stream = io.TextIOWrapper(buffer, encoding="cp1251")
    monkeypatch.setattr(sys, "stdout", stream)
    with pytest.raises(UnicodeEncodeError):
        print("→")
