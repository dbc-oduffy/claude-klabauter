"""Standing enforcement: the non-instructive, human-only override statement
must lead the bypass-naming sentence emitted by either of the two builders
that own this fleet-wide framing --
``coordinator_core.session.guard_unlock_sentinel.annotate_deny`` and
``coordinator_core.bash_guards._helpers.operator_override_note``. As of
2026-08-11, "lead" means different things for the two: `operator_override_
note`'s payload still leads the WHOLE message (unchanged, see below);
`annotate_deny`'s opening statement leads only ITS OWN block, which trails
the guard's own reason (see WHAT THIS CATCHES).

PREMISE CHANGE, 2026-08-11 (this dispatch) -- until this dispatch, both
builders opened with the IDENTICAL clause, ``"Bypass options for a human
operator, not this agent:"``, so one shared ``_DISCLAIMER`` constant pinned
both. That clause is now GONE from both builders (see WHY below) and the two
replacements are no longer textually identical -- `operator_override_note`
opens with ``"Override key (...)"`` (names the affordance directly, no
disclaimer framing at all); `annotate_deny`'s block opens with ``"An
in-session unlock exists for this guard, but it is a human-only
affordance:"`` (states the same human-only fact `annotate_deny` always
carried, without the old disclaimer register). The single shared-constant
shape this file used no longer fits; each builder is pinned against its own
current leading text below instead. The INVARIANT this file enforces (each
builder's non-instructive framing leads, not trails, the sentence that
follows) is unchanged -- only the literal string each half pins is now
builder-specific.

WHY THIS EXISTS

Item 2 of the cross-repo memo
``cross-repo/archive/2026-08-10-example-retrieval-repo-em-close-out-stamps-implemented-
without-reading-the-ac-table.md``: a sibling repo's dispatched reviewers
received a Bash guard denial naming the ``guard-unlock`` sentinel bypass and
classified it as prompt injection. The audited finding (claude-klabauter,
2026-08-10) established there is NO uncovered code path today -- both
builders hard-coded the disclaimer as the leading clause of their one
f-string, and both engine seams (``write_guards.engine.evaluate``,
``bash_guards.dispatch.evaluate_payload_json``) annotate at their only
deny-return points. That invariant was held BY CONSTRUCTION, not by any
test -- no test asserted the disclaimer's presence, let alone that it leads.
This file is the artifact that closes that gap, modeled on the shape of
``test_no_handwritten_override_clauses.py`` (same package, same
"a prior claim is not evidence" posture): a CLAIM that both builders always
disclaim first is not evidence they do -- this is the test that actually
checks it, by calling the real functions and asserting on their real output,
not by reading the source and trusting it.

SECOND DRIVING INCIDENT (2026-08-11) -- ``cross-repo/inbox/2026-08-11-doe-
claude-em-guard-unlock-banner-still-reads-as-agent-instruction.md``: leading
the WHOLE message was not sufficient for `annotate_deny`. Four independently
dispatched agents in a sibling repo hit the (then-prepended) line and three
classified it as prompt injection, because leading, a fully-resolved
sentinel path, a bare imperative ("create ... FIRST"), and success-
sequencing advice were one sentence ahead of the guard's own "you are
blocked, and why" text -- an agent under a hard-deny reads top-to-bottom and
acts on the first actionable thing it meets, and that sentence was
actionable regardless of its disclaimer. The fix split the two halves:
`annotate_deny` now puts the guard's own reason FIRST and appends the
(now de-imperativized and explicitly self-limiting) unlock block after it,
separated by a blank line.

THIRD DRIVING INCIDENT (2026-08-11, same day, this dispatch) -- cross-repo
memo (example-market-data-repo-em, plus two prior siblings): the disclaimer
opening ITSELF -- ``"Bypass options for a human operator, not this agent:"``
-- was a tell, not a safeguard, independent of ordering: it is exactly the
deniability-preserving phrasing an attacker writes to make an agent feel
authorised while covering itself. A code-reviewer and a review-integrator,
independently and with no shared context, classified it as prompt injection
and declined to act. Both builders' opening clauses were reworded to state
the load-bearing fact plainly instead of through a disclaimer frame -- see
each builder's own docstring ("RESHAPED 2026-08-11" / item 4) for the full
rationale. `operator_override_note`'s payload ALSO dropped its ``KEY=1``
assignment shape in the same reshape -- covered by the dedicated
``test_operator_override_note_no_assignment_form.py``, not re-tested here.

WHY `operator_override_note` DID NOT ALSO FLIP ORDER -- its payload names an
affordance an in-session agent structurally cannot use (a pre-launch env
var, read once at hook-process spawn); the payload corroborates the
human-only framing rather than contradicting it, wherever it sits.
`annotate_deny`'s old payload was the opposite: the sentinel it named WAS
creatable from inside the session, right now, by the reader -- that
asymmetry, not the leading position itself, was the ordering defect (fixed
by the SECOND incident above, unrelated to this file's THIRD-incident
wording change).

WHAT THIS CATCHES

For `operator_override_note`: a future edit that drops the leading
override-key statement, moves it to trail the env-var-naming clause
(unchanged requirement -- this builder still must lead with it), or
reintroduces the old disclaimer register or an assignment form (the latter
is `test_operator_override_note_no_assignment_form.py`'s job specifically).

For `annotate_deny`: a future edit that (a) puts the unlock block ahead of
the guard's own reason again, (b) drops the human-only opening statement
from the front of the unlock block, or (c) reintroduces the fused
imperative-plus-sequencing form ("create <path> FIRST, as its own command --
chaining it onto the denied command re-denies") that reads as agent-directed
instruction rather than an operator-only affordance.

NEGATIVE SPEC -- this does not re-verify unreachability (the "an agent
cannot act on this from inside a live session" property) -- that is
``tests/test_override_unreachability_boundary.py``'s job, cited by
``operator_override_note``'s own docstring, and re-litigating it here would
duplicate coverage rather than add it. This file checks exactly one thing:
each builder's leading statement, and its position, in both builders' actual
output.

Spec backlink: coordinator_core/bash_guards/_helpers.py (`operator_override_note`)
Spec backlink: coordinator_core/session/guard_unlock_sentinel.py (`annotate_deny`)
"""

from __future__ import annotations

from coordinator_core.bash_guards._helpers import operator_override_note
from coordinator_core.session import guard_unlock_sentinel as gus

#: `operator_override_note`'s current leading clause (2026-08-11 reshape) --
#: verbatim prefix match. Deliberately short: just enough to identify the
#: non-instructive override-key framing without pinning the exact
#: parenthetical (`(flag)`/`(reason)`) or key name that follows it.
_OVERRIDE_NOTE_LEAD = "Override key ("

#: `annotate_deny`'s unlock-block leading clause (2026-08-11 reshape) --
#: verbatim prefix match, the human-only-affordance statement that replaced
#: the old shared disclaimer.
_UNLOCK_BLOCK_LEAD = "An in-session unlock exists for this guard, but it is a human-only affordance:"

#: The retired shared disclaimer -- pinned here as an ABSENCE check, not a
#: presence check, in both test classes below: a future edit reintroducing
#: this exact register anywhere in either builder's output is the regression
#: the THIRD driving incident (module docstring) exists to prevent.
_RETIRED_DISCLAIMER = "Bypass options for a human operator, not this agent:"


class TestOperatorOverrideNoteDisclaimer:
    """`bash_guards._helpers.operator_override_note` -- the pre-launch
    env-var-only bypass pointer."""

    def test_override_key_statement_present(self):
        note = operator_override_note("COORDINATOR_OVERRIDE_SOME_GUARD")
        assert _OVERRIDE_NOTE_LEAD in note

    def test_override_key_statement_leads(self):
        note = operator_override_note("COORDINATOR_OVERRIDE_SOME_GUARD")
        assert note.startswith(_OVERRIDE_NOTE_LEAD), (
            "operator_override_note() must lead with the override-key "
            "statement, not bury it after the env-var instruction -- got: %r" % note
        )

    def test_override_key_statement_leads_with_reason_placeholder_variant(self):
        """The reason-shaped render (``reason_placeholder=``) is a distinct
        code path inside the same builder (2026-07-30 P1 fix) -- must not
        silently diverge from the flag-shaped default on where the leading
        statement sits."""
        note = operator_override_note(
            "COORDINATOR_QUEUE_PUNT", reason_placeholder="<why this is being punted>"
        )
        assert note.startswith(_OVERRIDE_NOTE_LEAD)

    def test_retired_disclaimer_register_does_not_reappear(self):
        note = operator_override_note("COORDINATOR_OVERRIDE_SOME_GUARD")
        assert _RETIRED_DISCLAIMER not in note, (
            "the old 'not this agent' disclaimer register must not reappear -- "
            "it was itself the injection tell the 2026-08-11 reshape removed"
        )


class TestAnnotateDenyDisclaimer:
    """`session.guard_unlock_sentinel.annotate_deny` -- the in-session,
    one-shot sentinel-unlock block appended AFTER a firing hard-deny
    envelope's own reason (2026-08-11 flip -- see module docstring)."""

    def _fire(self, tmp_path, monkeypatch):
        import tempfile

        monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))
        envelope = {
            "hookSpecificOutput": {
                "permissionDecision": "deny",
                "permissionDecisionReason": "BLOCKED: some guard fired.",
            }
        }
        return gus.annotate_deny(
            envelope,
            session_id="sess-disclaimer-test",
            guard_name="fake_guard",
            doc_display="claude-klabauter docs/reference/guard-override-keys.md",
        )

    def test_unlock_statement_present(self, tmp_path, monkeypatch):
        out = self._fire(tmp_path, monkeypatch)
        reason = out["hookSpecificOutput"]["permissionDecisionReason"]
        assert _UNLOCK_BLOCK_LEAD in reason

    def test_reason_leads_the_whole_payload(self, tmp_path, monkeypatch):
        """The guard's own reason must be the FIRST thing an agent meets --
        it is the "you are blocked, and why" half, and an agent under a
        hard-deny reads top-to-bottom and acts on the first actionable thing
        it meets (2026-08-11 driving incident, module docstring)."""
        out = self._fire(tmp_path, monkeypatch)
        reason = out["hookSpecificOutput"]["permissionDecisionReason"]
        assert reason.startswith("BLOCKED: some guard fired."), (
            "annotate_deny() must lead with the guard's own reason, not the "
            "operator-unlock block -- got: %r" % reason
        )

    def test_unlock_statement_leads_the_appended_block_not_the_whole_payload(self, tmp_path, monkeypatch):
        out = self._fire(tmp_path, monkeypatch)
        reason = out["hookSpecificOutput"]["permissionDecisionReason"]
        assert reason.index("BLOCKED: some guard fired.") < reason.index(_UNLOCK_BLOCK_LEAD), (
            "the guard's own reason must precede the operator-unlock block "
            "-- got: %r" % reason
        )
        # Within the appended block, the human-only statement still leads
        # the bypass-naming sentence -- that half of the old invariant is
        # unchanged, only the block's position in the whole payload (and its
        # wording, per the THIRD incident) moved.
        block = reason.split("\n\n", 1)[1]
        assert block.startswith(_UNLOCK_BLOCK_LEAD)

    def test_original_reason_still_present_after_the_unlock_block(self, tmp_path, monkeypatch):
        """The unlock block augments, it does not replace -- the guard's own
        reason must still be reachable (now ahead of it, see module
        docstring's 2026-08-11 flip)."""
        out = self._fire(tmp_path, monkeypatch)
        reason = out["hookSpecificOutput"]["permissionDecisionReason"]
        assert "BLOCKED: some guard fired." in reason
        assert reason.index("BLOCKED: some guard fired.") < reason.index(_UNLOCK_BLOCK_LEAD)

    def test_retired_disclaimer_register_does_not_reappear(self, tmp_path, monkeypatch):
        out = self._fire(tmp_path, monkeypatch)
        reason = out["hookSpecificOutput"]["permissionDecisionReason"]
        assert _RETIRED_DISCLAIMER not in reason, (
            "the old 'not this agent' disclaimer register must not reappear -- "
            "it was itself the injection tell the 2026-08-11 reshape removed"
        )

    def test_payload_does_not_read_as_agent_directed_instruction(self, tmp_path, monkeypatch):
        """Regression for the 2026-08-11 incident itself: the fused
        imperative-plus-sequencing form ("create <path> FIRST, as its own
        command -- chaining it onto the denied command re-denies") must not
        reappear, and the block must instead read as self-limiting --
        naming that the sentinel is created by a human from OUTSIDE this
        session, and that an agent creating it is a doctrine violation. A
        test that passes on both the old and new text catches nothing, so
        this asserts on the specific old phrasing being ABSENT and the new
        self-limiting phrasing being PRESENT, not merely on the leading
        statement's presence (already covered above)."""
        out = self._fire(tmp_path, monkeypatch)
        reason = out["hookSpecificOutput"]["permissionDecisionReason"]
        after_lead = reason.split(_UNLOCK_BLOCK_LEAD, 1)[1].lstrip()
        assert not after_lead.startswith("create "), (
            "the block must not open with a bare imperative verb "
            "immediately after the leading statement -- got: %r" % reason
        )
        assert "FIRST, as its own command" not in reason
        assert "chaining it onto the denied command re-denies" not in reason
        assert "outside this session" in reason
        assert "doctrine violation" in reason

    def test_no_resolved_sentinel_path_or_recipe(self, tmp_path, monkeypatch):
        """PM ruling, 2026-08-11 (this dispatch): "a guard's block message
        must STOP carrying its own unlock recipe." The prior fix (item 2,
        above) only fixed the ORDER and register of the block -- it still
        handed the reader a working, fully-parameterized recipe: the
        literal `sentinel_path(session_id, guard_name)`, plus the ordering
        fact framed as "how to make a combined call succeed." This asserts
        that recipe is gone entirely, not merely reordered or reworded --
        it would FAIL on the pre-this-dispatch text (which rendered a real
        `coordinator-guard-unlock-<session>.<guard>` path under the
        monkeypatched temp dir, and the create-then-retry sequencing
        sentence) and passes on the current one.

        RECIPE removed, but not INFORMATION (second PM pass, same
        dispatch): the bare ``session_id``/``guard_name`` VALUES this
        firing carries are still rendered -- no static wiki page can ever
        supply those two per-firing data points on its own, and the
        distinction the PM drew is data (kept) vs. an assembled,
        ready-to-paste path plus imperative/sequencing advice (removed).
        This test pins BOTH halves: the identifiers present as bare
        values, and no assembled path/prefix/temp-root ever appearing --
        so a future edit that either drops the identifiers again (making
        the unlock unreachable) or re-assembles them into a path
        (reintroducing the recipe) fails it."""
        import tempfile

        from coordinator_core.session.guard_unlock_sentinel import (
            _SENTINEL_PREFIX,
            sentinel_path,
        )

        out = self._fire(tmp_path, monkeypatch)
        reason = out["hookSpecificOutput"]["permissionDecisionReason"]
        literal_sentinel = str(
            sentinel_path("sess-disclaimer-test", "fake_guard")
        )
        assert literal_sentinel not in reason, (
            "annotate_deny() must not render the resolved sentinel path -- "
            "got: %r" % reason
        )
        assert _SENTINEL_PREFIX not in reason, (
            "annotate_deny() must not render the sentinel filename prefix "
            "at all -- got: %r" % reason
        )
        assert str(tempfile.gettempdir()) not in reason, (
            "annotate_deny() must not render the resolved temp directory "
            "-- got: %r" % reason
        )
        assert "create-then-retry" not in reason
        assert "before any command runs" not in reason
        assert "single combined" not in reason
        # The two bare identifiers ARE data the message must still carry --
        # see this test's own docstring, "RECIPE removed, but not
        # INFORMATION".
        assert "sess-disclaimer-test" in reason, (
            "annotate_deny() must still name the bare session_id as data -- "
            "got: %r" % reason
        )
        assert "fake_guard" in reason, (
            "annotate_deny() must still name the bare guard_name as data -- "
            "got: %r" % reason
        )

    def test_missing_session_id_skips_the_line_entirely(self, tmp_path, monkeypatch):
        """Documented negative case: a missing session_id must not render a
        sentinel path keyed to an empty session -- the envelope is returned
        unchanged, with no unlock block, since there is nothing to disclaim."""
        import tempfile

        monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))
        envelope = {
            "hookSpecificOutput": {
                "permissionDecision": "deny",
                "permissionDecisionReason": "BLOCKED: some guard fired.",
            }
        }
        out = gus.annotate_deny(envelope, session_id="", guard_name="fake_guard", doc_display="doc")
        reason = out["hookSpecificOutput"]["permissionDecisionReason"]
        assert _UNLOCK_BLOCK_LEAD not in reason
        assert reason == "BLOCKED: some guard fired."


class TestAnnotateDenyAgentIdSuppression:
    """2026-08-11 (this dispatch): the unlock block is a human-only
    affordance a dispatched subagent structurally cannot use (see
    `annotate_deny`'s docstring item 5) -- suppressed for a positively
    resolved subagent `agent_id`, still emitted for everyone else (fail
    direction: absence/malformed emits, only a resolved subagent
    suppresses)."""

    def _fire(self, tmp_path, monkeypatch, *, agent_id=""):
        import tempfile

        monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))
        envelope = {
            "hookSpecificOutput": {
                "permissionDecision": "deny",
                "permissionDecisionReason": "BLOCKED: some guard fired.",
            }
        }
        return gus.annotate_deny(
            envelope,
            session_id="sess-disclaimer-test",
            guard_name="fake_guard",
            doc_display="claude-klabauter docs/reference/guard-override-keys.md",
            agent_id=agent_id,
        )

    def test_resolved_subagent_agent_id_suppresses_the_block(self, tmp_path, monkeypatch):
        # Bare-hex unnamed-agent shape -- resolves via
        # `resolve_subagent_identity` path (a), unchanged, `session_id`
        # ignored by that resolution.
        out = self._fire(tmp_path, monkeypatch, agent_id="abcdef012345")
        reason = out["hookSpecificOutput"]["permissionDecisionReason"]
        assert _UNLOCK_BLOCK_LEAD not in reason
        assert reason == "BLOCKED: some guard fired.", (
            "a resolved subagent must get the deny reason back byte-"
            "identical, with no unlock block appended -- got: %r" % reason
        )

    def test_absent_agent_id_still_emits_the_block(self, tmp_path, monkeypatch):
        out = self._fire(tmp_path, monkeypatch, agent_id="")
        reason = out["hookSpecificOutput"]["permissionDecisionReason"]
        assert _UNLOCK_BLOCK_LEAD in reason

    def test_malformed_agent_id_still_emits_the_block(self, tmp_path, monkeypatch):
        """Fail direction: an unrecognised/malformed agent_id resolves to
        `""` via `resolve_subagent_identity`'s own fail-closed contract,
        which this function treats identically to "absent" -- it must NOT
        be treated as a resolved subagent and must NOT suppress."""
        out = self._fire(tmp_path, monkeypatch, agent_id="not-a-recognised-shape")
        reason = out["hookSpecificOutput"]["permissionDecisionReason"]
        assert _UNLOCK_BLOCK_LEAD in reason
