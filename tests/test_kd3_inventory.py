"""Инвентаризация на минимальной нейтральной выгрузке и ошибочных входах."""

import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import kd3_inventory  # noqa: E402  # скрипт, не пакет — как scripts/build_packs.py в его тесте

DATA = Path(__file__).parent / "data/kd3dump"


def test_synthetic_inventory() -> None:
    result = kd3_inventory.scan_dump("sample", "full", DATA.parent, DATA.name)
    assert not result.errors
    manager = result.managers[0]
    assert {
        key: manager[key]
        for key in ("version", "pko", "pod", "pkpd", "pks", "handlers", "algorithms")
    } == {"version": "2", "pko": 2, "pod": 1, "pkpd": 1, "pks": 3, "handlers": 1, "algorithms": 1}
    assert manager["lines"] == len(
        (DATA / "CommonModules" / kd3_inventory.MANAGER_PREFIX / "Ext/Module.bsl")
        .read_text(encoding="utf-8")
        .splitlines()
    )
    assert result.plans[0]["rules"] == 2
    assert result.packages[0]["format"] == "XML-текст"
    assert result.packages[0]["object_types"] == 1
    assert result.packages[0]["value_types"] == 1
    assert result.packages[0]["version"] == "1.22.1"
    assert result.reader["present"] == "нет"
    markdown = kd3_inventory.render_markdown([result])
    assert "## sample / full / kd3dump" in markdown
    assert "| ПКО | ПОД | ПКПД | ПКС | Обработчики | Алгоритмы |" in markdown
    assert "| 2 | 1 | 1 | 3 | 1 | 1 |" in markdown
    assert "| Синхронизация |" in markdown
    assert "| XML-текст | 1 | 1 |" in markdown
    assert str(DATA.resolve()) not in markdown


def test_unreadable_and_binary_files(tmp_path: Path) -> None:
    shutil.copytree(DATA, tmp_path / "dump")
    package = tmp_path / "dump/XDTOPackages/EnterpriseData_1_22_1/Ext/Package.bin"
    package.write_bytes(b"\x01\x02\x03\x00")
    result = kd3_inventory.scan_dump("sample", "full", tmp_path, "dump")
    assert result.packages[0]["format"] == "двоичный"
    assert result.packages[0]["object_types"] == "—"
    package.write_bytes(b"<package>")
    result = kd3_inventory.scan_dump("sample", "full", tmp_path, "dump")
    assert "не прочитано" in kd3_inventory.render_markdown([result])
    assert result.managers and result.plans
    missing = kd3_inventory.scan_dump("sample", "full", tmp_path, "missing")
    assert "не прочитано: файл отсутствует" in missing.errors[0]


def test_catalog_extensions_and_cli(tmp_path: Path, monkeypatch, capsys) -> None:
    shutil.copytree(DATA, tmp_path / "dump")
    shutil.copytree(DATA, tmp_path / "extension")
    (tmp_path / "projects.yaml").write_text(
        "projects:\n  sample:\n    configurations:\n      full:\n"
        "        dump: dump\n        extensions: [extension]\n",
        encoding="utf-8",
    )
    (tmp_path / "projects.local.yaml").write_text(
        f"projects:\n  sample: '{tmp_path.as_posix()}'\n", encoding="utf-8"
    )
    results = kd3_inventory.inventory(tmp_path, "sample")
    assert [r.dump for r in results] == ["dump", "extension"]
    assert all(r.managers and not r.errors for r in results)
    monkeypatch.setattr(kd3_inventory, "inventory", lambda **kwargs: results)
    output = tmp_path / "inventory.md"
    monkeypatch.setattr(
        "sys.argv", ["kd3_inventory.py", "--project", "sample", "--write", str(output)]
    )
    kd3_inventory.main()
    assert output.read_text(encoding="utf-8") == kd3_inventory.render_markdown(results)
    assert b"\r" not in output.read_bytes()
    monkeypatch.setattr("sys.argv", ["kd3_inventory.py"])
    kd3_inventory.main()
    assert capsys.readouterr().out == kd3_inventory.render_markdown(results)


def test_reader_version_and_nested_algorithms(tmp_path: Path) -> None:
    root = tmp_path / "dump/CommonModules"
    for name, text in (
        ("ОбменДаннымиXDTOСервер", "// Читатель\n"),
        ("ОбновлениеИнформационнойБазыБСП", 'Описание.Версия = "3.1.12.297";\n'),
    ):
        file = root / name / "Ext/Module.bsl"
        file.parent.mkdir(parents=True)
        file.write_text(text, encoding="utf-8")
    result = kd3_inventory.scan_dump("sample", "full", tmp_path, "dump")
    assert result.reader["bsp"] == "3.1.12.297"
    assert result.reader["lines"] == 1
    text = (
        "#Область Алгоритмы\n#Область Вложенная\nФункция Найти()\n"
        "#КонецОбласти\nПроцедура Записать()\n#КонецОбласти\nПроцедура Вне()\n"
    )
    assert kd3_inventory.manager_counts(text)["algorithms"] == 2


def test_indirect_plan_reference(tmp_path: Path) -> None:
    shutil.copytree(DATA, tmp_path / "dump")
    plan = tmp_path / "dump/ExchangePlans/Синхронизация/Ext/ManagerModule.bsl"
    plan.write_text(
        "НастройкиФормата.ПриПолученииДоступныхВерсийФормата(Версии);\n", encoding="utf-8"
    )
    provider = tmp_path / "dump/CommonModules/НастройкиФормата/Ext/Module.bsl"
    provider.parent.mkdir(parents=True)
    provider.write_text(
        "Процедура ПриПолученииДоступныхВерсийФормата(Версии) Экспорт\n"
        'Версии.Вставить("1.22", МенеджерОбменаЧерезУниверсальныйФормат);\n'
        "КонецПроцедуры\n",
        encoding="utf-8",
    )
    result = kd3_inventory.scan_dump("sample", "full", tmp_path, "dump")
    assert not result.errors
    assert result.plans[0]["rules"] == 2
    assert result.plans[0]["modules"] == kd3_inventory.MANAGER_PREFIX
    assert "НастройкиФормата/Ext/Module.bsl" in str(result.plans[0]["providers"])
