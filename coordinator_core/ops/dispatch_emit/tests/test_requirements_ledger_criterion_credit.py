"""judge credits a met `criterion` verdict item to the register rows the plan maps, and only those."""

from __future__ import annotations

import pytest

from coordinator_core.ops.dispatch_emit import requirements_ledger as rl

pytestmark = pytest.mark.complete

PLAN = """---
title: "plan"
register_claims:
  sizing: "state/sizings/2026-11/x.yaml"
  rows: ["R1"]
---

# plan

## Tasks

```yaml plan-tasks
- id: C1
  title: the work
  register_rows: ["R2"]
  disposition: open
```
"""


def _ledger(*ids):
    return rl.Ledger(
        items=tuple(rl.Item(i, f"text {i}", "pm", "2026-11-16T00:00:00Z", anchor="pm_verbatims") for i in ids),
        all_of_pm_ask=False,
    )


def _verdict(**criterion):
    return {"items": [{"id": "criterion", "status": "met", **criterion}]}


def test_a_met_criterion_credits_the_rows_the_plan_maps():
    out = rl.judge(_ledger("R1", "R2"), _verdict(), plan_text=PLAN)
    assert out["pass"] is True and out["failures"] == []


def test_an_unmapped_unjudged_row_still_fails():
    out = rl.judge(_ledger("R1", "R2", "R3"), _verdict(), plan_text=PLAN)
    assert not out["pass"]
    assert [f["id"] for f in out["failures"]] == ["R3"]
    assert out["failures"][0]["reason"].startswith("unjudged")


def test_a_criterion_that_is_not_met_credits_nothing():
    out = rl.judge(_ledger("R1"), {"items": [{"id": "criterion", "status": "not_met"}]}, plan_text=PLAN)
    assert [f["id"] for f in out["failures"]] == ["R1"]


def test_no_plan_text_credits_nothing():
    out = rl.judge(_ledger("R1"), _verdict())
    assert [f["id"] for f in out["failures"]] == ["R1"]


def test_the_credit_carries_the_criterion_hand_built_refusal():
    from coordinator_core.ops import requirement_register as rr

    verdict = _verdict(**{rr.JUDGE_EVIDENCE_ENTRY_POINT: rr.EVIDENCE_HAND_BUILT})
    out = rl.judge(_ledger("R1"), verdict, plan_text=PLAN)
    assert not out["pass"]
    assert rl.HAND_BUILT_REASON in [f["reason"] for f in out["failures"] if f["id"] == "R1"]


def test_an_own_verdict_row_outranks_the_credit():
    verdict = {"items": [{"id": "criterion", "status": "met"}, {"id": "R1", "status": "drifted"}]}
    out = rl.judge(_ledger("R1"), verdict, plan_text=PLAN)
    assert [f["id"] for f in out["failures"]] == ["R1"]
    assert out["failures"][0]["reason"].startswith("drifted")
