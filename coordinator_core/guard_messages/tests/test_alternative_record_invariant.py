"""Rung-C refusal: a record whose only actionable member is an Override."""

import pytest

from coordinator_core.guard_messages import (
    AlternativeRecord,
    Audience,
    CapabilityRef,
    EscalationRoute,
    Override,
    ReadyCommand,
    render,
)


def _rec(members, **kw):
    return AlternativeRecord("deny", "blocked.", tuple(members), "g", **kw)


def test_override_only_record_refused():
    with pytest.raises(ValueError):
        _rec([Override("COORDINATOR_X")])


def test_empty_members_refused():
    with pytest.raises(ValueError):
        _rec([])


def test_override_plus_ready_command_accepted():
    assert _rec([ReadyCommand(text="ls"), Override("COORDINATOR_X")])


def test_unknown_capability_refused():
    with pytest.raises(ValueError):
        CapabilityRef("no-such::capability")


def test_rewrite_needs_updated_input():
    with pytest.raises(ValueError):
        AlternativeRecord("rewrite", "s", (ReadyCommand(text="ls"),), "g")


def test_empty_em_text_refused():
    with pytest.raises(ValueError):
        _rec([ReadyCommand(text="ls")], em_text="")


def test_em_text_without_non_override_member_refused():
    with pytest.raises(ValueError):
        _rec([Override("COORDINATOR_X")], em_text="BLOCKED: x")


def test_em_text_defaults_keep_old_callers_working():
    rec = _rec([ReadyCommand(text="ls")])
    assert rec.em_text is None and rec.rewrite_allow is True


@pytest.mark.parametrize("aud", [Audience.SUBAGENT, Audience.UNRESOLVED])
def test_override_never_emitted_to_non_em(aud):
    rec = _rec([EscalationRoute("the block", "the bypass"), Override("COORDINATOR_X")])
    out = render(rec, aud, override_note="OVERRIDE-NOTE COORDINATOR_X")
    assert "COORDINATOR_X" not in str(out)
    assert "OVERRIDE-NOTE" not in str(out)
