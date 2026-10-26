"""Коды выхода `scripts/publish.py` по веткам отказа — без настоящей сборки копии."""

import argparse
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import publish  # noqa: E402 — модуль скрипта, путь добавлен строкой выше


def _args(target: Path, *, no_checks: bool = True, push: bool = False) -> argparse.Namespace:
    return argparse.Namespace(message="m", target=target, push=push, no_checks=no_checks)


@pytest.fixture
def target(tmp_path: Path) -> Path:
    folder = tmp_path / "public"
    (folder / ".git").mkdir(parents=True)
    return folder


class Git:
    """Подставной git: чистое дерево, без изменений в копии; запоминает вызовы."""

    def __init__(self, porcelain: str = "", status: str = "", fail: str | None = None) -> None:
        self.calls: list[tuple[str, ...]] = []
        self.porcelain = porcelain
        self.status = status
        self.fail = fail

    def __call__(self, repo: Path, *args: str) -> str:
        self.calls.append(args)
        if self.fail is not None and args[0] == self.fail:
            raise publish.PublishStop(publish.EXIT_GIT, f"git {' '.join(args)}: сбой")
        if args[:2] == ("status", "--porcelain"):
            return self.porcelain
        if args[:2] == ("status", "--short"):
            return self.status
        return ""


@pytest.fixture
def git(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Git:
    # В открытой копии каталога publish/ нет — подставляем свой.
    config = tmp_path / "publish"
    config.mkdir()
    monkeypatch.setattr(publish, "CONFIG", config)
    fake = Git()
    monkeypatch.setattr(publish, "_git", fake)
    monkeypatch.setattr(publish, "_published_files", lambda: ["a.txt"])
    monkeypatch.setattr(publish, "_sync", lambda files, target: None)
    monkeypatch.setattr(publish, "_forbidden_hits", lambda target, files: [])
    return fake


def test_no_publish_folder_is_setup_error(
    monkeypatch: pytest.MonkeyPatch, target: Path, tmp_path: Path, git: Git
) -> None:
    monkeypatch.setattr(publish, "CONFIG", tmp_path / "нет")
    assert publish.publish(_args(target)) == publish.EXIT_SETUP
    assert git.calls == []


def test_target_not_git_is_setup_error(tmp_path: Path, git: Git) -> None:
    assert publish.publish(_args(tmp_path)) == publish.EXIT_SETUP


def test_dirty_tree_is_setup_error(target: Path, git: Git, capsys: pytest.CaptureFixture) -> None:
    git.porcelain = " M src/x.py\n"
    assert publish.publish(_args(target)) == publish.EXIT_SETUP
    assert "незакоммиченные" in capsys.readouterr().err


def test_forbidden_words_stop_with_code_1(
    monkeypatch: pytest.MonkeyPatch, target: Path, git: Git, capsys: pytest.CaptureFixture
) -> None:
    monkeypatch.setattr(publish, "_forbidden_hits", lambda target, files: ["a.txt:1: «слово»"])
    assert publish.publish(_args(target)) == publish.EXIT_FORBIDDEN
    assert ("checkout", "--", ".") in git.calls and ("clean", "-fdq") in git.calls
    assert not any(call[0] == "commit" for call in git.calls)
    assert "Запретных слов: 1" in capsys.readouterr().err


def test_failed_checks_stop_with_code_2(
    monkeypatch: pytest.MonkeyPatch, target: Path, git: Git
) -> None:
    """Настоящий `_checks`: первая команда в копии упала — коммита нет."""

    def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess:
        return subprocess.CompletedProcess(command, 1, "", "сбой")

    monkeypatch.setattr(publish.subprocess, "run", run)
    assert publish.publish(_args(target, no_checks=False)) == publish.EXIT_CHECKS
    assert not any(call[0] in ("add", "commit") for call in git.calls)


def test_git_failure_stops_with_code_4(target: Path, git: Git) -> None:
    git.fail = "add"
    assert publish.publish(_args(target)) == publish.EXIT_GIT


def test_real_git_failure_is_code_4(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Настоящий `_git`: ненулевой код git превращается в остановку с кодом 4."""

    def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess:
        return subprocess.CompletedProcess(command, 128, "", "fatal: not a git repository")

    monkeypatch.setattr(publish.subprocess, "run", run)
    with pytest.raises(publish.PublishStop) as stop:
        publish._git(tmp_path, "status")
    assert stop.value.code == publish.EXIT_GIT


@pytest.mark.parametrize(("status", "committed"), [("", False), ("A  a.txt\n", True)])
def test_success_is_code_0(target: Path, git: Git, status: str, committed: bool) -> None:
    git.status = status
    assert publish.publish(_args(target)) == publish.EXIT_OK
    assert any(call[0] == "commit" for call in git.calls) is committed
    assert not any(call[0] == "push" for call in git.calls)


def test_main_exits_with_publish_code(
    monkeypatch: pytest.MonkeyPatch, target: Path, git: Git
) -> None:
    monkeypatch.setattr(publish, "_forbidden_hits", lambda target, files: ["a.txt: путь"])
    monkeypatch.setattr(sys, "argv", ["publish.py", "-m", "m", "--target", str(target)])
    with pytest.raises(SystemExit) as exited:
        publish.main()
    assert exited.value.code == publish.EXIT_FORBIDDEN
