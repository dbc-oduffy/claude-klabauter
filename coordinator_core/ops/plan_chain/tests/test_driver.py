"""driver.run: stage order, halt placement, one final digest per path, default-runner prompt."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from coordinator_core.ops.plan_chain import driver
from coordinator_core.ops.plan_chain.contract import ChainManifest, Halt, WorkflowResult


@pytest.fixture
def world(tmp_path, monkeypatch):
    (tmp_path / "plan.md").write_text("x", encoding="utf-8")
    manifest = ChainManifest(
        sizing_object="state/sizings/s.yaml",
        baton="state/handoffs/b.md",
        deliverable_id="dlv-x",
        interaction_mode="autonomous",
        repo_root=str(tmp_path),
        trail_dir="trail",
        wave_args={},
        script_source="plan-blitz.mjs",
    )
    calls: list[str] = []
    state = {"gate": None, "checks": None, "emit": None}

    def bind(m, *, child_session_id, chain_id=None):
        calls.append("bind")
        p = tmp_path / "trail" / "plan.mjs"
        p.parent.mkdir(exist_ok=True)
        p.write_text("//", encoding="utf-8")
        return p

    def gates(m, plan, *, repo_root):
        calls.append("gates")
        return state["gate"]

    def checks(m, plan, *, repo_root):
        calls.append("checks")
        return state["checks"]

    def emit(plan, **kw):
        calls.append("emit")
        if state["emit"] is not None:
            return state["emit"]
        p = tmp_path / "trail" / "exec.mjs"
        p.write_text("//", encoding="utf-8")
        return p, "sha"

    monkeypatch.setattr(driver.plan_stage, "bind_plan_script", bind)
    monkeypatch.setattr(driver.phase1_gates, "run", gates)
    monkeypatch.setattr(driver.phase1_checks, "run", checks)
    monkeypatch.setattr(driver.emit_leg, "run", emit)
    return manifest, calls, state, tmp_path


PLAN_DIGEST = {"kind": "plan", "outcome": "ready", "next_action": {"op": "plan_chain.run", "params": {"plan_path": "plan.md"}}}
EXEC_DIGEST = {
    "outcome": "complete",
    "review": {"status": "integrated", "slices": 1, "fixes_applied": 0},
    "criterion": {"status": "met", "observation": "ok", "sidecar": None},
    "next_action": {"op": "dispatch.terminal_commit", "params": {"paths": ["a"]}},
}


def make_runner(calls, plan=PLAN_DIGEST, execute=EXEC_DIGEST):
    def runner(script, *, session_id):
        calls.append("run:" + Path(script).stem)
        d = plan if Path(script).stem == "plan" else execute
        return WorkflowResult(digest=d, raw_result="", child_session_id=session_id)

    return runner


def make_commit(calls, reply=None):
    seen = []

    def commit(params):
        calls.append("commit")
        seen.append(params)
        return reply or {"result": {"committed": True, "sha": "abc123", "receipts": ["r.md"]}}

    commit.seen = seen
    return commit


def finals(root):
    return list((root / "trail").glob("*.final-digest.json"))


def test_completed_chain_orders_stages_and_commits_from_driver(world):
    manifest, calls, _s, root = world
    commit = make_commit(calls)
    digest = driver.run(manifest, runner=make_runner(calls), invoke_terminal_commit=commit)
    assert calls == ["bind", "run:plan", "gates", "checks", "emit", "run:exec", "commit"]
    assert commit.seen[0]["paths"] == ["a"]
    assert commit.seen[0]["script_path"].endswith("exec.mjs")
    assert digest["chain"]["halted_at"] is None
    assert digest["chain"]["commit"]["sha"] == "abc123"
    assert len(finals(root)) == 1


def test_plan_pulled_halts_at_ready_gate(world):
    manifest, calls, _s, root = world
    plan = {"kind": "plan", "outcome": "pulled", "decision_required": "why"}
    digest = driver.run(manifest, runner=make_runner(calls, plan=plan), invoke_terminal_commit=make_commit(calls))
    assert calls == ["bind", "run:plan"]
    assert digest["chain"]["halted_at"] == "ready-gate"
    assert digest["chain"]["stages_run"] == ["plan", "ready-gate"]
    assert len(finals(root)) == 1


def test_no_plan_digest_halts_at_plan(world):
    manifest, calls, _s, root = world
    runner = lambda script, *, session_id: WorkflowResult(None, "", session_id)  # noqa: E731
    digest = driver.run(manifest, runner=runner, invoke_terminal_commit=make_commit(calls))
    assert digest["chain"]["halted_at"] == "plan"
    assert len(finals(root)) == 1


def test_phase1_gate_halt_stops_before_emit(world):
    manifest, calls, state, root = world
    state["gate"] = Halt("execute", "peer claim")
    digest = driver.run(manifest, runner=make_runner(calls), invoke_terminal_commit=make_commit(calls))
    assert calls == ["bind", "run:plan", "gates"]
    assert digest["chain"]["halted_at"] == "execute"


def test_phase1_check_halt_stops_before_emit(world):
    manifest, calls, state, root = world
    state["checks"] = Halt("execute", "spine")
    driver.run(manifest, runner=make_runner(calls), invoke_terminal_commit=make_commit(calls))
    assert calls == ["bind", "run:plan", "gates", "checks"]


def test_emit_halt_stops_before_fire(world):
    manifest, calls, state, root = world
    state["emit"] = Halt("execute", "dirty")
    driver.run(manifest, runner=make_runner(calls), invoke_terminal_commit=make_commit(calls))
    assert calls[-1] == "emit"


def test_executor_block_halts_at_execute_without_commit(world):
    manifest, calls, _s, root = world
    ex = {"outcome": "complete", "deviations": [{"chunk": "C1", "kind": "blocked", "anchor": "a"}]}
    digest = driver.run(manifest, runner=make_runner(calls, execute=ex), invoke_terminal_commit=make_commit(calls))
    assert "commit" not in calls
    assert digest["chain"]["halted_at"] == "execute"
    assert len(finals(root)) == 1


def test_review_fail_halts_at_review(world):
    manifest, calls, _s, root = world
    ex = {"outcome": "complete", "review": {"status": "integrated", "delivery": {"verdict": "FAIL"}}}
    digest = driver.run(manifest, runner=make_runner(calls, execute=ex), invoke_terminal_commit=make_commit(calls))
    assert "commit" not in calls
    assert digest["chain"]["halted_at"] == "review"
    assert "review" in digest["chain"]["stages_run"]


def test_commit_refusal_halts_at_terminal_commit(world):
    manifest, calls, _s, root = world
    commit = make_commit(calls, reply={"result": {"committed": False, "error": "undeclared deletion"}})
    digest = driver.run(manifest, runner=make_runner(calls), invoke_terminal_commit=commit)
    assert digest["chain"]["halted_at"] == "terminal-commit"
    assert digest["chain"]["commit"] is None
    assert len(finals(root)) == 1


def test_unexpected_exception_becomes_halt_with_text(world):
    manifest, calls, _s, root = world

    def boom(params):
        raise RuntimeError("kaboom")

    digest = driver.run(manifest, runner=make_runner(calls), invoke_terminal_commit=boom)
    assert digest["chain"]["halted_at"] == "terminal-commit"
    assert "kaboom" in digest["chain"]["halt_reason"]
    assert digest["outcome"] != "complete"
    assert len(finals(root)) == 1


def test_cap_error_is_fire_cap_reached_and_deletes_receipt(world, monkeypatch):
    from coordinator_core.ops.workflow_fire import fire

    manifest, calls, _s, root = world
    deleted = []
    monkeypatch.setattr(driver, "_delete_receipt", lambda script: deleted.append(script))

    def runner(script, *, session_id):
        raise fire.ConcurrencyCapExceededError("cap")

    digest = driver.run(manifest, runner=runner, invoke_terminal_commit=make_commit(calls))
    assert digest["chain"]["halted_at"] == "plan"
    assert "cap" in digest["chain"]["halt_reason"]
    assert len(deleted) == 1
    assert len(finals(root)) == 1


def test_spawn_failure_deletes_receipt(world, monkeypatch):
    from coordinator_core.ops.workflow_fire import fire

    manifest, calls, _s, _root = world
    deleted = []
    monkeypatch.setattr(driver, "_delete_receipt", lambda script: deleted.append(script))

    def runner(script, *, session_id):
        raise fire.ChildSpawnFailedError("no claude")

    driver.run(manifest, runner=runner, invoke_terminal_commit=make_commit(calls))
    assert len(deleted) == 1


def test_default_runner_prompt_never_names_the_commit():
    prompt = driver._workflow_only_prompt(Path("x/chain.execute.mjs"))
    assert "scriptPath x" in prompt.replace("\\", "/")
    assert "coordinator-invoke" not in prompt
    assert "terminal_commit" not in prompt


def test_default_runner_parses_envelope_result(tmp_path, monkeypatch):
    from coordinator_core.ops.workflow_fire import fire

    log = tmp_path / "child.log"
    envelope = {"type": "result", "subtype": "success", "result": "```json\n" + json.dumps(PLAN_DIGEST) + "\n```"}
    log.write_text(json.dumps(envelope) + "\n", encoding="utf-8")
    captured = {}

    def fake_fire(script, **kw):
        captured.update(kw)
        return {"log_path": str(log)}

    monkeypatch.setattr(fire, "fire_workflow", fake_fire)
    result = driver._default_runner(str(tmp_path))(Path("s.mjs"), session_id="sid")
    assert result.digest == PLAN_DIGEST
    assert captured["wait"] is True and captured["session_id"] == "sid"
