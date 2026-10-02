"""The repo-root seam reports WHY a resolution failed, and lifecycle's callers surface it.

Purpose: `repo_root._spawn_rev_parse` used to collapse a spawn error, a timeout and a
non-zero exit into one `(False, None)`, and a walk miss carried no cause at all, so a
caller's RuntimeError could only say "not a git repo?". Every failure class is driven
through an injected seam here; nothing in this file spawns a real process.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core import lifecycle
from coordinator_core.git import repo_root as seam


@pytest.fixture(autouse=True)
def _fresh_memo():
    seam.clear_memo()
    lifecycle.git_common_dir.cache_clear()
    yield
    seam.clear_memo()
    lifecycle.git_common_dir.cache_clear()


def _no_walk_hit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(seam, "_walk_for_repo", lambda start: None)


def _forbid_spawn(monkeypatch: pytest.MonkeyPatch) -> list:
    calls: list = []

    def _boom(*args, **kwargs):
        calls.append(args[0] if args else kwargs.get("args"))
        raise AssertionError(f"unexpected spawn: {calls[-1]}")

    monkeypatch.setattr(subprocess, "run", _boom)
    monkeypatch.setattr(subprocess.Popen, "__init__", _boom)
    return calls


def _fake_run(monkeypatch: pytest.MonkeyPatch, *, raises=None, returncode=0, stderr="", stdout=""):
    def _run(cmd, **kwargs):
        if raises is not None:
            raise raises
        return subprocess.CompletedProcess(cmd, returncode, stdout=stdout, stderr=stderr)

    monkeypatch.setattr(subprocess, "run", _run)


def test_walk_miss_names_the_resolved_start_path(tmp_path, monkeypatch):
    _no_walk_hit(monkeypatch)
    resolved = str(tmp_path.resolve())

    assert seam.show_toplevel(str(tmp_path)) is None
    assert seam.last_failure_reason(str(tmp_path), seam.FORM_TOPLEVEL) == (
        f"no .git found walking up from {resolved}"
    )


@pytest.mark.parametrize(
    "form",
    [seam.FORM_TOPLEVEL, seam.FORM_GIT_DIR, seam.FORM_GIT_COMMON_DIR, seam.FORM_ABSOLUTE_GIT_DIR],
)
def test_every_walk_backed_form_reports_the_walk_miss(tmp_path, monkeypatch, form):
    _no_walk_hit(monkeypatch)

    assert "no .git found walking up from" in seam.last_failure_reason(str(tmp_path), form)


def test_walk_miss_reason_is_stated_on_a_windows_shaped_path(monkeypatch):
    windows_cwd = "C:\\Users\\someone\\proj"  # abs-path-ok: synthetic Windows-shaped fixture, never touched on disk
    monkeypatch.setattr(seam, "_resolve_cwd", lambda cwd: windows_cwd)
    _no_walk_hit(monkeypatch)

    assert seam.last_failure_reason(windows_cwd) == f"no .git found walking up from {windows_cwd}"


def test_reason_is_none_when_the_walk_finds_a_repo(tmp_path):
    (tmp_path / ".git").mkdir()

    assert seam.last_failure_reason(str(tmp_path), seam.FORM_TOPLEVEL) is None


def test_show_toplevel_spawns_nothing_on_a_miss(tmp_path, monkeypatch):
    _no_walk_hit(monkeypatch)
    calls = _forbid_spawn(monkeypatch)

    assert seam.show_toplevel(str(tmp_path)) is None
    assert seam.show_toplevel(str(tmp_path)) is None
    assert seam.last_failure_reason(str(tmp_path), seam.FORM_TOPLEVEL)
    assert calls == [], "show_toplevel must be zero-spawn on a walk miss"


def test_spawn_oserror_is_reported_as_spawn_error(tmp_path, monkeypatch):
    _fake_run(monkeypatch, raises=FileNotFoundError("git"))

    assert seam.show_prefix(str(tmp_path)) is None
    reason = seam.last_failure_reason(str(tmp_path), seam.FORM_SHOW_PREFIX)
    assert reason.startswith("spawn-error: FileNotFoundError")


def test_spawn_timeout_is_reported_as_timeout(tmp_path, monkeypatch):
    _fake_run(monkeypatch, raises=subprocess.TimeoutExpired(["git"], 2.0))

    assert seam.is_inside_work_tree(str(tmp_path)) is False
    reason = seam.last_failure_reason(str(tmp_path), seam.FORM_IS_INSIDE_WORK_TREE)
    assert reason.startswith("timeout after ")


def test_spawn_exit_128_carries_the_first_stderr_line(tmp_path, monkeypatch):
    _fake_run(
        monkeypatch,
        returncode=128,
        stderr="fatal: not a git repository (or any of the parent directories): .git\nsecond line\n",
    )

    assert seam.show_prefix(str(tmp_path)) is None
    assert seam.last_failure_reason(str(tmp_path), seam.FORM_SHOW_PREFIX) == (
        "exit-128: fatal: not a git repository (or any of the parent directories): .git"
    )


def test_a_failed_spawn_is_still_never_memoized_and_a_later_success_clears_the_reason(
    tmp_path, monkeypatch
):
    _fake_run(monkeypatch, returncode=128, stderr="fatal: nope\n")
    assert seam.show_prefix(str(tmp_path)) is None
    assert seam.last_failure_reason(str(tmp_path), seam.FORM_SHOW_PREFIX)

    _fake_run(monkeypatch, returncode=0, stdout="sub/\n")
    assert seam.show_prefix(str(tmp_path)) == "sub/"
    assert seam.last_failure_reason(str(tmp_path), seam.FORM_SHOW_PREFIX) is None


def test_lifecycle_find_repo_root_error_names_the_walk_start(tmp_path, monkeypatch):
    _no_walk_hit(monkeypatch)
    resolved = str(tmp_path.resolve())

    with pytest.raises(RuntimeError) as excinfo:
        lifecycle.find_repo_root(str(tmp_path))

    message = str(excinfo.value)
    assert message.startswith("git rev-parse --show-toplevel failed (not a git repo?)")
    assert f"no .git found walking up from {resolved}" in message


def test_lifecycle_git_common_dir_error_keeps_its_phrase_and_adds_the_reason(
    tmp_path, monkeypatch
):
    _no_walk_hit(monkeypatch)
    repo = Path(tmp_path) / "no-repo-here"

    with pytest.raises(RuntimeError) as excinfo:
        lifecycle.git_common_dir(repo)

    message = str(excinfo.value)
    assert "not a git repository (or any of the parent directories)" in message
    assert "no .git found walking up from" in message
