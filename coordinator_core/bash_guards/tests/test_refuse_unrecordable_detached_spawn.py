"""refuse_unrecordable_detached_spawn: deny a detached launch with a non-literal command, pass literal ones.

In-process, no spawn.
"""

from __future__ import annotations

import json

import pytest

from coordinator_core import deny_registry
from coordinator_core.bash_guards import refuse_unrecordable_detached_spawn as guard
from coordinator_core.guard_messages.testing import envelope_of


def verdict(command, tool="Bash"):
    payload = {"tool_name": tool, "tool_input": {"command": command}, "cwd": "."}
    out = envelope_of(guard.check(payload))
    if out is None:
        return None
    spec = out["hookSpecificOutput"]
    assert spec["permissionDecision"] == "deny"
    return spec["permissionDecisionReason"]


@pytest.mark.parametrize("command,tool", [
    ("nohup $CMD &", "Bash"),
    ("nohup $(make_cmd) > out.log 2>&1 &", "Bash"),
    ("setsid $RUNNER --flag", "Bash"),
    ("eval 'sleep 100' &", "Bash"),
    ("$CMD &", "Bash"),
    ('nohup bash -c "$PAYLOAD" &', "Bash"),
    ("Start-Process $exe -ArgumentList 'x'", "PowerShell"),
    ("Start-Process -FilePath $exe", "PowerShell"),
])
def test_opaque_detach_denies(command, tool):
    reason = verdict(command, tool)
    assert reason
    assert guard.ANCHOR in reason
    assert "proc.stop_own_child" in reason


@pytest.mark.parametrize("command,tool", [
    ("nohup python scratch/job.py > out.log 2>&1 &", "Bash"),
    ("sleep 100 &", "Bash"),
    ("setsid python job.py", "Bash"),
    ("Start-Process python -ArgumentList 'job.py'", "PowerShell"),
    ("Start-Process -FilePath python", "PowerShell"),
    ("nohup bash -c 'sleep 5' &", "Bash"),
])
def test_literal_detach_passes(command, tool):
    assert verdict(command, tool) is None


@pytest.mark.parametrize("command", [
    "ls -la",
    "git status && git diff",
    "echo $CMD",
    "$CMD --flag",
    "eval 'true'",
])
def test_non_detach_text_passes(command):
    assert verdict(command) is None


def test_prefilter_skips_text_without_a_detach_word(monkeypatch):
    def boom(_command):
        raise AssertionError("detach_shape reached for non-detach text")

    monkeypatch.setattr("coordinator_core.ops._spawn_ledger.detach_shape", boom)
    assert guard.check({"tool_name": "Bash", "tool_input": {"command": "echo $CMD"}}) is None


def test_internal_error_allows(monkeypatch):
    def boom(_command):
        raise RuntimeError("classifier broke")

    monkeypatch.setattr("coordinator_core.ops._spawn_ledger.detach_shape", boom)
    assert guard.check({"tool_name": "Bash", "tool_input": {"command": "nohup $CMD &"}}) is None


def test_other_tools_and_malformed_payloads_pass():
    assert guard.check({"tool_name": "Read", "tool_input": {"command": "nohup $CMD &"}}) is None
    assert guard.check({"tool_name": "Bash", "tool_input": None}) is None
    assert guard.check({"tool_name": "Bash", "tool_input": {"command": 7}}) is None


def test_registered_as_box_degrading_and_wired_in_the_cold_chain():
    from coordinator_core.host_hooks import cold_chain

    assert deny_registry.GUARD_DENY_CLASS[guard.GUARD_NAME] == deny_registry.BOX_DEGRADING
    assert guard.GUARD_NAME in cold_chain.shell_candidates("nohup $CMD &")
    assert guard.GUARD_NAME not in cold_chain.shell_candidates("ls -la")


def _dispatch(command, tmp_path):
    from coordinator_core.bash_guards import dispatch

    payload = {
        "tool_name": "Bash",
        "tool_input": {"command": command},
        "session_id": "sess-unrecordable-detach",
        "tool_use_id": "tu-unrecordable-detach",
        "cwd": str(tmp_path),
    }
    return dispatch.evaluate_payload_json(json.dumps(payload))


def test_dispatcher_denies_an_opaque_detach_and_passes_a_literal_one(tmp_path):
    out = _dispatch("nohup $CMD > out.log 2>&1 &", tmp_path)
    spec = (out or {}).get("hookSpecificOutput", {})
    assert spec.get("permissionDecision") == "deny"
    assert f"`{guard.GUARD_NAME}`" in spec.get("permissionDecisionReason", "")

    out = _dispatch("sleep 1 &", tmp_path)
    assert (out or {}).get("hookSpecificOutput", {}).get("permissionDecision") != "deny"
