"""`ceremony.commit_v2` returns a structured error for a machine-local path, force_ignored included."""

from __future__ import annotations

import subprocess

import pytest

from coordinator_core.ops.ceremony import commit_v2

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_NOWIN = {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}
_SHARE = ".coordinator-local/subagent-share/r.md"


def _git(repo, *args):
    return subprocess.run(
        ["git", *args], cwd=str(repo), capture_output=True, text=True, check=True, **_NOWIN
    ).stdout.strip()


@pytest.fixture()
def repo(tmp_path):
    r = tmp_path / "r"
    r.mkdir()
    _git(r, "init", "-q", "-b", "work/p")
    _git(r, "config", "user.email", "t@local")
    _git(r, "config", "user.name", "t")
    _git(r, "config", "commit.gpgsign", "false")
    (r / "keep.txt").write_text("k\n", encoding="utf-8")
    _git(r, "add", "keep.txt")
    _git(r, "commit", "-q", "-m", "seed")
    p = r / _SHARE
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("x\n", encoding="utf-8")
    return r


def test_a_force_ignored_share_path_is_refused_and_head_holds(repo):
    head = _git(repo, "rev-parse", "HEAD")
    out = commit_v2._handler(
        {"paths": [_SHARE], "force_ignored": [_SHARE], "message": "m"}, repo_root=repo / ".git"
    )
    assert out["committed"] is False
    assert "machine-local" in out["error"] and _SHARE in out["error"]
    assert _git(repo, "rev-parse", "HEAD") == head
