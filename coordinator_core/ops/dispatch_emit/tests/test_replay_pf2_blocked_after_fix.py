"""Replay of wf_3195e5db-282 #2: a reviewer BLOCKED after fixing its own findings was routed to
re-author through the readiness verdict. The verdict on the fixed plan must clear it; an unfixed
finding must still route to re-author."""

from coordinator_core.ops.plan_readiness_verdict import readiness_verdict

PASS = {"status": "PASS", "kind": None, "mechanical": False, "repair": None}
GATE = {"plan": "p.md", "verdict": "PREPPED", "withheld_rows": [], "classes": {"C0": PASS}, "message": ""}


def _readiness(verdict: str) -> dict:
    return readiness_verdict(GATE, None, [{"reviewer": "opus", "verdict": verdict}])


def test_consumer_routes_a_raw_blocked_review_to_re_author():
    out = _readiness("BLOCKED")
    assert out["verdict"] == "judgment"
    assert out["blocking_findings"] >= 1


def test_blocked_after_fix_returns_ok_on_the_fixed_plan_and_readiness_clears():
    from coordinator_core.ops.dispatch_emit.review_self_fix import final_verdict

    findings = [{"id": "F1", "status": "applied"}, {"id": "F2", "status": "applied"}]
    seen = []
    result = final_verdict("BLOCKED", findings, reverify=lambda f: seen.append(f["id"]) or True)
    assert seen == ["F1", "F2"]
    assert not result.reauthor
    assert result.verdict.verdict == "ok"
    assert result.verdict.reverified is True
    assert result.verdict.fixes_applied == ("F1", "F2")
    assert _readiness(result.verdict.verdict)["verdict"] == "ready"


def test_an_unfixed_finding_still_routes_to_re_author():
    from coordinator_core.ops.dispatch_emit.review_self_fix import final_verdict

    findings = [{"id": "F1", "status": "applied", "verified": True}, {"id": "F2", "status": "open"}]
    result = final_verdict("BLOCKED", findings, reverify=lambda f: True)
    assert result.reauthor and result.unfixed == ("F2",)
    assert result.verdict.verdict == "blocked"
    assert _readiness(result.verdict.verdict)["blocking_findings"] >= 1


def test_a_failed_or_missing_reverify_keeps_the_finding_unfixed():
    from coordinator_core.ops.dispatch_emit.review_self_fix import final_verdict

    findings = [{"id": "F1", "status": "applied"}]
    assert final_verdict("BLOCKED", findings, reverify=lambda f: False).unfixed == ("F1",)
    assert final_verdict("BLOCKED", findings).unfixed == ("F1",)


LIVE1_REVIEW_1 = {
    "verdict": "WARN",
    "findings": [{"id": f"F{n}", "status": "applied", "verified": True} for n in range(1, 7)],
}


def test_live1_warn_with_six_applied_fixes_needs_no_second_author_or_review():
    from coordinator_core.ops.dispatch_emit.review_self_fix import final_verdict

    result = final_verdict(LIVE1_REVIEW_1["verdict"], LIVE1_REVIEW_1["findings"])
    assert not result.reauthor
    assert result.verdict.verdict == "ok"
    assert result.verdict.fixes_applied == tuple(f"F{n}" for n in range(1, 7))
    assert _readiness(result.verdict.verdict)["verdict"] == "ready"


def test_blocked_without_findings_and_clear_verdicts():
    from coordinator_core.ops.dispatch_emit.review_self_fix import final_verdict

    assert final_verdict("BLOCKED", []).reauthor
    passed = final_verdict("ok", [])
    assert not passed.reauthor and passed.verdict.verdict == "ok"
