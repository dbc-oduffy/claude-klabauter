"""Tests for coordinator_core.ops.docgen.dr_allocator (vendored from example-doctrine-repo SSOT).

Covers the allocation algorithm's edge cases against a scratch directory —
independent of the live oracle, unlike test_c6_conformance.py's byte-identity
harness. Each case here mirrors an edge case named in the vendored module's
own docstring/comments (empty/missing dir, gaps, highest-wins, malformed-name
tolerance, mixed-prefix fallback, width padding, explicit-prefix validation),
plus the `assert_dr_id_unique` collision-detection cases gained with the
vendored file.

Spec backlink: docs/plans/2026-07-21-strang-12-doc-generation-strangle.md § C6 (AC5)
Oracle: example-doctrine-repo `coordinator/bin/lib/dr_allocator.py` (fleet SSOT, vendored
verbatim @ sha 1a7989eb — see docs/decisions/DR-225).
"""

from __future__ import annotations

import pytest

from coordinator_core.ops.docgen.dr_allocator import (
    DrAllocatorError,
    DrCollisionError,
    allocate_dr_number,
    assert_dr_id_unique,
)


def _touch(directory, *names):
    for name in names:
        (directory / name).write_text("", encoding="utf-8")


def test_missing_dir_starts_at_001(tmp_path):
    missing = tmp_path / "does-not-exist"
    assert allocate_dr_number(missing) == "DR-001"


def test_empty_dir_starts_at_001(tmp_path):
    assert allocate_dr_number(tmp_path) == "DR-001"


def test_single_existing_dr_increments(tmp_path):
    _touch(tmp_path, "DR-207-deliverable-spine.md")
    assert allocate_dr_number(tmp_path) == "DR-208"


def test_gap_in_sequence_fills_max_plus_one_not_the_gap(tmp_path):
    # max+1, never backfilling a gap — matches the oracle's plain max() scan.
    _touch(tmp_path, "DR-001-a.md", "DR-003-b.md")
    assert allocate_dr_number(tmp_path) == "DR-004"


def test_highest_number_wins_regardless_of_listing_order(tmp_path):
    _touch(tmp_path, "DR-050-mid.md", "DR-005-low.md", "DR-223-high.md", "DR-099-other.md")
    assert allocate_dr_number(tmp_path) == "DR-224"


def test_non_md_files_ignored(tmp_path):
    _touch(tmp_path, "DR-010-real.md", "DR-999-not-markdown.txt")
    assert allocate_dr_number(tmp_path) == "DR-011"


def test_malformed_names_tolerated_not_rejected(tmp_path):
    # Retired DR-XXX placeholder shape: "XXX" isn't \d+, so it's silently
    # skipped rather than raising — this is the exact shape the collision
    # memo's fix retired, and old files bearing it must not crash allocation.
    _touch(
        tmp_path,
        "DR-XXX-placeholder.md",
        "DR-no-number-here.md",
        "not-a-dr-file.md",
        "DR-012-real.md",
    )
    assert allocate_dr_number(tmp_path) == "DR-013"


def test_mixed_prefixes_fall_back_to_unprefixed_namespace(tmp_path):
    # Records split across two prefixes plus an unprefixed one -> more than
    # one distinct prefix on disk -> safer default is the unprefixed
    # namespace, started fresh at 001, never guessing which prefix to extend.
    _touch(tmp_path, "DR-EXAMPLE-GAME-REPO-005-a.md", "DR-RAG-010-b.md")
    assert allocate_dr_number(tmp_path) == "DR-001"


def test_sole_shared_prefix_inferred_when_unambiguous(tmp_path):
    _touch(tmp_path, "DR-EXAMPLE-GAME-REPO-005-a.md", "DR-EXAMPLE-GAME-REPO-006-b.md")
    assert allocate_dr_number(tmp_path) == "DR-EXAMPLE-GAME-REPO-007"


def test_explicit_prefix_overrides_disk_inference(tmp_path):
    _touch(tmp_path, "DR-010-unprefixed.md")
    assert allocate_dr_number(tmp_path, explicit_prefix="example-game-repo") == "DR-EXAMPLE-GAME-REPO-001"


def test_explicit_prefix_scopes_independently_of_other_namespaces(tmp_path):
    _touch(tmp_path, "DR-EXAMPLE-GAME-REPO-005-a.md", "DR-RAG-010-b.md")
    assert allocate_dr_number(tmp_path, explicit_prefix="EXAMPLE-GAME-REPO") == "DR-EXAMPLE-GAME-REPO-006"


def test_explicit_prefix_lowercased_input_normalized_uppercase(tmp_path):
    assert allocate_dr_number(tmp_path, explicit_prefix="rag") == "DR-RAG-001"


@pytest.mark.parametrize("bad_prefix", ["1RAG", "-RAG", "RAG!", " ", "RAG SPACE", ""])
def test_invalid_explicit_prefix_rejected(tmp_path, bad_prefix):
    # "" is the regression case: `if explicit_prefix:` truthiness let an
    # explicitly-supplied empty prefix silently fall through to disk-inferred
    # namespace in both repos' old copies — fixed here as `is not None`.
    with pytest.raises(DrAllocatorError):
        allocate_dr_number(tmp_path, explicit_prefix=bad_prefix)


def test_width_padding_widens_to_widest_existing_in_namespace(tmp_path):
    # A 4-digit existing entry widens the next allocation to 4 digits too,
    # even though the minimum floor is 3.
    _touch(tmp_path, "DR-0999-wide.md")
    assert allocate_dr_number(tmp_path) == "DR-1000"


def test_width_floor_is_three_digits_even_for_small_numbers(tmp_path):
    _touch(tmp_path, "DR-7-single-digit.md")
    assert allocate_dr_number(tmp_path) == "DR-008"


def test_width_padding_is_per_namespace_not_global(tmp_path):
    # A wide unprefixed entry must not leak its width into a prefixed
    # namespace's own (narrower) allocation.
    _touch(tmp_path, "DR-0999-wide-unprefixed.md", "DR-EXAMPLE-GAME-REPO-005-a.md")
    assert allocate_dr_number(tmp_path, explicit_prefix="EXAMPLE-GAME-REPO") == "DR-EXAMPLE-GAME-REPO-006"


def test_returns_bare_id_no_suffix(tmp_path):
    assert allocate_dr_number(tmp_path) == "DR-001"


def test_assert_dr_id_unique_passes_for_unused_id(tmp_path):
    _touch(tmp_path, "DR-001-existing.md")
    assert_dr_id_unique(tmp_path, "DR-002")  # not raising is the pass condition


def test_assert_dr_id_unique_rejects_exact_duplicate(tmp_path):
    _touch(tmp_path, "DR-002-existing.md")
    with pytest.raises(DrCollisionError):
        assert_dr_id_unique(tmp_path, "DR-002")


def test_assert_dr_id_unique_rejects_numeric_equivalence_wide_on_disk(tmp_path):
    # DR-0002 already on disk, DR-002 freshly proposed -> same (prefix, number).
    _touch(tmp_path, "DR-0002-foo.md")
    with pytest.raises(DrCollisionError):
        assert_dr_id_unique(tmp_path, "DR-002")


def test_assert_dr_id_unique_rejects_numeric_equivalence_narrow_on_disk(tmp_path):
    # DR-002 already on disk, DR-0002 freshly proposed -> same (prefix, number).
    _touch(tmp_path, "DR-002-foo.md")
    with pytest.raises(DrCollisionError):
        assert_dr_id_unique(tmp_path, "DR-0002")


# ---------------------------------------------------------------------------
# Frontmatter-carried ids — example-cockpit-repo date-named-record regression
# ---------------------------------------------------------------------------
# Spec backlink: cross-repo/inbox/2026-08-01-example-cockpit-repo-em-dr-allocator-frontmatter-id-blindness.md


def _write_frontmatter_record(directory, filename, dr_id):
    (directory / filename).write_text(f"---\nid: {dr_id}\ntitle: stub\n---\n")


def test_frontmatter_carried_id_extends_max_past_filename_led_records(tmp_path):
    # Reproduces the cockpit scenario: 7 filename-led records DR-001..DR-007
    # plus a date-named record carrying id: DR-008 in frontmatter. The
    # allocator must see the frontmatter id and return DR-009, not DR-008.
    for n in range(1, 8):
        _touch(tmp_path, f"DR-{n:03d}-decision.md")
    _write_frontmatter_record(
        tmp_path, "2026-07-29-frontmatter-carried-record.md", "DR-008"
    )
    assert allocate_dr_number(tmp_path) == "DR-009"


def test_assert_unique_raises_on_frontmatter_carried_id_collision(tmp_path):
    for n in range(1, 8):
        _touch(tmp_path, f"DR-{n:03d}-decision.md")
    _write_frontmatter_record(
        tmp_path, "2026-07-29-frontmatter-carried-record.md", "DR-008"
    )
    with pytest.raises(DrCollisionError):
        assert_dr_id_unique(tmp_path, "DR-008")


def test_frontmatter_zero_padding_equivalence(tmp_path):
    # A frontmatter id: DR-0008 (wider zero-padding) must still collide with
    # a freshly-proposed DR-008 — same numeric-not-literal comparison as the
    # filename path.
    _write_frontmatter_record(
        tmp_path, "2026-07-29-frontmatter-carried-record.md", "DR-0008"
    )
    with pytest.raises(DrCollisionError):
        assert_dr_id_unique(tmp_path, "DR-008")


def test_frontmatter_prefixed_namespace_participates_in_inference(tmp_path):
    _touch(tmp_path, "DR-COCKPIT-001-a.md", "DR-COCKPIT-002-b.md")
    _write_frontmatter_record(
        tmp_path, "2026-07-29-frontmatter-carried-record.md", "DR-COCKPIT-004"
    )
    assert allocate_dr_number(tmp_path) == "DR-COCKPIT-005"


def test_date_named_record_with_no_id_frontmatter_ignored(tmp_path):
    # Regression guard for this repo's actual corpus shape: a date-named
    # record with a "title:" frontmatter field but no "id:" field (see
    # docs/decisions/2026-07-03-tri-plane-ownership-boundary.md) must not be
    # treated as a DR entry.
    _touch(tmp_path, "DR-001-first.md")
    (tmp_path / "2026-07-03-tri-plane-ownership-boundary.md").write_text(
        "---\ntitle: Tri-plane ownership boundary for fleet work-state\n"
        "status: accepted\n---\n\n# Tri-plane ownership boundary\n"
    )
    assert allocate_dr_number(tmp_path) == "DR-002"


def test_frontmatter_scan_tolerates_malformed_or_unreadable_file(tmp_path):
    _touch(tmp_path, "DR-001-first.md")
    # No frontmatter at all.
    (tmp_path / "2026-07-29-no-frontmatter.md").write_text("just body text\n")
    # id: value that doesn't parse as a DR id.
    (tmp_path / "2026-07-29-bad-id-value.md").write_text("---\nid: not-a-dr-id\n---\n")
    assert allocate_dr_number(tmp_path) == "DR-002"
    assert_dr_id_unique(tmp_path, "DR-002")  # must not raise
