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
