"""
Guard test for the IDEMPOTENCY of the orphaned-claim-dir cull
(``coordinator_core.ops.session.reap._reap_orphaned_claims`` and its
per-repo wrapper ``_reap_claims_for_target``).

Why this exists, and what it deliberately does NOT guard. The
``d_step4_counts_reap_claims`` directive in
``coordinator_core/workweek_complete/brief.py`` carries a two-part comment:
(a) the cull is idempotent, so a double-fire is harmless, and (b)
/workweek-complete does not invoke /workday-complete, so the two never
double-fire today anyway. (b) is an ABSENCE-based safety claim — it holds
only until someone adds that call, and nothing tells them they broke it.
(a) is the real invariant: if the cull is idempotent, the absence of a
second caller stops being load-bearing at all. So this file guards (a) —
a second invocation against already-reaped state is a silent no-op, not an
error and not a second reap — and deliberately does not assert (b), which
would freeze a caller graph rather than a behaviour.

Liveness is stubbed, never real: ``cs_claim_holder_live`` is monkeypatched
on ``reap``'s own namespace (the name the module resolves at call time).
A dead holder is the ONLY shape under test — a live holder's claim is never
reaped on any pass, which is a different invariant with its own coverage.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from coordinator_core.ops.session import reap


def _plant_claim(sessions_dir: Path, subdir: str, name: str) -> Path:
    """Plant one claim dir with a holder file, as a claimed record leaves it."""
    claim_dir = sessions_dir / subdir / name
    claim_dir.mkdir(parents=True, exist_ok=True)
    (claim_dir / "holder").write_text("dead-session-id\n", encoding="utf-8")
    return claim_dir


@pytest.fixture
def dead_holder(monkeypatch):
    """Every claim's holder reads as not-live, on every call."""
    monkeypatch.setattr(reap, "cs_claim_holder_live", lambda _path: False)
    # handoff-claims takes a frontmatter-reconcile pass before rmtree; it is
    # not what this test measures, and it reaches outside the tmp hub.
    monkeypatch.setattr(
        reap, "reconcile_dead_handoff_claim_frontmatter", lambda *a, **k: None
    )


def test_second_cull_pass_is_a_silent_no_op(tmp_path, dead_holder):
    """AC: the invariant the brief.py comment asserts but nothing tested.

    Pass 1 reaps every planted claim. Pass 2, against the state pass 1 left,
    reaps nothing, defers nothing, and fails nothing — the same result a
    never-claimed hub returns. A double-fire is therefore harmless
    independent of which ceremonies happen to call it.
    """
    sessions = tmp_path / "coordinator-sessions"
    planted = [
        _plant_claim(sessions, "handoff-claims", "2026-08-26-a-handoff"),
        _plant_claim(sessions, "memo-claims", "2026-08-26-a-memo"),
        _plant_claim(sessions, "plan-claims", "2026-08-26-a-plan"),
    ]

    reaped_1, deferred_1, failed_1 = reap._reap_orphaned_claims(sessions)

    assert failed_1 == [], (reaped_1, deferred_1, failed_1)
    assert deferred_1 == [], (reaped_1, deferred_1, failed_1)
    assert sorted(reaped_1) == [
        "handoff-claims/2026-08-26-a-handoff",
        "memo-claims/2026-08-26-a-memo",
        "plan-claims/2026-08-26-a-plan",
    ]
    for claim_dir in planted:
        assert not claim_dir.exists(), claim_dir

    reaped_2, deferred_2, failed_2 = reap._reap_orphaned_claims(sessions)

    assert (reaped_2, deferred_2, failed_2) == ([], [], []), (
        "second cull pass against already-reaped state must be a silent no-op"
    )


def test_second_cull_pass_leaves_surviving_claims_alone(tmp_path, monkeypatch):
    """A live holder survives BOTH passes — idempotency must not be reached
    by the cheap route of "the second pass reaps whatever the first left"."""
    sessions = tmp_path / "coordinator-sessions"
    live = _plant_claim(sessions, "memo-claims", "2026-08-26-live-memo")
    dead = _plant_claim(sessions, "memo-claims", "2026-08-26-dead-memo")

    monkeypatch.setattr(
        reap, "cs_claim_holder_live", lambda path: "live-memo" in str(path)
    )

    reaped_1, _, failed_1 = reap._reap_orphaned_claims(sessions)
    assert failed_1 == []
    assert reaped_1 == ["memo-claims/2026-08-26-dead-memo"]

    reaped_2, deferred_2, failed_2 = reap._reap_orphaned_claims(sessions)

    assert (reaped_2, deferred_2, failed_2) == ([], [], [])
    assert live.exists()
    assert not dead.exists()


def test_per_repo_wrapper_is_idempotent_too(tmp_path, monkeypatch, dead_holder):
    """``_reap_claims_for_target`` is the surface the ceremony directive
    actually fires (via ``session.reap_claims_for_repos``), so the guard runs
    at that boundary as well — not only on the predicate underneath it.

    ``git_common_dir`` is stubbed to the tmp hub's parent: this test is about
    a repeated cull, not about git, and calling it for real would put a
    subprocess spawn on a test that needs none.
    """
    common_dir = tmp_path / ".git"
    sessions = common_dir / "coordinator-sessions"
    _plant_claim(sessions, "plan-claims", "2026-08-26-a-plan")

    monkeypatch.setattr(reap, "git_common_dir", lambda _target: common_dir)

    reaped_1, deferred_1, failed_1 = reap._reap_claims_for_target(tmp_path)
    assert (deferred_1, failed_1) == ([], [])
    assert reaped_1 == ["plan-claims/2026-08-26-a-plan"]

    assert reap._reap_claims_for_target(tmp_path) == ([], [], [])
