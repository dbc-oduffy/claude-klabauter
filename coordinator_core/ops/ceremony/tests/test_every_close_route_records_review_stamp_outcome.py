"""Every close route ends its review-stamp leg in ran, a reasoned review skip, or an unminted refusal."""
from __future__ import annotations

import json

import pytest

from coordinator_core.ops.ceremony import close_background as cb
from coordinator_core.ops.dispatch_emit import halted_record_commit as hrc
from coordinator_core.ops.dispatch_emit import halted_run_landed as hrl
from coordinator_core.ops.dispatch_emit import run_close
from coordinator_core.ops.tests.test_review_stamp_superseding import _git, _record, _repo

pytestmark = pytest.mark.spawns_process

RID = "run-1"
SKIP = "review skipped: "
UNMINTED = "review ran, stamp not minted: "
SPINE_PLAN = "docs/plans/p.spine.md"


@pytest.fixture(autouse=True)
def _opted_in(monkeypatch):
    monkeypatch.setattr(cb, "_not_opted_in", lambda ctx: False)
    monkeypatch.setattr(hrl, "_launch", lambda root, run_id: True)


def _assert_outcome(leg) -> None:
    """The invariant: the leg is ran, a skip with a vocabulary reason, or an unminted refusal."""
    if leg.status == "ran":
        return
    if leg.status == "not-applicable":
        assert leg.detail.startswith(SKIP), leg.detail
        reason = leg.detail[len(SKIP):].split(":")[0]
        assert reason in run_close.REVIEW_SKIP_REASONS, leg.detail
        return
    assert leg.status == "failed" and leg.detail.startswith(UNMINTED), (leg.status, leg.detail)


def _ctx(root, plan_rel, sha, inputs=None):
    record = {"inputs": inputs or {}}
    if plan_rel is not None:
        record["plan_rel"] = plan_rel
    return cb.JobContext(root, root / ".git", RID, (), None, sha, record)


# Each kind's close-job shape that reaches the review-stamp leg; an unlisted new kind falls to the
# plan-run shape, which must still end in a classified outcome.
_KIND_JOB = {
    "plan-run": ("docs/plans/example.md", True),
    "mise-run": (None, True),
    "xs-s-run": (SPINE_PLAN, True),
    "run-less-xs-s": ("docs/plans/example.md", False),
}


@pytest.mark.parametrize("kind", run_close.CLOSE_KINDS)
def test_unqueued_review_stamp_reads_a_reasoned_skip(kind):
    block = run_close.build_close_block(RID, True, {}, kind=kind)
    leg = run_close.LegResult(**block["legs"]["review-stamp"])
    _assert_outcome(leg)
    assert leg.status == "not-applicable"
    assert leg.detail == f"{SKIP}not-queued"


@pytest.mark.parametrize("kind", run_close.CLOSE_KINDS)
def test_queued_review_stamp_ends_classified(tmp_path, kind):
    repo, head = _repo(tmp_path)
    plan_rel, has_sha = _KIND_JOB.get(kind, _KIND_JOB["plan-run"])
    leg = cb._review_stamp(_ctx(repo, plan_rel, head if has_sha else None))
    _assert_outcome(leg)
    assert leg.status != "ran"


def test_halted_ledger_check_without_review_record_settles_no_review_outcome(tmp_path):
    repo, head = _repo(tmp_path)
    marker = {"status": "halted", "plan": "docs/plans/example.md", "blocking": []}
    out = hrl._queue_close(repo, RID, head, marker, None)
    assert out["close"]["launched"] is True
    job = json.loads((repo / run_close.close_job_path(RID)).read_text(encoding="utf-8"))
    assert "review-stamp" not in job["legs"]
    leg = run_close.LegResult(**job["settled"]["review-stamp"])
    _assert_outcome(leg)
    assert leg.detail == f"{SKIP}no-review-outcome"


def test_halted_ledger_check_with_review_record_mints_without_an_inline_review_trailer(tmp_path):
    repo, head = _repo(tmp_path)
    assert "Inline-Review" not in _git(repo, "log", "-1", "--format=%B")
    sidecar = _record(repo, head, tests={"status": "pass", "run": 3, "failed": 0, "sidecar": None})
    record_rel = sidecar.relative_to(repo).as_posix()
    # The record carries the delivery verdict the halted route's review would have written.
    sidecar.write_text(sidecar.read_text(encoding="utf-8").replace(
        "---\n\nbody", 'delivery: {"verdict": "PASS", "unbacked": []}\n---\n\nbody', 1), encoding="utf-8")
    written = hrc._persist_run_outcome(
        repo, RID, {"halted": "ledger-check"}, {"run_outcome": {"inline_review": {"integration": {"sidecar": record_rel}}}}
    )
    assert written is not None

    marker = {"status": "halted", "plan": "docs/plans/example.md", "blocking": []}
    hrl._queue_close(repo, RID, head, marker, None)
    job = json.loads((repo / run_close.close_job_path(RID)).read_text(encoding="utf-8"))
    assert job["inputs"]["review_record"] == record_rel
    assert job["legs"]["review-stamp"]["status"] == "queued"

    ctx = cb.JobContext(repo, repo / ".git", RID, (), None, job["terminal_sha"], job)
    leg = cb._review_stamp(ctx)
    _assert_outcome(leg)
    assert leg.status == "ran", leg.detail
