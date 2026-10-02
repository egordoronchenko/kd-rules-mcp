"""Токен MCP: заголовок Bearer, отказ старта вне петли, путь без внутреннего адреса сервера."""

from pathlib import Path

import pytest
from starlette.testclient import TestClient

from kd2_rules_mcp.errors import Kd2Error
from kd2_rules_mcp.server import create_app, main
from kd2_rules_mcp.service import Kd2Service, PathMap, Settings


def _service(tmp_path: Path) -> Kd2Service:
    return Kd2Service(Settings(cache_dir=tmp_path / "cache", workspace=tmp_path / "workspace"))


def test_mcp_without_token_is_not_unauthorized(tmp_path: Path) -> None:
    """Без токена middleware нет: запрос к `/mcp` не получает 401."""
    app = create_app(_service(tmp_path))
    with TestClient(app, base_url="http://127.0.0.1", raise_server_exceptions=False) as client:
        response = client.post("/mcp")
    assert response.status_code != 401


def test_mcp_bearer_token(tmp_path: Path) -> None:
    """Чужой или пустой заголовок — 401; верный Bearer до приложения доходит."""
    app = create_app(_service(tmp_path), "secret")
    with TestClient(app, base_url="http://127.0.0.1", raise_server_exceptions=False) as client:
        missing = client.post("/mcp")
        wrong = client.post("/mcp", headers={"Authorization": "Bearer other"})
        ok = client.post("/mcp", headers={"Authorization": "Bearer secret"})
    assert missing.status_code == 401
    assert missing.json() == {"error": "unauthorized"}
    assert wrong.status_code == 401
    assert wrong.json() == {"error": "unauthorized"}
    assert ok.status_code != 401


def test_settings_from_env_reads_token(tmp_path: Path) -> None:
    projects = str(tmp_path / "projects.yaml")
    assert (
        Settings.from_env({"KD2_PROJECTS_FILE": projects, "KD2_TOKEN": "секрет"}).token == "секрет"
    )
    assert Settings.from_env({"KD2_PROJECTS_FILE": projects}).token is None
    assert Settings.from_env({"KD2_PROJECTS_FILE": projects, "KD2_TOKEN": "  "}).token is None


def test_main_refuses_external_host_without_token(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Хост вне петли без токена и без KD2_IN_CONTAINER — SystemExit, uvicorn не стартует."""
    monkeypatch.delenv("KD2_IN_CONTAINER", raising=False)
    monkeypatch.delenv("KD2_TOKEN", raising=False)
    monkeypatch.setenv("KD2_HOST", "0.0.0.0")
    monkeypatch.setenv("KD2_PROJECTS_FILE", str(tmp_path / "projects.yaml"))
    ran = False

    def _run(*_args: object, **_kwargs: object) -> None:
        nonlocal ran
        ran = True

    monkeypatch.setattr("kd2_rules_mcp.server.uvicorn.run", _run)
    with pytest.raises(SystemExit, match="задайте token"):
        main()
    assert not ran


def test_main_in_container_listens_without_token(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """В контейнере KD2_HOST=0.0.0.0 без токена — нормальный старт: порт публикует compose."""
    monkeypatch.setenv("KD2_IN_CONTAINER", "1")
    monkeypatch.delenv("KD2_TOKEN", raising=False)
    monkeypatch.setenv("KD2_HOST", "0.0.0.0")
    monkeypatch.setenv("KD2_PROJECTS_FILE", str(tmp_path / "projects.yaml"))
    seen: dict[str, object] = {}

    def _run(*_args: object, **kwargs: object) -> None:
        seen.update(kwargs)

    monkeypatch.setattr("kd2_rules_mcp.server.uvicorn.run", _run)
    main()
    assert seen["host"] == "0.0.0.0"
    assert seen["port"] == 8060


def test_missing_path_hides_server_location(tmp_path: Path) -> None:
    """Ошибка чтения не содержит путь контейнера, только путь агента и подсказку project_list."""
    mounted = tmp_path / "mnt"
    mounted.mkdir()
    service = Kd2Service(
        Settings(
            cache_dir=tmp_path / "cache",
            workspace=tmp_path / "ws",
            path_map=PathMap.parse(rf"D:\Work={mounted}"),
        )
    )
    agent = r"D:\Work\нет.xml"
    with pytest.raises(Kd2Error, match="project_list") as caught:
        service._read_path(agent)
    text = str(caught.value)
    assert agent in text
    assert "на сервере" not in text
    assert str(mounted) not in text
