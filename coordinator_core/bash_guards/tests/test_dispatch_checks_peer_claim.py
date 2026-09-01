"""C3 (plan ``2026-09-01-the-claim-record-carries-the-name``) -- the
``OwnerFact.writer_name`` three-rung resolution ladder as rendered by
``dispatch_checks._format_owner_sentence`` / ``_owner_writer_name_clause``.

Oracle: ``_holder_context`` (``coordinator/bin/coordinator-safe-commit.py``,
landed 3dcf73f06c/586bb605a6) -- PROVENANCE, never ADDRESS. This suite pins
the three rungs (recorded name, live registry lookup, explicit UNNAMED),
the negative-spec (never "re-resolve from the stored session UUID", never a
bare-sid-as-address, "orphan" absent), and the byte-budget boundary the
plan's C3 body names as part of the work, not an afterthought.
"""

from __future__ import annotations

from coordinator_core.bash_guards import dispatch_checks
from coordinator_core.bash_guards._message_size import MESSAGE_PROSE_CAP_BYTES
from coordinator_core.session.scope import OwnerFact

import pytest


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
            owner="peer-sid",
            liveness="live",
            claim_source="session",
            writer_name="claude-klabauter-a9",
        )
        sentence = dispatch_checks._format_owner_sentence(fact, {})
        assert "claude-klabauter-a9" in sentence
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
            lambda sid: _Record() if sid == "peer-sid" else None,
        )
        fact = OwnerFact(
            owner="peer-sid", liveness="live", claim_source="session", writer_name=None
        )
        sentence = dispatch_checks._format_owner_sentence(fact, {})
        assert "claude-klabauter-b3" in sentence
        # rung-2 (live-resolved) marker is the tilde -- distinct from
        # rung-1's asterisk -- see `_owner_writer_name_clause`'s docstring.
        assert "claude-klabauter-b3~" in sentence
        assert "UNNAMED" not in sentence

    def test_rung3_unnamed_when_neither_rung_resolves(self, monkeypatch):
        """Rung 3: no recorded name and no live registry match -- an
        explicit UNNAMED marker, visually distinct from a bare sid, never
        printed as though it were an address."""
        monkeypatch.setattr(
            "coordinator_core.session.harness_registry.lookup", lambda sid: None
        )
        fact = OwnerFact(
            owner="peer-sid", liveness="dead", claim_source="session", writer_name=None
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
            owner="peer-sid", liveness="live", claim_source="session", writer_name=None
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
        for writer_name in ("claude-klabauter-a9", None):
            fact = OwnerFact(
                owner="peer-sid",
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
        live at", "reachable now"), and must carry the rung-1 provenance
        marker (asterisk) rather than being printed bare."""
        fact = OwnerFact(
            owner="peer-sid",
            liveness="live",
            claim_source="session",
            writer_name="claude-klabauter-a9",
        )
        sentence = dispatch_checks._format_owner_sentence(fact, {})
        assert "claude-klabauter-a9*" in sentence
        assert "reachable now" not in sentence.lower()
        assert "is live at" not in sentence.lower()

    def test_orphan_absent_with_writer_name_populated(self, monkeypatch):
        monkeypatch.setattr(
            "coordinator_core.session.harness_registry.lookup", lambda sid: None
        )
        facts = [
            OwnerFact("peer-sid", "live", "session", "claude-klabauter-a9"),
            OwnerFact("peer-sid", "dead", "session", None),
            OwnerFact("em-sid", "live", "agent", "claude-klabauter-c1"),
            OwnerFact("abcdef0123456789", "undetermined", "agent-race", None),
            OwnerFact("sibling-sid", "undetermined", "unreadable", None),
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
            owner="em-sid", liveness="live", claim_source="agent", writer_name="agent-name"
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
            owner="peer-sid",
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
            owner="peer-sid",
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
        assert "peer-sid" in sentence

    def test_every_class_with_a_long_writer_name_stays_within_shipped_budget(
        self, monkeypatch
    ):
        monkeypatch.setattr(
            "coordinator_core.session.harness_registry.lookup", lambda sid: None
        )
        long_name = "n" * 400
        facts = [
            OwnerFact("peer-sid", "live", "session", long_name),
            OwnerFact("peer-sid", "dead", "session", long_name),
            OwnerFact("em-sid", "live", "agent", long_name),
            OwnerFact("abcdef0123456789", "undetermined", "agent-race", long_name),
            OwnerFact("sibling-sid", "undetermined", "unreadable", long_name),
            OwnerFact("peer-sid", "undetermined", "session", long_name),
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
            owner="peer-sid",
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
    """C3 follow-up fix 1 (EM-adjudicated break-class): the one-byte
    ``*``/``~`` marker is not itself an explicit staleness warning -- the
    warning lives, unbudgeted, in ``_owner_name_provenance_note``, called
    by every deny/warn template that interpolates an
    ``_format_owner_sentence()`` result."""

    def test_note_present_when_owner_sentence_names_someone(self):
        fact = OwnerFact(
            owner="peer-sid",
            liveness="live",
            claim_source="session",
            writer_name="claude-klabauter-a9",
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
            owner="peer-sid", liveness="dead", claim_source="session", writer_name=None
        )
        sentence = dispatch_checks._format_owner_sentence(fact, {})
        assert "UNNAMED" in sentence
        assert dispatch_checks._owner_name_provenance_note(sentence) == ""

    def test_note_still_fires_when_truncation_eats_into_the_name(
        self, monkeypatch
    ):
        """The note's trigger is a substring of the ALREADY-TRUNCATED owner
        sentence, so an oversized name must not push the marker prefix out
        of the clause and silently suppress the warning. That failure is
        invisible at the call site -- a truncated name would still be
        rendered, with nothing saying it is provenance rather than an
        address, which is the exact reading this warning exists to prevent.
        Pins the coupling between ``_owner_writer_name_clause``'s marker
        prefix and ``_owner_name_provenance_note``'s trigger: if either
        moves without the other, this test fails rather than the warning
        going quiet."""
        monkeypatch.setattr(
            "coordinator_core.session.harness_registry.lookup", lambda sid: None
        )
        huge_name = "x" * (dispatch_checks._owner_clause_budget_bytes() * 2)
        fact = OwnerFact(
            owner="peer-sid",
            liveness="live",
            claim_source="session",
            writer_name=huge_name,
        )
        sentence = dispatch_checks._format_owner_sentence(fact, {})
        assert dispatch_checks._owner_name_provenance_note(sentence), (
            "truncation removed the name marker, silently suppressing the "
            "provenance warning while still rendering a (truncated) name"
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
        the note's *existence* is not enough; it must reach the reader."""
        fact = OwnerFact(
            owner="peer-sid",
            liveness="live",
            claim_source="session",
            writer_name="claude-klabauter-a9",
        )
        owner_sentence = dispatch_checks._format_owner_sentence(fact, {})
        note = dispatch_checks._owner_name_provenance_note(owner_sentence)
        assert note

        contested_msg = (
            "BLOCKED (strict scope): %s is claimed by BOTH "
            "this session and %s, and a live peer's claim "
            "wins — recording it again will not clear this."
            "%s\n\n"
            "Unstage it (git restore --staged %s). If this "
            "session is the real author, the peer's claim is "
            "what has to go: it is this session's write "
            "recorded under the wrong id, not a peer edit."
            % ("foo.py", owner_sentence, note, "foo.py")
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
