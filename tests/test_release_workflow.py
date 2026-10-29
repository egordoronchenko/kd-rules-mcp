"""Контракт публикации образа и упаковок проверяется без Actions и сети."""

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def test_release_workflow() -> None:
    workflow = yaml.safe_load(
        (ROOT / ".github/workflows/release-image.yml").read_text(encoding="utf-8")
    )
    assert workflow["on"] == {"push": {"tags": ["v*"]}}
    assert workflow["permissions"] == {"packages": "write", "contents": "write"}
    jobs = workflow["jobs"]
    steps = jobs["image"]["steps"]
    actions = {step["uses"].split("@")[0]: step for step in steps if "uses" in step}
    assert "docker/setup-qemu-action" in actions
    assert "docker/setup-buildx-action" in actions
    login = actions["docker/login-action"]["with"]
    assert login["registry"] == "ghcr.io"
    assert login["password"] == "${{ secrets.GITHUB_TOKEN }}"
    metadata = actions["docker/metadata-action"]["with"]
    assert metadata["images"] == "ghcr.io/egordoronchenko/kd-rules-mcp"
    assert "type=match,pattern=v(.*),group=1" in metadata["tags"]
    assert "type=raw,value=latest" in metadata["tags"]
    build = actions["docker/build-push-action"]["with"]
    assert build["platforms"] == "linux/amd64,linux/arm64"
    assert build["push"] is True
    assert build["context"] == "."
    assert build["cache-from"] == "type=gha"
    assert build["cache-to"] == "type=gha,mode=max"
    assert "VERSION=" in build["build-args"]
    assert jobs["skills"]["needs"] == "image"
    packing = next(
        step["run"] for step in jobs["skills"]["steps"] if "for client" in step.get("run", "")
    )
    assert "for client in claude agents" in packing
    assert "scripts/build_packs.py --dest" in packing
    assert 'mkdir -p "$dest"' in packing
    assert '--client "$client"' in packing
    assert "kd-rules-mcp-skills-$client-$version" in packing
    assert "scripts/changelog_section.py" in packing
    release = next(
        step for step in jobs["skills"]["steps"] if step.get("uses", "").startswith("softprops/")
    )
    assert release["with"]["tag_name"] == "${{ github.ref_name }}"
    assert release["with"]["body_path"] == "${{ runner.temp }}/release-notes.md"
    assert release["with"]["files"] == "${{ runner.temp }}/kd-rules-mcp-skills-*.zip"
    for job in jobs.values():
        for step in job["steps"]:
            if "uses" in step:
                assert re.fullmatch(r"[\w/-]+@v\d+", step["uses"])


def test_checks_workflow() -> None:
    """Проверки кода на Linux при каждом push в main: те же, что требует готовность задачи."""
    workflow = yaml.safe_load((ROOT / ".github/workflows/checks.yml").read_text(encoding="utf-8"))
    assert workflow["on"] == {"push": {"branches": ["main"]}, "workflow_dispatch": None}
    assert workflow["permissions"] == {"contents": "read"}
    job = workflow["jobs"]["checks"]
    assert job["runs-on"] == "ubuntu-latest"
    commands = [step["run"] for step in job["steps"] if "run" in step]
    assert commands == [
        "uv sync --frozen",
        "uvx ruff format --check",
        "uvx ruff check",
        "uvx pyright",
        'uv run pytest -q -m "not slow"',
    ]
    for step in job["steps"]:
        if "uses" in step:
            assert re.fullmatch(r"[\w/-]+@v\d+", step["uses"])


def test_issue_templates() -> None:
    """Шаблоны issue — формы GitHub; шаблон PR отправляет в Issues."""
    templates = ROOT / ".github/ISSUE_TEMPLATE"
    for name in ("exchange-problem.yml", "idea.yml"):
        form = yaml.safe_load((templates / name).read_text(encoding="utf-8"))
        assert form["name"] and form["description"] and form["body"]
        ids = [item["id"] for item in form["body"] if "id" in item]
        assert len(ids) == len(set(ids))
    config = yaml.safe_load((templates / "config.yml").read_text(encoding="utf-8"))
    assert config["blank_issues_enabled"] is True
    pr = (ROOT / ".github/pull_request_template.md").read_text(encoding="utf-8")
    assert "issues/new/choose" in pr
