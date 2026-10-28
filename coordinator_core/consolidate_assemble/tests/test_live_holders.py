from __future__ import annotations

import subprocess
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


def test_cross_box_source_absent_returns_none_for_unheld_branches():
    g = _git()
    result = lh.live_holders(g, Path("r"), ["free", "other"])
    assert result == {"free": None, "other": None}
    assert len(g.calls) == 1


def test_local_worktree_holder_is_nonempty_list():
    result = lh.live_holders(_git(), Path("r"), ["held", "free"])
    assert result["held"] == ["worktree /r-wt"]
    assert result["free"] is None


def test_failed_worktree_probe_is_unknown_for_all():
    assert lh.live_holders(_git(128, ""), Path("r"), ["held"]) == {"held": None}


def test_definite_cross_box_answer_allows_empty(monkeypatch):
    monkeypatch.setattr(lh, "_cross_box_holders", lambda g, r, b: {"free": [], "held": ["box2 pushing"]})
    result = lh.live_holders(_git(), Path("r"), ["free", "held", "unseen"])
    assert result["free"] == []
    assert result["held"] == ["worktree /r-wt", "box2 pushing"]
    assert result["unseen"] is None
