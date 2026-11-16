"""dispatch.docs_check_rag_gate: transcript-backed verdict for a UE docs-checker leg."""

from __future__ import annotations

import ast
import inspect
import json

import pytest

from coordinator_core.ops.dispatch_emit import docs_check_gate_op as gate
from coordinator_core.ops.dispatch_emit import rag_use

# Assembled: the publish scrub rewrites the whole-word literal, so a test literal would drift.
REAL = "-".join(("project", "rag"))
PLACEHOLDER = "example-retrieval-repo"
SESSION = "sess-gate"
RUN_ID = "ask-20261106T000000-abc123"
LABEL = "plan-docs-check"
AGENT = "agdocs1"


def _line(msg_id, content):
    return json.dumps({
        "requestId": f"r-{msg_id}",
        "message": {"id": msg_id, "model": "claude-sonnet-5-5", "role": "assistant",
                    "usage": {"input_tokens": 1, "output_tokens": 1}, "content": content},
    })


def _tool_use(msg_id, name, inp=None):
    return _line(msg_id, [{"type": "tool_use", "id": f"t-{msg_id}", "name": name, "input": inp or {}}])


def _text(msg_id, text):
    return _line(msg_id, [{"type": "text", "text": text}])


def _tool_error(msg_id):
    return json.dumps({"message": {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": f"t-{msg_id}", "is_error": True, "content": "x"}]}})


PROBE_OK = json.dumps({"state": "ok", "per_source": {"engine": "ok"}})
PROBE_DOWN = json.dumps({"state": "down", "per_source": {}})


@pytest.fixture(autouse=True)
def probe(monkeypatch):
    """Default: the probe CLI is absent and backoff does not sleep; tests set `probe.out`."""
    state = {"out": None, "calls": 0}

    def runner(argv):
        state["calls"] += 1
        return state["out"]

    monkeypatch.setattr(gate, "_probe_runner", runner)
    monkeypatch.setattr(gate, "_sleep", lambda _s: None)
    return state


@pytest.fixture
def env(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "Game.uproject").write_text("{}", encoding="utf-8")
    (repo / ".project-rag").mkdir()
    (repo / ".project-rag" / "state.json").write_text("{}", encoding="utf-8")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    wf = tmp_path / "projects" / "slug" / SESSION / "subagents" / "workflows" / "wf_one"
    wf.mkdir(parents=True)
    (wf / "journal.jsonl").write_text(
        json.dumps({"type": "started", "key": "k", "agentId": AGENT, "label": LABEL}) + "\n",
        encoding="utf-8",
    )
    (wf / "agent-other.jsonl").write_text(f"scratch/warp/{RUN_ID}\n", encoding="utf-8")

    def transcript(lines):
        (wf / f"agent-{AGENT}.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")

    def call(**over):
        params = {"repo_root": str(repo), "run_id": RUN_ID, "label": LABEL, "session_id": SESSION}
        params.update(over)
        return gate._handler(params)

    def peer(lines):
        (wf / "agent-peer.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")

    call.peer = peer
    return repo, transcript, call


def test_zero_rag_calls_is_rag_not_used_not_down(env):
    _repo, transcript, call = env
    transcript([_tool_use("m1", "Read")])
    out = call()
    assert out["verdict"] == "rag_not_used"
    assert out["branch"] == "zero_rag_calls"
    assert "message" not in out


def test_zero_rag_calls_with_engine_queryable_false_is_down(env):
    _repo, transcript, call = env
    transcript([_tool_use("m1", "Read")])
    out = call(engine_queryable=False)
    assert out["verdict"] == "rag_corpus_down"
    assert out["branch"] == "engine_queryable_false"


def test_one_successful_rag_call_is_ok(env):
    _repo, transcript, call = env
    transcript([_tool_use("m1", "mcp__project-rag__project_symbol")])
    assert call()["verdict"] == "ok"


def test_unclassified_all_failed_is_degraded_not_down(env):
    _repo, transcript, call = env
    transcript([_tool_use("m1", "mcp__project-rag__project_symbol"), _tool_error("m1")])
    assert call()["verdict"] == "degraded"


def test_engine_queryable_false_is_rag_corpus_down_despite_successful_call(env):
    _repo, transcript, call = env
    transcript([_tool_use("m1", "mcp__project-rag__project_symbol")])
    out = call(engine_queryable=False)
    assert out["verdict"] == rag_use.HALT_RAG_CORPUS_DOWN
    assert REAL in out["message"]


def test_engine_queryable_true_or_absent_does_not_down_a_working_corpus(env):
    _repo, transcript, call = env
    transcript([_tool_use("m1", "mcp__project-rag__project_symbol")])
    assert call(engine_queryable=True)["verdict"] == "ok"
    assert call()["verdict"] == "ok"


def _tool_error_kind(msg_id, text):
    return json.dumps({"message": {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": f"t-{msg_id}", "is_error": True, "content": text}]}})


def test_timeout_only_failure_is_degraded_with_counts(env):
    _repo, transcript, call = env
    transcript([
        _tool_use("m1", "mcp__project-rag__project_engine_examples"),
        _tool_error_kind("m1", "request timed out after 25s"),
    ])
    out = call()
    assert out["verdict"] == "degraded"
    assert (out["rag_calls"], out["rag_failed"], out["rag_timeouts"], out["rag_refused"]) == (1, 1, 1, 0)
    assert "message" not in out


def test_degraded_still_runs_the_citation_check(env):
    _repo, transcript, call = env
    transcript([
        _tool_use("m1", "mcp__project-rag__project_engine_examples"),
        _tool_error_kind("m1", "request timed out after 25s"),
        _text("m2", "[live: inspect L_Main]"),
    ])
    out = call()
    assert out["verdict"] == "citation_unbacked"
    assert out["unbacked"] == ["[live: inspect L_Main]"]


def test_refused_connection_stays_rag_corpus_down(env):
    _repo, transcript, call = env
    transcript([
        _tool_use("m1", "mcp__project-rag__project_symbol"),
        _tool_error_kind("m1", "connection refused"),
    ])
    out = call()
    assert out["verdict"] == "rag_corpus_down"
    assert out["branch"] == "all_calls_refused"
    assert REAL in out["message"]


def test_unreadable_transcript_is_transcript_unreadable_never_down(env):
    _repo, _transcript, call = env
    no_transcript = call()
    assert no_transcript["verdict"] == "transcript_unreadable"
    assert no_transcript["branch"] == "transcript_missing"
    assert "message" not in no_transcript
    assert call(label="no-such-leg")["branch"] == "agent_not_located"
    assert call(session_id="absent-session")["branch"] == "session_dir_not_located"
    assert call(run_id="")["branch"] == "params_missing"
    for out in (call(), call(label="no-such-leg"), call(session_id="absent-session")):
        assert out["verdict"] == "transcript_unreadable"


def test_unreadable_transcript_with_engine_queryable_false_is_down(env):
    _repo, _transcript, call = env
    out = call(label="no-such-leg", engine_queryable=False)
    assert out["verdict"] == "rag_corpus_down"
    assert out["branch"] == "engine_queryable_false"


def test_non_ue_or_rag_absent_repo_is_not_applicable(env, tmp_path):
    repo, transcript, call = env
    transcript([_tool_use("m1", "Read")])
    plain = tmp_path / "plain"
    plain.mkdir()
    assert call(repo_root=str(plain))["verdict"] == "not_applicable"
    (repo / ".project-rag" / "state.json").unlink()
    assert call()["verdict"] == "not_applicable"


RAG = "mcp__project-rag__project_cpp_symbol"


def test_all_citations_backed_across_run_is_ok(env):
    _repo, transcript, call = env
    transcript([
        _tool_use("m1", RAG, {"name": "AActor"}),
        _text("m2", "AActor exists [rag: project_cpp_symbol AActor] and [live: inspect L_Main]."),
    ])
    call.peer([_tool_use("p1", "mcp__example_game_repo-control__inspect", {"target": "L_Main"})])
    out = call()
    assert out["verdict"] == "ok"
    assert out["unbacked"] == []


def test_unbacked_citation_is_citation_unbacked(env):
    _repo, transcript, call = env
    transcript([
        _tool_use("m1", RAG, {"name": "AActor"}),
        _text("m2", "[rag: project_cpp_symbol AActor] [live: inspect L_Main]"),
    ])
    out = call()
    assert out["verdict"] == "citation_unbacked"
    assert out["unbacked"] == ["[live: inspect L_Main]"]


def test_only_final_text_is_read_and_rag_down_outranks_unbacked(env):
    _repo, transcript, call = env
    transcript([
        _tool_use("m1", RAG, {"name": "AActor"}),
        _text("m2", "[rag: project_cpp_symbol Ghost]"),
        _text("m3", "final: [rag: project_cpp_symbol AActor]"),
    ])
    assert call()["verdict"] == "ok"
    transcript([_tool_use("m1", "Read"), _text("m2", "[rag: project_cpp_symbol Ghost]")])
    assert call()["verdict"] == "citation_unbacked"
    transcript([
        _tool_use("m1", "mcp__project-rag__project_symbol"), _tool_error_kind("m1", "connection refused"),
        _text("m2", "[rag: project_cpp_symbol Ghost]"),
    ])
    assert call()["verdict"] == "rag_corpus_down"


def _bind_freshness(monkeypatch, tmp_path, state, remedy=("project-rag", "refresh")):
    from coordinator_core.ops import rag_freshness

    spawned = []
    real = rag_freshness.freshness_precondition

    def pre(root):
        return real(
            root,
            verdict_fn=lambda r: rag_freshness.FreshnessVerdict(state, remedy, "test"),
            spawn_fn=lambda argv, cwd: spawned.append(list(argv)),
            marker_root=tmp_path / "markers",
        )

    monkeypatch.setattr(gate.rag_freshness, "freshness_precondition", pre)
    return spawned


def test_stale_index_queues_one_refresh_and_reports_freshness(env, tmp_path, monkeypatch):
    repo, transcript, call = env
    transcript([_tool_use("m1", RAG, {"name": "AActor"})])
    spawned = _bind_freshness(monkeypatch, tmp_path, "stale")
    out = call()
    assert out["verdict"] == "ok"
    assert out["index_freshness"] == {"state": "stale", "refresh_queued": True}
    assert call()["index_freshness"] == {"state": "stale", "refresh_queued": False}
    assert spawned == [["project-rag", "refresh"]]


def test_fresh_index_queues_nothing_and_not_applicable_carries_no_field(env, tmp_path, monkeypatch):
    repo, transcript, call = env
    transcript([_tool_use("m1", RAG, {"name": "AActor"})])
    spawned = _bind_freshness(monkeypatch, tmp_path, "fresh")
    assert call()["index_freshness"] == {"state": "fresh", "refresh_queued": False}
    plain = tmp_path / "plain"
    plain.mkdir()
    out = call(repo_root=str(plain))
    assert out == {"verdict": "not_applicable"}
    assert spawned == []
def _refused(transcript):
    transcript([
        _tool_use("m1", "mcp__project-rag__project_symbol"), _tool_error_kind("m1", "connection refused"),
    ])


def test_down_message_names_real_corpus_probe_raw_output_and_restorer(env, probe):
    _repo, transcript, call = env
    _refused(transcript)
    probe["out"] = PROBE_DOWN
    out = call()
    assert out["verdict"] == "rag_corpus_down"
    msg = out["message"]
    assert REAL in msg and PLACEHOLDER not in msg
    assert "owner session" in msg and "probe-reachability" in msg
    assert '"state": "down"' in msg
    transcript([_tool_use("m1", "Read")])
    queryable_false = call(engine_queryable=False)["message"]
    assert "engine_queryable=false" in queryable_false and REAL in queryable_false


def test_serving_corpus_on_reprobe_never_yields_rag_corpus_down(env, probe):
    _repo, transcript, call = env
    probe["out"] = PROBE_OK
    _refused(transcript)
    out = call()
    assert out["verdict"] == "degraded"
    assert out["branch"] == "reprobe_recovered"
    assert "message" not in out
    transcript([_tool_use("m1", "mcp__project-rag__project_symbol")])
    out = call(engine_queryable=False)
    assert (out["verdict"], out["branch"]) == ("degraded", "reprobe_recovered")
    out = call(label="no-such-leg", engine_queryable=False)
    assert out["verdict"] == "degraded"


def test_recovered_reprobe_still_runs_the_citation_check(env, probe):
    _repo, transcript, call = env
    probe["out"] = PROBE_OK
    transcript([
        _tool_use("m1", "mcp__project-rag__project_symbol"), _tool_error_kind("m1", "connection refused"),
        _text("m2", "[live: inspect L_Main]"),
    ])
    assert call()["verdict"] == "citation_unbacked"


def test_reprobe_retries_with_backoff_before_halting(env, probe, monkeypatch):
    _repo, transcript, call = env
    _refused(transcript)
    sleeps = []
    monkeypatch.setattr(gate, "_sleep", sleeps.append)
    monkeypatch.setattr(gate, "_probe_runner", lambda _argv: PROBE_OK)
    assert call()["verdict"] == "degraded"
    assert sleeps == [0.1]
    assert sum(gate._REPROBE_BACKOFF_S) + gate._PROBE_TIMEOUT_S * len(gate._REPROBE_BACKOFF_S) <= 0.5
    probe["out"] = None
    monkeypatch.setattr(gate, "_probe_runner", lambda _argv: None)
    assert call()["verdict"] == "rag_corpus_down"


def test_down_probe_state_or_garbage_keeps_the_halt(env, probe):
    _repo, transcript, call = env
    _refused(transcript)
    for raw in (PROBE_DOWN, "not json", json.dumps({"state": "weird"}), None):
        probe["out"] = raw
        assert call()["verdict"] == "rag_corpus_down"


def test_gate_corpus_name_is_real_and_not_a_scrubbable_literal():
    assert gate._CORPUS == REAL
    tree = ast.parse(inspect.getsource(gate))
    docstrings = {
        id(n.body[0].value) for n in ast.walk(tree)
        if isinstance(n, (ast.Module, ast.FunctionDef, ast.ClassDef)) and n.body
        and isinstance(n.body[0], ast.Expr) and isinstance(n.body[0].value, ast.Constant)
    }
    literals = [
        n.value for n in ast.walk(tree)
        if isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in docstrings
    ]
    assert not [s for s in literals if REAL in s or PLACEHOLDER in s]
