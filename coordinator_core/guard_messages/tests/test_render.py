import json

import pytest

from coordinator_core.guard_messages import (
    AlternativeRecord,
    Audience,
    EscalationRoute,
    Override,
    ReadyCommand,
    render,
)
from coordinator_core.guard_messages.testing import envelope_of


def _rec(kind="deny", **kw):
    return AlternativeRecord(
        kind,
        "blocked.",
        (ReadyCommand(text="ls"), EscalationRoute("it", "the bypass"), Override("K")),
        "g",
        **kw,
    )


def _text(env):
    h = env["hookSpecificOutput"]
    return h.get("permissionDecisionReason") or h["additionalContext"]


def test_em_deny_carries_override_note():
    env = render(_rec(), Audience.EM, override_note="See doc.")
    assert env["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "See doc." in _text(env)


def test_advisory_shape():
    env = render(_rec("advisory"), Audience.EM)
    assert env["hookSpecificOutput"]["permissionDecision"] == "allow"


def test_rewrite_shape():
    env = render(_rec("rewrite", updated_input={"command": "x"}), Audience.EM)
    assert env["hookSpecificOutput"]["updatedInput"] == {"command": "x"}


def test_non_em_without_escalation_raises():
    rec = AlternativeRecord("deny", "s", (ReadyCommand(text="ls"), Override("K")), "g")
    with pytest.raises(ValueError):
        render(rec, Audience.UNRESOLVED)


def _em(kind, text, **kw):
    return AlternativeRecord(
        kind,
        "ignored summary.",
        (ReadyCommand(text="ls"), EscalationRoute("it", "the bypass"), Override("K")),
        "g",
        em_text=text,
        **kw,
    )


def _dump(env):
    return json.dumps(env, indent=1, sort_keys=True)


_STAMPED = "[coordinator] BLOCKED: x\n\nGuard: `g`."
_PLAIN = "BLOCKED: x\n\nGuard: `g`."


def test_em_text_deny_stamped():
    out = _dump(render(_em("deny", _STAMPED), Audience.EM, override_note="NOTE"))
    assert out == (
        '{\n "hookSpecificOutput": {\n  "hookEventName": "PreToolUse",\n'
        '  "permissionDecision": "deny",\n'
        '  "permissionDecisionReason": "[coordinator] BLOCKED: x\\n\\nGuard: `g`."\n }\n}'
    )


def test_em_text_deny_unstamped():
    out = _dump(render(_em("deny", _PLAIN), Audience.EM))
    assert out == (
        '{\n "hookSpecificOutput": {\n  "hookEventName": "PreToolUse",\n'
        '  "permissionDecision": "deny",\n'
        '  "permissionDecisionReason": "BLOCKED: x\\n\\nGuard: `g`."\n }\n}'
    )


def test_em_text_advisory_stamped():
    out = _dump(render(_em("advisory", _STAMPED), Audience.EM))
    assert out == (
        '{\n "hookSpecificOutput": {\n'
        '  "additionalContext": "[coordinator] BLOCKED: x\\n\\nGuard: `g`.",\n'
        '  "hookEventName": "PreToolUse",\n  "permissionDecision": "allow"\n }\n}'
    )


def test_em_text_advisory_unstamped():
    out = _dump(render(_em("advisory", _PLAIN), Audience.EM))
    assert out == (
        '{\n "hookSpecificOutput": {\n'
        '  "additionalContext": "BLOCKED: x\\n\\nGuard: `g`.",\n'
        '  "hookEventName": "PreToolUse",\n  "permissionDecision": "allow"\n }\n}'
    )


def test_em_text_rewrite_stamped():
    rec = _em("rewrite", _STAMPED, updated_input={"command": "x"})
    out = _dump(render(rec, Audience.EM))
    assert out == (
        '{\n "hookSpecificOutput": {\n'
        '  "additionalContext": "[coordinator] BLOCKED: x\\n\\nGuard: `g`.",\n'
        '  "hookEventName": "PreToolUse",\n  "permissionDecision": "allow",\n'
        '  "updatedInput": {\n   "command": "x"\n  }\n }\n}'
    )


def test_em_text_rewrite_unstamped():
    rec = _em("rewrite", _PLAIN, updated_input={"command": "x"})
    out = _dump(render(rec, Audience.EM))
    assert out == (
        '{\n "hookSpecificOutput": {\n'
        '  "additionalContext": "BLOCKED: x\\n\\nGuard: `g`.",\n'
        '  "hookEventName": "PreToolUse",\n  "permissionDecision": "allow",\n'
        '  "updatedInput": {\n   "command": "x"\n  }\n }\n}'
    )


def test_subagent_render_ignores_em_text():
    out = render(_em("deny", _PLAIN), Audience.SUBAGENT)
    assert "BLOCKED: x" not in str(out)
    assert "ignored summary." in str(out)
    rec = AlternativeRecord("deny", "s", (ReadyCommand(text="ls"),), "g", em_text=_PLAIN)
    with pytest.raises(ValueError):
        render(rec, Audience.SUBAGENT)


def test_override_note_ignored_when_em_text_set():
    out = render(_em("deny", _PLAIN), Audience.EM, override_note="OVERRIDE-NOTE")
    assert "OVERRIDE-NOTE" not in str(out)


def test_rewrite_allow_false_emits_no_permission_decision():
    for em_text in (None, _PLAIN):
        rec = _em("rewrite", em_text, updated_input={"command": "x"}, rewrite_allow=False)
        for aud in (Audience.EM, Audience.SUBAGENT):
            hso = render(rec, aud)["hookSpecificOutput"]
            assert "permissionDecision" not in hso
        hso = render(
            _em("rewrite", em_text, updated_input={"command": "x"}), Audience.EM
        )["hookSpecificOutput"]
        assert hso["permissionDecision"] == "allow"


def test_override_pointer_wording_is_the_one_sentence_and_names_no_key():
    from coordinator_core.guard_messages.render import override_pointer

    out = override_pointer("<doc>")
    assert out == "See <doc> for this guard's override keys."
    assert "=" not in out and "COORDINATOR_" not in out


def test_envelope_of_renders_record_and_passes_through():
    assert envelope_of(_rec()) == render(_rec(), Audience.EM)
    assert envelope_of(None) is None
    d = {"a": 1}
    assert envelope_of(d) is d
