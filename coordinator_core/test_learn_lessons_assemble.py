"""coordinator_core.test_learn_lessons_assemble -- co-located pytest for
coordinator_core.learn_lessons_assemble (the candidate-restatement
generator; read-only, no mutating half exists).

Mirrors the test_baton_assemble.py / test_sizing_assemble.py idiom: import
the module directly, exercise it in-process against tmp_path fixtures (no
subprocess round-trip to a real CLI). Covers:

  (a) the 8-key envelope shape.
  (b) signal 1 (phrase-overlap) firing on a genuinely shared passage.
  (c) signal 2 (heading-duplicate) firing on two near-duplicate headings
      within the SAME target file.
  (d) the no-candidates case (unrelated incoming text, no duplicate
      headings).
  (e) a nonexistent target path -- returns gracefully, not an exception.
  (f) the generator-not-adjudicator pin: no directives ever, no
      judgment_points ever, and no field anywhere in the envelope that
      reads as a verdict rather than a location pointer.
  (g) a CLI smoke test.

Run: python -m pytest coordinator_core/test_learn_lessons_assemble.py -q
"""
from __future__ import annotations

import json
from pathlib import Path

import coordinator_core.learn_lessons_assemble as lla


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# (a) envelope shape
# ---------------------------------------------------------------------------


class TestEnvelopeShape:
    def test_brief_returns_exactly_the_8_canonical_keys(self, tmp_path):
        target = _write(tmp_path / "wiki.md", "# Heading\n\nSome unrelated body text.\n")
        result = lla.brief(str(target), "totally unrelated incoming text")
        assert set(result.decision_object.keys()) == {
            "artifact",
            "preflight",
            "gates",
            "directives",
            "judgment_points",
            "decisions",
            "narration",
            "next_move",
        }
        assert result.exit_code == lla.EXIT_OK


# ---------------------------------------------------------------------------
# (b) phrase-overlap signal
# ---------------------------------------------------------------------------


class TestPhraseOverlapSignal:
    def test_fires_on_a_shared_phrase(self, tmp_path):
        target = _write(
            tmp_path / "wiki.md",
            (
                "# Some Doctrine\n\n"
                "Every session must scope its commits to the paths it touched.\n\n"
                "Unrelated trailer line.\n"
            ),
        )
        incoming = "New rule: every session must scope its commits to the paths it touched, always."
        candidates, meta = lla.generate_candidates(str(target), incoming)
        signals = {c["signal"] for c in candidates}
        assert "phrase-overlap" in signals
        hit = next(c for c in candidates if c["signal"] == "phrase-overlap")
        assert hit["line"] >= 1
        assert "scope its commits" in hit["excerpt"]
        assert meta["phrase_overlap_count"] >= 1

    def test_does_not_fire_below_the_ngram_size(self, tmp_path):
        target = _write(tmp_path / "wiki.md", "# Heading\n\nShort match: the paths it.\n")
        # Shares only a 3-token run ("the paths it"), below _PHRASE_NGRAM_SIZE=5.
        candidates, _ = lla.generate_candidates(str(target), "somewhere the paths it elsewhere")
        assert not any(c["signal"] == "phrase-overlap" for c in candidates)


# ---------------------------------------------------------------------------
# (b2) precision regression -- the 2026-07-27 first-dogfood failure.
#
# The real incident (against coordinator/docs/wiki/cross-repo-communication.md
# in example-doctrine-repo): appending a rule about worked N-node propagation examples
# produced 13 candidates, every one a `shared_ngrams: 1` hit on the single
# incidental 4-word run "a cross-repo memo" -- generic corpus vocabulary that
# recurs throughout the file, nowhere near the propagation/inheritance/
# override subject matter of the incoming text. This fixture reproduces that
# shape at a representative scale (one generic phrase repeated across many
# unrelated sections) and pins that it now yields zero phrase-overlap
# candidates, while a genuinely long shared run elsewhere in the same file
# still fires -- the fix must not have traded away recall to buy precision.
# ---------------------------------------------------------------------------


class TestPhraseOverlapPrecisionRegression:
    def test_does_not_fire_on_a_generic_recurring_phrase_alone(self, tmp_path):
        target = _write(
            tmp_path / "wiki.md",
            (
                "# Memo Hygiene\n\nA cross-repo memo must name its receiver explicitly.\n\n"
                "## Receiver Verification\n\nA cross-repo memo should be verified before acting.\n\n"
                "## Outbox Discipline\n\nA cross-repo memo sits in the outbox until sent.\n\n"
                "## Inbound Handling\n\nA cross-repo memo lands in the inbox for triage.\n\n"
                "## Unrelated Topic\n\nBody text about something else entirely.\n"
            ),
        )
        incoming = (
            "When accepting a cross-repo memo whose ask includes inheritance or "
            "propagation semantics, require a worked N-node example before signing "
            "off on the commitment."
        )
        candidates, meta = lla.generate_candidates(str(target), incoming)
        assert not any(c["signal"] == "phrase-overlap" for c in candidates)
        assert meta["phrase_overlap_count"] == 0

    def test_still_fires_when_a_genuinely_long_run_is_shared(self, tmp_path):
        target = _write(
            tmp_path / "wiki.md",
            (
                "# Memo Hygiene\n\nA cross-repo memo must name its receiver explicitly.\n\n"
                "## Propagation Rule\n\nEvery inheritance or propagation ask needs a worked "
                "N-node example before it is accepted.\n\n"
                "## Outbox Discipline\n\nA cross-repo memo sits in the outbox until sent.\n"
            ),
        )
        incoming = (
            "New finding: every inheritance or propagation ask needs a worked N-node "
            "example before it is accepted, no exceptions."
        )
        candidates, meta = lla.generate_candidates(str(target), incoming)
        assert meta["phrase_overlap_count"] >= 1
        hit = next(c for c in candidates if c["signal"] == "phrase-overlap")
        assert "propagation ask needs a worked" in hit["excerpt"]

    # Review: code-reviewer — Finding 5. The two tests above pin the exact anecdote
    # ("a cross-repo memo") that motivated the 4->5 n-gram size change. This pair
    # generalizes the assertion to the PROPERTY the fix is meant to hold, using a
    # different recurring generic phrase built from actual coordinator jargon (not
    # the anecdote's vocabulary), and a distinct rare long shared run -- so passing
    # both is evidence the fix generalizes rather than evidence it fits one file.

    def test_does_not_fire_on_generic_coordinator_jargon_alone(self, tmp_path):
        target = _write(
            tmp_path / "wiki.md",
            (
                "# Dispatch Hygiene\n\nThe acting agent should verify the target wiki "
                "file before writing.\n\n"
                "## Sequencing\n\nThe acting agent should verify the target wiki file "
                "against the dispatch brief.\n\n"
                "## Overlap\n\nThe acting agent should verify the target wiki file for "
                "prior claims by another chunk.\n\n"
                "## Rollback\n\nThe acting agent should verify the target wiki file was "
                "not already reverted.\n\n"
                "## Unrelated Topic\n\nBody text about something else entirely.\n"
            ),
        )
        incoming = (
            "When a chunk's dispatch brief names a shared-memory eviction policy, the "
            "executor must confirm the eviction watermark before touching cache state."
        )
        candidates, meta = lla.generate_candidates(str(target), incoming)
        assert not any(c["signal"] == "phrase-overlap" for c in candidates)
        assert meta["phrase_overlap_count"] == 0

    def test_still_fires_on_a_rare_shared_long_run_amid_generic_jargon(self, tmp_path):
        target = _write(
            tmp_path / "wiki.md",
            (
                "# Dispatch Hygiene\n\nThe acting agent should verify the target wiki "
                "file before writing.\n\n"
                "## Eviction Policy\n\nA shared-memory eviction watermark must be "
                "confirmed before touching cache state.\n\n"
                "## Sequencing\n\nThe acting agent should verify the target wiki file "
                "against the dispatch brief.\n"
            ),
        )
        incoming = (
            "New rule: a shared-memory eviction watermark must be confirmed before "
            "touching cache state, no exceptions."
        )
        candidates, meta = lla.generate_candidates(str(target), incoming)
        assert meta["phrase_overlap_count"] >= 1
        hit = next(c for c in candidates if c["signal"] == "phrase-overlap")
        assert "eviction watermark must be confirmed" in hit["excerpt"]


# ---------------------------------------------------------------------------
# (c) heading-duplicate signal
# ---------------------------------------------------------------------------


class TestHeadingDuplicateSignal:
    def test_fires_on_near_duplicate_headings(self, tmp_path):
        target = _write(
            tmp_path / "wiki.md",
            (
                "# Scoped Safety Commits\n\nBody one.\n\n"
                "## Doing Scoped Safety Commits\n\nBody two.\n\n"
                "## Totally Different Topic\n\nBody three.\n"
            ),
        )
        candidates, meta = lla.generate_candidates(str(target), "irrelevant incoming text with no overlap")
        heading_hits = [c for c in candidates if c["signal"] == "heading-duplicate"]
        assert len(heading_hits) == 2
        assert meta["heading_duplicate_count"] == 2
        lines = {c["line"] for c in heading_hits}
        assert lines == {1, 5}

    def test_does_not_fire_on_dissimilar_headings(self, tmp_path):
        target = _write(
            tmp_path / "wiki.md",
            "# Alpha Topic\n\nBody.\n\n## Something Entirely Else\n\nBody.\n",
        )
        candidates, meta = lla.generate_candidates(str(target), "")
        assert not any(c["signal"] == "heading-duplicate" for c in candidates)
        assert meta["heading_duplicate_count"] == 0

    def test_does_not_fire_on_generic_repeated_headings(self, tmp_path):
        # Review: code-reviewer — Finding 10. Two structurally-generic headings
        # ("## Overview" repeated in unrelated sections) score Jaccard 1.0 with no
        # genericity weighting, and would otherwise be indistinguishable from a
        # genuine near-duplicate section.
        target = _write(
            tmp_path / "wiki.md",
            (
                "# Topic One\n\n## Overview\n\nBody about topic one.\n\n"
                "# Topic Two\n\n## Overview\n\nCompletely different body about topic two.\n"
            ),
        )
        candidates, meta = lla.generate_candidates(str(target), "")
        assert not any(c["signal"] == "heading-duplicate" for c in candidates)
        assert meta["heading_duplicate_count"] == 0

    def test_still_fires_when_one_heading_has_topical_content(self, tmp_path):
        # A generic heading paired against a topically-specific near-duplicate
        # must still fire -- the filter only suppresses purely-generic PAIRS.
        target = _write(
            tmp_path / "wiki.md",
            (
                "# Scoped Safety Commits\n\nBody one.\n\n"
                "## Scoped Safety Commits Overview\n\nBody two.\n"
            ),
        )
        candidates, meta = lla.generate_candidates(str(target), "")
        heading_hits = [c for c in candidates if c["signal"] == "heading-duplicate"]
        assert len(heading_hits) == 2
        assert meta["heading_duplicate_count"] == 2


# ---------------------------------------------------------------------------
# (d) no-candidates case
# ---------------------------------------------------------------------------


class TestNoCandidatesCase:
    def test_unrelated_text_and_no_duplicate_headings_yields_empty(self, tmp_path):
        target = _write(
            tmp_path / "wiki.md",
            "# Alpha Topic\n\nCompletely unrelated body content here.\n",
        )
        result = lla.brief(str(target), "zebra giraffe elephant unrelated fauna words")
        assert result.decision_object["gates"]["candidates"] == []
        assert result.decision_object["next_move"] == "Proceed; no adjacent passages were surfaced."


# ---------------------------------------------------------------------------
# (e) nonexistent target path
# ---------------------------------------------------------------------------


class TestNonexistentTargetPath:
    def test_missing_file_returns_gracefully_not_an_exception(self, tmp_path):
        missing = tmp_path / "does" / "not" / "exist.md"
        candidates, meta = lla.generate_candidates(str(missing), "some incoming text")
        assert candidates == []
        assert meta["target_exists"] is False

    def test_missing_file_brief_still_produces_a_valid_envelope(self, tmp_path):
        missing = tmp_path / "nope.md"
        result = lla.brief(str(missing), "some incoming text")
        assert result.exit_code == lla.EXIT_OK
        assert result.decision_object["gates"]["candidates"] == []
        assert result.decision_object["gates"]["target_exists"] is False


# ---------------------------------------------------------------------------
# (f) generator-never-adjudicator pin
# ---------------------------------------------------------------------------

_VERDICT_SHAPED_KEYS = {
    "contradicts",
    "is_duplicate",
    "is_contradiction",
    "verdict",
    "severity",
    "should_fix",
    "disposition",
    "recommendation",
}

# Review: code-reviewer — Finding 4. A denylist of named verdict-shaped strings only
# catches the specific vocabulary chosen today — a future edit adding e.g. `confidence:
# "high"` or `flag: True` to a candidate dict would pass the isdisjoint() checks below
# vacuously. This allowlist is the structural form: it names every key the generator is
# permitted to emit and fails on ANY addition, verdict-shaped or not, forcing a deliberate
# look at the negative-spec (`__init__.py` module docstring) the moment someone adds a key.
_ALLOWED_CANDIDATE_KEYS = {
    "line",
    "excerpt",
    "signal",
    "shared_ngrams",
    "matched_line",
    "matched_excerpt",
    "heading_jaccard",
}


class TestGeneratorNeverAdjudicates:
    def test_no_directives_and_no_judgment_points_ever(self, tmp_path):
        target = _write(
            tmp_path / "wiki.md",
            "# Scoped Safety Commits\n\nEvery session must scope its commits to the paths it touched.\n",
        )
        incoming = "every session must scope its commits to the paths it touched"
        result = lla.brief(str(target), incoming)
        assert result.decision_object["directives"] == []
        assert result.decision_object["judgment_points"] == []

    def test_no_candidate_record_carries_a_verdict_shaped_field(self, tmp_path):
        target = _write(
            tmp_path / "wiki.md",
            (
                "# Scoped Safety Commits\n\nEvery session must scope its commits to the paths it touched.\n\n"
                "## Doing Scoped Safety Commits\n\nBody.\n"
            ),
        )
        incoming = "every session must scope its commits to the paths it touched, no exceptions"
        candidates, _ = lla.generate_candidates(str(target), incoming)
        assert candidates, "expected at least one candidate for this fixture"
        for candidate in candidates:
            assert set(candidate.keys()).isdisjoint(_VERDICT_SHAPED_KEYS)
            assert set(candidate.keys()) <= _ALLOWED_CANDIDATE_KEYS, (
                f"candidate carries an unexpected key not in the allowlist: "
                f"{set(candidate.keys()) - _ALLOWED_CANDIDATE_KEYS}"
            )
            assert "line" in candidate
            assert "excerpt" in candidate

    def test_envelope_contains_no_verdict_shaped_key_anywhere_in_gates(self, tmp_path):
        target = _write(
            tmp_path / "wiki.md",
            "# Scoped Safety Commits\n\nEvery session must scope its commits to the paths it touched.\n",
        )
        result = lla.brief(str(target), "every session must scope its commits to the paths it touched")
        gates = result.decision_object["gates"]
        assert set(gates.keys()).isdisjoint(_VERDICT_SHAPED_KEYS)


# ---------------------------------------------------------------------------
# (g) CLI smoke
# ---------------------------------------------------------------------------


class TestCliSmoke:
    """Calls `lla.main([...])` in-process -- mirrors test_baton_assemble.py's
    own CLI-smoke idiom (`ba.main([...])`), not a subprocess round-trip.
    `learn_lessons_assemble` is a package (has `__init__.py`, no
    `__main__.py`), so `python -m coordinator_core.learn_lessons_assemble`
    is not a valid invocation shape -- the real bin/ trampoline calls
    `mod.main(argv)` directly, which is what this exercises."""

    def test_cli_prints_a_valid_decision_object(self, tmp_path, capsys):
        target = _write(tmp_path / "wiki.md", "# Heading\n\nSome body text.\n")
        exit_code = lla.main([str(target), "unrelated text"])
        assert exit_code == lla.EXIT_OK
        decision = json.loads(capsys.readouterr().out)
        assert decision["directives"] == []

    def test_cli_usage_error_on_no_args(self):
        exit_code = lla.main([])
        assert exit_code == lla.EXIT_USAGE

    def test_cli_text_file_flag(self, tmp_path, capsys):
        target = _write(tmp_path / "wiki.md", "# Heading\n\nSome body text.\n")
        text_file = _write(tmp_path / "incoming.txt", "some incoming text")
        exit_code = lla.main([str(target), "--text-file", str(text_file)])
        assert exit_code == lla.EXIT_OK
        decision = json.loads(capsys.readouterr().out)
        assert decision["preflight"]["incoming_text_length"] == len("some incoming text")
