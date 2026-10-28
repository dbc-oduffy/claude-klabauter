"""A code run whose declared paths all equal HEAD with a met ledger closes already_landed naming HEAD
(no review-floor refusal, no refire); a run with a real diff and 0 verdicts still refuses review-floor."""

from __future__ import annotations

import pytest

from coordinator_core.ops.dispatch_emit import terminal_commit
from coordinator_core.ops.dispatch_emit.tests.test_ask_run_product_commit import (
    _REVIEWED,
    _SID,
    _git,
    _put,
    repo,  # noqa: F401  (fixture)
)
from coordinator_core.ops.dispatch_emit.tests.test_terminal_commit_review_floor import EMPTY_WAVE

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


def _call(repo, **extra) -> dict:
    params = {
        "script_path": "run.mjs", "incomplete_chunks": [], "inline_review": _REVIEWED, "session_id": _SID,
        "ledger_verdict": {"items": [{"id": "R1", "status": "met", "wired": True}]}, **extra,
    }
    return terminal_commit._handler(params, repo_root=repo / ".git")


def _land_a_py(repo) -> str:
    _git(repo, "add", "a.py")
    _git(repo, "commit", "-q", "-m", "behaviour already shipped")
    return _git(repo, "rev-parse", "HEAD").strip()


def test_empty_diff_met_ledger_closes_already_landed_naming_head(repo):
    head = _land_a_py(repo)
    out = _call(repo, inline_review=EMPTY_WAVE)
    assert out.get("refused") is None and out.get("error") is None, out
    assert out["already_landed"] is True and out["sha"] == head, out
    assert "next_action" not in out and out["paths"] == ["a.py"], out
    assert _git(repo, "rev-parse", "HEAD").strip() == head


def test_empty_diff_not_met_criterion_grade_still_closes_already_landed(repo):
    head = _land_a_py(repo)
    review = {**EMPTY_WAVE, "criterion": {"status": "not_met", "reason": "baseline equals result"}}
    out = _call(repo, inline_review=review)
    assert out.get("refused") is None and out.get("error") is None, out
    assert out["already_landed"] is True and out["sha"] == head, out
    assert "next_action" not in out, out


def test_empty_diff_with_an_unmet_ledger_is_not_closed(repo):
    _land_a_py(repo)
    out = _call(repo, inline_review=EMPTY_WAVE, ledger_verdict={"items": [{"id": "R1", "status": "not_met"}]})
    assert out["committed"] is False and out.get("already_landed") is None, out


def test_non_empty_diff_with_zero_verdicts_still_refuses_review_floor(repo):
    _land_a_py(repo)
    _put(repo, "a.py", "changed\n")
    head = _git(repo, "rev-parse", "HEAD").strip()
    out = _call(repo, inline_review=EMPTY_WAVE)
    assert out["committed"] is False and out["refused"] == "review-floor", out
    assert _git(repo, "rev-parse", "HEAD").strip() == head
