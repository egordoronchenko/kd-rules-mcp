"""Расширение загрузки правил из файла: исходники и сборка `.cfe` с подставным конфигуратором."""

from __future__ import annotations

import shutil
from collections.abc import Sequence
from pathlib import Path

import pytest
from lxml import etree

from kdbase.extension_build import (
    MD,
    SOURCE,
    build,
    check,
    main,
    read_extension,
    sources_digest,
    verify,
)

FORM = "InformationRegisters/ПравилаДляОбменаДанными/Forms/ЗагрузитьПравилаСинхронизацииДанных"


def _xml(path: Path) -> etree._Element:
    return etree.parse(str(path)).getroot()


def test_sources_are_one_extension_for_any_bsp_config() -> None:
    """Сопоставление по именам: UUID языка у БП, ЗУП и ERP разные, по UUID один .cfe не подходит."""
    props = _xml(SOURCE / "Configuration.xml").find(f"{{{MD}}}Configuration/{{{MD}}}Properties")
    assert props is not None
    assert props.findtext(f"{{{MD}}}Name") == "кд2_ЗагрузкаПравилИзФайла"
    assert props.findtext(f"{{{MD}}}NamePrefix") == "кд2_"
    assert props.findtext(f"{{{MD}}}ConfigurationExtensionPurpose") == "AddOn"
    assert props.findtext(f"{{{MD}}}ConfigurationExtensionCompatibilityMode") == "Version8_3_24"
    assert props.findtext(f"{{{MD}}}KeepMappingToExtendedConfigurationObjectsByIDs") == "false"
    for path in SOURCE.rglob("*.xml"):
        assert "<ExtendedConfigurationObject>" not in path.read_text(encoding="utf-8-sig"), path


def test_only_the_rules_form_is_adopted() -> None:
    files = {p.relative_to(SOURCE).as_posix() for p in SOURCE.rglob("*") if p.is_file()}
    assert files == {
        "Configuration.xml",
        "Languages/Русский.xml",
        "InformationRegisters/ПравилаДляОбменаДанными.xml",
        f"{FORM}.xml",
        f"{FORM}/Ext/Form.xml",
        f"{FORM}/Ext/Form/Module.bsl",
    }


def test_form_handlers_exist_in_module_with_prefix() -> None:
    form = (SOURCE / FORM / "Ext/Form.xml").read_text(encoding="utf-8-sig")
    module = (SOURCE / FORM / "Ext/Form/Module.bsl").read_text(encoding="utf-8-sig")
    for handler in ("кд2_ПриСозданииНаСервереПосле", "кд2_ЗагрузитьПравилаИзФайла"):
        assert f">{handler}<" in form
        assert f"Процедура {handler}(" in module
    assert "Процедура кд2_ЗагрузитьПравилаИзФайлаЗавершение(" in module
    # Вход в типовую загрузку формы; выбор файла — публичный API БСП, общий модуль не заимствуется.
    assert "ПередЗагрузкойПравил(ПомещенныйФайл.Хранение" in module
    assert "ФайловаяСистемаКлиент.ЗагрузитьФайл(" in module


STEPS = (
    "/LoadConfigFromFiles",
    "/CheckCanApplyConfigurationExtensions",
    "/DumpConfigToFiles",
    "/DumpCfg",
    "/LoadCfg",
)


def step_of(args: Sequence[str]) -> str:
    return next(arg for arg in args if arg in STEPS)


class FakeDesigner:
    """Пишет результаты так, как пакетный конфигуратор: файл /DumpResult, журнал /Out, выгрузки."""

    def __init__(self, source: Path, *, fail: str = "", tamper: bool = False) -> None:
        self.source = source
        self.fail = fail
        self.tamper = tamper
        self.calls: list[list[str]] = []

    def __call__(self, argv: Sequence[str]) -> int:
        args = list(argv)
        self.calls.append(args)
        step = step_of(args)
        result = Path(args[args.index("/DumpResult") + 1])
        log = Path(args[args.index("/Out") + 1])
        if self.fail and self.fail == step:
            log.write_text("Ошибка применимости", encoding="utf-8")
            result.write_text("1", encoding="utf-8")
            return 1
        if step == "/DumpConfigToFiles":
            target = Path(args[args.index(step) + 1])
            shutil.copytree(self.source, target)
            (target / "ConfigDumpInfo.xml").write_text("<x/>", encoding="utf-8")
            if self.tamper:
                (target / FORM / "Ext/Form/Module.bsl").write_text("// другое", encoding="utf-8")
        elif step == "/DumpCfg":
            Path(args[args.index(step) + 1]).write_bytes(b"CFE")
        result.write_text("0", encoding="utf-8")
        return 0


@pytest.fixture
def base(tmp_path: Path) -> Path:
    folder = tmp_path / "base"
    folder.mkdir()
    (folder / "1Cv8.1CD").write_bytes(b"ib")
    return folder


@pytest.fixture
def source(tmp_path: Path) -> Path:
    copy = tmp_path / "src"
    shutil.copytree(SOURCE, copy)
    return copy


def test_build_writes_cfe_and_passport(base: Path, source: Path, tmp_path: Path) -> None:
    out = tmp_path / "out" / "ext.cfe"
    run_root = tmp_path / "run"
    designer = FakeDesigner(source)
    code, report = build(
        base, source, out, Path("1cv8.exe"), user="U", run_root=run_root, runner=designer
    )
    assert code == 0, report
    assert out.read_bytes() == b"CFE"
    assert [step_of(call) for call in designer.calls] == [
        "/LoadConfigFromFiles",
        "/CheckCanApplyConfigurationExtensions",
        "/DumpConfigToFiles",
        "/DumpCfg",
    ]
    assert all(
        call[call.index("-Extension") + 1] == "кд2_ЗагрузкаПравилИзФайла" for call in designer.calls
    )
    assert designer.calls[0][2] == "/F" and designer.calls[0][1] == "DESIGNER"
    assert "/N" in designer.calls[0] and "/P" not in designer.calls[0]
    assert not any(run_root.iterdir()), "копия базы удалена"
    assert report["dump_equal"] is True
    code, passport = verify(out, source)
    assert code == 0 and passport["sources_sha256"] == sources_digest(source)
    # Исходники поменялись — сборка устарела.
    (source / FORM / "Ext/Form/Module.bsl").write_text("// правка", encoding="utf-8")
    assert verify(out, source)[0] == 2


def test_build_stops_when_dump_differs(base: Path, source: Path, tmp_path: Path) -> None:
    out = tmp_path / "ext.cfe"
    code, report = build(
        base,
        source,
        out,
        Path("1cv8.exe"),
        run_root=tmp_path / "run",
        runner=FakeDesigner(source, tamper=True),
    )
    assert code == 2
    assert report["mismatches"] == [f"{FORM}/Ext/Form/Module.bsl"]
    assert not out.exists()


def test_build_reports_designer_log_on_failure(base: Path, source: Path, tmp_path: Path) -> None:
    run_root = tmp_path / "run"
    code, report = build(
        base,
        source,
        tmp_path / "ext.cfe",
        Path("1cv8.exe"),
        run_root=run_root,
        runner=FakeDesigner(source, fail="/CheckCanApplyConfigurationExtensions"),
    )
    assert code == 1
    steps = report["steps"]
    assert isinstance(steps, list)
    assert steps[-1] == {
        "step": "applicability",
        "exit_code": 1,
        "result": "1",
        "log": ["Ошибка применимости"],
    }
    assert not any(run_root.iterdir())


def test_build_refuses_non_file_base(source: Path, tmp_path: Path) -> None:
    code, report = build(
        tmp_path, source, tmp_path / "x.cfe", Path("1cv8.exe"), runner=FakeDesigner(source)
    )
    assert code == 1 and "1Cv8.1CD" in str(report["error"])


def test_check_loads_same_cfe_into_another_base(base: Path, source: Path, tmp_path: Path) -> None:
    out = tmp_path / "ext.cfe"
    build(
        base, source, out, Path("1cv8.exe"), run_root=tmp_path / "run", runner=FakeDesigner(source)
    )
    designer = FakeDesigner(source)
    code, report = check(base, out, Path("1cv8.exe"), run_root=tmp_path / "run", runner=designer)
    assert code == 0, report
    assert [step_of(call) for call in designer.calls] == [
        "/LoadCfg",
        "/CheckCanApplyConfigurationExtensions",
    ]
    out.write_bytes(b"other")
    assert check(base, out, Path("1cv8.exe"), run_root=tmp_path / "run", runner=designer)[0] == 1


def test_verify_without_build(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    assert main(["verify", "--cfe", str(tmp_path / "none.cfe")]) == 1
    assert "сначала build" in capsys.readouterr().out


def test_read_extension_rejects_main_configuration(tmp_path: Path) -> None:
    (tmp_path / "Configuration.xml").write_text(
        f'<MetaDataObject xmlns="{MD}"><Configuration><Properties><Name>X</Name>'
        "</Properties></Configuration></MetaDataObject>",
        encoding="utf-8",
    )
    with pytest.raises(Exception, match="не расширение"):
        read_extension(tmp_path)
