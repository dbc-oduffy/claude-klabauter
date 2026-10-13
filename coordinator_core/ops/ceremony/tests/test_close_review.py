"""Tests for close.review: the review leg's ran/failed rules, the cap, and op registration."""

from __future__ import annotations

import json
import subprocess
from datetime import datetime, timedelta

import pytest

from coordinator_core.authz.classification import OP_CLASSIFICATION, OpClass
from coordinator_core.authz.completion_evidence import OP_COMPLETION_EVIDENCE, EvidenceClass
from coordinator_core.ops.ceremony import close_background as cb
from coordinator_core.ops.ceremony import close_review as cr
from coordinator_core.ops.dispatch_emit import run_close
from coordinator_core.ops.dispatch_emit.run_close import LegResult

pytestmark = pytest.mark.spawns_process

RUN = "run-1"
DID = "dlv-chain-1"
BATON = "state/handoffs/h1.md"


def _ctx(root, batons=(BATON,)):
    return cb.JobContext(root, root / ".git", RUN, tuple(batons), None)


def _record(root):
    return json.loads((root / run_close.close_job_path(RUN)).read_text(encoding="utf-8"))["legs"]


def _g(repo, *args):
    return subprocess.run(
        ["git", *args], cwd=str(repo), capture_output=True, text=True, check=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    ).stdout.strip()


def _chain_commit(repo, name, deliverable=DID):
    (repo / "src").mkdir(exist_ok=True)
    (repo / "src" / name).write_text(name, encoding="utf-8")
    _g(repo, "add", "--", f"src/{name}")
    _g(repo, "commit", "-q", "-m", f"{name}\n\nSession-Id: s1\nDeliverable-Id: {deliverable}")
    return _g(repo, "rev-parse", "HEAD")


@pytest.fixture
def chain_repo(tmp_path):
    _g(tmp_path, "init", "-q")
    _g(tmp_path, "config", "user.email", "t@example.com")
    _g(tmp_path, "config", "user.name", "t")
    (tmp_path / "state" / "handoffs").mkdir(parents=True)
    (tmp_path / BATON).write_text(f'---\ndeliverable_id: "{DID}"\n---\n', encoding="utf-8")
    return tmp_path


@pytest.fixture
def nothing_credited(monkeypatch):
    monkeypatch.setattr("coordinator_core.review_trail.reviewed_set.read_reviewed_set", lambda r: set())
    monkeypatch.setattr("coordinator_core.review_trail.receipt_credit.receipt_credited_shas", lambda r, c: set())


def _queue(root, *, base, terminal, receipt="docs/plans/p.md", batons=(BATON,), minutes=10):
    run_close.write_close_job(
        root, RUN, ("review",), datetime.now().astimezone() + timedelta(minutes=minutes),
        claimed_batons=batons, review_receipt=receipt, run_base_sha=base, terminal_sha=terminal,
    )


def _pay(root, batons=(BATON,)):
    cr.run_review(_ctx(root, batons))
    return _record(root)["review"]


def test_ran_with_zero_unreceipted_commits(chain_repo, monkeypatch):
    sha = _chain_commit(chain_repo, "a.py")
    monkeypatch.setattr("coordinator_core.review_trail.reviewed_set.read_reviewed_set", lambda r: {sha})
    _queue(chain_repo, base=sha, terminal=sha)
    leg = _pay(chain_repo)
    assert leg["status"] == "ran"
    assert "receipt docs/plans/p.md" in leg["detail"] and "0 unreceipted" in leg["detail"]


def test_failed_with_the_rendered_unreceipted_detail(chain_repo, nothing_credited):
    _chain_commit(chain_repo, "a.py")
    sha = _chain_commit(chain_repo, "b.py")
    _queue(chain_repo, base=sha, terminal=sha)
    leg = _pay(chain_repo)
    assert leg["status"] == "failed"
    assert sha[:12] in leg["detail"] and "2 unreceipted ancestor commits" in leg["detail"]


def test_truncated_at_the_cap_is_failed_never_ran(chain_repo, nothing_credited, monkeypatch):
    monkeypatch.setattr(cr, "REVIEW_RANGE_CAP", 2)
    for n in range(3):
        sha = _chain_commit(chain_repo, f"{n}.py")
    _queue(chain_repo, base=sha, terminal=sha)
    leg = _pay(chain_repo)
    assert leg["status"] == "failed" and "range truncated" in leg["detail"]


def test_no_receipt_is_failed(tmp_path):
    run_close.write_close_job(tmp_path, RUN, ("review",), datetime.now().astimezone() + timedelta(minutes=10))
    leg = _pay(tmp_path)
    assert leg["status"] == "failed" and "review_receipt" in leg["detail"]


def test_null_run_base_is_failed_naming_it(tmp_path):
    _queue(tmp_path, base=None, terminal="a" * 40)
    leg = _pay(tmp_path)
    assert leg["status"] == "failed" and "run_base_sha" in leg["detail"]


def test_bad_endpoint_is_failed_never_ran(chain_repo):
    _chain_commit(chain_repo, "a.py")
    sha = _g(chain_repo, "rev-parse", "HEAD")
    _queue(chain_repo, base=sha, terminal=sha)
    path = chain_repo / run_close.close_job_path(RUN)
    record = json.loads(path.read_text(encoding="utf-8"))
    record["run_base_sha"] = "HEAD"
    path.write_text(json.dumps(record), encoding="utf-8")
    leg = _pay(chain_repo)
    assert leg["status"] == "failed" and "not a full 40-hex" in leg["detail"]


def test_stops_at_a_receipted_tip_and_ignores_other_deliverables(chain_repo):
    old = _chain_commit(chain_repo, "old.py")
    _chain_commit(chain_repo, "other.py", deliverable="dlv-other")
    sha = _g(chain_repo, "rev-parse", "HEAD")
    plan = chain_repo / "docs" / "plans"
    plan.mkdir(parents=True)
    (plan / "prior.md").write_text(
        f'---\ndeliverable_id: "{DID}"\nreview_stamp:\n  terminal_commit_sha: {old}\n---\n', encoding="utf-8"
    )
    _queue(chain_repo, base=sha, terminal=sha)
    leg = _pay(chain_repo)
    assert leg["status"] == "ran" and f"since {old[:12]}" in leg["detail"]


def test_tip_from_a_close_job_record_with_a_ran_review(chain_repo, nothing_credited):
    old = _chain_commit(chain_repo, "old.py")
    sha = _chain_commit(chain_repo, "new.py")
    run_close.write_close_job(
        chain_repo, "prior-run", (), datetime.now().astimezone() + timedelta(minutes=10),
        claimed_batons=(BATON,), terminal_sha=old, settled={"review": LegResult("ran")},
    )
    _queue(chain_repo, base=sha, terminal=sha)
    leg = _pay(chain_repo)
    assert leg["status"] == "failed" and sha[:12] in leg["detail"] and old[:12] not in leg["detail"]


def test_a_raising_review_is_recorded_failed(tmp_path, monkeypatch):
    _queue(tmp_path, base="a" * 40, terminal="b" * 40)
    monkeypatch.setattr(cr, "_review", lambda ctx: (_ for _ in ()).throw(RuntimeError("boom")))
    leg = _pay(tmp_path)
    assert leg["status"] == "failed" and "RuntimeError" in leg["detail"]


def test_past_deadline_runs_nothing(tmp_path, monkeypatch):
    _queue(tmp_path, base="a" * 40, terminal="b" * 40, minutes=-5)
    monkeypatch.setattr(cr, "_review", lambda ctx: pytest.fail("review ran past its deadline"))
    assert cr.run_review(_ctx(tmp_path)) == {}


def test_handler_reports_a_missing_record_and_a_missing_run_id(tmp_path):
    (tmp_path / ".git").mkdir()
    assert "error" in cr._handler({"run_id": RUN}, tmp_path / ".git")
    assert "run_id" in cr._handler({}, tmp_path / ".git")["error"]


def test_op_is_registered_mutating_fire_and_forget_common_dir():
    from coordinator_core.op_scopes import _OP_KEY_SCOPE as OP_SCOPES

    assert OP_CLASSIFICATION["close.review"] is OpClass.MUTATING
    assert OP_COMPLETION_EVIDENCE["close.review"] is EvidenceClass.FIRE_AND_FORGET
    assert OP_SCOPES["close.review"] == "common_dir"
