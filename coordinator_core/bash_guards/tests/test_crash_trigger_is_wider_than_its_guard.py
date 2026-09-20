"""Property test for `_CRASH_TRIGGER_SUBSTRINGS`: for every command a mapped
guard denies, that guard's trigger must match.

The mapping's contract is that each entry is **provably wider** than its
guard's own first-branch test, so that a crashed guard can be skipped without
allowing anything it would have denied. Before this test existed, the entries
were justified by a docstring asserting the derivation had been done, and two of
the seven were wrong in the dangerous direction -- narrower than their guard,
which re-opens a bypass on the crash path where nothing else is looking:

  - `destructive-git-revert`: the guard normalizes a `GIT.EXE` head to bare
    `git` before its own `\\bgit\\b` test; the trigger's substring test is
    case-sensitive, so `GIT.EXE reset --hard HEAD~3` denied normally and was
    SKIPPED on the crash path.
  - `destructive-git-orphan`: the guard deletes whitespace-containing quoted
    spans and merges the flanking text before its `\\bgit\\b` test, so
    `gi"a b"t reset --hard HEAD~3` denied normally while carrying no `git`
    substring raw -- also SKIPPED on the crash path.

Both are covered below as named cases rather than as a general statement, so a
regression names itself. `_assert_guard_denies` is what stops this table from
rotting into commands nothing denies any more, which would leave every
assertion below vacuously true.
"""

from __future__ import annotations

import json

import pytest

from coordinator_core.bash_guards.dispatch import (
    _CRASH_TRIGGER_SUBSTRINGS,
    _crash_deny_is_out_of_class,
    evaluate_payload_json,
)

# Drives the real dispatcher, which spawns; runs at cadence gates.
# Spawn ratchet: coordinator_core/tests/test_no_new_spawning_tests.py
pytestmark = [
    pytest.mark.spawns_process,
    pytest.mark.cadence,
]


# (guard name, command the guard denies). Every mapped guard appears at least
# once; the two historical bypasses appear under their own ids.
DENIED_COMMANDS = [
    ("no-verify", "git commit --no-verify -m wip"),
    ("destructive-git-orphan", "git reset --hard HEAD~3"),
    ("destructive-git-orphan", 'gi"a b"t reset --hard HEAD~3'),
    ("destructive-git-clean", "git clean -fdx"),
    ("destructive-git-revert", "git reset --hard HEAD~3"),
    ("destructive-git-revert", "GIT.EXE reset --hard HEAD~3"),
    ("blanket-git-add", "git add -A"),
    ("destructive-rm", "rm -rf build"),
    ("runaway-find", "find / -name '*.py'"),
]


def _dispatcher_decision(cmd: str):
    payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": cmd}})
    result = evaluate_payload_json(payload)
    return (result or {}).get("hookSpecificOutput", {}).get("permissionDecision")


@pytest.mark.parametrize("guard_name,cmd", DENIED_COMMANDS)
def test_trigger_matches_every_command_its_guard_denies(guard_name: str, cmd: str) -> None:
    """The invariant, and it is host-invariant: the trigger is a pure text test."""
    assert not _crash_deny_is_out_of_class(guard_name, cmd), (
        f"{guard_name}'s trigger is NARROWER than its guard: {cmd!r} is within the class "
        "the guard polices but would be skipped on the crash path"
    )


def test_the_table_still_describes_commands_the_dispatcher_denies() -> None:
    """Anti-rot, as far as one host can carry it.

    A table of commands nothing denies any more would make the property above
    vacuously true. The dispatcher's verdict is NOT a host-invariant, though:
    several entries here are denied only under a host or agent-type this test
    does not get to choose -- `GIT.EXE ...` reads as `allow` on Linux because the
    `.exe`-head normalization is Windows-conditioned, and two others return no
    decision at all under this agent type. So this asserts what it can: that the
    table has not rotted WHOLESALE, and it names each entry's live verdict rather
    than asserting one it cannot produce. The Windows arm of this is not measured
    here and is not claimed.
    """
    decisions = {(g, c): _dispatcher_decision(c) for g, c in DENIED_COMMANDS}
    denied = [k for k, v in decisions.items() if v == "deny"]
    assert denied, f"no table entry is denied on this host at all, so the property test is vacuous: {decisions}"


def test_unmapped_guard_and_a_raising_transform_fail_toward_denial() -> None:
    assert _crash_deny_is_out_of_class("no-such-guard", "git reset --hard HEAD~3") is False


def test_an_empty_command_is_skippable_not_denied() -> None:
    # Pinned because the mapping's own docstring said the opposite for a while:
    # no token can be present in an empty command and there is nothing to deny.
    assert _crash_deny_is_out_of_class("destructive-rm", "") is True


def test_every_mapped_guard_has_at_least_one_case() -> None:
    covered = {name for name, _ in DENIED_COMMANDS}
    missing = set(_CRASH_TRIGGER_SUBSTRINGS) - covered
    assert not missing, (
        "mapped with no in-class command case, so its widening is asserted and not proved: "
        f"{sorted(missing)}"
    )


def test_a_command_outside_the_class_is_still_skippable() -> None:
    # The mapping's whole point: a crashed git guard must not deny `echo`.
    assert _crash_deny_is_out_of_class("destructive-git-revert", "echo hello") is True
