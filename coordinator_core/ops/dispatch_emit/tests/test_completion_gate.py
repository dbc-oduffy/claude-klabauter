"""completion_blockers: each blocker fires alone, and a backed outcome yields none."""

from coordinator_core.ops.dispatch_emit import completion_gate as cg
from coordinator_core.ops.dispatch_emit.requirements_ledger import ORIGIN_PM_VERBATIMS


def _review(**crit):
    criterion = {
        "status": "met",
        "authored_by": cg.JUDGE_AGENT,
        "observation": "wired end to end",
        "falsifier": {"output": "3 passed"},
        "withheld_clauses": [],
    }
    criterion.update(crit)
    return {"criterion": criterion, "tests": {"status": "pass", "run": 3, "failed": 0}}


def test_backed_run_has_no_blockers():
    assert cg.completion_blockers(inline_review=_review(), halt_marker=None) == []


def test_constants():
    assert cg.JUDGE_AGENT == "coordinator:exit-criterion-judge"
    assert cg.PM_VERBATIM_ORIGIN == ORIGIN_PM_VERBATIMS


def test_halt_standing_for_halted_and_resumed_not_landed():
    for status in ("halted", "resumed"):
        out = cg.completion_blockers(inline_review=_review(), halt_marker={"status": status, "halted": "gate red"})
        assert out == ["halt standing: gate red"]
    assert cg.completion_blockers(inline_review=_review(), halt_marker={"status": "landed"}) == []


def test_withheld_clause():
    out = cg.completion_blockers(inline_review=_review(withheld_clauses=["c1", "c2"]), halt_marker=None)
    assert out == ["withheld clause: c1, c2"]


def test_criterion_not_met():
    assert cg.completion_blockers(inline_review=_review(status="not_met"), halt_marker=None) == ["criterion not met"]


def test_not_judge_authored():
    out = cg.completion_blockers(inline_review=_review(authored_by="coordinator:executor"), halt_marker=None)
    assert out == ["verdict not judge-authored"]


def test_no_falsifier_output():
    for fals in ({"output": "  "}, {}, None):
        out = cg.completion_blockers(inline_review=_review(falsifier=fals), halt_marker=None)
        assert out == ["no captured falsifier output"]


def test_not_backed_blank_observation_or_tests():
    assert cg.completion_blockers(inline_review=_review(observation=" "), halt_marker=None) == ["not backed"]
    review = _review()
    review["tests"] = {"status": "pass", "run": 0}
    assert cg.completion_blockers(inline_review=review, halt_marker=None) == ["not backed"]
    review["tests"] = {"status": "fail", "run": 3}
    assert cg.completion_blockers(inline_review=review, halt_marker=None) == ["not backed"]


def test_missing_review_reports_every_review_blocker():
    out = cg.completion_blockers(inline_review=None, halt_marker=None)
    assert out == [
        "criterion not met",
        "verdict not judge-authored",
        "no captured falsifier output",
        "not backed",
    ]
