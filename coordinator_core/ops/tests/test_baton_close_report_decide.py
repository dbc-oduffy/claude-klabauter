"""Table-driven rules and precedence for baton_close_report_decide.decide."""

from __future__ import annotations

import pytest

from coordinator_core.ops.baton_close_report_contract import ResidueReason
from coordinator_core.ops.baton_close_report_decide import (
    STATUS_MISSING,
    STATUS_UNREADABLE,
    close_refusal,
    decide,
)


def _h(status: str = "open") -> dict:
    return {
        "path": "state/handoffs/x.md",
        "status": status,
        "kind": "session-handoff",
        "deliverable_id": "dlv-x",
        "predecessor": None,
        "governing_plan": None,
        "origin_plan_id": None,
        "sizing_object": None,
        "roadmap_id": None,
        "origin_goal_id": [],
    }


def _e(cls: str, status: str | None, done: bool) -> dict:
    return {
        "class": cls,
        "field": "f",
        "target_path": f"t/{cls}",
        "target_status": status,
        "done": done,
    }


DONE = _e("plan", "implemented", True)
DONE2 = _e("roadmap", "complete", True)
OPEN = _e("sizing", "open", False)
MISSING = _e("goal", STATUS_MISSING, False)
UNREAD = _e("goal", STATUS_UNREADABLE, False)
NONE_STATUS = _e("goal", None, False)


@pytest.mark.parametrize(
    "status,evidence,successor,expected",
    [
        ("open", [], False, ("no_evidence",)),
        ("open", [], True, ("no_evidence",)),
        ("claimed", [], False, ("no_evidence",)),
        ("open", [DONE], False, ("closable",)),
        ("open", [DONE, DONE2], False, ("closable",)),
        ("open", [DONE, MISSING], False, ("closable",)),
        ("open", [DONE, UNREAD], False, ("closable",)),
        ("claimed", [DONE], False, ("residue", "claimed")),
        ("open", [DONE], True, ("residue", "live_successor")),
        ("open", [DONE, OPEN], False, ("residue", "evidence_conflict")),
        ("open", [MISSING], False, ("residue", "target_missing")),
        ("open", [UNREAD], False, ("residue", "target_unreadable")),
        ("open", [NONE_STATUS], False, ("residue", "target_unstatused")),
        ("open", [NONE_STATUS, MISSING], False, ("residue", "target_unstatused")),
        ("open", [UNREAD, NONE_STATUS], False, ("residue", "target_unreadable")),
        ("open", [MISSING, UNREAD], False, ("residue", "target_unreadable")),
        ("open", [OPEN], False, ("no_evidence",)),
        ("open", [OPEN, MISSING], False, ("residue", "target_missing")),
        # precedence
        ("claimed", [DONE], True, ("residue", "claimed")),
        ("claimed", [DONE, OPEN], True, ("residue", "claimed")),
        ("open", [DONE, OPEN], True, ("residue", "live_successor")),
        ("open", [MISSING], True, ("residue", "live_successor")),
        ("claimed", [MISSING], False, ("residue", "target_missing")),
    ],
)
def test_rules(status, evidence, successor, expected):
    out = decide(_h(status), evidence, successor)
    assert out[: len(expected)] == expected
    if expected[0] == "no_evidence":
        assert out == expected
    elif expected[0] == "closable":
        assert out[1] == evidence
    else:
        assert out[2] == evidence


def _hk(kind, did, status: str = "open") -> dict:
    h = _h(status)
    h["kind"] = kind
    h["deliverable_id"] = did
    return h


@pytest.mark.parametrize(
    "kind,did,status,successor,evidence,expected",
    [
        ("spinoff", "dlv-x", "open", False, [DONE], ("residue", "spinoff_kind")),
        (" Spinoff ", "dlv-x", "open", False, [DONE], ("residue", "spinoff_kind")),
        ("spinoff", "x", "open", False, [DONE], ("residue", "no_deliverable_id")),
        ("session-handoff", None, "open", False, [DONE], ("residue", "no_deliverable_id")),
        ("session-handoff", 7, "open", False, [DONE], ("residue", "no_deliverable_id")),
        ("session-handoff", "  dlv-x ", "open", False, [DONE], ("closable",)),
        ("session-handoff", "dlv-x", "open", False, [DONE], ("closable",)),
        # precedence: claimed, successor and conflict outrank the header refusal
        ("spinoff", "dlv-x", "claimed", False, [DONE], ("residue", "claimed")),
        ("spinoff", "dlv-x", "open", True, [DONE], ("residue", "live_successor")),
        ("spinoff", "dlv-x", "open", False, [DONE, OPEN], ("residue", "evidence_conflict")),
        # no done evidence: unchanged
        ("spinoff", "x", "open", False, [], ("no_evidence",)),
        ("spinoff", "x", "open", False, [OPEN], ("no_evidence",)),
        ("spinoff", "x", "open", False, [MISSING], ("residue", "target_missing")),
    ],
)
def test_header_refusal_gates_closable(kind, did, status, successor, evidence, expected):
    out = decide(_hk(kind, did, status), evidence, successor)
    assert out[: len(expected)] == expected


@pytest.mark.parametrize(
    "kind,did,expected",
    [
        ("session-handoff", "dlv-x", None),
        ("spinoff", "dlv-x", ResidueReason.SPINOFF_KIND),
        ("spinoff", "nope", ResidueReason.NO_DELIVERABLE_ID),
        (None, None, ResidueReason.NO_DELIVERABLE_ID),
        (None, "dlv-x", None),
    ],
)
def test_close_refusal(kind, did, expected):
    assert close_refusal(kind, did) is expected


def test_does_not_mutate_inputs():
    ev = [dict(DONE), dict(OPEN)]
    snapshot = [dict(e) for e in ev]
    decide(_h(), ev, False)
    assert ev == snapshot
