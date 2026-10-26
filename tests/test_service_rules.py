"""Ответы сервиса по проектам правил: приватная копия rules_open."""

import json
from pathlib import Path

import pytest

from kd_rules_mcp.errors import (
    Kd2Error,
    RuleEditError,
    RulesSaveErrorsError,
    StructureNotFoundError,
    WorkspacePathError,
)
from kd_rules_mcp.service import Kd2Service, PathMap, Settings

DATA = Path(__file__).parent / "data"
_MISSING_ALGORITHM = "Выполнить(Алгоритмы.НетТакого);"


@pytest.mark.parametrize("kind", ["pko", "conversion"])
@pytest.mark.parametrize("options", [{}, {"fields": None}, {"fields": {}}])
def test_empty_update_keeps_project_unmodified(tmp_path: Path, kind: str, options: dict) -> None:
    service = Kd2Service(Settings(cache_dir=tmp_path / "cache", workspace=tmp_path / "workspace"))
    opened = service.rules_open(str(DATA / "exchange_rules.xml"))
    project = opened["project_id"]
    before = service.rules_projects()
    assert before["projects"][0]["modified"] is False
    with pytest.raises(RuleEditError, match="хотя бы одно поле"):
        service.rule_update(project, kind, "Организации" if kind == "pko" else "", **options)
    assert service.rules_projects() == before


def test_pro_edit_on_registration_project_points_to_registration_build(tmp_path: Path) -> None:
    service = Kd2Service(Settings(cache_dir=tmp_path / "cache", workspace=tmp_path / "workspace"))
    project = service.rules_open(str(DATA / "registration_rules.xml"))["project_id"]
    with pytest.raises(Kd2Error, match="registration_build"):
        service.rule_update(project, "pro", "Справочник.Организации", fields={"Комментарий": "1"})
    with pytest.raises(Kd2Error, match="registration_build"):
        service.rule_delete(project, "pro", "Справочник.Организации", "", None, None)
    assert service.rules_projects()["projects"][0]["modified"] is False


def test_rules_get_pro_on_exchange_project_names_registration_project(tmp_path: Path) -> None:
    service = Kd2Service(Settings(cache_dir=tmp_path / "cache", workspace=tmp_path / "workspace"))
    project = service.rules_open(str(DATA / "exchange_rules.xml"))["project_id"]
    with pytest.raises(Kd2Error, match="только у проекта правил регистрации"):
        service.rules_get(project, "pro", "Справочник.Организации", "", 10)


def test_migration_rules_get_property_rows_show_code_parameter_and_incoming(tmp_path):
    service = Kd2Service(Settings(cache_dir=tmp_path / "cache", workspace=tmp_path / "workspace"))
    opened = service.rules_open(str(DATA / "exchange_rules.xml"))
    document = service._exchange(opened["project_id"])
    pko = document.pko()[0]
    properties = pko.child("Свойства")
    assert properties is not None
    prop = properties.items[0]
    prop.values["ПередВыгрузкой"] = 'Значение = "Расчетный";'
    prop.values["ПриВыгрузке"] = "ИсходящиеДанные = Параметры.Банк;"
    prop.values["ПослеВыгрузки"] = "   "
    prop.values["ИмяПараметраДляПередачи"] = "Банк"
    prop.values["ПолучитьИзВходящихДанных"] = True
    result = service.rules_get(opened["project_id"], "pko", pko.code, "", 100)
    row = result["properties"]["items"][0]
    assert row["handlers"] == ["ПередВыгрузкой", "ПриВыгрузке"]
    assert row["ИмяПараметраДляПередачи"] == "Банк"
    assert row["ПолучитьИзВходящихДанных"] is True
    assert "Расчетный" not in str(row) and "ИсходящиеДанные" not in str(row)


def test_rules_open_private_copy_save_does_not_touch_shared(tmp_path: Path) -> None:
    """Копия: private и reused false; save копии не меняет saved_path общего проекта."""
    service = Kd2Service(Settings(cache_dir=tmp_path / "cache", workspace=tmp_path / "workspace"))
    path = str(DATA / "exchange_rules.xml")
    shared = service.rules_open(path)
    copy = service.rules_open(path, private=True)
    assert copy["private"] is True
    assert copy["reused"] is False
    assert "source_changed" not in copy
    assert "private" not in shared
    assert copy["project_id"] == f"{shared['project_id']}-p1"
    assert copy["source_path"] == shared["source_path"]

    listed = service.rules_projects()["projects"]
    by_id = {item["project_id"]: item for item in listed}
    assert set(by_id) == {shared["project_id"], copy["project_id"]}
    assert "private" not in by_id[shared["project_id"]]
    assert by_id[copy["project_id"]]["private"] is True

    saved = service.rules_save(shared["project_id"], "shared.xml", False)
    shared_path = service.workspace.get(shared["project_id"]).saved_path
    assert shared_path is not None
    assert shared_path == Path(saved["path"])
    copy_saved = service.rules_save(copy["project_id"], "copy.xml", False)
    assert service.workspace.get(shared["project_id"]).saved_path == shared_path
    copy_path = service.workspace.get(copy["project_id"]).saved_path
    assert copy_path is not None
    assert copy_path == Path(copy_saved["path"])
    assert copy_path != shared_path

    after = {item["project_id"]: item for item in service.rules_projects()["projects"]}
    assert after[shared["project_id"]]["saved_path"] == saved["path"]
    assert after[copy["project_id"]]["saved_path"] == copy_saved["path"]
    assert "private" not in after[shared["project_id"]]
    assert after[copy["project_id"]]["private"] is True


def test_missing_file_inside_workspace_is_not_found(tmp_path: Path) -> None:
    service = Kd2Service(Settings(cache_dir=tmp_path / "cache", workspace=tmp_path / "workspace"))
    missing = service.workspace.root / "no.xml"
    with pytest.raises(Kd2Error, match="Файл не найден"):
        service.rules_open(str(missing))
    with pytest.raises(Kd2Error, match="не входит в подключённые папки"):
        service.rules_open(r"C:\kd2-rules-missing\no.xml")


def test_rules_validate_level_aliases_fail_before_the_project(tmp_path: Path) -> None:
    service = Kd2Service(Settings(cache_dir=tmp_path / "cache", workspace=tmp_path / "workspace"))
    with pytest.raises(ValueError, match="Допустимые"):
        service.rules_validate("missing", None, None, "fatal", None, 0, 20)
    opened = service.rules_open(str(DATA / "exchange_rules.xml"))
    project = opened["project_id"]
    errors = service.rules_validate(project, None, None, "error", None, 0, 20)
    warnings = service.rules_validate(project, None, None, "WARNING", None, 0, 20)
    assert all(item["level"] == "ошибка" for item in errors["issues"]["items"])
    assert all(item["level"] == "предупреждение" for item in warnings["issues"]["items"])


def test_container_source_path_maps_to_the_agent_path() -> None:
    mapped = PathMap.parse(r"C:\1C\Бит=/projects/bit")
    assert mapped.to_host("/projects/bit/Проект/Main") == r"C:\1C\Бит\Проект\Main"


def _service(tmp_path: Path) -> Kd2Service:
    return Kd2Service(Settings(cache_dir=tmp_path / "cache", workspace=tmp_path / "workspace"))


def _open_exchange(service: Kd2Service) -> str:
    return str(service.rules_open(str(DATA / "exchange_rules.xml"))["project_id"])


def _missing_algorithm(service: Kd2Service, project_id: str) -> None:
    """Ошибка `algorithm.missing` без структур: обработчик зовёт алгоритм, которого нет."""
    service.rule_update(
        project_id,
        "conversion",
        "",
        fields={"ПередВыгрузкойДанных": _MISSING_ALGORITHM},
    )


def _modified(service: Kd2Service, project_id: str) -> bool:
    projects = service.rules_projects()["projects"]
    return bool(next(item["modified"] for item in projects if item["project_id"] == project_id))


def test_rules_save_errors_refuses_file(tmp_path: Path) -> None:
    """Ошибка проверки без структур: файла нет, проект изменён, список ошибок в отказе."""
    service = _service(tmp_path)
    project = _open_exchange(service)
    _missing_algorithm(service, project)
    validated = service.rules_validate(project, None, None, "ошибка", "algorithm.", 0, 20)
    assert validated["summary"]["errors"] >= 1
    assert validated["issues"]["items"][0]["check"] == "algorithm.missing"

    destination = service.workspace.root / "blocked.xml"
    with pytest.raises(RulesSaveErrorsError, match="Правила не записаны") as caught:
        service.rules_save(project, "blocked.xml", False)
    assert not destination.is_file()
    assert _modified(service, project) is True
    errors = caught.value.details["errors"]
    assert errors
    assert set(errors[0]) == {"level", "check", "address", "message"}
    assert errors[0]["level"] == "ошибка"
    assert errors[0]["check"] == "algorithm.missing"
    assert caught.value.details["errors_total"] >= 1
    assert caught.value.details["validation"]["errors"] >= 1


def test_rules_save_acknowledge_errors_writes_file(tmp_path: Path) -> None:
    """Те же ошибки с acknowledge_errors: файл есть, saved_with_errors и список ошибок."""
    service = _service(tmp_path)
    project = _open_exchange(service)
    _missing_algorithm(service, project)
    saved = service.rules_save(project, "acked.xml", False, acknowledge_errors=True)
    assert Path(saved["path"]).is_file()
    assert saved["saved_with_errors"] is True
    assert saved["errors_total"] >= 1
    assert saved["errors"][0]["check"] == "algorithm.missing"
    assert _modified(service, project) is False


def test_rules_save_warnings_only(tmp_path: Path) -> None:
    """Пустой источник ПКО — предупреждение format.required, запись без подтверждения."""
    service = _service(tmp_path)
    project = _open_exchange(service)
    service.rule_update(project, "pko", "Организации", fields={"Источник": ""})
    validated = service.rules_validate(
        project, None, None, "предупреждение", "format.required", 0, 20
    )
    assert validated["summary"]["errors"] == 0
    assert validated["issues"]["items"][0]["check"] == "format.required"

    saved = service.rules_save(project, "warnings.xml", False)
    assert Path(saved["path"]).is_file()
    assert saved["saved_with_errors"] is False
    assert saved["validation"]["errors"] == 0
    assert saved["validation"]["warnings"] >= 1
    assert "errors" not in saved


def test_rules_save_remembered_structure(tmp_path: Path) -> None:
    """Структуры последнего rules_validate останавливают запись; в снимок тройка не попадает."""
    service = _service(tmp_path)
    service.structure_load_md83exp("small", str(DATA / "md83exp_small.xml"))
    project = _open_exchange(service)
    without = service.rules_validate(project, None, None, None, "structure.pko_source", 0, 20)
    assert without["issues"]["items"] == []
    with_structure = service.rules_validate(
        project, "small", None, None, "structure.pko_source", 0, 20
    )
    assert with_structure["issues"]["items"][0]["check"] == "structure.pko_source"
    assert service.workspace.get(project).validation_inputs == ("small", None, None)
    with pytest.raises(ValueError, match="Допустимые"):
        service.rules_validate(project, "other", None, "fatal", None, 0, 20)
    assert service.workspace.get(project).validation_inputs == ("small", None, None)

    with pytest.raises(RulesSaveErrorsError) as caught:
        service.rules_save(project, "blocked.xml", False)
    assert not (service.workspace.root / "blocked.xml").is_file()
    assert caught.value.details["validation"]["structures"] == {
        "source": "small",
        "target": None,
        "exchange_project": None,
    }
    assert any(item["check"] == "structure.pko_source" for item in caught.value.details["errors"])

    # В снимке — только идентификаторы; после перезапуска первое сохранение идёт с ними.
    meta_path = service.workspace.root / ".projects" / project / "meta.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    assert meta["validation_inputs"] == {
        "source_structure": "small",
        "target_structure": None,
        "exchange_project_id": None,
    }
    restored = Kd2Service(service.settings)
    with pytest.raises(RulesSaveErrorsError) as after_restart:
        restored.rules_save(project, "after-restart.xml", False)
    assert not (restored.workspace.root / "after-restart.xml").is_file()
    assert after_restart.value.details["validation"]["structures"]["source"] == "small"
    assert any(
        item["check"] == "structure.pko_source" for item in after_restart.value.details["errors"]
    )


def test_rules_save_old_snapshot_without_inputs(tmp_path: Path) -> None:
    """Снимок старого сервера без validation_inputs и поле неверной формы — проект восстановлен."""
    service = _service(tmp_path)
    service.structure_load_md83exp("small", str(DATA / "md83exp_small.xml"))
    project = _open_exchange(service)
    service.rules_validate(project, "small", None, None, None, 0, 20)
    meta_path = service.workspace.root / ".projects" / project / "meta.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))

    for broken in (None, ["small", None, None], {"source_structure": 5}):
        if broken is None:
            meta.pop("validation_inputs", None)
        else:
            meta["validation_inputs"] = broken
        meta_path.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
        restored = Kd2Service(service.settings)
        assert restored.workspace.get(project).validation_inputs is None
        saved = restored.rules_save(project, "old.xml", True)
        assert saved["validation"]["structures"]["source"] is None
        assert "dropped" not in saved["validation"]


def test_rules_save_explicit_structure_overrides_remembered(tmp_path: Path) -> None:
    """Явный target и source=None заменяют запомненный источник целиком."""
    service = _service(tmp_path)
    small = str(DATA / "md83exp_small.xml")
    service.structure_load_md83exp("left", small)
    service.structure_load_md83exp("right", small)
    project = _open_exchange(service)
    service.rules_validate(project, "left", None, None, None, 0, 20)
    with pytest.raises(RulesSaveErrorsError) as caught:
        service.rules_save(
            project, "explicit.xml", False, source_structure=None, target_structure="right"
        )
    validation = caught.value.details["validation"]
    assert validation["structures"] == {
        "source": None,
        "target": "right",
        "exchange_project": None,
    }
    checks = {item["check"] for item in caught.value.details["errors"]}
    assert "structure.pko_target" in checks
    assert "structure.pko_source" not in checks
    assert not (service.workspace.root / "explicit.xml").is_file()
    assert service.workspace.get(project).validation_inputs == ("left", None, None)


def test_rules_save_explicit_unknown_structure(tmp_path: Path) -> None:
    """Неизвестная явная структура — StructureNotFoundError, как у rules_validate."""
    service = _service(tmp_path)
    service.structure_load_md83exp("left", str(DATA / "md83exp_small.xml"))
    project = _open_exchange(service)
    service.rules_validate(project, "left", None, None, None, 0, 20)
    with pytest.raises(StructureNotFoundError):
        service.rules_save(project, "missing.xml", False, source_structure="no-such")
    assert not (service.workspace.root / "missing.xml").is_file()
    assert service.workspace.get(project).validation_inputs == ("left", None, None)


def test_rules_save_dropped_structure(tmp_path: Path) -> None:
    """Запомненной структуры нет в кэше: запись идёт без неё, dropped называет идентификатор."""
    service = _service(tmp_path)
    service.structure_load_md83exp("small", str(DATA / "md83exp_small.xml"))
    project = _open_exchange(service)
    service.rules_validate(project, "small", None, None, None, 0, 20)
    service.store.path("small").unlink()
    saved = service.rules_save(project, "dropped.xml", False)
    assert Path(saved["path"]).is_file()
    assert saved["validation"]["dropped"] == ["source:small"]
    assert saved["validation"]["structures"]["source"] is None
    assert saved["saved_with_errors"] is False
    assert saved["validation"]["errors"] == 0


def test_rules_save_path_errors_precede_validation(tmp_path: Path) -> None:
    """Путь вне папок и существующий файл отказывают до проверки, даже если в правилах ошибки."""
    service = _service(tmp_path)
    project = _open_exchange(service)
    _missing_algorithm(service, project)
    outside = tmp_path / "elsewhere" / "x.xml"
    with pytest.raises(WorkspacePathError):
        service.rules_save(project, str(outside), False)
    assert not outside.exists()

    target = service.workspace.root / "exists.xml"
    target.write_bytes(b"keep")
    with pytest.raises(Kd2Error, match="уже существует") as caught:
        service.rules_save(project, "exists.xml", False)
    assert not isinstance(caught.value, RulesSaveErrorsError)
    assert target.read_bytes() == b"keep"
    assert _modified(service, project) is True


def test_rules_validate_exchange_with_paired_registration(tmp_path: Path) -> None:
    """Правила обмена с правилами регистрации в exchange_project_id: pko_ref_only видит режим
    выгрузки узла; правила обмена там — отказ; rules_save берёт ту же пару."""
    from tests.test_validation_structure import _pro, _ref_rules

    exchange_path = tmp_path / "ExchangeRules.xml"
    exchange_path.write_bytes(_ref_rules())
    registration_path = tmp_path / "RegistrationRules.xml"
    registration_path.write_bytes(
        (
            "<ПравилаРегистрации><ВерсияФормата>2.01</ВерсияФормата>"
            '<ПланОбмена Имя="План">ПланОбменаСсылка.План</ПланОбмена>'
            "<ПравилаРегистрацииОбъектов>"
            + _pro("Справочник.Контрагенты", "РежимВыгрузкиСправочников")
            + "</ПравилаРегистрацииОбъектов></ПравилаРегистрации>"
        ).encode()
    )
    service = Kd2Service(Settings(cache_dir=tmp_path / "cache", workspace=tmp_path / "workspace"))
    exchange = service.rules_open(str(exchange_path))["project_id"]
    registration = service.rules_open(str(registration_path))["project_id"]

    def ref_only(paired: str | None) -> list[dict]:
        view = service.rules_validate(
            exchange, None, None, None, "structure.pko_ref_only", 0, 50, paired
        )
        return view["issues"]["items"]

    alone = ref_only(None)
    assert [i["level"] for i in alone] == ["предупреждение"]
    paired = ref_only(registration)
    assert [i["level"] for i in paired] == ["info"]
    assert "РежимВыгрузкиСправочников" in paired[0]["message"]
    with pytest.raises(Kd2Error, match="правила регистрации того же плана"):
        ref_only(exchange)

    service.rules_validate(exchange, None, None, None, None, 0, 50, registration)
    saved = service.rules_save(exchange, "saved.xml", False)
    assert saved["validation"]["structures"]["exchange_project"] == registration


@pytest.mark.parametrize(
    ("relative", "message"),
    [
        ("CorrespondentExchangeRules.xml", "другой базе"),
        ("ExchangePlans/Другой/Ext/ExchangeRules.xml", "в плане «Другой»"),
    ],
)
def test_rules_validate_refuses_foreign_registration(
    tmp_path: Path, relative: str, message: str
) -> None:
    """ПРО берутся по плану узла (БСП:3551-3552): чужой план и правила корреспондента — отказ."""
    from tests.test_validation_structure import _pro, _ref_rules

    exchange_path = tmp_path / relative
    exchange_path.parent.mkdir(parents=True, exist_ok=True)
    exchange_path.write_bytes(_ref_rules())
    registration_path = tmp_path / "RegistrationRules.xml"
    registration_path.write_bytes(
        (
            "<ПравилаРегистрации><ВерсияФормата>2.01</ВерсияФормата>"
            '<ПланОбмена Имя="План">ПланОбменаСсылка.План</ПланОбмена>'
            "<ПравилаРегистрацииОбъектов>"
            + _pro("Справочник.Контрагенты", "Р")
            + "</ПравилаРегистрацииОбъектов></ПравилаРегистрации>"
        ).encode()
    )
    service = Kd2Service(Settings(cache_dir=tmp_path / "cache", workspace=tmp_path / "workspace"))
    exchange = service.rules_open(str(exchange_path))["project_id"]
    registration = service.rules_open(str(registration_path))["project_id"]
    with pytest.raises(Kd2Error, match=message):
        service.rules_validate(exchange, None, None, None, None, 0, 50, registration)
    assert service.workspace.get(exchange).validation_inputs is None
