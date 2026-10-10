"""criterion_evidence.record: registration, sidecar round-trip, refusals, and the rejudge route
pointing the judge at the recorded entry."""

from __future__ import annotations

import hashlib

import pytest

from coordinator_core import ipc
from coordinator_core.authz.classification import OP_CLASSIFICATION, OpClass
from coordinator_core.op_scopes import OP_KEY_SCOPE
from coordinator_core.ops import _registry_map
from coordinator_core.ops.criterion_evidence import _criterion_evidence_record as record
from coordinator_core.ops.dispatch_emit.reverify_delivery import compose_rejudge_script
from coordinator_core.ops.dispatch_emit.tests.test_emit_judge_reads_register import _fragment
from coordinator_core.ops.review_mint.tests.test_execute_review import _STAGE_SCHEMAS
from coordinator_core.ops.plan_tasks_mutate import (
    evidence_sidecar_path,
    has_recorded_evidence,
    read_criterion_evidence,
    read_row_evidence,
)
from coordinator_core.ops.tests.test_plan_tasks_mutate import _make_git_repo, _seed_plan

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]


def _setup(tmp_path):
    repo = _make_git_repo(tmp_path)
    plan = _seed_plan(repo, "e.md", "")
    plan.write_text("---\nplan_id: p1\n---\nbody\n", encoding="utf-8")
    out = tmp_path / "e2e.txt"
    out.write_text("running 4 tests\n4 passed (3.1s)\n", encoding="utf-8")
    return repo, plan, out


def _params(plan, out, **extra):
    return {"plan_path": str(plan), "clause": "e2e checks pass", "command": "npx playwright test",
            "output_path": str(out), **extra}


def test_registration_surfaces():
    assert ipc._REGISTRY.get("criterion_evidence.record") is not None
    assert _registry_map.OP_MODULE_MAP["criterion_evidence.record"] == "coordinator_core.ops.criterion_evidence"
    assert OP_KEY_SCOPE["criterion_evidence.record"] == "common_dir"
    assert OP_CLASSIFICATION["criterion_evidence.record"] is OpClass.MUTATING


def test_record_round_trips_and_leaves_plan_and_rows_alone(tmp_path):
    repo, plan, out = _setup(tmp_path)
    before = plan.read_bytes()
    res = record(_params(plan, out, exit_code=0), repo_root=repo / ".git")
    assert res["recorded"] is True and res["output_sha256"] == hashlib.sha256(out.read_bytes()).hexdigest()
    (entry,) = read_criterion_evidence(plan)
    assert entry["clause"] == "e2e checks pass" and entry["exit_code"] == 0
    assert "4 passed" in entry["output_tail"] and "status" not in entry
    assert plan.read_bytes() == before and read_row_evidence(plan) == {}
    assert has_recorded_evidence(plan)


def test_refusals_write_no_sidecar(tmp_path):
    repo, plan, out = _setup(tmp_path)
    empty = tmp_path / "empty.txt"
    empty.write_text("  \n", encoding="utf-8")
    for bad in (
        _params(plan, out, clause=""),
        _params(plan, out, command=None),
        _params(plan, tmp_path / "missing.txt"),
        _params(plan, empty),
        _params(plan, out, exit_code="0"),
        _params(repo / "docs" / "plans" / "nope.md", out),
    ):
        assert "error" in record(bad, repo_root=repo / ".git")
    assert not evidence_sidecar_path(plan).exists()


def _rejudge(plan, repo):
    return compose_rejudge_script(
        fragment=_fragment(),
        stage_schemas={**_STAGE_SCHEMAS, "judge-result": {"type": "object"}},
        plan_path=str(plan), plan_id="p1", run_base_sha="a" * 40, head_sha="b" * 40, repo_root=repo,
    )


def test_rejudge_points_judge_at_sidecar_only_once_evidence_exists(tmp_path):
    repo, plan, out = _setup(tmp_path)
    before = _rejudge(plan, repo)
    assert "criterion_evidence.record" in before  # the preamble names the door regardless
    assert f"row_evidence: {evidence_sidecar_path(plan).name}" not in before
    record(_params(plan, out), repo_root=repo / ".git")
    assert f"row_evidence: {str(plan.with_name(evidence_sidecar_path(plan).name)).replace(chr(92), '/')}" in _rejudge(plan, repo)
