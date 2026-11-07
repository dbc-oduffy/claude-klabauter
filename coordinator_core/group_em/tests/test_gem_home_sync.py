"""gem_home_sync against a bare-repo remote under tmp_path and two clones."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.group_em import gem_home, gem_home_sync
from coordinator_core.win_portability import no_console_creationflags

pytestmark = pytest.mark.complete


def _git(cwd: Path, *args: str) -> str:
    res = subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false", *args],
        cwd=str(cwd), capture_output=True, text=True, check=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    return res.stdout.strip()


def _clone(remote: Path, dest: Path) -> Path:
    _git(dest.parent, "clone", "-q", str(remote), str(dest))
    _git(dest, "config", "user.name", "t")
    _git(dest, "config", "user.email", "t@t")
    _git(dest, "config", "commit.gpgsign", "false")
    _git(dest, "checkout", "-q", "-B", "main")
    return dest


@pytest.fixture(autouse=True)
def _no_gh(monkeypatch):
    monkeypatch.setattr(gem_home, "remote_status", lambda: (None, "gh-absent"))


@pytest.fixture
def remote(tmp_path):
    bare = tmp_path / "remote.git"
    _git(tmp_path, "init", "-q", "--bare", "-b", "main", str(bare))
    return bare


@pytest.fixture
def seeded(tmp_path, remote):
    a = _clone(remote, tmp_path / "a")
    (a / "f.txt").write_text("base\n")
    (a / "g.txt").write_text("g\n")
    _git(a, "add", "-A")
    _git(a, "commit", "-q", "-m", "base")
    _git(a, "push", "-q", "-u", "origin", "main")
    return a


def test_no_remote_is_noop_for_both_legs(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    _git(home, "init", "-q")
    (home / "x.txt").write_text("x")
    for rep in (gem_home_sync.pull_on_entry(home), gem_home_sync.push_leg(home, "m")):
        assert rep["sync"] == "noop"
        assert rep["remote_opt_out"] == "gh-absent"
    assert subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(home), capture_output=True, **no_console_creationflags()).returncode != 0


def test_push_leg_commits_and_pushes(tmp_path, remote, seeded):
    (seeded / "new.txt").write_text("n")
    rep = gem_home_sync.push_leg(seeded, "handoff")
    assert rep == {"sync": "ok", "paths": [], "remote_opt_out": None}
    b = _clone(remote, tmp_path / "b")
    assert (b / "new.txt").read_text() == "n"
    assert _git(b, "log", "-1", "--format=%s") == "handoff"


def test_push_leg_first_push_to_empty_remote(tmp_path, remote):
    a = _clone(remote, tmp_path / "a")
    (a / "f.txt").write_text("x")
    assert gem_home_sync.push_leg(a, "first")["sync"] == "ok"
    assert _git(tmp_path, "--git-dir", str(remote), "log", "-1", "--format=%s", "main") == "first"


def test_pull_on_entry_brings_peer_work_and_keeps_dirty_edits(tmp_path, remote, seeded):
    b = _clone(remote, tmp_path / "b")
    (b / "peer.txt").write_text("p")
    gem_home_sync.push_leg(b, "peer")
    (seeded / "g.txt").write_text("dirty\n")
    rep = gem_home_sync.pull_on_entry(seeded)
    assert rep["sync"] == "ok"
    assert (seeded / "peer.txt").read_text() == "p"
    assert (seeded / "g.txt").read_text() == "dirty\n"


def test_push_leg_conflict_aborts_and_leaves_head(tmp_path, remote, seeded):
    b = _clone(remote, tmp_path / "b")
    (b / "f.txt").write_text("from-b\n")
    assert gem_home_sync.push_leg(b, "b")["sync"] == "ok"
    (seeded / "f.txt").write_text("from-a\n")
    rep = gem_home_sync.push_leg(seeded, "a")
    assert rep["sync"] == "conflict"
    assert rep["paths"] == ["f.txt"]
    assert not (seeded / ".git" / "rebase-merge").exists()
    assert (seeded / "f.txt").read_text() == "from-a\n"
    assert _git(seeded, "log", "-1", "--format=%s") == "a"
    assert _git(tmp_path, "--git-dir", str(remote), "log", "-1", "--format=%s", "main") == "b"


def test_pull_on_entry_conflict_does_not_move_head(tmp_path, remote, seeded):
    b = _clone(remote, tmp_path / "b")
    (b / "f.txt").write_text("from-b\n")
    gem_home_sync.push_leg(b, "b")
    (seeded / "f.txt").write_text("from-a\n")
    _git(seeded, "commit", "-q", "-am", "a-local")
    head = _git(seeded, "rev-parse", "HEAD")
    rep = gem_home_sync.pull_on_entry(seeded)
    assert rep["sync"] == "conflict"
    assert rep["paths"] == ["f.txt"]
    assert _git(seeded, "rev-parse", "HEAD") == head
    assert _git(seeded, "status", "--porcelain") == ""
