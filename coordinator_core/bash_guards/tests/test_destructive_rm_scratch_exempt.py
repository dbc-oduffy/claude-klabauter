"""check_destructive_rm passes targets resolving under <repo>/scratch and keeps
guarding the same rm outside it, via a `..` escape, or via a symlink escape."""

from __future__ import annotations

import os
import subprocess

import pytest

from coordinator_core.bash_guards.dispatch_checks import check_destructive_rm
from coordinator_core.guard_messages.testing import envelope_of
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


def _git(*args: str, cwd: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        env={**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull},
        **no_console_creationflags(),
    )


@pytest.fixture()
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    _git("init", "-q", cwd=str(root))
    _git("config", "user.email", "t@t.invalid", cwd=str(root))
    _git("config", "user.name", "t", cwd=str(root))
    (root / "tracked.txt").write_text("committed\n")
    _git("add", "tracked.txt", cwd=str(root))
    _git("commit", "-qm", "init", cwd=str(root))
    (root / "scratch").mkdir()
    return root


def _dirty_dir(path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    (path / "untracked.txt").write_text("uncommitted\n")


def _check(cmd: str):
    return envelope_of(check_destructive_rm(cmd))


def test_untracked_dir_in_scratch_passes_with_no_advisory(repo):
    _dirty_dir(repo / "scratch" / "Saved")
    assert _check(f"rm -rf {repo / 'scratch' / 'Saved'}") is None


def test_same_untracked_dir_outside_scratch_stays_denied(repo):
    _dirty_dir(repo / "control" / "Saved")
    out = _check(f"rm -rf {repo / 'control' / 'Saved'}")
    assert out is not None
    assert "uncommitted" in out["hookSpecificOutput"]["permissionDecisionReason"]


def test_dotdot_escape_out_of_scratch_stays_denied(repo):
    _dirty_dir(repo / "control" / "Saved")
    assert _check(f"rm -rf {repo / 'scratch' / '..' / 'control' / 'Saved'}") is not None


def test_symlink_escape_out_of_scratch_stays_denied(repo):
    _dirty_dir(repo / "control" / "Saved")
    link = repo / "scratch" / "link"
    try:
        os.symlink(str(repo / "control"), str(link), target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation unavailable (Windows without developer mode)")
    assert _check(f"rm -rf {link / 'Saved'}") is not None
    assert _check(f"rm -rf {link}") is not None


def test_nested_repo_root_with_a_commit_in_scratch_stays_denied(repo):
    probe = repo / "scratch" / "probe"
    probe.mkdir()
    _git("init", "-q", cwd=str(probe))
    _git("config", "user.email", "t@t.invalid", cwd=str(probe))
    _git("config", "user.name", "t", cwd=str(probe))
    (probe / "f.txt").write_text("x\n")
    _git("add", "f.txt", cwd=str(probe))
    _git("commit", "-qm", "c", cwd=str(probe))
    assert _check(f"rm -rf {probe}") is not None
