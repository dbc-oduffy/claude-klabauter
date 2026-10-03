"""`--reverify-delivery` also re-runs the tests stage when it was fail/error, and mint reads the
superseding `tests` in place of the frozen one."""

from __future__ import annotations

import pytest

from coordinator_core.ops import review_stamp as m
from coordinator_core.ops.dispatch_emit import reverify_delivery as rd
from coordinator_core.ops.tests.test_review_stamp_superseding_record import _git, _plan, _record, _repo

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

_PASS = {"verdict": "PASS", "product_files": 1, "claims_unbacked": 0}
_ERR = {"status": "error", "run": 0, "failed": 0, "sidecar": None}


def test_tests_only_staleness_allows_reverify_and_emits_tests_stage(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    record = _record(repo, _git(repo, "rev-parse", "HEAD"), delivery=_PASS, tests=_ERR)
    assert rd.prior_unbacked_claims(record)[1] == []
    plan = repo / "docs" / "plans" / "p.md"
    plan.parent.mkdir(parents=True, exist_ok=True)
    plan.write_text("---\nplan_id: pln-x\nstatus: executing\n---\nbody\n", encoding="utf-8")
    monkeypatch.setattr(rd, "_head_sha", lambda root: "b" * 40)
    out = repo / "docs" / "plans" / "p.reverify.workflow.mjs"
    rd.emit_reverify(repo_root=repo, plan_path=str(plan), run_record=str(record), out_path=str(out))
    text = out.read_text(encoding="utf-8")
    assert "Tests re-run" in text and "python3" in text and "tests_run" in text


def test_old_shape_record_emits_no_tests_stage(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    fail = {"verdict": "FAIL", "unbacked": [{"claim": "c", "anchor": "a"}]}
    record = _record(repo, _git(repo, "rev-parse", "HEAD"), delivery=fail)
    plan = repo / "docs" / "plans" / "p.md"
    plan.parent.mkdir(parents=True, exist_ok=True)
    plan.write_text("---\nplan_id: pln-x\nstatus: executing\n---\nbody\n", encoding="utf-8")
    monkeypatch.setattr(rd, "_head_sha", lambda root: "b" * 40)
    out = repo / "p.mjs"
    rd.emit_reverify(repo_root=repo, plan_path=str(plan), run_record=str(record), out_path=str(out))
    assert "Tests re-run" not in out.read_text(encoding="utf-8")
    assert rd.latest_tests_supersession(repo, "state/superseding-reviews/2026-10/rec.md") is None


def test_tests_error_then_reverified_pass_mints(tmp_path):
    repo = _repo(tmp_path)
    head = _git(repo, "rev-parse", "HEAD")
    record = _record(repo, head, delivery=_PASS, tests=_ERR)
    with pytest.raises(m.MintRefusal, match="error"):
        m.mint(_plan(repo), repo, build_test_path=None, superseding_record=record)
    rd.record_delivery_verdict(
        repo_root=repo,
        supersedes=record.relative_to(repo).as_posix(),
        plan_id=None,
        head_sha=head,
        verdict="PASS",
        unbacked=[],
        tests={"status": "pass", "run": 3, "failed": 0, "sidecar": None},
    )
    stamp = m.mint(_plan(repo), repo, build_test_path=None, superseding_record=record)
    assert stamp["terminal_commit_sha"] == head
