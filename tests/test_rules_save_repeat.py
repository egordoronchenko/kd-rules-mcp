"""Повторяемость rules_save: тот же проект и цикл rules_open → rules_save."""

import re
from datetime import datetime
from pathlib import Path

import pytest

from kd_rules_mcp.authoring import registration, workspace
from kd_rules_mcp.kd2.rules_io import load_rules
from kd_rules_mcp.service import Kd2Service, Settings
from tests.test_rules_io import as_file

DATA = Path(__file__).parent / "data"
_DATE_TAG = "ДатаВремяСоздания".encode()
_DATE = re.compile(b"(<" + _DATE_TAG + b">)[^<]*(</" + _DATE_TAG + b">)")


def _fixed_date(data: bytes) -> bytes:
    """Меняется только значение даты, без разбора и пересериализации XML."""
    normalized, count = _DATE.subn(rb"\g<1>2000-01-01T00:00:00\g<2>", data)
    assert count == 1
    return normalized


class _FutureDatetime(datetime):
    @classmethod
    def now(cls, tz=None):
        return cls(2099, 1, 1, tzinfo=tz)


def _assert_repeatable(
    service: Kd2Service, project_id: str, monkeypatch: pytest.MonkeyPatch
) -> bytes:
    document = service.workspace.get(project_id).document
    assert document is not None
    identifier = document.root.values["Ид"]
    created = document.root.values["ДатаВремяСоздания"]
    # Часы сдвинуты после создания/открытия: сохранение дату заново не назначает.
    monkeypatch.setattr(workspace, "datetime", _FutureDatetime)
    monkeypatch.setattr(registration, "datetime", _FutureDatetime)
    service.rules_save(project_id, "saved.xml", False, True)
    path = service.workspace.root / "saved.xml"
    first = path.read_bytes()
    service.rules_save(project_id, "saved.xml", True, True)
    second = path.read_bytes()
    service.rules_save(project_id, "second.xml", False, True)
    elsewhere = (service.workspace.root / "second.xml").read_bytes()
    reopened = service.rules_open(str(path))["project_id"]
    service.rules_save(reopened, "reopened.xml", False, True)
    cycle = (service.workspace.root / "reopened.xml").read_bytes()
    assert _fixed_date(first) == _fixed_date(second) == _fixed_date(elsewhere) == _fixed_date(cycle)
    # В текущем checkout совпадают и исходные байты: плавающих полей у save нет.
    assert first == second == elsewhere == cycle
    restored = load_rules(cycle)
    assert restored.root.values["Ид"] == identifier
    assert restored.root.values["ДатаВремяСоздания"] == created
    return first


@pytest.mark.parametrize("name", ["exchange_rules.xml", "registration_rules.xml"])
@pytest.mark.parametrize("style", ["lf", "bom-crlf", "mixed"])
def test_opened_rules_save_is_byte_repeatable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str, style: str
) -> None:
    data = (DATA / name).read_bytes()
    if style != "lf":
        data = as_file(data.decode("utf-8"))
        if style == "mixed":
            data = data.replace(b"\r\n", b"\n", 3)
    source = tmp_path / name
    source.write_bytes(data)
    service = Kd2Service(Settings(cache_dir=tmp_path / "cache", workspace=tmp_path / "ws"))
    project_id = service.rules_open(str(source))["project_id"]
    saved = _assert_repeatable(service, project_id, monkeypatch)
    assert _fixed_date(data) == _fixed_date(saved)
    assert data == saved


@pytest.mark.parametrize("kind", ["exchange", "registration"])
def test_generated_rules_save_is_byte_repeatable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    service = Kd2Service(Settings(cache_dir=tmp_path / "cache", workspace=tmp_path / "ws"))
    service.structure_load_xml("source", str(DATA / "xmldump/main"))
    if kind == "exchange":
        service.structure_load_xml("target", str(DATA / "xmldump/ext"))
        project_id = service.rules_create("source", "target")["project_id"]
    else:
        project_id = service.registration_build("source", "Обмен", None, None)["project_id"]
    _assert_repeatable(service, project_id, monkeypatch)
