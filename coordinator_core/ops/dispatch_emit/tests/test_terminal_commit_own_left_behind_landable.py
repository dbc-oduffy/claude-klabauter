"""terminal_commit's own-left-behind follow-on hands commit_v2 only landable (non-ignored) paths."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import own_carry, terminal_commit
from coordinator_core.session import scope as session_scope
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

SHA = "a" * 40
SID = "5e1f0000-0000-4000-8000-0000000000c3"
N_IGNORED = 700


def _git(args, cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, check=True, **no_console_creationflags())


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    _git(["init", "-q"], root)
    (root / ".gitignore").write_text("junk/\n", encoding="utf-8", newline="")
    (root / "junk").mkdir()
    for i in range(N_IGNORED):
        (root / "junk" / f"f{i}.txt").write_text("x", encoding="utf-8", newline="")
    return root


@pytest.fixture
def calls(monkeypatch):
    seen: list = []

    def fake(op, params, repo_root=None):
        seen.append((op, dict(params)))
        return {"committed": True, "sha": "b" * 40}

    monkeypatch.setattr(terminal_commit, "reentrant_dispatch", fake)
    monkeypatch.setattr(session_scope, "contested_by_live_peers", lambda paths, sid, cwd=None: {})
    return seen


def _spawns(monkeypatch) -> list:
    spawned: list = []
    real = terminal_commit.run_git

    def spy(args, **kw):
        spawned.append(list(args))
        return real(args, **kw)

    monkeypatch.setattr(terminal_commit, "run_git", spy)
    return spawned


def test_ignored_claims_are_not_handed_to_commit_v2(repo, calls, monkeypatch):
    spawned = _spawns(monkeypatch)
    landable = ["state/a.yaml", "state/b.yaml"]
    left = [f"junk/f{i}.txt" for i in range(N_IGNORED)] + landable
    reply: dict = {}
    terminal_commit._land_own_left_behind(reply, SHA, SID, repo, repo / ".git", left)
    assert len(calls) == 1
    params = calls[0][1]
    assert params["paths"] == landable
    assert params["ignored_untracked"] == []
    assert reply[own_carry.REPLY_KEY]["paths"] == landable
    assert [a for a in spawned if a[0] == "check-ignore"] == [["check-ignore", "-z", "--stdin"]]


def test_snapshot_covered_paths_take_no_spawn(repo, calls, monkeypatch):
    spawned = _spawns(monkeypatch)

    class Snap:
        def covers(self, p):
            return True

        def ignored_among(self, paths):
            return [p for p in paths if p.startswith("junk/")]

    left = ["junk/f1.txt", "state/a.yaml"]
    terminal_commit._land_own_left_behind({}, SHA, SID, repo, repo / ".git", left, snapshot=Snap())
    assert calls[0][1]["paths"] == ["state/a.yaml"]
    assert spawned == []


def test_only_landable_paths_refuse_nothing_when_all_ignored(repo, calls):
    terminal_commit._land_own_left_behind({}, SHA, SID, repo, repo / ".git", ["junk/f1.txt"])
    assert calls == []
