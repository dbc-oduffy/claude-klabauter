"""The override boundary survives moving guard evaluation onto a resident server.

WHY THIS FILE EXISTS SEPARATELY FROM `test_override_is_caller_keyed.py`. That suite (C14c,
`32d5224ed`, 9 tests) pins the READER: `_override` prefers a per-call `payload["env"]` over
ambient `os.environ`. Nothing pinned the WRITER, and the reader alone is not a boundary --
a warm branch that simply forgets to populate `payload["env"]` passes all 9 of those tests
while restoring exactly the defect the re-key was written to prevent.

THE DEFECT, stated concretely, because it is silent in every other test. Today a guard runs
in a fresh child process whose environ IS the calling shell's, so `os.environ` is the right
answer by construction. On a resident server that same read returns the SERVER's environ:
frozen at server start and shared by every session on the box. Two failures at once --

  * a legitimate per-session `COORDINATOR_ALLOW_*` set by one operator goes silently dead,
    because it is not in the server's environ; and
  * whatever the server happened to start under becomes a fleet-wide invisible disarm,
    applying to every other session's guard evaluation with nothing to see it.

The pin below is the second case: a server whose OWN environ carries an override that the
forwarded event does NOT, asserting the guard does not see it. Converged on independently
by claude-klabauter-88 and doe-claude-74 before either had spoken to the other, which is why
it is worded as its own file rather than folded into a broader suite.
"""

from __future__ import annotations

from coordinator_core.bash_guards.dispatch_checks import _override
from coordinator_core.warm import hook_http


def test_server_environ_override_is_not_seen_by_a_forwarded_event(monkeypatch):
    """THE INVISIBLE DISARM. The override lives only in this process (standing in for the
    resident server); the forwarded event carries none. The guard must not see it."""
    monkeypatch.setenv("COORDINATOR_ALLOW_RM", "1")

    payload = hook_http.payload_from_event(
        {"hook_event_name": "PreToolUse", "session_id": "s1"}
    )

    assert _override("COORDINATOR_ALLOW_RM", payload=payload) is False


def test_caller_override_is_seen_when_the_event_carries_it(monkeypatch):
    """The other direction: a legitimate per-session override must survive the trip, or
    the warm path silently strips operators of a control they are entitled to."""
    monkeypatch.delenv("COORDINATOR_ALLOW_RM", raising=False)

    payload = hook_http.payload_from_event(
        {"hook_event_name": "PreToolUse", "env": {"COORDINATOR_ALLOW_RM": "1"}}
    )

    assert _override("COORDINATOR_ALLOW_RM", payload=payload) is True


def test_caller_override_wins_over_a_conflicting_server_environ(monkeypatch):
    """Both present and disagreeing. The CALLER's value is the answer -- the server's
    environ is not a participant in this decision at all."""
    monkeypatch.setenv("COORDINATOR_ALLOW_RM", "0")

    payload = hook_http.payload_from_event(
        {"hook_event_name": "PreToolUse", "env": {"COORDINATOR_ALLOW_RM": "1"}}
    )

    assert _override("COORDINATOR_ALLOW_RM", payload=payload) is True


def test_caller_absence_wins_over_a_permissive_server_environ(monkeypatch):
    """The inverse, and the safety-relevant direction: the server is permissive, the
    caller is not, and the guard must side with the caller."""
    monkeypatch.setenv("COORDINATOR_ALLOW_RM", "1")

    payload = hook_http.payload_from_event(
        {"hook_event_name": "PreToolUse", "env": {"COORDINATOR_ALLOW_ORPHAN": "1"}}
    )

    assert _override("COORDINATOR_ALLOW_RM", payload=payload) is False


def test_forwarder_emits_env_even_when_the_caller_set_nothing(monkeypatch):
    """The mechanism the two tests above rest on. If the forwarder omitted `env` entirely
    on a caller with no overrides, `_override` would fall back to ambient environment and
    the disarm would return -- so the empty mapping is load-bearing, not a formality."""
    monkeypatch.setenv("COORDINATOR_ALLOW_RM", "1")

    payload = hook_http.payload_from_event({"hook_event_name": "PreToolUse"})

    assert payload["env"] == {}
    assert _override("COORDINATOR_ALLOW_RM", payload=payload) is False
