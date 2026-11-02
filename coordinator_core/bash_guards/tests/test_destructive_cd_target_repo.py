"""``destructive-rm`` / ``destructive-git-revert`` judge the repo named by a leading
``cd <dir> &&`` (optionally via an earlier ``VAR=dir;``), not the session cwd."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.bash_guards import _dc_rm
from coordinator_core.bash_guards import dispatch_checks as dc
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args], cwd=str(cwd), check=True, capture_output=True, **no_console_creationflags()
    )


def _repo(path: Path, dirty: bool) -> Path:
    (path / "sub").mkdir(parents=True)
    _git(path, "init", "-q")
    _git(path, "config", "user.email", "t@t")
    _git(path, "config", "user.name", "t")
    f = path / "sub" / "a.py"
    f.write_text("base\n", encoding="utf-8")
    _git(path, "add", "-A")
    _git(path, "commit", "-qm", "base")
    if dirty:
        f.write_text("base\nuncommitted\n", encoding="utf-8")
    return path


@pytest.fixture()
def repos(tmp_path, monkeypatch):
    monkeypatch.setattr(_dc_rm, "_rm_flush_touch", lambda *_a, **_k: None)
    live = _repo(tmp_path / "live", dirty=True)
    clone = _repo(tmp_path / "live" / "scratch" / "clone", dirty=False)
    return live, clone


def _rm(cmd: str, cwd: Path):
    return dc.check_destructive_rm(cmd, session_id="cd-target", payload={"cwd": str(cwd)})


def _revert(cmd: str, cwd: Path):
    return dc.check_destructive_git_revert(cmd, "cd-target", {"cwd": str(cwd)})


def test_rm_control_session_cwd_repo_is_denied(repos):
    live, _ = repos
    assert _rm("rm -rf sub", live) is not None


def test_rm_cd_to_clean_clone_evaluates_the_clone(repos):
    live, clone = repos
    assert _rm(f"cd {clone.as_posix()} && rm -rf sub", live) is None


def test_rm_cd_to_dirty_clone_still_denied(repos):
    live, clone = repos
    (clone / "sub" / "a.py").write_text("changed\n", encoding="utf-8")
    assert _rm(f"cd {clone.as_posix()} && rm -rf sub", live) is not None


def test_rm_var_target_resolves(repos):
    live, clone = repos
    assert _rm(f"C={clone.as_posix()}; cd $C && rm -rf sub", live) is None
    assert _rm(f"C={clone.as_posix()}; cd ${{C}} && rm -rf sub", live) is None


def test_rm_unresolved_target_keeps_session_cwd_behavior(repos):
    live, _ = repos
    assert _rm("cd $NOT_ASSIGNED_ANYWHERE && rm -rf sub", live) is not None


def test_git_control_session_cwd_repo_is_denied(repos):
    live, _ = repos
    assert _revert("git checkout -q -f FETCH_HEAD", live) is not None


def test_git_var_cd_to_clean_clone_evaluates_the_clone(repos):
    live, clone = repos
    cmd = f"C={clone.as_posix()}; cd $C && git checkout -q -f FETCH_HEAD"
    assert _revert(cmd, live) is None


def test_git_unresolved_target_keeps_session_cwd_behavior(repos):
    live, _ = repos
    assert _revert("cd $NOT_ASSIGNED_ANYWHERE && git checkout -q -f FETCH_HEAD", live) is not None
