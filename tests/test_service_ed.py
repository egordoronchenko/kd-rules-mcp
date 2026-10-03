"""Контракт сервиса ED на синтетических менеджерах, без изменения пакета чтения."""

import hashlib
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from kd2_rules_mcp import ed
from kd2_rules_mcp.errors import (
    AmbiguousAddressError,
    EdFormatError,
    EdReadError,
    EdResourceLimitError,
    ProjectNotFoundError,
    RuleNotFoundError,
)
from kd2_rules_mcp.server import error_payload
from kd2_rules_mcp.service import Kd2Service, PathMap, Settings
from kd2_rules_mcp.service.ed_views import short

DATA = Path(__file__).parent / "data/ed"


@pytest.fixture
def service(tmp_path):
    return Kd2Service(Settings(cache_dir=tmp_path / "cache", workspace=tmp_path / "workspace"))


def opened(service, version=2):
    return service.ed_open(str((DATA / f"manager_v{version}.bsl").resolve()))["project_id"]


def assert_page(value, total=None, offset=0, limit=50):
    assert set(value) == {"items", "total", "offset", "limit", "has_more"}
    assert value["offset"] == offset and value["limit"] == limit
    assert len(value["items"]) <= limit
    assert value["has_more"] == (offset + len(value["items"]) < value["total"])
    if total is not None:
        assert value["total"] == total


@pytest.mark.parametrize("version", [2, 3])
def test_open_overview(service, version):
    path = (DATA / f"manager_v{version}.bsl").resolve()
    result = service.ed_open(str(path))
    assert set(result) == {
        "project_id",
        "kind",
        "source_files",
        "manager_version",
        "parse_status",
        "counts",
        "reused",
        "source_changed",
        "diagnostics_summary",
    }
    digest = hashlib.sha256(str(path).encode()).hexdigest()[:12]
    assert result["project_id"] == f"ed-manager_v{version}-{digest}"
    assert result["kind"] == "ed" and result["reused"] is False
    assert result["source_changed"] is False
    assert result["source_files"] == [
        {
            "file_id": "module",
            "path": str(path),
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "lines": 139 if version == 2 else 53,
        }
    ]
    overview = service.ed_overview(result["project_id"])
    assert set(overview) == {
        "project_id",
        "manager_version",
        "format_versions",
        "counts",
        "coverage",
        "parse_status",
        "diagnostics_summary",
    }
    assert overview["manager_version"] == version
    expected = (
        {
            "pko": 2,
            "pod": 2,
            "pkpd": 1,
            "pks": 4,
            "pktch": 1,
            "search_sets": 1,
            "parameters": 1,
            "algorithms": 1,
            "handlers": 5,
            "dispatchers": 2,
            "support": 5,
            "unknown": 1,
        }
        if version == 2
        else {
            "pko": 1,
            "pod": 0,
            "pkpd": 1,
            "pks": 2,
            "pktch": 1,
            "search_sets": 0,
            "parameters": 0,
            "algorithms": 0,
            "handlers": 1,
            "dispatchers": 0,
            "support": 4,
            "unknown": 0,
        }
    )
    assert overview["counts"] == expected
    coverage = overview["coverage"]
    assert (
        sum(
            coverage[key]
            for key in ("trivia_lines", "opaque_code_lines", "unknown_lines", "declarative_lines")
        )
        == coverage["total_lines"]
    )
    assert overview["parse_status"] == ("partial" if version == 2 else "complete")
    assert overview["diagnostics_summary"] == {"code": {}, "severity": {}}
    versions = overview["format_versions"]
    assert versions["status"] == "mentions_only" and not versions["has_more"]
    assert [v["value"] for v in versions["items"]] == ([] if version == 2 else ["1.20.2"])
    if version == 3:
        item = service.ed_list(result["project_id"], "version")["items"][0]
        assert service.ed_get(result["project_id"], item["address"])["fields"]["value"] == "1.20.2"


def test_list_pages_filters_and_roles(service):
    project = opened(service)
    first = service.ed_list(project, "pks", limit=2)
    assert_page(first, 4, limit=2)
    assert [r["name"] for r in first["items"]] == ["Code", "Extra"]
    second = service.ed_list(project, "pks", offset=2, limit=200)
    assert_page(second, 4, offset=2, limit=200)
    assert [r["name"] for r in second["items"]] == ["Price", "Product"]
    assert_page(service.ed_list(project, "pks", offset=100), 4, offset=100)
    for filters in (
        {"format_object": " catalog.PRODUCT "},
        {"metadata_object": " справочник.ТОВАРЫ "},
        {"format_object": "Catalog.Product", "metadata_object": "Справочник.Товары"},
    ):
        assert service.ed_list(project, "pks", **filters)["total"] == 3
    assert service.ed_list(project, "pks", text="PRODUCT")["total"] == 4
    assert service.ed_list(project, "pks", text="цена")["total"] == 0
    assert service.ed_list(project, "pks", text="/ПКТЧ/")["total"] == 1
    assert service.ed_list(project, "pko", text="руЧнаяВставка")["total"] == 0
    assert (
        service.ed_list(
            project, "pks", format_object="Catalog.Product", metadata_object="Документ.Заказ"
        )["total"]
        == 0
    )
    assert service.ed_list(project, "algorithm", format_object="Catalog.Product")["total"] == 0
    for kind in (
        "pko",
        "pod",
        "pkpd",
        "parameter",
        "algorithm",
        "handler",
        "dispatcher",
        "support",
        "unknown",
        "diagnostic",
    ):
        listed = service.ed_list(project, kind)
        assert_page(listed)
        for item in listed["items"]:
            assert item["kind"] == kind
            assert service.ed_get(project, item["address"])["address"] == item["address"]


def test_get_fields_children_and_text(service):
    project = opened(service)
    result = service.ed_get(project, "пко/товар")
    assert set(result) == {
        "address",
        "kind",
        "fields",
        "span",
        "regions",
        "tags",
        "guards",
        "children",
        "diagnostics",
    }
    assert result["address"] == "ПКО/Товар" and result["kind"] == "pko"
    assert result["fields"]["format_object"] == {"presence": "literal", "value": "Catalog.Product"}
    for key in ("regions", "tags", "guards", "children", "diagnostics"):
        assert_page(result[key])
    pks = service.ed_get(project, "ПКО/Товар", children_kind="pks")
    assert [r["name"] for r in pks["children"]["items"]] == ["Code", "Extra"]
    assert service.ed_get(project, "ПКО/Товар", children_kind="unknown")["children"]["total"] == 1
    dispatcher = service.ed_list(project, "dispatcher")["items"][0]
    cases = service.ed_get(project, dispatcher["address"], children_kind="case")["children"]
    assert cases["total"] > 0
    assert service.ed_get(project, cases["items"][0]["address"])["fields"]["literal_name"]
    group = service.ed_get(project, "ПКО/Товар/ПКТЧ/Prices")
    assert group["fields"]["configuration_property"] == "Цены"
    assert [r["name"] for r in group["children"]["items"]] == ["Price"]
    prop = service.ed_get(project, "ПКО/Товар/ПКТЧ/Prices/ПКС/Price")
    assert prop["fields"]["configuration_property"] == "Цена"
    algorithm = service.ed_get(project, "Алгоритм/ЗавершитьТовар")
    assert "text" not in algorithm
    code = service.ed_get(
        project, "Алгоритм/ЗавершитьТовар", include_text=True, text_offset=4, text_limit=12
    )["text"]
    assert set(code) == {"text", "total_chars", "offset", "limit", "has_more"}
    assert len(code["text"]) == 12 and code["has_more"]
    assert (code["offset"], code["limit"]) == (4, 12)
    assert (
        service.ed_get(project, "Алгоритм/ЗавершитьТовар", include_text=True, text_offset=10000)[
            "text"
        ]["text"]
        == ""
    )
    for kind in ("pod", "pkpd", "parameter", "dispatcher", "support", "unknown"):
        item = service.ed_list(project, kind)["items"][0]
        assert service.ed_get(project, item["address"], include_text=True)["text"]["text"]
    partial = service.ed_get(project, "ПКО/Товар", offset=1, limit=1)
    for key in ("regions", "tags", "guards", "children", "diagnostics"):
        assert_page(partial[key], offset=1, limit=1)
    v3 = opened(service, 3)
    assert service.ed_get(v3, "ПКО/Заказ/ПКС/Number")["fields"]["condition_name"] == "Версия120"
    assert service.ed_get(v3, "ПКО/Заказ/ПКТЧ/Products")["fields"]["condition_name"] == "Версия120"


def test_locate(service):
    project = opened(service)
    rows = service.ed_list(project, "pks")["items"]
    prop = next(r for r in rows if r["name"] == "Price")
    located = service.ed_locate(project, prop["line_start"])
    assert set(located) == {"file_id", "line", "classification", "matches"}
    assert located["classification"] == "declarative"
    assert [r["kind"] for r in located["matches"]["items"]][:3] == ["pks", "pktch", "pko"]
    assert [r["relation"] for r in located["matches"]["items"]][:3] == [
        "innermost",
        "ancestor",
        "ancestor",
    ]
    assert_page(service.ed_locate(project, prop["line_start"], limit=1)["matches"], 3, limit=1)
    comment = service.ed_locate(project, 1)
    assert comment["classification"] == "trivia" and comment["matches"]["items"] == []
    algorithm = service.ed_list(project, "algorithm")["items"][0]
    related = service.ed_locate(project, algorithm["line_start"] + 1)
    assert related["classification"] == "opaque_code"
    assert any(
        r["relation"] == "associated" and r["address"] == "ПКО/Товар"
        for r in related["matches"]["items"]
    )


def test_reuse_close_and_missing_source(service, tmp_path):
    path = tmp_path / "manager.bsl"
    path.write_bytes((DATA / "manager_v2.bsl").read_bytes())
    first = service.ed_open(str(path))
    assert service.ed_open(str(path))["reused"]
    path.write_bytes((DATA / "manager_v3.bsl").read_bytes())
    reused = service.ed_open(str(path))
    assert reused["source_changed"] and reused["manager_version"] == 2
    assert reused["source_files"] == first["source_files"]
    project = first["project_id"]
    assert service.ed_close(project) == {"project_id": project, "closed": True}
    assert service.ed_open(str(path))["manager_version"] == 3
    path.unlink()
    with pytest.raises(EdReadError):
        service.ed_open(str(path))
    assert service.ed_overview(project)["manager_version"] == 3
    assert not list((tmp_path / "workspace").rglob("*"))


def test_ambiguous_address(service, tmp_path):
    path = tmp_path / "conflict.bsl"
    text = (DATA / "manager_v2.bsl").read_text(encoding="utf-8")
    text = text.replace(
        'ДобавитьПКС(СвойстваШапки, "Код", "Code");',
        'ДобавитьПКС(СвойстваШапки, "Код", "Code");\n'
        '    ДобавитьПКС(СвойстваШапки, "ДругойКод", "Code");',
    )
    path.write_text(text, encoding="utf-8")
    project = service.ed_open(str(path))["project_id"]
    with pytest.raises(AmbiguousAddressError) as caught:
        service.ed_get(project, "ПКО/Товар/ПКС/Code", offset=1, limit=1)
    payload = error_payload(caught.value)
    assert payload["code"] == "ambiguous_address"
    assert_page(payload["candidates"], 2, offset=1, limit=1)
    assert payload["candidates"]["items"] == ["ПКО/Товар/ПКС/Code#2"]
    assert (
        service.ed_get(project, "ПКО/Товар/ПКС/Code#2")["fields"]["configuration_property"]
        == "ДругойКод"
    )


@pytest.mark.parametrize(
    "arguments", [{"offset": -1}, {"limit": 0}, {"limit": 201}, {"limit": True}]
)
def test_page_errors(service, arguments):
    project = opened(service)
    for call in (
        lambda: service.ed_list(project, "pko", **arguments),
        lambda: service.ed_get(project, "ПКО/Товар", **arguments),
        lambda: service.ed_locate(project, 1, **arguments),
    ):
        with pytest.raises(ValueError):
            call()


def test_errors(service, tmp_path):
    project = opened(service)
    for call in (
        lambda: service.ed_list(project, "bad"),
        lambda: service.ed_get(project, "ПКО/Товар", children_kind="value"),
        lambda: service.ed_get(project, "ПКО/Товар", text_limit=8001),
        lambda: service.ed_get(project, "ПКО/Товар", text_limit=0),
        lambda: service.ed_get(project, "ПКО/Товар", text_offset=-1),
        lambda: service.ed_locate(project, 0),
        lambda: service.ed_locate(project, 140),
        lambda: service.ed_open(""),
    ):
        with pytest.raises(ValueError):
            call()
    with pytest.raises(RuleNotFoundError):
        service.ed_get(project, "ПКО/Нет")
    for call in (
        lambda: service.ed_overview("missing"),
        lambda: service.ed_list("missing", "pko"),
        lambda: service.ed_get("missing", "Конвертация"),
        lambda: service.ed_locate("missing", 1),
        lambda: service.ed_close("missing"),
    ):
        with pytest.raises(ProjectNotFoundError):
            call()
    for path in (tmp_path / "missing", tmp_path):
        with pytest.raises(EdReadError) as caught:
            service.ed_open(str(path))
        assert error_payload(caught.value)["code"] == "ed_read_error"
    foreign = tmp_path / "foreign.bsl"
    foreign.write_text("Процедура Чужая()\nКонецПроцедуры", encoding="utf-8")
    with pytest.raises(EdFormatError) as caught:
        service.ed_open(str(foreign))
    assert error_payload(caught.value)["code"] == "ed_format"
    for package_error, service_error, code in (
        (ed.EdReadError, EdReadError, "ed_read_error"),
        (ed.EdFormatError, EdFormatError, "ed_format"),
        (ed.EdResourceLimitError, EdResourceLimitError, "ed_resource_limit"),
    ):
        with patch(
            "kd2_rules_mcp.service.ed.ed.read_manager", side_effect=package_error("Сообщение")
        ):
            with pytest.raises(service_error, match="Сообщение") as caught:
                service.ed_open(str(foreign))
            assert error_payload(caught.value)["code"] == code


def test_path_map(tmp_path):
    service = Kd2Service(
        Settings(
            cache_dir=tmp_path / "cache",
            workspace=tmp_path / "workspace",
            path_map=PathMap.parse(f"/agent={DATA.resolve()}"),
        )
    )
    result = service.ed_open("/agent/manager_v2.bsl")
    assert result["source_files"][0]["path"].replace("\\", "/") == "/agent/manager_v2.bsl"


def test_preview_and_version_limits(service):
    assert short("x" * 2000) == "x" * 2000
    assert short("x" * 2001) == {"preview": "x" * 2000, "truncated": True, "total_chars": 2001}
    document = ed.read_manager(DATA / "manager_v3.bsl")
    mention = document.conversion.format_version_mentions[0]
    mentions = tuple(replace(mention, entity_id=f"version-{i}") for i in range(23))
    document = replace(
        document, conversion=replace(document.conversion, format_version_mentions=mentions)
    )
    with patch("kd2_rules_mcp.service.ed.ed.read_manager", return_value=document):
        project = opened(service, 3)
    versions = service.ed_overview(project)["format_versions"]
    assert versions["total"] == 23 and len(versions["items"]) == 20 and versions["has_more"]
    listed = service.ed_list(project, "version", offset=20)
    assert_page(listed, 23, offset=20)
    assert len(listed["items"]) == 3


def test_long_raw_and_diagnostics(service, tmp_path):
    path = tmp_path / "long.bsl"
    text = (DATA / "manager_v2.bsl").read_text(encoding="utf-8")
    text = text.replace(
        'ПравилоКонвертации.ОбъектФормата = "Catalog.Product";',
        'ПравилоКонвертации.ОбъектФормата = "Catalog.Product";\n'
        '    ПравилоКонвертации.ОбъектФормата = "Other";',
    )
    text = text.replace("ВычислитьЗначение();", "ВычислитьЗначение(" + "1," * 2100 + "1);")
    path.write_text(text, encoding="utf-8")
    project = service.ed_open(str(path))["project_id"]
    overview = service.ed_overview(project)
    assert overview["diagnostics_summary"] == {
        "code": {"repeated_assignment": 1},
        "severity": {"warning": 1},
    }
    assert service.ed_list(project, "pko", format_object="Catalog.Product")["total"] == 0
    result = service.ed_get(project, "ПКО/Товар")
    assert result["diagnostics"]["total"] == 1
    assert "raw_text" not in result["tags"]["items"][0]
    fragment = service.ed_list(project, "unknown")["items"][0]
    code = service.ed_get(project, fragment["address"], include_text=True, text_limit=8000)["text"]
    assert code["total_chars"] > 2000 and not code["has_more"]
    diagnostic = service.ed_list(project, "diagnostic")["items"][0]
    assert service.ed_get(project, diagnostic["address"])["fields"]["code"] == "repeated_assignment"


def test_index_built_once_and_atomic_open(service, tmp_path):
    path = tmp_path / "atomic.bsl"
    path.write_bytes((DATA / "manager_v2.bsl").read_bytes())
    from kd2_rules_mcp.ed.address import build_addresses

    with patch(
        "kd2_rules_mcp.service.ed.addresses.build_addresses", wraps=build_addresses
    ) as build:
        project = service.ed_open(str(path))["project_id"]
        service.ed_open(str(path))
        service.ed_get(project, "ПКО/Товар")
        assert build.call_count == 1
    path.write_text("не менеджер", encoding="utf-8")
    assert service.ed_open(str(path))["source_changed"]
    assert service.ed_overview(project)["manager_version"] == 2
    service.ed_close(project)
    with pytest.raises(EdFormatError):
        service.ed_open(str(path))
    with pytest.raises(ProjectNotFoundError):
        service.ed_overview(project)


def test_short_id_collision(service, tmp_path):
    first = tmp_path / "first" / "manager.bsl"
    second = tmp_path / "second" / "manager.bsl"
    for path in (first, second):
        path.parent.mkdir()
        path.write_bytes((DATA / "manager_v2.bsl").read_bytes())
    hashes = {
        str(first.resolve()).encode(): "a" * 12 + "b" * 52,
        str(second.resolve()).encode(): "a" * 12 + "c" * 52,
    }
    real_hash = hashlib.sha256

    def collide(raw=b"", **kwargs):
        if raw in hashes:
            return SimpleNamespace(hexdigest=lambda: hashes[raw])
        return real_hash(raw, **kwargs)

    with patch("kd2_rules_mcp.service.ed.hashlib.sha256", side_effect=collide):
        left = service.ed_open(str(first))
        right = service.ed_open(str(second))
        assert left["project_id"] == "ed-manager-" + "a" * 12
        assert right["project_id"] == "ed-manager-" + "a" * 12 + "c" * 52
        assert service.ed_open(str(second))["project_id"] == right["project_id"]
