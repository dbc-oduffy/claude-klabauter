"""
coordinator_core.orient_assemble.tests.test_reconcile_surface_legibility —
pins that `readers_branch_reconcile._read_auto_reconcile` never lets a
reader mistake "N shown" for "N exist."

`_read_auto_reconcile` caps `surfaced[]` at `_AUTO_RECONCILE_JUDGMENT_POINT_CAP`
via the shared `reader_result.cap_judgment_points` helper. That helper
already implements shape (a) from this chunk's two admissible options — an
aggregate judgment point naming the true surfaced total — rather than shape
(b), a real ranking key: `surfaced[]` has no meaningful priority field to
rank by (see `_read_auto_reconcile`'s own "Cap-order note"), so inventing
one would fake a signal that does not exist. This module asserts that
choice holds at both boundaries: the over-cap case (an aggregate naming the
true total) and the exact-cap boundary (nothing withheld, so no aggregate
is needed or emitted).

Spec backlink: pln-legible-reconcile-surface-and-33eb9a, chunk C1
Spec backlink: docs/decisions/DR-300-pickup-may-not-call-the-reconcile-orchestrator.md

Negative-spec:
    - Does NOT assert a ranking/ordering key on `surfaced[]` — this file's
      whole premise is that no such key exists; asserting one would
      contradict `_read_auto_reconcile`'s own documented reasoning.
    - Does NOT raise or reference `_AUTO_RECONCILE_JUDGMENT_POINT_CAP` as
      something needing a higher value — the cap is fixed; see this plan's
      Anti-scope.
"""

from __future__ import annotations

from coordinator_core.orient_assemble import readers_branch_reconcile as rbr


def _fake_surfaced(n: int) -> list[dict]:
    return [
        {
            "handoff_id": f"h-{i}",
            "reason": "gate_eval verdict=surface",
            "evidence": "x",
        }
        for i in range(n)
    ]


def _patch_response(monkeypatch, surfaced: list[dict], gates_cleared: list[dict] | None = None):
    import coordinator_core.ops.check_auto_reconcile as check_auto_reconcile

    result: dict = {"surfaced": surfaced}
    if gates_cleared is not None:
        result["gates_cleared"] = gates_cleared
    monkeypatch.setattr(
        check_auto_reconcile,
        "get_response",
        lambda: {"result": result},
    )


def test_over_cap_overflow_names_the_true_surfaced_total(monkeypatch):
    """Shape (a): when `surfaced[]` exceeds the cap, the overflow judgment
    point's `evidence` names the true total — the concrete measure that
    licenses "5 shown" != "5 exist" being distinguishable at all."""
    total = rbr._AUTO_RECONCILE_JUDGMENT_POINT_CAP + 119  # e.g. ~124 surfaced
    _patch_response(monkeypatch, _fake_surfaced(total))

    result = rbr._read_auto_reconcile()

    overflow = [
        jp
        for jp in result.judgment_points
        if jp["id"] == "j-overflow-auto-reconcile"
    ]
    assert len(overflow) == 1
    evidence = overflow[0]["evidence"]
    assert str(total) in evidence
    assert str(rbr._AUTO_RECONCILE_JUDGMENT_POINT_CAP) in evidence
    withheld = total - rbr._AUTO_RECONCILE_JUDGMENT_POINT_CAP
    assert str(withheld) in evidence
    assert "check-auto-reconcile" in evidence

    kept = [
        jp
        for jp in result.judgment_points
        if jp["id"].startswith("j-auto-reconcile-")
    ]
    assert len(kept) == rbr._AUTO_RECONCILE_JUDGMENT_POINT_CAP


def test_exact_cap_boundary_emits_no_overflow(monkeypatch):
    """Surfaced count == cap exactly: every surfaced entry is shown, so no
    aggregate is needed — the absence of `j-overflow-auto-reconcile` here
    IS the "5-of-5" signal, not a silent truncation."""
    cap = rbr._AUTO_RECONCILE_JUDGMENT_POINT_CAP
    _patch_response(monkeypatch, _fake_surfaced(cap))

    result = rbr._read_auto_reconcile()

    overflow = [
        jp
        for jp in result.judgment_points
        if jp["id"] == "j-overflow-auto-reconcile"
    ]
    kept = [
        jp
        for jp in result.judgment_points
        if jp["id"].startswith("j-auto-reconcile-")
    ]
    assert overflow == []
    assert len(kept) == cap
    assert len(result.judgment_points) == cap


def test_dry_run_gate_clear_with_blockers_renders_judgment_point(monkeypatch):
    """A clear verdict computed under dry_run=true never reaches surfaced[]
    (see readers_branch_reconcile's module docstring) -- it must still
    surface here via gates_cleared[], or the dry-run steady state is
    indistinguishable from "nothing to do.\""""
    gates_cleared = [
        {
            "handoff_id": "h-100",
            "verdict": "clear",
            "dry_run": True,
            "applied": False,
            "blocker_ids": ["b-001", "b-002"],
        }
    ]
    _patch_response(monkeypatch, [], gates_cleared)

    result = rbr._read_auto_reconcile()

    gate_points = [
        jp for jp in result.judgment_points if jp["id"] == "j-gate-cleared-1"
    ]
    assert len(gate_points) == 1
    evidence = gate_points[0]["evidence"]
    assert "b-001" in evidence and "b-002" in evidence
    assert "dry_run=true" in evidence


def test_narrow_verdict_also_renders_judgment_point(monkeypatch):
    gates_cleared = [
        {
            "handoff_id": "h-101",
            "verdict": "narrow",
            "dry_run": True,
            "applied": False,
            "blocker_ids": ["b-003"],
        }
    ]
    _patch_response(monkeypatch, [], gates_cleared)

    result = rbr._read_auto_reconcile()

    gate_points = [
        jp for jp in result.judgment_points if jp["id"] == "j-gate-cleared-1"
    ]
    assert len(gate_points) == 1
    assert "narrow" in gate_points[0]["question"]


def test_non_dry_run_empty_blockers_gate_clear_stays_silent(monkeypatch):
    """`_route_gate_clear`'s own guard (`if dry_run or not blocker_ids`) means
    a non-dry-run entry with empty blocker_ids is a genuine no-op -- nothing
    to announce."""
    gates_cleared = [
        {
            "handoff_id": "h-102",
            "verdict": "clear",
            "dry_run": False,
            "applied": False,
            "blocker_ids": [],
        }
    ]
    _patch_response(monkeypatch, [], gates_cleared)

    result = rbr._read_auto_reconcile()

    assert result.judgment_points == []


def test_absent_gates_cleared_key_stays_silent(monkeypatch):
    _patch_response(monkeypatch, [])

    result = rbr._read_auto_reconcile()

    assert result.judgment_points == []


def test_malformed_gates_cleared_type_stays_silent(monkeypatch):
    import coordinator_core.ops.check_auto_reconcile as check_auto_reconcile

    monkeypatch.setattr(
        check_auto_reconcile,
        "get_response",
        lambda: {"result": {"surfaced": [], "gates_cleared": "not-a-list"}},
    )

    result = rbr._read_auto_reconcile()

    assert result.judgment_points == []


def test_gate_cleared_family_caps_and_names_true_total(monkeypatch):
    total = rbr._AUTO_RECONCILE_JUDGMENT_POINT_CAP + 3
    gates_cleared = [
        {
            "handoff_id": f"h-2{i}",
            "verdict": "clear",
            "dry_run": True,
            "applied": False,
            "blocker_ids": [f"b-{i}"],
        }
        for i in range(total)
    ]
    _patch_response(monkeypatch, [], gates_cleared)

    result = rbr._read_auto_reconcile()

    overflow = [
        jp for jp in result.judgment_points if jp["id"] == "j-overflow-gate-cleared"
    ]
    assert len(overflow) == 1
    kept = [
        jp for jp in result.judgment_points if jp["id"].startswith("j-gate-cleared-")
    ]
    assert len(kept) == rbr._AUTO_RECONCILE_JUDGMENT_POINT_CAP


def test_surfaced_and_gate_cleared_both_render_independently(monkeypatch):
    surfaced = _fake_surfaced(1)
    gates_cleared = [
        {
            "handoff_id": "h-300",
            "verdict": "clear",
            "dry_run": True,
            "applied": False,
            "blocker_ids": ["b-300"],
        }
    ]
    _patch_response(monkeypatch, surfaced, gates_cleared)

    result = rbr._read_auto_reconcile()

    ids = {jp["id"] for jp in result.judgment_points}
    assert "j-auto-reconcile-1" in ids
    assert "j-gate-cleared-1" in ids
