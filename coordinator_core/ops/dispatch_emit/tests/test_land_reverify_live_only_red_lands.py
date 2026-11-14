"""The terminal-commit landing judges the review re-verify's reds against the fresh isolated-export retest
record: a red that exists only in the live tree, or is already red at HEAD, lands as foreign; a red the
run's own blobs cause, or one the export never measured, stays listed."""

from __future__ import annotations

import json

import pytest

from coordinator_core.ops.dispatch_emit import landing_retest, landing_targeted_tests as ltt, terminal_commit
from coordinator_core.ops.dispatch_emit.tests._review_fixture import reviewed
from coordinator_core.ops.dispatch_emit.tests.test_terminal_commit_landing_integrity import (  # noqa: F401
    _fire,
    _git,
    repo,
)

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

TEST_FILE = "tests/test_mod.py"
NODE = f"{TEST_FILE}::test_x"
RUN = "run-1"


def _stage(repo, baseline: dict | None) -> None:
    (repo / "tests").mkdir()
    (repo / "mod.py").write_text("x = 1\n", encoding="utf-8")
    (repo / TEST_FILE).write_text("import mod\n\n\ndef test_x():\n    assert mod.x\n", encoding="utf-8")
    if baseline is None:
        return
    path = landing_retest.fresh_verdict_path(repo, RUN)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "run_id": RUN, "head": _git(["rev-parse", "HEAD"], repo), "tests": [], "status": "pass",
        "covered": landing_retest.landing_fingerprint(repo, ["mod.py", TEST_FILE]),
        "baseline": baseline,
    }), encoding="utf-8")


def _review(**record) -> dict:
    reverify = {"status_before": "fail", "status_after": "fail", "failing": [NODE], "reverted": [], **record}
    return {**reviewed(), "tests": {"status": "pass"}, ltt.REVERIFY_KEY: reverify}


def _land(repo, baseline: dict | None, **record) -> dict:
    _stage(repo, baseline)
    return _fire(repo, "mod.py", inline_review=_review(**record), run_id=RUN)


def _gate(repo, **record) -> dict:
    """The gate's own extras (the handler reply surfaces only some of them), on the tree ``_land`` staged."""
    return terminal_commit._landing_integrity_gate(
        repo, ["mod.py"], [], None, {"inline_review": _review(**record)}, RUN,
    )


def _reverify_reasons(out: dict) -> list[str]:
    return [a["reason"] for a in out.get("advisory", []) if a["reason"] == ltt.REVIEW_REVERIFY_FAILED]


def test_a_live_only_red_on_a_non_run_path_lands_as_foreign(repo):
    out = _land(repo, {ltt.OWED_CLEARED_KEY: [NODE]})

    assert out["committed"] is True, out
    assert "refused" not in out and _reverify_reasons(out) == []
    assert out["reverify_check"] == "fail-foreign-only"
    assert _gate(repo)["reverify_foreign"] == [NODE]


def test_a_red_that_is_also_red_at_pure_head_lands_as_foreign(repo):
    out = _land(repo, {ltt.HEAD_RED_KEY: [NODE]})

    assert out["committed"] is True, out
    assert _reverify_reasons(out) == []
    assert out["reverify_check"] == "fail-foreign-only"


def test_a_red_the_runs_own_blobs_cause_stays_listed(repo):
    out = _land(repo, {ltt.REVERIFY_EXPORT_RED_KEY: [NODE]})

    assert _reverify_reasons(out) == [ltt.REVIEW_REVERIFY_FAILED]
    assert out["reverify_check"] == "fail"
    assert "reverify_foreign" not in out


def test_a_red_with_no_export_measurement_keeps_the_retest_remedy(repo):
    out = _land(repo, {ltt.HEAD_RED_KEY: [], ltt.OWED_CLEARED_KEY: []})

    assert _reverify_reasons(out) == [ltt.REVIEW_REVERIFY_FAILED]
    assert out["reverify_check"] == "fail"
    assert "re-run:" in out["advisory"][0]["detail"]


def test_no_retest_record_at_all_keeps_the_existing_judgement(repo):
    out = _land(repo, None)

    assert _reverify_reasons(out) == [ltt.REVIEW_REVERIFY_FAILED]
    assert out["reverify_check"] == "fail"


def test_a_mixed_record_halts_on_the_owned_id_only(repo):
    other = f"{TEST_FILE}::test_y"
    out = _land(
        repo, {ltt.OWED_CLEARED_KEY: [NODE], ltt.REVERIFY_EXPORT_RED_KEY: [other]}, failing=[NODE, other],
    )

    assert _reverify_reasons(out) == [ltt.REVIEW_REVERIFY_FAILED]
    detail = out["advisory"][0]["detail"]
    assert other in detail and NODE not in detail
    assert out["reverify_check"] == "fail"


def test_a_foreign_export_id_leaves_the_unverified_list(repo):
    other = f"{TEST_FILE}::test_y"
    _land(repo, {ltt.OWED_CLEARED_KEY: [NODE]}, failing=[NODE, other], foreign_unverified=[NODE, other])

    assert _gate(repo, failing=[NODE, other], foreign_unverified=[NODE, other])["reverify_unverified"] == [other]
