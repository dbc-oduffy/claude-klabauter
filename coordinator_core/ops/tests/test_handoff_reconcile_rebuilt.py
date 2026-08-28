"""
coordinator_core.ops.tests.test_handoff_reconcile_rebuilt — C4 rebuild fixtures for
`handoff.reconcile_open`.

Covers the requirements table (`state/audits/2026-08-25-where-reconcile-open-spends-its-cpu.md`)
this rebuild discharges, narrowed to the gate-evaluation half (C10 killed the shipped-ness half):

  - R2  — `not-cleared` is load-bearing silence (no array entry, no history-map entry).
  - R3  — narrow+surface composite (both a `gates_cleared[]` row AND a `surfaced[]` row).
  - R4  — DR-266 § 93 fall-through is reproduced, not closed (an unrecognized verdict silently
          falls through with no entry, no raise).
  - R5  — writer in every mode, dry-run included (`surfaced-history.json` always written, even
          with an empty map).
  - R6/R6b (DR-299) — `dry_run` resolution is policy-authoritative; a caller override requires a
          non-empty `dry_run_override_reason`, else refused.
  - R8  — `repo_root is None` -> `exit_code: 1`, both arrays empty.
  - R12 (DR-320) — `gate_evidence` present but not consumed by `evaluate_gate`'s rule 0 forces
          the handoff onto `surfaced[]`-only, regardless of `verdict`.

Uses monkeypatch to substitute the corpus/gate/policy/transition seams with in-memory fixtures —
this module never touches the real repo's `state/handoffs/` corpus or git common dir.

Spec backlink: docs/plans/2026-08-25-reconcile-open-comes-back-under-the-bar.md § C4
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

from coordinator_core.ops import handoff_reconcile as hr


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# _resolve_dry_run (R6/R6b, DR-299)
# ---------------------------------------------------------------------------


def test_resolve_dry_run_defaults_true_on_absent_policy_key():
    dry_run, override = hr._resolve_dry_run({}, {})
    assert dry_run is True
    assert override is None


def test_resolve_dry_run_policy_authoritative_no_caller_param():
    dry_run, override = hr._resolve_dry_run({"dry_run": False}, {})
    assert dry_run is False
    assert override is None


def test_resolve_dry_run_caller_agrees_is_not_an_override():
    dry_run, override = hr._resolve_dry_run({"dry_run": True}, {"dry_run": True})
    assert dry_run is True
    assert override is None


def test_resolve_dry_run_override_refused_without_reason():
    dry_run, override = hr._resolve_dry_run({"dry_run": True}, {"dry_run": False})
    assert dry_run is True  # policy wins
    assert override == {
        "applied": False,
        "policy_dry_run": True,
        "requested_dry_run": False,
        "reason": None,
    }


def test_resolve_dry_run_override_applied_with_reason():
    dry_run, override = hr._resolve_dry_run(
        {"dry_run": True},
        {"dry_run": False, "dry_run_override_reason": "test injection"},
    )
    assert dry_run is False
    assert override["applied"] is True
    assert override["reason"] == "test injection"


def test_resolve_dry_run_malformed_policy_value_fails_conservative():
    dry_run, override = hr._resolve_dry_run({"dry_run": "nope"}, {})
    assert dry_run is True
    assert override is None


# ---------------------------------------------------------------------------
# _handoff_identifier / _history_path / _save_surfaced_history
# ---------------------------------------------------------------------------


def test_handoff_identifier_prefers_handoff_id():
    assert hr._handoff_identifier({"handoff_id": "hnd-x-abc123", "id": "01"}) == "hnd-x-abc123"


def test_handoff_identifier_falls_back_to_path():
    assert hr._handoff_identifier({"_path": "state/handoffs/x.md"}) == "state/handoffs/x.md"


def test_history_path_is_under_common_dir_coordinator_sessions(tmp_path):
    p = hr._history_path(tmp_path)
    assert p == tmp_path / "coordinator-sessions" / "reconcile-history" / "surfaced-history.json"


def test_save_surfaced_history_writes_even_when_empty(tmp_path):
    history_path = hr._history_path(tmp_path)
    hr._save_surfaced_history(history_path, {})
    assert history_path.is_file()
    data = json.loads(history_path.read_text(encoding="utf-8"))
    assert data == {"surfaced": {}}


def test_save_surfaced_history_survives_unwritable_target(tmp_path, monkeypatch):
    # A write failure must not raise out of this helper (R5's own best-effort contract).
    bogus = tmp_path / "nonexistent-parent-that-is-a-file"
    bogus.write_text("x", encoding="utf-8")
    target = bogus / "surfaced-history.json"
    hr._save_surfaced_history(target, {"h1": "state/handoffs/h1.md"})  # must not raise


# ---------------------------------------------------------------------------
# _handler — R8 repo_root guard
# ---------------------------------------------------------------------------


def test_handler_repo_root_none_returns_exit_code_1():
    result = _run(hr._handler({}, repo_root=None))
    assert result == {"gates_cleared": [], "surfaced": [], "exit_code": 1}


# ---------------------------------------------------------------------------
# _handler end-to-end, seams substituted
# ---------------------------------------------------------------------------


class _FakePolicyResult:
    def __init__(self, policy: Dict[str, Any]) -> None:
        self.policy = policy


def _patch_common_seams(
    monkeypatch,
    *,
    open_handoffs: List[Dict[str, Any]],
    all_handoffs: Optional[List[Dict[str, Any]]] = None,
    policy: Optional[Dict[str, Any]] = None,
    gate_evidence_by_path: Optional[Dict[str, Dict[str, Any]]] = None,
    gate_results_by_id: Optional[Dict[str, Dict[str, Any]]] = None,
    gate_cascade_clear_result: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Wire every external seam `_handler` calls to caller-supplied fixtures.

    Returns a dict of call-recording lists the test can assert on
    (`gate_cascade_clear_calls`).
    """
    monkeypatch.setattr(hr, "main_worktree_root", lambda repo_root: repo_root)
    monkeypatch.setattr(hr, "_collect_open_handoffs", lambda worktree: open_handoffs)
    monkeypatch.setattr(
        hr,
        "_collect_all_handoffs_for_gate_index",
        lambda worktree: (all_handoffs if all_handoffs is not None else open_handoffs, []),
    )
    monkeypatch.setattr(hr, "load_policy", lambda policy_path=None: _FakePolicyResult(policy or {}))

    gate_evidence_by_path = gate_evidence_by_path or {}
    monkeypatch.setattr(
        hr,
        "_read_gate_evidence_resolved",
        lambda path, today: gate_evidence_by_path.get(str(path)),
    )

    gate_results_by_id = gate_results_by_id or {}

    def _fake_evaluate_gate(handoff, live_and_archived, **kwargs):
        hid = hr._handoff_identifier(handoff)
        return gate_results_by_id.get(
            hid, {"verdict": "not-cleared", "evidence": [], "also_surface": False}
        )

    monkeypatch.setattr(hr, "evaluate_gate", _fake_evaluate_gate)

    def _fake_consumes_gate_evidence(handoff, gate_evidence):
        # Default: consumed iff evidence was supplied at all (tests override per-case below).
        return gate_evidence is not None

    monkeypatch.setattr(hr, "consumes_gate_evidence", _fake_consumes_gate_evidence)

    calls: Dict[str, Any] = {"gate_cascade_clear_calls": []}

    def _fake_gate_cascade_clear(handoff_path, blocker_ids, blocker_shas, worktree, repo_root):
        calls["gate_cascade_clear_calls"].append(
            (handoff_path, blocker_ids, blocker_shas, worktree, repo_root)
        )
        return gate_cascade_clear_result or {"applied": True, "exit_code": 0}

    monkeypatch.setattr(hr, "_gate_cascade_clear", _fake_gate_cascade_clear)

    return calls


def test_handler_not_cleared_is_silent(tmp_path, monkeypatch):
    handoff = {
        "handoff_id": "hnd-a-000001",
        "deployment_state": "awaiting_gate",
        "_path": str(tmp_path / "a.md"),
    }
    _patch_common_seams(
        monkeypatch,
        open_handoffs=[handoff],
        gate_results_by_id={"hnd-a-000001": {"verdict": "not-cleared", "evidence": []}},
    )
    result = _run(hr._handler({}, repo_root=tmp_path))
    assert result["gates_cleared"] == []
    assert result["surfaced"] == []
    assert result["exit_code"] == 0
    history = json.loads(hr._history_path(tmp_path).read_text(encoding="utf-8"))
    assert history == {"surfaced": {}}


def test_handler_narrow_plus_surface_composite(tmp_path, monkeypatch):
    handoff = {
        "handoff_id": "hnd-b-000002",
        "deployment_state": "awaiting_gate",
        "_path": str(tmp_path / "b.md"),
    }
    _patch_common_seams(
        monkeypatch,
        open_handoffs=[handoff],
        gate_results_by_id={
            "hnd-b-000002": {
                "verdict": "narrow",
                "also_surface": True,
                "cleared_blocker_ids": ["hnd-x"],
                "cleared_by_shas": ["deadbeef"],
                "evidence": ["dead blocker abandoned"],
            }
        },
    )
    result = _run(hr._handler({}, repo_root=tmp_path))
    assert len(result["gates_cleared"]) == 1
    assert result["gates_cleared"][0]["verdict"] == "narrow"
    assert len(result["surfaced"]) == 1
    assert result["surfaced"][0]["handoff_id"] == "hnd-b-000002"


def test_handler_surface_routes_to_surfaced_only(tmp_path, monkeypatch):
    handoff = {
        "handoff_id": "hnd-c-000003",
        "deployment_state": "awaiting_gate",
        "_path": str(tmp_path / "c.md"),
    }
    _patch_common_seams(
        monkeypatch,
        open_handoffs=[handoff],
        gate_results_by_id={"hnd-c-000003": {"verdict": "surface", "evidence": ["asymmetry"]}},
    )
    result = _run(hr._handler({}, repo_root=tmp_path))
    assert result["gates_cleared"] == []
    assert len(result["surfaced"]) == 1
    history = json.loads(hr._history_path(tmp_path).read_text(encoding="utf-8"))
    assert history["surfaced"]["hnd-c-000003"] == str(tmp_path / "c.md")


def test_handler_clear_dry_run_true_never_calls_cascade_clear(tmp_path, monkeypatch):
    handoff = {
        "handoff_id": "hnd-d-000004",
        "deployment_state": "awaiting_gate",
        "_path": str(tmp_path / "d.md"),
    }
    calls = _patch_common_seams(
        monkeypatch,
        open_handoffs=[handoff],
        policy={"dry_run": True},
        gate_results_by_id={
            "hnd-d-000004": {
                "verdict": "clear",
                "cleared_blocker_ids": ["hnd-x"],
                "cleared_by_shas": ["deadbeef"],
                "evidence": [],
            }
        },
    )
    result = _run(hr._handler({}, repo_root=tmp_path))
    assert len(result["gates_cleared"]) == 1
    entry = result["gates_cleared"][0]
    assert entry["applied"] is False
    assert entry["dry_run"] is True
    assert calls["gate_cascade_clear_calls"] == []


def test_handler_clear_dry_run_false_invokes_cascade_clear(tmp_path, monkeypatch):
    handoff = {
        "handoff_id": "hnd-e-000005",
        "deployment_state": "awaiting_gate",
        "_path": str(tmp_path / "e.md"),
    }
    calls = _patch_common_seams(
        monkeypatch,
        open_handoffs=[handoff],
        policy={"dry_run": False},
        gate_results_by_id={
            "hnd-e-000005": {
                "verdict": "clear",
                "cleared_blocker_ids": ["hnd-x"],
                "cleared_by_shas": ["deadbeef"],
                "evidence": [],
            }
        },
        gate_cascade_clear_result={"applied": True, "exit_code": 0},
    )
    result = _run(hr._handler({}, repo_root=tmp_path))
    entry = result["gates_cleared"][0]
    assert entry["applied"] is True
    assert entry["dry_run"] is False
    assert len(calls["gate_cascade_clear_calls"]) == 1


def test_handler_gate_evidence_present_but_unconsumed_forces_surfaced_only(tmp_path, monkeypatch):
    """R12 (DR-320) — gate_evidence present, gate_evidence_resolved False -> surfaced[]-only,
    even though the underlying verdict says `clear` (which would otherwise auto-transition)."""
    handoff_path = str(tmp_path / "f.md")
    handoff = {
        "handoff_id": "hnd-f-000006",
        "deployment_state": "awaiting_gate",
        "_path": handoff_path,
    }
    calls = _patch_common_seams(
        monkeypatch,
        open_handoffs=[handoff],
        gate_evidence_by_path={handoff_path: {"covers_prose": False, "legs": []}},
        gate_results_by_id={
            "hnd-f-000006": {
                "verdict": "clear",
                "cleared_blocker_ids": [],
                "cleared_by_shas": [],
                "evidence": [],
            }
        },
    )
    # Force gate_evidence_resolved False regardless of presence.
    monkeypatch.setattr(hr, "consumes_gate_evidence", lambda handoff, gate_evidence: False)
    result = _run(hr._handler({}, repo_root=tmp_path))
    assert result["gates_cleared"] == []
    assert len(result["surfaced"]) == 1
    assert "gate_evidence_resolved=False" in result["surfaced"][0]["reason"] or "not consumed" in result["surfaced"][0]["reason"]
    assert calls["gate_cascade_clear_calls"] == []


def test_handler_unrecognized_verdict_falls_through_silently(tmp_path, monkeypatch):
    """R4 (DR-266 § 93) — reproduced fall-through: an unrecognized verdict value produces no
    array entry, no history-map entry, and does not raise."""
    handoff = {
        "handoff_id": "hnd-g-000007",
        "deployment_state": "awaiting_gate",
        "_path": str(tmp_path / "g.md"),
    }
    _patch_common_seams(
        monkeypatch,
        open_handoffs=[handoff],
        gate_results_by_id={"hnd-g-000007": {"verdict": "some-future-unrecognized-value", "evidence": []}},
    )
    result = _run(hr._handler({}, repo_root=tmp_path))
    assert result["gates_cleared"] == []
    assert result["surfaced"] == []
    assert result["exit_code"] == 0


def test_handler_skips_non_awaiting_gate_handoffs(tmp_path, monkeypatch):
    handoff = {
        "handoff_id": "hnd-h-000008",
        "deployment_state": "ready_to_fire",
        "_path": str(tmp_path / "h.md"),
    }
    _patch_common_seams(monkeypatch, open_handoffs=[handoff])
    result = _run(hr._handler({}, repo_root=tmp_path))
    assert result == {"gates_cleared": [], "surfaced": [], "exit_code": 0}


def test_handler_writes_surfaced_history_even_in_dry_run_with_nothing_surfaced(tmp_path, monkeypatch):
    """R5 — writer in every mode, dry-run included, even when nothing surfaced this pass."""
    _patch_common_seams(monkeypatch, open_handoffs=[], policy={"dry_run": True})
    _run(hr._handler({}, repo_root=tmp_path))
    history_path = hr._history_path(tmp_path)
    assert history_path.is_file()


# ---------------------------------------------------------------------------
# C3 — the live+archived gate index loads lazily, on first need, at most once.
# docs/plans/2026-08-27-one-corpus-read-or-the-housekeeping-job-dies-a-fourth-time.md
# ---------------------------------------------------------------------------


def _count_gate_index_loads(monkeypatch, open_handoffs) -> int:
    """Run one `_handler` pass and return how many times the live+archived
    gate-index walk was actually invoked.

    Counts CALLS, never wall clock. The claim under test is "fewer files
    read"; on a box running 50-70 concurrent sessions a timing assertion
    would measure the peers (see the plan's Anti-scope, "Do not measure in
    wall clock").
    """
    loads: list[str] = []

    _patch_common_seams(monkeypatch, open_handoffs=open_handoffs)

    def _counting(worktree):
        loads.append(str(worktree))
        return (open_handoffs, [])

    monkeypatch.setattr(hr, "_collect_all_handoffs_for_gate_index", _counting)
    _run(hr._handler({}, Path("/repo")))
    return len(loads)


def _handoff(name: str, state: str) -> Dict[str, Any]:
    return {
        "handoff_id": name,
        "_path": f"/repo/state/handoffs/{name}.md",
        "deployment_state": state,
        "blocked_by": "some-stub-id" if state == hr._AWAITING_GATE_STATE else None,
    }


def test_gate_index_is_not_walked_when_nothing_is_awaiting_gate(monkeypatch):
    """The whole point of C3: a cycle with no gated handoff pays nothing.

    Measured warm at 1ac97e6e1, this walk is 78-94ms p50 over 1,687 files
    (281ms on a cold first call), against a 200ms process-time bar. It was
    computed unconditionally until 2026-08-28 and consumed only for
    `awaiting_gate` handoffs -- 16 of 253 on the real corpus.
    """
    opens = [_handoff(f"open{i}", "ready_to_fire") for i in range(12)]
    assert _count_gate_index_loads(monkeypatch, opens) == 0


def test_gate_index_is_walked_once_when_something_is_awaiting_gate(monkeypatch):
    """Lazy, not removed -- an `awaiting_gate` handoff can legitimately name
    an ARCHIVED record via `blocked_by`, which is the entire reason the
    archived half of this walk exists."""
    opens = [_handoff(f"open{i}", "ready_to_fire") for i in range(12)]
    opens.append(_handoff("gated0", hr._AWAITING_GATE_STATE))
    assert _count_gate_index_loads(monkeypatch, opens) == 1


def test_gate_index_is_memoised_not_rewalked_per_gated_handoff(monkeypatch):
    """A memo, not a cache: five gated handoffs pay ONE walk, and it is
    discarded with the call.

    This is the assertion that fails if a later edit moves the load back
    inside the loop -- which would read as correct and cost five walks. It is
    not a claim that repeat reads are free; see the plan's Anti-scope, "Do not
    claim a win from a cache"."""
    opens = [_handoff(f"open{i}", "ready_to_fire") for i in range(12)]
    opens += [_handoff(f"gated{i}", hr._AWAITING_GATE_STATE) for i in range(5)]
    assert _count_gate_index_loads(monkeypatch, opens) == 1
