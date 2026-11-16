"""A failed staged-path derivation in coordinator-prepare-commit-msg.py never
refuses a commit: the hook writes one stderr advisory, exits 0, and the commit
lands without a path-derived Deliverable-Id.

Runs the real hook, installed as `.git/hooks/prepare-commit-msg`, under a real
`git commit` in a tmp_path repo with many dirty worktree files; a
`sitecustomize` on PYTHONPATH forces the staged-set git spawn to time out or
exit non-zero (no shell, no git shim, so it runs unchanged on every OS).
Declared: spawns the hook and real `git` — test_no_new_spawning_tests Rule 2/4.
"""
from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from types import ModuleType

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

_BIN_DIR = Path(__file__).resolve().parent.parent
_HOOK_PATH = _BIN_DIR / "coordinator-prepare-commit-msg.py"
_NO_WINDOW = {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}
_DIRTY_FILES = 300

_SITECUSTOMIZE = """
import os, subprocess, sys
_RealPopen = subprocess.Popen
_mode = os.environ.get("FORCE_STAGED_SET", "")


class _TimingOut:
    returncode = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def communicate(self, input=None, timeout=None):
        raise subprocess.TimeoutExpired("git staged set", timeout or 0)

    def kill(self):
        pass

    def wait(self, timeout=None):
        return 0


def _popen(args, *a, **kw):
    argv = [x for x in list(args) if x != "--no-optional-locks"]
    staged_set = _mode and "--cached" in argv and ("diff" in argv or "diff-index" in argv)
    if staged_set:
        if _mode == "timeout":
            return _TimingOut()
        return _RealPopen([sys.executable, "-c", "import sys; sys.exit(128)"], *a[:0], **kw)
    return _RealPopen(args, *a, **kw)


subprocess.Popen = _popen
"""


def _git(args, cwd, **kw):
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        **_NO_WINDOW,
        **kw,
    )


def _install_hook(root: Path) -> None:
    hook = root / ".git" / "hooks" / "prepare-commit-msg"
    hook.parent.mkdir(exist_ok=True)
    hook.write_text(
        f'#!/bin/sh\nexec "{Path(sys.executable).as_posix()}" "{_HOOK_PATH.as_posix()}" "$@"\n',
        encoding="utf-8",
        newline="\n",
    )
    hook.chmod(0o755)


@pytest.fixture
def repo(tmp_path: Path) -> tuple[Path, Path]:
    root = tmp_path / "repo"
    root.mkdir()
    assert _git(["init", "-q"], root).returncode == 0
    shim = tmp_path / "shim"
    shim.mkdir()
    (shim / "sitecustomize.py").write_text(_SITECUSTOMIZE, encoding="utf-8")
    return root, shim


def _dirty_worktree(root: Path) -> None:
    for i in range(_DIRTY_FILES):
        (root / f"tracked{i}.txt").write_text(f"v1 {i}\n", encoding="utf-8")
    assert _git(["add", "-A"], root).returncode == 0
    assert _git(["commit", "-q", "-m", "base"], root).returncode == 0
    for i in range(_DIRTY_FILES):
        (root / f"tracked{i}.txt").write_text(f"v2 {i}\n", encoding="utf-8")


def _commit(root: Path, shim: Path, message: str, mode: str):
    (root / "staged.txt").write_text("staged\n", encoding="utf-8")
    assert _git(["add", "staged.txt"], root).returncode == 0
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in ("GIT_DIR", "GIT_INDEX_FILE", "GIT_WORK_TREE", "CLAUDE_SESSION_ID")
    }
    env["PYTHONPATH"] = str(shim)
    env["FORCE_STAGED_SET"] = mode
    proc = _git(["commit", "-q", "-F", "-"], root, input=message, env=env, timeout=120)
    log = _git(["log", "-1", "--format=%B"], root).stdout
    return proc, log


@pytest.mark.parametrize("mode", ["timeout", "nonzero"])
def test_failed_derivation_in_a_dirty_repo_does_not_refuse_the_commit(repo, mode) -> None:
    root, shim = repo
    _install_hook(root)
    _dirty_worktree(root)
    proc, log = _commit(root, shim, "subject\n", mode)
    assert proc.returncode == 0, proc.stderr
    assert log.startswith("subject")
    assert "staged-path derivation failed" in proc.stderr
    assert "REFUSING" not in proc.stderr


@pytest.mark.parametrize("mode", ["timeout", "nonzero"])
def test_failed_derivation_with_trailer_present_keeps_the_trailer(repo, mode) -> None:
    root, shim = repo
    _install_hook(root)
    proc, log = _commit(root, shim, "subject\n\nDeliverable-Id: some-plan\n", mode)
    assert proc.returncode == 0, proc.stderr
    assert "Deliverable-Id: some-plan" in log


def test_working_derivation_in_a_dirty_repo_commits_without_advisory(repo) -> None:
    root, shim = repo
    _install_hook(root)
    _dirty_worktree(root)
    proc, log = _commit(root, shim, "subject\n", "")
    assert proc.returncode == 0, proc.stderr
    assert "staged-path derivation failed" not in proc.stderr
    assert log.startswith("subject")


def test_default_timeout_is_unchanged() -> None:
    spec = importlib.util.spec_from_file_location("pcm_staged_failure", _HOOK_PATH)
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    import inspect

    assert inspect.signature(mod._resolve_staged_paths).parameters["timeout"].default == 10.0


def test_outside_a_worktree_is_an_empty_stage(tmp_path: Path) -> None:
    outside = tmp_path / "not-a-repo"
    outside.mkdir()
    msg = outside / "MSG"
    msg.write_text("subject\n", encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if k not in ("GIT_DIR", "GIT_INDEX_FILE")}
    proc = subprocess.run(
        [sys.executable, str(_HOOK_PATH), str(msg)],
        cwd=str(outside),
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        **_NO_WINDOW,
    )
    assert proc.returncode == 0
    assert "staged-path derivation failed" not in proc.stderr


def _load_hook() -> ModuleType:
    spec = importlib.util.spec_from_file_location("pcm_mirrored_ladder", _HOOK_PATH)
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


@pytest.mark.parametrize("deliverable_id", ["", "some-plan"])
def test_mirrored_ladder_never_refuses_on_a_lost_derivation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str], deliverable_id
) -> None:
    mod = _load_hook()
    msg = tmp_path / "MSG"
    msg.write_text("subject\n", encoding="utf-8")
    monkeypatch.setattr(mod, "_ensure_claude_klabauter_on_syspath", lambda: False)
    monkeypatch.setattr(mod, "_resolve_git_dir", lambda: "")
    monkeypatch.setattr(mod, "_resolve_session_id", lambda git_dir: "")
    monkeypatch.setattr(mod, "_resolve_operator", lambda git_dir: "")
    monkeypatch.setattr(mod, "_resolve_staged_paths", lambda *a, **k: None)
    monkeypatch.setattr(mod, "_resolve_deliverable_id", lambda *a, **k: deliverable_id)
    monkeypatch.setattr(mod.subprocess, "run", lambda *a, **k: None)
    assert mod.main([str(msg)]) == 0
    assert "REFUSING" not in capsys.readouterr().err
