
from __future__ import annotations

import uuid

import pytest

from coordinator_core.hooks import preuse_search_dispatch as m


def _ctx(envelope: dict) -> str:
    return (envelope.get("hookSpecificOutput") or {}).get("additionalContext") or ""


@pytest.fixture
def indexed_repo(tmp_path):
    (tmp_path / ".git").mkdir()
    rag = tmp_path / ".project-rag"
    rag.mkdir()
    (rag / "graph.db").write_bytes(b"")
    (tmp_path / "pkg").mkdir()
    return tmp_path


def _payload(repo, tool="Grep", pattern="foo", session_id=None, agent_id=None, path=None):
    tool_input = {"pattern": pattern}
    if path is not None:
        tool_input["path"] = path
    p = {
        "tool_name": tool,
        "tool_input": tool_input,
        "session_id": session_id or str(uuid.uuid4()),
        "cwd": str(repo),
    }
    if agent_id:
        p["agent_id"] = agent_id
    return p


def test_general_fires_once_per_agent(indexed_repo):
    sid = str(uuid.uuid4())
    first = _ctx(m._handler(_payload(indexed_repo, pattern="some text", session_id=sid)))
    assert "this repo has a code index" in first
    assert _ctx(m._handler(_payload(indexed_repo, pattern="other", session_id=sid))) == ""
    sub = _ctx(m._handler(_payload(indexed_repo, pattern="x", session_id=sid, agent_id="a1b2")))
    assert "this repo has a code index" in sub


def test_glob_gets_general_only(indexed_repo):
    out = _ctx(m._handler(_payload(indexed_repo, tool="Glob", pattern="_resolve_registry_key")))
    assert "this repo has a code index" in out
    assert "symbol_name" not in out


@pytest.mark.parametrize(
    "pattern, shape_text",
    [
        ("_resolve_registry_key", 'project_symbol(symbol_name="_resolve_registry_key")'),
        (r"\b_resolve_registry_key\b", 'project_symbol(symbol_name="_resolve_registry_key")'),
        ("resolveRegistryKey", 'project_symbol(symbol_name="resolveRegistryKey")'),
        (r"_resolve_registry_key\(", 'project_symbol_callers(symbol_name="_resolve_registry_key")'),
        ("def _resolve_registry_key", "definition: project_symbol"),
        (r"def\s+_resolve_registry_key", "definition: project_symbol"),
        (r"^class\s+Foo", "definition: project_symbol"),
    ],
)
def test_shape_classifiers(indexed_repo, pattern, shape_text):
    assert shape_text in _ctx(m._handler(_payload(indexed_repo, pattern=pattern)))


@pytest.mark.parametrize("pattern", ["TODO", "import", "error", "foo.*bar", "a_b", "def"])
def test_non_symbol_patterns_get_no_shape(indexed_repo, pattern):
    assert "symbol_name" not in _ctx(m._handler(_payload(indexed_repo, pattern=pattern)))


def test_shape_fires_once_per_agent(indexed_repo):
    sid = str(uuid.uuid4())
    m._handler(_payload(indexed_repo, pattern="_alpha_beta", session_id=sid))
    assert _ctx(m._handler(_payload(indexed_repo, pattern="_gamma_delta", session_id=sid))) == ""
    assert "callers" in _ctx(m._handler(_payload(indexed_repo, pattern=r"gamma_delta\(", session_id=sid)))


def test_search_path_resolves_index(indexed_repo, tmp_path_factory):
    elsewhere = tmp_path_factory.mktemp("unindexed")
    out = _ctx(m._handler(_payload(elsewhere, pattern="x", path=str(indexed_repo / "pkg"))))
    assert "this repo has a code index" in out


def test_unindexed_repo_is_silent(tmp_path):
    (tmp_path / ".git").mkdir()
    (tmp_path / ".project-rag").mkdir()
    assert m._handler(_payload(tmp_path, pattern="_resolve_registry_key")) == m.no_advisory()


def test_state_json_without_graph_db_is_silent(indexed_repo):
    (indexed_repo / ".project-rag" / "state.json").write_text("{}")
    (indexed_repo / ".project-rag" / "graph.db").unlink()
    assert m._handler(_payload(indexed_repo)) == m.no_advisory()


def test_synthetic_session_id_is_silent(indexed_repo):
    assert m._handler(_payload(indexed_repo, session_id="probe")) == m.no_advisory()


def test_other_tools_are_silent(indexed_repo):
    assert m._handler(_payload(indexed_repo, tool="Bash")) == m.no_advisory()


_GUARD_DENY = {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                      "permissionDecisionReason": "[em-prefire-survey] x"}}


@pytest.mark.parametrize("tool", ["Read", "Grep", "Glob"])
def test_guard_deny_returned_before_advisories(indexed_repo, monkeypatch, tool):
    sid = str(uuid.uuid4())
    spent: list = []
    monkeypatch.setattr(m.guard_em_prefire_survey, "evaluate", lambda params: _GUARD_DENY)
    monkeypatch.setattr(m.nudge_em_turn_budget, "advisory_text",
                        lambda params: spent.append(1) or _TURN)
    assert m._handler(_payload(indexed_repo, tool=tool, pattern="some text", session_id=sid)) == _GUARD_DENY
    assert spent == []
    monkeypatch.setattr(m.guard_em_prefire_survey, "evaluate", lambda params: None)
    monkeypatch.setattr(m.nudge_em_turn_budget, "advisory_text", lambda params: "")
    if tool != "Read":
        assert "this repo has a code index" in _ctx(
            m._handler(_payload(indexed_repo, tool=tool, pattern="some text", session_id=sid)))


def test_read_reaches_door_and_gets_no_rag_advice(indexed_repo, monkeypatch):
    monkeypatch.setattr(m.guard_em_prefire_survey, "evaluate", lambda params: None)
    monkeypatch.setattr(m.nudge_em_turn_budget, "advisory_text", lambda params: _TURN)
    out = _ctx(m._handler(_payload(indexed_repo, tool="Read", pattern="_resolve_registry_key")))
    assert _TURN in out and "this repo has a code index" not in out
    monkeypatch.setattr(m.nudge_em_turn_budget, "advisory_text", lambda params: "")
    assert m._handler(_payload(indexed_repo, tool="Read")) == m.no_advisory()


def test_guard_exception_fails_open(indexed_repo, monkeypatch):
    def boom(params):
        raise RuntimeError("x")

    monkeypatch.setattr(m.guard_em_prefire_survey, "evaluate", boom)
    assert "this repo has a code index" in _ctx(m._handler(_payload(indexed_repo)))


_TURN = "[em-turn-budget] test signal"


def test_turn_text_merges_with_example_retrieval_repo_parts(indexed_repo, monkeypatch):
    monkeypatch.setattr(m.nudge_em_turn_budget, "advisory_text", lambda params: _TURN)
    out = _ctx(m._handler(_payload(indexed_repo, pattern="some text")))
    assert "this repo has a code index" in out
    assert out.endswith(_TURN)


def test_turn_text_alone_when_example_retrieval_repo_already_fired(indexed_repo, monkeypatch):
    sid = str(uuid.uuid4())
    m._handler(_payload(indexed_repo, pattern="x", session_id=sid))
    monkeypatch.setattr(m.nudge_em_turn_budget, "advisory_text", lambda params: _TURN)
    out = _ctx(m._handler(_payload(indexed_repo, pattern="y", session_id=sid)))
    assert _TURN in out and "this repo has a code index" not in out


def test_turn_text_signals_in_repo_without_index(tmp_path, monkeypatch):
    (tmp_path / ".git").mkdir()
    monkeypatch.setattr(m.nudge_em_turn_budget, "advisory_text", lambda params: _TURN)
    assert _TURN in _ctx(m._handler(_payload(tmp_path)))


def test_turn_leg_not_reached_for_synthetic_session_or_other_tool(indexed_repo, monkeypatch):
    monkeypatch.setattr(m.nudge_em_turn_budget, "advisory_text", lambda params: _TURN)
    assert m._handler(_payload(indexed_repo, session_id="probe")) == m.no_advisory()
    assert m._handler(_payload(indexed_repo, tool="Bash")) == m.no_advisory()


def test_turn_leg_exception_keeps_example_retrieval_repo_advice(indexed_repo, monkeypatch):
    def boom(params):
        raise RuntimeError("x")

    monkeypatch.setattr(m.nudge_em_turn_budget, "advisory_text", boom)
    assert "this repo has a code index" in _ctx(m._handler(_payload(indexed_repo)))


def test_turn_leg_exception_without_index_is_no_advisory(tmp_path, monkeypatch):
    (tmp_path / ".git").mkdir()

    def boom(params):
        raise RuntimeError("x")

    monkeypatch.setattr(m.nudge_em_turn_budget, "advisory_text", boom)
    assert m._handler(_payload(tmp_path)) == m.no_advisory()


def test_kill_switch(indexed_repo, monkeypatch):
    monkeypatch.setenv(m._KILL_SWITCH, "1")
    assert m._handler(_payload(indexed_repo)) == m.no_advisory()


def test_advice_fits_message_register():
    assert len(m._GENERAL_ADVICE) <= 280
    for shape in m.SHAPES:
        assert len(shape.advice.format(symbol="x" * 40, sidx=" Offline: .structural-index/symbols.ndjson.")) <= 280


@pytest.mark.parametrize(
    "pattern, shape_text",
    [
        (r"^(def|class) ", "File outline: project_code_outline"),
        (r"^\s*def\b", "File outline: project_code_outline"),
        ("^timeout:", 'config key: project_symbol(symbol_name="timeout")'),
        ('"plugin_root":', 'config key: project_symbol(symbol_name="plugin_root")'),
        ("coordinator.dayBranch =", 'config key: project_symbol(symbol_name="coordinator.dayBranch")'),
    ],
)
def test_outline_and_config_key_shapes(indexed_repo, pattern, shape_text):
    assert shape_text in _ctx(m._handler(_payload(indexed_repo, pattern=pattern)))


@pytest.mark.parametrize("pattern", ["TODO:", "Note:", "a:"])
def test_prose_markers_are_not_config_keys(indexed_repo, pattern):
    assert "config key" not in _ctx(m._handler(_payload(indexed_repo, pattern=pattern)))


def test_structural_index_named_only_when_present(indexed_repo):
    assert "symbols.ndjson" not in _ctx(m._handler(_payload(indexed_repo, pattern="def _alpha_beta")))
    sidx = indexed_repo / ".structural-index"
    sidx.mkdir()
    (sidx / "symbols.ndjson").write_text("{}\n", encoding="utf-8")
    assert ".structural-index/symbols.ndjson" in _ctx(m._handler(_payload(indexed_repo, pattern="def _gamma_delta")))
