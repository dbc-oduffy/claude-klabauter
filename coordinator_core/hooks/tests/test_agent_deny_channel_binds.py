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

import coordinator_core.hooks.block_unenumerated_agent_type as mod

# Repo root -- prepended onto PYTHONPATH for the one genuine subprocess spawn
# below so `coordinator_core.hooks.block_unenumerated_agent_type`'s own
# module-level `from coordinator_core._hook_envelope import deny` resolves
# regardless of the spawned child's cwd. Same shape as
# coordinator_core/tests/test_invoke_main.py::_make_env's _PROJECT_ROOT.
_PROJECT_ROOT = str(Path(__file__).resolve().parents[3])

# test_main_subprocess_contract_exit_and_stdout_shape needs one genuine OS-
# level subprocess run of main()'s real stdin/stdout/exit-code boundary
# (per module docstring) -- the in-process monkeypatch runs elsewhere in
# this file cannot observe the actual process exit-code/stdout channel a
# real hooks.json registration drives. The spawn ratchet's `_BASELINE` is
# shrink-only pre-existing residue and is explicitly not the route for this
# file -- coordinator_core/tests/test_no_new_spawning_tests.py Rule 2.
pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]


def _run_main_via_stdin(monkeypatch: pytest.MonkeyPatch, payload: dict) -> "tuple[int, str]":
    """Drive `main()` in-process through its real stdin/stdout contract --
    `sys.stdin.read()` in, `sys.stdout.write()` out, `SystemExit` carrying
    the process exit code -- rather than calling `check()` directly. Kept
    in-process (not a real subprocess) so the roster can still be injected
    via monkeypatch; the subprocess-level exit-code/stdout shape is
    equivalent for what this test pins (envelope JSON + exit code), and an
    in-process run keeps this file fast and independent of `python`
    resolution on the host.
    """
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
    """THE PIN. An unenumerated `subagent_type` dispatched through the real
    `main()` stdin/stdout contract must (a) exit 0 -- a deny is
    communicated via the envelope, never via process exit status, and an
    exit-1/exit-2 regression here would fail OPEN on any harness wiring
    that only checks exit status -- and (b) emit
    `hookSpecificOutput.permissionDecision == "deny"` with a non-empty
    `permissionDecisionReason` on stdout, valid JSON.
    """
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
    """The channel itself -- not just the guard's own decision logic (that
    is AC4, pinned in the sibling file) -- must bind identically whether or
    not `name` is present on the wire. Kept here, not merely duplicated
    from the sibling file, because this asserts it through the real
    `main()` I/O path rather than `check()`.
    """
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
    """The complementary shape of the same channel pin: an ALLOWED
    dispatch must produce no stdout envelope at all (silence, not an
    explicit `permissionDecision: allow`) and still exit 0 -- the harness
    contract this guard relies on for the allow path, not merely the deny
    path.
    """
    _patch_roster(monkeypatch, frozenset({"coordinator:executor"}))
    payload = {
        "tool_name": "Agent",
        "tool_input": {"subagent_type": "coordinator:executor", "prompt": "do the thing"},
    }

    exit_code, stdout_text = _run_main_via_stdin(monkeypatch, payload)

    assert exit_code == 0
    assert stdout_text == ""


def test_main_subprocess_contract_exit_and_stdout_shape() -> None:
    """One genuine OS-level subprocess run of `main()`'s `if __name__ ==
    "__main__"` entrypoint -- the actual `stdin -> stdout, exit 0`
    boundary a real `hooks.json` registration drives, not merely the
    in-process `sys.stdin`/`sys.stdout` monkeypatch used above. Resolves
    roster for real (no injection possible across a process boundary), so
    it asserts only the CHANNEL shape -- exit 0 and a well-formed JSON
    object with `hookSpecificOutput.permissionDecision` when non-silent,
    or empty stdout when silent -- never a specific deny/allow verdict,
    since that depends on this host's real coordinator-claude/plugin state.
    """
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
