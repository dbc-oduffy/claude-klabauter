"""`block_subagent_destructive_action` for an agy subagent whose id is the child conversation UUID.

The live payload (agent_id d8f5a804-..., no backpointer, so the kind is unresolvable): the dry run is allowed, the
destructive form is still confined, and the kind-resolution signal names the backpointer leg, never the
agent_id-canonicalization leg.
"""
from __future__ import annotations

import pytest

from coordinator_core.bash_guards import block_subagent_destructive_action as guard
from coordinator_core.guard_messages.testing import envelope_of

_CONVERSATION = "d8f5a804-4fe2-40a9-a267-0c7e7c9929f3"
_AGENT_ID_LEG = "agent_id-canonicalization"


def _payload(command: str, agent_id: str = _CONVERSATION) -> dict:
    return {
        "tool_name": "Bash",
        "tool_input": {"command": command},
        "agent_id": agent_id,
        "session_id": _CONVERSATION,
        "cwd": None,
    }


def test_dry_run_is_allowed_and_signal_names_the_backpointer_leg(capsys):
    envelope = envelope_of(guard.check(_payload("git clean -n")))
    assert (envelope or {}).get("hookSpecificOutput", {}).get("permissionDecision", "allow") == "allow"
    err = capsys.readouterr().err
    assert _AGENT_ID_LEG not in err
    assert "leg=backpointer-read" in err


def test_force_clean_is_confined_for_unknown_kind_and_signal_names_the_backpointer_leg(capsys):
    verdict = envelope_of(guard.check(_payload("git clean -f untracked-b.txt")))
    assert verdict is not None
    err = capsys.readouterr().err
    assert _AGENT_ID_LEG not in err
    assert "leg=backpointer-read" in err
    assert "CONFINES" in err


def test_unparseable_id_still_reports_the_canonicalization_leg_and_stays_confined(capsys):
    verdict = envelope_of(guard.check(_payload("git clean -f untracked-b.txt", agent_id="not-a-known-shape")))
    assert verdict is not None
    assert _AGENT_ID_LEG in capsys.readouterr().err


@pytest.mark.parametrize("agent_id", ["D8F5A804-4FE2-40A9-A267-0C7E7C9929F3"], ids=["uppercase-uuid"])
def test_uppercase_uuid_stays_unresolved_and_destructive_stays_confined(capsys, agent_id):
    assert envelope_of(guard.check(_payload("git clean -f untracked-b.txt", agent_id=agent_id))) is not None
    assert _AGENT_ID_LEG in capsys.readouterr().err
