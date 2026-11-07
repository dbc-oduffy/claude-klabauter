"""A single-baton drop costs O(that baton), never O(corpus), and reads nothing under the lock.

`pickup-assemble drop` -> `_unclaim` -> `locked_rmw(mutate)`: the mutate closure
used to walk and YAML-parse every live and archived handoff (and scan every
plan) while holding the record lock, so one drop held the lock 30s+ while every
peer waited. These tests pin the fix on a real-size corpus fixture under
`tmp_path`: the walkers are never called, no directory is enumerated inside the
lock, and the verb completes under the 500ms process-time bar.
"""

import os
import time
from pathlib import Path

import pytest

import coordinator_core.ops.handoff_transition as ht

pytestmark = pytest.mark.complete

ARCHIVED_HANDOFFS = 3000
PLANS = 1500
PROCESS_TIME_BAR_S = 0.5

_DELIVERABLE_ID = "dlv-real-size-fixture-000001"


def _archived_handoff(i: int) -> str:
    return (
        "---\n"
        f'title: "Archived baton {i}"\n'
        "created: 2026-06-01\n"
        'branch: "work/test/2026-06-01"\n'
        "status: consumed\n"
        "predecessor: none\n"
        "kind: session-handoff\n"
        "deployment_state: shipped\n"
        f"stub_id: archived-{i:05d}\n"
        f"handoff_id: hnd-archived-{i:06d}\n"
        "category: infra\n"
        'summary: "An archived baton padding the corpus"\n'
        "pickup_ready: false\n"
        "blocked_by: []\n"
        "---\n\n## What Was Accomplished\n\n" + ("padding line\n" * 60)
    )


def _plan(i: int, deliverable_id: str, status: str) -> str:
    return (
        "---\n"
        f'title: "Plan {i}"\n'
        "created: 2026-06-01\n"
        f"status: {status}\n"
        f'deliverable_id: "{deliverable_id}"\n'
        "---\n\n## Problem\n\n" + ("padding line\n" * 60)
    )


@pytest.fixture
def worktree(tmp_path, monkeypatch):
    gitdir = tmp_path / ".git"
    gitdir.mkdir()
    monkeypatch.setattr(
        "coordinator_core.git.repo_root.git_common_dir", lambda _root: str(gitdir)
    )
    import coordinator_core.lifecycle as _lc

    if hasattr(_lc, "_git_common_dir_cached"):
        _lc._git_common_dir_cached.cache_clear()

    archive = tmp_path / "archive" / "handoffs" / "2026-06"
    archive.mkdir(parents=True)
    for i in range(ARCHIVED_HANDOFFS):
        (archive / f"2026-06-01_{i:05d}_archived.md").write_text(
            _archived_handoff(i), encoding="utf-8"
        )

    plans = tmp_path / "docs" / "plans"
    plans.mkdir(parents=True)
    for i in range(PLANS):
        (plans / f"2026-06-01-plan-{i:05d}.md").write_text(
            _plan(i, f"dlv-other-{i:06d}", "implemented"), encoding="utf-8"
        )
    return tmp_path


def _claimed_blocked_baton(worktree: Path) -> Path:
    d = worktree / "state" / "handoffs"
    d.mkdir(parents=True, exist_ok=True)
    p = d / "2026-11-02_200913_dropped-baton.md"
    p.write_text(
        "---\n"
        'title: "Dropped baton"\n'
        "created: 2026-11-02\n"
        'branch: "work/test/2026-11-02"\n'
        "status: claimed\n"
        "predecessor: none\n"
        "kind: session-handoff\n"
        "deployment_state: in_flight\n"
        f'deliverable_id: "{_DELIVERABLE_ID}"\n'
        "category: infra\n"
        'summary: "A claimed baton whose blocker sits in the archive"\n'
        "pickup_ready: false\n"
        "blocked_by:\n"
        "  - hnd-archived-000001\n"
        "---\n\n## What Was Accomplished\n\nbody\n",
        encoding="utf-8",
    )
    return p


@pytest.fixture
def corpus_reads_forbidden(monkeypatch):
    import coordinator_core.reconcile.handoff_corpus as hc

    def _boom(*_a, **_k):
        raise AssertionError("single-baton drop walked the handoff corpus")

    monkeypatch.setattr(hc, "_collect_all_handoffs_for_gate_index", _boom)
    monkeypatch.setattr(hc, "_walk_archive_md_files", _boom)
    monkeypatch.setattr(hc, "_build_blocker_index", _boom)


def _forbid_enumeration_inside_lock(monkeypatch):
    """Wrap `locked_rmw` so that any directory enumeration while the mutate
    closure runs (the shape of every corpus or plan-set scan) fails the test."""
    real_locked_rmw = ht.locked_rmw
    enumerated: list = []

    def _locked_rmw(path, mutate, **kwargs):
        def _guarded(old_text):
            real = {
                "scandir": os.scandir,
                "walk": os.walk,
                "listdir": os.listdir,
            }

            def _spy(name):
                def _inner(*a, **k):
                    enumerated.append((name, a))
                    return real[name](*a, **k)

                return _inner

            for name in real:
                monkeypatch.setattr(os, name, _spy(name))
            try:
                return mutate(old_text)
            finally:
                for name, fn in real.items():
                    monkeypatch.setattr(os, name, fn)

        return real_locked_rmw(path, _guarded, **kwargs)

    monkeypatch.setattr(ht, "locked_rmw", _locked_rmw)
    return enumerated


def test_drop_of_a_blocked_baton_stays_under_the_bar_and_walks_nothing(
    worktree, corpus_reads_forbidden, monkeypatch
):
    p = _claimed_blocked_baton(worktree)
    enumerated = _forbid_enumeration_inside_lock(monkeypatch)

    started = time.process_time()
    result = ht._unclaim(str(p), "", worktree, worktree)
    elapsed = time.process_time() - started

    assert result.get("exit_code", 0) == 0, result
    text = p.read_text(encoding="utf-8")
    assert "deployment_state: awaiting_gate" in text
    assert "pickup_ready: false" in text
    assert "hnd-archived-000001" in text
    assert enumerated == [], f"directory enumeration under the lock: {enumerated}"
    assert elapsed < PROCESS_TIME_BAR_S, f"{elapsed * 1000:.0f}ms process time"


def test_drop_still_refuses_when_the_governing_plan_is_implemented(worktree):
    """The plan scan moved out of the lock; the refusal it feeds did not."""
    p = _claimed_blocked_baton(worktree)
    (worktree / "docs" / "plans" / "2026-06-02-governing.md").write_text(
        _plan(0, _DELIVERABLE_ID, "implemented"), encoding="utf-8"
    )
    before = p.read_text(encoding="utf-8")

    result = ht._unclaim(str(p), "", worktree, worktree)

    assert result.get("exit_code") == 1, result
    assert "2026-06-02-governing.md" in result["error"]
    assert p.read_text(encoding="utf-8") == before


def test_governing_plan_lookup_matches_the_full_index(worktree):
    plans = worktree / "docs" / "plans"
    (plans / "2026-06-02-open.md").write_text(
        _plan(0, _DELIVERABLE_ID, "executing"), encoding="utf-8"
    )
    (plans / "2026-06-03-done-a.md").write_text(
        _plan(1, _DELIVERABLE_ID, "implemented"), encoding="utf-8"
    )
    (plans / "2026-06-04-done-b.md").write_text(
        _plan(2, _DELIVERABLE_ID, "implemented"), encoding="utf-8"
    )

    found = ht._find_implemented_governing_plan(worktree, _DELIVERABLE_ID)

    assert found == ht.build_implemented_plan_index(worktree)[_DELIVERABLE_ID]
    assert found["path"].endswith("2026-06-03-done-a.md")
    assert ht._find_implemented_governing_plan(worktree, "dlv-absent-000001") is None
