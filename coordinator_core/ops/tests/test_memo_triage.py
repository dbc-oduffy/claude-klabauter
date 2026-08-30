"""
coordinator_core.ops.tests.test_memo_triage

Tests for the memo.triage COMPUTE_ONLY op (deterministic pre-filter).

Coverage:
  (a) distill_fate short-circuit — ratification -> promote; commitment/ephemeral
      -> not promoted, regardless of decision/decision_note contents.
  (b) pre-filter scoring — bare decision:accepted (no boundary keyword) = 0,
      not promoted; decision_note boundary keyword = +2, promoted.
  (c) already-captured cross-check — a DISTINCTIVE term (a genuine
      identifier: digit-bearing single word, or id-prefix+number pair like
      "dr-620") hit against docs/decisions, CLAUDE.md, or the auto-memory
      index DISQUALIFIES an otherwise-promotable memo. An alpha-only word
      pair ("drift-anchor", "load-bearing", "routine-load") never qualifies
      as a cross-check term at all, regardless of whether it was ever
      observed and denylisted by name — the generating rule, not an
      enumeration of instances.
  (d) generic boundary vocabulary ("boundary", "owner", "contract", "scope")
      is NOT usable as a cross-check term even when present verbatim in the
      capture corpus — it must not saturate-disqualify the whole corpus.
  (e) malformed/missing-frontmatter memos are quarantined (skipped, not
      counted, not erroring the whole run).
  (f) GOLDEN promote-set fixture — a fixed 9-memo corpus with a pinned,
      by-name expected promote set (not a 1-9 band) exercised end-to-end via
      the handler with archive_dir/repo_root overrides.
  (g) calibration asserts — the golden run demotes a KNOWN-captured memo and
      does NOT demote a genuine ratification-shaped memo.
  (h) command-type dispatch_message smoke with _origin_worktree set (the
      _OP_KEY_SCOPE wire-registration gate) — temp-registers memo.triage into
      _REGISTRY and _OP_KEY_SCOPE for the duration of the test only, restoring
      both afterward; this op is NOT wired into the real seams yet (that is
      the registration agent's job) so this smoke proves the handler is
      dispatch_message-compatible without depending on that wiring landing.
  (i) LIVE-CORPUS golden (AC6) — runs the real handler read-only against this
      repo's actual cross-repo/archive/ corpus (not a synthetic fixture) and
      asserts the promote-set matches a checked-in golden fixture under
      coordinator_core/distill/tests/goldens/memo_triage_live_corpus.json.
      promote is pinned EXACTLY (the load-bearing invariant); corpus_total
      (lower bound) and disqualified (subset) are checked loosely because
      cross-repo/archive/ is a live, multi-session-mutated tracked corpus —
      exact pins there are flaky-by-construction. Regeneration procedure
      documented inline at the test.
  (j) self-contradiction gate — promote==0 while distill_fate_reads>0 must
      raise MemoTriageContradictionError, never return a clean-looking empty
      result; a genuinely empty pre_filter-only result (no fate reads at
      all) must NOT raise.

Spec backlink: coordinator_core/ops/memo_triage.py
Plan: docs/plans/2026-07-12-distill-ceremony-mechanical-substrate-joint-design.md § C5/C6/AC6
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import textwrap
from pathlib import Path

import pytest

from coordinator_core.ops.memo_triage import (
    MemoTriageContradictionError,
    _already_captured,
    _collect_memo_records,
    _corpus_tokens_auto_memory,
    _corpus_tokens_claude_md,
    _corpus_tokens_docs_decisions,
    _distinctive_tokens,
    _handler,
    _score_memo,
    triage_memos,
)

# Spawns a real external process; runs at cadence gates, not per-commit.
# Spawn ratchet: coordinator_core/tests/test_no_new_spawning_tests.py
pytestmark = [
    pytest.mark.spawns_process,
    pytest.mark.cadence,
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _memo(
    *,
    title: str,
    decision=None,
    decision_note=None,
    distill_fate=None,
) -> str:
    """Render a minimal memo file (frontmatter + body) for fixture writes."""
    lines = ["---", f'title: "{title}"', "from: \"sibling-em\"", "to: \"claude-klabauter-em\""]
    if distill_fate is not None:
        lines.append(f"distill_fate: {distill_fate}")
    if decision is not None:
        lines.append(f"decision: {decision}")
    if decision_note is not None:
        lines.append(f"decision_note: '{decision_note}'")
    lines.append("---")
    lines.append("")
    lines.append(f"## {title}")
    lines.append("")
    lines.append("Body text.")
    return "\n".join(lines) + "\n"


def _write_memo(archive_dir: Path, memo_id: str, **kwargs) -> None:
    archive_dir.mkdir(parents=True, exist_ok=True)
    (archive_dir / f"{memo_id}.md").write_text(_memo(**kwargs), encoding="utf-8")


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# (a) distill_fate short-circuit
# ---------------------------------------------------------------------------


def test_distill_fate_ratification_promotes_regardless_of_score():
    fm = {
        "distill_fate": "ratification",
        "decision": None,
        "decision_note": None,
    }
    outcome = _score_memo(fm)
    assert outcome["path"] == "distill_fate"
    assert outcome["fate"] == "ratification"
    assert outcome["promote"] is True
    assert outcome["score"] is None


def test_distill_fate_ephemeral_does_not_promote():
    fm = {
        "distill_fate": "ephemeral",
        # even a boundary-keyword-laden decision_note must NOT override the
        # fate read — fate is a short-circuit, not an additional signal.
        "decision": "accepted",
        "decision_note": "ownership boundary ratified as authoritative",
    }
    outcome = _score_memo(fm)
    assert outcome["path"] == "distill_fate"
    assert outcome["fate"] == "ephemeral"
    assert outcome["promote"] is False


def test_distill_fate_commitment_promotes():
    """Regression pin for the Guard 7 deletion deadlock: a commitment memo,
    never promoted, is also never harvested/cited, so delete_guard.py's
    check_harvest_provenance permanently blocks its deletion. commitment MUST
    stay in _FATE_PROMOTE regardless of decision/decision_note contents."""
    fm = {
        "distill_fate": "commitment",
        "decision": None,
        "decision_note": None,
    }
    outcome = _score_memo(fm)
    assert outcome["path"] == "distill_fate"
    assert outcome["fate"] == "commitment"
    assert outcome["promote"] is True


def test_distill_fate_unknown_value_falls_back_to_pre_filter():
    """An unrecognized distill_fate value (schema drift) falls back to scoring
    rather than silently promoting or silently dropping the memo."""
    fm = {
        "distill_fate": "some-future-value",
        "decision": "accepted",
        "decision_note": "no boundary signal here",
    }
    outcome = _score_memo(fm)
    assert outcome["path"] == "pre_filter"


# ---------------------------------------------------------------------------
# (b) pre-filter scoring
# ---------------------------------------------------------------------------


def test_bare_accepted_no_boundary_keyword_scores_zero():
    fm = {"decision": "accepted", "decision_note": "Shipped as discussed, thanks."}
    outcome = _score_memo(fm)
    assert outcome["path"] == "pre_filter"
    assert outcome["score"] == 0
    assert outcome["promote"] is False


def test_decision_note_boundary_keyword_scores_two_and_promotes():
    fm = {
        "decision": "accepted",
        "decision_note": "Ownership boundary of the widget is now settled.",
    }
    outcome = _score_memo(fm)
    assert outcome["score"] == 2
    assert outcome["promote"] is True


def test_no_decision_at_all_scores_zero():
    fm = {"decision": None, "decision_note": None}
    outcome = _score_memo(fm)
    assert outcome["score"] == 0
    assert outcome["promote"] is False


# ---------------------------------------------------------------------------
# (c) already-captured cross-check — distinctive term hit disqualifies
# ---------------------------------------------------------------------------


def test_cross_check_disqualifies_on_dr_id_hit_in_docs_decisions(tmp_path):
    decisions_dir = tmp_path / "docs" / "decisions"
    decisions_dir.mkdir(parents=True)
    (decisions_dir / "DR-401-widget-ownership.md").write_text(
        "# DR-401 widget ownership\n\nSettled.\n", encoding="utf-8"
    )
    corpus, degraded = _corpus_tokens_docs_decisions(tmp_path)
    assert degraded is False

    terms = _distinctive_tokens(
        title="DR-401 widget ownership ratified",
        decision_note="DR-401 ownership boundary is now ratified as scoped.",
    )
    assert _already_captured(terms, corpus) is True


def test_cross_check_disqualifies_on_dr_id_hit_in_claude_md(tmp_path):
    (tmp_path / "CLAUDE.md").write_text(
        "The DR-620 subsystem is documented here.\n", encoding="utf-8"
    )
    corpus = _corpus_tokens_claude_md(tmp_path)

    terms = _distinctive_tokens(
        title="DR-620 ownership boundary settled",
        decision_note="DR-620 ownership is ratified as authoritative.",
    )
    assert _already_captured(terms, corpus) is True


def test_cross_check_disqualifies_on_auto_memory_hit(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path / "dot-claude"))
    memory_dir = tmp_path / "dot-claude" / "projects" / "test-slug" / "memory"
    memory_dir.mkdir(parents=True)
    (memory_dir / "dr-630-owner-axis-not-flat.md").write_text(
        "DR-630 owner axis is plural-typed, not flat.\n", encoding="utf-8"
    )
    corpus, degraded = _corpus_tokens_auto_memory("test-slug")
    assert degraded is False

    terms = _distinctive_tokens(
        title="DR-630 owner axis decomposition confirmed",
        decision_note="DR-630 ownership boundary ratified; plural-typed confirmed.",
    )
    assert _already_captured(terms, corpus) is True


def test_alpha_only_slug_fragment_no_longer_disqualifies(tmp_path):
    """Narrowed-rule regression pin: an alpha-only compound slug ("drift-
    anchor") is lexically distinctive-LOOKING but is no longer a qualifying
    cross-check term — only a genuine identifier (digit-bearing) qualifies.
    This is the deliberate behavior change of the class-not-instances fix;
    see module docstring."""
    (tmp_path / "CLAUDE.md").write_text(
        "The drift-anchor subsystem is documented here.\n", encoding="utf-8"
    )
    corpus = _corpus_tokens_claude_md(tmp_path)

    terms = _distinctive_tokens(
        title="drift-anchor ownership boundary settled",
        decision_note="drift-anchor ownership is ratified as authoritative.",
    )
    assert terms == set()
    assert _already_captured(terms, corpus) is False


def test_unlisted_generic_bigram_does_not_disqualify(tmp_path):
    """Pins the generating rule, not the eight instances the old
    _GENERIC_BIGRAM_VOCAB denylist enumerated: 'routine-load' and
    'findings-ack' were NEVER in that denylist, yet must not disqualify —
    because NO alpha-only word pair ever qualifies as a cross-check term,
    regardless of whether it was ever observed and named."""
    (tmp_path / "CLAUDE.md").write_text(
        "This is a routine-load memo; findings-ack noted here routinely.\n",
        encoding="utf-8",
    )
    corpus = _corpus_tokens_claude_md(tmp_path)

    terms = _distinctive_tokens(
        title="Routine routine-load findings-ack notice",
        decision_note="routine-load findings-ack noted, boundary settled.",
    )
    assert "routine-load" not in terms
    assert "findings-ack" not in terms
    assert _already_captured(terms, corpus) is False


def test_ordinary_word_plus_number_does_not_disqualify(tmp_path):
    """Regression pin for coordinator-code-reviewer bd2f004c's P1 finding:
    the id-prefix+number bigram shape must not match ordinary English words
    (phase, chunk, step, figure, page, line) followed by a bare number —
    that reopens the saturation bug the whole rewrite exists to close, via
    ordinary-word+number instead of alpha-only word pairs. A repo organised
    in phases/chunks is near-certain to contain "phase 2"/"chunk 5"
    somewhere in docs/decisions or CLAUDE.md, so these must never become
    cross-check terms."""
    (tmp_path / "CLAUDE.md").write_text(
        "Phase 2 rollout ratified. Chunk 5 boundary settled. Step 3 done. "
        "See figure 6, page 9, line 40 for details.\n",
        encoding="utf-8",
    )
    corpus = _corpus_tokens_claude_md(tmp_path)

    terms = _distinctive_tokens(
        title="Phase 2 rollout ratified",
        decision_note="Phase 2 boundary settled; chunk 5 and step 3 also ratified.",
    )
    for bigram in ("phase-2", "chunk-5", "step-3"):
        assert bigram not in terms
    assert _already_captured(terms, corpus) is False


def test_short_alpha_prefix_plus_number_still_disqualifies(tmp_path):
    """Companion pin: genuine short id-prefixes (dr, c, ac, b — all <=3
    chars) must still qualify as distinctive id-prefix+number pairs after
    the shape bound — the fix narrows the class, it does not close it."""
    (tmp_path / "CLAUDE.md").write_text(
        "AC 9 is documented here.\n", encoding="utf-8"
    )
    corpus = _corpus_tokens_claude_md(tmp_path)

    terms = _distinctive_tokens(
        title="AC 9 boundary settled",
        decision_note="AC 9 ownership boundary is now ratified as scoped.",
    )
    assert "ac-9" in terms
    assert _already_captured(terms, corpus) is True


def test_cross_check_absent_project_slug_yields_empty_corpus():
    assert _corpus_tokens_auto_memory(None) == (set(), False)


# ---------------------------------------------------------------------------
# (d) generic boundary vocabulary must NOT be a usable cross-check term
# ---------------------------------------------------------------------------


def test_generic_boundary_vocab_does_not_saturate_cross_check(tmp_path):
    """CLAUDE.md is saturated with 'boundary'/'owner'/'contract'/'scope' — a
    memo whose title/decision_note contains ONLY generic vocab (no DR-id, no
    distinctive multi-word slug fragment) must NOT be disqualified merely
    because those generic words appear in CLAUDE.md."""
    (tmp_path / "CLAUDE.md").write_text(
        "This project has clear boundary and owner and contract and scope "
        "conventions for every ownership decision.\n",
        encoding="utf-8",
    )
    corpus = _corpus_tokens_claude_md(tmp_path)

    terms = _distinctive_tokens(
        title="Routine ownership boundary confirmation",
        decision_note="Ownership boundary and contract scope reconfirmed as usual.",
    )
    # No DR-id, no distinctive multi-word fragment survives the generic-vocab
    # exclusion filter -> empty term set -> cannot be disqualified by this
    # corpus regardless of how saturated it is with the same words.
    assert terms == set()
    assert _already_captured(terms, corpus) is False


# ---------------------------------------------------------------------------
# (c2) already-captured cross-check does NOT gate distill_fate-stamped memos
# ---------------------------------------------------------------------------


def test_cross_check_does_not_disqualify_fate_stamped_memo(tmp_path):
    """A distill_fate=commitment (or ratification) memo is an explicit author
    declaration that durable capture is owed — the already-captured
    cross-check must not override that stamp, even when the memo's title/
    decision_note contains a term that DOES hit the capture corpus. Pins the
    fix for the compounding defect where 109/110 commitment memos and
    21/21 ratifications were blanket-disqualified by generic-vocabulary
    overlap, deadlocking their deletion (Guard 7 in delete_guard.py)."""
    decisions_dir = tmp_path / "docs" / "decisions"
    decisions_dir.mkdir(parents=True)
    (decisions_dir / "DR-401-widget-ownership.md").write_text(
        "# DR-401 widget ownership\n\nSettled.\n", encoding="utf-8"
    )
    docs_corpus, _ = _corpus_tokens_docs_decisions(tmp_path)

    records = [
        {
            "memo_id": "commitment-with-captured-term",
            "slug": "commitment-with-captured-term",
            "title": "DR-401 widget ownership follow-up commitment",
            "decision": None,
            "decision_note": "",
            "fm": {"distill_fate": "commitment"},
        }
    ]
    result = triage_memos(records, capture_corpus=docs_corpus)
    assert result["promote"] == ["commitment-with-captured-term"]
    assert result["disqualified"] == []


def test_generic_vocab_term_does_not_disqualify_pre_filter_memo(tmp_path):
    """A pre_filter-path memo whose only cross-check terms are generic
    coordinator vocabulary ('load-bearing', 'review-findings') must NOT be
    disqualified on that basis alone — pins the bigram-vocabulary-saturation
    defect independently of the golden fixture's synthetic terms."""
    (tmp_path / "CLAUDE.md").write_text(
        "This is load-bearing substrate; review-findings apply here routinely.\n",
        encoding="utf-8",
    )
    claude_corpus = _corpus_tokens_claude_md(tmp_path)

    terms = _distinctive_tokens(
        title="Routine load-bearing review-findings ack",
        decision_note="load-bearing review-findings noted, boundary settled.",
    )
    assert "load-bearing" not in terms
    assert "review-findings" not in terms
    assert _already_captured(terms, claude_corpus) is False


# ---------------------------------------------------------------------------
# (e) quarantine — malformed / missing frontmatter
# ---------------------------------------------------------------------------


def test_missing_frontmatter_is_quarantined(tmp_path):
    archive_dir = tmp_path / "archive"
    archive_dir.mkdir(parents=True)
    (archive_dir / "no-frontmatter.md").write_text("Just a body, no frontmatter.\n")
    _write_memo(archive_dir, "well-formed", title="Well formed", decision="accepted")

    records, degraded = _collect_memo_records(archive_dir)
    assert degraded is False
    ids = {r["memo_id"] for r in records}
    assert "no-frontmatter" not in ids
    assert "well-formed" in ids


def test_malformed_yaml_frontmatter_is_quarantined(tmp_path):
    archive_dir = tmp_path / "archive"
    archive_dir.mkdir(parents=True)
    (archive_dir / "bad-yaml.md").write_text(
        "---\ntitle: [unterminated\n---\nBody.\n", encoding="utf-8"
    )
    records, degraded = _collect_memo_records(archive_dir)
    assert records == []
    assert degraded is False


def test_empty_archive_dir_yields_no_records(tmp_path):
    assert _collect_memo_records(tmp_path / "does-not-exist") == ([], False)


# ---------------------------------------------------------------------------
# (f) GOLDEN promote-set fixture — pinned by-name expected set
# ---------------------------------------------------------------------------


@pytest.fixture
def golden_corpus(tmp_path):
    """A checked-in 9-memo fixture corpus + capture-corpus (docs/decisions,
    CLAUDE.md, auto-memory) with a hand-computed, pinned golden promote set.

    Memo-by-memo classification (see inline comments) — the golden set is
    EXACTLY {"m01-fate-ratification", "m02-fate-commitment",
    "m04-dr502-boundary-ratified", "m08-dr733-partial-scope-ratified"} — a
    silent N->N' regression must fail.
    """
    root = tmp_path
    archive_dir = root / "cross-repo" / "archive"

    # m01: distill_fate=ratification -> promote (fate short-circuit).
    _write_memo(
        archive_dir,
        "m01-fate-ratification",
        title="Explicit ratification via forward distill_fate schema",
        distill_fate="ratification",
    )
    # m02: distill_fate=commitment -> promoted (fate short-circuit; commitment
    # is a promote signal so the memo can be harvested and satisfy
    # delete_guard.py's Guard 7 harvest-provenance check before deletion).
    _write_memo(
        archive_dir,
        "m02-fate-commitment",
        title="Explicit commitment via forward distill_fate schema",
        distill_fate="commitment",
        decision="accepted",
        decision_note="ownership boundary settled",
    )
    # m03: bare accepted, no boundary keyword -> score 0 -> NOT promoted.
    _write_memo(
        archive_dir,
        "m03-bare-accepted-fyi",
        title="FYI ack of a routine landed change",
        decision="accepted",
        decision_note="Shipped as discussed, no further action needed here.",
    )
    # m04: accepted + boundary keyword, DR-502 not present elsewhere -> promoted.
    _write_memo(
        archive_dir,
        "m04-dr502-boundary-ratified",
        title="DR-502 widget-cache ownership boundary settled",
        decision="accepted",
        decision_note="DR-502 widget-cache ownership boundary is now ratified as scoped.",
    )
    # m05: accepted + boundary keyword, but DR-401 IS captured in docs/decisions -> disqualified.
    _write_memo(
        archive_dir,
        "m05-dr401-already-captured",
        title="DR-401 widget ownership boundary reconfirmed",
        decision="accepted",
        decision_note="DR-401 ownership boundary reconfirmed as ratified.",
    )
    # m06: accepted + boundary keyword, but DR-620 IS captured in CLAUDE.md -> disqualified.
    _write_memo(
        archive_dir,
        "m06-drift-anchor-already-captured",
        title="DR-620 ownership boundary reconfirmed",
        decision="accepted",
        decision_note="DR-620 ownership boundary reconfirmed as authoritative.",
    )
    # m07: accepted + boundary keyword, but DR-630 IS captured in auto-memory -> disqualified.
    _write_memo(
        archive_dir,
        "m07-owner-axis-already-captured",
        title="DR-630 owner axis decomposition reconfirmed",
        decision="accepted",
        decision_note="DR-630 ownership boundary ratified; plural-typed reconfirmed.",
    )
    # m08: partial decision + boundary keyword, DR-733 not present elsewhere -> promoted.
    _write_memo(
        archive_dir,
        "m08-dr733-partial-scope-ratified",
        title="DR-733 partial scope carve ratified",
        decision="partial",
        decision_note="DR-733 scope carve is ratified for the claude-klabauter half; boundary settled.",
    )
    # m09: declined, no boundary keyword -> NOT promoted.
    _write_memo(
        archive_dir,
        "m09-declined-out-of-scope",
        title="Declined out of claude-klabauter scope",
        decision="declined",
        decision_note="Out of scope; forwarded elsewhere for handling.",
    )

    # Capture corpus: docs/decisions has DR-401 (disqualifies m05).
    decisions_dir = root / "docs" / "decisions"
    decisions_dir.mkdir(parents=True)
    (decisions_dir / "DR-401-widget-ownership.md").write_text(
        "# DR-401 widget ownership\n\nSettled.\n", encoding="utf-8"
    )

    # CLAUDE.md has DR-620 (disqualifies m06) plus generic boundary vocab
    # noise (must NOT disqualify m04/m08, which carry no distinctive overlap).
    (root / "CLAUDE.md").write_text(
        textwrap.dedent(
            """\
            # project

            DR-620 defines ownership boundary and contract
            scope conventions used across every ownership decision in this repo.
            """
        ),
        encoding="utf-8",
    )

    # Auto-memory has dr-630-owner-axis-not-flat (disqualifies m07).
    memory_dir = root / "dot-claude" / "projects" / "test-slug" / "memory"
    memory_dir.mkdir(parents=True)
    (memory_dir / "dr-630-owner-axis-not-flat.md").write_text(
        "DR-630 owner axis is plural-typed, not flat.\n", encoding="utf-8"
    )

    return root


_GOLDEN_PROMOTE_SET = sorted(
    [
        "m01-fate-ratification",
        "m02-fate-commitment",
        "m04-dr502-boundary-ratified",
        "m08-dr733-partial-scope-ratified",
    ]
)
_GOLDEN_DISQUALIFIED_SET = sorted(
    [
        "m05-dr401-already-captured",
        "m06-drift-anchor-already-captured",
        "m07-owner-axis-already-captured",
    ]
)


def test_golden_promote_set_matches_pinned_fixture(golden_corpus, monkeypatch):
    monkeypatch.setenv("CLAUDE_HOME", str(golden_corpus / "dot-claude"))

    params = {
        "archive_dir": str(golden_corpus / "cross-repo" / "archive"),
        "project_slug": "test-slug",
    }
    result = _handler(params, repo_root=golden_corpus / ".git")

    assert result["promote"] == _GOLDEN_PROMOTE_SET, (
        f"Golden promote set drifted: expected {_GOLDEN_PROMOTE_SET}, "
        f"got {result['promote']} — a silent regression in the pre-filter "
        f"or cross-check must fail this test, not pass silently."
    )
    assert result["counts"]["promote"] == 4
    assert result["counts"]["total"] == 9


# ---------------------------------------------------------------------------
# (g) calibration asserts — demotes known-captured, does not demote genuine ratification
# ---------------------------------------------------------------------------


def test_calibration_demotes_known_captured_memos(golden_corpus, monkeypatch):
    monkeypatch.setenv("CLAUDE_HOME", str(golden_corpus / "dot-claude"))
    params = {
        "archive_dir": str(golden_corpus / "cross-repo" / "archive"),
        "project_slug": "test-slug",
    }
    result = _handler(params, repo_root=golden_corpus / ".git")

    assert set(result["disqualified"]) == set(_GOLDEN_DISQUALIFIED_SET)
    for memo_id in _GOLDEN_DISQUALIFIED_SET:
        assert memo_id not in result["promote"], (
            f"{memo_id} is KNOWN-captured (already documented) and must be "
            f"demoted by the cross-check, not promoted."
        )


def test_calibration_does_not_demote_genuine_ratification(golden_corpus, monkeypatch):
    """m01 (distill_fate=ratification), m02 (distill_fate=commitment), and
    m04/m08 (genuine novel boundary settlements) must survive the
    cross-check — the disqualification path must be selective, not a
    blanket demotion of every candidate."""
    monkeypatch.setenv("CLAUDE_HOME", str(golden_corpus / "dot-claude"))
    params = {
        "archive_dir": str(golden_corpus / "cross-repo" / "archive"),
        "project_slug": "test-slug",
    }
    result = _handler(params, repo_root=golden_corpus / ".git")

    for memo_id in (
        "m01-fate-ratification",
        "m02-fate-commitment",
        "m04-dr502-boundary-ratified",
        "m08-dr733-partial-scope-ratified",
    ):
        assert memo_id in result["promote"]
        assert memo_id not in result["disqualified"]


# ---------------------------------------------------------------------------
# (j) self-contradiction gate — promote==0 while distill_fate_reads>0 must
# fail loud, never return a clean-looking empty result
# ---------------------------------------------------------------------------


def test_zero_promote_with_distill_fate_reads_raises_contradiction():
    """Pins the 2026-08-06 live-corpus incident's regression guard: a
    distill_fate stamp is the author's own promotion request, so a run that
    read one or more such stamps and still promoted nothing must fail loud
    (MemoTriageContradictionError), never return promote: 0, degraded: false
    as if that were a clean result."""
    records = [
        {
            "memo_id": "m-ephemeral-only",
            "slug": "m-ephemeral-only",
            "title": "Ephemeral fyi ack",
            "decision": None,
            "decision_note": "",
            "fm": {"distill_fate": "ephemeral"},
        }
    ]
    with pytest.raises(MemoTriageContradictionError):
        triage_memos(records, capture_corpus=set())


def test_zero_distill_fate_reads_with_zero_promote_does_not_raise():
    """A genuinely empty pre_filter-only result (no distill_fate stamps read
    at all) is NOT a contradiction — the gate is scoped to the specific
    implausible shape (promote==0 alongside distill_fate_reads>0), not to
    every zero-promote outcome."""
    records = [
        {
            "memo_id": "m-bare-fyi",
            "slug": "m-bare-fyi",
            "title": "Routine ack",
            "decision": "accepted",
            "decision_note": "Shipped as discussed, no further action needed.",
            "fm": {"decision": "accepted", "decision_note": "Shipped as discussed, no further action needed."},
        }
    ]
    result = triage_memos(records, capture_corpus=set())
    assert result["promote"] == []
    assert result["counts"]["distill_fate_reads"] == 0


# ---------------------------------------------------------------------------
# handler-level edge cases
# ---------------------------------------------------------------------------


def test_handler_returns_empty_outcome_when_repo_root_none():
    result = _handler({}, repo_root=None)
    assert result["promote"] == []
    assert result["counts"]["total"] == 0


def test_triage_memos_pure_function_matches_handler_shape():
    """triage_memos() is directly callable as a pure function (no repo_root/
    filesystem dependency) — the classification core is unit-testable in
    isolation from the handler's I/O concerns."""
    records = [
        {
            "memo_id": "solo",
            "slug": "solo",
            "title": "Solo memo",
            "decision": "accepted",
            "decision_note": "boundary settled",
            "fm": {"decision": "accepted", "decision_note": "boundary settled"},
        }
    ]
    result = triage_memos(records, capture_corpus=set())
    assert result["counts"]["total"] == 1
    assert result["promote"] == ["solo"]


# ---------------------------------------------------------------------------
# (h) command-type dispatch_message smoke — _OP_KEY_SCOPE wire-registration gate
# ---------------------------------------------------------------------------


def test_dispatch_message_smoke_with_origin_worktree(golden_corpus, monkeypatch):
    """Prove the handler is dispatch_message-compatible via a command-type
    JSON-RPC round-trip with _origin_worktree set, WITHOUT depending on the
    real _OP_KEY_SCOPE / ops/__init__.py wiring (that is the registration
    agent's job, not this chunk's). Temp-registers memo.triage into a COPY of
    the live registry/scope for the duration of this test only; both are
    restored in the finally block regardless of outcome.

    This directly exercises the wire-registration gate this chunk must not
    miss: an op absent from _OP_KEY_SCOPE silently degrades to central scope
    (lesson 2026-07-06-compute-only-op-registration-needs-an-op) — this test
    asserts the op behaves correctly when GIVEN a scope, so the registration
    agent's follow-up wiring has a proven-correct handler to wire in.
    """
    import subprocess

    import coordinator_core.ipc as ipc
    from coordinator_core.win_portability import no_console_passthrough_kwargs

    monkeypatch.setenv("CLAUDE_HOME", str(golden_corpus / "dot-claude"))

    # dispatch_message's routing-key resolution shells out to `git
    # rev-parse --git-common-dir` under _origin_worktree — make the fixture
    # root a real (throwaway) git repo so that resolution succeeds.
    subprocess.run(
        ["git", "init", "-q"], cwd=golden_corpus, check=True, **no_console_passthrough_kwargs()
    )

    saved_registry = ipc._REGISTRY.get("memo.triage")
    saved_scope = ipc._OP_KEY_SCOPE.get("memo.triage")
    try:
        ipc._REGISTRY["memo.triage"] = _handler
        # common_dir: memo.triage reads repo-relative state (cross-repo/archive,
        # docs/decisions, CLAUDE.md) keyed off the calling worktree.
        ipc._OP_KEY_SCOPE["memo.triage"] = "common_dir"

        msg = {
            "jsonrpc": "2.0",
            "id": 99,
            "method": "memo.triage",
            "params": {
                "archive_dir": str(golden_corpus / "cross-repo" / "archive"),
                "project_slug": "test-slug",
            },
            "_origin_worktree": str(golden_corpus),
        }
        d = _run(ipc.dispatch_message(msg))
    finally:
        if saved_registry is None:
            ipc._REGISTRY.pop("memo.triage", None)
        else:
            ipc._REGISTRY["memo.triage"] = saved_registry
        if saved_scope is None:
            ipc._OP_KEY_SCOPE.pop("memo.triage", None)
        else:
            ipc._OP_KEY_SCOPE["memo.triage"] = saved_scope

    assert "result" in d, f"dispatch_message must succeed; got error: {d.get('error')}"
    assert d["result"]["promote"] == _GOLDEN_PROMOTE_SET


# ---------------------------------------------------------------------------
# (i) LIVE-CORPUS golden — real cross-repo/archive/ corpus (AC6)
# ---------------------------------------------------------------------------

# This repo's own working tree, resolved from this test file's location —
# NOT tmp_path. AC6 explicitly requires validating against the LIVE 57(+)-memo
# corpus, not a synthetic fixture (the synthetic 9-memo golden above already
# covers the classifier's decision-boundary unit behaviour).
_REPO_ROOT = Path(__file__).resolve().parents[3]
_LIVE_GOLDEN_PATH = (
    Path(__file__).resolve().parents[2]
    / "distill"
    / "tests"
    / "goldens"
    / "memo_triage_live_corpus.json"
)


def _load_live_golden() -> dict:
    return json.loads(_LIVE_GOLDEN_PATH.read_text(encoding="utf-8"))


def test_live_corpus_promote_set_matches_golden():
    """Regression-pinning test (AC6): the memo.triage deterministic pre-filter,
    run READ-ONLY against this repo's real cross-repo/archive/ corpus, must
    match the checked-in golden fixture exactly — a silent classifier
    regression (or an unnoticed corpus-shape change) must fail LOUD here, with
    a diff-shaped message telling the maintainer exactly how to regenerate.

    This is a REGRESSION PIN, not a brittle content-equality snapshot: the
    live corpus (cross-repo/archive/) is a tracked, growing directory — new
    memos land there routinely as cross-repo coordination lands and gets
    archived, including from CONCURRENT sessions between this test's authoring
    and its next run. The promote-set is the load-bearing invariant this test
    exists to protect, and it is asserted with STRICT equality below — that
    must never silently relax.

    corpus_total and disqualified, by contrast, are INCIDENTAL telemetry that
    a benign, concurrently-mutated corpus is expected to change. Note this
    corpus mutates in BOTH directions: memos land as coordination archives,
    AND distill disposal deletes actioned memos en masse under PM approval
    (bd1a5cbb removed 175 in a single run). An earlier revision of this test
    asserted monotonic growth on both fields; that premise was false, and the
    first disposal ceremony after the golden was cut turned this test red for
    a reason that was not a defect. So: corpus_total is NOT pinned or floored
    at all, and the disqualified check is narrowed to golden entries STILL
    PRESENT on disk — a deleted memo cannot flip classification. A
    still-present memo flipping to promoted/unscored remains a genuine
    classifier regression and still fails loud. Only the promote-set is
    asserted with strict equality.

    HOW TO REGENERATE the golden fixture after a legitimate corpus change:

        python3 -c "
        import asyncio, json
        from pathlib import Path
        from coordinator_core.ops.memo_triage import _handler

        async def run():
            return await _handler({}, repo_root=Path('.git').resolve())

        result = asyncio.run(run())
        golden = {
            'note': 'GOLDEN regression-pinning fixture for memo.triage over the '
                    'LIVE cross-repo/archive corpus (AC6). See '
                    'coordinator_core/ops/tests/test_memo_triage.py::'
                    'test_live_corpus_promote_set_matches_golden for the '
                    'regeneration procedure.',
            'corpus_total': result['counts']['total'],
            'promote': result['promote'],
            'disqualified': result['disqualified'],
        }
        Path('coordinator_core/distill/tests/goldens/memo_triage_live_corpus.json'
        ).write_text(json.dumps(golden, indent=2, sort_keys=True) + chr(10))
        "

    Run from the repo root with CWD = repo root. Then MANUALLY REVIEW the
    diff on the regenerated golden before committing — a promote-set that
    silently grew/shrank by more than the count of memos you know landed
    since the last regeneration is exactly the classifier regression this
    test exists to catch; do not regenerate-and-commit reflexively.
    """
    golden = _load_live_golden()

    params: dict = {}
    result = _handler(params, repo_root=_REPO_ROOT / ".git")

    assert result["promote"] == golden["promote"], (
        "memo.triage promote-set drifted from the checked-in live-corpus "
        f"golden.\n  golden promote-set ({len(golden['promote'])}): "
        f"{golden['promote']}\n  actual promote-set "
        f"({len(result['promote'])}): {result['promote']}\n\n"
        "If this drift is EXPECTED (the cross-repo/archive/ corpus genuinely "
        "changed — new memos archived, memos actioned/swept, or a deliberate "
        "classifier tuning change), regenerate the golden fixture per the "
        "docstring on test_live_corpus_promote_set_matches_golden in this "
        "file, review the diff by hand, and commit the updated golden "
        "alongside your change. If this drift is UNEXPECTED, it is a "
        "classifier regression — do not regenerate the golden to paper over "
        "it."
    )
    # A golden memo that no longer EXISTS cannot have "flipped classification" —
    # it has nothing to be classified. Distill disposal (a PM-gated, recurring
    # ceremony) deletes actioned memos from this corpus en masse, so a golden
    # entry's absence from disk is an authorized deletion, not a regression.
    # Narrow the comparison to golden entries still present, which preserves the
    # real signal (a LIVE memo silently flipping to promoted/unscored) while
    # tolerating the deletions this corpus is designed to receive.
    archive_dir = _REPO_ROOT / "cross-repo" / "archive"
    golden_disqualified = set(golden["disqualified"])
    deleted_since_golden = {
        memo_id
        for memo_id in golden_disqualified
        if not (archive_dir / f"{memo_id}.md").is_file()
    }
    live_golden_disqualified = golden_disqualified - deleted_since_golden

    missing_disqualified = live_golden_disqualified - set(result["disqualified"])
    assert not missing_disqualified, (
        "memo.triage disqualified-set REGRESSED — a memo that is STILL PRESENT "
        "in cross-repo/archive/ and was previously known as already-captured "
        "(disqualified) is no longer disqualified in the actual run: "
        f"{sorted(missing_disqualified)}\n  golden disqualified-set "
        f"({len(golden_disqualified)}, of which {len(deleted_since_golden)} "
        f"deleted since): {sorted(live_golden_disqualified)}\n  actual "
        f"disqualified-set ({len(result['disqualified'])}): "
        f"{result['disqualified']}\n\n"
        "Entries deleted since the golden was cut are excluded from this "
        "comparison by design — see the block comment above. A still-present "
        "memo flipping classification is a genuine classifier regression; do "
        "NOT regenerate the golden to paper over it."
    )
    # NOTE: there is deliberately no `counts.total >= golden.corpus_total`
    # lower bound. This corpus is not monotonically growing: distill disposal
    # (bd1a5cbb deleted 175 actioned memos in one PM-approved run) is a
    # designed, recurring operation, so a growth-only floor on cross-repo/
    # archive/ fails on every ceremony rather than on any real defect. The
    # promote-set equality above and the still-present-memo subset check are
    # the load-bearing invariants; raw corpus size is not one.
    assert result["counts"]["total"] > 0, (
        "memo.triage reported an EMPTY cross-repo/archive/ corpus — this test "
        "is meaningless against nothing, and an empty result here more likely "
        "means an unreadable/mis-resolved scan surface than a genuinely empty "
        f"archive. degraded={result.get('degraded')!r}"
    )


# ---------------------------------------------------------------------------
# Unscannable scan surfaces — silent-success guard (silent-enumeration
# audit). Path.glob() silently swallows PermissionError even on a flat,
# non-recursive pattern (empirically re-verified: a chmod-000 dir yields an
# empty iterator from glob(), no exception) — an unreadable corpus/cross-
# check surface must not be indistinguishable from "genuinely nothing here",
# which would silently bias promotion decisions.
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    sys.platform == "win32" or (hasattr(os, "geteuid") and os.geteuid() == 0),
    reason="chmod 0o000 permission denial is not reliable on Windows or as root",
)
def test_unreadable_archive_dir_sets_degraded_not_silently_empty(tmp_path):
    archive_dir = tmp_path / "archive"
    archive_dir.mkdir(parents=True)
    _write_memo(archive_dir, "unreachable", title="Unreachable", decision="accepted")

    original_mode = archive_dir.stat().st_mode
    os.chmod(archive_dir, 0o000)
    try:
        records, degraded = _collect_memo_records(archive_dir)
    finally:
        os.chmod(archive_dir, original_mode)

    assert records == []
    assert degraded is True, "an unreadable archive dir must set degraded=True, not read as empty"


@pytest.mark.skipif(
    sys.platform == "win32" or (hasattr(os, "geteuid") and os.geteuid() == 0),
    reason="chmod 0o000 permission denial is not reliable on Windows or as root",
)
def test_unreadable_docs_decisions_sets_degraded(tmp_path):
    decisions_dir = tmp_path / "docs" / "decisions"
    decisions_dir.mkdir(parents=True)
    (decisions_dir / "DR-401-widget-ownership.md").write_text(
        "# DR-401 widget ownership\n\nSettled.\n", encoding="utf-8"
    )

    original_mode = decisions_dir.stat().st_mode
    os.chmod(decisions_dir, 0o000)
    try:
        corpus, degraded = _corpus_tokens_docs_decisions(tmp_path)
    finally:
        os.chmod(decisions_dir, original_mode)

    assert corpus == set()
    assert degraded is True


@pytest.mark.skipif(
    sys.platform == "win32" or (hasattr(os, "geteuid") and os.geteuid() == 0),
    reason="chmod 0o000 permission denial is not reliable on Windows or as root",
)
def test_unreadable_auto_memory_sets_degraded(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path / "dot-claude"))
    memory_dir = tmp_path / "dot-claude" / "projects" / "test-slug" / "memory"
    memory_dir.mkdir(parents=True)
    (memory_dir / "some-lesson.md").write_text("some lesson body\n", encoding="utf-8")

    original_mode = memory_dir.stat().st_mode
    os.chmod(memory_dir, 0o000)
    try:
        corpus, degraded = _corpus_tokens_auto_memory("test-slug")
    finally:
        os.chmod(memory_dir, original_mode)

    assert corpus == set()
    assert degraded is True


@pytest.mark.skipif(
    sys.platform == "win32" or (hasattr(os, "geteuid") and os.geteuid() == 0),
    reason="chmod 0o000 permission denial is not reliable on Windows or as root",
)
def test_handler_surfaces_degraded_true_when_archive_dir_unreadable(tmp_path):
    archive_dir = tmp_path / "cross-repo" / "archive"
    archive_dir.mkdir(parents=True)
    _write_memo(archive_dir, "unreachable", title="Unreachable", decision="accepted")

    original_mode = archive_dir.stat().st_mode
    os.chmod(archive_dir, 0o000)
    try:
        result = _handler({}, repo_root=tmp_path / ".git")
    finally:
        os.chmod(archive_dir, original_mode)

    assert result["degraded"] is True, (
        "an unreadable cross-repo/archive/ must surface degraded=True on the "
        f"handler result — got {result.get('degraded')!r}"
    )


def test_handler_degraded_false_on_clean_scan(tmp_path):
    result = _handler({}, repo_root=tmp_path / ".git")
    assert result["degraded"] is False
