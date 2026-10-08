"""Setup образа и локальный скрипт дают одинаковые файлы; Docker не требуется."""

import os
from pathlib import Path

import pytest
import yaml

from scripts import setup_local

ROOT = Path(__file__).resolve().parents[1]
OUTPUTS = (".env", "docker-compose.override.yml", ".mcp.json", ".cursor/mcp.json")


@pytest.mark.parametrize("image_tag", [None, "1.2.3"])
def test_image_setup_matches_local_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, image_tag: str | None
) -> None:
    catalog = yaml.safe_load((ROOT / "projects.example.yaml").read_text(encoding="utf-8"))
    settings = yaml.safe_load((ROOT / "projects.local.example.yaml").read_text(encoding="utf-8"))
    settings["projects"] = {}
    settings["port"] = 8061
    settings["server_url"] = "http://localhost:8061/mcp"
    if image_tag is not None:
        settings["image_tag"] = image_tag
    for project_id in catalog["projects"]:
        project = tmp_path / "projects" / project_id
        project.mkdir(parents=True)
        (project / ".mcp.json").write_bytes(b'{"mcpServers":{}}\n')
        if project_id == "bp":
            (project / "ПравилаОбмена").mkdir()
            (project / "src/cfe/МоёРасширение").mkdir(parents=True)
        settings["projects"][project_id] = str(project)
    local = tmp_path / "local"
    image = tmp_path / "image"
    for folder in (local, image):
        folder.mkdir()
        (folder / "projects.yaml").write_bytes(yaml.safe_dump(catalog, allow_unicode=True).encode())
        (folder / "projects.local.yaml").write_bytes(
            yaml.safe_dump(settings, allow_unicode=True).encode()
        )
    monkeypatch.setattr(setup_local, "ROOT", local)
    monkeypatch.setattr(setup_local, "legacy_runtime_notice", lambda: None)
    setup_local.main(["--root", str(local)])
    setup_local.main(["--root", str(image), "--host-paths"])
    for name in OUTPUTS:
        assert (local / name).read_bytes() == (image / name).read_bytes()
        assert b"\r" not in (image / name).read_bytes()
    text = (image / ".env").read_text(encoding="utf-8")
    assert ("KD_IMAGE_TAG=" in text) == (image_tag is not None)
    if image_tag:
        assert f"KD_IMAGE_TAG={image_tag}\n" in text


def test_image_setup_preserves_inaccessible_host_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "projects.yaml").write_bytes((ROOT / "projects.example.yaml").read_bytes())
    (tmp_path / "projects.local.yaml").write_bytes(
        (ROOT / "projects.local.example.yaml").read_bytes()
    )
    monkeypatch.setattr(setup_local, "ROOT", tmp_path)
    monkeypatch.setattr(os, "geteuid", lambda: 0, raising=False)
    setup_local.main(["--root", str(tmp_path), "--host-paths"])
    service = yaml.safe_load((tmp_path / "docker-compose.override.yml").read_bytes())["services"][
        "kd-rules-mcp"
    ]
    assert "D:/Repos/bp:/projects/bp:ro" in service["volumes"]
    assert "D:/Repos/bp/ПравилаОбмена:/rules/bp" in service["volumes"]
    assert "D:/Repos/bp/src/cfe/МоёРасширение:/extensions/bp.full.0" in service["volumes"]
    sources = [
        entry.partition("=")[0] for entry in service["environment"]["KD2_PATH_MAP"].split(";")
    ]
    assert all(not source.startswith("/work") for source in sources)
    assert "root" in capsys.readouterr().out


def test_explicit_user_does_not_get_root_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "projects.yaml").write_bytes((ROOT / "projects.example.yaml").read_bytes())
    (tmp_path / "projects.local.yaml").write_bytes(b"projects: {}\n")
    monkeypatch.setattr(setup_local, "ROOT", tmp_path)
    monkeypatch.setattr(os, "geteuid", lambda: 1001, raising=False)
    setup_local.main(["--root", str(tmp_path), "--host-paths"])
    assert "root" not in capsys.readouterr().out
    assert not (tmp_path / ".env").exists()


@pytest.mark.parametrize("tag", ["", "${TOKEN}", "one\ntwo", "v/1.0", "a" * 129])
def test_invalid_image_tag_is_rejected(tmp_path: Path, tag: str) -> None:
    path = tmp_path / "projects.local.yaml"
    path.write_bytes(yaml.safe_dump({"image_tag": tag}).encode())
    with pytest.raises(SystemExit):
        setup_local.image_tag_from_file(path)


def test_docker_entrypoint_dispatches_setup_before_server_privileges() -> None:
    text = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "python /app/scripts/setup_local.py --root /work --host-paths" in text
    assert text.index("= setup ]") < text.index("chown kd2:kd2 /data/cache'")
    assert "scripts/check_server.py" in text
    assert "scripts/build_packs.py scripts/our_skills.py" in text
    assert "org.opencontainers.image.version" in text


def test_project_mount_reads_mcp_without_changing_host_volumes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "projects.yaml").write_bytes((ROOT / "projects.example.yaml").read_bytes())
    (tmp_path / "projects.local.yaml").write_bytes(
        (ROOT / "projects.local.example.yaml").read_bytes()
    )
    mounted = tmp_path / "inputs/bp"
    mounted.mkdir(parents=True)
    (mounted / ".mcp.json").write_bytes(
        b'{"mcpServers":{"1c-code-metadata-mcp":{"url":"http://example.test/code"}}}\n'
    )
    monkeypatch.setattr(setup_local, "ROOT", tmp_path)
    setup_local.main(["--root", str(tmp_path), "--host-paths", "--project-mount", f"bp={mounted}"])
    mcp = yaml.safe_load((tmp_path / ".mcp.json").read_bytes())
    assert mcp["mcpServers"]["bp-1c-code-metadata-mcp"]["url"] == "http://example.test/code"
    override = (tmp_path / "docker-compose.override.yml").read_text(encoding="utf-8")
    assert "D:/Repos/bp:/projects/bp:ro" in override
    assert str(mounted) not in override
