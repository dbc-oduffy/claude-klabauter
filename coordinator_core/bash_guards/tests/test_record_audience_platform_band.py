"""Audience contract for the platform-conditioned band's AlternativeRecords.

Each guard returns a record; the dispatcher renders it per audience. These
tests drive the production guard entry (`check`) and render the record both
ways: the EM sees the override pointer, a subagent sees none and is given an
escalation route.
"""
from __future__ import annotations

import pytest

from coordinator_core.bash_guards import (
    _platform_verdict,
    guard_grep_via_bash,
    guard_multiprobe_banner,
    guard_plumbing_and_loops,
    multiprobe_banner_rewrite,
)
from coordinator_core.guard_messages.alternative_record import (
    AlternativeRecord,
    Audience,
    EscalationRoute,
    Override,
)
from coordinator_core.guard_messages.render import render

_NOTE = "OVERRIDE-POINTER-SENTINEL"


def _payload(command):
    return {
        "tool_name": "Bash",
        "tool_input": {"command": command},
        "session_id": "platform-band-sess",
        "cwd": None,
    }


_BANNER = 'echo "=== facts ==="; pwd; whoami; git status; git rev-parse HEAD'

_FIRING_CASES = [
    ("grep", guard_grep_via_bash.check, "grep -Pn TODO src/", "COORDINATOR_OVERRIDE_GREP_VIA_BASH_GUARD"),
    ("multiprobe", guard_multiprobe_banner.check, _BANNER, "COORDINATOR_OVERRIDE_MULTIPROBE_BANNER"),
    ("head-tail", guard_plumbing_and_loops.check, "find . -type f | head -n 5", "COORDINATOR_OVERRIDE_PLUMBING_AND_LOOPS"),
    ("for-loop", guard_plumbing_and_loops.check, 'for f in *.txt; do rm "$f"; done', "COORDINATOR_OVERRIDE_PLUMBING_AND_LOOPS"),
    ("while-read", guard_plumbing_and_loops.check, 'cat items.txt | while read x; do echo "$x"; done', "COORDINATOR_OVERRIDE_PLUMBING_AND_LOOPS"),
    ("label-echo", guard_plumbing_and_loops.check, 'git log | head -3; echo "--- done ---"', "COORDINATOR_OVERRIDE_PLUMBING_AND_LOOPS"),
]


def _fire(check, command):
    return check(_payload(command), host_is_windows=False)


def _text(envelope) -> str:
    return envelope["hookSpecificOutput"]["additionalContext"]


@pytest.mark.parametrize("name,check,command,env_var", _FIRING_CASES, ids=[c[0] for c in _FIRING_CASES])
def test_guard_returns_a_record_with_an_env_scoped_override(name, check, command, env_var):
    record = _fire(check, command)
    assert isinstance(record, AlternativeRecord)
    assert record.verdict_kind == "advisory"
    assert [m.env_var for m in record.members if isinstance(m, Override)] == [env_var]


@pytest.mark.parametrize("name,check,command,env_var", _FIRING_CASES, ids=[c[0] for c in _FIRING_CASES])
def test_em_render_is_the_legacy_text_with_the_override_pointer_and_subagent_render_has_none(
    name, check, command, env_var
):
    record = _fire(check, command)
    assert record.em_text
    em = _text(render(record, Audience.EM, override_note=_NOTE))
    assert em == record.em_text
    assert "guard-override-keys.md" in em
    subagent = _text(render(record, Audience.SUBAGENT, override_note=_NOTE))
    assert _NOTE not in subagent
    assert env_var not in subagent
    assert any(isinstance(m, EscalationRoute) for m in record.members)
    assert "needs authorizing" in subagent


@pytest.mark.parametrize(
    "check,command",
    [
        (guard_grep_via_bash.check, "grep -rn TODO src/"),
        (guard_multiprobe_banner.check, "git status"),
        (guard_plumbing_and_loops.check, "ls -la"),
    ],
)
def test_a_command_outside_each_guards_shape_stays_silent(check, command):
    assert _fire(check, command) is None


def test_ready_rewrite_outlet_is_a_ready_command_and_a_skeleton_is_adaptable():
    from coordinator_core.guard_messages.alternative_record import AdaptableOffer, ReadyCommand

    ready = _platform_verdict.shape_advisory_record("s", "cmd", guard_name="g", ready=True)
    skeleton = _platform_verdict.shape_advisory_record("s", "cmd", guard_name="g")
    assert isinstance(ready.members[0], ReadyCommand)
    assert isinstance(skeleton.members[0], AdaptableOffer)
    assert not any(isinstance(m, Override) for m in skeleton.members)


def test_multiprobe_rewrite_record_keeps_the_updated_command_and_hides_the_override_from_subagents():
    record = multiprobe_banner_rewrite.check_multiprobe_banner_rewrite(
        'echo "=== facts ==="; pwd; whoami; date',
        "platform-band-sess",
        payload=_payload('echo "=== facts ==="; pwd; whoami; date'),
    )
    assert isinstance(record, AlternativeRecord)
    assert record.verdict_kind == "rewrite"
    assert record.updated_input["command"]
    em = render(record, Audience.EM, override_note=_NOTE)
    sub = render(record, Audience.SUBAGENT, override_note=_NOTE)
    assert em["hookSpecificOutput"]["updatedInput"] == record.updated_input
    assert sub["hookSpecificOutput"]["updatedInput"] == record.updated_input
    assert em["hookSpecificOutput"]["permissionDecision"] == "allow"
    assert _text(em) == record.em_text
    assert "guard-override-keys.md" in _text(em)
    assert "guard-override-keys.md" not in _text(sub)
    assert _NOTE not in _text(sub)


def test_multiprobe_rewrite_declines_a_chain_with_an_unrecognized_probe():
    assert (
        multiprobe_banner_rewrite.check_multiprobe_banner_rewrite(
            "sed -n 1,5p a.py; echo ===; sed -n 6,9p b.py", "platform-band-sess"
        )
        is None
    )


def test_platform_verdict_for_shape_still_renders_a_dict_for_the_em():
    out = _platform_verdict.platform_verdict_for_shape("shape-x", "cmd", "the outlet", "outlet()")
    ctx = _text(out)
    assert "shape-x" in ctx and "the outlet" in ctx and "outlet()" in ctx
    assert out["hookSpecificOutput"]["permissionDecision"] == "allow"
