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


def _unmodeled_payload():
    return {
        "tool_name": "Workflow",
        "tool_input": {"script": "agent('do the thing')"},
    }


def _agent_payload():
    return {
        "tool_name": "Agent",
        "tool_input": {"subagent_type": "zzz-not-a-rostered-type", "prompt": "x"},
    }


def _hso(envelope):
    return envelope.get("hookSpecificOutput") or {}


def test_unmodeled_default_rewrites_without_a_decision():
    hso = _hso(bwua._handler(_unmodeled_payload()))
    assert "permissionDecision" not in hso
    assert "model: 'sonnet'" in hso["updatedInput"]["script"]


def test_unmodeled_global_strict_still_never_denies(monkeypatch):
    _set(monkeypatch, _GLOBAL, "strict")
    hso = _hso(bwua._handler(_unmodeled_payload()))
    assert hso.get("permissionDecision") != "deny"
    assert "updatedInput" in hso


def test_agent_leg_default_allows_through_fanin():
    hso = _hso(pad._handler(_agent_payload()))
    assert hso.get("permissionDecision") != "deny"


def test_agent_leg_global_strict_denies_through_fanin(monkeypatch):
    _set(monkeypatch, _GLOBAL, "strict")
    assert _hso(pad._handler(_agent_payload()))["permissionDecision"] == "deny"


def test_per_guard_strict_hardens_one_leg_only(monkeypatch):
    _set(monkeypatch, _GLOBAL, "warn")
    _set(monkeypatch, _per_guard(UNENUMERATED), "strict")
    assert _hso(pad._handler(_agent_payload()))["permissionDecision"] == "deny"
    assert _hso(bwua._handler(_unmodeled_payload())).get("permissionDecision") != "deny"


def test_per_guard_strict_on_workflow_leg_never_denies_and_leaves_agent_leg_warning(monkeypatch):
    _set(monkeypatch, _GLOBAL, "warn")
    _set(monkeypatch, _per_guard(bwua.GUARD_NAME), "strict")
    assert _hso(bwua._handler(_unmodeled_payload())).get("permissionDecision") != "deny"
    assert _hso(pad._handler(_agent_payload())).get("permissionDecision") != "deny"
