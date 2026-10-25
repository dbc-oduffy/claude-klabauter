"""dispatch.land_run on an ask run: a run whose chunks declare a code path needs a reviewer verdict
before the commit (any arm); a docs-only run lands with an empty wave; the commit names the verdict count."""

from __future__ import annotations

import pytest

from coordinator_core.ops.dispatch_emit.commit_request import ChunkCommit, CommitRequest, render_marker
from coordinator_core.ops.dispatch_emit.ask_contract import RUN_DIR_ROOT
from coordinator_core.ops.dispatch_emit.tests.test_ask_run_product_commit import (
    DLV,
    PLAN,
    RUN,
    WAVE_REVIEW,
    _call,
    _git,
    _REVIEWED,
    _size_l,
    _write,
    repo,  # noqa: F401  (fixture)
)

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

EMPTY_WAVE = {
    "integration_stem": "wave-bookkeeping-run1", "slices": 0, "fixes": 0, "prep_sidecar": None,
    "plan_id": None, "wave_sidecar_paths": [], "prep": None, "delivery": None,
}
ONE_VERDICT = {**EMPTY_WAVE, "wave_reviews": [WAVE_REVIEW]}


def _docs_only(repo) -> None:
    _write(repo, "docs/reference/x.md", "x\n")
    request = CommitRequest(
        chunks=(ChunkCommit(id="C1", title="t", paths=("docs/reference/x.md",)),), deliverable_id=DLV, plan_path=PLAN
    )
    _write(repo, f"{RUN_DIR_ROOT}/{RUN}/commit-request.txt", render_marker(request) + "\n")


def test_a_code_run_with_an_empty_review_wave_is_refused_and_commits_nothing(repo):
    head = _git(repo, "rev-parse", "HEAD")
    out = _call(repo, inline_review=EMPTY_WAVE)
    assert out["committed"] is False and out["refused"] == "review-floor", out
    assert out["code_paths"] == ["a.py"], out
    assert _git(repo, "rev-parse", "HEAD") == head


def test_a_docs_only_run_with_an_empty_review_wave_lands_and_says_zero_verdicts(repo):
    _docs_only(repo)
    out = _call(repo, inline_review=EMPTY_WAVE)
    assert out["committed"] is True, out
    assert "Ledger-Receipt: review: 0 verdicts" in _git(repo, "log", "-1", "--format=%B", out["sha"])


def test_a_code_run_with_one_verdict_lands_and_says_so(repo):
    out = _call(repo, inline_review=ONE_VERDICT)
    assert out["committed"] is True, out
    assert "Ledger-Receipt: review: 1 verdicts" in _git(repo, "log", "-1", "--format=%B", out["sha"])


def test_a_non_xs_code_run_with_a_verdict_lands_unchanged(repo):
    _size_l(repo)
    out = _call(repo, inline_review=_REVIEWED)
    assert out["committed"] is True, out
    assert "Ledger-Receipt: review: 1 verdicts" in _git(repo, "log", "-1", "--format=%B", out["sha"])


def test_a_non_xs_code_run_with_an_empty_wave_is_refused(repo):
    _size_l(repo)
    out = _call(repo, inline_review={**_REVIEWED, "wave_reviews": []})
    assert out["committed"] is False and out["refused"] == "review-floor", out


def _code_request() -> CommitRequest:
    return CommitRequest(chunks=(ChunkCommit(id="C1", title="t", paths=("a.py",)),), deliverable_id=DLV, plan_path=PLAN)


def test_terminal_floor_exempts_a_verify_only_run_only_on_disk_prep(tmp_path):
    from coordinator_core.ops.dispatch_emit import terminal_commit

    review = {**EMPTY_WAVE, "prep": {"product_files": 0}}
    assert terminal_commit._review_floor_refusal(_code_request(), review, True, tmp_path) is None
    refused = terminal_commit._review_floor_refusal(_code_request(), review, False, tmp_path)
    assert refused["refused"] == "review-floor" and refused["code_paths"] == ["a.py"], refused
    nonzero = {**EMPTY_WAVE, "prep": {"product_files": 2}}
    assert terminal_commit._review_floor_refusal(_code_request(), nonzero, True, tmp_path)["refused"] == "review-floor"


def test_terminal_floor_needs_the_verdict_sidecar_to_exist_under_the_root(tmp_path):
    from coordinator_core.ops.dispatch_emit import terminal_commit

    review = {**EMPTY_WAVE, "wave_reviews": [{"verdict": "PASS", "sidecar_path": "rev.md"}]}
    assert terminal_commit._review_floor_refusal(_code_request(), review, False, tmp_path)["refused"] == "review-floor"
    (tmp_path / "rev.md").write_text("# r\n", encoding="utf-8")
    assert terminal_commit._review_floor_refusal(_code_request(), review, False, tmp_path) is None
    bare = {**EMPTY_WAVE, "wave_reviews": [{"verdict": "x"}]}
    assert terminal_commit._review_floor_refusal(_code_request(), bare, False, tmp_path)["refused"] == "review-floor"


def test_a_repo_relative_sidecar_resolves_against_the_repo_not_the_process_cwd(repo, tmp_path, monkeypatch):
    elsewhere = tmp_path / "unrelated-cwd"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    out = _call(repo, inline_review=ONE_VERDICT)
    assert out["committed"] is True, out
    assert "Ledger-Receipt: review: 1 verdicts" in _git(repo, "log", "-1", "--format=%B", out["sha"])


def test_a_missing_sidecar_verdict_through_the_handler_is_refused_review_floor(repo, tmp_path):
    (repo.parent / "wave-review.md").unlink()
    out = _call(repo, inline_review=ONE_VERDICT)
    assert out["committed"] is False and out["refused"] == "review-floor", out
