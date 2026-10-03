"""Контракт сервиса ED на синтетических менеджерах, без изменения пакета чтения."""

import hashlib
import threading
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from kd2_rules_mcp import ed
from kd2_rules_mcp.ed import forms
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


SCHEMA = "Схема формата и структура конфигурации не переданы: проверки по схеме не выполнялись"
REFERENCE_KINDS = {
    "pko_lookup",
    "instruction_rule",
    "pod_use",
    "additional_key",
    "parameter",
    "format_property",
    "received_property",
}


def base_text() -> str:
    return (DATA / "checks_base.bsl").read_text(encoding="utf-8")


def checks_file(tmp_path: Path, text: str | None = None) -> Path:
    source = base_text() if text is None else text
    helpers = "\n".join(forms.helper_forms(name, 2)[0] for name in ("ДобавитьПКС", "ДобавитьПКТЧ"))
    path = tmp_path / "checks.bsl"
    path.write_text(source + "\n" + helpers + "\n", encoding="utf-8", newline="\n")
    return path


def one_issue_text() -> str:
    return base_text().replace(
        "// <pko>",
        'ПравилоКонвертации.ПриОтправкеДанных = "Обработать";',
    )


def two_issues_text() -> str:
    return one_issue_text().replace(
        "// <event>",
        'ОбменДаннымиXDTOСервер.ПКОПоИмени(КомпонентыОбмена, "НетПравила");',
    )


def references_text() -> str:
    long_name = "И" * 180
    return base_text().replace(
        "// <event>",
        'ОбменДаннымиXDTOСервер.ПКОПоИмени(КомпонентыОбмена, "Товар");\n'
        f"    ОбменДаннымиXDTOСервер.ПКОПоИмени(КомпонентыОбмена, {long_name});",
    )


def test_validate_clean_base_and_existing_modules(service, tmp_path):
    project = service.ed_open(str(checks_file(tmp_path)))["project_id"]
    report = service.ed_validate(project)
    assert set(report) == {
        "project_id",
        "summary",
        "skipped",
        "issues",
        "references",
        "profile",
        "coverage",
    }
    assert report["summary"]["errors"] == 0 and report["summary"]["warnings"] == 0
    assert report["skipped"] == [
        {"check": "ed.schema", "reason": SCHEMA},
        {
            "check": "ed.structure",
            "reason": "Структура конфигурации не передана: проверки по структуре не выполнялись",
        },
    ]
    assert report["summary"]["skipped"] == 2
    assert "ed.schema" in report["summary"]["text"]
    assert_page(report["issues"], total=0)
    assert set(report["references"]["unparsed_by_kind"]) == REFERENCE_KINDS
    assert report["references"]["deferred_argument_unparsed"] == 0
    for version in (2, 3):
        opened_id = opened(service, version)
        existing = service.ed_validate(opened_id)
        assert any(item["check"] == "ed.schema" for item in existing["skipped"])
        assert existing["issues"]["total"] == (
            existing["summary"]["errors"] + existing["summary"]["warnings"]
        )


def test_validate_profile_inputs_and_filters(service, tmp_path):
    from tests.test_ed_profile import BASE, document
    from tests.test_ed_profile import DATA as SCHEMA_DATA

    # Только одна профильная проблема, направление и обязательные источники чистые.
    text = BASE.replace("// <properties>", 'ДобавитьПКС(СвойстваШапки, "Код", "Нет", 0);')
    path = tmp_path / "profile.bsl"
    path.write_text(document(text).files[0].text, encoding="utf-8")
    project = service.ed_open(str(path))["project_id"]
    schema = service.ed_schema_open("1.2", path=str(SCHEMA_DATA / "validation.bin"))["schema_id"]
    structure = service.structure_load_md83exp("synthetic", str(SCHEMA_DATA / "structure.xml"))[
        "structure_id"
    ]
    result = service.ed_validate(project, schema_id=schema, structure_id=structure)
    assert result["summary"]["by_check"] == {"ed.schema.property_missing": 1}
    assert result["issues"]["items"] == [
        {
            "level": "предупреждение",
            "check": "ed.schema.property_missing",
            "address": "ПКО/Тест/ПКС/Нет",
            "message": "Свойство формата «Нет» отсутствует в выбранном профиле; "
            "проверьте версию и обработчик.",
        }
    ]
    assert result["profile"] == {
        "schema_id": schema,
        "structure_id": structure,
        "format_version": "1.2",
        "active_namespaces": ["urn:test:validation"],
        "direction": "both",
        "fingerprints": {
            "module": hashlib.sha256(path.read_bytes()).hexdigest(),
            "schema": hashlib.sha256(
                hashlib.sha256((SCHEMA_DATA / "validation.bin").read_bytes())
                .hexdigest()
                .encode("ascii")
            ).hexdigest(),
            "structure": service.store.meta(structure)["input_hash"],
        },
    }
    assert result["coverage"]["checked"] > 0
    filtered = service.ed_validate(
        project, schema_id=schema, structure_id=structure, check_prefix="ed.structure.", limit=1
    )
    assert not filtered["issues"]["items"] and filtered["summary"] == result["summary"]
    assert filtered["coverage"] == result["coverage"] and filtered["skipped"] == result["skipped"]
    schema_only = service.ed_validate(project, schema_id=schema)
    assert {s["check"] for s in schema_only["skipped"]} >= {
        "ed.structure",
        "ed.schema.type_incompatible",
    }
    structure_only = service.ed_validate(project, structure_id=structure)
    assert any(s["check"] == "ed.schema" for s in structure_only["skipped"])
    assert not any(s["check"] == "ed.structure" for s in structure_only["skipped"])
    path.write_text("не читается повторно", encoding="utf-8")
    assert service.ed_validate(project, schema_id=schema, structure_id=structure) == result


def test_validate_profile_input_errors(service, tmp_path):
    from kd2_rules_mcp.errors import EdSchemaNotFoundError, StructureNotFoundError

    project = service.ed_open(str(checks_file(tmp_path)))["project_id"]
    with pytest.raises(EdSchemaNotFoundError):
        service.ed_validate(project, schema_id="missing")
    with pytest.raises(StructureNotFoundError):
        service.ed_validate(project, structure_id="missing")
    with pytest.raises(ValueError):
        service.ed_validate(project, direction="other")


def test_validate_without_schema_keeps_version_condition_unknown(service, tmp_path):
    from tests.test_ed_profile import BASE, document
    from tests.test_ed_profile import DATA as SCHEMA_DATA

    call = "    ДобавитьПКО_Тест(ПравилаКонвертации);"
    text = BASE.replace(
        call,
        'Если КомпонентыОбмена.ВерсияФорматаОбмена = "1.2" Тогда\n' + call + "\nКонецЕсли;",
        1,
    )
    path = tmp_path / "version.bsl"
    path.write_text(document(text).files[0].text, encoding="utf-8")
    project = service.ed_open(str(path))["project_id"]
    structure = service.structure_load_md83exp("synthetic", str(SCHEMA_DATA / "structure.xml"))[
        "structure_id"
    ]
    unknown = service.ed_validate(project, structure_id=structure)
    assert any(
        s["check"] == "ed.structure.property_missing"
        and s["reason"].startswith("opaque_condition:")
        for s in unknown["skipped"]
    )
    assert unknown["coverage"]["opaque_conditions"] > 0
    schema = service.ed_schema_open("1.2", path=str(SCHEMA_DATA / "validation.bin"))["schema_id"]
    known = service.ed_validate(project, schema_id=schema, structure_id=structure)
    assert known["coverage"]["opaque_conditions"] == 0
    assert not [s for s in known["skipped"] if s["check"] == "ed.structure.property_missing"]


def test_validate_caches_structure_input_and_releases_general_lock(service, tmp_path, monkeypatch):
    from kd2_rules_mcp.service import ed as service_ed
    from kd2_rules_mcp.validation.ed_structure_snapshot import StructureSnapshot
    from tests.test_ed_profile import BASE, document
    from tests.test_ed_profile import DATA as SCHEMA_DATA

    path = tmp_path / "profile.bsl"
    path.write_text(document(BASE).files[0].text, encoding="utf-8")
    project = service.ed_open(str(path))["project_id"]
    schema = service.ed_schema_open("1.2", path=str(SCHEMA_DATA / "validation.bin"))["schema_id"]
    structure_path = tmp_path / "structure.xml"
    structure_text = (SCHEMA_DATA / "structure.xml").read_text(encoding="utf-8")
    structure_path.write_text(structure_text, encoding="utf-8")
    structure = service.structure_load_md83exp("synthetic", str(structure_path))["structure_id"]
    loaded = []

    def assert_general_lock_available():
        # RLock в вызывающем потоке дал бы ложное подтверждение отсутствия блокировки.
        acquired = []

        def probe():
            available = service._lock.acquire(timeout=1)
            acquired.append(available)
            if available:
                service._lock.release()

        thread = threading.Thread(target=probe)
        thread.start()
        thread.join(timeout=2)
        assert acquired == [True]

    original_load = StructureSnapshot.load

    def observed_load(connection):
        assert_general_lock_available()
        snapshot = original_load(connection)
        loaded.append(snapshot)
        return snapshot

    monkeypatch.setattr(StructureSnapshot, "load", staticmethod(observed_load))

    def unlocked_validator(original):
        def validate(*args, **kwargs):
            assert_general_lock_available()
            return original(*args, **kwargs)

        return validate

    for name in ("validate_links", "validate_schema", "validate_structure"):
        monkeypatch.setattr(service_ed, name, unlocked_validator(getattr(service_ed, name)))
    first = service.ed_validate(project, schema_id=schema, structure_id=structure)
    assert service.ed_validate(project, schema_id=schema, structure_id=structure) == first
    assert len(loaded) == 1
    structure_path.write_text(structure_text.replace(">Код<", ">ДругойКод<"), encoding="utf-8")
    assert (
        service.structure_load_md83exp("synthetic", str(structure_path))["structure_id"]
        == structure
    )
    changed = service.ed_validate(project, schema_id=schema, structure_id=structure)
    assert len(loaded) == 2 and loaded[0] is not loaded[1]
    assert (
        changed["profile"]["fingerprints"]["structure"]
        != first["profile"]["fingerprints"]["structure"]
    )
    assert service.ed_validate(project, schema_id=schema, structure_id=structure) == changed
    assert len(loaded) == 2


def test_validate_sorts_reference_with_stub_address(service, tmp_path, monkeypatch):
    from kd2_rules_mcp.service import ed as service_ed
    from tests.test_ed_profile import BASE, document

    text = BASE + (
        "\nПроцедура Помощник(КомпонентыОбмена)\n"
        '    ОбменДаннымиXDTOСервер.ПКОПоИмени(КомпонентыОбмена, "НетПравила");\n'
        "КонецПроцедуры\n"
    )
    path = tmp_path / "stub.bsl"
    path.write_text(document(text).files[0].text, encoding="utf-8")
    project = service.ed_open(str(path))["project_id"]
    original_validate = service_ed.validate_links

    def with_stub(*args):
        report = original_validate(*args)
        report.warning(
            "ed.reference.code_rule_missing",
            "Служебный/Помощник",
            "Поиск ПКО ссылается на отсутствующее правило «НетПравила».",
        )
        return report

    monkeypatch.setattr(service_ed, "validate_links", with_stub)
    result = service.ed_validate(project)
    assert result["summary"]["by_check"] == {"ed.reference.code_rule_missing": 1}
    issue = result["issues"]["items"][0]
    assert issue["address"] not in service._ed_projects[project].index.by_address
    assert service.ed_validate(project) == result


def test_validate_filters_pages_and_keeps_full_summary(service, tmp_path):
    project = service.ed_open(str(checks_file(tmp_path, two_issues_text())))["project_id"]
    full = service.ed_validate(project)
    assert full["summary"]["errors"] == 1 and full["summary"]["warnings"] == 1
    assert full["summary"]["by_check"] == {
        "ed.handler.missing": 1,
        "ed.reference.code_rule_missing": 1,
    }
    assert full["skipped"] == [
        {"check": "ed.schema", "reason": SCHEMA},
        {
            "check": "ed.structure",
            "reason": "Структура конфигурации не передана: проверки по структуре не выполнялись",
        },
    ]
    assert_page(full["issues"], total=2)
    errors = service.ed_validate(project, level="ошибка")
    warnings = service.ed_validate(project, level="предупреждение")
    assert errors["summary"] == full["summary"] and warnings["summary"] == full["summary"]
    assert errors["skipped"] == full["skipped"] and warnings["references"] == full["references"]
    assert [item["check"] for item in errors["issues"]["items"]] == ["ed.handler.missing"]
    assert [item["level"] for item in warnings["issues"]["items"]] == ["предупреждение"]
    prefixed = service.ed_validate(project, check_prefix="ed.handler")
    assert prefixed["issues"]["total"] == 1
    assert prefixed["issues"]["items"][0]["check"] == "ed.handler.missing"
    assert prefixed["summary"] == full["summary"]
    assert service.ed_validate(project, check_prefix="")["issues"] == full["issues"]
    unknown = service.ed_validate(project, check_prefix="ed.no.such")
    assert unknown["issues"]["items"] == [] and unknown["issues"]["total"] == 0
    assert unknown["summary"] == full["summary"]
    page = service.ed_validate(project, offset=1, limit=1)
    assert_page(page["issues"], total=2, offset=1, limit=1)
    assert page["summary"]["by_check"] == full["summary"]["by_check"]


@pytest.mark.parametrize("arguments", [{"limit": 0}, {"limit": 201}, {"limit": True}])
def test_validate_page_limits(service, tmp_path, arguments):
    project = service.ed_open(str(checks_file(tmp_path)))["project_id"]
    with pytest.raises(ValueError):
        service.ed_validate(project, **arguments)


def test_validate_unknown_project_and_level(service):
    with pytest.raises(ProjectNotFoundError):
        service.ed_validate("missing")
    project = opened(service)
    with pytest.raises(ValueError):
        service.ed_validate(project, level="error")


def test_validate_repeat_does_not_change_snapshot(service, tmp_path):
    project = service.ed_open(str(checks_file(tmp_path, one_issue_text())))["project_id"]
    stored = service._ed_projects[project]
    overview = service.ed_overview(project)
    first = service.ed_validate(project)
    assert service.ed_overview(project) == overview
    assert service._ed_projects[project].document is stored.document
    assert service._ed_projects[project].index is stored.index
    assert service._ed_projects[project].document.files[0].sha256 == stored.document.files[0].sha256
    cached = service._ed_projects[project].references
    assert cached is not None
    assert service.ed_validate(project) == first
    assert service._ed_projects[project].references is cached
    assert first["summary"]["errors"] == 1 and first["summary"]["warnings"] == 0
    assert first["issues"]["items"][0]["check"] == "ed.handler.missing"
    service.ed_close(project)
    with pytest.raises(ProjectNotFoundError):
        service.ed_validate(project)


def test_get_code_references(service, tmp_path):
    project = opened(service)
    handler = service.ed_get(
        project, "Обработчик/ПКО_Товар_ПриОтправкеДанных", children_kind="reference"
    )
    assert_page(handler["children"], total=1)
    row = handler["children"]["items"][0]
    assert set(row) == {
        "kind",
        "name",
        "form",
        "access",
        "direction",
        "line_start",
        "line_end",
    }
    assert row["kind"] == "format_property" and row["name"] == "Code"
    assert row["form"] == "call" and row["access"] == "write"
    assert row["direction"] in {"send", "receive", "both", None}
    assert "unparsed" not in row and "text" not in handler
    plain = service.ed_get(project, "Обработчик/ПКО_Товар_ПриОтправкеДанных")
    assert all("form" not in item for item in plain["children"]["items"])
    used = service.ed_get(project, "ПОД/Товары", children_kind="used_pko")
    assert used["children"]["items"][0]["kind"] == "used_pko"
    for address in (
        "ПКО/Товар",
        "ПОД/Товары",
        "Диспетчер/ВыполнитьПроцедуруМодуляМенеджера",
        "Служебный/ЗаполнитьПараметрыКонвертации",
    ):
        with pytest.raises(ValueError):
            service.ed_get(project, address, children_kind="reference")
    algorithm = service.ed_get(project, "Алгоритм/ЗавершитьТовар", children_kind="reference")
    assert_page(algorithm["children"], total=0)

    linked = service.ed_open(str(checks_file(tmp_path, references_text())))["project_id"]
    page = service.ed_get(linked, "Событие/ПередКонвертацией", children_kind="reference", limit=1)
    assert_page(page["children"], total=2, limit=1)
    assert page["children"]["items"][0]["name"] == "Товар"
    assert "unparsed" not in page["children"]["items"][0]
    rest = service.ed_get(
        linked, "Событие/ПередКонвертацией", children_kind="reference", offset=1, limit=1
    )
    computed = rest["children"]["items"][0]
    assert computed["name"] is None and computed["unparsed"] is True
    assert len(computed["raw"]) == 160 and computed["raw"] == "И" * 160
    cached = service._ed_projects[linked].references
    service.ed_validate(linked)
    assert service._ed_projects[linked].references is cached
