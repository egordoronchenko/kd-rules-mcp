"""`scripts/check_server.py`: справка не принимается за адрес; строка базы с признаком логина."""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import check_server  # noqa: E402 — скрипт из scripts/, не пакет


@pytest.mark.parametrize("flag", ["--help", "-h"])
def test_help_exits_zero(
    flag: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(sys, "argv", ["check_server.py", flag])
    with pytest.raises(SystemExit) as caught:
        check_server.main()
    assert caught.value.code == 0
    out = capsys.readouterr().out
    assert "Проверка установки" in out
    assert "url" in out


def test_server_url_from_argument_and_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Без аргумента — адрес по умолчанию, с адресом — он же, без подключения к серверу."""
    monkeypatch.setattr(check_server, "ROOT", tmp_path)
    assert check_server.server_url([]) == check_server.LocalSettings().server_url
    assert check_server.server_url(["http://127.0.0.1:9/mcp"]) == "http://127.0.0.1:9/mcp"


def test_print_projects_reports_login_and_data_server(capsys: pytest.CaptureFixture[str]) -> None:
    code = check_server.print_projects(
        {
            "projects": [
                {
                    "project": "alpha",
                    "available": True,
                    "configurations": {"full": {}},
                    "bases": {
                        "sandbox": {
                            "role": "песочница",
                            "login": True,
                            "data_mcp": "data-a",
                            "data_mcp_server": "alpha-data-a",
                        },
                        "plain": {"role": "боевая", "login": False},
                    },
                }
            ]
        }
    )
    out = capsys.readouterr().out
    assert code == 0
    assert "база sandbox (песочница): логин есть, сервер данных: alpha-data-a" in out
    assert "база plain (боевая): логин нет, сервер данных: нет" in out
