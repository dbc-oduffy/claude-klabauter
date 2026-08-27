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

SECOND PREMISE CHANGE, SAME DAY (docs/plans/2026-08-11-guard-messages-point-
to-docs-never-name.md) -- `operator_override_note`'s ``"Override key (...)"``
opening above was itself replaced a few hours later in the same dispatch
sequence: the render no longer names the key (or its flag/reason shape) at
all, only a doc pointer (``"See <doc> for this guard's override keys."``).
`_OVERRIDE_NOTE_LEAD` below is updated to match; the invariant this class
enforces (the builder's one sentence leads the whole message, unchanged
since it IS the whole message now) still holds and is still worth pinning
against a future regrowth that reintroduces framing ahead of the pointer.

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

For `operator_override_note`: a future edit that drops the leading doc-
pointer statement, reintroduces the old disclaimer register, reintroduces
an assignment form (`test_operator_override_note_no_assignment_form.py`'s
job specifically), or reintroduces the env-var name into the render at all
(`test_operator_override_note_retains_affordances.py`'s job).

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

#: `operator_override_note`'s current leading clause (2026-08-11 SECOND
#: reshape, same day -- see module docstring). Deliberately short: this is
#: now effectively the whole rendered string (a doc pointer only, no key,
#: no shape parenthetical), so "leads" and "is" have converged -- pinned
#: here anyway as the regression guard against a future edit that
#: reintroduces framing text ahead of the pointer.
_OVERRIDE_NOTE_LEAD = "See "

#: `annotate_deny`'s unlock-block leading clause. UPDATED 2026-08-13 (C4d,
#: docs/plans/2026-08-13-guard-messages-stop-handing-agents-the-keys.md
#: AC-2, item 9 in `annotate_deny`'s docstring): the human-only-affordance
#: disclosure sentence this constant used to pin is gone entirely -- the
#: block is now the bare doc/wiki pointer sentence, "See <wiki-pointer> and
#: <doc_display> for guard-override conventions." -- so the leading clause
#: this file's "leads, not trails" invariant checks is now just "See ".
_UNLOCK_BLOCK_LEAD = "See "

#: The retired shared disclaimer -- pinned here as an ABSENCE check, not a
#: presence check, in both test classes below: a future edit reintroducing
#: this exact register anywhere in either builder's output is the regression
#: the THIRD driving incident (module docstring) exists to prevent.
_RETIRED_DISCLAIMER = "Bypass options for a human operator, not this agent:"


class TestOperatorOverrideNoteDisclaimer:
    """`bash_guards._helpers.operator_override_note` -- the pre-launch
    env-var-only bypass pointer."""

    def test_doc_pointer_statement_present(self):
        note = operator_override_note(
            "COORDINATOR_OVERRIDE_SOME_GUARD", payload={"session_id": "sess-c1d-em"}
        )
        assert _OVERRIDE_NOTE_LEAD in note

    def test_doc_pointer_statement_leads(self):
        note = operator_override_note(
            "COORDINATOR_OVERRIDE_SOME_GUARD", payload={"session_id": "sess-c1d-em"}
        )
        assert note.startswith(_OVERRIDE_NOTE_LEAD), (
            "operator_override_note() must lead with the doc-pointer "
            "statement -- got: %r" % note
        )

    def test_doc_pointer_statement_leads_with_reason_placeholder_variant(self):
        """`reason_placeholder=` is a distinct call path into the same
        builder (2026-07-30 P1 fix) -- must not silently diverge from the
        default on where the leading statement sits. As of the 2026-08-11
        second reshape, ``reason_placeholder`` no longer changes the
        rendered output at all (see that function's own docstring), so this
        is now also a direct regression check that both calls render the
        identical string."""
        default_note = operator_override_note(
            "COORDINATOR_QUEUE_PUNT", payload={"session_id": "sess-c1d-em"}
        )
        note = operator_override_note(
            "COORDINATOR_QUEUE_PUNT",
            payload={"session_id": "sess-c1d-em"},
            reason_placeholder="<why this is being punted>",
        )
        assert note.startswith(_OVERRIDE_NOTE_LEAD)
        assert note == default_note

    def test_retired_disclaimer_register_does_not_reappear(self):
        note = operator_override_note(
            "COORDINATOR_OVERRIDE_SOME_GUARD", payload={"session_id": "sess-c1d-em"}
        )
        assert _RETIRED_DISCLAIMER not in note, (
            "the old 'not this agent' disclaimer register must not reappear -- "
            "it was itself the injection tell the 2026-08-11 reshape removed"
        )


class TestAnnotateDenyDisclaimer:
    """`session.guard_unlock_sentinel.annotate_deny` -- the in-session,
    one-shot sentinel-unlock block that USED TO be appended AFTER a firing
    hard-deny envelope's own reason (2026-08-11 flip -- see module
    docstring).

    UPDATED 2026-08-13 (C4d, docs/plans/2026-08-13-guard-messages-stop-
    handing-agents-the-keys.md AC-2, item 9 in `annotate_deny`'s docstring):
    the block is gone entirely -- `message_register._rules.run_rule("B8")`
    fires on even the narrowed bare doc/wiki-pointer sentence this dispatch
    tried first (leg (d): any pointer into the override-key/unlock doc
    surface is itself a gate-referent), so `annotate_deny` now always
    returns `out` unchanged. Every "leads"/"trails" assertion in this class
    is inverted accordingly: there is no longer a second half to order
    against the guard's own reason."""

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

    def test_unlock_statement_no_longer_present(self, tmp_path, monkeypatch):
        out = self._fire(tmp_path, monkeypatch)
        reason = out["hookSpecificOutput"]["permissionDecisionReason"]
        assert _UNLOCK_BLOCK_LEAD not in reason

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

    def test_no_second_block_is_appended(self, tmp_path, monkeypatch):
        """Inverted 2026-08-13 (C4d): the envelope is now returned
        byte-identical to what it went in as -- no `\\n\\n`-separated
        second half exists to order against the guard's own reason any
        more."""
        out = self._fire(tmp_path, monkeypatch)
        reason = out["hookSpecificOutput"]["permissionDecisionReason"]
        assert reason == "BLOCKED: some guard fired."

    def test_original_reason_still_present_unchanged(self, tmp_path, monkeypatch):
        """The reason must still be reachable, now byte-identical (item 9)
        rather than merely ahead of an appended block."""
        out = self._fire(tmp_path, monkeypatch)
        reason = out["hookSpecificOutput"]["permissionDecisionReason"]
        assert reason == "BLOCKED: some guard fired."

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
        reappear.

        Inverted 2026-08-13 (C4d, item 9 in `annotate_deny`'s docstring):
        this test used to also assert the block's self-limiting framing
        ("outside this session" / "doctrine violation") was PRESENT -- that
        framing is gone entirely now (AC-2: an EM message may carry the
        wiki pointer and nothing else, and even that was found to trip
        B8), so those two phrases are now asserted ABSENT instead."""
        out = self._fire(tmp_path, monkeypatch)
        reason = out["hookSpecificOutput"]["permissionDecisionReason"]
        assert "create " not in reason.split("BLOCKED: some guard fired.", 1)[-1], (
            "the (now-nonexistent) block must not open with a bare "
            "imperative verb -- got: %r" % reason
        )
        assert "FIRST, as its own command" not in reason
        assert "chaining it onto the denied command re-denies" not in reason
        assert "outside this session" not in reason
        assert "doctrine violation" not in reason

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

        REVERTED (2026-08-13, C3, tasks/guard-messages-keys/C3.md Task 1):
        a later, separate regression (2026-08-12) re-inlined the bare
        ``session_id``/``guard_name`` VALUES plus the sentinel's filename
        shape and drop location as live parameters -- the exact recipe this
        test originally asserted was removed, reintroduced one layer
        differently. C3 reverted that regression: this test now pins that
        NEITHER the assembled path/prefix/temp-root NOR the bare per-firing
        identifiers appear -- only the wiki/doc pointers do."""
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
        # REVERTED (C3, Task 1): the two bare identifiers were re-inlined by
        # a later regression (2026-08-12) and taken back out (2026-08-13) --
        # see this test's own docstring.
        assert "sess-disclaimer-test" not in reason, (
            "annotate_deny() must not name the bare session_id -- "
            "got: %r" % reason
        )
        assert "fake_guard" not in reason, (
            "annotate_deny() must not name the bare guard_name -- "
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
    """2026-08-11: the unlock block is a human-only affordance a dispatched
    subagent structurally cannot use (see `annotate_deny`'s docstring item
    5) -- suppressed for a positively resolved subagent `agent_id`.
    SUPERSEDED direction (2026-08-13, C3, item 8 -- AC-3): the EM decision
    for everything else now routes through `identity.resolves_em_audience`,
    which only emits for a positively-resolved EM audience -- absence emits
    (a well-formed envelope with no agent legs IS the EM signal), but
    malformed/unresolvable/exception now degrade to terse instead of
    emitting."""

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

    def test_absent_agent_id_resolved_em_still_renders_nothing(self, tmp_path, monkeypatch):
        """Inverted 2026-08-13 (C4d, item 9): a resolved-EM audience used
        to be the condition that made the block emit; there is no longer
        any block to emit (B8 fires on it, see class docstring), so a
        resolved EM now gets the reason back unchanged too."""
        out = self._fire(tmp_path, monkeypatch, agent_id="")
        reason = out["hookSpecificOutput"]["permissionDecisionReason"]
        assert _UNLOCK_BLOCK_LEAD not in reason
        assert reason == "BLOCKED: some guard fired."

    def test_malformed_agent_id_degrades_to_terse(self, tmp_path, monkeypatch):
        """AC-3 inversion (2026-08-13, C3): a malformed/unrecognised
        agent_id resolves to `""` via `resolve_subagent_identity`'s own
        fail-closed contract -- it is NOT treated as a resolved subagent
        (unchanged), but the EM-audience decision now routes through
        `identity.resolves_em_audience`, which treats a present-but-
        unresolvable `agent_id` as "cannot resolve" and degrades to terse,
        reversing the old fail-open-to-emit direction."""
        out = self._fire(tmp_path, monkeypatch, agent_id="not-a-recognised-shape")
        reason = out["hookSpecificOutput"]["permissionDecisionReason"]
        assert _UNLOCK_BLOCK_LEAD not in reason
