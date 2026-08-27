"""AC4 (docs/plans/2026-08-23-no-hook-fire-pays-an-interpreter-start.md).

AC4's own wording: "On a box with warm disabled, no resident server, or no door, a
hook fire produces byte-identical on-disk effects to today's cold path, and the
guard's exit code is preserved (a deny still denies)."

The claude-klabauter-side entry point this AC governs is `warm/entry_seam.py ::
try_warm_guard_dispatch` -- the only production seam that attempts a warm dispatch
for a guard-shaped call before a caller falls through to its own cold path
(`bash_guards.dispatch.evaluate_payload_json`, the same chain
`ops/warm_guard_evaluate.py` wraps). The three arms map onto `warm.client
._try_warm_dispatch_inner`'s own documented cases:

    - warm disabled -> `warm.settings.is_warm_enabled()` resolves False.
    - no resident server -> `client._open_pipe` raises `ConnectionRefusedError`
      -- per that module's own comment, a unix socket's path OUTLIVES the
      process that bound it, so a hard-killed server leaves a corpse file that
      EXISTS and refuses; a Windows named pipe with nobody behind it answers
      the same way. The door is present; nobody is behind it.
    - no door -> `client._open_pipe` raises `FileNotFoundError` -- no endpoint
      was ever created at all (ENOENT).

`test_entry_seam.py` (excluded from this workstream, landed by a concurrent
session) already pins "warm off" and "no door" returning
`WarmGuardOutcome(hit=False, response=None)` as bare facts about that function.
This file does not re-derive those two return-value assertions; what it adds,
for all THREE arms including the "no resident server" one neither existing
suite covers, is the AC4 CLAUSE those tests stop short of: that the fail-open
outcome is truly indistinguishable from the cold path ever having been consulted
at all.

WHAT "BYTE-IDENTICAL ON-DISK EFFECTS" IS ASSERTED AGAINST, STATED PLAINLY. A
guard verdict itself writes nothing to disk on any path (`evaluate_payload_json`
does not accept or need write authority to decide a permission), so the
disk-effects clause is not "diff two artifacts" -- there is no artifact to diff
on either path (cold path or warm-off path) at this layer, and asserting
otherwise would fabricate a comparison neither path performs. What IS a real,
measurable disk hazard, and what a warm-attempt-that-should-be-a-no-op could
introduce that the pre-warm-engine cold path never did, is the RESPAWN side
effect `client._open_pipe`'s failure branch triggers (`_spawn_once`, module
docstring: "Best-effort spawn on the FileNotFoundError trigger"). Every test
below patches `client._live_tree_cold = True` (the same short-circuit
`_spawn_once` checks BEFORE computing an engine root or touching disk at all)
and additionally spies on `client.spawn_detached`, asserting it is never
called -- pinning that the fail-open arms leave the filesystem exactly as
untouched as the cold path always has, rather than merely asserting that a
spawn WOULD have been debounced.

THE CLAUSE THIS FILE COULD NOT REACH, STATED PLAINLY. "The guard's exit code is
preserved" is a fact about the REAL hook script's process exit code
(`preuse-bash-dispatch.py`), which lives in DoE-claude's repo, not this one --
this repo owns `try_warm_guard_dispatch` and the guard chain it falls back to,
not the cold script that turns a verdict into `sys.exit()`. What this file
verifies instead, as the claude-klabauter-side half of that guarantee: `try_warm_guard_
dispatch` returns a genuine miss (`hit=False`, `response=None`) on all three
arms -- never a fabricated verdict of any kind -- so whatever exit-code
translation the caller's cold path already performs runs completely unmodified,
against the SAME `evaluate_payload_json` verdict this file drives directly and
asserts denies. The exit-code mapping itself is not exercised here because the
process that performs it is outside this repo's `writes:`.
"""

from __future__ import annotations

import json

import pytest

from coordinator_core.bash_guards.dispatch import evaluate_payload_json
from coordinator_core.warm import client, settings
from coordinator_core.warm.entry_seam import WarmGuardOutcome, try_warm_guard_dispatch

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

#: Same fixture command `ops/tests/test_warm_guard_evaluate.py` uses: needs no git
#: repository, no filesystem target, no network, and denies unconditionally via
#: `check_no_verify` with no override present -- the smallest payload that proves
#: "a deny still denies" without depending on this suite's own working-tree state.
_DENY_CMD = "git commit --no-verify -m x -- foo.py"


def _deny_event() -> dict:
    return {
        "hook_event_name": "PreToolUse",
        "session_id": "s-ac4-parity",
        "cwd": "C:/Windows/Temp",
        "tool_name": "Bash",
        "tool_input": {"command": _DENY_CMD},
        "env": {},
    }


def _assert_cold_path_still_denies() -> None:
    """The cold path this AC's parity clause is measured against, driven directly
    (never through `try_warm_guard_dispatch`, which the arms below have already
    proven returns no verdict of its own): a caller that received `hit=False`
    falls through to exactly this call, unmodified."""
    out = evaluate_payload_json(json.dumps(_deny_event()))
    assert isinstance(out, dict)
    hso = out.get("hookSpecificOutput")
    assert isinstance(hso, dict)
    assert hso.get("permissionDecision") == "deny"


def _assert_no_respawn_side_effect(monkeypatch: pytest.MonkeyPatch) -> list:
    """Wires the shared "no incidental disk footprint" pin every arm below
    reuses. Returns the spy list so the calling test can assert it stayed empty
    AFTER driving `try_warm_guard_dispatch`."""
    calls: list = []
    monkeypatch.setattr(client, "_live_tree_cold", True)
    monkeypatch.setattr(client, "_spawned_this_process", False)
    monkeypatch.setattr(client, "spawn_detached", lambda *a, **kw: calls.append((a, kw)))
    return calls


def test_warm_disabled_falls_open_with_cold_path_parity(monkeypatch: pytest.MonkeyPatch):
    """Arm 1: warm genuinely disabled, exercised via the established seam
    (`settings.registry_get` monkeypatched as a module attribute -- ~10 existing
    tests use this exact pattern; `registry_get` is a deferred delegate but stays
    module-level and patchable per its own docstring). `client.try_warm_dispatch`
    is left completely unstubbed: the "warm off" branch inside `_try_warm_dispatch
    _inner` is what produces the miss here, not a fake standing in for it.
    """
    spawn_calls = _assert_no_respawn_side_effect(monkeypatch)
    monkeypatch.delenv(settings.ENV_VAR, raising=False)
    monkeypatch.setattr(settings, "registry_get", lambda key: None)
    settings._reset_for_test()
    try:
        outcome = try_warm_guard_dispatch("warm_guard.evaluate", {"payload": _deny_event()})
    finally:
        settings._reset_for_test()

    assert outcome == WarmGuardOutcome(hit=False, response=None)
    assert spawn_calls == []
    _assert_cold_path_still_denies()


def test_no_resident_server_falls_open_with_cold_path_parity(monkeypatch: pytest.MonkeyPatch):
    """Arm 2, AS ITSELF -- distinct from "no door" below. `client._open_pipe`
    raises `ConnectionRefusedError`: per that module's own documented anti-storm
    table, this is the "a door exists but nothing answers behind it" outcome (a
    hard-killed server's corpse socket file, or a Windows named pipe nobody is
    serving) -- a different real-world precondition from "no endpoint was ever
    created", even though `_try_warm_dispatch_inner` folds both into the same
    except clause and the same `_spawn_once` trigger.
    """
    spawn_calls = _assert_no_respawn_side_effect(monkeypatch)
    monkeypatch.setattr(client, "is_warm_enabled", lambda: True)
    monkeypatch.setattr(client, "engine_token", lambda: "test-ac4-no-resident-server-token")

    def _raise_refused(endpoint):
        raise ConnectionRefusedError()

    monkeypatch.setattr(client, "_open_pipe", _raise_refused)

    outcome = try_warm_guard_dispatch("warm_guard.evaluate", {"payload": _deny_event()})

    assert outcome == WarmGuardOutcome(hit=False, response=None)
    assert spawn_calls == []
    _assert_cold_path_still_denies()


def test_no_door_falls_open_with_cold_path_parity(monkeypatch: pytest.MonkeyPatch):
    """Arm 3, AS ITSELF -- no endpoint exists at all (`FileNotFoundError`, ENOENT).
    `test_entry_seam.py ::
    test_try_warm_guard_dispatch_falls_open_when_the_door_is_absent` already pins
    the bare `WarmGuardOutcome(hit=False)` fact for this exact fault; this test
    adds the parity clause that one does not assert: that the cold path a caller
    falls through to on this outcome still denies, and that the fail-open branch
    left no disk footprint of its own.
    """
    spawn_calls = _assert_no_respawn_side_effect(monkeypatch)
    monkeypatch.setattr(client, "is_warm_enabled", lambda: True)
    monkeypatch.setattr(client, "engine_token", lambda: "test-ac4-no-door-token")

    def _raise_absent(endpoint):
        raise FileNotFoundError()

    monkeypatch.setattr(client, "_open_pipe", _raise_absent)

    outcome = try_warm_guard_dispatch("warm_guard.evaluate", {"payload": _deny_event()})

    assert outcome == WarmGuardOutcome(hit=False, response=None)
    assert spawn_calls == []
    _assert_cold_path_still_denies()
