"""The cold rung must evaluate guards as the CALLING session, not as its own host process.

Reported cross-repo by example-retrieval-repo-em on 2026-09-02: a live Tier-U ceremony grant read as
absent through the hook while `check_tier_u_grant` returned True for the same session,
same minute, same engine. The cause was not the grant and not the predicate. Guard
evaluation had moved off a per-call child process (`hooks.json`'s
PreToolUse(Bash|PowerShell) entry became `type: "http"`), and on the cold rung nothing
rebinds the caller's identity -- so `session.core.resolve_session_id()` answered with the
environ of whichever resident process hosted the call.

WHY A DEDICATED FILE RATHER THAN A CASE IN `test_cold_guard_entry.py`. That file's contract
is "cold matches served", and it would go on passing with this defect live: both rungs
would simply agree on the wrong identity. The fact under test here is different in kind --
whose session is the chain speaking for -- so it gets its own pin, and its own name in a
failure report.

Negative-spec: these tests never assert a GUARD's verdict. Identity resolution is the
whole subject; wiring an authorization outcome in as the assertion would couple this pin to
`check_test_suite_invocation`'s grant leg, which is one consumer of the identity among many
(`dispatch_checks`'s per-sid scan and `_resolved_sid_for_diagnosis` are others) and is free
to change its own rules without this fact changing.
"""

from __future__ import annotations

from coordinator_core.session import core as session_core
from coordinator_core.warm import hook_http

_CALLER_SID = "e641c238-68e3-480a-9e44-3ed73e8c5c94"
_HOST_SID = "747f89ba-b778-4690-8278-a02c053cb3c8"


def _event(cmd: str = "echo hello", *, session_id: str | None = _CALLER_SID) -> dict:
    return {
        "hook_event_name": "PreToolUse",
        "session_id": session_id,
        "cwd": "C:/Windows/Temp",
        "tool_name": "Bash",
        "tool_input": {"command": cmd},
        "env": {},
    }


def _identity_seen_by_chain(monkeypatch, event: dict) -> list:
    seen: list = []

    def _probe(_raw, **_kwargs):
        seen.append(session_core.resolve_session_id(None))
        return None

    monkeypatch.setattr(
        "coordinator_core.bash_guards.dispatch.evaluate_payload_json", _probe
    )
    hook_http.evaluate_cold(event)
    return seen


class TestColdRungBindsTheCaller:
    def test_the_chain_sees_the_events_session_not_the_hosts(self, monkeypatch):
        # The host process's own environ names a DIFFERENT session -- the shape a
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", _HOST_SID)
        monkeypatch.delenv("COORDINATOR_SESSION_ID", raising=False)
        monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)

        assert _identity_seen_by_chain(monkeypatch, _event()) == [_CALLER_SID]

    def test_an_event_with_no_session_id_degrades_to_ambient_rather_than_fabricating(
        self, monkeypatch
    ):
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", _HOST_SID)
        monkeypatch.delenv("COORDINATOR_SESSION_ID", raising=False)
        monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)

        assert _identity_seen_by_chain(
            monkeypatch, _event(session_id=None)
        ) == [_HOST_SID]

    def test_the_bind_does_not_leak_past_the_call(self, monkeypatch):
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", _HOST_SID)
        monkeypatch.delenv("COORDINATOR_SESSION_ID", raising=False)
        monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)

        _identity_seen_by_chain(monkeypatch, _event())

        assert session_core.resolve_session_id(None) == _HOST_SID
