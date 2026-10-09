"""`--reverify-delivery` also runs for a delivery PASS whose criterion is not_met/indeterminate,
and refuses only when delivery PASSed and the criterion was met."""

from __future__ import annotations

import pytest

from coordinator_core.ops.dispatch_emit import reverify_delivery as rd
from coordinator_core.ops.review_mint.execute_review import _JUDGE_PREAMBLE
from coordinator_core.ops.tests.test_review_stamp_superseding_record import _git, _record, _repo

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

_PASS = {"verdict": "PASS", "product_files": 1, "claims_unbacked": 0}


def _criterion(status):
    return {"status": status, "observation": "o", "sidecar": None}


def _run(tmp_path, criterion_status):
    repo = _repo(tmp_path)
    record = _record(
        repo, _git(repo, "rev-parse", "HEAD"), delivery=_PASS, criterion=_criterion(criterion_status),
        tests={"status": "pass", "run": 1, "failed": 0, "sidecar": None},
    )
    return repo, record


@pytest.mark.parametrize("status", ["not_met", "indeterminate"])
def test_delivery_pass_with_unsettled_criterion_is_allowed(tmp_path, monkeypatch, status):
    repo, record = _run(tmp_path, status)
    _, claims = rd.prior_unbacked_claims(record)
    assert claims == []

    plan = repo / "docs" / "plans" / "p.md"
    plan.parent.mkdir(parents=True, exist_ok=True)
    plan.write_text("---\nplan_id: pln-x\nstatus: executing\n---\nbody\n", encoding="utf-8")
    monkeypatch.setattr(rd, "_head_sha", lambda root: "b" * 40)
    out = repo / "docs" / "plans" / "p.reverify.workflow.mjs"
    result = rd.emit_reverify(repo_root=repo, plan_path=str(plan), run_record=str(record), out_path=str(out))
    assert result["claims"] == 0
    text = out.read_text(encoding="utf-8")
    assert "python3" in text and "criterion" in text


def test_delivery_pass_with_met_criterion_is_refused(tmp_path):
    _, record = _run(tmp_path, "met")
    with pytest.raises(rd.ReverifyRefused):
        rd.prior_unbacked_claims(record)


def test_delivery_fail_still_returns_its_claims(tmp_path):
    repo = _repo(tmp_path)
    fail = {"verdict": "FAIL", "unbacked": [{"claim": "c", "anchor": "a"}]}
    record = _record(repo, _git(repo, "rev-parse", "HEAD"), delivery=fail)
    assert rd.prior_unbacked_claims(record)[1] == [{"claim": "c", "anchor": "a"}]


def test_judge_preamble_names_the_python_interpreter():
    assert "`python3`" in _JUDGE_PREAMBLE and "`python`" in _JUDGE_PREAMBLE


def test_recorded_row_evidence_reaches_the_verifier_and_the_judge(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    fail = {"verdict": "FAIL", "unbacked": [{"claim": "timing", "anchor": "unmeasured"}]}
    record = _record(repo, _git(repo, "rev-parse", "HEAD"), delivery=fail)
    plan = repo / "docs" / "plans" / "p.md"
    plan.parent.mkdir(parents=True, exist_ok=True)
    plan.write_text("---\nplan_id: pln-x\nstatus: executing\n---\nbody\n", encoding="utf-8")
    (plan.parent / "p.evidence.yaml").write_text(
        "rows:\n  D1:\n  - {recorded_at: '2026-10-09', text: 40.6ms per op}\n", encoding="utf-8"
    )
    monkeypatch.setattr(rd, "_head_sha", lambda root: "b" * 40)
    out = repo / "docs" / "plans" / "p.reverify.workflow.mjs"
    rd.emit_reverify(repo_root=repo, plan_path="docs/plans/p.md", run_record=str(record), out_path=str(out))
    text = out.read_text(encoding="utf-8")
    assert "Row evidence recorded through evidence-append is in docs/plans/p.evidence.yaml" in text
    assert "row_evidence: docs/plans/p.evidence.yaml" in text


def test_no_recorded_evidence_names_no_sidecar(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    fail = {"verdict": "FAIL", "unbacked": [{"claim": "c", "anchor": "a"}]}
    record = _record(repo, _git(repo, "rev-parse", "HEAD"), delivery=fail)
    plan = repo / "docs" / "plans" / "p.md"
    plan.parent.mkdir(parents=True, exist_ok=True)
    plan.write_text("---\nplan_id: pln-x\nstatus: executing\n---\nbody\n", encoding="utf-8")
    monkeypatch.setattr(rd, "_head_sha", lambda root: "b" * 40)
    out = repo / "docs" / "plans" / "p.reverify.workflow.mjs"
    rd.emit_reverify(repo_root=repo, plan_path="docs/plans/p.md", run_record=str(record), out_path=str(out))
    assert "evidence.yaml" not in out.read_text(encoding="utf-8")
