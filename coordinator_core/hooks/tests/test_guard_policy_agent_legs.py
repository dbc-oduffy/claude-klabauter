"""Agent and workflow PreToolUse gates resolve their deny at the guard-level policy point."""

import pytest

from coordinator_core.hooks import block_workflow_unmodeled_agent as bwua
from coordinator_core.hooks import preuse_agent_dispatch as pad
from coordinator_core.hooks.block_unenumerated_agent_type import (
    GUARD_NAME as UNENUMERATED,
)

_GLOBAL = "MACHINE_LOCAL_COORDINATOR_GUARD_LEVEL"


def _per_guard(name):
    return _GLOBAL + "_" + name.upper()


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    import os

    from coordinator_core import machine_profile

    for key in [k for k in os.environ if k.startswith("MACHINE_LOCAL_COORDINATOR_")]:
        monkeypatch.delenv(key)
    reg = tmp_path / "reg"
    reg.mkdir()
    (reg / "registry.toml").write_text("", encoding="utf-8")
    monkeypatch.setenv("MACHINE_LOCAL_REGISTRY_DIR", str(reg))
    machine_profile.reset_cache()
    yield
    machine_profile.reset_cache()


def _set(monkeypatch, name, level):
    monkeypatch.setenv(name, level)


def _unmodeled_payload(tmp_path):
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text('{"model":"claude-opus-4"}\n', encoding="utf-8")
    return {
        "tool_name": "Workflow",
        "tool_input": {"script": "agent('do the thing')"},
        "transcript_path": str(transcript),
    }


def _agent_payload():
    return {
        "tool_name": "Agent",
        "tool_input": {"subagent_type": "zzz-not-a-rostered-type", "prompt": "x"},
    }


def _hso(envelope):
    return envelope.get("hookSpecificOutput") or {}


def test_unmodeled_default_is_allow_with_context(tmp_path, monkeypatch):
    hso = _hso(bwua._handler(_unmodeled_payload(tmp_path)))
    assert hso["permissionDecision"] == "allow"
    assert "coordinator.guard_level.block-workflow-unmodeled-agent off" in hso["additionalContext"]


def test_unmodeled_global_strict_denies(tmp_path, monkeypatch):
    _set(monkeypatch, _GLOBAL, "strict")
    assert _hso(bwua._handler(_unmodeled_payload(tmp_path)))["permissionDecision"] == "deny"


def test_agent_leg_default_allows_through_fanin():
    hso = _hso(pad._handler(_agent_payload()))
    assert hso.get("permissionDecision") != "deny"


def test_agent_leg_global_strict_denies_through_fanin(monkeypatch):
    _set(monkeypatch, _GLOBAL, "strict")
    assert _hso(pad._handler(_agent_payload()))["permissionDecision"] == "deny"


def test_per_guard_strict_hardens_one_leg_only(tmp_path, monkeypatch):
    _set(monkeypatch, _GLOBAL, "warn")
    _set(monkeypatch, _per_guard(UNENUMERATED), "strict")
    assert _hso(pad._handler(_agent_payload()))["permissionDecision"] == "deny"
    hso = _hso(bwua._handler(_unmodeled_payload(tmp_path)))
    assert hso["permissionDecision"] == "allow"


def test_per_guard_strict_on_workflow_leg_leaves_agent_leg_warning(tmp_path, monkeypatch):
    _set(monkeypatch, _GLOBAL, "warn")
    _set(monkeypatch, _per_guard(bwua.GUARD_NAME), "strict")
    assert _hso(bwua._handler(_unmodeled_payload(tmp_path)))["permissionDecision"] == "deny"
    assert _hso(pad._handler(_agent_payload())).get("permissionDecision") != "deny"
