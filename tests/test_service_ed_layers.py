"""Контракт инструментов слоя на полных синтетических выгрузках."""

import hashlib
import json
from dataclasses import replace
from pathlib import Path
from shutil import copytree

import pytest

from kd2_rules_mcp import ed
from kd2_rules_mcp.ed.schema.profile import ValidationProfile
from kd2_rules_mcp.errors import EdReadError, RuleNotFoundError
from kd2_rules_mcp.server import error_payload
from kd2_rules_mcp.service import Kd2Service, Settings
from kd2_rules_mcp.service import ed as ed_service
from kd2_rules_mcp.service import ed_routes as routes_service
from kd2_rules_mcp.service.ed_views import references_summary, validation_view
from kd2_rules_mcp.validation.ed_links import validate_links
from kd2_rules_mcp.validation.report import Issue
from tests import session_inputs
from tests.test_ed_layers_handler_safety import extension_wrapper, pod_handler
from tests.test_ed_layers_handlers import EXTENSION, SEND_BODY, base_document

ROOT = Path(__file__).parent / "data/ed/layers"
MODULE = "CommonModules/МенеджерДемо/Ext/Module.bsl"


@pytest.fixture
def service(tmp_path, monkeypatch):
    # Общая обвязка сеанса пока не принимает layers; сам производственный читатель принимает.
    assert session_inputs._orig_read_routes is not None
    monkeypatch.setattr(routes_service, "read_routes", session_inputs._orig_read_routes)
    return Kd2Service(Settings(cache_dir=tmp_path / "cache", workspace=tmp_path / "workspace"))


def open_layer(service, name="b", root=ROOT):
    return service.ed_open(
        path=str((root / "base" / MODULE).resolve()),
        configuration_path=str((root / "base").resolve()),
        extensions=[str((root / name).resolve())],
    )


def test_live_layer_handler_name_filter_and_code_alias(service):
    project = open_layer(service)["project_id"]
    rows = service.ed_list(project, "handler")["items"]
    assert len(rows) > 1
    row = rows[0]
    filtered = service.ed_list(project, "handler", name_filter=row["name"])
    assert filtered["total"] == 1
    assert filtered["items"][0]["name"] == row["name"]
    address = "Обработчик/" + row["name"]
    alias = "Код/" + row["name"]
    assert (
        service.ed_get(project, address, include_text=True)["text"]
        == service.ed_get(project, alias, include_text=True)["text"]
    )


def legacy_validate(service, project_id):
    """Оракул прежнего сервиса без профилей; фиксирует весь JSON до добавления слоёв."""
    project = service._ed_project(project_id)
    refs = ed.build_references(project.document)
    report = validate_links(project.document, project.index, refs)
    report.skip(
        "ed.schema",
        "Схема формата и структура конфигурации не переданы: проверки по схеме не выполнялись",
    )
    report.skip(
        "ed.structure",
        "Структура конфигурации не передана: проверки по структуре не выполнялись",
    )
    report.issues = list(dict.fromkeys(report.issues))

    def issue_key(issue: Issue):
        entity = project.index.by_address.get(issue.address)
        return (
            entity.span.file_id if entity else project.document.files[0].file_id,
            entity.span.char_start if entity else len(project.document.files[0].text),
            issue.check,
            issue.message,
        )

    report.issues.sort(key=issue_key)
    report.skipped.sort(key=lambda item: (item.check, item.reason))
    profile = ValidationProfile.build(None, None, "both")
    return {
        "project_id": project_id,
        **validation_view(report, None, None, None, "issues", 0, 50),
        "references": references_summary(refs),
        "profile": {
            "schema_id": None,
            "structure_id": None,
            "format_version": profile.format_version or None,
            "active_namespaces": list(profile.active_namespaces),
            "direction": "both",
            "fingerprints": {
                "module": project.document.files[0].sha256,
                "schema": None,
                "structure": None,
            },
        },
        "coverage": {
            key: 0
            for key in (
                "checked",
                "not_applicable",
                "opaque_conditions",
                "handler_may_supply",
                "unresolved_schema",
            )
        },
    }


def encoded(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def test_open_snapshot_key_compactness_and_overview(service):
    path = str((ROOT / "base" / MODULE).resolve())
    plain = service.ed_open(path)
    opened = open_layer(service)
    ident = opened["project_id"]
    assert ident != plain["project_id"]
    assert not opened["reused"] and not opened["source_changed"]
    assert opened["composition_status"] == "complete"
    assert opened["changes_summary"]["added"] > 0
    assert opened["layers"]["total"] == 2
    assert len(encoded(opened)) < 6000
    assert b"raw_text" not in encoded(opened)
    assert open_layer(service)["reused"]
    overview = service.ed_overview(ident)
    assert len(encoded(overview)) < 6000
    assert overview["composition"]["status"] == "complete"
    assert overview["composition"]["coverage"]["base"]["file_id"] == "module"
    assert overview["composition"]["coverage"]["layers"]["total"] == 1
    assert len({f["file_id"] for f in opened["source_files"]}) == 2
    assert service.ed_close(ident)["closed"]
    assert not open_layer(service)["reused"]


def test_effective_rules_provenance_revision_and_children(service):
    ident = open_layer(service)["project_id"]
    listed = service.ed_list(ident, "pko")
    product = next(r for r in listed["items"] if r["name"] == "Товар")
    assert len(product["contexts"]) == 2
    assert product["state"] == "changed" and product["certainty"] == "known"
    order = next(r for r in listed["items"] if r["name"] == "ДопЗаказ")
    assert order["contexts"] == [{"direction": "send", "headers_only": False}]
    assert service.ed_list(ident, "pko", direction="receive")["total"] == 1
    entity = service.ed_get(ident, "ПКО/Товар")
    assert entity["state"] == "changed"
    assert entity["origins"]["total"] > 1 and entity["changed_fields"]["total"] > 0
    props = service.ed_get(ident, "ПКО/Товар", children_kind="pks")["children"]["items"]
    assert len(props) == 2
    for prop in props:
        assert service.ed_get(ident, prop["address"])["kind"] == "pks"
    base = service.ed_get(ident, "Слой/base/ПКО/Товар", children_kind="pks")
    assert base["state"] == "base" and base["children"]["total"] == 1
    layer = entity["layer_id"]
    revision = service.ed_get(ident, f"Слой/{layer}/ПКО/Товар", include_text=True)
    assert revision["children"]["total"] >= 2
    assert "ДопКод" not in revision["text"]["text"]
    history = service.ed_list(ident, "change", entity_id=entity["logical_id"])
    assert history["total"] == 1
    assert history["items"][0]["state"] == "changed"
    assert service.ed_get(ident, history["items"][0]["address"])["state"] == "changed"
    assert service.ed_list(ident, "pks", metadata_object="Справочник.Товары")["total"] == 2


def test_hooks_locate_files_and_text(service):
    ident = open_layer(service)["project_id"]
    hook = service.ed_list(ident, "hook", text="Доп_ПКО")["items"][0]
    assert "text" not in hook
    result = service.ed_get(ident, hook["address"], include_text=True, text_limit=20)
    assert result["kind"] == "hook" and len(result["text"]["text"]) == 20
    location = service.ed_locate(ident, hook["line_start"], file_id=hook["file_id"])
    assert location["matches"]["items"][0]["address"] == hook["address"]
    assert location["matches"]["items"][0]["kind"] == "hook"
    assert service.ed_locate(ident, 1)["file_id"] == "module"
    assert service.ed_list(ident, "layer", limit=1)["has_more"]


def test_different_fields_require_context(service, tmp_path):
    root = tmp_path / "kits"
    copytree(ROOT, root)
    path = root / "b" / MODULE
    text = path.read_text("utf-8").replace(
        'ДобавитьПКС(Свойства, "ДопКод", "Code");',
        'Если НаправлениеОбмена = "Отправка" Тогда\n'
        'Правило.ОбъектФормата = "Catalog.Changed";\nКонецЕсли;\n'
        'ДобавитьПКС(Свойства, "ДопКод", "Code");',
    )
    path.write_text(text, encoding="utf-8", newline="\n")
    ident = open_layer(service, root=root)["project_id"]
    result = service.ed_get(ident, "ПКО/Товар")
    assert result["requires_context"] and result["variants"]["total"] == 2
    assert (
        service.ed_get(ident, "ПКО/Товар", direction="send")["fields"]["format_object"]["value"]
        == "Catalog.Changed"
    )
    assert (
        service.ed_get(ident, "ПКО/Товар", direction="receive")["fields"]["format_object"]["value"]
        == "Catalog.Product"
    )


def test_validation_union_routes_skips_and_filters(service):
    ident = open_layer(service)["project_id"]
    both = service.ed_validate(ident)
    send = service.ed_validate(ident, direction="send")
    receive = service.ed_validate(ident, direction="receive")
    assert both["composition"]["contexts"] == [
        {"direction": "send", "headers_only": False},
        {"direction": "receive", "headers_only": False},
    ]
    assert {encoded(i) for i in both["issues"]["items"]} == {
        encoded(i) for r in (send, receive) for i in r["issues"]["items"]
    }
    assert "ed.layer.route.single_version" in both["skipped"]["by_check"]
    routes = service.ed_routes(
        path=str((ROOT / "base").resolve()), extensions=[str((ROOT / "b").resolve())]
    )
    checked = service.ed_validate(ident, route_profile_id=routes["profile_id"])
    assert "ed.layer.route.single_version" not in checked["skipped"]["by_check"]
    page = service.ed_validate(ident, section="skipped", check_prefix="ed.layer.", limit=1)
    assert len(page["skipped"]["items"]) == 1 and "issues" not in page
    assert len(encoded(checked)) < 6000


def test_unknown_operations_are_successful_and_explicit(service, tmp_path):
    root = tmp_path / "kits"
    copytree(ROOT, root)
    path = root / "b" / MODULE
    path.write_text(
        path.read_text("utf-8").replace(
            'ДобавитьПКС(Свойства, "ДопКод", "Code");', "НеизвестнаяФункция(ПравилаКонвертации);"
        ),
        encoding="utf-8",
    )
    opened = open_layer(service, root=root)
    assert opened["composition_status"] == "partial"
    ident = opened["project_id"]
    unknown = service.ed_list(ident, "layer_unknown")["items"][0]
    assert "text" not in unknown and "raw" not in unknown
    assert service.ed_get(ident, unknown["address"], include_text=True)["text"]["text"]
    assert service.ed_validate(ident)["skipped"]["by_check"]["ed.layer.reading"] > 0


def test_layer_source_changed_does_not_mutate_snapshot(service, tmp_path):
    root = tmp_path / "kits"
    copytree(ROOT, root)
    first = open_layer(service, root=root)
    path = root / "b" / MODULE
    path.write_bytes(path.read_bytes() + b"\n")
    repeated = open_layer(service, root=root)
    assert repeated["source_changed"] and repeated["reused"]
    assert repeated["source_files"] == first["source_files"]
    service.ed_close(first["project_id"])
    assert not open_layer(service, root=root)["source_changed"]


def test_project_none_empty_and_override(service, tmp_path):
    catalog = tmp_path / "projects.yaml"
    catalog.write_text(
        "projects:\n  demo:\n    name: Демо\n    configurations:\n"
        "      full:\n        dump: base\n        extensions: [b]\n",
        encoding="utf-8",
    )
    service = Kd2Service(
        replace(service.settings, projects_file=catalog, project_dirs={"demo": ROOT.resolve()})
    )
    automatic = service.ed_open(project="demo", module="МенеджерДемо")
    assert automatic["composition_status"] == "complete"
    base = service.ed_open(project="demo", module="МенеджерДемо", extensions=[])
    assert "composition_status" not in base
    assert automatic["project_id"] != base["project_id"]
    override = service.ed_open(
        project="demo", module="МенеджерДемо", extensions=[str((ROOT / "a").resolve())]
    )
    assert override["project_id"] != automatic["project_id"]
    routes = service.ed_routes(project="demo")
    assert routes["extension_policy"] == "ordered_layers"
    assert service.ed_routes(project="demo", extensions=[])["extension_policy"] == "base_only"


@pytest.mark.parametrize(
    "arguments",
    [
        {},
        {"path": "x", "project": "demo"},
        {"path": "x", "module": "x"},
        {"path": "x", "configuration": "other"},
        {"path": "x", "extensions": "x"},
        {"path": "x", "extensions": ["x"]},
    ],
)
def test_open_argument_errors(service, arguments):
    with pytest.raises(ValueError):
        service.ed_open(**arguments)


def test_missing_extension_file_and_borrowed_path_are_errors(service, tmp_path):
    args = {
        "path": str((ROOT / "base" / MODULE).resolve()),
        "configuration_path": str((ROOT / "base").resolve()),
    }
    with pytest.raises(EdReadError):
        service.ed_open(**args, extensions=[str(tmp_path / "missing")])
    root = tmp_path / "kits"
    copytree(ROOT, root)
    (root / "b" / MODULE).unlink()
    with pytest.raises(EdReadError):
        open_layer(service, root=root)
    with pytest.raises(ValueError, match="Заимствованный"):
        service.ed_open(
            path=str((ROOT / "b" / MODULE).resolve()),
            configuration_path=str((ROOT / "base").resolve()),
            extensions=[str((ROOT / "b").resolve())],
        )


def test_authoring_refuses_layer_before_other_inputs(service):
    ident = open_layer(service)["project_id"]
    with pytest.raises(ValueError, match=r"extensions=\[\]") as caught:
        service._inputs_for("demo", "full", (ident, "schema", "structure"))
    assert error_payload(caught.value)["code"] == "invalid_argument"


def test_legacy_validation_bytes_and_plain_open(service):
    path = (ROOT / "base" / MODULE).resolve()
    opened = service.ed_open(str(path))
    ident = opened["project_id"]
    before = legacy_validate(service, ident)
    assert encoded(service.ed_validate(ident)) == encoded(before)
    assert encoded(service.ed_validate(ident, direction=None)) == encoded(before)
    repeated = service.ed_open(str(path), extensions=[])
    assert repeated == {**opened, "reused": True}
    assert opened["source_files"][0]["sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
    with pytest.raises(RuleNotFoundError):
        service.ed_get(ident, "Слой/base/ПКО/Товар")


def test_all_listed_addresses_get_and_layer_sources(service):
    ident = open_layer(service)["project_id"]
    for kind in (
        "pko",
        "pks",
        "pod",
        "pkpd",
        "parameter",
        "handler",
        "support",
        "dispatcher",
        "layer",
        "hook",
    ):
        for row in service.ed_list(ident, kind)["items"]:
            result = service.ed_get(ident, row["address"])
            assert result["address"] == row["address"]
    files = service.ed_get(ident, "Слой/L01-ДемоB")["source_files"]
    assert files["total"] == 1


def test_v3_headers_and_context_errors(service):
    opened = service.ed_open(
        str((ROOT / "v3/base" / MODULE).resolve()),
        configuration_path=str((ROOT / "v3/base").resolve()),
        extensions=[str((ROOT / "v3/ext").resolve())],
    )
    ident = opened["project_id"]
    assert len(opened["contexts"]) == 4
    full = service.ed_list(ident, "pks", direction="send")
    headers = service.ed_list(ident, "pks", direction="send", headers_only=True)
    assert full["total"] > headers["total"]
    checked = service.ed_validate(ident, direction="send", headers_only=True)
    assert checked["composition"]["contexts"] == [{"direction": "send", "headers_only": True}]
    for call in (
        lambda: service.ed_list(ident, "pko", direction="both"),
        lambda: service.ed_get(ident, "ПКО/Товар", headers_only=1),
        lambda: service.ed_validate(ident, direction="other"),
    ):
        with pytest.raises(ValueError):
            call()


def test_deleted_rule_excluded_and_history_accessible(service, tmp_path):
    root = tmp_path / "kits"
    copytree(ROOT, root)
    path = root / "b" / MODULE
    path.write_text(
        '&После("ЗаполнитьПравилаКонвертацииОбъектов")\n'
        "Процедура Доп(НаправлениеОбмена, ПравилаКонвертации)\n"
        'Правило = ПравилаКонвертации.Найти("Товар", "ИмяПКО");\n'
        "Если Правило <> Неопределено Тогда\n"
        "ПравилаКонвертации.Удалить(Правило);\nКонецЕсли;\nКонецПроцедуры\n",
        encoding="utf-8",
    )
    ident = open_layer(service, root=root)["project_id"]
    assert service.ed_list(ident, "pko")["total"] == 0
    with pytest.raises(RuleNotFoundError):
        service.ed_get(ident, "ПКО/Товар")
    deleted = service.ed_list(ident, "change")["items"][0]
    assert deleted["state"] == "deleted"
    assert service.ed_get(ident, deleted["address"])["state"] == "deleted"


def test_public_authoring_tools_refuse_layer(service):
    ident = open_layer(service)["project_id"]
    target = {
        "project": "demo",
        "configuration": "full",
        "plan": "ДемоОбмен",
        "format_version": "1.20",
        "direction": "send",
        "pko_address": "ПКО/Товар",
        "project_id": ident,
        "schema_id": "schema",
        "structure_id": "structure",
    }
    with pytest.raises(ValueError, match=r"extensions=\[\]"):
        service.ed_authoring_candidates(target, "format")
    build_target = {k: v for k, v in target.items() if k not in ("project", "configuration")}
    with pytest.raises(ValueError, match=r"extensions=\[\]"):
        service.ed_authoring_build(
            "demo",
            "full",
            {"name": "Демо", "prefix": "доп_"},
            [
                {
                    "target": build_target,
                    "configuration_attribute": "Код",
                    "format_property": "Code",
                }
            ],
        )


def test_members_have_independent_origins_and_context_union(service):
    ident = open_layer(service)["project_id"]
    rows = service.ed_list(ident, "pks", metadata_object="Справочник.Товары")["items"]
    assert len(rows) == 2 and len({r["address"] for r in rows}) == 2
    base = next(r for r in rows if r["state"] == "base")
    added = next(r for r in rows if r["state"] == "added")
    assert base["layer_id"] == "base" and base["file_id"] == "module"
    assert added["layer_id"] == "L01-ДемоB"
    assert added["file_id"] == "L01-ДемоB:" + MODULE
    assert all(len(r["contexts"]) == 2 for r in rows)
    assert all("configuration_object" not in r and "format_object" not in r for r in rows)
    for row in rows:
        entity = service.ed_get(ident, row["address"])
        assert not entity.get("requires_context")
        assert entity["state"] == row["state"]
        assert entity["origins"]["items"][0]["layer_id"] == row["layer_id"]
    history = service.ed_list(ident, "change", entity_id=added["entity_id"])
    assert history["total"] == 1
    assert history["items"][0]["state"] == "added"
    assert len(history["items"][0]["contexts"]) == 2
    assert service.ed_get(ident, history["items"][0]["address"])["state"] == "added"


def test_extension_binding_dispatcher_and_routine_are_added(service):
    opened = open_layer(service)
    ident = opened["project_id"]
    rule = service.ed_get(ident, "Действующее/ПКО/ДопЗаказ", direction="send")
    binding_row = next(r for r in rule["children"]["items"] if r["kind"] == "binding")
    assert binding_row["address"].startswith("Действующее/")
    binding = service.ed_get(ident, binding_row["address"], direction="send")
    assert binding["layer_id"] == "L01-ДемоB" and binding["state"] == "added"
    assert binding["fields"]["resolution"] == "call"
    assert binding["fields"]["target_id"] is not None
    routine = service.ed_get(ident, "Обработчик/Доп_Заказ_Отправка")
    source = service.ed_get(ident, "Слой/L01-ДемоB/Обработчик/Доп_Заказ_Отправка")
    assert routine["contexts"] == source["contexts"]
    assert source["contexts"] == [{"direction": "send", "headers_only": False}]
    with pytest.raises(RuleNotFoundError, match="receive"):
        service.ed_get(ident, source["address"], direction="receive")
    assert routine["state"] == source["state"] == "added"
    dispatcher = service.ed_get(ident, "Действующее/Диспетчер/ВыполнитьПроцедуруМодуляМенеджера")
    case_row = next(r for r in dispatcher["children"]["items"] if r["name"] == "Доп_Заказ_Отправка")
    assert case_row["address"].startswith("Действующее/")
    case = service.ed_get(ident, case_row["address"])
    assert case["layer_id"] == "L01-ДемоB" and case["state"] == "added"
    assert opened["changes_summary"]["added"] >= 3


def test_base_locate_preserves_plain_addresses_and_relations(service):
    plain = service.ed_open(str((ROOT / "base" / MODULE).resolve()))["project_id"]
    layered = open_layer(service)["project_id"]
    row = service.ed_list(plain, "pks")["items"][0]
    before = service.ed_locate(plain, row["line_start"])["matches"]["items"]
    after = service.ed_locate(layered, row["line_start"])["matches"]["items"]
    assert after == [{**item, "address": "Слой/base/" + item["address"]} for item in before]
    assert {r["relation"] for r in after} == {"innermost", "ancestor"}


def test_handler_changed_field_and_source_declaration(service, tmp_path):
    root = tmp_path / "kits"
    copytree(ROOT, root)
    path = root / "b" / MODULE
    path.write_text(
        path.read_text("utf-8").replace(
            "Свойства = Правило.Свойства;",
            'Правило.ПриОтправкеДанных = "Доп_Заказ_Отправка";\nСвойства = Правило.Свойства;',
        ),
        encoding="utf-8",
    )
    ident = open_layer(service, root=root)["project_id"]
    rule = service.ed_get(ident, "ПКО/Товар", direction="send", include_text=True)
    change = next(c for c in rule["changed_fields"]["items"] if c["path"] == ["ПриОтправкеДанных"])
    assert change["after"]["value"] == "Доп_Заказ_Отправка"
    assert rule["source_declaration"] is True
    assert "Доп_Заказ_Отправка" not in rule["text"]["text"]
    for prefix in ("", "Действующее/", "Слой/base/", "Слой/L01-ДемоB/"):
        entity = service.ed_get(ident, prefix + "ПКО/Товар", direction="send")
        for child in entity["children"]["items"]:
            if "address" in child:
                assert child["address"].startswith(prefix)
                assert (
                    service.ed_get(ident, child["address"], direction="send")["kind"]
                    == child["kind"]
                )


def test_layer_validation_cache_filters_dependencies_and_close(service, monkeypatch):
    calls = []
    original = ed_service.validate_effective_links

    def counted(*args, **kwargs):
        calls.append(args[1])
        return original(*args, **kwargs)

    monkeypatch.setattr(ed_service, "validate_effective_links", counted)
    ident = open_layer(service)["project_id"]
    first = service.ed_validate(ident, direction=None)
    assert len(calls) == 2
    assert service.ed_validate(ident, direction="both") == first
    for filters in (
        {"level": "предупреждение"},
        {"check_prefix": "ed.layer.", "section": "skipped"},
        {"address_prefix": "ПКО/", "offset": 1, "limit": 1},
    ):
        service.ed_validate(ident, **filters)
    assert len(calls) == 2
    first["composition"]["contexts"].clear()
    assert len(service.ed_validate(ident)["composition"]["contexts"]) == 2
    service.ed_validate(ident, direction="send")
    assert len(calls) == 3
    schema = service.ed_schema_open(
        "1.2", path=str(Path(__file__).parent / "data/ed/schema/validation.bin")
    )["schema_id"]
    service.ed_validate(ident, schema_id=schema)
    assert len(calls) == 5
    project = service._ed_project(ident)
    assert any(key[0] == schema for key in project.validation_cache)
    service.ed_schema_close(schema)
    assert not any(key[0] == schema for key in project.validation_cache)
    routes = service.ed_routes(path=str(ROOT / "base"), extensions=[str(ROOT / "b")])["profile_id"]
    service.ed_validate(ident, route_profile_id=routes)
    assert len(calls) == 7
    service._routes.pop(routes)
    assert not any(key[4] == routes for key in project.validation_cache)
    service.ed_close(ident)
    assert ident not in service._ed_projects
    assert not project.validation_cache
    fresh = open_layer(service)["project_id"]
    assert not service._ed_project(fresh).validation_cache


def test_route_profile_requires_same_root_and_ordered_layers(service, tmp_path):
    args = {
        "path": str(ROOT / "base" / MODULE),
        "configuration_path": str(ROOT / "base"),
        "extensions": [str(ROOT / "a"), str(ROOT / "b")],
    }
    ident = service.ed_open(**args)["project_id"]
    for extensions in ([], [str(ROOT / "a")], [str(ROOT / "b"), str(ROOT / "a")]):
        routes = service.ed_routes(path=str(ROOT / "base"), extensions=extensions)["profile_id"]
        with pytest.raises(ValueError, match="упорядоченные расширения отличаются"):
            service.ed_validate(ident, route_profile_id=routes)
    root = tmp_path / "other"
    copytree(ROOT / "base", root)
    routes = service.ed_routes(path=str(root), extensions=args["extensions"])["profile_id"]
    with pytest.raises(ValueError, match="основная выгрузка отличается"):
        service.ed_validate(ident, route_profile_id=routes)
    plain = service.ed_open(args["path"])["project_id"]
    with pytest.raises(ValueError, match="route_profile_id"):
        service.ed_validate(plain, route_profile_id=routes)


def test_unknown_filters_plain_context_and_missing_direction(service):
    ident = open_layer(service)["project_id"]
    with pytest.raises(ValueError, match="Неизвестный слой"):
        service.ed_list(ident, "pko", layer="missing")
    with pytest.raises(RuleNotFoundError, match="missing"):
        service.ed_list(ident, "change", entity_id="missing")
    with pytest.raises(RuleNotFoundError, match="receive"):
        service.ed_get(ident, "ПКО/ДопЗаказ", direction="receive")
    with pytest.raises(ValueError, match="headers_only"):
        service.ed_validate(ident, headers_only=True)
    plain = service.ed_open(str(ROOT / "base" / MODULE))["project_id"]
    for call in (
        lambda: service.ed_list(plain, "pko", direction="send"),
        lambda: service.ed_list(plain, "pko", headers_only=True),
        lambda: service.ed_get(plain, "ПКО/Товар", direction="receive"),
        lambda: service.ed_get(plain, "ПКО/Товар", headers_only=True),
        lambda: service.ed_validate(plain, headers_only=True),
        lambda: service.ed_validate(plain, direction="send"),
        lambda: service.ed_validate(plain, direction="receive"),
    ):
        with pytest.raises(ValueError, match="расширени"):
            call()


def test_duplicate_extensions_and_helpful_errors(service, tmp_path):
    args = {"path": str(ROOT / "base" / MODULE), "configuration_path": str(ROOT / "base")}
    with pytest.raises(ValueError, match="дважды"):
        service.ed_open(**args, extensions=[str(ROOT / "b")] * 2)
    duplicate = tmp_path / "other"
    copytree(ROOT / "b", duplicate)
    with pytest.raises(ValueError, match="имя"):
        service.ed_open(**args, extensions=[str(ROOT / "b"), str(duplicate)])
    with pytest.raises(ValueError, match="Основная выгрузка"):
        service.ed_open(**args, extensions=[str(ROOT / "base")])
    other_base = tmp_path / "other_base"
    copytree(ROOT / "base", other_base)
    with pytest.raises(ValueError, match="Основная выгрузка"):
        service.ed_open(**args, extensions=[str(other_base)])
    with pytest.raises(EdReadError, match=r"[Рр]асширени"):
        service.ed_open(**args, extensions=[str(tmp_path / "missing")])


def test_file_ids_and_addresses_do_not_contain_absolute_paths(service):
    ident = open_layer(service)["project_id"]
    for kind in ("pks", "change", "handler", "hook", "layer_unknown", "layer"):
        result = service.ed_list(ident, kind, limit=200)
        assert str(ROOT.resolve()).encode("utf-8") not in encoded(result)
        assert b"C:" not in encoded(result)
    report = service.ed_validate(ident, section="skipped", limit=200)
    assert str(ROOT.resolve()).encode("utf-8") not in encoded(report)


def test_layer_validation_cache_tracks_structure_reload_and_headers(service, tmp_path, monkeypatch):
    calls = []
    original = ed_service.validate_effective_links

    def counted(*args, **kwargs):
        calls.append(args[1])
        return original(*args, **kwargs)

    monkeypatch.setattr(ed_service, "validate_effective_links", counted)
    ident = service.ed_open(
        path=str(ROOT / "v3/base" / MODULE),
        configuration_path=str(ROOT / "v3/base"),
        extensions=[str(ROOT / "v3/ext")],
    )["project_id"]
    source = Path(__file__).parent / "data/ed/schema/structure.xml"
    path = tmp_path / "structure.xml"
    path.write_bytes(source.read_bytes())
    structure = service.structure_load_md83exp("demo", str(path))["structure_id"]
    first = service.ed_validate(ident, structure_id=structure, direction="send")
    assert service.ed_validate(ident, structure_id=structure, direction="send") == first
    assert len(calls) == 1
    service.ed_validate(ident, structure_id=structure, direction="send", headers_only=True)
    assert len(calls) == 2
    path.write_text(path.read_text("utf-8").replace(">Код<", ">ДругойКод<"), encoding="utf-8")
    assert service.structure_load_md83exp("demo", str(path))["structure_id"] == structure
    changed = service.ed_validate(ident, structure_id=structure, direction="send")
    assert len(calls) == 3
    assert (
        changed["profile"]["fingerprints"]["structure"]
        != first["profile"]["fingerprints"]["structure"]
    )
    assert len(service._ed_project(ident).validation_cache) == 1


def inert_layer_root(tmp_path):
    root = tmp_path / "kits"
    copytree(ROOT, root)
    path = root / "b/Configuration.xml"
    path.write_text(
        path.read_text("utf-8").replace("<CommonModule>МенеджерДемо</CommonModule>", ""),
        encoding="utf-8",
    )
    return root


def test_inert_layer_preserves_algorithm_handler_count(service, tmp_path):
    root = inert_layer_root(tmp_path)
    path = root / "base" / MODULE
    text = path.read_text("utf-8")
    name = "ПКО_Товар_ПриОтправкеДанных"
    text = text.replace(name, "АлгоритмТовара")
    name = "АлгоритмТовара"
    start = text.index("Процедура " + name)
    end = text.index("КонецПроцедуры", start) + len("КонецПроцедуры")
    text = (
        text[:start] + "#Область Алгоритмы\n" + text[start:end] + "\n#КонецОбласти\n" + text[end:]
    )
    path.write_text(text, encoding="utf-8")
    plain = service.ed_open(str(path))
    layered = open_layer(service, root=root)
    assert layered["changes_summary"] == {"added": 0, "changed": 0, "deleted": 0}
    assert all(
        context["counts"]["handlers"] == plain["counts"]["handlers"]
        for context in layered["effective_counts"]
    )


def test_inert_layer_preserves_base_issues_skips_and_order(service, tmp_path):
    root = inert_layer_root(tmp_path)
    path = root / "base" / MODULE
    text = path.read_text("utf-8")
    start = text.index("Процедура ДобавитьПКО_Товар(")
    end = text.index("КонецПроцедуры", start) + len("КонецПроцедуры")
    extra = text[start:end].replace("Товар", "Получение")
    text = text.replace(
        "    ДобавитьПКО_Товар(ПравилаКонвертации);",
        'Если НаправлениеОбмена = "Отправка" Тогда\n'
        "ДобавитьПКО_Товар(ПравилаКонвертации);\nИначе\n"
        "ДобавитьПКО_Получение(ПравилаКонвертации);\nКонецЕсли;",
    )
    text += (
        "\n" + extra + "\nПроцедура СвободныйМетод()\n"
        'Правило = ПравилаКонвертации.Найти("Получение", "ИмяПКО");\nКонецПроцедуры\n'
    )
    path.write_text(text, encoding="utf-8")
    plain = service.ed_open(str(path))["project_id"]
    layered = open_layer(service, root=root)["project_id"]
    before = service.ed_validate(plain, section="skipped", limit=200)
    after = service.ed_validate(layered, section="skipped", limit=200)
    assert [
        s for s in after["skipped"]["items"] if not s["check"].startswith("ed.layer.")
    ] == before["skipped"]["items"]
    assert (
        service.ed_validate(layered, limit=200)["issues"]
        == service.ed_validate(plain, limit=200)["issues"]
    )


def open_handler_input(service, tmp_path, *texts):
    """Полные выгрузки с теми же телами, что проверяет синтетика безопасности читателя."""
    root = tmp_path / "handlers"
    copytree(ROOT, root)
    (root / "base" / MODULE).write_text(base_document().files[0].text, encoding="utf-8")
    extensions = []
    for ordinal, text in enumerate(texts, 1):
        folder = root / f"extension{ordinal}"
        copytree(root / "b", folder)
        configuration = folder / "Configuration.xml"
        configuration.write_text(
            configuration.read_text("utf-8").replace("ДемоB", f"Обработчики{ordinal}"),
            encoding="utf-8",
        )
        (folder / MODULE).write_text(text, encoding="utf-8")
        extensions.append(str(folder))
    return service.ed_open(
        str(root / "base" / MODULE), configuration_path=str(root / "base"), extensions=extensions
    )["project_id"]


def event_view(service, ident, rule, event, direction):
    parent = service.ed_get(ident, rule, direction=direction, children_kind="binding")
    row = next(r for r in parent["children"]["items"] if r["name"] == event)
    return service.ed_get(ident, row["address"], direction=direction)


@pytest.mark.parametrize("event", ["ПриКонвертацииДанныхXDTO", "ПередЗаписьюПолученныхДанных"])
def test_service_binding_into_base_dispatch_preserves_signature_result(service, tmp_path, event):
    text = EXTENSION[: EXTENSION.index('&Вместо("ВыполнитьПроцедуруМодуляМенеджера")')]
    text = text.replace(
        'Правило.ПриКонвертацииДанныхXDTO = "Преобразовать";',
        f'Правило.{event} = "СтарыйПисатель";',
    ).replace('Правило.ПередЗаписьюПолученныхДанных = "СохранитьЗначение";', "")
    text = text.replace('ДругоеПравило.ПередЗаписьюПолученныхДанных = "ПередЗаписью";', "А = 1;")
    text = text.replace('Правило.ПриОтправкеДанных = "Отправить";', "А = 1;")
    ident = open_handler_input(service, tmp_path, text)
    binding = event_view(service, ident, "ПКО/Товар", event, "receive")
    issues = service.ed_validate(ident, direction="receive", limit=200)["issues"]["items"]
    bad = event == "ПриКонвертацииДанныхXDTO"
    assert binding["fields"]["resolution"] == ("invalid_signature" if bad else "call")
    assert bool(binding["fields"]["target_id"]) is (not bad)
    assert bool([i for i in issues if i["check"] == "ed.layer.handler.unreachable"]) is bad
    assert not [i for i in issues if i["check"] == "ed.handler.missing"]
    if bad:
        skips = service.ed_validate(ident, direction="receive", section="skipped", limit=200)
        assert any("Несовместимая сигнатура" in s["reason"] for s in skips["skipped"]["items"])


def test_service_unbound_branch_does_not_lower_bound_paths(service, tmp_path):
    text = (
        EXTENSION.replace(
            "\tИначе\n",
            '\tИначеЕсли ИмяПроцедуры = "Лишняя" Тогда\n'
            "Лишняя(Параметры.КомпонентыОбмена);\n\tИначе\n",
            1,
        )
        + "\nПроцедура Лишняя(Контекст)\nСообщить(1);\nКонецПроцедуры\n"
    )
    ident = open_handler_input(service, tmp_path, text)
    unknown = service.ed_list(ident, "layer_unknown")["items"]
    assert any(
        r["name"] == "event_signature"
        and r["certainty"] == "unknown"
        and "ветка без действующей привязки"
        in service.ed_get(ident, r["address"], include_text=True)["text"]["text"]
        for r in unknown
    )
    binding = event_view(service, ident, "ПКО/Товар", "ПриОтправкеДанных", "send")
    assert binding["fields"]["resolution"] == "call" and binding["certainty"] == "known"
    assert service.ed_get(ident, "ПКО/Товар", direction="receive")["certainty"] == "known"
    assert all(r["name"] != "Лишняя" for r in service.ed_list(ident, "handler")["items"])


def test_service_function_event_cannot_use_procedure_dispatcher(service, tmp_path):
    ident = open_handler_input(
        service, tmp_path, pod_handler("ВыборкаДанных", "КомпонентыОбмена", "Контекст")
    )
    binding = event_view(service, ident, "ПОД/Товары", "ВыборкаДанных", "send")
    assert binding["fields"]["target_id"] is None
    assert binding["fields"]["resolution"] != "call"
    report = service.ed_validate(ident, direction="send", limit=200)
    issue = next(
        i for i in report["issues"]["items"] if i["check"] == "ed.layer.handler.unreachable"
    )
    assert issue["level"] == "ошибка" and "function" in issue["message"]
    assert "ВыборкаДанных" in issue["message"]


@pytest.mark.parametrize("previous,valid", [("СтарыйОтправитель", False), ("ПерваяОбертка", True)])
def test_service_previous_handler_evidence_uses_lower_layer(service, tmp_path, previous, valid):
    ident = open_handler_input(
        service,
        tmp_path,
        extension_wrapper("ПервыйЛитерал", "ПерваяОбертка", None),
        extension_wrapper("ВторойЛитерал", "ВтораяОбертка", previous),
    )
    unknown = service.ed_list(ident, "layer_unknown", limit=200)["items"]
    mismatch = [r for r in unknown if r["name"] == "previous_handler_mismatch"]
    assert bool(mismatch) is (not valid)
    report = service.ed_validate(ident, direction="send", section="skipped", limit=200)
    assert any("previous_handler_mismatch" in s["reason"] for s in report["skipped"]["items"]) is (
        not valid
    )
    if valid:
        for layer, name in ((1, "ПерваяОбертка"), (2, "ВтораяОбертка")):
            routine = service.ed_get(ident, "Обработчик/" + name)
            declaration = service.ed_get(
                ident, f"Слой/L0{layer}-Обработчики{layer}/Обработчик/{name}"
            )
            assert (
                routine["contexts"]
                == declaration["contexts"]
                == [{"direction": "send", "headers_only": False}]
            )


def test_service_handler_hint_preserves_static_certainty(service, tmp_path):
    text = EXTENSION.replace(SEND_BODY, "Помощник(КомпонентыОбмена, ДанныеXDTO);")
    text += (
        "\nПроцедура Помощник(К, ДанныеXDTO)\n"
        "К.ПравилаКонвертацииОбъектов.Очистить();\nКонецПроцедуры\n"
    )
    ident = open_handler_input(service, tmp_path, text)
    assert service.ed_get(ident, "ПКО/Товар", direction="send")["certainty"] == "known"
    assert service.ed_get(ident, "ПКО/Товар", direction="receive")["certainty"] == "known"
    unknown = service.ed_list(ident, "layer_unknown", limit=200)["items"]
    assert not unknown
    report = service.ed_validate(ident, direction="send", check_prefix="ed.layer.handler.")
    assert any(
        i["check"] == "ed.layer.handler.touches_rules" and i["level"] == "предупреждение"
        for i in report["issues"]["items"]
    )
    note = service.ed_overview(ident)["handler_execution_note"]
    assert report["handler_execution_note"] == note


def test_service_typical_handler_body_change_and_unmodeled_hook(service, tmp_path):
    from tests.test_ed_layers_handler_promise import only_hook

    text = only_hook("СтарыйОтправитель", "ДанныеИБ, ДанныеXDTO, КомпонентыОбмена, СтекВыгрузки")
    ident = open_handler_input(service, tmp_path, text)
    binding = event_view(service, ident, "ПКО/Товар", "ПриОтправкеДанных", "send")
    change = binding["fields"]["body_changes"][0]
    assert change["kind"] == "after" and change["target"] == "СтарыйОтправитель"
    assert change["span"]["line_start"] == 1 and change["file_id"].startswith("L01-")
    assert binding["certainty"] == "known" and not service.ed_list(ident, "layer_unknown")["items"]
    function = only_hook(
        "ВыполнитьФункциюМодуляМенеджера",
        "ИмяФункции, Параметры",
        "Возврат Неопределено;",
        function=True,
    )
    ident = open_handler_input(service, tmp_path / "second", text + function)
    unknown = service.ed_list(ident, "layer_unknown")["items"]
    assert len(unknown) == 1 and "ВыполнитьФункциюМодуляМенеджера" in unknown[0]["detail"]
    assert service.ed_get(ident, "ПКО/Товар", direction="send")["certainty"] == "known"


def test_service_base_handler_declaration_uses_effective_contexts(service, tmp_path):
    ident = open_handler_input(service, tmp_path, EXTENSION)
    for name, direction in (("СтарыйОтправитель", "send"), ("СтарыйПисатель", "receive")):
        effective = service.ed_get(ident, "Обработчик/" + name)
        declaration = service.ed_get(ident, "Слой/base/Обработчик/" + name)
        assert (
            effective["contexts"]
            == declaration["contexts"]
            == [{"direction": direction, "headers_only": False}]
        )
    project = service._ed_project(ident)
    assert project.layered is not None
    for view in project.layered.contexts:
        # Счётчик исходных деклараций не возвращает неактивные тела в индекс контекста.
        original = project.document.counts["handlers"]
        own = sum(
            "handler" in r.roles and r.span.file_id != "module" for r in view.document.routines
        )
        assert view.document.counts["handlers"] == original + own


def test_service_declared_other_direction_does_not_restore_deleted_rule(service, tmp_path):
    root = tmp_path / "kits"
    copytree(ROOT, root)
    base = root / "base" / MODULE
    base.write_text(
        base.read_text("utf-8").replace(
            "Процедура ПередКонвертацией(КомпонентыОбмена) Экспорт",
            "Процедура ПередКонвертацией(КомпонентыОбмена) Экспорт\n"
            'ОбменДаннымиXDTOСервер.ПКОПоИмени(КомпонентыОбмена, "Товар");',
        ),
        encoding="utf-8",
    )
    (root / "b" / MODULE).write_text(
        """&После("ЗаполнитьПравилаКонвертацииОбъектов")
Процедура Удалить(НаправлениеОбмена, ПравилаКонвертации)
Если НаправлениеОбмена = "Отправка" Тогда
П = ПравилаКонвертации.Найти("Товар", "ИмяПКО");
Если П <> Неопределено Тогда
ПравилаКонвертации.Удалить(П);
КонецЕсли;
КонецЕсли;
КонецПроцедуры
""",
        encoding="utf-8",
    )
    ident = open_layer(service, root=root)["project_id"]
    sent = service.ed_validate(ident, direction="send", limit=200)["issues"]["items"]
    received = service.ed_validate(ident, direction="receive", limit=200)["issues"]["items"]
    assert any(
        i["check"] == "ed.reference.code_rule_missing" and "Товар" in i["message"] for i in sent
    )
    assert not any(i["check"] == "ed.reference.code_rule_missing" for i in received)
