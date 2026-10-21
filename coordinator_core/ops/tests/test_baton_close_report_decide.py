"""Table-driven rules and precedence for baton_close_report_decide.decide."""

from __future__ import annotations

import pytest

from coordinator_core.ops.baton_close_report_contract import CONFLICT_RULE_OWN_PLAN, ResidueReason
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


OWN_PLAN_DONE = {**DONE, "field": "governing_plan"}
JOIN_DONE = {**DONE, "field": "plan.deliverable_id"}


@pytest.mark.parametrize(
    "evidence,expected",
    [
        ([OWN_PLAN_DONE], ("closable",)),
        ([JOIN_DONE, OWN_PLAN_DONE], ("closable",)),
        ([JOIN_DONE], ("residue", "spinoff_kind")),
        ([{**OWN_PLAN_DONE, "done": False, "target_status": "draft"}, JOIN_DONE],
         ("residue", "evidence_conflict")),
    ],
)
def test_spinoff_closes_only_on_its_own_governing_plan(evidence, expected):
    out = decide(_hk("spinoff", "dlv-x"), evidence, False)
    assert out[: len(expected)] == expected


def test_close_refusal_spinoff_with_own_plan_evidence_is_none():
    assert close_refusal("spinoff", "dlv-x", [OWN_PLAN_DONE]) is None
    assert close_refusal("spinoff", "dlv-x", [JOIN_DONE]) is ResidueReason.SPINOFF_KIND
    assert close_refusal("spinoff", "nope", [OWN_PLAN_DONE]) is ResidueReason.NO_DELIVERABLE_ID


RUN_DONE = {**_e("run", "committed", True), "field": "run.sizing_rel"}
RUN_ENDED = _e("run", "ended", False)
RUN_LIVE_ITEM = _e("run", "live", False)
PLAN_APPROVED = _e("plan", "approved", False)


def test_spinoff_closes_on_a_done_run():
    out = decide(_hk("spinoff", "dlv-x"), [RUN_DONE], False)
    assert out == ("closable", [RUN_DONE], None)
    assert close_refusal("spinoff", "dlv-x", [RUN_DONE]) is None


def test_spinoff_parent_join_still_refused_beside_an_ended_run():
    out = decide(_hk("spinoff", "dlv-x"), [JOIN_DONE, RUN_ENDED], False)
    assert out[:2] == ("residue", "spinoff_kind")


def test_ended_or_live_run_never_contradicts():
    assert decide(_h(), [DONE, RUN_ENDED], False)[0] == "closable"
    assert decide(_h(), [DONE, RUN_LIVE_ITEM], False)[0] == "closable"
    assert decide(_h(), [RUN_ENDED], False) == ("no_evidence",)


def test_claimed_released_when_every_naming_run_ended():
    out = decide(_h("claimed"), [DONE, RUN_ENDED], False)
    assert out[0] == "closable"
    assert decide(_h("claimed"), [RUN_DONE], False)[0] == "closable"


def test_claimed_held_when_a_naming_run_is_live():
    out = decide(_h("claimed"), [DONE, RUN_ENDED, RUN_LIVE_ITEM], False)
    assert out[:2] == ("residue", "claimed")


def test_conflict_won_by_a_done_run():
    out = decide(_h(), [RUN_DONE, PLAN_APPROVED], False)
    assert out[0] == "closable"
    note = out[2]
    assert note["winner"] == "done"
    assert note["done"] == [RUN_DONE]
    assert note["contradicting"] == [PLAN_APPROVED]
    assert note["rule"]


def test_conflict_won_by_contradicting_without_a_done_run():
    out = decide(_h(), [DONE, PLAN_APPROVED], False)
    assert out[:2] == ("residue", "evidence_conflict")
    note = out[3]
    assert note["winner"] == "contradicting"
    assert note["done"] == [DONE]
    assert note["contradicting"] == [PLAN_APPROVED]


def _fe(field: str, status: str, done: bool) -> dict:
    return {
        "class": "plan",
        "field": field,
        "target_path": f"docs/plans/{field}.md",
        "target_status": status,
        "done": done,
    }


def test_governing_plan_outranks_sizing_join():
    own = _fe("governing_plan", "implemented", True)
    join = _fe("plan.sizing_object", "approved", False)
    out = decide(_h(), [own, join], False)
    assert out[0] == "closable"
    assert out[2]["winner"] == "done"
    assert out[2]["rule"] == CONFLICT_RULE_OWN_PLAN


def test_governing_plan_does_not_outrank_deliverable_join():
    own = _fe("governing_plan", "implemented", True)
    sizing_done = _fe("plan.deliverable_id", "implemented", True)
    sizing_open = _fe("plan.sizing_object", "approved", False)
    deliverable_open = _fe("plan.deliverable_id", "approved", False)
    assert decide(_h(), [sizing_done, sizing_open], False)[:2] == ("residue", "evidence_conflict")
    assert decide(_h(), [own, deliverable_open], False)[:2] == ("residue", "evidence_conflict")
    assert decide(_h(), [own, sizing_open, deliverable_open], False)[:2] == (
        "residue",
        "evidence_conflict",
    )


def _re(status: str) -> dict:
    return {
        "class": "roadmap",
        "field": "roadmap_id",
        "target_path": "docs/roadmaps/r/OVERVIEW.md",
        "target_status": status,
        "done": False,
    }


def test_done_plan_backlink_outranks_roadmap_rollup_leg():
    link = _fe("plan.deliverable_id", "implemented", True)
    out = decide(_h(), [link, _re("final-approved")], False)
    assert out[0] == "closable"
    assert out[2]["winner"] == "done"
    assert out[2]["done"] == [link]


def test_done_plan_backlink_loses_to_a_non_roadmap_contradiction():
    link = _fe("plan.deliverable_id", "implemented", True)
    other = _fe("plan.predecessor_handoff", "approved", False)
    goal = dict(_e("goal", "active", False))
    for contra in (other, goal):
        out = decide(_h(), [link, _re("final-approved"), contra], False)
        assert out[:2] == ("residue", "evidence_conflict")


def test_roadmap_rollup_alone_still_contradicts_a_non_plan_done_leg():
    out = decide(_h(), [DONE, _re("final-approved")], False)
    assert out[:2] == ("residue", "evidence_conflict")


def test_run_won_conflict_still_yields_to_a_live_successor():
    out = decide(_h(), [RUN_DONE, PLAN_APPROVED], True)
    assert out[:2] == ("residue", "live_successor")


def test_no_conflict_note_when_evidence_agrees():
    assert decide(_h(), [DONE], False)[2] is None
    assert decide(_h(), [MISSING], False)[3] is None


def test_does_not_mutate_inputs():
    ev = [dict(DONE), dict(OPEN)]
    snapshot = [dict(e) for e in ev]
    decide(_h(), ev, False)
    assert ev == snapshot
