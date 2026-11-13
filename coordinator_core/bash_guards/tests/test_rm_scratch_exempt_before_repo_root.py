"""rm of a repo root or git store resolving under <repo>/scratch passes; the same
rm outside scratch stays denied and its text names the move-to-scratch remedy."""

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


def _init_with_commit(path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    _git("init", "-q", cwd=str(path))
    _git("config", "user.email", "t@t.invalid", cwd=str(path))
    _git("config", "user.name", "t", cwd=str(path))
    (path / "f.txt").write_text("x\n")
    _git("add", "f.txt", cwd=str(path))
    _git("commit", "-qm", "c", cwd=str(path))


@pytest.fixture()
def repo(tmp_path):
    root = tmp_path / "repo"
    _init_with_commit(root)
    (root / "scratch").mkdir()
    return root


def _check(cmd: str):
    return envelope_of(check_destructive_rm(cmd))


def test_nested_repo_root_in_scratch_passes(repo):
    probe = repo / "scratch" / "fam-1a" / "r5-repo"
    _init_with_commit(probe)
    (probe / "dirty.txt").write_text("uncommitted\n")
    assert _check(f"rm -rf {probe}") is None


def test_git_store_inside_scratch_clone_passes(repo):
    probe = repo / "scratch" / "probe"
    _init_with_commit(probe)
    assert _check(f"rm -rf {probe / '.git'}") is None


def test_same_repo_root_outside_scratch_is_denied_with_move_remedy(repo, tmp_path):
    probe = tmp_path / "repo" / ".coordinator-local" / "r5-repo"
    _init_with_commit(probe)
    out = _check(f"rm -rf {probe}")
    assert out is not None
    reason = out["hookSpecificOutput"]["permissionDecisionReason"]
    assert "ROOT of a git repository" in reason
    assert "move it under <repo>/scratch, then delete it there" in reason
