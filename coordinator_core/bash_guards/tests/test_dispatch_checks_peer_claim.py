"""C3 (plan ``2026-09-01-the-claim-record-carries-the-name``) -- the
``OwnerFact.writer_name`` three-rung resolution ladder as rendered by
``dispatch_checks._format_owner_sentence`` / ``_owner_display_id`` /
``_owner_writer_name_clause``.

Oracle: ``_holder_context`` (``coordinator/bin/coordinator-safe-commit.py``,
landed 3dcf73f06c/586bb605a6) -- PROVENANCE, never ADDRESS. This suite pins
the three rungs (recorded name, live registry lookup, explicit UNNAMED),
the negative-spec (never "re-resolve from the stored session UUID", never a
bare-sid-as-address, "orphan" absent), and the byte-budget boundary the
plan's C3 body names as part of the work, not an afterthought.

C3 follow-up fix 2 (EM-adjudicated break-class): the original fixtures used
a 9-character fake sid (``"peer-sid"``), which fit comfortably inside the
73-byte owner-clause budget and hid the defect that a REAL 36-character
session id, plus a name, does not. Fixtures below use real-shaped uuids so
this suite actually exercises the budget the way production does.
"""

from __future__ import annotations

from coordinator_core.bash_guards import dispatch_checks
from coordinator_core.bash_guards._message_size import MESSAGE_PROSE_CAP_BYTES
from coordinator_core.session.scope import OwnerFact

import pytest


REAL_SID = "46499673-d8dd-4fdd-a514-d8cd34bbba81"
REAL_SID_2 = "9b6b537a-82d0-44dc-bc46-cc3306238051"
REAL_SID_3 = "3d18b2c0-3d17-44ca-b91d-24a769c2f511"
REAL_NAME = "claude-klabauter-65"


class TestWriterNameThreeRungLadder:
    def test_rung1_recorded_name_renders_without_registry_lookup(self, monkeypatch):
        """Rung 1: ``fact.writer_name`` present -- rendered directly, no
        ``harness_registry.lookup`` call at all (the durable, machine-
        independent answer the plan exists to add)."""

        def _boom(_sid):
            raise AssertionError("rung 1 must not fall through to lookup()")

        monkeypatch.setattr(
            "coordinator_core.session.harness_registry.lookup", _boom
        )
        fact = OwnerFact(
            owner=REAL_SID,
            liveness="live",
            claim_source="session",
            writer_name=REAL_NAME,
        )
        sentence = dispatch_checks._format_owner_sentence(fact, {})
        assert REAL_NAME in sentence
        assert "UNNAMED" not in sentence

    def test_rung2_falls_back_to_live_registry_lookup_when_unrecorded(
        self, monkeypatch
    ):
        """Rung 2: no recorded name (pre-C1 claim) -- resolves via a live
        ``harness_registry.lookup`` instead, and says so distinctly from a
        rung-1 recorded name."""

        class _Record:
            name = "claude-klabauter-b3"

        monkeypatch.setattr(
            "coordinator_core.session.harness_registry.lookup",
            lambda sid: _Record() if sid == REAL_SID else None,
        )
        fact = OwnerFact(
            owner=REAL_SID, liveness="live", claim_source="session", writer_name=None
        )
        sentence = dispatch_checks._format_owner_sentence(fact, {})
        assert "claude-klabauter-b3" in sentence
        # The subject slot shrinks to the short (8-char) sid once a name
        # resolves -- see `_owner_display_id`'s docstring. No rung marker
        # -- rung 1 and rung 2 render identically, see
        # `_owner_writer_name_clause`'s docstring.
        assert REAL_SID[:8] in sentence
        assert REAL_SID not in sentence
        assert " -- w:claude-klabauter-b3" in sentence
        assert "UNNAMED" not in sentence

    def test_rung3_unnamed_when_neither_rung_resolves(self, monkeypatch):
        """Rung 3: no recorded name and no live registry match -- an
        explicit UNNAMED marker, visually distinct from a bare sid, never
        printed as though it were an address."""
        monkeypatch.setattr(
            "coordinator_core.session.harness_registry.lookup", lambda sid: None
        )
        fact = OwnerFact(
            owner=REAL_SID, liveness="dead", claim_source="session", writer_name=None
        )
        sentence = dispatch_checks._format_owner_sentence(fact, {})
        assert "UNNAMED" in sentence

    def test_lookup_exception_degrades_to_unnamed_not_a_crash(self, monkeypatch):
        """A resolver exception (registry unavailable/corrupt) degrades to
        rung 3 -- Check 5 is advisory infrastructure and must never turn a
        lookup failure into a guard crash."""
        monkeypatch.setattr(
            "coordinator_core.session.harness_registry.lookup",
            lambda sid: (_ for _ in ()).throw(OSError("registry unreadable")),
        )
        fact = OwnerFact(
            owner=REAL_SID, liveness="live", claim_source="session", writer_name=None
        )
        sentence = dispatch_checks._format_owner_sentence(fact, {})
        assert "UNNAMED" in sentence


class TestWriterNameNegativeSpec:
    def test_never_instructs_re_resolve_from_stored_session_uuid(self, monkeypatch):
        """Anti-scope: the stale-name warning must never tell the reader to
        re-resolve from the stored session UUID -- that sid is, by
        hypothesis, precisely the one that no longer resolves."""
        monkeypatch.setattr(
            "coordinator_core.session.harness_registry.lookup", lambda sid: None
        )
        for writer_name in (REAL_NAME, None):
            fact = OwnerFact(
                owner=REAL_SID,
                liveness="live",
                claim_source="session",
                writer_name=writer_name,
            )
            sentence = dispatch_checks._format_owner_sentence(fact, {})
            assert "re-resolve" not in sentence.lower()
            assert "stored session uuid" not in sentence.lower()

    def test_recorded_name_never_asserted_as_present_tense_reachable(self):
        """A recorded name is provenance, not a live address -- the
        rendering must not claim present-tense reachability (e.g. "is
        live at", "reachable now")."""
        fact = OwnerFact(
            owner=REAL_SID,
            liveness="live",
            claim_source="session",
            writer_name=REAL_NAME,
        )
        sentence = dispatch_checks._format_owner_sentence(fact, {})
        assert REAL_SID[:8] in sentence
        assert REAL_SID not in sentence
        assert " -- w:%s" % REAL_NAME in sentence
        assert "reachable now" not in sentence.lower()
        assert "is live at" not in sentence.lower()

    def test_orphan_absent_with_writer_name_populated(self, monkeypatch):
        monkeypatch.setattr(
            "coordinator_core.session.harness_registry.lookup", lambda sid: None
        )
        facts = [
            OwnerFact(REAL_SID, "live", "session", REAL_NAME),
            OwnerFact(REAL_SID, "dead", "session", None),
            OwnerFact(REAL_SID_2, "live", "agent", "claude-klabauter-c1"),
            OwnerFact(REAL_SID_3, "undetermined", "agent-race", None),
            OwnerFact(REAL_SID_2, "undetermined", "unreadable", None),
        ]
        for fact in facts:
            sentence = dispatch_checks._format_owner_sentence(fact, {})
            assert "orphan" not in sentence, sentence

    def test_six_owner_classes_keep_current_meanings_with_name_additive(
        self, monkeypatch
    ):
        """A name is additive to the subject clause -- it must not change
        which of the six classes a rendering belongs to."""
        monkeypatch.setattr(
            "coordinator_core.session.harness_registry.lookup", lambda sid: None
        )
        fact = OwnerFact(
            owner=REAL_SID_2, liveness="live", claim_source="agent", writer_name="agent-name"
        )
        sentence = dispatch_checks._format_owner_sentence(fact, {})
        assert "dispatched agent" in sentence
        assert "unknown owner" not in sentence


class TestWriterNameBudgetBoundary:
    def test_name_appended_within_budget_survives_intact(self, monkeypatch):
        monkeypatch.setattr(
            "coordinator_core.session.harness_registry.lookup", lambda sid: None
        )
        fact = OwnerFact(
            owner=REAL_SID,
            liveness="live",
            claim_source="session",
            writer_name="short-name",
        )
        sentence = dispatch_checks._format_owner_sentence(fact, {})
        assert "short-name" in sentence
        assert len(sentence.encode("utf-8")) <= dispatch_checks._owner_clause_budget_bytes()

    def test_load_bearing_prefix_survives_truncation_over_a_long_name(
        self, monkeypatch
    ):
        """AT the budget boundary, not just well under it: an oversized
        writer name must not push the load-bearing liveness verdict (which
        sits FIRST in the assembled sentence) out of the truncated result --
        truncation degrades the additive name tail, never the safety-
        relevant prefix."""
        monkeypatch.setattr(
            "coordinator_core.session.harness_registry.lookup", lambda sid: None
        )
        huge_name = "x" * (dispatch_checks._owner_clause_budget_bytes() * 2)
        fact = OwnerFact(
            owner=REAL_SID,
            liveness="live",
            claim_source="session",
            writer_name=huge_name,
        )
        sentence = dispatch_checks._format_owner_sentence(fact, {})
        # `_truncate_to_budget` cuts to the budget then appends a 3-byte
        # ellipsis marker (pre-existing behavior, not this chunk's), so the
        # precedent check (`test_all_owner_class_renderings_stay_within_
        # shipped_message_budget`) asserts against the wider
        # MESSAGE_PROSE_CAP_BYTES, not the tighter owner-clause budget --
        # matched here rather than re-litigated.
        assert len(sentence.encode("utf-8")) <= MESSAGE_PROSE_CAP_BYTES
        assert "confirmed live" in sentence

    def test_real_sid_and_real_name_both_survive_intact_at_rung1(self, monkeypatch):
        """C3 follow-up fix 2 (EM-adjudicated break-class): on a REAL
        36-character sid with a realistic name, the SHORT sid (not the full
        uuid) appears in the subject, and the load-bearing CONTESTED verdict
        always survives intact -- that prefix must never be the thing a
        budget cut degrades.

        Scoped to the ``agent-race``/``unreadable`` classes only -- the
        ``session``/``agent`` x live/dead/undetermined classes this test
        used to also cover are pinned more strongly (liveness-token AND
        budget-length assertions) by
        ``test_all_six_owner_classes_render_name_and_verdict_intact``;
        duplicating them here added no coverage (Review:
        overengineering-reviewer).

        The verdict and name assertions below are NOT decoration, and this
        docstring used to claim them while the body checked neither. Both
        classes were measured emitting the opposite: ``agent-race``'s base
        sentence ran 117 bytes against the ~73-byte budget BEFORE any name,
        so its ``CONTESTED`` verdict was truncated away on every call, named
        or unnamed; ``unreadable`` emitted a ``-- w:proj…`` fragment that
        still tripped ``_owner_name_provenance_note``'s ``" -- w:"``
        trigger, firing a staleness warning beside an unreadable name. Both
        passed every assertion this test then made. Do not weaken these back
        to an id-substitution check.
        """
        monkeypatch.setattr(
            "coordinator_core.session.harness_registry.lookup", lambda sid: None
        )
        cases = [
            (OwnerFact(REAL_SID_3, "undetermined", "agent-race", REAL_NAME), "CONTESTED"),
            (OwnerFact(REAL_SID_2, "undetermined", "unreadable", REAL_NAME), "unreadable"),
        ]
        for fact, verdict in cases:
            sentence = dispatch_checks._format_owner_sentence(fact, {})
            assert fact.owner[:8] in sentence, sentence
            assert fact.owner not in sentence, sentence
            # The load-bearing verdict survives the budget, always.
            assert verdict in sentence, (fact.claim_source, sentence)
            # And so does the whole name -- never a `w:proj…` fragment.
            assert REAL_NAME in sentence, (fact.claim_source, sentence)
            assert "…" not in sentence, (fact.claim_source, sentence)
            assert len(sentence.encode("utf-8")) <= (
                dispatch_checks._owner_clause_budget_bytes()
            ), (fact.claim_source, sentence)

    def test_all_six_owner_classes_render_name_and_verdict_intact(
        self, monkeypatch
    ):
        """Bug 616e4449f90c's own TARGET, as a standalone pin: for the six
        ``session``/``agent`` x live/dead/undetermined owner classes
        (crossed, not the docstring's six-way claim_source taxonomy), the
        full writer name AND the liveness verdict both survive intact --
        neither is truncated to a fragment. Fixtures use a real-shaped
        36-char sid, per this suite's own module docstring: the original
        defect survived review on a 9-char fake that never touched the
        budget."""
        monkeypatch.setattr(
            "coordinator_core.session.harness_registry.lookup", lambda sid: None
        )
        expected_liveness = {
            "live": "confirmed live",
            "dead": "no longer live",
            "undetermined": "CONTESTED",
        }
        for claim_source, sid in (("session", REAL_SID), ("agent", REAL_SID_2)):
            for liveness in ("live", "dead", "undetermined"):
                fact = OwnerFact(sid, liveness, claim_source, REAL_NAME)
                sentence = dispatch_checks._format_owner_sentence(fact, {})
                assert REAL_NAME in sentence, (claim_source, liveness, sentence)
                assert expected_liveness[liveness] in sentence, (
                    claim_source,
                    liveness,
                    sentence,
                )
                assert "…" not in sentence, (claim_source, liveness, sentence)
                assert len(sentence.encode("utf-8")) <= (
                    dispatch_checks._owner_clause_budget_bytes()
                ), (claim_source, liveness, sentence)

    def test_real_sid_and_real_name_both_survive_intact_at_rung2(self, monkeypatch):
        """Same as rung-1 sibling test, but resolved via the live-lookup
        rung rather than a recorded name."""

        class _Record:
            name = REAL_NAME

        monkeypatch.setattr(
            "coordinator_core.session.harness_registry.lookup",
            lambda sid: _Record(),
        )
        fact = OwnerFact(
            owner=REAL_SID, liveness="live", claim_source="session", writer_name=None
        )
        sentence = dispatch_checks._format_owner_sentence(fact, {})
        assert REAL_NAME in sentence, sentence
        assert "confirmed live" in sentence, sentence
        assert "…" not in sentence, sentence

    def test_every_class_with_a_long_writer_name_stays_within_shipped_budget(
        self, monkeypatch
    ):
        monkeypatch.setattr(
            "coordinator_core.session.harness_registry.lookup", lambda sid: None
        )
        long_name = "n" * 400
        facts = [
            OwnerFact(REAL_SID, "live", "session", long_name),
            OwnerFact(REAL_SID, "dead", "session", long_name),
            OwnerFact(REAL_SID_2, "live", "agent", long_name),
            OwnerFact(REAL_SID_3, "undetermined", "agent-race", long_name),
            OwnerFact(REAL_SID_2, "undetermined", "unreadable", long_name),
            OwnerFact(REAL_SID, "undetermined", "session", long_name),
        ]
        for fact in facts:
            sentence = dispatch_checks._format_owner_sentence(fact, {})
            assert len(sentence.encode("utf-8")) <= MESSAGE_PROSE_CAP_BYTES, sentence

    def test_owner_clause_fits_budget_at_the_boundary_with_realistic_long_name(
        self, monkeypatch
    ):
        """AT the budget boundary, not merely well under it: a realistic
        (not adversarially huge) long session/hostname-shaped writer name
        must still land within ``_owner_clause_budget_bytes()`` once the
        clause is truncated -- the boundary itself, not just headroom
        under it, is what this pins."""
        monkeypatch.setattr(
            "coordinator_core.session.harness_registry.lookup", lambda sid: None
        )
        budget = dispatch_checks._owner_clause_budget_bytes()
        # A realistic long hostname-shaped name, sized to straddle the
        # budget boundary exactly (neither trivially short nor
        # adversarially oversized).
        realistic_long_name = "claude-klabauter-executor-fleet-node" * 3
        assert len(realistic_long_name.encode("utf-8")) >= budget
        fact = OwnerFact(
            owner=REAL_SID,
            liveness="live",
            claim_source="session",
            writer_name=realistic_long_name,
        )
        sentence = dispatch_checks._format_owner_sentence(fact, {})
        # `_truncate_to_budget` cuts to the budget then appends a 3-byte
        # ellipsis marker (pre-existing behavior -- see the precedent test
        # above), so the boundary check is against the wider
        # MESSAGE_PROSE_CAP_BYTES, not the tighter owner-clause budget.
        assert len(sentence.encode("utf-8")) <= MESSAGE_PROSE_CAP_BYTES
        assert len(sentence.encode("utf-8")) <= budget + len("…".encode("utf-8"))


class TestOwnerNameProvenanceNote:
    """C3 follow-up fix 1 (EM-adjudicated break-class): the ``" -- w:"``
    name clause is not itself an explicit staleness warning -- the
    warning lives, unbudgeted, in ``_owner_name_provenance_note``, called
    by every deny/warn template that interpolates an
    ``_format_owner_sentence()`` result."""

    def test_note_present_when_owner_sentence_names_someone(self):
        fact = OwnerFact(
            owner=REAL_SID,
            liveness="live",
            claim_source="session",
            writer_name=REAL_NAME,
        )
        sentence = dispatch_checks._format_owner_sentence(fact, {})
        note = dispatch_checks._owner_name_provenance_note(sentence)
        assert note
        assert "provenance" in note.lower()
        assert "not a live address" in note.lower()
        assert "verify" in note.lower()

    def test_note_absent_when_owner_sentence_names_nobody(self, monkeypatch):
        monkeypatch.setattr(
            "coordinator_core.session.harness_registry.lookup", lambda sid: None
        )
        fact = OwnerFact(
            owner=REAL_SID, liveness="dead", claim_source="session", writer_name=None
        )
        sentence = dispatch_checks._format_owner_sentence(fact, {})
        assert "UNNAMED" in sentence
        assert dispatch_checks._owner_name_provenance_note(sentence) == ""

    def test_note_still_fires_when_truncation_eats_into_the_name(
        self, monkeypatch
    ):
        """The note's trigger is a substring/regex match of the ALREADY-
        TRUNCATED owner sentence, so an oversized name must not push the
        ``" -- w:"`` prefix out of the clause and silently suppress the
        warning. That failure is invisible at the call site -- a truncated
        name would still be rendered, with nothing saying it is provenance
        rather than an address, which is the exact reading this warning
        exists to prevent. Pins the coupling between
        ``_owner_writer_name_clause``'s ``" -- w:"`` prefix and
        ``_owner_name_provenance_note``'s trigger: if either moves without
        the other, this test fails rather than the warning going quiet."""
        monkeypatch.setattr(
            "coordinator_core.session.harness_registry.lookup", lambda sid: None
        )
        # A name short enough that the " -- w:" prefix survives
        # _truncate_to_budget intact (unlike the earlier adversarial-huge
        # case, whose whole point is that the clause gets cut) -- this
        # test is about the coupling firing when the clause IS present,
        # not about surviving an oversized name.
        fact = OwnerFact(
            owner=REAL_SID,
            liveness="live",
            claim_source="session",
            writer_name=REAL_NAME,
        )
        sentence = dispatch_checks._format_owner_sentence(fact, {})
        assert dispatch_checks._owner_name_provenance_note(sentence), (
            "the resolved-name clause did not fire the provenance warning"
        )

    def test_note_absent_for_no_claim_found(self):
        sentence = dispatch_checks._format_owner_sentence(None, {})
        assert dispatch_checks._owner_name_provenance_note(sentence) == ""

    def test_note_never_instructs_re_resolve_from_stored_session_uuid(self):
        note = dispatch_checks._OWNER_NAME_PROVENANCE_WARNING
        assert "re-resolve" not in note.lower()
        assert "stored session uuid" not in note.lower()
        assert "orphan" not in note.lower()

    def test_deny_and_warn_templates_carry_the_warning(self, monkeypatch, tmp_path):
        """The three call sites that render ``owner_sentence`` into a
        human-facing message (the CONTESTED strict-mode deny, the plain
        strict-mode deny, and the warn-only advisory) must actually
        include the provenance warning when a name resolves -- pinning
        the note's *existence* is not enough; it must reach the reader.

        KNOWN WEAK (2026-09-02): the three templates below are COPIES of
        the shipped strings, not reads of them, so this assertion cannot
        fail if a call site in ``dispatch_checks`` drops ``%s`` for the
        note -- the vacuous-pin shape ``state/lessons/2026-08-19-a-
        suppressor-pin-can-pass-vacuously.md`` was written about. Backlog
        row: 2026-09-02-the-provenance-note-pin-copies-the-message. The
        copies are kept in sync by hand until then; the contested one was
        updated with the message fix that names two causes."""
        fact = OwnerFact(
            owner=REAL_SID,
            liveness="live",
            claim_source="session",
            writer_name=REAL_NAME,
        )
        owner_sentence = dispatch_checks._format_owner_sentence(fact, {})
        note = dispatch_checks._owner_name_provenance_note(owner_sentence)
        assert note

        contested_msg = (
            "BLOCKED (strict scope): %s is claimed by BOTH "
            "this session and %s, and a live peer's claim "
            "wins — recording it again will not clear this."
            "%s\n\n"
            "Unstage it (git restore --staged %s). Two "
            "causes: this session's write was recorded "
            "under the wrong id, or a peer commit landed "
            "in this file after this session's last read "
            "and this write discarded it. "
            "git log --oneline -3 -- %s tells you which."
            % ("foo.py", owner_sentence, note, "foo.py", "foo.py")
        )
        assert "provenance" in contested_msg.lower()

        plain_msg = (
            "BLOCKED (strict scope): %s is staged but not in "
            "this session's touch list — owned by %s.%s\n\n"
            "Unstage it (git restore --staged %s) or, if it "
            "genuinely belongs to this session's work, record it "
            "as touched first."
            % ("foo.py", owner_sentence, note, "foo.py")
        )
        assert "provenance" in plain_msg.lower()

        warn_msg = (
            "SCOPE: %s is staged but not in this session's touch "
            "list — likely owned by %s. Strict mode would block "
            "this commit.%s"
            % ("foo.py", owner_sentence, note)
        )
        assert "provenance" in warn_msg.lower()
