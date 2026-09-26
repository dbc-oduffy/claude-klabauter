"""C8 of `docs/plans/2026-08-19-the-held-guard-cohort-becomes-dialect-safe.md`
-- AC7, AC9: observed-block evidence for the four held-cohort guards
(`block_stash_destruction`, `block_subagent_stash_creation`,
`block_worktree_creation`, `block_subagent_destructive_action`) now that
C4-C7 have widened their `MATCHERS` onto the PowerShell leg.

AC7. A green suite is NOT evidence for this AC: every guard's own test file
calls `check()` directly, bypassing the harness matcher, the master gate, and
the chain-entry skip. This module goes through the REAL entrypoint,
`coordinator_core.bash_guards.dispatch.evaluate_payload_json`, exactly like
`test_ac5_bash_flip_runtime_probes.py` (reused pattern, not reinvented) --
the same seam a real PreToolUse(PowerShell) hook call runs through.

SAFETY. The danger this module exists to guard against is the NEGATIVE
outcome: if a guard fails to fire, the probe command in `_PROBES` below
RUNS, for real, on a shared tree with 50-70 concurrent sessions. Every probe
is therefore chosen to be INERT even if the guard fails to block it -- see
each entry's own one-line rationale in `_PROBES`. None of them is a bare
`git stash clear`/`git worktree add <fresh-path>` (both would succeed and
mutate the tree if unblocked); each instead targets a nonexistent stash
entry, a nonexistent pathspec, an already-occupied worktree target, or a
nonexistent branch -- shapes git itself refuses before any mutation happens.

AC9. With `tree_sitter_pwsh` unimportable, the cohort's PowerShell leg must
NOT deny a BENIGN command that merely mentions the guard's own trigger word
(`stash`/`worktree`) -- Convention (a) routes a `tokens is None` parse
failure to C2's PowerShell-shaped scanner, which still DENIES on a HIT (by
PM ruling), so this AC is proven on a command that is a miss for the
scanner's own subcommand classification, not on a parse failure alone.
Simulated via the same `_dialect._parser` monkeypatch technique already
established in `test_command_tokenizer_length_ceiling.py` (`test_import_
error_falls_back_to_silent` et al.) -- the identical code path
`_dialect.probe_armed` exercises in a child process (its own docstring:
"Runs `_powershell_tokens` -- the SAME code path a real PowerShell Bash call
takes"). A direct `probe_armed` call under the CURRENT interpreter is also
exercised below to confirm the harness itself reports ARMED here (this repo
ships `tree_sitter_pwsh`), pinning the assumption the in-process simulation
stands in for.

Spec backlink: pln-the-held-guard-cohort-becomes-a94c56, AC7, AC9.
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any, Callable, Dict

import pytest

from coordinator_core.bash_guards import _dialect
from coordinator_core.bash_guards import dispatch


def _payload_dict(command: str, **extra: Any) -> Dict[str, Any]:
    p: Dict[str, Any] = {
        "tool_name": "PowerShell",
        "tool_input": {"command": command},
        "session_id": "c8-observed-block",
        "cwd": os.getcwd(),
    }
    p.update(extra)
    return p


def _evaluate(payload_dict: Dict[str, Any]) -> Any:
    return dispatch.evaluate_payload_json(json.dumps(payload_dict))


def _assert_denied(out: Any) -> None:
    assert out is not None, (
        "expected a hard-deny envelope through the real dispatch chain; got "
        "ALLOW (None) -- the observed-block AC7 evidence this module exists "
        "to capture is missing"
    )
    hso = out["hookSpecificOutput"]
    assert hso.get("permissionDecision") == "deny", (
        "expected permissionDecision == 'deny', got %r" % (out,)
    )


def _assert_not_denied(out: Any) -> None:
    if out is None:
        return
    hso = out.get("hookSpecificOutput", {})
    assert hso.get("permissionDecision") != "deny", (
        "AC9: disarmed PowerShell leg must not deny a benign command that "
        "merely mentions the guard's trigger word -- got a deny envelope: "
        "%r" % (out,)
    )


_PROBES: Dict[str, Callable[[], Dict[str, Any]]] = {
    "block_stash_destruction": lambda: _payload_dict(
        "git stash drop 'stash@{999}'"
    ),
    "block_subagent_stash_creation": lambda: _payload_dict(
        "git stash push -- nonexistent-inert-probe-path-xyz",
        agent_id="deadbeef0123",
    ),
    "block_worktree_creation": lambda: _payload_dict(
        "git worktree add docs some-branch"
    ),
    "block_subagent_destructive_action": lambda: _payload_dict(
        "git branch -D nonexistent-branch-inert-probe-xyz",
        agent_id="deadbeef0123",
        agent_type="coordinator:executor",
    ),
}

_BENIGN: Dict[str, Callable[[], Dict[str, Any]]] = {
    # "list" is not in `_DENY_SUBCOMMANDS` -- allowed under both the
    "block_stash_destruction": lambda: _payload_dict("git stash list"),
    "block_subagent_stash_creation": lambda: _payload_dict(
        "git stash list", agent_id="deadbeef0123"
    ),
    # "list" is in `_ALLOW_SUBCOMMANDS` explicitly (the cleanup-reachability
    "block_worktree_creation": lambda: _payload_dict("git worktree list"),
    "block_subagent_destructive_action": lambda: _payload_dict(
        "git status", agent_id="deadbeef0123", agent_type="coordinator:executor"
    ),
}


class TestObservedBlocksAC7:

    @pytest.mark.parametrize("guard_name", sorted(_PROBES))
    def test_powershell_probe_denies_through_dispatch(self, guard_name: str) -> None:
        payload = _PROBES[guard_name]()
        out = _evaluate(payload)
        _assert_denied(out)


class TestDisarmedLegDoesNotDenyBenignAC9:

    @pytest.fixture(autouse=True)
    def _disarm_parser(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _boom():
            raise ImportError("planted fixture: tree_sitter_pwsh absent")

        monkeypatch.setattr(_dialect, "_parser", _boom)
        monkeypatch.setattr(_dialect, "_parser_cache", None)

    @pytest.mark.parametrize("guard_name", sorted(_BENIGN))
    def test_disarmed_benign_command_does_not_deny(self, guard_name: str) -> None:
        payload = _BENIGN[guard_name]()
        out = _evaluate(payload)
        _assert_not_denied(out)


class TestProbeArmedReportsArmedUnderThisInterpreter:

    def test_probe_armed_under_current_interpreter(self) -> None:
        armed, detail = _dialect.probe_armed(sys.executable, os.getcwd())
        assert armed is True, (
            "expected the current interpreter to report ARMED (tree_sitter_"
            "pwsh is a real dependency of this repo) -- got disarmed: %s"
            % (detail,)
        )
