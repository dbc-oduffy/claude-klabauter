"""check_destructive_rm allows removing the doctrine-edit approval sentinel in the
git dir and still denies other git-store targets and a same-named directory."""

from __future__ import annotations

import os
import subprocess

import pytest

from coordinator_core.bash_guards.dispatch_checks import check_destructive_rm
from coordinator_core.guard_messages.testing import envelope_of
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_SENTINEL = ".coordinator-doctrine-edit-approved"


@pytest.fixture()
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(
        ["git", "init", "-q"],
        cwd=str(root),
        check=True,
        capture_output=True,
        env={**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull},
        **no_console_creationflags(),
    )
    return root


def _check(cmd: str):
    return envelope_of(check_destructive_rm(cmd))


def test_rm_of_approval_sentinel_file_passes(repo):
    sentinel = repo / ".git" / _SENTINEL
    sentinel.write_text("")
    assert _check(f"rm {sentinel}") is None
    assert _check(f"rm -f {sentinel}") is None


def test_rm_of_other_git_store_file_stays_denied(repo):
    other = repo / ".git" / "HEAD"
    out = _check(f"rm {other}")
    assert out is not None
    assert "git store" in out["hookSpecificOutput"]["permissionDecisionReason"]


def test_rm_of_sentinel_named_directory_stays_denied(repo):
    d = repo / ".git" / _SENTINEL
    d.mkdir()
    assert _check(f"rm -rf {d}") is not None


def test_rm_of_sentinel_lookalike_stays_denied(repo):
    look = repo / ".git" / (_SENTINEL + ".bak")
    look.write_text("")
    assert _check(f"rm {look}") is not None
