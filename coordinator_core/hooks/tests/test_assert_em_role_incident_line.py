"""assert_em_role incident-claim banner line: absent with zero holders, capped at 3 keys, advisory on failure."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from coordinator_core.hooks import assert_em_role as mod
from coordinator_core.session import incident_claims


@pytest.fixture
def env(tmp_path, monkeypatch):
    (tmp_path / ".git").mkdir()
    monkeypatch.delenv("CLAUDE_PLUGIN_ROOT", raising=False)
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "cfg"))
    return tmp_path


def _text(result) -> str:
    return result["hookSpecificOutput"]["additionalContext"]


def _run() -> str:
    return _text(mod._handler({"payload": {"session_id": "s-self"}}))


def _holders(*pairs):
    return [SimpleNamespace(key=k, session_id=s) for k, s in pairs]


def test_zero_holders_is_byte_identical_to_pre_change(env, monkeypatch):
    monkeypatch.setattr(incident_claims, "list_peers", lambda *_a, **_k: [])
    baseline = _run()
    monkeypatch.setattr(mod, "_incident_claims_line", lambda _r: "")
    assert baseline == _run()
    assert "incident claim" not in baseline


def test_one_line_names_key_and_session_count(env, monkeypatch):
    monkeypatch.setattr(
        incident_claims, "list_peers",
        lambda *_a, **_k: _holders(("outage-a", "s1"), ("outage-a", "s2")),
    )
    out = _run()
    assert out.count("live incident claim(s)") == 1
    assert "outage-a (2 session(s))" in out
    assert "incident-peers <key>" in out


def test_caps_at_three_keys_then_more(env, monkeypatch):
    holders = _holders(*[(f"k{i}", f"s{i}") for i in range(5)])
    monkeypatch.setattr(incident_claims, "list_peers", lambda *_a, **_k: holders)
    out = _run()
    assert "k0 (1 session(s)), k1 (1 session(s)), k2 (1 session(s)), +2 more" in out
    assert "k3" not in out


def test_failure_never_blocks_session_start(env, monkeypatch):
    def boom(*_a, **_k):
        raise RuntimeError("x")

    monkeypatch.setattr(incident_claims, "list_peers", boom)
    assert "incident claim" not in _run()
