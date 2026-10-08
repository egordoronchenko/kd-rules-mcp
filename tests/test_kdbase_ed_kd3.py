"""Адаптер КД 3: текст исполняемого сценария, изоляция базы и синтетические потери, без 1С."""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from kd_rules_mcp.ed.reader import read_manager_text
from kd_rules_mcp.ed.writer import render
from kd_rules_mcp.ed.writer_import import import_manager
from kdbase import ed_kd3_check as adapter

DATA = Path(__file__).parent / "data" / "ed"
KEYS = {
    "ИсточникЗагрузки": "1",
    "ПомещенныеФайлы": "files",
    "Конвертация": "base.Справочники.Конвертации.ПустаяСсылка()",
    "СоздатьНовуюКонвертацию": "true",
    "ИмяНовойКонвертации": "name",
    "Конфигурация": "sampleRef.Конфигурация",
    "ИмяМенеджераКонвертации": '"МенеджерОбменаЧерезУниверсальныйФормат"',
    "ТолькоОбработчики": "false",
    "ИтерацияЗагрузки": "1",
}


def assert_scenario_contract(text: str) -> None:
    """Проверяется именно исполняемый код, не описание вызовов в комментарии."""
    code = re.sub(r"^\s*//.*$", "", text, flags=re.MULTILINE)
    for expression in (
        'new ActiveXObject("V83.COMConnector")',
        "connector.Connect(connection)",
        "base.Обработки.ВыгрузкаМодуля.Создать()",
        "processor.Конвертация = ref",
        "processor.ВыполнитьВыгрузкуМодулей()",
        "result.МенеджерОбменаЧерезУниверсальныйФормат.ПолучитьТекст()",
        "base.Обработки.ЗагрузкаМодуляМенеджера."
        "ВыполнитьЗагрузкуМодуляМенеджера(parameters, address)",
        'base.ПоместитьВоВременноеХранилище(undefined, base.NewObject("УникальныйИдентификатор"))',
        "base.ПолучитьИзВременногоХранилища(address)",
        "if (!result.Успешно) throw new Error",
        'if (result.Свойство("СообщениеОРезультате"))',
        'file.Вставить("ПолноеИмя", input)',
        'file.Вставить("Хранение", input)',
        'file.Вставить("ЭтоРасширение", false)',
        "files.Добавить(file)",
        'new ActiveXObject("ADODB.Stream")',
        'stream.Charset = "utf-8"',
        "stream.Position = 3",
        'environment("KD3_USER")',
        'environment("KD3_PASSWORD")',
        "var text = redact(String(value))",
        'record("error", error.message)',
        'WScript.StdErr.WriteLine("ОШИБКА " + redact(String(error.message)))',
        "WScript.Quit(exitCode)",
    ):
        assert expression in code
    for key, value in KEYS.items():
        assert f'parameters.Вставить("{key}", {value})' in code
    assert code.count('new ActiveXObject("V83.COMConnector")') == 1
    assert code.count("connect(args[1])") == 1
    assert "Справочник.СоставыКонвертаций КАК С" in code
    assert "С.ЭлементКонвертации = П.Ссылка И С.Владелец = &К И С.Отключить = ЛОЖЬ" in code
    assert "П.ПометкаУдаления = ЛОЖЬ И П.ВременнаяКопия = ЛОЖЬ" in code
    for catalog in (
        "ПравилаКонвертацииОбъектов",
        "ПравилаОбработкиДанных",
        "ПравилаКонвертацииПредопределенныхДанных",
        "Алгоритмы",
    ):
        assert f'"{catalog}"' in code
    assert "Справочник.ПравилаКонвертацииСвойств КАК ПКС" in code
    assert 'propertyQuery + "НЕ ПКС.ЭтоГруппа И " + owners' in code
    assert 'propertyQuery + "ПКС.ЭтоГруппа И " + owners' in code
    assert "ПКС.Владелец В (ВЫБРАТЬ С.ЭлементКонвертации" in code
    assert "ПКС.ПометкаУдаления = ЛОЖЬ" in code
    assert 'query.УстановитьПараметр("К", ref)' in code
    assert "selection.Получить(0)" in code
    assert "base.String(enumerator.item().ВерсияФормата)" in code
    assert "Base64Строка" not in code
    assert not re.search(r"catch\s*\([^)]*\)\s*\{\s*\}", code)


def test_scenario_contract() -> None:
    assert_scenario_contract(adapter.SCENARIO.read_text(encoding="utf-8"))


def test_scenario_is_es3() -> None:
    text = adapter.SCENARIO.read_text(encoding="utf-8")
    assert not re.search(r"\b(?:JSON|let|const)\b|=>|\.(?:forEach|map)\s*\(", text)
    assert not re.search(r",\s*[}\]]", text)


def pilot() -> str:
    text = (DATA / "writer" / "pilot.bsl").read_text(encoding="utf-8-sig")
    text = text.replace("(PilotManager)", "(Образец от 01.01.2020 00:00:00)")
    return text + (
        "\nФункция Подключаемый_ИдентификаторМодуля() Экспорт\n"
        '\tВозврат "11111111-1111-1111-1111-111111111111";\nКонецФункции\n'
    )


def generated(text: str) -> str:
    return text.replace(
        "(Образец от 01.01.2020 00:00:00)", "(Круг от 05.10.2026 12:00:00)"
    ).replace("11111111-1111-1111-1111-111111111111", "22222222-2222-2222-2222-222222222222")


def counts(text: str) -> dict[str, int]:
    document = read_manager_text(text)
    return {kind: document.counts[kind] for kind in adapter.KINDS}


def escaped(value: str) -> str:
    return (
        value.replace("\\", "\\\\").replace("\t", "\\t").replace("\r", "\\r").replace("\n", "\\n")
    )


def result_text(command: str, name: str, text: str) -> str:
    values = {
        "protocol": "1",
        "command": command,
        "status": "OK",
        "conversion_count": "1",
        "conversion.0.name": name,
        "conversion.0.manager_version": "2",
        "conversion.0.format_version": "1.0",
        # Намеренно ложное число: оно не должно влиять на сверку состава.
        "loader_message": "Загружено правил: 999",
        **{f"conversion.0.count.{key}": str(value) for key, value in counts(text).items()},
    }
    return "".join(f"{key}\t{escaped(value)}\n" for key, value in values.items())


class FakeCscript:
    """Проверяет файл сценария при каждом вызове и имитирует удаление входа загрузчиком."""

    def __init__(self, original_base: Path, returned: str):
        self.original_base = original_base
        self.returned = returned
        self.calls: list[list[str]] = []
        self.uploads: list[bytes] = []

    def __call__(self, argv: list[str], **options: Any) -> subprocess.CompletedProcess[bytes]:
        assert options == {"capture_output": True, "timeout": adapter.TIMEOUT_S, "check": False}
        assert argv[:4] == ["cscript.exe", "//nologo", "//E:jscript", "//U"]
        raw = Path(argv[4]).read_bytes()
        assert raw.startswith(b"\xff\xfe")
        assert_scenario_contract(raw[2:].decode("utf-16-le"))
        copied = Path(argv[6])
        assert copied.resolve() != self.original_base.resolve()
        assert str(self.original_base) not in argv
        assert copied.name == "base" and copied.parent.name.startswith("kd3-")
        assert (copied / "1Cv8.1CD").read_bytes() == b"synthetic-base"
        assert (copied / "nested" / "synthetic.txt").read_text() == "copy-all"
        self.calls.append(argv)
        name = "Образец"
        if argv[5] == "roundtrip":
            upload = Path(argv[8])
            assert upload.parent == copied.parent
            self.uploads.append(upload.read_bytes())
            upload.unlink()
            name = argv[9]
            assert argv[10] == "Образец"
            Path(argv[11]).write_text(self.returned, encoding="utf-8", newline="")
        elif argv[5] == "export":
            assert argv[8] == "Образец"
            Path(argv[9]).write_text(self.returned, encoding="utf-8", newline="")
        Path(argv[7]).write_text(result_text(argv[5], name, self.returned), encoding="utf-8")
        return subprocess.CompletedProcess(argv, 0, b"", b"")


@pytest.fixture
def isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, FakeCscript]:
    base = tmp_path / "original-base"
    (base / "nested").mkdir(parents=True)
    (base / "1Cv8.1CD").write_bytes(b"synthetic-base")
    (base / "nested" / "synthetic.txt").write_text("copy-all")
    monkeypatch.setattr(adapter, "RUNS", tmp_path / "runs")
    monkeypatch.delenv("KD3_USER", raising=False)
    monkeypatch.delenv("KD3_PASSWORD", raising=False)
    monkeypatch.setenv("KD3_BASE", str(base))
    fake = FakeCscript(base, generated(pilot()))
    monkeypatch.setattr(adapter.subprocess, "run", fake)
    return base, fake


@pytest.mark.parametrize("command", ["list", "export", "roundtrip"])
def test_commands_copy_base_and_encode_scenario(
    command: str,
    isolated: tuple[Path, FakeCscript],
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    base, fake = isolated
    source = tmp_path / "module.bsl"
    source.write_bytes(pilot().encode("utf-8"))
    snapshot = source.read_bytes()
    args = [command]
    if command == "export":
        args += ["Образец", str(tmp_path / "export.bsl")]
    elif command == "roundtrip":
        args += [str(source), "--like", "Образец", "--report", str(tmp_path / "report.json")]
    assert adapter.main(["--base", str(base), *args]) == 0
    out = capsys.readouterr().out
    assert "ИТОГ OK\nКОНЕЦ\n" in out
    assert source.read_bytes() == snapshot
    assert (base / "1Cv8.1CD").read_bytes() == b"synthetic-base"
    assert sorted(p.name for p in base.iterdir()) == ["1Cv8.1CD", "nested"]
    assert len(fake.calls) == 1
    # Копия базы после запуска удаляется, папка запуска с текстами остаётся.
    copied = Path(fake.calls[0][6])
    assert not copied.exists() and copied.parent.is_dir()
    if command == "export":
        assert (tmp_path / "export.bsl").read_bytes().decode("utf-8") == fake.returned
    if command == "roundtrip":
        assert fake.uploads == [snapshot]
        assert not Path(fake.calls[0][8]).exists()
        assert "СОСТАВ ПКО | 1 | 1 | 1" in out
        # Только строки отчёта: в пути папки запуска цифры случайные.
        assert not any("999" in line for line in out.splitlines() if "|" in line)


def test_keep_base_leaves_the_copy(isolated: tuple[Path, FakeCscript]) -> None:
    base, fake = isolated
    assert adapter.main(["--base", str(base), "list", "--keep-base"]) == 0
    assert (Path(fake.calls[0][6]) / "1Cv8.1CD").is_file()


@pytest.mark.parametrize("mode", ["preserve", "canonical"])
def test_via_writer_uses_real_server(
    mode: str,
    isolated: tuple[Path, FakeCscript],
    tmp_path: Path,
) -> None:
    _, fake = isolated
    source = tmp_path / "module.bsl"
    raw = ("\ufeff" + pilot().replace("\n", "\r\n")).encode("utf-8")
    source.write_bytes(raw)
    model = import_manager(read_manager_text(raw.decode("utf-8")), project_id="kd3-check")[0]
    expected = render(model, "preserve" if mode == "preserve" else "canonical").data
    assert adapter.main(["roundtrip", str(source), "--like", "Образец", "--via-writer", mode]) == 0
    assert fake.uploads == [expected]
    assert source.read_bytes() == raw


def test_credentials_not_in_arguments_output_or_reports(
    isolated: tuple[Path, FakeCscript],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _, fake = isolated
    secrets = ["synthetic-user-secret", 'synthetic-password-"secret']
    monkeypatch.setenv("KD3_USER", secrets[0])
    monkeypatch.setenv("KD3_PASSWORD", secrets[1])
    assert adapter.main(["list"]) == 0
    assert not any(secret in str(fake.calls) + capsys.readouterr().out for secret in secrets)
    for path in adapter.RUNS.rglob("*"):
        if path.suffix in (".js", ".tsv", ".json"):
            assert all(secret.encode("utf-8") not in path.read_bytes() for secret in secrets)
            assert all(secret.encode("utf-16-le") not in path.read_bytes() for secret in secrets)

    def failed(argv: list[str], **options: Any) -> subprocess.CompletedProcess[bytes]:
        assert not any(secret in str(argv) for secret in secrets)
        return subprocess.CompletedProcess(
            argv, 1, b"", ("Ошибка " + " / ".join(secrets)).encode("utf-16-le")
        )

    monkeypatch.setattr(adapter.subprocess, "run", failed)
    assert adapter.main(["list"]) == 1
    out = capsys.readouterr().out
    assert "<скрыто>" in out and "ИТОГ ОШИБКА" in out
    assert not any(secret in out for secret in secrets)
    report = tmp_path / "secret-report.json"
    adapter._write_report(report, {"notice": secrets[1]})
    assert secrets[1] not in report.read_text(encoding="utf-8")
    assert "secret" not in report.read_text(encoding="utf-8")


def test_missing_base_does_not_start_cscript(
    isolated: tuple[Path, FakeCscript],
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _, fake = isolated
    assert adapter.main(["list", "--base", str(tmp_path / "missing")]) == 1
    out = capsys.readouterr().out
    assert "1Cv8.1CD" in out and "ИТОГ ОШИБКА\nКОНЕЦ\n" in out
    assert not fake.calls and not adapter.RUNS.exists()


def test_roundtrip_creates_unique_conversion_each_time(
    isolated: tuple[Path, FakeCscript],
    tmp_path: Path,
) -> None:
    _, fake = isolated
    source = tmp_path / "module.bsl"
    source.write_text(pilot(), encoding="utf-8")
    for _ in range(2):
        assert adapter.main(["roundtrip", str(source), "--like", "Образец"]) == 0
    assert fake.calls[0][9] != fake.calls[1][9]
    assert Path(fake.calls[0][6]) != Path(fake.calls[1][6])


@pytest.mark.parametrize("problem", ["timeout", "exit", "stderr", "missing", "status", "command"])
def test_process_errors_are_protocol_errors(
    problem: str,
    isolated: tuple[Path, FakeCscript],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def failed(argv: list[str], **options: Any) -> subprocess.CompletedProcess[bytes]:
        if problem == "timeout":
            raise subprocess.TimeoutExpired(argv, options["timeout"])
        if problem in ("status", "command"):
            text = result_text("list", "Образец", pilot())
            text = (
                text.replace("status\tOK", "status\tERROR\nerror\tошибка 1С")
                if problem == "status"
                else text.replace("command\tlist", "command\texport")
            )
            Path(argv[7]).write_text(text, encoding="utf-8")
        code = 7 if problem == "exit" else 0
        stderr = "Ошибка платформы".encode("utf-16-le") if problem == "stderr" else b""
        return subprocess.CompletedProcess(argv, code, b"", stderr)

    monkeypatch.setattr(adapter.subprocess, "run", failed)
    assert adapter.main(["list"]) == 1
    out = capsys.readouterr().out
    assert "ИТОГ ОШИБКА\nКОНЕЦ\n" in out and "Traceback" not in out
    if problem == "timeout":
        assert "таймаут 300" in out
    if problem == "exit":
        assert "код 7" in out
    if problem == "stderr":
        assert "Ошибка платформы" in out
    if problem == "status":
        assert "ошибка 1С" in out


@pytest.mark.parametrize(
    "filename", ["writer/empty.bsl", "writer/pilot.bsl", "writer/positions.bsl", "schema/rules.bsl"]
)
def test_clean_synthetic_roundtrip(filename: str) -> None:
    text = (DATA / filename).read_text(encoding="utf-8-sig")
    report = adapter.compare_texts(text, "\ufeff" + text.replace("\n", "\r\n"), counts(text))
    assert report["status"] == "OK"
    assert not report["text_changes"] and not report["model_changes"]
    assert report["model_equal"] and report["canonical_equal"]


def test_only_generator_fields_are_masked() -> None:
    text = pilot()
    report = adapter.compare_texts(
        text, "\ufeff" + generated(text).replace("\n", "\r\n"), counts(text)
    )
    assert report["status"] == "OK" and report["equal_after_mask"]
    assert not report["model_changes"]
    body = '\nПроцедура Своя()\n\tВозврат "33333333-3333-3333-3333-333333333333";\nКонецПроцедуры\n'
    changed = body.replace("33333333", "44444444")
    changes = adapter.text_changes(text + body, generated(text) + changed)
    assert any(c.kind == "изменена строка" and c.procedure == "Своя" for c in changes)
    assert adapter.mask(text).count("<уид>") == 1


def probe() -> str:
    text = pilot()
    text = text.replace(
        "#Область ОбработчикиКонвертации",
        (
            "#Область Алгоритмы\nПроцедура СвояВАлгоритмах() Экспорт\n"
            "\tПроба = 1;\nКонецПроцедуры\n#КонецОбласти\n\n"
            "Процедура СвояВСлужебнойОбласти()\n\tПроба = 2;\nКонецПроцедуры\n\n"
            "#Область ОбработчикиКонвертации\n"
            "Процедура ПКО_Должности_ПриОтправкеДанных("
            "ДанныеИБ, ДанныеXDTO, КомпонентыОбмена, СтекВыгрузки)\n"
            "\t// Тело обработчика переносится как есть\n\tПроба = 3;\nКонецПроцедуры"
        ),
    )
    text = text.replace(
        "Процедура ДобавитьПКО_Должности(ПравилаКонвертации)\n",
        (
            "Процедура ДобавитьПКО_Должности(ПравилаКонвертации)\n"
            "\t// Комментарий описания правила\n"
        ),
    )
    return text + "\nПроцедура СвояВКонцеМодуля()\n\tПроба = 4;\nКонецПроцедуры\n"


def without_routine(text: str, name: str) -> str:
    routine = next(r for r in read_manager_text(text).routines if r.name == name)
    return text[: routine.span.char_start] + text[routine.span.char_end :]


def test_prediction_matches_actual_lost_procedures() -> None:
    text = probe()
    predicted = adapter.predict_losses(read_manager_text(text))
    assert predicted == ["СвояВСлужебнойОбласти", "СвояВКонцеМодуля"]
    returned = without_routine(without_routine(text, predicted[0]), predicted[1])
    report = adapter.compare_texts(text, returned, counts(returned))
    assert report["status"] == "РАЗЛИЧИЯ"
    assert report["actual_losses"] == predicted and report["prediction_matches"]
    assert len([c for c in report["text_changes"] if c["kind"] == "потеряна процедура"]) == 2
    assert any(
        c["action"] == "delete" and "СвояВКонцеМодуля" in c["address"]
        for c in report["model_changes"]
    )
    assert not report["notices"]


def test_description_loss_and_changed_line_are_classified() -> None:
    text = probe()
    returned = text.replace("\t// Комментарий описания правила\n", "").replace(
        '"Справочник.Должности";', '"Справочник.Другой";'
    )
    report = adapter.compare_texts(text, returned, counts(returned))
    lost = [c for c in report["text_changes"] if c["kind"] == "потеряны строки в описании правила"]
    assert any(c["procedure"] == "ДобавитьПКО_Должности" and c["count"] == 1 for c in lost)
    assert any(
        c["kind"] == "изменена строка"
        and c["procedure"] == "ДобавитьПКО_Должности"
        and c["line_before"] > 1
        for c in report["text_changes"]
    )
    assert report["status"] == "РАЗЛИЧИЯ"
    assert not report["prediction_matches"] and report["notices"]
    assert any("ПКО/Должности" in c["address"] for c in report["model_changes"])


def test_adjacent_lost_description_and_changed_property_are_separate() -> None:
    returned = pilot()
    line = next(
        line
        for line in returned.splitlines()
        if 'ДобавитьПКС(СвойстваШапки, "Наименование",' in line
    )
    modified = line.replace('"Наименование");', '"Наименование", 0, "", "urn:extra");')
    original = returned.replace(
        line, "\tОбменДаннымиXDTOСервер.СвояИнициализация();\n\n" + modified
    )
    changes = adapter.text_changes(original, returned)
    lost = [c for c in changes if c.kind == "потеряны строки в описании правила"]
    changed = [c for c in changes if c.kind == "изменена строка"]
    assert len(lost) == 1 and lost[0].count == 2
    assert len(changed) == 1 and changed[0].before == (modified[:80],)
    assert changed[0].after == (line[:80],)


def test_report_lines_are_short_and_other_differences_count() -> None:
    text = pilot()
    returned = text + "// " + "д" * 200 + "\n"
    changes = adapter.text_changes(text, returned)
    assert any(c.kind == "прочее" for c in changes)
    assert all(len(line) <= 80 for c in changes for line in (*c.before, *c.after))
    report = adapter.compare_texts(text, text, {**counts(text), "pks": 999})
    assert report["status"] == "РАЗЛИЧИЯ" and report["notices"]
    assert report["composition"]["pks"] == {"original": 2, "returned": 2, "base": 999}


@pytest.mark.parametrize("with_losses", [False, True])
def test_dry_run_never_copies_or_connects(
    with_losses: bool,
    isolated: tuple[Path, FakeCscript],
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _, fake = isolated
    module = tmp_path / "module.bsl"
    module.write_text(probe() if with_losses else pilot(), encoding="utf-8")
    assert adapter.main(["roundtrip", str(module), "--dry-run"]) == int(with_losses)
    out = capsys.readouterr().out
    assert "прогноз без базы" in out
    assert "ИТОГ " + ("РАЗЛИЧИЯ" if with_losses else "OK") in out
    assert not fake.calls and not adapter.RUNS.exists()


def test_result_format_roundtrip_and_rejects_malformed(tmp_path: Path) -> None:
    result = tmp_path / "result.tsv"
    value = "Проверка\tстроки\r\nи\\пути"
    result.write_text("protocol\t1\nstatus\tOK\necho\t" + escaped(value) + "\n", encoding="utf-8")
    assert adapter.parse_result(result)["echo"] == value
    for invalid in (
        "status\tOK\n",
        "protocol\t1\nstatus\tOK\nstatus\tOK\n",
        "protocol\t1\nstatus\tOK\necho\t\\z\n",
    ):
        result.write_text(invalid, encoding="utf-8")
        with pytest.raises(adapter.CheckError):
            adapter.parse_result(result)


@pytest.mark.skipif(sys.platform != "win32", reason="cscript доступен только в Windows")
def test_cscript_selftest_without_connection(tmp_path: Path) -> None:
    executable = shutil.which("cscript.exe")
    if executable is None:
        pytest.skip("cscript не установлен")
    script = tmp_path / "scenario.js"
    script.write_bytes(
        b"\xff\xfe" + adapter.SCENARIO.read_text(encoding="utf-8").encode("utf-16-le")
    )
    result = tmp_path / "result.tsv"
    value = "Проба\tстроки\nи\\пути"
    try:
        process = subprocess.run(
            [
                executable,
                "//nologo",
                "//E:jscript",
                "//U",
                str(script),
                "selftest",
                str(result),
                value,
            ],
            capture_output=True,
            timeout=30,
            check=False,
        )
    except PermissionError:
        pytest.skip("Запуск cscript запрещён песочницей")
    if process.returncode and b"Access is denied" in process.stdout and not result.exists():
        pytest.skip("Песочница запрещает cscript загрузить настройки WSH (Access is denied)")
    assert process.returncode == 0, process.stderr.decode("utf-16-le", errors="replace")
    assert not process.stderr
    assert not result.read_bytes().startswith(b"\xef\xbb\xbf")
    values = adapter.parse_result(result)
    assert values == {"protocol": "1", "command": "selftest", "echo": value, "status": "OK"}
