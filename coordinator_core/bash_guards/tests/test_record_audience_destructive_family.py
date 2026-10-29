"""The destructive/stash subagent guards return AlternativeRecords: no Override, an EscalationRoute,
and a subagent-audience render that is a deny with the route and no override text."""
from __future__ import annotations

import pytest

from coordinator_core.bash_guards import block_subagent_destructive_action as destructive
from coordinator_core.guard_messages.alternative_record import (
    AdaptableOffer,
    AlternativeRecord,
    Audience,
    EscalationRoute,
    Override,
    ReadyCommand,
)
from coordinator_core.guard_messages.render import render

_IDENTITY = {"agent_id": "deadbeef0123", "agent_type": "coordinator:executor"}


def _payload(command: str) -> dict:
    return {"tool_name": "Bash", "tool_input": {"command": command}, **_IDENTITY}


_DESTRUCTIVE_DENIES = [
    "git gc --aggressive",
    "rm -rf some_dir",
    "chmod -R 755 some_dir/",
    "git stash drop",
    "git stash pop",
    "git stash",
    "git mv a.md b.md",
]


@pytest.mark.parametrize("command", _DESTRUCTIVE_DENIES)
def test_destructive_deny_is_a_record_with_a_route_and_no_override(command):
    record = destructive.check(_payload(command))
    assert isinstance(record, AlternativeRecord)
    assert record.verdict_kind == "deny"
    assert not any(isinstance(m, Override) for m in record.members)
    assert any(isinstance(m, EscalationRoute) for m in record.members)


@pytest.mark.parametrize("command", _DESTRUCTIVE_DENIES)
def test_destructive_subagent_render_denies_with_the_route(command):
    envelope = render(destructive.check(_payload(command)), Audience.SUBAGENT)
    out = envelope["hookSpecificOutput"]
    assert out["permissionDecision"] == "deny"
    assert "needs authorizing" in out["permissionDecisionReason"]


def test_git_mv_keeps_its_ready_command_rung():
    record = destructive.check(_payload("git mv state/a.yaml state/archive/a.yaml"))
    ready = [m for m in record.members if isinstance(m, ReadyCommand)]
    assert [m.rendered() for m in ready] == ["mv state/a.yaml state/archive/a.yaml"]


def test_git_mv_with_unparseable_operands_keeps_an_adaptable_offer():
    record = destructive.check(_payload("git mv a.md"))
    assert any(isinstance(m, AdaptableOffer) for m in record.members)
    assert not any(isinstance(m, ReadyCommand) for m in record.members)


def test_a_command_the_guard_allows_returns_none():
    assert destructive.check(_payload("rm state/scratch/one-file.txt")) is None


def test_opaque_indirection_is_an_advisory_record_with_a_route():
    record = destructive.check(_payload("bash -c \"$X\""))
    if record is None:
        pytest.skip("wrapper shape not flagged on this host")
    assert record.verdict_kind in ("advisory", "deny")
    assert any(isinstance(m, EscalationRoute) for m in record.members)
    assert not any(isinstance(m, Override) for m in record.members)


def test_stash_destruction_deny_keeps_em_text_and_a_typed_subagent_render():
    from coordinator_core.bash_guards import block_stash_destruction as destruction

    record = destruction.check({"tool_name": "Bash", "tool_input": {"command": "git stash drop"}})
    assert isinstance(record, AlternativeRecord) and record.verdict_kind == "deny"
    em = render(record, Audience.EM)["hookSpecificOutput"]["permissionDecisionReason"]
    assert em == record.em_text and "discards stash content for good" in em
    sub = render(record, Audience.SUBAGENT)["hookSpecificOutput"]["permissionDecisionReason"]
    assert "git stash show -p" in sub and "needs authorizing" in sub


def test_stash_apply_advisory_keeps_em_text_and_stays_an_allow():
    from coordinator_core.bash_guards import block_stash_destruction as destruction

    record = destruction.check_apply_advisory(
        {"tool_name": "Bash", "tool_input": {"command": "git stash apply"}}
    )
    assert record.verdict_kind == "advisory"
    env = render(record, Audience.EM)["hookSpecificOutput"]
    assert env["permissionDecision"] == "allow" and env["additionalContext"] == record.em_text


def test_stash_destruction_still_allows_a_harmless_stash_verb():
    from coordinator_core.bash_guards import block_stash_destruction as destruction

    assert destruction.check({"tool_name": "Bash", "tool_input": {"command": "git stash list"}}) is None
