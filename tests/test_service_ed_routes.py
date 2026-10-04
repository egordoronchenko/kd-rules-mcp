"""Контракт сервиса маршрутов: страницы, ошибки, кэш менеджера и аргументы перехода."""

import shutil
from dataclasses import replace
from pathlib import Path

import pytest

from kd2_rules_mcp.ed import routes as routes_module
from kd2_rules_mcp.ed.schema.model import EdSchema
from kd2_rules_mcp.errors import (
    EdRouteFormatError,
    EdRouteProfileNotFoundError,
    EdRouteReadError,
    EdRouteResourceLimitError,
    EdSchemaFormatError,
    EdSchemaResourceLimitError,
    Kd2Error,
)
from kd2_rules_mcp.server import error_payload
from kd2_rules_mcp.service import Kd2Service, Settings
from kd2_rules_mcp.service import ed_routes as routes_service
from kd2_rules_mcp.validation.ed_routes import SchemaUnavailable

DATA = Path(__file__).parent / "data" / "ed" / "routes"
GRAMMAR = DATA / "grammar"
MANAGER = Path(__file__).parent / "data" / "ed" / "manager_v2.bsl"
FORMAT_URI = "urn:test/1.2"
MESSAGE_URI = "http://www.1c.ru/SSL/Exchange/Message"


@pytest.fixture
def service(tmp_path):
    return Kd2Service(Settings(cache_dir=tmp_path / "cache", workspace=tmp_path / "workspace"))


def _page_keys(payload: dict) -> None:
    assert set(payload) >= {"items", "total", "offset", "limit", "has_more"}
    assert payload["has_more"] == (payload["offset"] + len(payload["items"]) < payload["total"])


def test_opened_manager_cache_keeps_route_profiles_and_tool_responses(
    service, tmp_path, monkeypatch
):
    root = tmp_path / "dump"
    make_dump(root)
    expected = Kd2Service(
        Settings(cache_dir=tmp_path / "expected-cache", workspace=tmp_path / "expected-workspace")
    )
    ordinary = expected.ed_routes(path=str(root))
    path = root / "CommonModules/МенеджерОбменаПример/Ext/Module.bsl"
    opened = service.ed_open(str(path))
    document = service._ed_project(opened["project_id"]).document

    def forbidden_manager(*args, **kwargs):
        raise AssertionError("Открытый документ менеджера не должен разбираться повторно")

    monkeypatch.setattr(routes_module, "read_manager", forbidden_manager)
    snap, reused, stale = service._open_snapshot(
        root, None, None, False, documents={path: document}, read_files_only=True
    )
    assert not reused and not stale
    assert snap.profile == expected._require_route(ordinary["profile_id"]).profile
    assert snap.file_hashes[path] == document.files[0].sha256
    for section in ("summary", "plans", "versions", "variants", "packages", "skipped"):
        assert service.ed_routes(
            profile_id=snap.profile.profile_id, section=section
        ) == expected.ed_routes(profile_id=snap.profile.profile_id, section=section)
    # Переход к обычному инструменту сохраняет его инвентарную проверку свежести.
    assert service.ed_routes(path=str(root)) == expected.ed_routes(path=str(root))


def _xml(name: str, namespace: str) -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<MetaDataObject xmlns="http://v8.1c.ru/8.3/MDClasses" '
        'xmlns:v8="http://v8.1c.ru/8.1/data/core">\n'
        '  <XDTOPackage uuid="00000000-0000-0000-0000-000000000099">\n'
        "    <Properties>\n"
        f"      <Name>{name}</Name>\n"
        "      <Synonym><v8:item><v8:lang>ru</v8:lang>"
        "<v8:content>1.2</v8:content></v8:item></Synonym>\n"
        f"      <Namespace>{namespace}</Namespace>\n"
        "    </Properties>\n"
        "  </XDTOPackage>\n"
        "</MetaDataObject>\n"
    )


def _bin(namespace: str) -> str:
    return (
        '<package xmlns="http://v8.1c.ru/8.1/xdto" '
        'xmlns:xs="http://www.w3.org/2001/XMLSchema" '
        f'targetNamespace="{namespace}">\n'
        '  <objectType name="Item"/>\n'
        "</package>\n"
    )


def make_dump(root: Path, *, broken: bool = False, ambiguous: bool = False) -> None:
    """Маленькая вымышленная выгрузка: один план, менеджер и пакет выбранной версии."""
    packages = root / "XDTOPackages"
    (packages / "Пакет" / "Ext").mkdir(parents=True)
    (packages / "Сообщение" / "Ext").mkdir(parents=True)
    (root / "ExchangePlans" / "План" / "Ext").mkdir(parents=True)
    (root / "CommonModules" / "МенеджерОбменаПример" / "Ext").mkdir(parents=True)
    names = ["План", "МенеджерОбменаПример", "Пакет", "Сообщение"]
    if ambiguous:
        (packages / "Копия" / "Ext").mkdir(parents=True)
        names.append("Копия")
    children = "\n".join(
        f"      <{kind}>{name}</{kind}>"
        for kind, name in (
            [("ExchangePlan", "План")]
            + [("CommonModule", "МенеджерОбменаПример")]
            + [
                ("XDTOPackage", item)
                for item in names
                if item not in ("План", "МенеджерОбменаПример")
            ]
        )
    )
    (root / "Configuration.xml").write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<MetaDataObject xmlns="http://v8.1c.ru/8.3/MDClasses">\n'
        '  <Configuration uuid="00000000-0000-0000-0000-000000000001">\n'
        "    <Properties><Name>Вымышленная</Name></Properties>\n"
        "    <ChildObjects>\n"
        f"{children}\n"
        "    </ChildObjects>\n"
        "  </Configuration>\n"
        "</MetaDataObject>\n",
        encoding="utf-8",
        newline="\n",
    )
    (root / "ExchangePlans" / "План.xml").write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<MetaDataObject xmlns="http://v8.1c.ru/8.3/MDClasses">\n'
        '  <ExchangePlan uuid="00000000-0000-0000-0000-000000000010">\n'
        "    <Properties><Name>План</Name></Properties>\n"
        "  </ExchangePlan>\n"
        "</MetaDataObject>\n",
        encoding="utf-8",
        newline="\n",
    )
    (root / "ExchangePlans" / "План" / "Ext" / "ManagerModule.bsl").write_text(
        "Процедура ПриПолученииНастроек(Настройки) Экспорт\n"
        "    Настройки.ЭтоПланОбменаXDTO = Истина;\n"
        '    Настройки.ФорматОбмена = "urn:test";\n'
        "    Настройки.ПравилаРегистрацииВМенеджере = Ложь;\n"
        "    Версии = Новый Соответствие;\n"
        '    Версии.Вставить("1.2", МенеджерОбменаПример);\n'
        "    Настройки.ВерсииФорматаОбмена = Версии;\n"
        "КонецПроцедуры\n",
        encoding="utf-8",
        newline="\n",
    )
    shutil.copyfile(MANAGER, root / "CommonModules" / "МенеджерОбменаПример" / "Ext" / "Module.bsl")
    (packages / "Пакет.xml").write_text(_xml("Пакет", FORMAT_URI), encoding="utf-8", newline="\n")
    (packages / "Сообщение.xml").write_text(
        _xml("Сообщение", MESSAGE_URI), encoding="utf-8", newline="\n"
    )
    bin_text = "" if broken else _bin(FORMAT_URI)
    (packages / "Пакет" / "Ext" / "Package.bin").write_text(
        bin_text, encoding="utf-8", newline="\n"
    )
    (packages / "Сообщение" / "Ext" / "Package.bin").write_text(
        _bin(MESSAGE_URI), encoding="utf-8", newline="\n"
    )
    if ambiguous:
        (packages / "Копия.xml").write_text(
            _xml("Копия", FORMAT_URI), encoding="utf-8", newline="\n"
        )
        (packages / "Копия" / "Ext" / "Package.bin").write_text(
            _bin(FORMAT_URI), encoding="utf-8", newline="\n"
        )


def test_summary_pages_and_plan_case(service):
    opened = service.ed_routes(path=str(GRAMMAR))
    assert opened["reused"] is False and opened["stale"] is False
    assert opened["node_state"] == "unknown"
    assert opened["extension_policy"] == "base_only"
    assert opened["available_sections"] == [
        "summary",
        "plans",
        "versions",
        "variants",
        "packages",
        "skipped",
    ]
    again = service.ed_routes(path=str(GRAMMAR))
    assert again["reused"] is True and again["stale"] is False
    assert again["profile_id"] == opened["profile_id"]
    assert again["source"]["fingerprint"] == opened["source"]["fingerprint"]
    ident = opened["profile_id"]
    for section in ("plans", "versions", "variants", "packages", "skipped"):
        page = service.ed_routes(profile_id=ident, section=section, limit=200)
        _page_keys(page)
        assert page["section"] == section
        assert page["total"] >= 1
    versions = service.ed_routes(profile_id=ident, section="versions", limit=1)
    rest = service.ed_routes(profile_id=ident, section="versions", offset=1, limit=200)
    full = service.ed_routes(profile_id=ident, section="versions", limit=200)
    _page_keys(versions)
    assert versions["total"] == rest["total"] == full["total"]
    assert versions["has_more"] is True
    assert versions["items"] + rest["items"] == full["items"]
    assert "call_chain" not in full["items"][0]
    named = service.ed_routes(profile_id=ident, section="versions", plan="планформата", limit=200)
    assert named["items"]
    assert {item["plan"] for item in named["items"]} == {"ПланФормата"}
    assert "call_chain" in named["items"][0]
    assert len(named["items"][0]["call_chain"]) <= 4
    with pytest.raises(ValueError, match="не найден"):
        service.ed_routes(profile_id=ident, section="plans", plan="НетТакого")


@pytest.mark.parametrize("limit", [0, 201, True])
def test_page_limits_reject_bool(service, limit):
    with pytest.raises(ValueError):
        service.ed_routes(path=str(GRAMMAR), limit=limit)
    ident = service.ed_routes(path=str(DATA / "no-ed"))["profile_id"]
    with pytest.raises(ValueError):
        service.ed_route_compare(ident, ident, limit=limit)
    with pytest.raises(ValueError):
        service.ed_routes(path=str(GRAMMAR), offset=True)


def test_selectors_and_error_codes(service, tmp_path, monkeypatch):
    with pytest.raises(ValueError):
        service.ed_routes()
    with pytest.raises(ValueError):
        service.ed_routes(path=str(GRAMMAR), project="sample")
    ident = service.ed_routes(path=str(GRAMMAR))["profile_id"]
    with pytest.raises(ValueError):
        service.ed_routes(path=str(GRAMMAR), profile_id=ident)
    with pytest.raises(ValueError):
        service.ed_routes(profile_id=ident, force=True)
    with pytest.raises(ValueError):
        service.ed_routes(path=str(GRAMMAR), configuration="ext")
    with pytest.raises(ValueError):
        service.ed_routes(path=str(GRAMMAR), section="нет")
    with pytest.raises(ValueError):
        service.ed_routes(path=str(GRAMMAR), force=1)
    with pytest.raises(EdRouteProfileNotFoundError):
        service.ed_routes(profile_id="route-missing")
    with pytest.raises(EdRouteReadError):
        service.ed_routes(path=str(tmp_path / "нет-такого-корня"))
    with pytest.raises(EdRouteFormatError):
        service.ed_routes(path=str(DATA / "not-a-dump"))
    with pytest.raises(EdRouteFormatError):
        service.ed_routes(path=str(GRAMMAR / "Configuration.xml"))
    broken = tmp_path / "broken"
    shutil.copytree(GRAMMAR, broken)
    (broken / "Configuration.xml").write_text("<", encoding="utf-8")
    with pytest.raises(EdRouteFormatError):
        service.ed_routes(path=str(broken), force=True)
    saved_file_limit = routes_module.MAX_FILE_BYTES
    monkeypatch.setattr(routes_module, "MAX_FILE_BYTES", 8)
    with pytest.raises(EdRouteResourceLimitError):
        service.ed_routes(path=str(GRAMMAR), force=True)
    monkeypatch.setattr(routes_module, "MAX_FILE_BYTES", saved_file_limit)
    monkeypatch.setattr(routes_service, "MAX_STORED_BYTES", 1)
    with pytest.raises(EdRouteResourceLimitError):
        service.ed_routes(path=str(DATA / "no-ed"), force=True)
    with pytest.raises(Kd2Error) as caught:
        service.ed_routes(project="нет-такого-проекта-маршрутов")
    assert type(caught.value) is Kd2Error
    codes = {
        "invalid_argument": ValueError("план"),
        "ed_route_profile_not_found": EdRouteProfileNotFoundError("нет"),
        "ed_route_read_error": EdRouteReadError("нет"),
        "ed_route_format": EdRouteFormatError("нет"),
        "ed_route_resource_limit": EdRouteResourceLimitError("нет"),
        "rejected": caught.value,
    }
    for code, error in codes.items():
        assert error_payload(error)["code"] == code


def test_compare_filters_keep_summary_and_skipped(service):
    ident = service.ed_routes(path=str(GRAMMAR))["profile_id"]
    full = service.ed_route_compare(ident, ident)
    assert full["summary"]["skipped"] == len(full["skipped"])
    assert full["profile"]["actual_node_version"] is None
    narrow = service.ed_route_compare(
        ident, ident, level="ошибка", check_prefix="ed.route.нет_такой"
    )
    assert narrow["summary"] == full["summary"]
    assert narrow["skipped"] == full["skipped"]
    assert narrow["issues"]["items"] == []
    assert narrow["issues"]["total"] == 0
    warnings = service.ed_route_compare(ident, ident, level="предупреждение")
    assert warnings["summary"] == full["summary"]
    assert all(item["level"] == "предупреждение" for item in warnings["issues"]["items"])
    skipped = service.ed_route_compare(ident, ident, section="skipped", level="ошибка", limit=1)
    assert skipped["skipped"]["total"] == full["summary"]["skipped"]
    _page_keys(skipped["skipped"])
    versions = service.ed_route_compare(ident, ident, section="versions", limit=1)
    _page_keys(versions["versions"])
    diff = service.ed_route_compare(ident, ident, section="schema_diff")
    _page_keys(diff["schema_diff"])
    without = service.ed_route_compare(ident, ident, context="without_node")
    assert without["profile"]["context"] == "without_node"
    assert without["summary"]["skipped"] >= 1
    with pytest.raises(ValueError, match="без узла"):
        service.ed_route_compare(ident, ident, context="without_node", left_plan="ПланФормата")
    with pytest.raises(ValueError):
        service.ed_route_compare(ident, ident, context="node")
    with pytest.raises(ValueError):
        service.ed_route_compare(ident, ident, level="error")
    empty = service.ed_routes(path=str(DATA / "no-ed"))["profile_id"]
    assert "summary" in service.ed_route_compare(empty, empty)


def test_plan_names_are_case_insensitive(service):
    ident = service.ed_routes(path=str(DATA / "maps-equal"))["profile_id"]
    with pytest.raises(ValueError, match="Несколько планов"):
        service.ed_route_compare(ident, ident)
    named = service.ed_route_compare(ident, ident, left_plan="планполный", right_plan="ПЛАНПОЛНЫЙ")
    assert named["profile"]["left_plan"] == "ПланПолный"
    assert named["profile"]["right_plan"] == "ПланПолный"
    with pytest.raises(ValueError, match="не найден"):
        service.ed_route_compare(ident, ident, left_plan="НетТакого", right_plan="ПланПолный")


def test_ready_arguments_open_schema_and_manager(service, tmp_path):
    root = tmp_path / "dump"
    make_dump(root)
    ident = service.ed_routes(path=str(root))["profile_id"]
    report = service.ed_route_compare(ident, ident)
    assert report["profile"]["status"] == "statically_compatible"
    assert report["profile"]["negotiated_candidate"] == "1.2"
    for side in ("left", "right"):
        chosen = report["profile"]["selected"][side]
        assert chosen["ed_open_reason"] is None
        assert chosen["ed_schema_reason"] is None
        opened = service.ed_open(chosen["ed_open"]["path"])
        assert opened["manager_version"] == 2
        schema = service.ed_schema_open(**chosen["ed_schema_open"])
        assert schema["format_version"] == "1.2"
        assert schema["status"] == "complete"


def test_project_arguments_open_schema(service, tmp_path):
    project = tmp_path / "project"
    make_dump(project / "main")
    catalog = tmp_path / "projects.yaml"
    catalog.write_text(
        "projects:\n  sample:\n    name: Sample\n"
        "    configurations:\n      full:\n        dump: main\n",
        encoding="utf-8",
    )
    routed = Kd2Service(
        Settings(
            cache_dir=tmp_path / "cache",
            workspace=tmp_path / "workspace",
            projects_file=catalog,
            project_dirs={"sample": project},
        )
    )
    ident = routed.ed_routes(project="sample")["profile_id"]
    chosen = routed.ed_route_compare(ident, ident)["profile"]["selected"]["left"]
    args = chosen["ed_schema_open"]
    assert args["project"] == "sample"
    assert args["package"] == "Пакет"
    assert args["format_version"] == "1.2"
    assert "path" not in args
    assert routed.ed_schema_open(**args)["status"] == "complete"
    assert routed.ed_open(chosen["ed_open"]["path"])["manager_version"] == 2


def test_schema_failure_is_not_a_tool_error(service, tmp_path):
    root = tmp_path / "dump"
    make_dump(root, broken=True)
    ident = service.ed_routes(path=str(root))["profile_id"]
    report = service.ed_route_compare(ident, ident)
    assert report["profile"]["status"] == "unknown"
    assert any(item["check"] == "ed.route.schema_diff" for item in report["skipped"])


@pytest.mark.parametrize(
    "error",
    [
        EdSchemaFormatError("пустой файл"),
        EdSchemaResourceLimitError("превышен лимит"),
        RuntimeError("свойство без имени"),
    ],
)
def test_any_schema_exception_becomes_unavailable(service, tmp_path, monkeypatch, error):
    root = tmp_path / "dump"
    make_dump(root)

    def boom(*args, **kwargs):
        raise error

    monkeypatch.setattr(routes_service, "load_schema", boom)
    ident = service.ed_routes(path=str(root))["profile_id"]
    report = service.ed_route_compare(ident, ident)
    assert report["profile"]["status"] == "unknown"
    assert str(error) in " ".join(item["reason"] for item in report["skipped"])


def test_schema_is_passed_only_under_its_uri(service, tmp_path, monkeypatch):
    root = tmp_path / "dump"
    make_dump(root)
    real_load = routes_service.load_schema

    def fake(path, **kwargs):
        schema = real_load(path, **kwargs)
        return replace(schema, base_namespace="urn:other")

    monkeypatch.setattr(routes_service, "load_schema", fake)
    seen: dict[str, list] = {}
    real_compare = routes_service.compare_routes

    def spy(left, right, selection, schemas):
        seen["values"] = list(schemas.values())
        seen["keys"] = list(schemas)
        return real_compare(left, right, selection, schemas)

    monkeypatch.setattr(routes_service, "compare_routes", spy)
    ident = service.ed_routes(path=str(root))["profile_id"]
    report = service.ed_route_compare(ident, ident)
    assert seen["values"]
    assert all(isinstance(item, SchemaUnavailable) for item in seen["values"])
    assert all(not isinstance(item, EdSchema) for item in seen["values"])
    assert all(key[1] == FORMAT_URI for key in seen["keys"])
    assert all("не соответствует" in item.reason for item in seen["values"])
    assert report["profile"]["status"] == "unknown"


def test_ambiguous_package_has_no_schema_arguments(service, tmp_path):
    root = tmp_path / "dump"
    make_dump(root, ambiguous=True)
    ident = service.ed_routes(path=str(root))["profile_id"]
    chosen = service.ed_route_compare(ident, ident)["profile"]["selected"]["left"]
    assert chosen["ed_schema_open"] is None
    assert chosen["ed_schema_reason"]
    assert chosen["ed_open"]["path"]


def test_stale_force_and_lru(service, tmp_path, monkeypatch):
    root = tmp_path / "dump"
    shutil.copytree(GRAMMAR, root)
    first = service.ed_routes(path=str(root))
    body = root / "CommonModules" / "МенеджерОбменаПример" / "Ext" / "Module.bsl"
    body.write_text(body.read_text(encoding="utf-8") + "\n// trivia\n", encoding="utf-8")
    stale = service.ed_routes(path=str(root))
    assert stale["reused"] is True and stale["stale"] is True
    assert stale["profile_id"] == first["profile_id"]
    forced = service.ed_routes(path=str(root), force=True)
    assert forced["stale"] is False
    assert forced["profile_id"] != first["profile_id"]
    assert forced["source"]["fingerprint"] != first["source"]["fingerprint"]

    def keys(ident: str) -> list[tuple]:
        page = service.ed_routes(
            profile_id=ident, section="versions", plan="ПланФормата", limit=200
        )
        return [(item["key"], item["manager_name"], item["state"]) for item in page["items"]]

    assert keys(first["profile_id"]) == keys(forced["profile_id"])
    plan = root / "ExchangePlans" / "ПланФормата" / "Ext" / "ManagerModule.bsl"
    plan.write_text(
        plan.read_text(encoding="utf-8").replace(
            'Карта.Вставить("1.2", МенеджерОбменаПример);',
            'Карта.Вставить("9.9", МенеджерОбменаПример);',
            1,
        ),
        encoding="utf-8",
    )
    replaced = service.ed_routes(path=str(root), force=True)
    assert "1.2" in {item[0] for item in keys(first["profile_id"])}
    assert "9.9" in {item[0] for item in keys(replaced["profile_id"])}
    assert "9.9" not in {item[0] for item in keys(first["profile_id"])}

    monkeypatch.setattr(routes_service, "MAX_PROFILES", 2)
    dumps = []
    for index in range(3):
        folder = tmp_path / f"copy{index}"
        shutil.copytree(GRAMMAR, folder)
        config = folder / "Configuration.xml"
        config.write_text(
            config.read_text(encoding="utf-8").replace(
                "ВымышленнаяКонфигурация", f"Конфигурация{index}"
            ),
            encoding="utf-8",
        )
        dumps.append(str(folder))
    kept = service.ed_routes(path=dumps[0])["profile_id"]
    gone = service.ed_routes(path=dumps[1])["profile_id"]
    service.ed_routes(profile_id=kept)
    service.ed_routes(path=dumps[2])
    assert service.ed_routes(profile_id=kept)["profile_id"] == kept
    with pytest.raises(EdRouteProfileNotFoundError):
        service.ed_routes(profile_id=gone)


def test_stale_follows_only_directories_the_reader_uses(service, tmp_path):
    """Устаревание считается по каталогам чтения маршрутов, а не по всей выгрузке."""
    root = tmp_path / "dump"
    make_dump(root)
    first = service.ed_routes(path=str(root))
    assert first["stale"] is False

    foreign = root / "Documents" / "Заказ.xml"
    foreign.parent.mkdir(parents=True)
    foreign.write_text("<MetaDataObject/>", encoding="utf-8")
    assert service.ed_routes(path=str(root))["stale"] is False

    for folder in ("ExchangePlans", "CommonModules", "Subsystems", "XDTOPackages"):
        added = root / folder / "НовыйОбъект.xml"
        added.parent.mkdir(parents=True, exist_ok=True)
        added.write_text("<MetaDataObject/>", encoding="utf-8")
        assert service.ed_routes(path=str(root))["stale"] is True, folder
        added.unlink()
        assert service.ed_routes(path=str(root))["stale"] is False, folder

    config = root / "Configuration.xml"
    config.write_text(config.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    assert service.ed_routes(path=str(root))["stale"] is True


def test_two_compares_are_equal(service, tmp_path):
    root = tmp_path / "dump"
    make_dump(root)
    ident = service.ed_routes(path=str(root))["profile_id"]
    assert service.ed_route_compare(ident, ident) == service.ed_route_compare(ident, ident)
