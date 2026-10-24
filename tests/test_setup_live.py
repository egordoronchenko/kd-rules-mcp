"""Настройки адресов сервера данных и абсолютные пути в override."""

import json
from pathlib import Path, PureWindowsPath

import pytest
import yaml

from kd_rules_mcp.projects import LocalSettings, load_catalog
from scripts import setup_local


def machine(tmp_path, urls):
    servers = {f"data{index}": {"url": url} for index, url in enumerate(urls)}
    (tmp_path / ".mcp.json").write_text(json.dumps({"mcpServers": servers}), encoding="utf-8")
    bases = {
        f"b{index}": {
            "role": "песочница",
            "configuration": "full",
            "connection": "File=x;",
            "data_mcp": name,
        }
        for index, name in enumerate(servers)
    }
    (tmp_path / "projects.yaml").write_text(
        yaml.safe_dump(
            {
                "projects": {
                    "alpha": {
                        "mcp_config": ".mcp.json",
                        "configurations": {"full": {"dump": "main"}},
                        "bases": bases,
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    return load_catalog(tmp_path / "projects.yaml"), LocalSettings(project_dirs={"alpha": tmp_path})


def test_b1_setup_maps_only_machine_hosts(tmp_path, monkeypatch):
    catalog, local = machine(
        tmp_path,
        [
            "http://192.0.2.10:8081/base",
            "http://198.51.100.20/foreign",
            "http://localhost:80/base",
            "http://127.0.0.1/base",
        ],
    )
    monkeypatch.setattr(
        setup_local.socket, "getaddrinfo", lambda *args: [(2, 1, 6, "", ("192.0.2.10", 0))]
    )
    environment = yaml.safe_load(setup_local.compose_override(catalog, local))["services"][
        "kd-rules-mcp"
    ]["environment"]
    assert environment["KD2_HOST_MAP"] == (
        "127.0.0.1=host.docker.internal;192.0.2.10=host.docker.internal;localhost=host.docker.internal"
    )


def test_b1_setup_omits_map_for_foreign_host(tmp_path, monkeypatch):
    catalog, local = machine(tmp_path, ["http://198.51.100.20/base"])
    monkeypatch.setattr(setup_local.socket, "getaddrinfo", lambda *args: [])
    environment = yaml.safe_load(setup_local.compose_override(catalog, local))["services"][
        "kd-rules-mcp"
    ]["environment"]
    assert "KD2_HOST_MAP" not in environment


def test_b1_image_setup_reads_host_map_from_project_mount(tmp_path, monkeypatch):
    machine(tmp_path, ["http://localhost:8081/base"])
    (tmp_path / "projects.local.yaml").write_text(
        yaml.safe_dump({"projects": {"alpha": "D:/Repos/alpha"}, "host_addresses": ["localhost"]}),
        encoding="utf-8",
    )
    monkeypatch.setattr(setup_local.socket, "getaddrinfo", lambda *args: [])
    setup_local.main(
        ["--root", str(tmp_path), "--host-paths", "--project-mount", f"alpha={tmp_path}"]
    )
    environment = yaml.safe_load(
        (tmp_path / "docker-compose.override.yml").read_text(encoding="utf-8")
    )["services"]["kd-rules-mcp"]["environment"]
    assert environment["KD2_HOST_MAP"] == "localhost=host.docker.internal"


def test_b1_clone_combines_detected_and_explicit_addresses(tmp_path, monkeypatch):
    from dataclasses import replace

    catalog, local = machine(tmp_path, ["http://192.0.2.10/base", "http://192.0.2.20/base"])
    monkeypatch.setattr(
        setup_local.socket, "getaddrinfo", lambda *args: [(2, 1, 6, "", ("192.0.2.10", 0))]
    )
    local = replace(local, host_addresses=("192.0.2.20",))
    environment = yaml.safe_load(setup_local.compose_override(catalog, local))["services"][
        "kd-rules-mcp"
    ]["environment"]
    assert (
        environment["KD2_HOST_MAP"]
        == "192.0.2.10=host.docker.internal;192.0.2.20=host.docker.internal"
    )


@pytest.mark.parametrize("configured", [False, True])
def test_b1_image_uses_only_explicit_addresses(tmp_path, monkeypatch, capsys, configured):
    machine(tmp_path, ["http://192.0.2.10:8081/base?token=private-value"])
    settings: dict[str, object] = {"projects": {"alpha": "D:/Repos/alpha"}}
    if configured:
        settings["host_addresses"] = ["192.0.2.10"]
    (tmp_path / "projects.local.yaml").write_text(yaml.safe_dump(settings), encoding="utf-8")
    monkeypatch.setattr(
        setup_local.socket, "getaddrinfo", lambda *args: pytest.fail("адреса контейнера не нужны")
    )
    monkeypatch.setattr(
        setup_local.socket, "gethostname", lambda: pytest.fail("имя контейнера не нужно")
    )
    setup_local.main(
        ["--root", str(tmp_path), "--host-paths", "--project-mount", f"alpha={tmp_path}"]
    )
    environment = yaml.safe_load(
        (tmp_path / "docker-compose.override.yml").read_text(encoding="utf-8")
    )["services"]["kd-rules-mcp"]["environment"]
    output = capsys.readouterr().out
    warning = "сервер данных 192.0.2.10 — если это адрес этой машины, добавьте его в host_addresses"
    if configured:
        assert environment["KD2_HOST_MAP"] == "192.0.2.10=host.docker.internal"
        assert warning not in output
    else:
        assert "KD2_HOST_MAP" not in environment
        assert output.count(warning) == 1
    assert "private-value" not in output


@pytest.mark.parametrize("workspace", [None, "relative", "D:/Rules/workspace"])
def test_c1_image_omits_unknown_paths(tmp_path, monkeypatch, capsys, workspace):
    machine(tmp_path, [])
    settings: dict[str, object] = {"projects": {}}
    if workspace is not None:
        settings["workspace"] = workspace
    (tmp_path / "projects.local.yaml").write_text(yaml.safe_dump(settings), encoding="utf-8")
    setup_local.main(["--root", str(tmp_path), "--host-paths"])
    environment = yaml.safe_load(
        (tmp_path / "docker-compose.override.yml").read_text(encoding="utf-8")
    )["services"]["kd-rules-mcp"]["environment"]
    pairs = dict(item.split("=", 1) for item in environment["KD2_PATH_MAP"].split(";") if item)
    output = capsys.readouterr().out
    if workspace == "D:/Rules/workspace":
        assert pairs == {str(Path(workspace)): "/data/workspace"}
        assert "абсолютный workspace" not in output
    else:
        assert pairs == {}
        assert output.count("задайте абсолютный workspace") == 1
    assert "/structures" not in pairs.values()
    assert all(
        Path(source).is_absolute() or PureWindowsPath(source).is_absolute() for source in pairs
    )


@pytest.mark.parametrize("workspace", [None, Path("custom-workspace")])
def test_c1_path_map_has_only_absolute_host_sources(tmp_path, monkeypatch, workspace):
    monkeypatch.setattr(setup_local, "ROOT", tmp_path)
    local = LocalSettings(workspace=workspace)
    catalog = machine(tmp_path, [])[0]
    environment = yaml.safe_load(setup_local.compose_override(catalog, local))["services"][
        "kd-rules-mcp"
    ]["environment"]
    pairs = dict(item.split("=", 1) for item in environment["KD2_PATH_MAP"].split(";"))
    assert str(tmp_path / (workspace or "workspace")) in pairs
    assert pairs[str(tmp_path / "structures")] == "/structures"
    assert all(
        Path(source).is_absolute() or PureWindowsPath(source).is_absolute() for source in pairs
    )
