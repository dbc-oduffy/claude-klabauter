"""dispatch.land_run re-pays a landed run's failed close legs from a ``close_job`` digest or a landed
run's script, never re-lands product, and names what was actually missing when it refuses."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from coordinator_core.benchmarks import close_without_script_full_path as bench
from coordinator_core.ops.dispatch_emit import land_run
from coordinator_core.ops.dispatch_emit.tests.test_close_without_script import (  # noqa: F401 -- fixture
    PLAN,
    env,
)

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_BASE = "a" * 40


def _digest(env, *, kind="close_job", run_base=None, name="close-job-digest") -> str:
    result = {
        "next_action": {"kind": kind, "op": None, "params": {"run_id": bench.RUN_ID, "close": {"legs": {}}}},
    }
    if run_base:
        result["run_base_sha"] = run_base
    path = env.repo.parent / f"{env.repo.name}-{name}.json"
    path.write_text(
        json.dumps({"summary": "s", "agentCount": 1, "logs": [], "result": result}), encoding="utf-8"
    )
    return str(path)


def _land(env, **params) -> dict:
    return land_run._handler(
        {"script_path": bench.SCRIPT, "session_id": bench.SESSION_ID, **params}, repo_root=env.repo / ".git"
    )


def test_close_job_digest_repays_the_failed_legs_and_lands_no_product(env):
    env.new_record()
    env.set_record(run_base_sha=_BASE)
    head = env.git("rev-parse", "HEAD").strip()

    out = _land(env, task_output_path=_digest(env))

    assert out.get("refused") is None, out
    assert out["review_stamp"] == "minted" and out["plan_status"] == "implemented"
    assert out["verdict_source"] == "close-job"
    assert env.record()["settled"]["completion-entry"]["status"] == "ran"
    assert env.plan()["status"] == "implemented"
    assert env.git("rev-parse", "HEAD") != head
    changed = env.git("diff", "--name-only", head, "HEAD").split()
    assert changed and all(p == PLAN or p.startswith(("archive/", "state/", "docs/")) for p in changed)


def test_landed_script_with_no_run_outcome_repays_the_close_legs(env):
    env.new_record()
    env.set_record(run_base_sha=_BASE)

    out = _land(env)

    assert out["review_stamp"] == "minted" and out["plan_status"] == "implemented"


def test_review_leg_without_run_base_is_not_applicable_never_silent(env):
    env.new_record()
    env.set_record(run_base_sha=None)

    out = _land(env, task_output_path=_digest(env))

    assert out["plan_status"] == "implemented"
    record = env.record()
    review = record["settled"]["review"] if "review" in record.get("settled", {}) else record["legs"]["review"]
    assert review["status"] == "not-applicable" and "run_base_sha" in review["detail"]
    assert env.launches == []


def test_review_leg_takes_run_base_from_the_digest(env):
    env.new_record()
    env.set_record(run_base_sha=None)

    out = _land(env, task_output_path=_digest(env, run_base=_BASE))

    assert out["plan_status"] == "implemented"
    record = env.record()
    assert record["run_base_sha"] == _BASE and record["legs"]["review"]["status"] == "queued"
    assert len(env.launches) == 1


def test_digest_with_no_outcome_and_no_close_job_names_what_was_missing(env):
    path = _digest(env, kind="wait")

    out = _land(env, task_output_path=path)

    assert out["refused"] == "missing-run-outcome"
    assert "no params.task_output_path was given" not in out["error"]
    assert Path(path).name in out["error"]


_GATE_REASON = "register row R1 (surface internal): no observed entry point (observed_ref is empty)"


def _demoted_criterion() -> dict:
    return {
        "status": "not_met", "reason": _GATE_REASON, "observation": _GATE_REASON,
        "completion_summary": "Derive streams with bounded RAM.",
        "register_rows": [{"claim": "this-plan", "id": "R1", "status": "met", "wired": True}],
    }


def test_digest_run_id_without_workflow_suffix_matches_the_record_key(env):
    env.new_record()
    env.set_record(run_base_sha=_BASE)
    path = _digest(env)
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    data["result"]["next_action"]["params"]["run_id"] = bench.RUN_ID.removesuffix(".workflow")
    Path(path).write_text(json.dumps(data), encoding="utf-8")

    out = _land(env, task_output_path=path)

    assert out.get("refused") is None, out
    assert out["plan_status"] == "implemented"


def test_gate_demoted_criterion_is_rejudged_with_the_ledger_not_read_back(env, monkeypatch):
    from coordinator_core.ops.dispatch_emit import terminal_commit as tc

    seen: list = []
    monkeypatch.setattr(
        tc, "_register_gate", lambda root, plan, review, ledger=(), **kw: seen.append((dict(review["criterion"]), list(ledger)))
    )
    env.new_record(criterion=_demoted_criterion())
    env.set_record(run_base_sha=_BASE, inputs={"ledger_judged": ["R1"]})

    out = _land(env)

    assert seen and seen[0][0]["status"] == "met" and seen[0][1] == ["R1"]
    assert out["criterion_status"] == "met" and out["plan_status"] == "implemented"
    assert env.record()["criterion"]["status"] == "met"
    assert env.record()["settled"]["completion-entry"]["status"] == "ran"


def test_a_judge_not_met_without_a_gate_reason_stays_not_met(env, monkeypatch):
    from coordinator_core.ops.dispatch_emit import terminal_commit as tc

    monkeypatch.setattr(tc, "_register_gate", lambda *a, **k: None)
    crit = {**_demoted_criterion(), "reason": "tests fail", "observation": "tests fail"}
    env.new_record(criterion=crit)

    out = _land(env)

    assert out["criterion_status"] == "not_met"


def test_completion_entry_remedy_names_the_re_close_route_never_a_hand_write(env):
    crit = {**_demoted_criterion(), "completion_summary": None, "observation": None, "status": "met"}
    crit.pop("reason")
    env.new_record(criterion=crit)

    _land(env)

    out = _land(env)

    leg = out["close"]["legs"]["completion-entry"]
    assert leg["status"] == "failed"
    assert "dispatch.land_run" in leg["detail"] and "--rejudge" in leg["detail"]
    assert "by hand" not in leg["detail"] and "coordinator-complete-entry" not in leg["detail"]
