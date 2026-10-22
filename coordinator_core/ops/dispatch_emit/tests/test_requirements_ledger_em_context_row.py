"""requirements_ledger.judge: an EM-context row is never a deliverable, whatever the grader says."""

from __future__ import annotations

from coordinator_core.ops.dispatch_emit import requirements_ledger as rl
from coordinator_core.ops.sizing_intake_contract import EM_CONTEXT_MARKER


def _ledger():
    return rl.Ledger(
        items=(
            rl.Item(id="R1", text="do the thing", origin="pm", at="t", anchor="relayed"),
            rl.Item(id="R2", text=f"{EM_CONTEXT_MARKER}\nPM: update your TODOs", origin="pm", at="t", anchor="relayed"),
        ),
        all_of_pm_ask=True,
    )


def test_not_met_essential_context_row_does_not_block():
    verdict = {"items": [
        {"id": "R1", "status": "met", "met_by": ["x"]},
        {"id": "R2", "status": "not_met", "essential": True, "remaining": ["met_by: sha"]},
    ]}
    out = rl.judge(_ledger(), verdict)
    assert out["pass"], out
    assert {"id": "R2", "reason": "em-context"} in out["open"]


def test_unjudged_context_row_does_not_block():
    out = rl.judge(_ledger(), {"items": [{"id": "R1", "status": "met", "met_by": ["x"]}]})
    assert out["pass"], out


def test_ordinary_row_still_blocks_when_not_met():
    verdict = {"items": [
        {"id": "R1", "status": "not_met", "essential": True},
        {"id": "R2", "status": "met"},
    ]}
    out = rl.judge(_ledger(), verdict)
    assert not out["pass"]
    assert [f["id"] for f in out["failures"]] == ["R1"]
