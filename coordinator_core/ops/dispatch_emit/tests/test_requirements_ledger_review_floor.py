"""judge's review floor: a code-editing run with 0 reviewer verdicts passes and carries a note."""

from __future__ import annotations

from coordinator_core.ops.dispatch_emit import requirements_ledger as rl


def _ledger():
    items = (
        rl.Item(id="R1", text="do it", origin=rl.ORIGIN_ASK, at="2026-10-25T00:00:00Z"),
        rl.Item(id="R2", text="and that", origin=rl.ORIGIN_ASK, at="2026-10-25T00:00:00Z"),
    )
    return rl.Ledger(items=items, all_of_pm_ask=False)


def _verdict(status="met"):
    return {"items": [{"id": "R1", "status": status, "met_by": ["x"]}, {"id": "R2", "status": "met", "met_by": ["x"]}]}


def test_code_run_with_zero_verdicts_passes_and_carries_the_note():
    out = rl.judge(_ledger(), _verdict(), code_paths=["a.py"], review_verdicts=0)
    assert out["pass"] is True and out["failures"] == []
    assert "review: 0 verdicts" in out["review_floor_note"]
    assert out["receipt"] == "review: 0 verdicts"


def test_code_run_with_one_verdict_passes_and_carries_the_receipt():
    out = rl.judge(_ledger(), _verdict(), code_paths=["a.py"], review_verdicts=1)
    assert out["pass"] is True
    assert out["receipt"] == "review: 1 verdicts"
    assert "review_floor_note" not in out


def test_docs_only_run_with_zero_verdicts_passes_and_says_so():
    out = rl.judge(_ledger(), _verdict(), code_paths=[], review_verdicts=0)
    assert out["pass"] is True
    assert out["receipt"] == "review: 0 verdicts"
    assert "review_floor_note" not in out


def test_default_call_is_unchanged_and_has_no_receipt():
    out = rl.judge(_ledger(), _verdict())
    assert out["pass"] is True
    assert "receipt" not in out


def test_post_commit_item_passes_the_floor_with_the_note():
    verdict = {"items": [
        {"id": "R1", "status": "post_commit", "post_commit_acts": ["commit"]},
        {"id": "R2", "status": "met", "met_by": ["x"]},
    ]}
    out = rl.judge(_ledger(), verdict, code_paths=["a.py"], review_verdicts=0)
    assert out["failures"] == [] and out["review_floor_note"]
