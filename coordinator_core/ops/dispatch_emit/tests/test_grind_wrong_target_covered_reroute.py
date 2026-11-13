"""A routing verify that fails a wrong-target row retires it as covered, reroutes it once, or refuses a self-target.

Live evidence (example-stats-repo lessons run 20261113T035948Z, four rows handed back ``rejected-after-retry``): the
verifier's reasons for the two covered rows (fuzzy-query-..., capture-runs-preferences) cited the covering
``path:line`` in prose only ("Routing target is wrong ... docs/guides/name-matching.md:50"), so no structured
``covered_by`` reached compose, the flip never fired and the row retried then failed. The flip itself ignores
triage's target (it keys on a pre-fix verify node and ``covered_by`` alone); no ``verify_retried`` guard blocked
it. The prose-only shape is fixed by the verify brief; the structured shape is pinned here. The third row
(claude-md-bar) targeted the lesson row itself; the fourth (uefa-lineups-endpoint) named its right home.
"""
import pytest

from coordinator_core.ops.dispatch_emit import grind_compose as gc

from .test_grind_already_covered_no_retry import _LESSONS, _Budget
from .test_grind_compose import _GOLDEN_PATH

_FAIL_COVERED = {
    "outcome": "fail",
    "reason": "Routing target is wrong; the maintained guide already states it",
    "covered_by": "docs/guides/name-matching.md:50",
}
_RIGHT_HOME = {
    "outcome": "fail",
    "reason": "wiki-append is right but the target is wrong",
    "right_home": "docs/guides/journalism-intelligence.md",
}
_ROUTING = {**_LESSONS, "triage": {**_LESSONS["triage"], "edges": {**_LESSONS["triage"]["edges"], "wiki-append": "verify-routing-fix"}}}


def _run(profile, verify, *, declared=("docs/audits/dated-record.md",), fix=()):
    """One row ``r0`` through ``run_admission``; ``declared`` is what triage names as the target."""
    calls = []
    verifies, fixes = list(verify), list(fix)

    def agent(kind, unit):
        calls.append(kind)
        if kind == "triage":
            return [{"row": "r0", "verdict": "wiki-append", "tshirt_size": "S", "tradeoff": "",
                     "declared_files": list(declared)}]
        if kind == "verify":
            return verifies.pop(0)
        if kind == "fix":
            return fixes.pop(0)
        if kind == "refute-close":
            return {"confirmed": [{"row": "r0", "new_path": "archive/r0.md"}]}
        if kind == "commit":
            return {"committed": True, "sha": "0" * 40}
        return {}

    result = gc.run_admission(
        [("b0", ["r0"])], _ROUTING, "triage", reserve=gc.batch_reserve(1),
        max_agent_calls=None, budget=_Budget(), agent=agent, profile=profile,
    )
    return calls, result


def _types(result):
    return [h["type"] for h in result["handed_back"] if h["row"] == "r0"]


@pytest.mark.parametrize("profile", ["lessons", "profile"])
def test_a_covering_artifact_elsewhere_retires_a_wrong_target_row_as_covered(profile):
    calls, result = _run(profile, [_FAIL_COVERED])
    assert calls.count("verify") == 1 and "fix" not in calls and "refute-close" in calls
    assert _types(result) == []


@pytest.mark.parametrize("profile", ["lessons", "profile"])
def test_a_fail_without_a_cover_or_a_right_home_still_retries_then_hands_back(profile):
    calls, result = _run(profile, [{"outcome": "fail", "reason": "no"}, {"outcome": "fail", "reason": "no"}])
    assert calls.count("verify") == 2 and "fix" not in calls
    assert _types(result) == ["rejected-after-retry"]


@pytest.mark.parametrize("profile", ["lessons", "profile"])
def test_right_home_reroutes_the_row_to_fix_once_without_a_verify_retry(profile):
    calls, result = _run(
        profile, [_RIGHT_HOME, {"outcome": "pass"}],
        fix=[{"outcome": "done", "touched_files": ["docs/guides/journalism-intelligence.md"]}],
    )
    assert calls.count("fix") == 1 and calls.count("verify") == 2
    assert "rejected-after-retry" not in _types(result)


def test_a_right_home_outside_the_repo_is_handed_back_not_fixed():
    reply = {**_RIGHT_HOME, "right_home": "../outside/guide.md"}
    calls, result = _run("lessons", [reply])
    assert "fix" not in calls
    assert _types(result) == ["needs-judgment"]
    assert "../outside/guide.md" in next(h["reason"] for h in result["handed_back"] if h["row"] == "r0")


def test_a_right_home_from_the_post_fix_verify_is_a_plain_fail_that_retries_the_fix():
    done = {"outcome": "done", "touched_files": ["docs/guides/journalism-intelligence.md"]}
    calls, result = _run("lessons", [_RIGHT_HOME, _RIGHT_HOME, {"outcome": "pass"}], fix=[done, done])
    assert calls.count("fix") == 2 and calls.count("verify") == 3


def test_a_right_home_naming_the_lesson_row_falls_through_to_the_retry_in_the_lessons_profile():
    reply = {**_RIGHT_HOME, "right_home": "./r0"}
    calls, result = _run("lessons", [reply, reply])
    assert calls.count("verify") == 2 and "fix" not in calls
    assert _types(result) == ["rejected-after-retry"]


def test_a_right_home_naming_the_row_file_reroutes_outside_the_lessons_profile():
    reply = {**_RIGHT_HOME, "right_home": "r0"}
    calls, _ = _run("profile", [reply, {"outcome": "pass"}], fix=[{"outcome": "done", "touched_files": ["r0"]}])
    assert calls.count("fix") == 1


def test_a_covered_row_never_reroutes_even_when_the_verdict_also_names_a_right_home():
    calls, result = _run("lessons", [{**_FAIL_COVERED, "right_home": "docs/guides/other.md"}])
    assert "fix" not in calls and "refute-close" in calls and _types(result) == []


def test_the_lesson_row_as_triage_target_is_refused_before_fix_in_the_lessons_profile():
    calls, result = _run("lessons", [], declared=("r0",))
    assert "verify" not in calls and "fix" not in calls
    assert _types(result) == ["needs-judgment"]
    assert "triage targeted the lesson row itself: r0" in next(h["reason"] for h in result["handed_back"] if h["row"] == "r0")


@pytest.mark.parametrize("profile", ["profile", "lessons-strip", "bug"])
def test_a_non_lessons_profile_still_sends_a_self_target_row_to_fix(profile):
    calls, result = _run(
        profile, [{"outcome": "pass"}, {"outcome": "pass"}], declared=("r0",),
        fix=[{"outcome": "done", "touched_files": ["r0"]}],
    )
    assert calls.count("fix") == 1 and "needs-judgment" not in _types(result)


def test_a_lessons_row_naming_another_file_is_not_refused():
    calls, _ = _run("lessons", [{"outcome": "pass"}, {"outcome": "pass"}], declared=("docs/guides/x.md",),
                    fix=[{"outcome": "done", "touched_files": ["docs/guides/x.md"]}])
    assert calls.count("fix") == 1


def test_the_emitted_script_carries_the_same_three_behaviours():
    golden = _GOLDEN_PATH.read_text(encoding="utf-8")
    verify = golden[golden.index("async function _verifyStage"):golden.index("async function _commitStage")]
    triage = golden[golden.index("async function _triageBatch"):golden.index("async function _closeBatch")]
    assert "const SELF_TARGET_REFUSED_PROFILES = [\"lessons\"];" in golden
    assert "rerouted: false" in golden and "row.rerouted = false;" in golden
    assert "!result.covered_by && !row.rerouted && VERIFY_PRE_FIX.includes(row.node)" in verify
    assert "row.declaredFiles = [_home];" in verify
    assert "followEdge(row.node, 'pass', row), 'rerouted to right_home'" in verify
    assert "_home === _normRel(row.path)" in verify
    assert "triage targeted the lesson row itself" in triage
    assert "_reachesFix(route.value, new Set())" in triage
