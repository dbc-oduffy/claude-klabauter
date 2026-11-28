"""dispatch.stage_rag_gate in a UE repo: one UE-corpus retrieval per phase at the sizing, plan and review boundaries."""

from __future__ import annotations

import json

import pytest

from coordinator_core.ops.dispatch_emit import rag_use
from coordinator_core.ops.dispatch_emit import stage_rag_gate_op as gate

SESSION = "sess-uephase"
RUN_ID = "ask-20261127T225414-ea32d2"
UE_TOOL = "mcp__project-rag__project_engine_examples"
PLAIN_TOOL = "mcp__project-rag__project_symbol"
PHASE_AGENT = {"sizing": "coordinator:sizing-scout", "planning": "coordinator:plan-author",
               "review": "coordinator:plan-reviewer"}
STAGE = {"sizing": "sizing", "planning": "plan", "review": "review"}


def _tool_use(msg_id, name):
    return json.dumps({"requestId": f"r-{msg_id}", "message": {
        "id": msg_id, "model": "claude-sonnet-5-5", "role": "assistant",
        "usage": {"input_tokens": 1, "output_tokens": 1},
        "content": [{"type": "tool_use", "id": f"t-{msg_id}", "name": name, "input": {}}]}})


def _text(text):
    return json.dumps({"message": {"role": "assistant", "content": [{"type": "text", "text": text}]}})


@pytest.fixture
def env(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    (repo / ".coordinator-local").mkdir(parents=True)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr(rag_use, "rag_tier", lambda _root, **_kw: "ue")
    monkeypatch.setattr(rag_use, "is_ue_repo", lambda _root: True)
    monkeypatch.setattr("coordinator_core.bash_guards._rag_serving.serves_repo", lambda _cwd=None: True)
    monkeypatch.setattr("coordinator_core.ops.dispatch_emit.rag_health.repo_verdict", lambda _root, **_kw: None)
    wf = tmp_path / "projects" / "slug" / SESSION / "subagents" / "workflows" / "wf_one"
    wf.mkdir(parents=True)
    agents: list[tuple[str, str]] = []
    plugin = tmp_path / "plugin" / rag_use.GATED_ROLES_RELPATH
    plugin.parent.mkdir(parents=True)
    plugin.write_text(json.dumps({"gated": [], "exempt": [], "ue_cadence": {"window_tool_calls": 99, "min_rag_calls": 0}}),
                      encoding="utf-8")

    def add(agent_id, agent_type, tools=(), final=None):
        agents.append((agent_id, f"label-{agent_id}"))
        (wf / "journal.jsonl").write_text(
            "\n".join(json.dumps({"type": "started", "agentId": a, "label": l}) for a, l in agents) + "\n",
            encoding="utf-8")
        lines = [f"scratch/warp/{RUN_ID}"] + [_tool_use(f"{agent_id}-{i}", n) for i, n in enumerate(["Read", *tools])]
        if final:
            lines.append(_text(final))
        (wf / f"agent-{agent_id}.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
        (wf / f"agent-{agent_id}.meta.json").write_text(json.dumps({"agentType": agent_type}), encoding="utf-8")

    def metric(agent_id, agent_type, decision):
        row = {"ts": "t", "agent_id": agent_id, "run_id": RUN_ID, "session_id": SESSION,
               "agent_type": agent_type, "decision": decision, "tool": "x"}
        with (repo / ".coordinator-local" / "ue-research-gate-metrics.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row) + "\n")

    def call(stage):
        return gate._handler({"repo_root": str(repo), "run_id": RUN_ID, "session_id": SESSION, "stage": stage,
                              "plugin_root": str(tmp_path / "plugin")})

    call.add, call.metric, call.repo = add, metric, repo
    return call


@pytest.mark.parametrize("phase", ["sizing", "planning", "review"])
def test_phase_with_zero_retrievals_aborts(env, phase):
    env.add("a1", PHASE_AGENT[phase], [PLAIN_TOOL])
    out = env(STAGE[phase])
    assert out["halted_by"]["halted"] == "ue_phase_retrieval_missing"
    assert out["halted_by"]["phase"] == phase
    assert [a["agent_id"] for a in out["halted_by"]["agents"]] == ["a1"]
    assert out["ue_phase"]["phase"] == phase and out["ue_phase"]["agents"][0]["retrievals"] == 0


@pytest.mark.parametrize("phase", ["sizing", "planning", "review"])
def test_phase_with_one_retrieval_passes(env, phase):
    env.add("a1", PHASE_AGENT[phase], [UE_TOOL])
    out = env(STAGE[phase])
    assert "halted_by" not in out
    assert out["ue_phase"]["agents"][0] == {
        "agent_id": "a1", "agent_type": PHASE_AGENT[phase], "retrievals": 1, "structural_calls": 0, "denies": 0,
        "metrics_rows": 0}


def test_metrics_retrieval_row_meets_the_bar_and_is_counted(env):
    env.add("a1", "coordinator:plan-author", [PLAIN_TOOL])
    env.metric("a1", "coordinator:plan-author", "retrieval")
    env.metric("a1", "coordinator:plan-author", "deny")
    out = env("plan")
    assert "halted_by" not in out
    assert out["ue_phase"]["agents"][0]["retrievals"] == 1 and out["ue_phase"]["agents"][0]["metrics_rows"] == 2


def test_another_phases_retrieval_does_not_pass_this_phase(env):
    env.add("a1", "coordinator:sizing-scout", [UE_TOOL])
    env.add("a2", "coordinator:plan-reviewer", [PLAIN_TOOL])
    out = env("review")
    assert out["halted_by"]["halted"] == "ue_phase_retrieval_missing"
    assert [a["agent_id"] for a in out["ue_phase"]["agents"]] == ["a2"]


def test_null_or_unmapped_agent_type_never_passes(env):
    env.add("a1", "general-purpose", [UE_TOOL])
    env.add("a2", "", [UE_TOOL])
    env.metric("a2", None, "retrieval")
    out = env("sizing")
    assert out["halted_by"]["halted"] == "ue_phase_retrieval_missing"
    assert out["ue_phase"]["agents"] == []


def test_example_retrieval_repo_outage_in_the_phase_aborts_despite_a_retrieval(env):
    env.add("a1", "coordinator:plan-reviewer", [UE_TOOL], final="The example-retrieval-repo server was down for the review.")
    out = env("review")
    assert out["halted_by"]["halted"] == rag_use.HALT_EXAMPLE_RETRIEVAL_REPO_OUTAGE_REPORTED
    assert out["halted_by"]["phase"] == "review"


def test_retrieval_check_failed_friction_row_aborts_and_is_counted_per_agent(env):
    env.add("a1", "coordinator:sizing-scout", [UE_TOOL])
    (env.repo / ".coordinator-local" / "hook-friction.jsonl").write_text(
        json.dumps({"kind": "retrieval_check_failed", "agent_id": "a1"}) + "\n", encoding="utf-8")
    out = env("sizing")
    assert out["ue_phase"]["agents"][0]["denies"] == 1
    assert out["halted_by"]["halted"] == rag_use.HALT_UE_RETRIEVAL_CHECK_FAILED
    assert out["halted_by"]["phase"] == "sizing"


def test_friction_row_of_an_agent_outside_the_phase_does_not_abort(env):
    env.add("a1", "coordinator:sizing-scout", [UE_TOOL])
    env.add("a2", "general-purpose", [PLAIN_TOOL])
    (env.repo / ".coordinator-local" / "hook-friction.jsonl").write_text(
        json.dumps({"kind": "retrieval_check_failed", "agent_id": "a2"}) + "\n", encoding="utf-8")
    out = env("sizing")
    assert "halted_by" not in out and out["ue_phase"]["agents"][0]["denies"] == 0


@pytest.mark.parametrize("verdict", ["degraded", "partial", "down"])
def test_structural_call_counts_when_the_semantic_lane_is_not_ok(env, monkeypatch, verdict):
    monkeypatch.setattr("coordinator_core.ops.dispatch_emit.rag_health.repo_verdict",
                        lambda _root, **_kw: {"verdict": verdict})
    env.add("a1", "coordinator:plan-author", ["mcp__project-rag__project_code_outline"])
    out = env("plan")
    assert "halted_by" not in out
    assert out["ue_phase"]["semantic_lane_down"] is True
    assert out["ue_phase"]["agents"][0]["retrievals"] == 1 and out["ue_phase"]["agents"][0]["structural_calls"] == 1


@pytest.mark.parametrize("verdict", [None, "health_unreadable", "health_unknown_repo"])
def test_structural_call_does_not_count_when_the_semantic_lane_is_ok_or_unjudged(env, monkeypatch, verdict):
    monkeypatch.setattr("coordinator_core.ops.dispatch_emit.rag_health.repo_verdict",
                        lambda _root, **_kw: {"verdict": verdict} if verdict else None)
    env.add("a1", "coordinator:plan-author", [PLAIN_TOOL])
    out = env("plan")
    assert out["halted_by"]["halted"] == "ue_phase_retrieval_missing"
    assert out["ue_phase"]["agents"][0]["structural_calls"] == 1 and out["ue_phase"]["agents"][0]["retrievals"] == 0


def test_non_structural_non_ue_call_does_not_count_when_the_semantic_lane_is_down(env, monkeypatch):
    monkeypatch.setattr("coordinator_core.ops.dispatch_emit.rag_health.repo_verdict",
                        lambda _root, **_kw: {"verdict": "degraded"})
    env.add("a1", "coordinator:plan-author", ["mcp__project-rag__project_semantic_search"])
    assert env("plan")["halted_by"]["halted"] == "ue_phase_retrieval_missing"


def test_non_ue_repo_and_unchecked_stage_carry_no_ue_phase(env, monkeypatch):
    env.add("a1", "coordinator:sizing-scout", [PLAIN_TOOL])
    assert "ue_phase" not in env("build")
    monkeypatch.setattr(rag_use, "is_ue_repo", lambda _root: False)
    out = env("sizing")
    assert "ue_phase" not in out and "halted_by" not in out
