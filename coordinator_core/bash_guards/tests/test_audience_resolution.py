"""resolve_audience: one case per tier, plus fail-closed."""

from coordinator_core.bash_guards import _audience
from coordinator_core.guard_messages.alternative_record import Audience

_HEX = "a" * 16


def test_no_agent_id_is_em_without_identity_work(monkeypatch):
    import coordinator_core.write_guards._subagent_identity as ident

    def boom(*a, **k):
        raise AssertionError("EM path must not resolve identity")

    monkeypatch.setattr(ident, "_resolve_subagent_identity", boom)
    assert _audience.resolve_audience({"tool_name": "Bash"}) == (Audience.EM, None)
    assert _audience.resolve_audience({"agent_id": ""}) == (Audience.EM, None)


def test_subagent_prefers_backpointer_type(monkeypatch):
    import coordinator_core.write_guards._subagent_identity as ident

    monkeypatch.setattr(ident, "_read_backpointer_subagent_type", lambda *a, **k: "coordinator:enricher")
    payload = {"agent_id": _HEX, "agent_type": "x", "session_id": "s" * 8, "cwd": "."}
    assert _audience.resolve_audience(payload) == (Audience.SUBAGENT, "coordinator:enricher")


def test_subagent_falls_back_to_payload_agent_type(monkeypatch):
    import coordinator_core.write_guards._subagent_identity as ident

    monkeypatch.setattr(ident, "_read_backpointer_subagent_type", lambda *a, **k: "")
    payload = {"agent_id": _HEX, "agent_type": "coordinator:code-reviewer", "session_id": "s" * 8}
    assert _audience.resolve_audience(payload) == (
        Audience.SUBAGENT,
        "coordinator:code-reviewer",
    )


def test_no_type_anywhere_is_subagent_with_none(monkeypatch):
    import coordinator_core.write_guards._subagent_identity as ident

    monkeypatch.setattr(ident, "_read_backpointer_subagent_type", lambda *a, **k: "")
    assert _audience.resolve_audience({"agent_id": _HEX}) == (Audience.SUBAGENT, None)


def test_unparseable_agent_id_fails_closed_to_subagent():
    assert _audience.resolve_audience({"agent_id": "not an id!", "agent_type": "t"}) == (
        Audience.SUBAGENT,
        None,
    )


def test_resolution_exception_fails_closed(monkeypatch):
    import coordinator_core.write_guards._subagent_identity as ident

    def boom(*a, **k):
        raise OSError("x")

    monkeypatch.setattr(ident, "_read_backpointer_subagent_type", boom)
    assert _audience.resolve_audience({"agent_id": _HEX}) == (Audience.SUBAGENT, None)
