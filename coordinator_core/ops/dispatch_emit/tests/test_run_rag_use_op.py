"""dispatch.run_rag_use: the rag_use receipt of one run, built on the stage gate's run resolution and verdict rule."""

from __future__ import annotations

import json

import pytest

from coordinator_core.authz.classification import OP_CLASSIFICATION, OpClass
from coordinator_core.ops.dispatch_emit import rag_use
from coordinator_core.ops.dispatch_emit import run_rag_use_op as op
from coordinator_core.ops.dispatch_emit import stage_rag_gate_op as gate

SESSION = "sess-run-rag-use"
RUN_ID = "ask-20261125T114055-72db8d"


def _tool_use(msg_id, name):
    return json.dumps({"requestId": f"r-{msg_id}", "message": {
        "id": msg_id, "model": "claude-sonnet-5-5", "role": "assistant",
        "usage": {"input_tokens": 1, "output_tokens": 1},
        "content": [{"type": "tool_use", "id": f"t-{msg_id}", "name": name, "input": {}}]}})


@pytest.fixture
def env(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    state = {"tier": "rag", "serving": True}
    monkeypatch.setattr(rag_use, "rag_tier", lambda _root, **_kw: state["tier"])
    monkeypatch.setattr("coordinator_core.bash_guards._rag_serving.serves_repo",
                        lambda _cwd=None: state["serving"])
    plugin = tmp_path / "plugin"
    roles = plugin / rag_use.GATED_ROLES_RELPATH
    roles.parent.mkdir(parents=True)
    roles.write_text(json.dumps({"gated": ["*scout*"], "exempt": ["*executor*"],
                                 "ue_cadence": {"window_tool_calls": 3, "min_rag_calls": 1}}), encoding="utf-8")
    wf = tmp_path / "projects" / "slug" / SESSION / "subagents" / "workflows" / "wf_one"
    wf.mkdir(parents=True)
    agents: list[tuple[str, str]] = []

    def add(agent_id, label, tools, agent_type="coordinator:scout"):
        agents.append((agent_id, label))
        (wf / "journal.jsonl").write_text(
            "\n".join(json.dumps({"type": "started", "agentId": a, "label": l}) for a, l in agents) + "\n",
            encoding="utf-8")
        lines = [f"scratch/warp/{RUN_ID}"] + [_tool_use(f"{agent_id}-{i}", n) for i, n in enumerate(tools)]
        (wf / f"agent-{agent_id}.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
        (wf / f"agent-{agent_id}.meta.json").write_text(json.dumps({"agentType": agent_type}), encoding="utf-8")

    def call(**over):
        params = {"repo_root": str(repo), "run_id": RUN_ID, "session_id": SESSION, "plugin_root": str(plugin)}
        params.update(over)
        return op._handler(params)

    call.add, call.state, call.tmp = add, state, tmp_path
    return call


def test_receipt_counts_rag_and_grep_per_agent_and_matches_the_gate(env):
    env.add("a1", "scout-A", ["mcp__project-rag__project_symbol", "Grep", "Read"])
    env.add("a2", "scout-B", ["Grep", "Glob"])
    env.add("a3", "work", ["Grep"], agent_type="coordinator:executor")
    out = env()
    receipt = out["rag_use"]
    assert receipt["installed"] is True and receipt["tier"] == "rag"
    assert receipt["totals"] == {"rag_calls": 1, "grep_calls": 4}
    verdicts = {a["agent_id"]: a["verdict"] for a in receipt["agents"]}
    assert verdicts == {"a1": "pass", "a2": "fail", "a3": "exempt"}
    assert out["halted_by"]["halted"] == rag_use.HALT_EXAMPLE_RETRIEVAL_REPO_UNUSED
    # receipt and gate name the same agents
    g = gate._handler({"repo_root": str(env.tmp / "repo"), "run_id": RUN_ID, "session_id": SESSION,
                       "plugin_root": str(env.tmp / "plugin")})
    assert [a["agent_id"] for a in g["halted_by"]["agents"]] == ["a2"]
    assert [a["agent_id"] for a in out["halted_by"]["agents"]] == ["a2"]


def test_all_gated_agents_used_rag_has_no_halt(env):
    env.add("a1", "scout-A", ["mcp__project-rag__project_symbol"])
    out = env()
    assert "halted_by" not in out and out["rag_use"]["agents"][0]["verdict"] == "pass"


def test_tier_none_is_all_exempt_and_never_halts(env):
    env.state["tier"] = "none"
    env.add("a1", "scout-A", ["Grep"])
    out = env()
    assert out["rag_use"]["installed"] is False
    assert out["rag_use"]["agents"][0]["verdict"] == "exempt"
    assert "halted_by" not in out


def test_unreadable_run_dir_is_an_error_never_zeros(env):
    env.add("a1", "scout-A", ["Grep"])
    assert env(run_id="ask-not-in-any-transcript")["error"] == "transcript_unreadable"
    assert env(session_id="no-such-session")["error"] == "transcript_unreadable"
    assert env(run_id="")["error"] == "transcript_unreadable"


def _ue(env, monkeypatch, ue=True):
    monkeypatch.setattr(rag_use, "is_ue_repo", lambda _root: ue)
    env.state["tier"] = "ue"


def _friction(env, *rows):
    d = env.tmp / "repo" / ".coordinator-local"
    d.mkdir(exist_ok=True)
    (d / "hook-friction.jsonl").write_text("not json\n" + "\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")


def _report(env, agent_id, text):
    wf = env.tmp / "projects" / "slug" / SESSION / "subagents" / "workflows" / "wf_one"
    with open(wf / f"agent-{agent_id}.jsonl", "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"requestId": "rq-final", "message": {
            "id": "m-final", "role": "assistant", "usage": {"input_tokens": 1, "output_tokens": 1},
            "content": [{"type": "text", "text": text}]}}) + "\n")


def test_ue_repo_with_no_ue_call_halts_retrieval_missing(env, monkeypatch):
    _ue(env, monkeypatch)
    env.add("a1", "scout-A", ["mcp__project-rag__project_symbol"])
    hb = env()["halted_by"]
    assert hb["halted"] == hb["cause"] == rag_use.HALT_UE_RETRIEVAL_MISSING
    assert [a["agent_id"] for a in hb["agents"]] == ["a1"]


def test_ue_call_passes_and_non_ue_repo_never_gets_the_ue_verdict(env, monkeypatch):
    _ue(env, monkeypatch)
    env.add("a1", "scout-A", ["mcp__project-rag__project_engine_symbol_source"])
    assert "halted_by" not in env()
    _ue(env, monkeypatch, ue=False)
    env.state["tier"] = "rag"
    _friction(env, {"kind": "retrieval_check_failed", "agent_id": "a1"})
    assert "halted_by" not in env()


def test_friction_row_for_the_run_halts_check_failed_and_others_are_ignored(env, monkeypatch):
    _ue(env, monkeypatch)
    env.add("a1", "scout-A", ["mcp__project-rag__project_engine_symbol_source"])
    _friction(env, {"kind": "retrieval_check_failed", "agent_id": "other"},
              {"kind": "something_else", "agent_id": "a1"})
    assert "halted_by" not in env()
    _friction(env, {"kind": "retrieval_check_failed", "agent_id": "a1"})
    hb = env()["halted_by"]
    assert hb["halted"] == rag_use.HALT_UE_RETRIEVAL_CHECK_FAILED and hb["friction_count"] == 1
    _friction(env, {"kind": "retrieval_check_failed", "session_id": SESSION})
    assert env()["halted_by"]["halted"] == rag_use.HALT_UE_RETRIEVAL_CHECK_FAILED


def test_outage_phrase_in_final_report_halts_and_plain_text_does_not(env, monkeypatch):
    _ue(env, monkeypatch)
    env.add("a1", "scout-A", ["mcp__project-rag__project_engine_symbol_source"])
    _report(env, "a1", "Done.\nexample_retrieval_repo was fine; the build was slow")
    assert "halted_by" not in env()
    for benign in (
        "example-retrieval-repo worked, retry after it timed out once",
        "example-retrieval-repo was not down",
        "example-retrieval-repo server wasn't unreachable",
        "no example-retrieval-repo outage",
        "There was no outage of example-retrieval-repo",
        "example-retrieval-repo was down at first but recovered",
    ):
        _report(env, "a1", "Done.\n" + benign)
        assert "halted_by" not in env(), benign
    _report(env, "a1", "Done.\nThe example-retrieval-repo server was unreachable for the whole run")
    hb = env()["halted_by"]
    assert hb["halted"] == rag_use.HALT_EXAMPLE_RETRIEVAL_REPO_OUTAGE_REPORTED
    assert "unreachable" in hb["reports"][0] and hb["reports"][0].startswith("scout-A:")
    for stated in ("example-retrieval-repo is down", "example-retrieval-repo outage: every call timed out"):
        _report(env, "a1", "Done.\n" + stated)
        assert env()["halted_by"]["halted"] == rag_use.HALT_EXAMPLE_RETRIEVAL_REPO_OUTAGE_REPORTED, stated


def test_recovery_word_in_a_later_clause_does_not_clear_an_outage(env, monkeypatch):
    _ue(env, monkeypatch)
    env.add("a1", "scout-A", ["mcp__project-rag__project_engine_symbol_source"])
    _report(env, "a1", "Done.\nexample_retrieval_repo was down; fine, used grep instead")
    assert env()["halted_by"]["halted"] == rag_use.HALT_EXAMPLE_RETRIEVAL_REPO_OUTAGE_REPORTED


def test_ue_run_with_no_worker_rows_is_unreadable_never_a_pass(env, monkeypatch):
    env.add("a1", "scout-A", ["Grep"])
    _ue(env, monkeypatch)
    assert env(labels=["nomatch"])["error"] == "transcript_unreadable"
    _ue(env, monkeypatch, ue=False)
    assert "error" not in env(labels=["nomatch"])


def test_friction_outranks_outage_and_ue_verdict_outranks_the_unused_halt(env, monkeypatch):
    _ue(env, monkeypatch)
    env.add("a1", "scout-A", ["Grep"])
    _report(env, "a1", "example-retrieval-repo is down")
    _friction(env, {"kind": "retrieval_check_failed", "agent_id": "a1"})
    assert env()["halted_by"]["halted"] == rag_use.HALT_UE_RETRIEVAL_CHECK_FAILED


def _metrics(env, *rows):
    d = env.tmp / "repo" / ".coordinator-local"
    d.mkdir(exist_ok=True)
    (d / "ue-research-gate-metrics.jsonl").write_text(
        "garbage\n" + "\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")


def _mrow(agent_id, decision, agent_type="coordinator:sizing-scout", run_id=RUN_ID):
    return {"ts": "t", "agent_id": agent_id, "run_id": run_id, "session_id": SESSION,
            "agent_type": agent_type, "decision": decision, "tool": "mcp__project-rag__project_engine_x"}


def test_metrics_retrieval_row_passes_and_breakdown_is_by_phase_and_agent(env, monkeypatch):
    _ue(env, monkeypatch)
    env.add("a1", "scout-A", ["mcp__project-rag__project_symbol"], agent_type="coordinator:sizing-scout")
    env.add("a2", "plan", ["mcp__project-rag__project_engine_symbol_source"], agent_type="coordinator:plan-author")
    assert env()["halted_by"]["agents"][0]["agent_id"] == "a1"
    _metrics(env, _mrow("a1", "retrieval"), _mrow("a1", "deny"), _mrow("zz", "retrieval", run_id="other"))
    out = env()
    assert "halted_by" not in out
    bp = out["ue_retrieval"]["by_phase"]
    assert bp["sizing"]["retrievals"] == 1 and bp["sizing"]["metrics_rows"] == 2
    assert bp["planning"]["agents"][0]["agent_id"] == "a2" and bp["planning"]["retrievals"] == 1


def test_null_agent_type_is_unmapped_never_a_pass(env, monkeypatch):
    _ue(env, monkeypatch)
    env.add("a1", "scout-A", ["Grep"], agent_type="")
    _metrics(env, _mrow("a1", "deny", agent_type=None))
    out = env()
    assert out["halted_by"]["halted"] == rag_use.HALT_UE_RETRIEVAL_MISSING
    assert list(out["halted_by"]["by_phase"]) == ["unmapped"]
    assert out["ue_retrieval"]["by_phase"]["unmapped"]["agents"][0]["retrievals"] == 0


def test_friction_denies_are_counted_per_agent_in_the_breakdown(env, monkeypatch):
    _ue(env, monkeypatch)
    env.add("a1", "rev", ["mcp__project-rag__project_engine_symbol_source"], agent_type="coordinator:plan-reviewer")
    _friction(env, {"kind": "retrieval_check_failed", "agent_id": "a1"})
    hb = env()["halted_by"]
    assert hb["by_phase"]["review"]["denies"] == 1 and hb["agents"][0]["denies"] == 1


def test_ue_halted_by_validates_against_the_vendored_wake_digest_schema(env, monkeypatch):
    import jsonschema

    from coordinator_core.ops.dispatch_emit import wake_digest

    _ue(env, monkeypatch)
    env.add("a1", "rev", ["Grep"], agent_type="coordinator:plan-reviewer")
    hb = env()["halted_by"]
    assert "by_phase" in hb
    sub = wake_digest.load_doe_schema()["properties"]["halted_by"]
    assert list(jsonschema.Draft202012Validator(sub).iter_errors(hb)) == []
    assert list(jsonschema.Draft202012Validator(sub).iter_errors({**hb, "bogus": 1}))


def test_non_ue_repo_has_no_breakdown(env, monkeypatch):
    _ue(env, monkeypatch, ue=False)
    env.add("a1", "scout-A", ["Grep"])
    assert "ue_retrieval" not in env()


def test_op_is_registered_compute_only_and_scoped():
    from coordinator_core.op_scopes import _OP_KEY_SCOPE
    from coordinator_core.ops._registry_map import OP_MODULE_MAP as reg

    assert OP_CLASSIFICATION["dispatch.run_rag_use"] is OpClass.COMPUTE_ONLY
    assert _OP_KEY_SCOPE["dispatch.run_rag_use"] == "common_dir"
    assert reg["dispatch.run_rag_use"] == "coordinator_core.ops.dispatch_emit.run_rag_use_op"
