"""Регрессии живой приёмки: снимок, предпросмотр и диагностика без живых баз."""

import copy
import json
from pathlib import Path

import pytest

from kd_rules_mcp.errors import LiveStageError, PreviewMismatchError
from kd_rules_mcp.live.codegen import rules
from kd_rules_mcp.server import error_payload
from tests.test_live_tools import exchange_args, ok
from tests.test_live_tools import setup as setup


def install(service, files, base="source.sandbox"):
    preview = service.live_rules_install(base, "План", files)
    return service.live_rules_install(base, "План", files, "apply", preview["preview_hash"])


def test_b2_snapshot_restores_register_attributes(setup):
    service, source, _, files = setup
    source.record_attributes = {
        "exchange": {
            "ПравилаЗагружены": True,
            "ИспользоватьФильтрВыборочнойРегистрацииОбъектов": True,
        },
        "registration": {"ПравилаЗагружены": True},
    }
    before = copy.deepcopy(source.record_attributes)
    result = install(service, files)
    manifest_path = Path(result["backup"]["manifest"])
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["schema"] == "kd2-live-register/2"
    assert manifest["records"]["exchange"]["attributes"] == {
        "ПравилаЗагружены": "true",
        "ИспользоватьФильтрВыборочнойРегистрацииОбъектов": "true",
    }
    source.record_attributes["exchange"] = {
        "ПравилаЗагружены": False,
        "ИспользоватьФильтрВыборочнойРегистрацииОбъектов": False,
    }
    restored = install(service, str(manifest_path))
    assert restored["report"]["restored"] is True
    assert source.record_attributes == before


def test_b2_attribute_changes_record_hash(setup):
    service, source, _, _ = setup
    before = service.live_status("source.sandbox", "План")["plans"][0]["rules"]["exchange"][
        "record_hash"
    ]
    source.record_attributes = {"exchange": {"ПравилаЗагружены": False}}
    after = service.live_status("source.sandbox", "План")["plans"][0]["rules"]["exchange"][
        "record_hash"
    ]
    assert before != after
    assert ".Реквизиты" in rules.metadata_code("План", "exchange")


def test_b2_legacy_manifest_is_incomplete_and_cannot_restore(setup):
    service, source, _, files = setup
    result = install(service, files)
    path = Path(result["backup"]["manifest"])
    manifest = json.loads(path.read_text(encoding="utf-8"))
    manifest["schema"] = "kd2-live-register/1"
    for record in manifest["records"].values():
        record.pop("attributes", None)
    path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    preview = service.live_rules_install("source.sandbox", "План", str(path.parent))
    assert preview["effects"][0]["irreversible"] is True
    assert "реквизит" in preview["effects"][0]["reason"]
    source.codes.clear()
    with pytest.raises(ValueError, match="новый снимок"):
        service.live_rules_install(
            "source.sandbox", "План", str(path.parent), "apply", preview["preview_hash"]
        )
    assert not any("ЗаполнитьЗначенияСвойств" in code for code in source.codes)


def test_b2_post_restore_hash_mismatch_stops_stage(setup):
    service, source, _, files = setup
    result = install(service, files)
    original = source.execute

    def execute(code):
        reply = original(code)
        if "ЗаполнитьЗначенияСвойств" in code:
            source.record_attributes = {"exchange": {"ПравилаЗагружены": False}}
        return reply

    source.execute = execute
    with pytest.raises(LiveStageError) as caught:
        install(service, result["backup"]["folder"])
    stage = caught.value.details["stages"][-1]
    assert stage["stage"] == "ВОССТАНОВЛЕНИЕ ПРАВИЛ"
    assert stage["status"] == "ОШИБКА"
    assert "record_hash" in stage["message"]


def test_b2_codegen_visits_attributes_in_snapshot_and_restore():
    assert ".Реквизиты" in rules.record_snapshot_code("План", "exchange", "hash")
    assert ".Реквизиты" in rules.restore_file_code("План", "path")
    assert not hasattr(rules, "RESOURCE_NAMES")


def test_b2_snapshot_without_attributes_is_incomplete(setup):
    service, source, _, files = setup
    original = source.execute

    def execute(code):
        if "СнимокРесурсов" in code:
            return "ОШИБКА Реквизиты снимка не прочитаны"
        return original(code)

    source.execute = execute
    preview = service.live_rules_install("source.sandbox", "План", files)
    assert preview["effects"][0]["irreversible"] is True
    assert "Реквизиты" in preview["effects"][0]["reason"]


def test_m3_manifest_file_is_snapshot_input(setup):
    service, _, _, files = setup
    result = install(service, files)
    preview = service.live_rules_install("source.sandbox", "План", result["backup"]["manifest"])
    assert preview["effects"][0]["action"] == "восстановить записи регистра"


@pytest.mark.parametrize("tool", ["live_rules_check", "live_rules_install"])
def test_m3_non_zip_has_input_error(setup, tmp_path, tool):
    service, _, _, _ = setup
    path = tmp_path / "rules.txt"
    path.write_text("не ZIP", encoding="utf-8")
    with pytest.raises(ValueError) as caught:
        getattr(service, tool)("source.sandbox", "План", str(path))
    assert error_payload(caught.value)["code"] == "invalid_argument"


def test_b1_bad_host_map_is_configuration_error_before_stages(setup, monkeypatch):
    from kd_rules_mcp.projects import ProjectConfigError

    service, source, _, _ = setup
    monkeypatch.setenv("KD2_HOST_MAP", "неверная карта")
    with pytest.raises(ProjectConfigError) as caught:
        service.live_status("source.sandbox", "План")
    assert error_payload(caught.value)["code"] == "project_config"
    assert source.codes == []


def test_m4_previews_for_two_bases_do_not_replace_each_other(setup):
    service, _, _, files = setup
    a = service.live_rules_install("source.sandbox", "План", files)
    b = service.live_rules_install("target.sandbox", "План", files)
    assert a["preview_hash"] != b["preview_hash"]
    service.live_rules_install("source.sandbox", "План", files, "apply", a["preview_hash"])
    with pytest.raises(PreviewMismatchError):
        service.live_rules_install("source.sandbox", "План", files, "apply", a["preview_hash"])
    service.live_rules_install("target.sandbox", "План", files, "apply", b["preview_hash"])


@pytest.mark.parametrize("digest", [None, "чужой хеш"])
def test_m8_apply_rejects_hash_before_snapshot(setup, digest):
    service, source, _, files = setup
    service.live_rules_install("source.sandbox", "План", files)
    source.codes.clear()
    with pytest.raises(PreviewMismatchError):
        service.live_rules_install("source.sandbox", "План", files, "apply", digest)
    assert not any("СнимокРесурсов" in code or "Сред(Текст64" in code for code in source.codes)


def test_m8_snapshot_failure_after_valid_hash_requires_new_preview(setup):
    service, source, _, files = setup
    preview = service.live_rules_install("source.sandbox", "План", files)
    source.chunk_fault = "short"
    with pytest.raises(PreviewMismatchError, match="полнота снимка"):
        service.live_rules_install(
            "source.sandbox", "План", files, "apply", preview["preview_hash"]
        )
    assert not any("З.Записать();" in code for code in source.codes)


def test_m6_platform_exception_has_rule_kinds_and_messages(setup):
    service, source, _, files = setup
    original = source.execute

    def execute(code):
        if "ЗагрузитьКомплектПравил" in code:
            assert "ОшибкаЗагрузки = ПодробноеПредставлениеОшибки" in code
            assert code.index("КонецПопытки;") < code.index("ПолучитьСообщенияПользователю")
            return ok(
                {
                    "loaded": False,
                    "rules": [
                        {"kind": kind, "loaded": False, "info": ""}
                        for kind in ("exchange", "correspondent", "registration")
                    ],
                    "messages": ["Сообщение до исключения"],
                    "compatibility": None,
                    "error": "Тип не определен (СправочникСсылка.Банки)",
                }
            )
        return original(code)

    source.execute = execute
    with pytest.raises(LiveStageError) as caught:
        service.live_rules_check("source.sandbox", "План", files)
    result = caught.value.details
    assert result["report"]["messages"] == ["Сообщение до исключения"]
    assert [r["kind"] for r in result["report"]["rules"]] == [
        "exchange",
        "correspondent",
        "registration",
    ]
    assert result["stages"][0]["stage"] == "ПРОВЕРКА ПРАВИЛ"
    assert result["stages"][0]["message"] == "Тип не определен (СправочникСсылка.Банки)"


def test_m6_cli_keeps_outer_platform_exception_protocol():
    legacy = rules.load_file_code("План", "path", "rules.zip", write=False)
    report = rules.load_file_code("План", "path", "rules.zip", write=False, report_errors=True)
    assert "ОшибкаЗагрузки = ПодробноеПредставлениеОшибки" not in legacy
    assert "ОшибкаЗагрузки = ПодробноеПредставлениеОшибки" in report


def test_m7_preview_explains_nonzero_registration_tables(setup):
    service, source, _, _ = setup
    source.nodes["nodes"][1]["registrations"] = 5
    source.nodes["nodes"][1]["registration_tables"] = {
        "Справочник.Элементы": 2,
        "Документ.Заказ": 3,
    }
    preview = service.live_exchange_run(**exchange_args())
    assert preview["state"]["source"]["nodes"][1]["registration_tables"] == {
        "Справочник.Элементы": 2,
        "Документ.Заказ": 3,
    }
    effect = preview["effects"][0]
    assert effect["registrations"] == {"Справочник.Элементы": 2, "Документ.Заказ": 3}
    assert "5" in effect["action"] and "Справочник.Элементы: 2" in effect["action"]


def test_m7_codegen_counts_only_nonzero_tables():
    from kd_rules_mcp.live.codegen.exchange import nodes_code

    code = nodes_code("План")
    assert "Если В.Следующий() И В.Число > 0 Тогда" in code
    assert "РегистрацииПоТаблицам.Вставить(Элемент.Метаданные.ПолноеИмя(), В.Число)" in code
    assert '"code,name,this,sent,received,ready,registrations,registration_tables"' in code
