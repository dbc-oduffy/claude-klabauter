"""Tests for the R14 unpinned-model advisory leg of `enforce_agent_model_pin`.

Covers: an unpinned, unparam'd dispatch gets the advisory; a pinned type, or
one with a model param, gets none. See that module's "UNPINNED-MODEL
ADVISORY" docstring section.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import coordinator_core.hooks.enforce_agent_model_pin as mod


def _agent_payload(
    subagent_type: str,
    model: str = "",
    effort: str = "",
    *,
    session_id: str = "",
    cwd: str = "",
    prompt: str = "do the thing",
) -> dict:
    tool_input: dict = {"subagent_type": subagent_type, "prompt": prompt}
    if model:
        tool_input["model"] = model
    if effort:
        tool_input["effort"] = effort
    payload: dict = {"tool_name": "Agent", "tool_input": tool_input}
    if session_id:
        payload["session_id"] = session_id
    if cwd:
        payload["cwd"] = cwd
    return payload


def _patch_pins(monkeypatch: pytest.MonkeyPatch, pins, reason=None) -> None:
    def _fake_resolve_model_pins(*, doe_root=None):
        del doe_root
        return (pins, reason)

    monkeypatch.setattr(mod, "resolve_model_pins", _fake_resolve_model_pins)


def _advisory_text(envelope: dict) -> str:
    return envelope["hookSpecificOutput"]["additionalContext"]


def test_unpinned_unparam_dispatch_gets_the_advisory(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_pins(monkeypatch, {"coordinator:executor": {"model": "sonnet", "_source_path": "x"}})

    envelope = mod.check(_agent_payload("general-purpose"))

    assert envelope is not None
    assert envelope["hookSpecificOutput"]["permissionDecision"] == "allow"
    reason = _advisory_text(envelope)
    assert "general-purpose" in reason
    assert "model: sonnet" in reason
    assert "no model pin" in reason.lower() or "no `model`" in reason.lower()


def test_pinned_type_unparam_dispatch_gets_no_advisory(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_pins(monkeypatch, {"coordinator:executor": {"model": "sonnet", "_source_path": "x"}})

    assert mod.check(_agent_payload("coordinator:executor")) is None


def test_unpinned_type_with_model_param_gets_no_advisory(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_pins(monkeypatch, {"coordinator:executor": {"model": "sonnet", "_source_path": "x"}})

    assert mod.check(_agent_payload("general-purpose", model="opus")) is None


def test_unpinned_type_effort_only_no_model_still_gets_advisory(monkeypatch: pytest.MonkeyPatch) -> None:
    """Omitting `model` alone is enough -- an `effort` param does not suppress it."""
    _patch_pins(monkeypatch, {"coordinator:executor": {"model": "sonnet", "_source_path": "x"}})

    envelope = mod.check(_agent_payload("general-purpose", effort="high"))

    assert envelope is not None
    assert envelope["hookSpecificOutput"]["permissionDecision"] == "allow"


def test_pin_resolution_failure_yields_no_advisory_not_a_deny(monkeypatch: pytest.MonkeyPatch) -> None:
    """Nothing was passed, so the fail-closed deny leg (pins is None) never
    fires here -- the early return preserves that pre-existing contract."""
    _patch_pins(monkeypatch, None, reason="model-pin roster unresolved")

    assert mod.check(_agent_payload("general-purpose")) is None


def test_fork_type_unparam_gets_no_advisory(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_pins(monkeypatch, {})

    assert mod.check(_agent_payload("fork")) is None


def test_override_env_suppresses_the_advisory(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_pins(monkeypatch, {})
    monkeypatch.setenv(mod._OVERRIDE_ENV, "1")

    assert mod.check(_agent_payload("general-purpose")) is None


def test_fires_once_per_session_and_subagent_type(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_pins(monkeypatch, {})
    (tmp_path / ".git").mkdir()
    session_id = "11111111-2222-3333-4444-555555555555"

    first = mod.check(_agent_payload("general-purpose", session_id=session_id, cwd=str(tmp_path)))
    second = mod.check(_agent_payload("general-purpose", session_id=session_id, cwd=str(tmp_path)))

    assert first is not None
    assert first["hookSpecificOutput"]["permissionDecision"] == "allow"
    assert second is None


def test_different_subagent_type_in_same_session_fires_independently(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_pins(monkeypatch, {})
    (tmp_path / ".git").mkdir()
    session_id = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"

    first = mod.check(_agent_payload("general-purpose", session_id=session_id, cwd=str(tmp_path)))
    other_type = mod.check(_agent_payload("Explore", session_id=session_id, cwd=str(tmp_path)))

    assert first is not None
    assert other_type is not None


def test_no_real_session_id_fires_every_time(monkeypatch: pytest.MonkeyPatch) -> None:
    """Dedup is best-effort plumbing (see module docstring) -- an absent or
    non-uuid session id never suppresses the advisory, it just skips dedup."""
    _patch_pins(monkeypatch, {})

    first = mod.check(_agent_payload("general-purpose"))
    second = mod.check(_agent_payload("general-purpose"))

    assert first is not None
    assert second is not None
