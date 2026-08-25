"""test_directives_review_trail_range_termination — regression tests for
`coordinator_core.workstream_complete.directives_review`'s trail-range-
termination disbelief predicate (SKILL.md:556).

Root defect this pins (verified 2026-07-25, `work/machine-a/2026-07-21`):
`verify_trail_range_termination` had ZERO production callers, and every
on-disk trail record it was ever handed would fall through its `tip =
record.get("sha_range_tip") or record.get("tip")` lookup — every real
record carries `sha_range` only — so the function returned `False`
unconditionally regardless of input, without ever evaluating a genuine
termination relationship. A stale `<sha>..HEAD` record would (once wired)
have been treated identically to a legitimately-terminated one, because
neither ever reached the `is_ancestor` check.

Each test below is written so it fails if the corresponding production
logic were deleted or reverted to the old `sha_range_tip`/`tip`-only
lookup: assertions pin concrete `(tip, reason)` values and concrete
`True`/`False` predicate outcomes, never merely "no exception was raised".

Spec backlink: this defect / fix pair has no dedicated plan doc; the
originating incident is documented in `verify_trail_range_termination`'s
own docstring and in the dispatch brief that produced this fix
(2026-07-27, coordinator:executor dispatch on
`work/machine-b/2026-07-21to26`).
"""

from __future__ import annotations

from coordinator_core.workstream_complete.directives_review import (
    classify_untrusted_trail_ranges,
    resolve_trail_range_tip,
    verify_trail_range_termination,
)


# ---------------------------------------------------------------------------
# resolve_trail_range_tip — the real on-disk shape is the working path
# ---------------------------------------------------------------------------


def test_resolve_tip_from_real_on_disk_shape_terminated_range():
    """Regression pin for the field-mismatch bug: every record actually on
    disk (state/review-trail/*.json) carries `sha_range` only, no
    `sha_range_tip`/`tip` key. A concrete, terminated range must resolve a
    concrete tip through `sha_range` alone."""
    record = {
        "sha_range": "0227ea17..a4ef240b",
        "reviewer": "code-reviewer",
        "scope": "chain",
        "scope_kind": "diff",
        "session_id": "abc-123",
        "verdict": "ok",
        "diff_loc": 42,
        "workstream": "work/machine-a/2026-07-21",
    }
    tip, reason = resolve_trail_range_tip(record)
    assert tip == "a4ef240b"
    assert reason is None


def test_resolve_tip_from_real_on_disk_shape_caret_prefixed_start():
    record = {"sha_range": "0227ea17^..a4ef240b"}
    tip, reason = resolve_trail_range_tip(record)
    assert tip == "a4ef240b"
    assert reason is None


def test_resolve_tip_rejects_unterminated_head_range():
    """The exact stale shape from the 2026-07-25 incident: 8 records shaped
    like this produced VERDICT=COVERED while genuinely unreviewed commits
    landed after they were written."""
    record = {"sha_range": "0227ea17..HEAD"}
    tip, reason = resolve_trail_range_tip(record)
    assert tip is None
    assert reason is not None
    assert "HEAD" in reason


def test_resolve_tip_rejects_head_with_relative_suffix():
    record = {"sha_range": "0227ea17..HEAD~2"}
    tip, reason = resolve_trail_range_tip(record)
    assert tip is None
    assert "HEAD" in reason


def test_resolve_tip_rejects_dag_prefixed_range():
    record = {"sha_range": "dag:closing-handoff-segment"}
    tip, reason = resolve_trail_range_tip(record)
    assert tip is None
    assert "dag:" in reason


def test_resolve_tip_rejects_unparseable_range():
    record = {"sha_range": "not-a-range-at-all"}
    tip, reason = resolve_trail_range_tip(record)
    assert tip is None
    assert reason is not None


def test_resolve_tip_rejects_missing_sha_range():
    record = {"reviewer": "code-reviewer"}
    tip, reason = resolve_trail_range_tip(record)
    assert tip is None
    assert "missing sha_range" in reason


def test_resolve_tip_honors_explicit_tip_field_forward_compat():
    """A not-yet-observed future record shape carrying `sha_range_tip`/`tip`
    directly is honored ahead of `sha_range` parsing."""
    record = {"sha_range": "0227ea17..HEAD", "sha_range_tip": "a4ef240b"}
    tip, reason = resolve_trail_range_tip(record)
    assert tip == "a4ef240b"
    assert reason is None


def test_resolve_tip_rejects_explicit_head_tip_field():
    record = {"tip": "HEAD"}
    tip, reason = resolve_trail_range_tip(record)
    assert tip is None
    assert "HEAD" in reason


# ---------------------------------------------------------------------------
# verify_trail_range_termination — the disbelief predicate proper
# ---------------------------------------------------------------------------


def test_unterminated_head_record_does_not_confer_trust():
    records = [{"sha_range": "0227ea17..HEAD"}]
    trusted = verify_trail_range_termination(
        records, chain_tip_sha="deadbeef", is_ancestor=lambda a, b: True
    )
    assert trusted is False


def test_terminated_range_at_chain_tip_confers_trust():
    records = [{"sha_range": "0227ea17..deadbeef"}]
    trusted = verify_trail_range_termination(
        records, chain_tip_sha="deadbeef", is_ancestor=lambda a, b: False
    )
    assert trusted is True


def test_terminated_range_after_chain_tip_confers_trust_via_is_ancestor():
    calls: list[tuple[str, str]] = []

    def _is_ancestor(chain_tip: str, tip: str) -> bool:
        calls.append((chain_tip, tip))
        return chain_tip == "deadbeef" and tip == "newer-sha"

    records = [{"sha_range": "0227ea17..newer-sha"}]
    trusted = verify_trail_range_termination(
        records, chain_tip_sha="deadbeef", is_ancestor=_is_ancestor
    )
    assert trusted is True
    assert calls == [("deadbeef", "newer-sha")]


def test_stale_and_fresh_records_mixed_confers_trust_via_the_fresh_one():
    records = [
        {"sha_range": "0227ea17..HEAD"},
        {"sha_range": "abc123..dead12"},
        {"sha_range": "dag:some-segment"},
    ]
    trusted = verify_trail_range_termination(
        records, chain_tip_sha="dead12", is_ancestor=lambda a, b: False
    )
    assert trusted is True


def test_all_records_untrustworthy_confers_no_trust():
    records = [
        {"sha_range": "0227ea17..HEAD"},
        {"sha_range": "abc123..HEAD"},
        {"sha_range": "dag:some-segment"},
    ]
    trusted = verify_trail_range_termination(
        records, chain_tip_sha="deadbeef", is_ancestor=lambda a, b: True
    )
    assert trusted is False


def test_empty_record_set_confers_no_trust():
    """A non-vacuous negative: an empty input must resolve to False, not
    True by some accidental short-circuit."""
    trusted = verify_trail_range_termination(
        [], chain_tip_sha="deadbeef", is_ancestor=lambda a, b: True
    )
    assert trusted is False


# ---------------------------------------------------------------------------
# classify_untrusted_trail_ranges — the fail-loud diagnostic feed
# ---------------------------------------------------------------------------


def test_classify_names_every_rejected_record_and_reason():
    records = [
        {"sha_range": "0227ea17..HEAD"},
        {"sha_range": "abc123..deadbeef"},
        {"sha_range": "dag:some-segment"},
    ]
    rejected = classify_untrusted_trail_ranges(records)
    assert len(rejected) == 2
    reasons = [reason for _record, reason in rejected]
    assert any("HEAD" in reason for reason in reasons)
    assert any("dag:" in reason for reason in reasons)


def test_classify_empty_on_all_trustworthy_records():
    records = [{"sha_range": "abc123..deadbeef"}]
    assert classify_untrusted_trail_ranges(records) == []
