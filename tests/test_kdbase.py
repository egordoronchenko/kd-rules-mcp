"""Сверка через базы 1С: штатная загрузка принимает реальный макет, испорченный — нет.

Задача 8.1 — база КД (`kdbase/kd_check.py`): тесты запускают толстый клиент 1С, поэтому
выполняются только с `KD2_KDBASE_CHECK=1` на машине с платформой и подготовленной базой
(`kd_check.py prepare`). Макет — первый файл `ПравилаОбмена` корпуса (`KD2_CORPUS_DIRS`).

Задача 8.2 — типовая БСП (`kdbase/bsp_check.py`): дополнительно нужны `KD2_BSP_PROJECT` и
`KD2_BSP_BASE` (проект и база-песочница из каталога проектов) и `KD2_BSP_PLAN` — план обмена,
макеты `ПравилаОбмена` и `ПравилаОбменаКорреспондента` которого есть в корпусе.
`bsp_check.require_login` проверяется без базы и без `KD2_KDBASE_CHECK`.
"""

import os
import re
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

from kd2_rules_mcp.authoring.pack import pack_rules
from kd2_rules_mcp.kd2.canonical import parse_xml
from tests.corpus import CORPUS_ENV, CorpusFile, corpus_files

ROOT = Path(__file__).resolve().parents[1]
KD_SCRIPT = ROOT / "kdbase" / "kd_check.py"
BSP_SCRIPT = ROOT / "kdbase" / "bsp_check.py"
sys.path.insert(0, str(ROOT / "kdbase"))

import bsp_check  # noqa: E402 — скрипт из kdbase/, не пакет
import bsp_load  # noqa: E402 — скрипт из kdbase/, не пакет
import exchange_check  # noqa: E402 — скрипт из kdbase/, не пакет
import kd_check  # noqa: E402 — скрипт из kdbase/, не пакет

_needs_base = pytest.mark.skipif(
    os.environ.get("KD2_KDBASE_CHECK") != "1",
    reason="сверка через базы 1С включается KD2_KDBASE_CHECK=1",
)


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    return subprocess.run(
        [sys.executable, *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        timeout=600,
    )


def _template() -> Path:
    """Первый макет `ПравилаОбмена` корпуса; нет корпуса — пропуск."""
    files = corpus_files(("ПравилаОбмена",))
    if not files:
        pytest.skip(f"Нет макетов ПравилаОбмена: задайте {CORPUS_ENV}")
    return files[0].path


@pytest.mark.kdbase
@_needs_base
def test_real_template_is_accepted() -> None:
    template = _template()
    expected = len(parse_xml(template.read_bytes()).findall("ПравилаКонвертацииОбъектов//Правило"))
    result = _run(str(KD_SCRIPT), "check", str(template))
    assert result.returncode == 0, result.stdout + result.stderr
    assert "ИТОГ OK" in result.stdout
    assert f"КОЛИЧЕСТВО ПравилаКонвертацииОбъектов {expected}" in result.stdout


@pytest.mark.kdbase
@_needs_base
def test_broken_file_is_rejected_with_protocol_error(tmp_path: Path) -> None:
    broken = tmp_path / "broken.xml"
    broken.write_bytes(_template().read_bytes()[:30000])
    result = _run(str(KD_SCRIPT), "check", str(broken))
    assert result.returncode == 1
    assert "ОШИБКА" in result.stdout and "Ошибка разбора XML" in result.stdout
    assert "ИТОГ ОШИБКА" in result.stdout


@pytest.mark.kdbase
@_needs_base
def test_rules_without_source_and_receiver_are_rejected(tmp_path: Path) -> None:
    """Правильный XML, но конвертация пустая: КД читает его без ошибки, обработка — отклоняет."""
    empty = tmp_path / "empty.xml"
    empty.write_text(
        "<ПравилаОбмена><ВерсияФормата>2.01</ВерсияФормата></ПравилаОбмена>", encoding="utf-8"
    )
    result = _run(str(KD_SCRIPT), "check", str(empty))
    assert result.returncode == 1, result.stdout + result.stderr
    assert "Правила не прочитаны целиком" in result.stdout
    assert "ИТОГ ОШИБКА" in result.stdout


def _bsp_pair() -> tuple[str, str, str, CorpusFile, CorpusFile]:
    """Проект, база, план и пара макетов для `bsp_check`; чего-то нет — пропуск."""
    names = ("KD2_BSP_PROJECT", "KD2_BSP_BASE", "KD2_BSP_PLAN")
    values = [os.environ.get(name, "").strip() for name in names]
    if not all(values):
        pytest.skip(f"Проверка БСП включается переменными {', '.join(names)}")
    project, base, plan = values
    found = {
        item.kind: item
        for item in corpus_files(("ПравилаОбмена", "ПравилаОбменаКорреспондента"))
        if item.exchange_plan == plan
    }
    if len(found) < 2:
        pytest.skip(f"В корпусе ({CORPUS_ENV}) нет пары макетов плана обмена {plan}")
    return project, base, plan, found["ПравилаОбмена"], found["ПравилаОбменаКорреспондента"]


@pytest.mark.kdbase
@_needs_base
def test_bsp_accepts_packed_archive_and_rejects_wrong_names(tmp_path: Path) -> None:
    """Архив `rules_pack` (два файла) БСП принимает; архив с чужими именами — нет."""
    project, base, plan, rules, correspondent = _bsp_pair()
    files = {"exchange": rules.path, "correspondent": correspondent.path}
    packed = pack_rules(files, tmp_path / "rules.zip")
    wrong = tmp_path / "wrong.zip"
    with zipfile.ZipFile(wrong, "w") as archive:
        archive.write(rules.path, "Rules.xml")
        archive.write(correspondent.path, "CorrespondentExchangeRules.xml")
    common = ["--plan", plan, "--project", project, "--base", base]
    accepted = _run(str(BSP_SCRIPT), "--archive", str(packed.path), *common)
    rejected = _run(str(BSP_SCRIPT), "--archive", str(wrong), *common)
    assert accepted.returncode == 0, accepted.stdout + accepted.stderr
    assert "ИТОГ OK" in accepted.stdout
    assert rejected.returncode == 1 and "ИТОГ ОШИБКА" in rejected.stdout


@pytest.mark.kdbase
@_needs_base
def test_bsp_accepts_real_rules_pair() -> None:
    project, base, plan, rules, correspondent = _bsp_pair()
    result = _run(
        str(BSP_SCRIPT),
        str(rules.path),
        str(correspondent.path),
        "--plan",
        plan,
        "--project",
        project,
        "--base",
        base,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "ИТОГ OK" in result.stdout


def test_check_without_base_prints_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Нет `1Cv8.1CD` — протокол с ошибкой, без трассировки и без запуска 1С."""
    rules = tmp_path / "rules.xml"
    rules.write_text(
        "<ПравилаОбмена><ВерсияФормата>2.01</ВерсияФормата></ПравилаОбмена>",
        encoding="utf-8",
    )
    empty = tmp_path / "empty-base"
    empty.mkdir()
    monkeypatch.setattr(kd_check, "BASE", empty)
    monkeypatch.setattr(kd_check, "RUNS", tmp_path / "run")
    code = kd_check.check(rules)
    out = capsys.readouterr().out
    assert code == 1
    assert (
        "ОШИБКА нет базы КД: создайте файловую базу «Конвертация данных 2.1» "
        "в base\\ и выполните kd_check.py prepare"
    ) in out
    assert "ИТОГ ОШИБКА" in out
    assert out.rstrip().endswith("КОНЕЦ")
    assert "Traceback" not in out
    assert not (tmp_path / "run").exists()


def test_require_login_refuses_client_server_without_user(monkeypatch: pytest.MonkeyPatch) -> None:
    """`Srvr=` без `Usr=` и без `KD2_BSP_USER` — отказ; строка с `Usr=` проходит дальше."""
    monkeypatch.delenv("KD2_BSP_USER", raising=False)
    server = 'Srvr="x";Ref="y";'
    with pytest.raises(SystemExit, match="логин не задан") as caught:
        bsp_check.require_login(server)
    message = str(caught.value)
    assert server in message
    assert "projects.local.yaml" in message
    assert "клиент-серверной" in message
    bsp_check.require_login('Srvr="x";Ref="y";Usr="u";')
    bsp_check.require_login('File="C:/base";')
    monkeypatch.setenv("KD2_BSP_USER", "agent")
    bsp_check.require_login(server)


def _projects(tmp_path: Path) -> Path:
    """Песочница с `data_mcp`, песочница без него и боевая; строки `File=`, без `.mcp.json`."""
    (tmp_path / "projects.yaml").write_text(
        """
projects:
  alpha:
    name: Альфа
    mcp_config: .mcp.json
    configurations: {full: {dump: main}}
    bases:
      sandbox:
        {role: песочница, configuration: full, connection: 'File="C:/sb";', data_mcp: data-a}
      nodata: {role: песочница, configuration: full, connection: 'File="C:/nd";'}
      prod: {role: боевая, configuration: full, connection: 'File="C:/pr";', data_mcp: data-a}
""",
        encoding="utf-8",
    )
    return tmp_path


def test_resolve_transport_reads_only_projects(tmp_path: Path) -> None:
    root = _projects(tmp_path)
    assert bsp_check.resolve_transport("alpha", "sandbox", None, root) == "data"
    assert bsp_check.resolve_transport("alpha", "sandbox", "com", root) == "com"
    assert bsp_check.resolve_transport("alpha", "sandbox", "data", root) == "data"
    assert bsp_check.resolve_transport("alpha", "nodata", None, root) == "com"
    with pytest.raises(SystemExit, match="data_mcp") as caught:
        bsp_check.resolve_transport("alpha", "nodata", "data", root)
    assert "alpha.nodata" in str(caught.value)
    for via in (None, "data"):
        with pytest.raises(SystemExit, match="только к песочницам"):
            bsp_check.resolve_transport("alpha", "prod", via, root)


@pytest.fixture
def routes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, int]:
    """`main` с подменёнными ветками загрузки; каталог — `_projects`."""
    monkeypatch.setattr(bsp_check, "ROOT", _projects(tmp_path))
    seen = {"com": 0, "data": 0}

    def check_com(archive: Path, plan: str, connection: str) -> int:
        seen["com"] += 1
        return 0

    def check_data(archive: Path, plan: str, server: bsp_load.DataServer) -> int:
        seen["data"] += 1
        return 0

    def server_for(ref: str, root: Path = tmp_path) -> bsp_load.DataServer:
        return bsp_load.DataServer(ref, "http://stub", {})

    monkeypatch.setattr(bsp_check, "check_com", check_com)
    monkeypatch.setattr(bsp_check, "check_data", check_data)
    monkeypatch.setattr(bsp_check, "server_for", server_for)
    return seen


def _main(monkeypatch: pytest.MonkeyPatch, argv: list[str]) -> SystemExit:
    monkeypatch.setattr(sys, "argv", ["bsp_check", *argv])
    with pytest.raises(SystemExit) as caught:
        bsp_check.main()
    return caught.value


def test_main_picks_transport(
    tmp_path: Path, routes: dict[str, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    def go(*args: str) -> SystemExit:
        routes["com"] = 0
        routes["data"] = 0
        return _main(monkeypatch, ["--archive", str(tmp_path / "a.zip"), "--plan", "План", *args])

    assert go("--project", "alpha", "--base", "sandbox").code == 0
    assert routes == {"com": 0, "data": 1}
    assert go("--project", "alpha", "--base", "nodata").code == 0
    assert routes == {"com": 1, "data": 0}
    assert go("--project", "alpha", "--base", "sandbox", "--via", "com").code == 0
    assert routes == {"com": 1, "data": 0}
    refused = go("--project", "alpha", "--base", "nodata", "--via", "data")
    assert routes == {"com": 0, "data": 0}
    assert "nodata" in str(refused) and "data_mcp" in str(refused)
    assert go("--connection", 'File="C:/base";').code == 0
    assert routes == {"com": 1, "data": 0}
    refused = go(
        "--connection",
        'File="C:/base";',
        "--via",
        "data",
        "--project",
        "alpha",
        "--base",
        "sandbox",
    )
    assert routes == {"com": 0, "data": 0}
    assert "data_mcp" in str(refused)


def test_load_rules_code_write_and_report_branches() -> None:
    plan, archive, name = "План", b"PK\x03\x04", "rules.zip"
    written = bsp_load.load_rules_code(plan, archive, name, write=True)
    assert exchange_check.load_rules(plan, archive, name) == written
    assert ".Записать()" in written
    assert "ЗагрузитьКомплектПравил(Отказ, Данные, Описание, Адрес, " in written
    quiet = bsp_load.load_rules_code(plan, archive, name, write=False)
    pair = bsp_load.load_rules_code(plan, archive, name, write=False, full_set=False)
    for code in (written, quiet, pair):
        assert "\n" not in code
        assert code.startswith("Попытка ") and code.endswith("КонецПопытки;")
        assert not re.search(r"Новый \w+\([^;]*?\)\s*\.", code)
    assert ".Записать()" not in quiet and ".Записать()" not in pair
    assert "ЗагрузитьКомплектПравил" in quiet
    assert "ЗагрузитьПравила(Отказ, Записи[0], Адрес, " in pair
    assert re.search(r"ЗагрузитьПравила\([^;]*?, Истина\)", pair)
    assert "ЗагрузитьКомплектПравил" not in pair
    with pytest.raises(ValueError):
        bsp_load.load_rules_code(plan, archive, name, write=True, full_set=False)


_INFO = "строка1\nстрока2"
_MESSAGE = "ошибка\nразбора"
_EXAMPLE = "\n".join(["OK", "R", "2", "1", "15", _INFO, "0", "0", "", "M", "1", "14", _MESSAGE])


def test_parse_load_report_reads_lengths_not_lines() -> None:
    assert len(_INFO) == 15 and len(_MESSAGE) == 14
    assert "\n0\n\nM\n" in _EXAMPLE
    assert bsp_load.parse_load_report(_EXAMPLE) == bsp_load.LoadReport(
        (True, False), (_INFO, ""), (_MESSAGE,)
    )
    ok = "\n".join(["OK", "R", "2", "1", "0", "", "1", "0", "", "M", "0"])
    assert bsp_load.parse_load_report(ok) == bsp_load.LoadReport((True, True), ("", ""), ())
    with pytest.raises(ValueError):
        bsp_load.parse_load_report("ОШИБКА нет")
    with pytest.raises(ValueError):
        bsp_load.parse_load_report(_EXAMPLE + "\nхвост")
    with pytest.raises(ValueError):
        bsp_load.parse_load_report("OK\nR\n1\n1\n5\nаб")


class _Scripted(bsp_load.DataServer):
    """Сервер данных с ответами по очереди (без HTTP)."""

    def __init__(self, label: str, replies: list[str]) -> None:
        super().__init__(label, "", {})
        object.__setattr__(self, "_replies", replies)

    def call(self, code: str) -> str:
        return self._replies.pop(0)  # type: ignore[attr-defined]  # задан в __init__


def _rules_zip(path: Path) -> Path:
    with zipfile.ZipFile(path, "w") as archive:
        for name in (
            "ExchangeRules.xml",
            "CorrespondentExchangeRules.xml",
            "RegistrationRules.xml",
        ):
            archive.writestr(name, b"x")
    return path


def test_check_data_prints_protocol(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    archive = _rules_zip(tmp_path / "rules.zip")
    code = bsp_check.check_data(archive, "План", _Scripted("alpha.sandbox", [_EXAMPLE]))
    out = capsys.readouterr().out
    assert code == 1
    assert "ТРАНСПОРТ сервер данных" in out
    assert "ЗАГРУЗКА ЗагрузитьКомплектПравил" in out
    assert "ИНФОРМАЦИЯ строка1 | строка2" in out
    assert out.count("ИНФОРМАЦИЯ") == 1
    assert "СООБЩЕНИЕ ошибка разбора" in out
    assert "ИТОГ ОШИБКА" in out
    assert not any(line.startswith("ОШИБКА ") for line in out.splitlines())

    ok = "\n".join(["OK", "R", "2", "1", "0", "", "1", "0", "", "M", "0"])
    code = bsp_check.check_data(archive, "План", _Scripted("alpha.sandbox", [ok]))
    out = capsys.readouterr().out
    assert code == 0
    assert "ИТОГ OK" in out
    assert "ОШИБКА" not in out
    assert "ИНФОРМАЦИЯ" not in out
    assert "СООБЩЕНИЕ" not in out


def test_check_data_reports_call_failures(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    archive = _rules_zip(tmp_path / "rules.zip")
    failed = "ОШИБКА Ошибка разбора\n{Модуль(1)}:ВызватьИсключение"
    code = bsp_check.check_data(archive, "План", _Scripted("alpha.sandbox", [failed]))
    out = capsys.readouterr().out
    assert code == 1
    assert "ОШИБКА Ошибка разбора" in out
    assert "{Модуль" not in out
    assert "ИТОГ ОШИБКА" in out
    assert "ИНФОРМАЦИЯ" not in out

    code = bsp_check.check_data(archive, "План", _Scripted("alpha.sandbox", [""]))
    out = capsys.readouterr().out
    assert code == 1
    assert "alpha.sandbox: пустой ответ сервера данных" in out
    assert "vcexecutecode" in out

    class _Down(bsp_load.DataServer):
        def __init__(self) -> None:
            super().__init__("alpha.sandbox", "", {})

        def call(self, code: str) -> str:
            raise bsp_load.ExchangeCheckError("alpha.sandbox: сервер данных недоступен")

    code = bsp_check.check_data(archive, "План", _Down())
    out = capsys.readouterr().out
    assert code == 1
    assert "ОШИБКА alpha.sandbox: сервер данных недоступен" in out
    assert "ТРАНСПОРТ сервер данных" in out
