
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
    assert "example-retrieval-repo indexes this repo" in first
    assert _ctx(m._handler(_payload(indexed_repo, pattern="other", session_id=sid))) == ""
    sub = _ctx(m._handler(_payload(indexed_repo, pattern="x", session_id=sid, agent_id="a1b2")))
    assert "example-retrieval-repo indexes this repo" in sub


def test_glob_gets_general_only(indexed_repo):
    out = _ctx(m._handler(_payload(indexed_repo, tool="Glob", pattern="_resolve_registry_key")))
    assert "example-retrieval-repo indexes this repo" in out
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
    assert "example-retrieval-repo indexes this repo" in out


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
    assert m._handler(_payload(indexed_repo, tool="Read")) == m.no_advisory()


def test_kill_switch(indexed_repo, monkeypatch):
    monkeypatch.setenv(m._KILL_SWITCH, "1")
    assert m._handler(_payload(indexed_repo)) == m.no_advisory()


def test_advice_fits_message_register():
    assert len(m._GENERAL_ADVICE) <= 280
    for shape in m.SHAPES:
        assert len(shape.advice.format(symbol="x" * 40)) <= 280
