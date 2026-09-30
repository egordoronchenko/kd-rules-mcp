"""`kd_check.py check`: битый файл отклоняется до запуска клиента 1С (без платформы и базы КД)."""

import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
KD_SCRIPT = ROOT / "kdbase" / "kd_check.py"


def _check(path: Path) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "KD2_1CV8": str(path.parent / "нет-1cv8.exe")}
    return subprocess.run(
        [sys.executable, str(KD_SCRIPT), "check", str(path)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        timeout=120,
    )


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("<ПравилаОбмена><ВерсияФормата>2.01</ВерсияФормата>", "Ошибка разбора XML до запуска КД"),
        (
            "<ПравилаРегистрации><ВерсияФормата>2.01</ВерсияФормата></ПравилаРегистрации>",
            "ожидается ПравилаОбмена",
        ),
        ("", "Ошибка разбора XML до запуска КД"),
    ],
    ids=["обрезан-после-заголовка", "другой-корень", "пустой"],
)
def test_broken_rules_rejected_without_client(tmp_path: Path, text: str, message: str) -> None:
    rules = tmp_path / "rules.xml"
    rules.write_text(text, encoding="utf-8")
    result = _check(rules)
    assert result.returncode == 1, result.stdout + result.stderr
    assert message in result.stdout
    assert "ИТОГ ОШИБКА" in result.stdout and "КОНЕЦ" in result.stdout


def test_missing_file_rejected(tmp_path: Path) -> None:
    result = _check(tmp_path / "нет.xml")
    assert result.returncode == 1
    assert "Нет файла правил" in result.stdout
