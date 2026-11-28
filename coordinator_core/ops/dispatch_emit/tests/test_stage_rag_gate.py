"""dispatch.stage_rag_gate: per-run, per-agent example-retrieval-repo use read from agent transcripts, armed by tier."""

from __future__ import annotations

import json
import time

import pytest

from coordinator_core.ops.dispatch_emit import rag_use
from coordinator_core.ops.dispatch_emit import stage_rag_gate_op as gate

SESSION = "sess-stage"
RUN_ID = "ask-20261124T145236-b8a33e"


def _tool_use(msg_id, name):
    return json.dumps({"requestId": f"r-{msg_id}", "message": {
        "id": msg_id, "model": "claude-sonnet-5-5", "role": "assistant",
        "usage": {"input_tokens": 1, "output_tokens": 1},
        "content": [{"type": "tool_use", "id": f"t-{msg_id}", "name": name, "input": {}}]}})


def _roles_file(plugin_root, **over):
    data = {"gated": ["*scout*", "*reviewer*"], "exempt": ["*executor*"],
            "ue_cadence": {"window_tool_calls": 3, "min_rag_calls": 1}}
    data.update(over)
    path = plugin_root / rag_use.GATED_ROLES_RELPATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")
    return plugin_root


@pytest.fixture
def env(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    state = {"tier": "rag", "serving": True}
    monkeypatch.setattr(rag_use, "rag_tier", lambda _root, **_kw: state["tier"])
    monkeypatch.setattr("coordinator_core.bash_guards._rag_serving.serves_repo",
                        lambda _cwd=None: state["serving"])
    wf = tmp_path / "projects" / "slug" / SESSION / "subagents" / "workflows" / "wf_one"
    wf.mkdir(parents=True)
    agents: list[tuple[str, str]] = []

    def add(agent_id, label, rag_calls, agent_type="coordinator:scout", names_run=True, tools=None,
            error_text=None):
        agents.append((agent_id, label))
        (wf / "journal.jsonl").write_text(
            "\n".join(json.dumps({"type": "started", "agentId": a, "label": l}) for a, l in agents) + "\n",
            encoding="utf-8")
        lines = [f"scratch/warp/{RUN_ID}" if names_run else "unrelated"]
        if tools is None:
            tools = ["mcp__project-rag__project_symbol"] * rag_calls + ["Read"]
        for i, name in enumerate(tools):
            lines.append(_tool_use(f"{agent_id}-{i}", name))
            if error_text is not None and name.startswith("mcp__project-rag__"):
                lines.append(json.dumps({"type": "user", "message": {"role": "user", "content": [
                    {"type": "tool_result", "tool_use_id": f"t-{agent_id}-{i}", "is_error": True,
                     "content": error_text}]}}))
        (wf / f"agent-{agent_id}.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
        (wf / f"agent-{agent_id}.meta.json").write_text(json.dumps({"agentType": agent_type}), encoding="utf-8")

    def call(**over):
        params = {"repo_root": str(repo), "run_id": RUN_ID, "session_id": SESSION,
                  "plugin_root": str(_roles_file(tmp_path / "plugin"))}
        params.update(over)
        return gate._handler(params)

    call.add = add
    call.state = state
    call.tmp = tmp_path
    return call


def test_gated_agent_with_zero_calls_halts_even_when_a_peer_used_rag(env):
    env.add("a1", "scout-A", 0)
    env.add("a3", "scout-D", 1)
    out = env()
    assert out["verdict"] == rag_use.RAG_NOT_USED and out["rag_calls"] == 1
    assert out["tier"] == "rag" and out["roles_source"] == "file"
    assert out["halted_by"]["halted"] == rag_use.HALT_EXAMPLE_RETRIEVAL_REPO_UNUSED
    assert [a["agent_id"] for a in out["halted_by"]["agents"]] == ["a1"]
    assert out["halted_by"]["agents"][0]["rag_calls"] == 0
    rows = {w["label"]: w for w in out["per_worker"]}
    assert rows["scout-A"]["verdict"] == "fail" and rows["scout-D"]["verdict"] == "pass"
    assert rows["scout-D"]["agent_type"] == "coordinator:scout" and rows["scout-D"]["gated"] is True


def test_every_gated_agent_calling_is_ok_and_does_not_halt(env):
    env.add("a1", "scout-A", 1)
    env.add("a2", "scout-B", 4)
    out = env()
    assert out["verdict"] == "ok" and "halted_by" not in out and out["rag_calls"] == 5
    assert {w["label"]: w["rag_calls"] for w in out["per_worker"]} == {"scout-A": 1, "scout-B": 4}


def test_backpressured_calls_count_as_attempts_and_record_search_degraded(env):
    env.add("a1", "scout-A", 2, error_text="backpressure: call_deadline, all lanes cut")
    out = env()
    assert out["rag_calls"] == 2 and out["verdict"] == rag_use.DEGRADED
    assert out[rag_use.SEARCH_DEGRADED_KEY] is True
    assert not (out.get("halted_by") or {}).get("halted") == rag_use.HALT_EXAMPLE_RETRIEVAL_REPO_UNUSED
    assert out["per_worker"][0]["rag_calls"] == 2 and out["per_worker"][0]["verdict"] == "pass"


def test_non_timeout_errors_and_partial_success_do_not_set_search_degraded(env):
    env.add("a1", "scout-A", 2, error_text="boom: index corrupt")
    out = env()
    assert out["verdict"] == rag_use.DEGRADED and rag_use.SEARCH_DEGRADED_KEY not in out
    env.add("a2", "scout-B", 1)
    out = env()
    assert out["verdict"] == "ok" and rag_use.SEARCH_DEGRADED_KEY not in out


def test_exempt_executor_with_zero_calls_neither_halts_nor_fails(env):
    env.add("a1", "scout-A", 2)
    env.add("x1", "exec-1", 0, agent_type="coordinator:executor")
    out = env()
    assert out["verdict"] == "ok" and "halted_by" not in out
    exec_row = next(w for w in out["per_worker"] if w["label"] == "exec-1")
    assert exec_row["gated"] is False and exec_row["verdict"] == "exempt" and "rag_errors" in exec_row


def test_relay_and_close_agents_are_not_workers(env):
    env.add("a1", "scout-A", 0)
    env.add("r1", "research-close", 5)
    env.add("r2", "deferred-emit", 5, agent_type="coordinator:op-relay")
    out = env()
    assert out["verdict"] == rag_use.RAG_NOT_USED
    assert [w["label"] for w in out["per_worker"]] == ["scout-A"]


def test_labels_param_narrows_the_workers_counted(env):
    env.add("a1", "scout-A", 0)
    env.add("a2", "scout-D", 3)
    out = env(labels=["scout-A"])
    assert out["verdict"] == rag_use.RAG_NOT_USED


def test_tier_none_is_not_applicable_and_never_halts(env):
    env.add("a1", "scout-A", 0)
    env.state["tier"] = "none"
    out = env()
    assert out["verdict"] == "not_applicable" and out["tier"] == "none" and "halted_by" not in out


def test_unserved_repo_still_arms_tier_rag(env):
    env.add("a1", "scout-A", 0)
    env.state["serving"] = False
    out = env()
    assert out["verdict"] == rag_use.RAG_NOT_USED
    assert out["halted_by"]["halted"] == rag_use.HALT_EXAMPLE_RETRIEVAL_REPO_UNUSED


def test_tier_ue_unserved_halts_not_serving(env):
    env.add("a1", "scout-A", 3)
    env.state.update(tier="ue", serving=False)
    out = env()
    assert out["tier"] == "ue" and out["halted_by"]["halted"] == rag_use.HALT_EXAMPLE_RETRIEVAL_REPO_NOT_SERVING


def test_tier_ue_serving_boundary_both_sides(env):
    env.add("a1", "scout-A", 3, tools=["mcp__project-rag__project_symbol", "Read", "Read"])
    env.state.update(tier="ue", serving=True)
    assert "halted_by" not in env()


def test_tier_ue_low_cadence_halts_and_tier_rag_does_not(env):
    env.add("a1", "scout-A", 1, tools=["mcp__project-rag__project_symbol"] + ["Read"] * 6)
    env.state["tier"] = "ue"
    out = env()
    assert out["halted_by"]["halted"] == rag_use.HALT_EXAMPLE_RETRIEVAL_REPO_CADENCE
    assert out["halted_by"]["agents"][0]["agent_id"] == "a1"
    env.state["tier"] = "rag"
    assert "halted_by" not in env()


def test_missing_roles_file_gates_by_fallback_and_reports_it(env):
    env.add("a1", "scout-A", 0)
    out = env(plugin_root=str(env.tmp / "no-such-plugin"))
    assert out["roles_source"] == "fallback" and out["roles_reason"]
    assert out["halted_by"]["halted"] == rag_use.HALT_EXAMPLE_RETRIEVAL_REPO_UNUSED


def test_stage_param_is_echoed(env):
    env.add("a1", "scout-A", 1)
    assert env(stage="sizing")["stage"] == "sizing"


def test_unknown_is_transcript_unreadable_never_zero_and_never_halts(env):
    out = env()
    assert out["verdict"] == rag_use.TRANSCRIPT_UNREADABLE and out["reason"] and "halted_by" not in out
    env.add("a1", "scout-A", 0, names_run=False)
    assert env()["verdict"] == rag_use.TRANSCRIPT_UNREADABLE
    assert env(session_id="no-such-session")["verdict"] == rag_use.TRANSCRIPT_UNREADABLE
    assert env(run_id="")["verdict"] == rag_use.TRANSCRIPT_UNREADABLE


def test_ten_agent_run_is_judged_under_200ms_process_time(env):
    for i in range(10):
        env.add(f"a{i}", f"scout-{i}", 2)
    env()
    start = time.process_time()
    out = env()
    assert time.process_time() - start < 0.2
    assert out["verdict"] == "ok" and len(out["per_worker"]) == 10


def test_op_is_registered_compute_only_common_dir():
    from coordinator_core.authz.classification import OpClass, OP_CLASSIFICATION
    from coordinator_core.op_scopes import OP_KEY_SCOPE
    from coordinator_core.ops._registry_map import OP_MODULE_MAP

    assert OP_CLASSIFICATION[gate.OP_NAME] is OpClass.COMPUTE_ONLY
    assert OP_KEY_SCOPE[gate.OP_NAME] == "common_dir"
    assert OP_MODULE_MAP[gate.OP_NAME] == gate.__name__
