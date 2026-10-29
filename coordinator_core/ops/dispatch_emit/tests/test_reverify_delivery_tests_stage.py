"""`--reverify-delivery` also re-runs the tests stage when it was fail/error, and mint reads the
superseding `tests` in place of the frozen one."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from coordinator_core.ops import review_stamp as m
from coordinator_core.ops.dispatch_emit import reverify_delivery as rd
from coordinator_core.ops.dispatch_emit.test_interpreter import PORTABLE_CLAUSE, interpreter_clause
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
    assert interpreter_clause(None) in text and "tests_run" in text
    assert ("Verify" in text) != ("Tests re-run" in text), "one tests leg: merged into the judge or standalone"


def test_pinned_interpreter_clause_replaces_python3_sentence(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    pin = Path(sys.executable)
    (repo / "coordinator.local.md").write_text(f"---\ntargeted_test_python: {pin}\n---\n", encoding="utf-8")
    record = _record(repo, _git(repo, "rev-parse", "HEAD"), delivery=_PASS, tests=_ERR)
    plan = repo / "docs" / "plans" / "p.md"
    plan.parent.mkdir(parents=True, exist_ok=True)
    plan.write_text("---\nplan_id: pln-x\nstatus: executing\n---\nbody\n", encoding="utf-8")
    monkeypatch.setattr(rd, "_head_sha", lambda root: "b" * 40)
    out = repo / "docs" / "plans" / "p.reverify.workflow.mjs"
    rd.emit_reverify(repo_root=repo, plan_path=str(plan), run_record=str(record), out_path=str(out))
    text = out.read_text(encoding="utf-8")
    assert json.dumps(str(pin))[1:-1] in text and "pinned interpreter" in text
    assert PORTABLE_CLAUSE not in text


def test_old_shape_record_emits_no_tests_stage(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    fail = {"verdict": "FAIL", "unbacked": [{"claim": "c", "anchor": "a"}]}
    record = _record(repo, _git(repo, "rev-parse", "HEAD"), delivery=fail, tests={"status": "pass", "run": 1, "failed": 0, "sidecar": None})
    plan = repo / "docs" / "plans" / "p.md"
    plan.parent.mkdir(parents=True, exist_ok=True)
    plan.write_text("---\nplan_id: pln-x\nstatus: executing\n---\nbody\n", encoding="utf-8")
    monkeypatch.setattr(rd, "_head_sha", lambda root: "b" * 40)
    out = repo / "p.mjs"
    rd.emit_reverify(repo_root=repo, plan_path=str(plan), run_record=str(record), out_path=str(out))
    assert "Tests re-run" not in out.read_text(encoding="utf-8")
    assert rd.latest_tests_supersession(repo, "state/superseding-reviews/2026-10/rec.md") is None


@pytest.mark.parametrize("status", ["pass", "pass-with-skips"])
def test_tests_error_then_reverified_pass_mints(tmp_path, status):
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
        tests={"status": status, "run": 3, "failed": 0, "sidecar": None},
    )
    stamp = m.mint(_plan(repo), repo, build_test_path=None, superseding_record=record)
    assert stamp["terminal_commit_sha"] == head


def test_not_run_tests_and_criterion_are_legs_to_run_and_all_pass_still_refuses(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    head = _git(repo, "rev-parse", "HEAD")
    not_run = {"status": "not_run", "run": 0, "failed": 0, "sidecar": None}
    record = _record(repo, head, delivery=_PASS, tests=not_run)
    assert rd.prior_unbacked_claims(record)[1] == []
    assert rd._tests_stale(rd._frontmatter(record))
    plan = repo / "docs" / "plans" / "p.md"
    plan.parent.mkdir(parents=True, exist_ok=True)
    plan.write_text("---\nplan_id: pln-x\nstatus: executing\n---\nbody\n", encoding="utf-8")
    monkeypatch.setattr(rd, "_head_sha", lambda root: "b" * 40)
    out = repo / "p.mjs"
    rd.emit_reverify(repo_root=repo, plan_path=str(plan), run_record=str(record), out_path=str(out))
    assert "tests_run" in out.read_text(encoding="utf-8")
    assert rd._criterion_unsettled({"criterion": {"status": "not_run"}})
    ok = {"tests": {"status": "pass"}, "criterion": {"status": "met"}}
    assert not rd._unsettled(ok)


def test_a_pass_with_skips_records_pass_and_keeps_the_skip(tmp_path):
    repo = _repo(tmp_path)
    head = _git(repo, "rev-parse", "HEAD")
    record = _record(repo, head, delivery=_PASS, tests=_ERR)
    rel = rd.record_delivery_verdict(
        repo_root=repo, supersedes=record.relative_to(repo).as_posix(), plan_id=None, head_sha=head,
        verdict="PASS", unbacked=[], tests={"status": "pass-with-skips", "run": 3, "failed": 0, "sidecar": None},
    )
    tests = rd._frontmatter(repo / rel)["tests"]
    assert tests["status"] == "pass" and tests["skipped"] is True


def test_the_emitted_tests_field_names_both_baseline_keys():
    import inspect

    source = inspect.getsource(rd)
    assert "baseline_method: _tests.baseline_method ?? null" in source
    assert "baseline: _tests.baseline ?? null" in source


def _baseline_tests(unverified):
    return {
        "status": "fail", "run": 5, "failed": 2, "sidecar": None, "baseline_method": "export",
        "baseline": {"pre_existing": ["t::a", "t::b"], "caused": [], "unverified": unverified},
    }


def test_a_superseding_fail_with_an_all_pre_existing_baseline_mints(tmp_path):
    repo = _repo(tmp_path)
    head = _git(repo, "rev-parse", "HEAD")
    record = _record(repo, head, delivery=_PASS, tests={"status": "fail", "run": 5, "failed": 2, "sidecar": None})
    with pytest.raises(m.MintRefusal):
        m.mint(_plan(repo), repo, build_test_path=None, superseding_record=record)
    rd.record_delivery_verdict(
        repo_root=repo, supersedes=record.relative_to(repo).as_posix(), plan_id=None, head_sha=head,
        verdict="PASS", unbacked=[], tests=_baseline_tests([]),
    )
    stamp = m.mint(_plan(repo), repo, build_test_path=None, superseding_record=record)
    assert stamp["terminal_commit_sha"] == head


_ENV_TEXT = "git archive HEAD exited 128 (also fails at base)"


def _env_fault_tests(failures):
    return {
        "status": "fail", "failed": 2, "baseline_method": "export", "failing": ["t::a", "t::b"],
        "failures": failures,
        "baseline": {"pre_existing": ["t::a", "t::b"], "caused": [], "unverified": []},
    }


def test_an_export_environment_fault_is_never_set_aside_as_pre_existing():
    tests = _env_fault_tests({"t::a": _ENV_TEXT, "t::b": "AssertionError: x"})
    assert rd.charged_status(tests) == "fail"
    tests = _env_fault_tests([{"id": "t::a", "message": _ENV_TEXT}, {"id": "t::b", "message": "boom"}])
    assert rd.charged_status(tests) == "fail"
    tests = _env_fault_tests({})
    tests["baseline"]["pre_existing"] = [{"id": "t::a", "note": _ENV_TEXT}, "t::b"]
    assert rd.charged_status(tests) == "fail"


def test_a_no_dot_git_export_fault_is_never_set_aside_as_pre_existing():
    tests = _env_fault_tests({"t::a": "fatal: not a git repository (or any parent)", "t::b": "boom"})
    assert rd.charged_status(tests) == "fail"


def test_a_genuine_base_red_is_still_set_aside():
    tests = _env_fault_tests({"t::a": "AssertionError: red at base", "t::b": "boom"})
    assert rd.charged_status(tests) == "pass"


def test_a_superseding_fail_with_unverified_failures_still_refuses(tmp_path):
    repo = _repo(tmp_path)
    head = _git(repo, "rev-parse", "HEAD")
    record = _record(repo, head, delivery=_PASS, tests={"status": "fail", "run": 5, "failed": 2, "sidecar": None})
    rd.record_delivery_verdict(
        repo_root=repo, supersedes=record.relative_to(repo).as_posix(), plan_id=None, head_sha=head,
        verdict="PASS", unbacked=[], tests=_baseline_tests(["t::c"]),
    )
    with pytest.raises(m.MintRefusal):
        m.mint(_plan(repo), repo, build_test_path=None, superseding_record=record)
