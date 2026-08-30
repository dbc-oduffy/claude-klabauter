"""
coordinator_core.housekeeping.tests.test_corpus_fixture — the fixture's own
self-test.

Purpose: "test_corpus_fixture.py asserts the fixture's own shape -- counts
per deployment_state, that the archive is populated both nested and at root,
that .archive/ decoys exist, and above all that at least one gate CLEARS. A
fixture that cannot see a gate clear cannot measure the criterion, so this
assertion is the chunk's real deliverable" (plan chunk C1, verbatim).

Spec backlink: docs/plans/2026-08-29-the-housekeeping-cycle-stops-committing.md
  § C1.

Negative-spec: this file does NOT test the production housekeeping cycle
(coordinator_core/housekeeping/cycle.py, chunk C6c, not yet built) -- it
tests only that `corpus_fixture.build_corpus` produces the corpus shape the
plan's prime_exit_criterion requires the cycle to be measured against.
"""

from __future__ import annotations

import os
from collections import Counter
from pathlib import Path

import pytest

from coordinator_core.housekeeping.tests.corpus_fixture import (
    ARCHIVE_TOTAL_TARGET,
    DECOY_COUNT,
    LIVE_STATE_COUNTS,
    TERMINAL_STATES,
    TOTAL_LIVE,
    build_corpus,
    gate_clears,
    scan_archive_recursive,
    scan_live_non_recursive,
)


@pytest.fixture(scope="module")
def fixture(tmp_path_factory):
    root = tmp_path_factory.mktemp("housekeeping_corpus_fixture")
    return build_corpus(root)


def test_live_record_count_and_non_recursive_scan(fixture):
    """~250 live records, directly under state/handoffs/*.md, NON-recursive
    -- a scan of the live dir must return exactly TOTAL_LIVE entries, never
    more (which would mean it descended into .archive/)."""
    assert len(fixture.live_records) == TOTAL_LIVE == 249

    scanned = scan_live_non_recursive(fixture.live_dir)
    assert len(scanned) == TOTAL_LIVE, (
        f"non-recursive live scan found {len(scanned)} files, expected {TOTAL_LIVE} -- "
        "either the fixture under- or over-produced live records, or the scan "
        "descended into a subdirectory it should not have"
    )


def test_deployment_state_distribution_matches_the_real_corpus(fixture):
    """Per-state counts on the ~249 live records match LIVE_STATE_COUNTS
    exactly -- the real corpus's distribution the plan's prime_exit_criterion
    names verbatim."""
    counts = Counter(rec["deployment_state"] for rec in fixture.live_records)
    assert dict(counts) == LIVE_STATE_COUNTS

    assert counts["awaiting_gate"] >= 17


def test_deployment_state_present_on_every_record(fixture):
    """deployment_state must appear in EVERY record's frontmatter -- live,
    decoy and archived alike (prime_exit_criterion: "deployment_state
    present on every record")."""
    all_records = fixture.live_records + fixture.decoy_records + fixture.archived_records
    assert all_records, "fixture produced no records at all"
    for rec in all_records:
        text = rec["path"].read_text(encoding="utf-8")
        assert "deployment_state:" in text, f"{rec['path']} is missing deployment_state"
        assert rec["deployment_state"], f"{rec['path']} has an empty deployment_state"


def test_archive_decoys_exist_and_are_never_descended_into(fixture):
    """state/handoffs/.archive/ holds decoy records (contract 7) -- present
    on disk, but excluded by the non-recursive live scan."""
    assert fixture.live_archive_decoy_dir.is_dir()
    assert len(fixture.decoy_records) == DECOY_COUNT
    for rec in fixture.decoy_records:
        assert rec["path"].is_file()

    scanned_live = {p.name for p in scan_live_non_recursive(fixture.live_dir)}
    decoy_names = {rec["path"].name for rec in fixture.decoy_records}
    assert not (scanned_live & decoy_names), (
        "a non-recursive live scan picked up a decoy record from .archive/ -- "
        "the scan is descending where contract 7 forbids it"
    )


def test_archive_populated_both_nested_and_at_root(fixture):
    """~1,470 archived records (10% headroom either side of the target),
    distributed across YYYY-MM/ subdirectories AND directly at the archive
    root -- the root-level case is real in the live corpus and a glob of
    archive/handoffs/*/*.md misses it (this chunk's own instruction)."""
    assert len(fixture.archived_records) == pytest.approx(ARCHIVE_TOTAL_TARGET, rel=0.05)

    root_level = [
        rec for rec in fixture.archived_records
        if rec["path"].parent == fixture.archive_dir
    ]
    nested = [
        rec for rec in fixture.archived_records
        if rec["path"].parent != fixture.archive_dir
    ]
    assert root_level, "no archived records sit directly at archive/handoffs/ -- the root-level case is missing"
    assert nested, "no archived records sit under a YYYY-MM/ subdirectory"

    # Every nested parent dir name must look like YYYY-MM.
    for rec in nested:
        month = rec["path"].parent.name
        assert len(month) == 7 and month[4] == "-" and month[:4].isdigit() and month[5:].isdigit(), (
            f"archived record {rec['path']} sits under a non-YYYY-MM directory: {month!r}"
        )


def test_archive_recursive_scan_finds_both_shapes(fixture):
    """A recursive scan over archive/handoffs/ must find every archived
    record regardless of whether it is nested or root-level."""
    scanned = scan_archive_recursive(fixture.archive_dir)
    assert len(scanned) == len(fixture.archived_records)
    scanned_set = {str(p) for p in scanned}
    for rec in fixture.archived_records:
        assert str(rec["path"]) in scanned_set


def test_at_least_one_gate_clears(fixture):
    """The chunk's real deliverable: at least one awaiting_gate record's gate
    genuinely clears -- resolved via `gate_clears`, re-reading the record's
    gate_blocker_id from disk and checking it against a terminal record,
    never assumed from the fixture's own bookkeeping alone."""
    assert gate_clears(fixture, fixture.clearing_record_id) is True, (
        f"the fixture's designated clearing record {fixture.clearing_record_id!r} "
        "did not clear its gate when independently re-resolved -- a fixture "
        "that cannot see a gate clear cannot measure the criterion"
    )


def test_gate_clearing_is_not_vacuous(fixture):
    """Not every awaiting_gate record clears -- if the assertion above passed
    only because every awaiting_gate record clears trivially, it would prove
    nothing about the discriminating (blocked-vs-cleared) case the cycle
    must actually handle."""
    other_awaiting = [
        rec for rec in fixture.live_records
        if rec["deployment_state"] == "awaiting_gate" and rec["handoff_id"] != fixture.clearing_record_id
    ]
    assert other_awaiting, "fixture has no other awaiting_gate records to contrast against"

    non_clearing = [rec for rec in other_awaiting if not gate_clears(fixture, rec["handoff_id"])]
    assert non_clearing, (
        "every other awaiting_gate record also cleared its gate -- the fixture's "
        "'at least one clears' assertion is vacuous, not discriminating"
    )
    assert len(non_clearing) == len(other_awaiting), (
        "some non-designated awaiting_gate records unexpectedly cleared -- "
        "the bogus blocker id must resolve to nothing"
    )


def test_clearing_blocker_is_terminal(fixture):
    """The clearing record's blocker resolves to a record whose
    deployment_state is one of the four terminal states (contract 4)."""
    by_id = fixture.records_by_stub_id()
    blocker = by_id.get(fixture.clearing_blocker_id)
    assert blocker is not None, "the fixture's clearing_blocker_id does not resolve to any record"
    assert blocker["deployment_state"] in TERMINAL_STATES


def test_fixture_is_deterministic_for_a_fixed_seed(tmp_path_factory):
    """Two builds with the same seed produce the same corpus shape -- a
    flaky fixture would make every downstream leg-budget measurement
    unreproducible."""
    root_a = tmp_path_factory.mktemp("housekeeping_corpus_fixture_det_a")
    root_b = tmp_path_factory.mktemp("housekeeping_corpus_fixture_det_b")
    fixture_a = build_corpus(root_a, seed=12345)
    fixture_b = build_corpus(root_b, seed=12345)

    assert len(fixture_a.live_records) == len(fixture_b.live_records)
    assert len(fixture_a.archived_records) == len(fixture_b.archived_records)
    assert fixture_a.clearing_record_id == fixture_b.clearing_record_id
    assert fixture_a.clearing_blocker_id == fixture_b.clearing_blocker_id
    assert Counter(r["deployment_state"] for r in fixture_a.live_records) == Counter(
        r["deployment_state"] for r in fixture_b.live_records
    )
