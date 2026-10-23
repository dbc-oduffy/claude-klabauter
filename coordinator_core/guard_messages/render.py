"""Render an AlternativeRecord into a hook envelope via the _hook_envelope builders."""

from __future__ import annotations

from typing import Optional

from coordinator_core import _hook_envelope as _env
from coordinator_core.guard_messages.alternative_record import (
    AdaptableOffer,
    AlternativeRecord,
    Audience,
    CapabilityRef,
    EscalationRoute,
    Override,
    ReadyCommand,
)

_EVENT = "PreToolUse"


def override_pointer(doc_display: str) -> str:
    """The one-sentence override-doc pointer an EM-audience message appends.

    Names no key and no assignment form; the caller decides whether the reader
    may see it (audience, doc reachability) and passes the doc's display path.
    """
    return "See %s for this guard's override keys." % doc_display


def _member_line(member) -> str:
    if isinstance(member, ReadyCommand):
        return "Run: %s" % member.rendered()
    if isinstance(member, AdaptableOffer):
        return "Adapt %s: %s" % (member.what_to_adapt, member.template)
    if isinstance(member, CapabilityRef):
        return "Capability: %s" % member.capability_id
    if isinstance(member, EscalationRoute):
        return "Report %s; %s needs authorizing." % (member.report_what, member.authorize_what)
    return ""


def render(
    record: AlternativeRecord,
    audience: Optional[Audience] = None,
    *,
    override_note: str = "",
) -> dict:
    """Return the envelope for ``record`` as seen by ``audience``.

    SUBAGENT and UNRESOLVED drop Override members and require an
    EscalationRoute (ValueError if absent). ``override_note`` is the
    caller-supplied override pointer wording, appended only for EM.
    """
    aud = record.audience if audience is None else audience
    em = aud is Audience.EM
    if em and record.em_text is not None:
        return _verbatim(record)
    members = record.members
    if not em:
        members = tuple(m for m in members if not isinstance(m, Override))
        if not any(isinstance(m, EscalationRoute) for m in members):
            raise ValueError("non-EM render requires an EscalationRoute member")
    parts = [record.summary]
    parts.extend(line for line in (_member_line(m) for m in members) if line)
    if em and override_note and any(isinstance(m, Override) for m in members):
        parts.append(override_note)
    text = " ".join(parts)
    if record.verdict_kind == "deny":
        return _env.deny(_EVENT, text)
    if record.verdict_kind == "rewrite":
        env = _env.rewrite_input(_EVENT, record.updated_input, text)
        if record.rewrite_allow:
            env["hookSpecificOutput"]["permissionDecision"] = "allow"
        return env
    return _env.allow_advisory(_EVENT, text)


def _verbatim(record: AlternativeRecord) -> dict:
    """EM envelope carrying ``em_text`` byte for byte: no stamp, members or note.

    Builds the dict locally because the _hook_envelope builders stamp; keeps
    their ``_record`` capture side effect.
    """
    text = record.em_text
    if record.verdict_kind == "deny":
        hso = {
            "hookEventName": _EVENT,
            "permissionDecision": "deny",
            "permissionDecisionReason": text,
        }
        name = "deny"
    elif record.verdict_kind == "rewrite":
        hso = {
            "hookEventName": _EVENT,
            "updatedInput": record.updated_input,
            "additionalContext": text,
        }
        if record.rewrite_allow:
            hso["permissionDecision"] = "allow"
        name = "rewrite_input"
    else:
        hso = {
            "hookEventName": _EVENT,
            "permissionDecision": "allow",
            "additionalContext": text,
        }
        name = "allow_advisory"
    envelope = {"hookSpecificOutput": hso}
    _env._record(name, envelope)
    return envelope
