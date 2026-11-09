from __future__ import annotations

import subprocess
import time
from pathlib import Path

from coordinator_core.consolidate_assemble import live_holders as lh

_PORCELAIN = (
    "worktree /r\nHEAD aaa\nbranch refs/heads/work\n\n"
    "worktree /r-wt\nHEAD bbb\nbranch refs/heads/held\n\n"
    "worktree /r-det\nHEAD ccc\ndetached\n"
)


def _git(rc: int = 0, out: str = _PORCELAIN):
    calls = []

    def _fake(argv, cwd):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, rc, stdout=out, stderr="")

    _fake.calls = calls
    return _fake


def _routed(listing: str, rc: int = 0):
    def _fake(argv, cwd):
        out = listing if argv[0] == "for-each-ref" else _PORCELAIN
        return subprocess.CompletedProcess(argv, rc if argv[0] == "for-each-ref" else 0, stdout=out, stderr="")

    return _fake


def test_no_holder_and_no_recent_push_is_free():
    g = _git()
    result = lh.live_holders(g, Path("r"), ["free", "other"])
    assert result == {"free": [], "other": []}
    assert len(g.calls) == 2


def test_recent_remote_push_is_held_by_another_box():
    fresh = int(time.time()) - 60
    old = int(time.time()) - 3 * 24 * 3600
    g = _routed(f"origin/fresh\t{fresh}\norigin/stale\t{old}\n")
    result = lh.live_holders(g, Path("r"), ["fresh", "stale", "absent"])
    assert result["fresh"] == ["recent push to origin/fresh"]
    assert result["stale"] == []
    assert result["absent"] == []


def test_failed_remote_listing_is_unknown():
    result = lh.live_holders(_routed("", rc=128), Path("r"), ["free"])
    assert result == {"free": None}


def test_local_worktree_holder_is_nonempty_list():
    result = lh.live_holders(_git(), Path("r"), ["held", "free"])
    assert result["held"] == ["worktree /r-wt"]
    assert result["free"] == []


def test_failed_worktree_probe_is_unknown_for_all():
    assert lh.live_holders(_git(128, ""), Path("r"), ["held"]) == {"held": None}


def test_definite_cross_box_answer_allows_empty(monkeypatch):
    monkeypatch.setattr(lh, "_cross_box_holders", lambda g, r, b: {"free": [], "held": ["box2 pushing"]})
    result = lh.live_holders(_git(), Path("r"), ["free", "held", "unseen"])
    assert result["free"] == []
    assert result["held"] == ["worktree /r-wt", "box2 pushing"]
    assert result["unseen"] is None
