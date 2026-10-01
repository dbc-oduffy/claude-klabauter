"""Tests for the pending-resync drain: classification, spawn counts, peer-staging safety.

Spawn-free: ``run_git`` is a counting fake over an in-memory index, and HEAD entries
are supplied by patching ``git_state.head_blobs``.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from coordinator_core.git import git_state
from coordinator_core.ops.fleet import _common
from coordinator_core.ops.fleet import _index_resync_drain as drain
from coordinator_core.ops.fleet._index_resync_pending import (
    PendingResync,
    list_pending,
    record_pending,
)

REG = 0o100644
BLOB = "a" * 40
OTHER = "b" * 40


def _rec(n=1, blob=BLOB, cls=""):
    return PendingResync(
        f"state/h/{n}.md", f"archive/h/{n}.md", f"cid{n}", blob,
        "archive_and_commit", "2026-09-30T00:00:00Z", cls,
    )


class FakeGit:
    """Counting runner over an in-memory index; ``restore --staged`` mimics git."""

    def __init__(self, index, head, restore_rc=0):
        self.index = dict(index)
        self.head = head
        self.restore_rc = restore_rc
        self.calls = []

    async def __call__(self, argv, *, cwd, env):
        self.calls.append(list(argv))
        paths = argv[argv.index("--") + 1:]
        if argv[1] == "ls-files":
            rows = [
                f"{m:o} {s} {st}\t{p}" for p in paths
                for (m, s, st) in self.index.get(p, [])
            ]
            return 0, "\x00".join(rows) + ("\x00" if rows else ""), ""
        if self.restore_rc:
            return self.restore_rc, "", "index.lock"
        for p in paths:
            self.index.pop(p, None)
            if p in self.head:
                m, s = self.head[p]
                self.index[p] = [(m, s, 0)]
        return 0, "", ""


@pytest.fixture
def env(tmp_path, monkeypatch):
    (tmp_path / ".git").mkdir()
    state = {"head": {}, "reports": []}
    monkeypatch.setattr(
        git_state, "head_blobs",
        lambda repo, paths: {p: state["head"][p] for p in paths if p in state["head"]},
    )
    monkeypatch.setattr(
        _common, "_persist_index_resync_failure",
        lambda **kw: state["reports"].append(kw),
    )
    return tmp_path, state


def _run(root, git):
    return asyncio.run(drain.drain_pending_resyncs(root, run_git=git))


def _needed_fixture(root, state, n=1):
    rec = _rec(n)
    state["head"][rec.dst] = (REG, BLOB)
    record_pending(root, [rec])
    return rec, {rec.src: [(REG, BLOB, 0)]}


def test_empty_steady_state_spawns_nothing(env):
    root, state = env
    git = FakeGit({}, state["head"])
    assert _run(root, git) == {"restored": [], "discharged": [], "kept": {}}
    assert git.calls == []


def test_needed_records_restored_in_two_spawns(env):
    root, state = env
    index = {}
    for n in range(1, 21):
        _rec_n, idx = _needed_fixture(root, state, n)
        index.update(idx)
    git = FakeGit(index, state["head"])
    result = _run(root, git)
    assert len(result["restored"]) == 20
    assert [c[1] for c in git.calls] == ["ls-files", "restore"]
    assert "state/h/7.md" in git.calls[1] and "archive/h/7.md" in git.calls[1]
    assert list_pending(root) == []
    assert git.index.get("state/h/1.md") is None
    assert git.index["archive/h/1.md"] == [(REG, BLOB, 0)]


def test_healed_and_stale_discharged_without_restore(env):
    root, state = env
    healed, stale = _rec(1), _rec(2)
    state["head"][healed.dst] = (REG, BLOB)
    record_pending(root, [healed, stale])
    git = FakeGit({healed.dst: [(REG, BLOB, 0)]}, state["head"])
    result = _run(root, git)
    assert sorted(result["discharged"]) == ["cid1", "cid2"]
    assert result["restored"] == []
    assert [c[1] for c in git.calls] == ["ls-files"]
    assert list_pending(root) == []


def test_contested_peer_staged_dst_survives(env):
    root, state = env
    rec, idx = _needed_fixture(root, state)
    idx[rec.dst] = [(REG, OTHER, 0)]
    git = FakeGit(idx, state["head"])
    result = _run(root, git)
    assert result["kept"] == {"cid1": "contested"}
    assert result["restored"] == []
    assert [c[1] for c in git.calls] == ["ls-files"]
    assert git.index[rec.dst] == [(REG, OTHER, 0)]
    assert git.index[rec.src] == [(REG, BLOB, 0)]
    assert len(list_pending(root)) == 1


def test_contested_peer_staged_src_survives(env):
    root, state = env
    rec, _idx = _needed_fixture(root, state)
    git = FakeGit({rec.src: [(REG, OTHER, 0)]}, state["head"])
    assert _run(root, git)["kept"] == {"cid1": "contested"}
    assert [c[1] for c in git.calls] == ["ls-files"]


def test_unknown_blob_unmerged_and_mode_are_contested(env):
    root, state = env
    a, b, c = _rec(1, blob=""), _rec(2), _rec(3)
    for r in (a, b, c):
        state["head"][r.dst] = (REG, BLOB)
    record_pending(root, [a, b, c])
    git = FakeGit(
        {
            a.src: [(REG, BLOB, 0)],
            b.src: [(REG, BLOB, 1), (REG, BLOB, 2)],
            c.src: [(0o100755, BLOB, 0)],
        },
        state["head"],
    )
    kept = _run(root, git)["kept"]
    assert kept == {"cid1": "contested", "cid2": "contested", "cid3": "contested"}
    assert [c_[1] for c_ in git.calls] == ["ls-files"]


def test_reversed_kept(env):
    root, state = env
    rec = _rec()
    state["head"][rec.src] = (REG, BLOB)
    record_pending(root, [rec])
    git = FakeGit({rec.src: [(REG, BLOB, 0)]}, state["head"])
    assert _run(root, git)["kept"] == {"cid1": "reversed"}
    assert [c[1] for c in git.calls] == ["ls-files"]


def test_kept_reported_once_per_class_change(env):
    root, state = env
    rec = _rec()
    state["head"][rec.src] = (REG, BLOB)
    record_pending(root, [rec])
    git = FakeGit({rec.src: [(REG, BLOB, 0)]}, state["head"])
    _run(root, git)
    _run(root, git)
    _run(root, git)
    assert len(state["reports"]) == 1
    assert state["reports"][0]["reason"] == f"drain-reversed: {rec.src} -> {rec.dst}"
    assert list_pending(root)[0].last_reported_class == "reversed"


def test_restore_failure_keeps_record_for_retry(env):
    root, state = env
    rec, idx = _needed_fixture(root, state)
    git = FakeGit(idx, state["head"], restore_rc=1)
    result = _run(root, git)
    assert result["kept"] == {"cid1": "restore-failed"}
    assert result["restored"] == []
    assert len(list_pending(root)) == 1
    assert [c[1] for c in git.calls] == ["ls-files", "restore"]


def test_dst_absent_from_head_restores_src_only(env):
    root, state = env
    rec = _rec()
    record_pending(root, [rec])
    git = FakeGit({rec.src: [(REG, BLOB, 0)]}, state["head"])
    result = _run(root, git)
    assert result["restored"] == ["cid1"]
    assert git.calls[1][-1] == rec.src
    assert rec.dst not in git.calls[1]
