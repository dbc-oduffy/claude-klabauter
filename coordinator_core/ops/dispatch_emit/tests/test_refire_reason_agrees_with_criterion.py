"""The refire decision of a judged ask reads the criterion as it stands after reconcile: run under node on the composed tail."""

from __future__ import annotations

import pytest

from coordinator_core.ops.dispatch_emit.tests.test_ask_ledger_commit_pending_e2e import (  # noqa: F401
    _item,
    _judge_result,
    _run,
    _xs_arm,
)

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_DOC = "docs/a.md"


def _judged(items: list[dict], **extra) -> dict:
    return _judge_result(items, falsifier={"verdict": "pass"}, **extra)


def test_stale_not_met_criterion_settles_when_reconcile_flips_the_last_item():
    item = _item("R1", "not_met", remaining=[_DOC])
    (out,) = _run([_judged([item])], judged=True, doc_writes=(_DOC,), doc_paths=(_DOC,))
    assert out["halted_by"] is None, out["halted_by"]
    assert out["ledger"]["items"][0]["status"] == "met"
    assert out["revise_and_refire"] is None
    assert "commit" in out["phases"]


def test_settled_criterion_reads_met_in_the_next_action_params():
    item = _item("R1", "not_met", remaining=[_DOC])
    (out,) = _run([_judged([item])], judged=True, doc_writes=(_DOC,), doc_paths=(_DOC,))
    assert out["revise_and_refire"] is None
    assert out["next_action"]["kind"] != "revise_and_refire"
    assert out["ledger"]["items"][0]["status"] == "met"
    assert out["ledger"]["items"][0].get("remaining") is None


def test_gated_withheld_row_with_not_met_criterion_still_blocks():
    item = _item("R1", "not_met", withheld_by="C9", remaining=["C9 is gated"])
    (out,) = _run([_judged([item])], judged=True, gated=("C9",))
    assert out["halted_by"] is None
    revise = out["revise_and_refire"]
    assert revise is not None and revise["reason"].startswith("criterion not_met")
    assert out["commit"] is None and "commit" not in out["phases"]
    assert out["ledger"]["items"][0]["status"] == "not_met"


def test_a_non_doc_gap_left_after_reconcile_keeps_the_criterion_open():
    item = _item("R1", "not_met", remaining=[_DOC, "the test is red"])
    (out,) = _run([_judged([item])], judged=True, doc_writes=(_DOC,), doc_paths=(_DOC,))
    assert out["commit"] is None and "commit" not in out["phases"]
    assert out["revise_and_refire"] is not None


def test_a_second_item_still_not_met_after_reconcile_keeps_the_criterion_open():
    flipped = _item("R1", "not_met", remaining=[_DOC])
    open_ = _item("R2", "not_met", withheld_by="C9", remaining=["C9 is gated"])
    (out,) = _run([_judged([flipped, open_])], judged=True, gated=("C9",), doc_writes=(_DOC,), doc_paths=(_DOC,))
    revise = out["revise_and_refire"]
    assert revise is not None and revise["reason"].startswith("criterion not_met")


def test_a_reachability_demotion_reads_its_own_reason_and_is_never_settled():
    item = _item("R1", "met")
    judge = _judge_result(
        [item], status="met", observed=["x"], falsifier={"verdict": "pass"},
        reachability={"unreachable": ["x.py:f"], "undecidable": []},
    )
    (out,) = _run([judge], judged=True, in_run_commit=False)
    revise = out["revise_and_refire"]
    assert revise is not None
    assert revise["reason"].startswith("not wired up: x.py:f"), revise["reason"]
    assert not revise["reason"].startswith("criterion not_met")


def test_stale_not_met_over_a_ledger_met_from_the_start_settles_without_a_reconcile_flip():
    judge = _judge_result(
        [_item("R1", "met")], clauses=[{"status": "met"}], falsifier={"verdict": "pass"},
    )
    (out,) = _run([judge], judged=True)
    assert out["halted_by"] is None, out["halted_by"]
    assert out["revise_and_refire"] is None
    assert "commit" in out["phases"]
    assert out["ledger"]["items"][0]["status"] == "met"


def test_an_unmet_clause_keeps_a_met_ledger_criterion_open():
    judge = _judge_result(
        [_item("R1", "met")], clauses=[{"status": "met"}, {"status": "not_met"}], falsifier={"verdict": "pass"},
    )
    (out,) = _run([judge], judged=True)
    assert out["revise_and_refire"] is not None
    assert out["commit"] is None and "commit" not in out["phases"]


def test_a_failing_falsifier_verdict_keeps_a_met_ledger_criterion_open():
    judge = _judge_result([_item("R1", "met")], clauses=[{"status": "met"}], falsifier={"verdict": "fail"})
    (out,) = _run([judge], judged=True)
    assert out["revise_and_refire"] is not None
    assert out["commit"] is None and "commit" not in out["phases"]


def test_a_met_judge_and_met_ledger_do_not_revise():
    judge = _judge_result([_item("R1", "met")], status="met", observed=["x"], falsifier={"verdict": "pass"})
    (out,) = _run([judge], judged=True)
    assert out["revise_and_refire"] is None and out["halted_by"] is None
    assert "commit" in out["phases"]
