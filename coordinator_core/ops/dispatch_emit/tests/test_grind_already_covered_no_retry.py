"""An already-covered row closes at a retire node that never re-enters fix: no verify retry, no re-fix."""
import pytest

from coordinator_core.ops.dispatch_emit import grind_compose as gc

from .test_grind_verify_covered_flip import _ROUTING as _FLIP_ROUTING

_LESSONS = {
    "triage": {
        "kind": "triage",
        "edges": {"bug": "verify-routing-fix", "dedupe": "verify-routing-fix", "discard": "verify-routing-discard"},
        "on_fail": "needs-judgment",
    },
    "verify-routing-fix": {"kind": "verify", "edges": {"pass": "fix"}, "on_fail": "verify-failed"},
    "verify-routing-discard": {"kind": "verify", "edges": {"pass": "refute-close"}, "on_fail": "verify-failed"},
    "refute-close": {"kind": "refute-close", "edges": {"confirmed": "commit"}, "on_fail": "needs-judgment"},
    "fix": {"kind": "fix", "edges": {"done": "verify"}, "on_fail": "baton"},
    "verify": {"kind": "verify", "edges": {"pass": "commit"}, "on_fail": "verify-failed"},
    "commit": {"kind": "commit", "edges": {}, "on_fail": "commit-failed"},
}

#: A bug-grind shape: no dedupe or discard edge, so no fix-free retire route exists.
_NO_RETIRE = {
    "triage": {"kind": "triage", "edges": {"bug": "fix"}, "on_fail": None},
    "fix": {"kind": "fix", "edges": {"done": "verify"}, "on_fail": None},
    "verify": {"kind": "verify", "edges": {"pass": "commit"}, "on_fail": None},
    "commit": {"kind": "commit", "edges": {}, "on_fail": None},
}

#: The dedupe route exists but re-enters fix, and there is no discard edge.
_DEDUPE_REENTERS_FIX = {
    "triage": {"kind": "triage", "edges": {"bug": "verify-routing-fix", "dedupe": "verify-routing-fix"}, "on_fail": None},
    "verify-routing-fix": {"kind": "verify", "edges": {"pass": "fix"}, "on_fail": None},
    "fix": {"kind": "fix", "edges": {"done": "verify"}, "on_fail": None},
    "verify": {"kind": "verify", "edges": {"pass": "commit"}, "on_fail": None},
    "commit": {"kind": "commit", "edges": {}, "on_fail": None},
}

_UNDONE = {"outcome": "undone"}


class _Budget:
    total = None
    _s = 0

    def spent(self):
        return self._s

    def remaining(self):
        return None

    def spend(self, n):
        self._s += n


def _run(routing, *, fix=(), verify=(), verdict="bug"):
    """Drive one row through ``run_admission``; ``fix`` and ``verify`` are the scripted replies in call order."""
    calls = []
    fixes, verifies = list(fix), list(verify)

    def agent(kind, unit):
        calls.append(kind)
        if kind == "triage":
            return [{"row": "r0", "verdict": verdict, "tshirt_size": "S", "tradeoff": ""}]
        if kind == "fix":
            return fixes.pop(0)
        if kind == "verify":
            return verifies.pop(0)
        if kind == "refute-close":
            return {"confirmed": [{"row": "r0", "new_path": "archive/r0.md"}]}
        if kind == "commit":
            return {"committed": True, "sha": "0" * 40}
        if kind == "ledger-sweep":
            return {"outcome": "recorded", "swept": True}
        return {}

    sink = []
    result = gc.run_admission(
        [("b0", ["r0"])], routing, "triage", reserve=gc.batch_reserve(1),
        max_agent_calls=None, budget=_Budget(), agent=agent, undo_paths_sink=sink,
    )
    return calls, result, sink


def _types(result):
    return [h["type"] for h in result["handed_back"] if h["row"] == "r0"]


def test_pre_fix_covered_row_closes_through_discard_without_a_verify_retry():
    calls, result, _ = _run(_LESSONS, verify=[
        {"outcome": "fail", "reason": "covered", "covered_by": "docs/wiki/x.md:12"},
        {"outcome": "pass"},
    ])
    assert calls.count("verify") == 2 and "fix" not in calls and "refute-close" in calls
    assert _types(result) == []


def test_post_fix_covered_verify_undoes_the_fix_and_closes_without_a_second_fix():
    calls, result, sink = _run(
        _LESSONS,
        fix=[{"outcome": "done", "touched_files": ["lib/a.py"]}],
        verify=[
            {"outcome": "pass"},
            {"outcome": "fail", "reason": "covered", "covered_by": "docs/wiki/x.md:3"},
            {"undo": _UNDONE},
            {"outcome": "pass"},
        ],
    )
    assert calls.count("fix") == 1 and calls.count("verify") == 4
    assert sink == [{"row": "r0", "paths": ["lib/a.py"]}]
    assert _types(result) == []


def test_post_fix_covered_verify_with_a_failed_undo_is_stage_dead():
    calls, result, _ = _run(
        _LESSONS,
        fix=[{"outcome": "done", "touched_files": ["lib/a.py"]}],
        verify=[{"outcome": "pass"}, {"outcome": "fail", "covered_by": "docs/wiki/x.md:3"}, {"undo": {"outcome": "undo-failed"}}],
    )
    assert calls.count("fix") == 1 and _types(result) == ["stage-dead"]


def test_fix_reporting_covered_by_closes_without_any_post_fix_verify():
    calls, result, sink = _run(
        _LESSONS,
        fix=[{"outcome": "done", "touched_files": [], "covered_by": "docs/wiki/x.md:1"}],
        verify=[{"outcome": "pass"}, {"outcome": "pass"}],
    )
    assert calls.count("fix") == 1 and calls.count("verify") == 2
    assert sink == [] and _types(result) == []


def test_fix_covered_with_edits_undoes_them_before_closing():
    calls, result, sink = _run(
        _LESSONS,
        fix=[{"outcome": "done", "touched_files": ["lib/a.py"], "covered_by": "docs/wiki/x.md:1"}],
        verify=[{"outcome": "pass"}, {"undo": _UNDONE}, {"outcome": "pass"}],
    )
    assert calls.count("fix") == 1 and sink == [{"row": "r0", "paths": ["lib/a.py"]}]
    assert _types(result) == []


def test_no_fix_free_route_hands_a_fix_reported_cover_back_needs_judgment():
    calls, result, _ = _run(
        _NO_RETIRE, fix=[{"outcome": "done", "touched_files": [], "covered_by": "docs/wiki/x.md:1"}],
    )
    assert calls.count("fix") == 1 and "verify" not in calls
    [entry] = [h for h in result["handed_back"] if h["row"] == "r0"]
    assert entry["type"] == "needs-judgment" and "docs/wiki/x.md:1" in entry["reason"]


def test_post_fix_cover_in_a_profile_with_no_fix_free_route_hands_back_instead_of_refixing():
    calls, result, _ = _run(
        _NO_RETIRE,
        fix=[{"outcome": "done", "touched_files": ["lib/a.py"]}],
        verify=[{"outcome": "fail", "covered_by": "docs/wiki/x.md:3"}, {"undo": _UNDONE}],
    )
    assert calls.count("fix") == 1 and calls.count("verify") == 2
    [entry] = [h for h in result["handed_back"] if h["row"] == "r0"]
    assert entry["type"] == "needs-judgment" and "docs/wiki/x.md:3" in entry["reason"]


def test_post_fix_covered_by_naming_the_rows_own_touched_file_takes_the_normal_retry():
    calls, result, _ = _run(
        _LESSONS,
        fix=[{"outcome": "done", "touched_files": ["docs/wiki/x.md"]}] * 2,
        verify=[
            {"outcome": "pass"},
            {"outcome": "fail", "covered_by": "./docs/wiki/x.md:4"},
            {"outcome": "fail", "covered_by": "docs/wiki/x.md:4", "undo": _UNDONE},
        ],
    )
    assert calls.count("fix") == 2 and _types(result) == ["rejected-after-retry"]


def test_covered_by_in_a_second_post_fix_verify_is_not_re_undone():
    calls, result, _ = _run(
        _LESSONS,
        fix=[{"outcome": "done", "touched_files": ["lib/a.py"]}] * 2,
        verify=[{"outcome": "pass"}, {"outcome": "fail"}, {"outcome": "fail", "covered_by": "docs/wiki/x.md:3", "undo": _UNDONE}],
    )
    assert calls.count("verify") == 3 and _types(result) == ["rejected-after-retry"]


def test_pre_fix_cover_whose_only_route_re_enters_fix_is_handed_back_not_retried():
    calls, result, _ = _run(_DEDUPE_REENTERS_FIX, verify=[{"outcome": "fail", "covered_by": "docs/wiki/x.md:2"}])
    assert calls.count("verify") == 1 and "fix" not in calls
    assert _types(result) == ["needs-judgment"]


def test_covered_retire_route_picks_the_fix_free_route_and_never_the_current_node():
    assert gc.covered_retire_route(_LESSONS, "triage", "verify-routing-fix") == "verify-routing-discard"
    assert gc.covered_retire_route(_LESSONS, "triage", "verify-routing-discard") is None
    assert gc.covered_retire_route(_NO_RETIRE, "triage", "verify") is None
    assert gc.covered_retire_route(_DEDUPE_REENTERS_FIX, "triage", "verify-routing-fix") is None
    assert gc.covered_retire_route(_FLIP_ROUTING, "triage", "verify-routing") == gc.dedupe_flip_route(_FLIP_ROUTING, "triage")


def test_covered_retire_route_refuses_a_route_that_reaches_fix_through_on_fail():
    routing = {
        **_LESSONS,
        "verify-routing-discard": {"kind": "verify", "edges": {"pass": "refute-close"}, "on_fail": "fix"},
    }
    assert gc.covered_retire_route(routing, "triage", "verify-routing-fix") is None


def test_emitted_script_twins_the_retire_route_and_its_stage_branches():
    from .test_grind_compose import _compose

    script = _compose()
    for name in ("function routeCoveredRetire(", "function _reachesFix(", "async function _closeCovered("):
        assert name in script
    verify_stage = script[script.index("async function _verifyStage"):script.index("async function _commitStage")]
    assert "routeCoveredRetire(row.node)" in verify_stage and "_closeCovered(row, rowId" in verify_stage
    fix_stage = script[script.index("async function _fixStage"):script.index("async function _verifyStage")]
    assert "_closeCovered(row, rowId, result.covered_by)" in fix_stage
