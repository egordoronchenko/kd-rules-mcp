"""Сценарии спецификации через адаптер, который проверяет исполняемый текст."""

import base64
import copy
import hashlib
import io
import json
import re
import zipfile
from pathlib import Path

import pytest
from mcp.client import Client

from kd_rules_mcp.errors import LiveBaseRefusedError, LiveStageError, PreviewMismatchError
from kd_rules_mcp.server import create_server, error_payload
from kd_rules_mcp.service import Kd2Service, Settings

DATA = Path(__file__).parent / "data"
FORBIDDEN = (
    "Записать",
    "Удалить",
    "СоздатьУзел",
    "ЗарегистрироватьИзменения",
    "УдалитьРегистрациюИзменений",
)


def ok(data: dict) -> str:
    return "OK " + json.dumps(data, ensure_ascii=False)


class Database:
    """Эмуляция стороны платформы: ответы только текстом OK JSON / ОШИБКА."""

    def __init__(self, own: str, peer: str):
        self.nodes = {
            "enabled": True,
            "nodes": [
                {
                    "code": own,
                    "name": own,
                    "this": True,
                    "sent": 1,
                    "received": 2,
                    "ready": False,
                    "registrations": 0,
                    "registration_tables": {},
                },
                {
                    "code": peer,
                    "name": peer,
                    "this": False,
                    "sent": 3,
                    "received": 4,
                    "ready": True,
                    "registrations": 5,
                    "registration_tables": {"Справочник.Элемент": 5},
                },
            ],
        }
        self.rules = {
            "exchange": (DATA / "exchange_rules.xml").read_bytes(),
            "correspondent": (DATA / "exchange_rules.xml").read_bytes(),
            "registration": (DATA / "registration_rules.xml").read_bytes(),
        }
        self.codes: list[str] = []
        self.readonly = False
        self.included = True
        self.load_ok = True
        self.query_count = 1
        self.fail = ""
        self.chunk_fault = ""
        self.chunk_number = 0
        self.file = b""
        self.uploads = 0
        self.record_resources: dict[str, dict] = {}
        self.record_attributes: dict[str, dict] = {}

    def attributes(self, kind):
        return copy.deepcopy(self.record_attributes.get(kind, {"ПравилаЗагружены": True}))

    def resources(self, kind):
        values = copy.deepcopy(self.record_resources.get(kind, {"ИсточникПравил": "Файл"}))
        values["ПравилаXML"] = base64.b64encode(self.rules[kind]).decode()
        if kind == "exchange":
            values["ПравилаXMLКорреспондента"] = base64.b64encode(
                self.rules["correspondent"]
            ).decode()
        return values

    def execute(self, code: str) -> str:
        assert "\r" not in code and "\n" not in code and "//" not in code
        assert code.startswith("Попытка ") and "ЗаписатьJSON(" in code
        body = code.split(" Запись = Новый ЗаписьJSON;", 1)[0].replace("ЗаписатьJSON", "JSON")
        if self.readonly:
            assert all(word not in body for word in FORBIDDEN)
        self.codes.append(code)
        if '"enabled,nodes"' in code:
            return ok(copy.deepcopy(self.nodes))
        if "ПланыОбменаБСП()" in code:
            return ok({"plans": ["План"]})
        if "СнимокРесурсов" in code:
            assert ".Реквизиты" in code
            kind = "registration" if ".ПравилаРегистрацииОбъектов;" in code else "exchange"
            values = {k: json.dumps(v, ensure_ascii=False) for k, v in self.resources(kind).items()}
            attributes = {
                k: json.dumps(v, ensure_ascii=False) for k, v in self.attributes(kind).items()
            }
            data = json.dumps(
                {"resources": values, "attributes": attributes}, ensure_ascii=False
            ).encode()
            encoded = base64.b64encode(data).decode()
            digest = base64.b64encode(hashlib.sha256(data).digest()).decode()
            match = re.search(r"Сред\(Текст64, (\d+), (\d+)\)", code)
            if match:
                offset, count = map(int, match.groups())
                return ok({"chunk": encoded[offset - 1 : offset - 1 + count], "hash": digest})
            return ok({"size": len(data), "length": len(encoded), "hash": digest})
        if "ЗаполнитьЗначенияСвойств(З, ЗначенияРесурсов)" in code:
            assert "ЗначениеИзСтрокиВнутр" in code and "Записи[Индекс].Удалить()" in code
            assert ".Реквизиты" in code
            manifest = json.loads(self.file)
            for kind, record in manifest["records"].items():
                if record["present"]:
                    values = {k: json.loads(v) for k, v in record["resources"].items()}
                    self.rules[kind] = base64.b64decode(values.pop("ПравилаXML"))
                    if kind == "exchange":
                        self.rules["correspondent"] = base64.b64decode(
                            values.pop("ПравилаXMLКорреспондента")
                        )
                    self.record_resources[kind] = values
                    self.record_attributes[kind] = {
                        k: json.loads(v) for k, v in record["attributes"].items()
                    }
                else:
                    self.rules.pop(kind, None)
                    if kind == "exchange":
                        self.rules.pop("correspondent", None)
            return ok({"restored": True})
        if "З.Прочитать();" in code:
            kind = (
                "registration"
                if ".ПравилаРегистрацииОбъектов;" in code
                else "correspondent"
                if "З.ПравилаXMLКорреспондента" in code
                else "exchange"
            )
            data = self.rules.get(kind)
            if data is None:
                return ok({"present": False})
            digest = base64.b64encode(hashlib.sha256(data).digest()).decode()
            if "Сред(Текст64," in code:
                self.chunk_number += 1
                match = re.search(r"Сред\(Текст64, (\d+), (\d+)\)", code)
                assert match is not None
                offset, count = map(int, match.groups())
                part = base64.b64encode(data).decode()[offset - 1 : offset - 1 + count]
                if self.chunk_fault == "short":
                    part = part[:-1]
                if self.chunk_fault == "digest":
                    part = "A" * len(part)
                if self.chunk_fault == "changed":
                    digest = "changed"
                if self.chunk_fault == "between" and self.chunk_number == 2:
                    return "ОШИБКА Правила изменились во время чтения"
                return ok({"chunk": part, "hash": digest})
            return ok(
                {
                    "present": True,
                    "source": self.record_resources.get(kind, {}).get("ИсточникПравил", "Файл"),
                    "template": "",
                    "file": "rules.zip",
                    "loaded": self.attributes(
                        "registration" if kind == "registration" else "exchange"
                    )["ПравилаЗагружены"],
                    "info": "Версия 2.01; Дата 2026-01-01",
                    "binary": True,
                    "size": len(data),
                    "length": len(base64.b64encode(data)),
                    "hash": digest,
                    "record_hash": base64.b64encode(
                        hashlib.sha256(
                            json.dumps(
                                {
                                    **self.resources(
                                        "registration" if kind == "registration" else "exchange"
                                    ),
                                    **self.attributes(
                                        "registration" if kind == "registration" else "exchange"
                                    ),
                                },
                                ensure_ascii=False,
                                sort_keys=True,
                            ).encode()
                        ).digest()
                    ).decode(),
                }
            )
        if '"included"' in code:
            return ok({"included": self.included})
        if "РежимОткрытияФайла.Создать" in code:
            self.file = b""
            self.uploads += 1
            return ok({"path": "server-file"})
        if "РежимОткрытияФайла.Дописать" in code:
            match = re.search(r'Base64Значение\(\("([A-Za-z0-9+/=]*)"\)\)', code)
            assert match is not None
            self.file += base64.b64decode(match[1])
            return ok({})
        if '"is_file,size,hash"' in code:
            return ok(
                {
                    "is_file": True,
                    "size": len(self.file),
                    "hash": base64.b64encode(hashlib.sha256(self.file).digest()).decode(),
                }
            )
        if "УдалитьФайлы" in code:
            if self.fail == "cleanup":
                return "ОШИБКА удаление не удалось"
            self.file = b""
            return ok({})
        if "ЗагрузитьКомплектПравил" in code or "ЗагрузитьПравила(Отказ" in code:
            if "З.Записать();" in code:
                assert self.load_ok, "запись после неуспешной проверки"
                with zipfile.ZipFile(io.BytesIO(self.file)) as archive:
                    for kind, name in (
                        ("exchange", "ExchangeRules.xml"),
                        ("correspondent", "CorrespondentExchangeRules.xml"),
                        ("registration", "RegistrationRules.xml"),
                    ):
                        if name in archive.namelist():
                            self.rules[kind] = archive.read(name)
            return ok(
                {
                    "loaded": self.load_ok,
                    "rules": [
                        {"kind": "exchange", "loaded": self.load_ok, "info": "сведения"},
                        {"kind": "correspondent", "loaded": self.load_ok, "info": "корреспондент"},
                    ]
                    + (
                        [{"kind": "registration", "loaded": self.load_ok, "info": "регистрация"}]
                        if "ЗагрузитьКомплектПравил" in code
                        else []
                    ),
                    "messages": []
                    if self.load_ok
                    else ['ошибка "|;\nСимвол(10) XML корреспондента'],
                    "compatibility": None,
                }
            )
        if "М.Записать();" in code:
            self.nodes["enabled"] = True
            return ok({"code": self.nodes["nodes"][0]["code"]})
        if "СоздатьУзел" in code:
            self.nodes["nodes"][1]["ready"] = True
            return ok({"id": self.nodes["nodes"][0]["code"]})
        if "ЗарегистрироватьИзменения" in code:
            if self.fail == "registration":
                return "ОШИБКА нет объекта"
            return ok({})
        if "ВыполнитьВыгрузкуДляУзла" in code:
            return ok({"message": "PG1lc3NhZ2UvPg=="})
        if "ВыполнитьОбменДаннымиДляУзла" in code:
            return "ОШИБКА загрузка" if self.fail == "load" else ok({"messages": ["загружено"]})
        if "З = Новый Запрос(" in code:
            return ok({"count": self.query_count, "columns": ["Имя"], "rows": [["строка"]]})
        raise AssertionError("неизвестный код: " + code[:100])


@pytest.fixture
def setup(tmp_path: Path):
    for name in ("source", "target"):
        folder = tmp_path / name
        folder.mkdir()
        (folder / ".dev.env").write_text(
            "IB_USER=login-secret\nIB_PASSWORD=pwd-secret", encoding="utf-8"
        )
        (folder / ".mcp.json").write_text(
            json.dumps({"mcpServers": {"data": {"url": f"http://{name}/base?token=secret"}}}),
            encoding="utf-8",
        )
    catalog = tmp_path / "projects.yaml"
    catalog.write_text(
        "projects:\n"
        + "".join(
            f'''  {name}:
    mcp_config: .mcp.json
    configurations: {{full: {{dump: main}}}}
    bases:
      sandbox:
        role: песочница
        configuration: full
        connection: 'File="{name}";'
        data_mcp: data
        dev_env: .dev.env
'''
            for name in ("source", "target")
        ),
        encoding="utf-8",
    )
    service = Kd2Service(
        Settings(
            cache_dir=tmp_path / "cache",
            workspace=tmp_path / "workspace",
            projects_file=catalog,
            project_dirs={n: tmp_path / n for n in ("source", "target")},
        )
    )
    source, target = Database("SRC", "DST"), Database("DST", "SRC")
    service.live_access_factory = lambda access: source if "source" in access.url else target
    files = {
        kind: str(
            DATA / ("registration_rules.xml" if kind == "registration" else "exchange_rules.xml")
        )
        for kind in ("exchange", "correspondent", "registration")
    }
    return service, source, target, files


def test_status_readonly(setup):
    service, source, _, _ = setup
    source.readonly = True
    result = service.live_status("source.sandbox", "План")
    assert result["status"] == "OK"
    assert result["plans"][0]["nodes"][1] == {
        "code": "DST",
        "name": "DST",
        "this": False,
        "sent": 3,
        "received": 4,
        "ready": True,
        "registrations": 5,
        "registration_tables": {"Справочник.Элемент": 5},
    }
    assert result["plans"][0]["rules"]["exchange"]["source"] == "Файл"
    assert service.live_status("source.sandbox")["plans"][0]["plan"] == "План"


def test_dump_paths_usable_in_diff(setup):
    service, source, _, _ = setup
    source.readonly = True
    result = service.live_rules_dump("source.sandbox", "План")
    assert result["status"] == "OK" and len(result["files"]) == 3
    path = result["files"][0]["path"]
    assert Path(path).read_bytes() == (DATA / "exchange_rules.xml").read_bytes()
    diff = service.rules_diff(path, str(DATA / "exchange_rules.xml"), False, False, None, 0, 50)
    assert diff["summary"] == {}
    assert ".live" in Path(path).parts and Path(result["protocol_path"]).is_file()


def test_dump_open_returns_projects_for_diff(setup):
    service, source, _, _ = setup
    source.readonly = True
    result = service.live_rules_dump("source.sandbox", "План", open=True)
    assert [p["kind"] for p in result["projects"]] == ["exchange", "correspondent", "registration"]
    assert result["stages"][-1]["stage"] == "ОТКРЫТИЕ ПРАВИЛ"
    exchange_id = result["projects"][0]["project_id"]
    assert service.rules_overview(exchange_id)["kind"] == "exchange"
    assert service.rules_overview(result["projects"][2]["project_id"])["kind"] == "registration"
    diff = service.rules_diff(
        exchange_id, str(DATA / "exchange_rules.xml"), False, False, None, 0, 50
    )
    assert diff["summary"] == {}
    # Без флага проекты не открываются.
    assert "projects" not in service.live_rules_dump("source.sandbox", "План")


@pytest.mark.parametrize("fault", ["changed", "short", "digest", "between"])
def test_dump_changed_chunks_leave_no_results(setup, fault):
    service, source, _, _ = setup
    source.chunk_fault = fault
    service.live_chunk_size = 4
    with pytest.raises(LiveStageError) as caught:
        service.live_rules_dump("source.sandbox", "План")
    assert caught.value.details["status"] == "ОШИБКА"
    assert list(Path(caught.value.details["run_path"]).rglob("*.xml")) == []


def test_check_failed_correspondent_without_record_write(setup):
    service, source, _, files = setup
    source.load_ok = False
    with pytest.raises(LiveStageError) as caught:
        service.live_rules_check("source.sandbox", "План", files)
    report = caught.value.details["stages"][0]["report"]
    assert report["rules"][1]["kind"] == "correspondent"
    assert report["messages"] == ['ошибка "|;\nСимвол(10) XML корреспондента']
    assert not any("З.Записать();" in c for c in source.codes)
    assert source.file == b"" and source.uploads == 1


def test_check_pair_uses_archive_conversion_only(setup):
    service, source, _, files = setup
    del files["registration"]
    result = service.live_rules_check("source.sandbox", "План", files)
    assert result["status"] == "OK"
    assert [r["kind"] for r in result["report"]["rules"]] == ["exchange", "correspondent"]
    assert any("ЗагрузитьПравила(Отказ" in c for c in source.codes)
    assert not any("ЗагрузитьКомплектПравил" in c or "З.Записать();" in c for c in source.codes)


def test_missing_dev_env_ignores_local_logins(setup):
    service, source, _, _ = setup
    (service.settings.project_dirs["source"] / ".dev.env").unlink()
    service.settings.projects_file.with_name("projects.local.yaml").write_text(
        "logins:\n  source.sandbox: {user: ignored, password: ignored}\n", encoding="utf-8"
    )
    with pytest.raises(LiveBaseRefusedError, match="нет логина"):
        service.live_status("source.sandbox", "План")
    assert source.codes == []


def test_status_empty_object_is_not_success(setup):
    service, _, _, _ = setup

    class Empty:
        def execute(self, code):
            return "OK {}"

    service.live_access_factory = lambda access: Empty()
    with pytest.raises(LiveStageError) as caught:
        service.live_status("source.sandbox", "План")
    assert caught.value.details["stages"][0]["code"] == "live_parse"


@pytest.mark.parametrize("tool", ["install", "setup", "exchange"])
def test_preview_state_change_refused_for_each_writer(setup, tool):
    service, source, _, files = setup
    source.readonly = True

    def call(**kw):
        if tool == "install":
            return service.live_rules_install("source.sandbox", "План", files, **kw)
        if tool == "setup":
            return service.live_nodes_setup("source.sandbox", "target.sandbox", "План", **kw)
        return service.live_exchange_run(
            "source.sandbox", "target.sandbox", "План", "Справочник.Элемент", "uuid", **kw
        )

    preview = call()
    if tool == "install":
        source.rules["exchange"] = "изменились".encode()
    else:
        source.nodes["nodes"][1]["sent"] = 99
    with pytest.raises(PreviewMismatchError):
        call(mode="apply", expected_preview_hash=preview["preview_hash"])


def test_preview_missing_foreign_tool_or_inputs(setup):
    service, source, target, files = setup
    source.readonly = target.readonly = True
    with pytest.raises(PreviewMismatchError):
        service.live_rules_install("source.sandbox", "План", files, mode="apply")
    preview = service.live_nodes_setup("source.sandbox", "target.sandbox", "План")
    with pytest.raises(PreviewMismatchError):
        service.live_rules_install(
            "source.sandbox",
            "План",
            files,
            mode="apply",
            expected_preview_hash=preview["preview_hash"],
        )
    preview = service.live_rules_install("source.sandbox", "План", files)
    with pytest.raises(PreviewMismatchError):
        service.live_rules_install(
            "source.sandbox",
            "Другой",
            files,
            mode="apply",
            expected_preview_hash=preview["preview_hash"],
        )


@pytest.mark.parametrize("tool", ["install", "setup", "exchange"])
@pytest.mark.parametrize("foreign", ["tool", "inputs"])
def test_preview_foreign_hash_each_writer(setup, tool, foreign):
    service, source, target, files = setup
    source.readonly = target.readonly = True

    def call(plan="План", **kw):
        if tool == "install":
            return service.live_rules_install("source.sandbox", plan, files, **kw)
        if tool == "setup":
            return service.live_nodes_setup("source.sandbox", "target.sandbox", plan, **kw)
        return service.live_exchange_run(**(exchange_args() | {"plan": plan}), **kw)

    preview = (
        service.live_nodes_setup("source.sandbox", "target.sandbox", "План")
        if foreign == "tool" and tool != "setup"
        else service.live_rules_install("source.sandbox", "План", files)
        if foreign == "tool"
        else call()
    )
    with pytest.raises(PreviewMismatchError):
        call(
            "Другой" if foreign == "inputs" else "План",
            mode="apply",
            expected_preview_hash=preview["preview_hash"],
        )


@pytest.mark.parametrize(
    "ref,change,message",
    [
        ("source.sandbox", "role: песочница", "не песочница"),
        ("source.sandbox", "data_mcp: data", "нет способа доступа"),
        ("unknown.sandbox", "", "нет проекта unknown"),
        ("source.unknown", "", "в проекте source нет базы unknown"),
    ],
)
def test_service_refusal_never_constructs_adapter(setup, ref, change, message):
    service, _, _, _ = setup
    if change:
        path = service.settings.projects_file
        text = path.read_text(encoding="utf-8")
        text = text.replace(
            change, "role: боевая" if change.startswith("role") else "data_mcp: ''", 1
        )
        path.write_text(text, encoding="utf-8")
    calls = []
    service.live_access_factory = lambda access: (
        calls.append(access) or pytest.fail("адаптер запрещён")
    )
    with pytest.raises(LiveBaseRefusedError, match=message):
        service.live_status(ref, "План")
    assert calls == []


def test_install_backup_and_restore(setup, tmp_path: Path):
    service, source, _, _ = setup
    original = copy.deepcopy(source.rules)
    folder = tmp_path / "new"
    folder.mkdir()
    files = {}
    for kind, name in (
        ("exchange", "ExchangeRules.xml"),
        ("correspondent", "CorrespondentExchangeRules.xml"),
        ("registration", "RegistrationRules.xml"),
    ):
        path = folder / name
        path.write_bytes(b"new rules " + kind.encode())
        files[kind] = str(path)
    preview = service.live_rules_install("source.sandbox", "План", files)
    result = service.live_rules_install(
        "source.sandbox", "План", files, "apply", preview["preview_hash"]
    )
    assert result["status"] == "OK" and result["backup"]["complete"] is True
    assert source.rules["exchange"] == b"new rules exchange"
    backup = result["backup"]["folder"]
    preview = service.live_rules_install("source.sandbox", "План", backup)
    service.live_rules_install("source.sandbox", "План", backup, "apply", preview["preview_hash"])
    assert source.rules == original


def test_install_check_failure_no_write(setup):
    service, source, _, files = setup
    preview = service.live_rules_install("source.sandbox", "План", files)
    source.load_ok = False
    with pytest.raises(LiveStageError) as caught:
        service.live_rules_install(
            "source.sandbox", "План", files, "apply", preview["preview_hash"]
        )
    assert caught.value.details["stages"][-1]["status"] == "ПРОПУЩЕНА"
    assert not any("З.Записать();" in c for c in source.codes)


def test_nodes_setup_idempotent(setup):
    service, source, target, _ = setup
    source.readonly = target.readonly = True
    preview = service.live_nodes_setup("source.sandbox", "target.sandbox", "План")
    assert preview["effects"][0]["action"] == "изменений нет"
    result = service.live_nodes_setup(
        "source.sandbox",
        "target.sandbox",
        "План",
        mode="apply",
        expected_preview_hash=preview["preview_hash"],
    )
    assert result["changed"] is False and result["status"] == "OK"


def test_nodes_setup_enables_and_preserves_existing(setup):
    service, source, _, _ = setup
    source.nodes["enabled"] = False
    source.nodes["nodes"][1]["ready"] = False
    preview = service.live_nodes_setup("source.sandbox", "target.sandbox", "План")
    result = service.live_nodes_setup(
        "source.sandbox",
        "target.sandbox",
        "План",
        mode="apply",
        expected_preview_hash=preview["preview_hash"],
    )
    assert result["changed"] is True
    assert source.nodes["enabled"] is True and source.nodes["nodes"][1]["ready"] is True


def test_nodes_setup_same_base_and_codes_refused(setup):
    service, source, target, _ = setup
    with pytest.raises(LiveBaseRefusedError):
        service.live_nodes_setup("source.sandbox", "source.sandbox", "План")
    assert source.codes == target.codes == []
    with pytest.raises(ValueError, match="разные"):
        service.live_nodes_setup("source.sandbox", "target.sandbox", "План", "SAME", "SAME")


def exchange_args():
    return {
        "source": "source.sandbox",
        "target": "target.sandbox",
        "plan": "План",
        "object": "Справочник.Элемент",
        "ref": "uuid",
    }


def test_exchange_composition_stops_before_write(setup):
    service, source, target, _ = setup
    source.readonly = target.readonly = True
    source.included = False
    preview = service.live_exchange_run(**exchange_args())
    with pytest.raises(LiveStageError) as caught:
        service.live_exchange_run(
            **exchange_args(), mode="apply", expected_preview_hash=preview["preview_hash"]
        )
    stages = caught.value.details["stages"]
    assert [(s["stage"], s["status"]) for s in stages] == [
        ("УЗЕЛ ИСТОЧНИКА", "OK"),
        ("УЗЕЛ ПРИЕМНИКА", "OK"),
        ("СОСТАВ", "ОШИБКА"),
        ("РЕГИСТРАЦИЯ", "ПРОПУЩЕНА"),
        ("ВЫГРУЗКА", "ПРОПУЩЕНА"),
        ("ЗАГРУЗКА", "ПРОПУЩЕНА"),
    ]


@pytest.mark.parametrize("count,success", [(1, True), (2, False)])
def test_exchange_query_and_message_saved(setup, count, success):
    service, _, target, _ = setup
    target.query_count = count
    args = {**exchange_args(), "query": 'ВЫБРАТЬ "|;Символ(10)" КАК Имя\n', "expect_rows": 1}
    preview = service.live_exchange_run(**args)
    assert [e["irreversible"] for e in preview["effects"]] == [True, True, True]
    if success:
        result = service.live_exchange_run(
            **args, mode="apply", expected_preview_hash=preview["preview_hash"]
        )
        assert result["status"] == "OK"
    else:
        with pytest.raises(LiveStageError) as caught:
            service.live_exchange_run(
                **args, mode="apply", expected_preview_hash=preview["preview_hash"]
            )
        result = caught.value.details
        assert result["stages"][-1]["expected"] == 1
        assert result["stages"][-1]["actual"] == 2
        assert result["stages"][-1]["rows"] == [["строка"]]
    assert Path(result["message_path"]).read_bytes() == b"<message/>"
    assert Path(result["message_path"]).parent == Path(result["run_path"])


def test_exchange_first_error_skips_query(setup):
    service, _, target, _ = setup
    target.fail = "load"
    args = {**exchange_args(), "query": "ВЫБРАТЬ 1"}
    preview = service.live_exchange_run(**args)
    with pytest.raises(LiveStageError) as caught:
        service.live_exchange_run(
            **args, mode="apply", expected_preview_hash=preview["preview_hash"]
        )
    assert [(s["stage"], s["status"]) for s in caught.value.details["stages"]][-2:] == [
        ("ЗАГРУЗКА", "ОШИБКА"),
        ("ЗАПРОС", "ПРОПУЩЕНА"),
    ]
    assert target.file == b""


def test_cleanup_failure_keeps_main_result(setup):
    service, source, _, files = setup
    source.fail = "cleanup"
    result = service.live_rules_check("source.sandbox", "План", files)
    assert result["status"] == "OK"
    assert any(s["stage"] == "УДАЛЕНИЕ ФАЙЛА" and s["status"] == "ОШИБКА" for s in result["stages"])


@pytest.mark.anyio
async def test_mcp_status_and_typed_errors(setup):
    service, _, _, _ = setup
    async with Client(create_server(service)) as client:
        result = await client.call_tool("live_status", {"base": "source.sandbox", "plan": "План"})
        assert not result.is_error
        assert result.structured_content["status"] == "OK"
        result = await client.call_tool("live_status", {"base": "unknown.base"})
        assert result.is_error
        text = "".join(getattr(p, "text", "") for p in result.content)
        assert '"code": "live_base_refused"' in text
    payload = error_payload(PreviewMismatchError("повторите preview"))
    assert payload == {"code": "preview_mismatch", "message": "повторите preview"}


@pytest.fixture
def anyio_backend():
    return "asyncio"
