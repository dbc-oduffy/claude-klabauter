"""A ledger-check halt record withholds every path the run wrote and left uncommitted, declared or not."""

from __future__ import annotations

import pytest

from coordinator_core.ops import warp_run_deliverable
from coordinator_core.ops.dispatch_emit import halted_record_commit
from coordinator_core.ops.dispatch_emit.tests.test_terminal_commit_halted_record import (  # noqa: F401
    LEDGER,
    PASSING,
    RUN,
    _call,
    _git,
    _write_record,
    repo,
)

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

TRACKED = "pkg/__init__.py"
UNTRACKED = "pkg/test_floor.py"
PEER_DIRT = "product.py"


@pytest.fixture
def own_dirt(repo, monkeypatch):
    """The 6137b0 shape: an undeclared tracked hunk and an undeclared untracked file the run wrote,
    beside a tracked edit a peer made."""
    (repo / "pkg").mkdir()
    (repo / TRACKED).write_text("floor = 0\n", encoding="utf-8")
    _git(["add", "pkg"], repo)
    _git(["commit", "-q", "-m", "seed pkg"], repo)
    (repo / TRACKED).write_text("floor = 1000\n", encoding="utf-8")
    (repo / UNTRACKED).write_text("def test_floor():\n    pass\n", encoding="utf-8")
    (repo / PEER_DIRT).write_text("x = 2000\n", encoding="utf-8")
    _write_record(repo, RUN)
    monkeypatch.setattr(halted_record_commit, "_run_written", lambda *_a, **_k: [TRACKED, UNTRACKED])
    return repo


def test_undeclared_own_tracked_and_untracked_dirt_reaches_the_halt_record(own_dirt):
    out = _call(own_dirt, LEDGER, **PASSING)

    held = warp_run_deliverable.halted_withheld_paths(own_dirt, RUN)
    assert {TRACKED, UNTRACKED} <= held
    assert PEER_DIRT not in held
    assert out["withheld"][TRACKED] == halted_record_commit.RUN_DIRT_UNCOMMITTED
    assert out["withheld"][UNTRACKED] == halted_record_commit.RUN_DIRT_UNCOMMITTED


def test_own_dirt_stays_uncommitted_and_the_peer_edit_is_not_claimed(own_dirt):
    _call(own_dirt, LEDGER, **PASSING)

    status = _git(["status", "--porcelain", "-uall"], own_dirt)
    assert TRACKED in status and UNTRACKED in status and PEER_DIRT in status
    assert PEER_DIRT not in warp_run_deliverable.halted_withheld_paths(own_dirt, RUN)


@pytest.fixture
def touch_record_dirt(repo, tmp_path, monkeypatch):
    """The same dirt, but `_run_written` is real: the paths come from a session touch record."""
    from coordinator_core.ops.dispatch_emit import terminal_commit
    from coordinator_core.session import core, touch_record

    (repo / "pkg").mkdir()
    (repo / TRACKED).write_text("floor = 0\n", encoding="utf-8")
    _git(["add", "pkg"], repo)
    _git(["commit", "-q", "-m", "seed pkg"], repo)
    (repo / TRACKED).write_text("floor = 1000\n", encoding="utf-8")
    (repo / UNTRACKED).write_text("def test_floor():\n    pass\n", encoding="utf-8")
    (repo / PEER_DIRT).write_text("x = 2000\n", encoding="utf-8")
    (repo / "scratch").mkdir()
    (repo / "scratch" / "dump.txt").write_text("dump\n", encoding="utf-8")
    _write_record(repo, RUN)

    hub = tmp_path / "hub"
    monkeypatch.setattr(core, "sessions_dir", lambda *_a, **_k: str(hub))
    monkeypatch.setattr(terminal_commit, "_run_started_at", lambda *_a, **_k: 100.0)
    sink = touch_record.sink_path(hub / "sess-1")
    for path, ts, kind in (
        (TRACKED, 200.0, touch_record.KIND_WRITE),
        (UNTRACKED, 200.0, touch_record.KIND_WRITE),
        ("scratch/dump.txt", 200.0, touch_record.KIND_WRITE),
        (PEER_DIRT, 200.0, touch_record.KIND_READ),
        ("docs/guide.md", 50.0, touch_record.KIND_WRITE),
    ):
        touch_record.append_event(
            sink, session_id="sess-1", agent_id=None, verb=touch_record.VERB_TOUCH,
            path=path, timestamp=ts, kind=kind, name=None,
        )
    return repo


def test_run_written_reads_the_touch_record_and_drops_scratch_reads_and_pre_run(touch_record_dirt):
    assert halted_record_commit._run_written(touch_record_dirt, "sess-1", RUN) == [TRACKED, UNTRACKED]
    assert halted_record_commit._run_written(touch_record_dirt, None, RUN) == []


def test_touch_record_dirt_reaches_the_halt_record(touch_record_dirt, monkeypatch):
    real = halted_record_commit._run_written
    monkeypatch.setattr(halted_record_commit, "_run_written", lambda root, _sid, rid: real(root, "sess-1", rid))

    out = _call(touch_record_dirt, LEDGER, **PASSING)

    held = warp_run_deliverable.halted_withheld_paths(touch_record_dirt, RUN)
    assert {TRACKED, UNTRACKED} <= held
    assert PEER_DIRT not in held and "scratch/dump.txt" not in held
    assert out["withheld"][TRACKED] == halted_record_commit.RUN_DIRT_UNCOMMITTED


def test_red_without_the_run_dirt_capture(own_dirt, monkeypatch):
    monkeypatch.setattr(halted_record_commit, "_withhold_run_dirt", lambda *_a, **_k: None)

    _call(own_dirt, LEDGER, **PASSING)

    held = warp_run_deliverable.halted_withheld_paths(own_dirt, RUN)
    assert TRACKED not in held and UNTRACKED not in held
