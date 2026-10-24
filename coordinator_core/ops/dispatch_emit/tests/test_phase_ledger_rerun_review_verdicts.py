"""resume_phases carries a landed review verdict when review is re-run."""
from coordinator_core.ops.dispatch_emit import phase_ledger as pl


def test_landed_review_rerun_carries_verdict():
    landed = {"plan": {"a": 1}, "review": {"r": 2}}
    out = pl.resume_phases(landed, "review_blocked")
    assert "review" in out["rerun"] and "review" not in out["reuse"]
    assert out["verdicts"]["review"] == {"r": 2}
    assert out["verdicts"]["plan"] == {"a": 1}


def test_unlanded_review_has_no_verdict():
    out = pl.resume_phases({"plan": {"a": 1}}, "review_blocked")
    assert "review" not in out["verdicts"]


def test_reused_review_keeps_verdict_and_reuse():
    landed = {"plan": {"a": 1}, "review": {"r": 2}}
    out = pl.resume_phases(landed, None)
    assert "review" in out["reuse"]
    assert out["verdicts"]["review"] == {"r": 2}
    assert "review" not in out["rerun"]
