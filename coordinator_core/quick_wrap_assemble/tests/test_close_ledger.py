"""
Tests for `coordinator_core.quick_wrap_assemble._close_ledger`.

Spec: state/sizings/2026-09-06-close-out-emits-what-is-discharged-so-th.yaml —
the close ceremony must name what THIS session discharged, mechanically,
rather than leave an EM to compose a residual "still open" section that can
re-open an item already closed by the very diff being reported or by a
landed bug-backlog row.

Negative-spec covered here:
    - A degraded or untrustworthy leg produces an explicit
      `could-not-establish` entry, never silent omission (omission would
      read as "nothing discharged here", the same class of false signal in
      the opposite direction).
    - `_close_ledger` never emits an "open"/"remaining" entry for anything —
      it only names what closed and its landing artifact.
"""
from __future__ import annotations

from typing import Any

from coordinator_core.quick_wrap_assemble import (
    _CLOSE_LEDGER_NARRATION,
    _LEDGER_COULD_NOT_ESTABLISH,
    _LEDGER_DISCHARGED,
    _close_ledger,
)


def _computed_pickup(actioned_memos: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {
        "degraded": False,
        "value": {
            "classification": "memo" if actioned_memos else "none",
            "artifact_path": None,
            "basename": None,
            "deliverable_id": None,
            "actioned_memos": actioned_memos or [],
            "consumed_predecessor": False,
        },
        "source": "session_pickup_kind",
        "collision": None,
    }


def _computed_plan(*, present: bool, status: str | None = None) -> dict[str, Any]:
    return {
        "degraded": False,
        "value": {
            "present": present,
            "path": "docs/plans/example.md" if present else None,
            "status": status,
            "scope_mode": "spec-dispatch" if present else None,
            "slug": "example" if present else None,
            "terminal_write_owed": status == "landed",
        },
        "source": "session_governing_plan",
        "collision": None,
    }


def _computed_diff(
    *, trustworthy: bool = True, commit_count: int = 3, scoping_method: str = "session-trailer"
) -> dict[str, Any]:
    return {
        "degraded": False,
        "value": {
            "scoping_method": scoping_method,
            "trustworthy": trustworthy,
            "sha_range": "abc123..def456",
            "commit_count": commit_count,
            "surface_count": 2,
            "touched_paths": ["a.py", "b.py"],
        },
        "source": "session_diff_brightline",
        "collision": None,
    }


def _close_gate(**overrides: Any) -> dict[str, Any]:
    gate = {
        "pickup_kind": _computed_pickup(),
        "governing_plan": _computed_plan(present=False),
        "diff": _computed_diff(),
    }
    gate.update(overrides)
    return gate


# ---------------------------------------------------------------------------
# Shape
# ---------------------------------------------------------------------------


def test_ledger_entries_carry_item_and_status():
    entries = _close_ledger(_close_gate(diff=_computed_diff(commit_count=0)))
    for entry in entries:
        assert "item" in entry
        assert entry["status"] in (_LEDGER_DISCHARGED, _LEDGER_COULD_NOT_ESTABLISH)
        if entry["status"] == _LEDGER_DISCHARGED:
            assert "discharged_by" in entry
        else:
            assert "evidence" in entry


def test_nothing_to_report_emits_no_entries():
    gate = _close_gate(diff=_computed_diff(commit_count=0))
    entries = _close_ledger(gate)
    assert entries == []


# ---------------------------------------------------------------------------
# Actioned-memo entry appears with its decision
# ---------------------------------------------------------------------------


def test_actioned_memo_appears_with_its_decision():
    gate = _close_gate(
        pickup_kind=_computed_pickup(
            actioned_memos=[{"basename": "2026-09-01-example.md", "decision": "partial"}]
        ),
        diff=_computed_diff(commit_count=0),
    )
    entries = _close_ledger(gate)
    memo_entries = [e for e in entries if "2026-09-01-example.md" in e["item"]]
    assert len(memo_entries) == 1
    entry = memo_entries[0]
    assert entry["status"] == _LEDGER_DISCHARGED
    assert "partial" in entry["discharged_by"]


# ---------------------------------------------------------------------------
# Governing plan and diff discharge entries
# ---------------------------------------------------------------------------


def test_governing_plan_present_names_its_status():
    gate = _close_gate(
        governing_plan=_computed_plan(present=True, status="landed"),
        diff=_computed_diff(commit_count=0),
    )
    entries = _close_ledger(gate)
    plan_entries = [e for e in entries if "docs/plans/example.md" in e["item"]]
    assert len(plan_entries) == 1
    assert plan_entries[0]["status"] == _LEDGER_DISCHARGED
    assert "landed" in plan_entries[0]["discharged_by"]


def test_diff_commit_range_is_the_landing_artifact():
    gate = _close_gate(diff=_computed_diff(commit_count=5))
    entries = _close_ledger(gate)
    diff_entries = [e for e in entries if e["item"] == "session code changes"]
    assert len(diff_entries) == 1
    assert diff_entries[0]["status"] == _LEDGER_DISCHARGED
    assert "abc123..def456" in diff_entries[0]["discharged_by"]
    assert "5 commit" in diff_entries[0]["discharged_by"]


# ---------------------------------------------------------------------------
# Degraded / untrustworthy inputs produce could-not-establish, never omission
# ---------------------------------------------------------------------------


def test_degraded_pickup_kind_produces_could_not_establish():
    gate = _close_gate(
        pickup_kind={"degraded": True, "evidence": "probe failed", "source": "x"},
        diff=_computed_diff(commit_count=0),
    )
    entries = _close_ledger(gate)
    matches = [e for e in entries if e["item"] == "memos actioned this session"]
    assert len(matches) == 1
    assert matches[0]["status"] == _LEDGER_COULD_NOT_ESTABLISH
    assert matches[0]["evidence"] == "probe failed"


def test_degraded_governing_plan_produces_could_not_establish():
    gate = _close_gate(
        governing_plan={"degraded": True, "evidence": "plan probe degraded", "source": "x"},
        diff=_computed_diff(commit_count=0),
    )
    entries = _close_ledger(gate)
    matches = [e for e in entries if e["item"] == "governing plan"]
    assert len(matches) == 1
    assert matches[0]["status"] == _LEDGER_COULD_NOT_ESTABLISH
    assert matches[0]["evidence"] == "plan probe degraded"


def test_degraded_diff_produces_could_not_establish():
    gate = _close_gate(diff={"degraded": True, "evidence": "diff probe degraded", "source": "x"})
    entries = _close_ledger(gate)
    matches = [e for e in entries if e["item"] == "session code changes"]
    assert len(matches) == 1
    assert matches[0]["status"] == _LEDGER_COULD_NOT_ESTABLISH
    assert matches[0]["evidence"] == "diff probe degraded"


def test_untrustworthy_diff_produces_could_not_establish_not_omission():
    gate = _close_gate(diff=_computed_diff(trustworthy=False, scoping_method="ambiguous-x-node"))
    entries = _close_ledger(gate)
    matches = [e for e in entries if e["item"] == "session code changes"]
    assert len(matches) == 1
    assert matches[0]["status"] == _LEDGER_COULD_NOT_ESTABLISH
    assert "ambiguous-x-node" in matches[0]["evidence"]


def test_untrustworthy_diff_never_reads_as_discharged():
    gate = _close_gate(diff=_computed_diff(trustworthy=False))
    entries = _close_ledger(gate)
    diff_entries = [e for e in entries if e["item"] == "session code changes"]
    assert all(e["status"] != _LEDGER_DISCHARGED for e in diff_entries)


# ---------------------------------------------------------------------------
# Narration
# ---------------------------------------------------------------------------


def test_narration_constant_instructs_verbatim_push_not_composition():
    assert "verbatim" in _CLOSE_LEDGER_NARRATION
    assert "close_ledger" in _CLOSE_LEDGER_NARRATION
    assert "residual" in _CLOSE_LEDGER_NARRATION or "open-items" in _CLOSE_LEDGER_NARRATION
