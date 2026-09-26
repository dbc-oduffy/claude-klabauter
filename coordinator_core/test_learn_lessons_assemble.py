from __future__ import annotations

import json
from pathlib import Path

import coordinator_core.learn_lessons_assemble as lla


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


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

    # generalizes the assertion to the PROPERTY the fix is meant to hold, using a

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


class TestNoCandidatesCase:
    def test_unrelated_text_and_no_duplicate_headings_yields_empty(self, tmp_path):
        target = _write(
            tmp_path / "wiki.md",
            "# Alpha Topic\n\nCompletely unrelated body content here.\n",
        )
        result = lla.brief(str(target), "zebra giraffe elephant unrelated fauna words")
        assert result.decision_object["gates"]["candidates"] == []
        assert result.decision_object["next_move"] == "Proceed; no adjacent passages were surfaced."


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


class TestCliSmoke:

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
