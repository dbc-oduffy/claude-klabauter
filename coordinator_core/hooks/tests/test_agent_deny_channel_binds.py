"""coordinator_core.hooks.tests.test_agent_deny_channel_binds -- regression
probe pinning the `PreToolUse(Agent)` DENY CHANNEL ITSELF, not the guard's
roster logic.

WHY THIS FILE IS SEPARATE FROM test_block_unenumerated_agent_type.py. That
file (22 tests) covers roster resolution, AC4 named/unnamed pairing, the
AC2 override marker, and AC10 reason shape -- the GUARD's decision logic.
This file covers a narrower, more load-bearing claim: that the HARNESS
CONTRACT this guard depends on -- `PreToolUse(Agent)` firing, its
`hookSpecificOutput.permissionDecision: "deny"` envelope being honored, and
exit 0 (not exit 2) communicating the block -- still holds on this build.
Do not add roster/override/AC10 assertions here; that is the sibling
file's job, and duplicating it here is exactly the drift this docstring
exists to prevent.

**A failure in this file means the HARNESS CONTRACT MOVED, not that the
guard regressed.** The next reader debugging a red run here should look at
the installed Claude Code / harness build's `PreToolUse` handling for the
`Agent` tool -- e.g. whether `permissionDecision: "deny"` stopped being
honored, or exit 0 stopped being the signal for "decision communicated via
envelope" -- not at `block_unenumerated_agent_type.py`'s roster or deny
logic, which is exercised elsewhere.

THE MEASUREMENT THIS PINS (spike verdict,
docs/research/spike-verdicts/2026-08-10-pretooluse-agent-deny-for-
unenumerated-agent-types.md, verdict `viable`, commit `d8b1979ed`): at
`PreToolUse(Agent)`, the hook FIRES for a named dispatch, `additionalContext`
BINDS, `updatedInput` does NOT bind (measured dead, corroborating an earlier
finding that the sibling channel already moved once), and
`permissionDecision: "deny"` DOES bind -- verified live by blocking a real
named dispatch (no spawn, no transcript). The docs are SILENT on Agent-tool
blocking specifically; this file is the durable, offline half of that
measurement (the live half is inherently non-reproducible in a unit-test
run and is not re-attempted here -- see the spike verdict's own "Probe
disposition: throwaway" for why the live probe was never meant to become a
fixture).

This file drives the guard through its REAL stdin/stdout contract
(`main()`), not just `check()` -- the envelope's JSON *serialization* on
stdout and the process *exit code* are themselves part of what is being
pinned; a bug in `main()`'s I/O plumbing that `check()`-only coverage would
miss is exactly the kind of drift this file exists to catch.

Spec backlink: pln-deny-unenumerated-agent-types-e56d1b § C5 / AC8
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Optional

import pytest

import coordinator_core.hooks.block_ungranted_opus_subagent as opus_gate_mod
import coordinator_core.hooks.block_unenumerated_agent_type as mod

# Repo root -- prepended onto PYTHONPATH for the one genuine subprocess spawn
# coordinator_core/tests/test_invoke_main.py::_make_env's _PROJECT_ROOT.
_PROJECT_ROOT = str(Path(__file__).resolve().parents[3])

# real hooks.json registration drives. The spawn ratchet's `_BASELINE` is
pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]


def _run_main_via_stdin(monkeypatch: pytest.MonkeyPatch, payload: dict) -> "tuple[int, str]":
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    captured = io.StringIO()
    monkeypatch.setattr(sys, "stdout", captured)
    exit_code = mod.main()
    return exit_code, captured.getvalue()


def _patch_roster(monkeypatch: pytest.MonkeyPatch, roster, reason: Optional[str] = None) -> None:
    def _fake_resolve_roster(*, doe_root=None, home=None):
        del doe_root, home
        return (roster, reason)

    monkeypatch.setattr(mod, "resolve_roster", _fake_resolve_roster)


def test_deny_channel_binds_exit_zero_with_deny_envelope(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_roster(monkeypatch, frozenset({"coordinator:executor"}))
    payload = {
        "tool_name": "Agent",
        "tool_input": {
            "subagent_type": "hookprobe-channel-pin",
            "name": "channel-pin-probe",
            "prompt": "do the thing",
        },
    }

    exit_code, stdout_text = _run_main_via_stdin(monkeypatch, payload)

    assert exit_code == 0, (
        "deny must be communicated via the hookSpecificOutput envelope, "
        "not via process exit status -- an exit-1/exit-2 regression here "
        "means a harness wiring that only checks exit status would fail "
        "OPEN on this guard"
    )
    envelope = json.loads(stdout_text)
    hook_output = envelope["hookSpecificOutput"]
    assert hook_output["permissionDecision"] == "deny"
    reason = hook_output.get("permissionDecisionReason")
    assert isinstance(reason, str) and reason.strip()


def test_deny_channel_binds_regardless_of_name_presence(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_roster(monkeypatch, frozenset({"coordinator:executor"}))
    unnamed_payload = {
        "tool_name": "Agent",
        "tool_input": {"subagent_type": "hookprobe-channel-pin", "prompt": "do the thing"},
    }
    named_payload = {
        "tool_name": "Agent",
        "tool_input": {
            "subagent_type": "hookprobe-channel-pin",
            "name": "channel-pin-probe",
            "prompt": "do the thing",
        },
    }

    unnamed_exit, unnamed_stdout = _run_main_via_stdin(monkeypatch, unnamed_payload)
    unnamed_decision = json.loads(unnamed_stdout)["hookSpecificOutput"]["permissionDecision"]

    named_exit, named_stdout = _run_main_via_stdin(monkeypatch, named_payload)
    named_decision = json.loads(named_stdout)["hookSpecificOutput"]["permissionDecision"]

    assert unnamed_exit == 0
    assert named_exit == 0
    assert unnamed_decision == "deny"
    assert named_decision == "deny"


def test_enumerated_type_channel_emits_nothing_and_exits_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_roster(monkeypatch, frozenset({"coordinator:executor"}))
    monkeypatch.setattr(opus_gate_mod, "check", lambda payload: None)
    payload = {
        "tool_name": "Agent",
        "tool_input": {"subagent_type": "coordinator:executor", "prompt": "do the thing"},
    }

    exit_code, stdout_text = _run_main_via_stdin(monkeypatch, payload)

    assert exit_code == 0
    assert stdout_text == ""


def test_main_subprocess_contract_exit_and_stdout_shape() -> None:
    payload = {
        "tool_name": "Agent",
        "tool_input": {"subagent_type": "hookprobe-subprocess-channel-pin", "prompt": "do the thing"},
    }
    module_path = Path(mod.__file__)
    env = os.environ.copy()
    existing_pp = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = f"{_PROJECT_ROOT}{os.pathsep}{existing_pp}" if existing_pp else _PROJECT_ROOT
    result = subprocess.run(
        [sys.executable, str(module_path)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )

    assert result.returncode == 0, (
        f"main() must exit 0 regardless of the verdict (stderr: {result.stderr!r})"
    )
    stdout_text = result.stdout.strip()
    if stdout_text:
        envelope = json.loads(stdout_text)
        assert "hookSpecificOutput" in envelope
        assert envelope["hookSpecificOutput"]["permissionDecision"] in ("deny", "allow", "ask")
