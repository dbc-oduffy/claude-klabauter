"""
Tests for coordinator_core.housekeeping.corpus — Step A of the housekeeping
pseudocode, the ONE live-corpus read (plan chunk C3).

Covers contract 7's scan-root split (live non-recursive, archive recursive,
the `.archive/` decoy never descended into), the PermissionError-as-gap
distinguishability requirement, the exactly-one-read-per-record assertion,
and the 20 ms leg budget measured against the real-shaped fixture corpus.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any, Dict, Iterable

import pytest

from coordinator_core.housekeeping import corpus
from coordinator_core.housekeeping.tests.corpus_fixture import (
    TOTAL_LIVE,
    build_corpus,
)


# ---------------------------------------------------------------------------
# Scan roots: live is non-recursive and never descends into .archive/
# ---------------------------------------------------------------------------


def test_list_live_handoffs_is_non_recursive_and_skips_archive_decoys(tmp_path):
    fixture = build_corpus(tmp_path)

    paths, gaps = corpus.list_live_handoffs(fixture.live_dir)

    assert gaps == []
    assert len(paths) == TOTAL_LIVE
    for p in paths:
        assert p.parent == fixture.live_dir
    decoy_names = {rec["path"].name for rec in fixture.decoy_records}
    assert not any(p.name in decoy_names for p in paths)


def test_list_archived_handoffs_covers_nested_and_root_records(tmp_path):
    fixture = build_corpus(tmp_path)

    paths, gaps = corpus.list_archived_handoffs(fixture.archive_dir)

    assert gaps == []
    assert len(paths) == len(fixture.archived_records)
    nested = [p for p in paths if p.parent != fixture.archive_dir]
    root_level = [p for p in paths if p.parent == fixture.archive_dir]
    assert nested, "fixture's month-nested archive records were not found"
    assert root_level, "fixture's root-level archive records were not found"


# ---------------------------------------------------------------------------
# PermissionError surfaces as a distinguishable scan gap, never an absence
# ---------------------------------------------------------------------------


def test_permission_error_on_live_dir_is_a_distinguishable_gap(tmp_path, monkeypatch):
    fixture = build_corpus(tmp_path)

    def _raise_scandir(_path):
        raise PermissionError(13, "permission denied")

    monkeypatch.setattr(corpus.os, "scandir", _raise_scandir)

    paths, gaps = corpus.list_live_handoffs(fixture.live_dir)

    assert paths == []
    assert len(gaps) == 1
    assert "permission denied" in gaps[0]


def test_permission_error_gap_is_distinct_from_a_genuinely_empty_dir(tmp_path, monkeypatch):
    empty_dir = tmp_path / "empty_live"
    empty_dir.mkdir()

    empty_paths, empty_gaps = corpus.list_live_handoffs(empty_dir)
    assert empty_paths == []
    assert empty_gaps == []

    def _raise_scandir(_path):
        raise PermissionError(13, "permission denied")

    monkeypatch.setattr(corpus.os, "scandir", _raise_scandir)
    denied_paths, denied_gaps = corpus.list_live_handoffs(empty_dir)

    assert denied_paths == []
    assert denied_gaps != []
    # The two "empty paths" results are reached through observably different
    # states -- an empty gaps list vs a non-empty one -- so a caller can
    # always tell "nothing here" from "could not look".
    assert empty_gaps != denied_gaps


def test_permission_error_on_archive_walk_is_a_distinguishable_gap(tmp_path):
    fixture = build_corpus(tmp_path)

    real_walk = corpus.os.walk

    def _walk_with_injected_error(top, onerror=None, **kwargs):
        if onerror is not None:
            onerror(PermissionError(13, "permission denied", str(top)))
        return real_walk(top, onerror=onerror, **kwargs)

    orig_walk = corpus.os.walk
    corpus.os.walk = _walk_with_injected_error
    try:
        paths, gaps = corpus.list_archived_handoffs(fixture.archive_dir)
    finally:
        corpus.os.walk = orig_walk

    assert len(gaps) == 1
    assert "permission denied" in gaps[0]
    # A partial gap does not suppress the records that WERE listed.
    assert len(paths) == len(fixture.archived_records)


# ---------------------------------------------------------------------------
# Step A: read_live_corpus — one read per record, correct fields, budget
# ---------------------------------------------------------------------------


def test_read_live_corpus_returns_every_live_record_with_requested_keys(tmp_path):
    fixture = build_corpus(tmp_path)

    result = corpus.read_live_corpus(fixture.live_dir)

    assert len(result.records) == TOTAL_LIVE
    assert result.scan_gaps == []

    by_path = {rec["path"]: rec for rec in fixture.live_records}
    for path, fields in result.records.items():
        expected = by_path[path]
        assert fields.get("handoff_id") == expected["handoff_id"]
        assert fields.get("deployment_state") == expected["deployment_state"]


def test_read_live_corpus_never_includes_archive_decoys(tmp_path):
    fixture = build_corpus(tmp_path)

    result = corpus.read_live_corpus(fixture.live_dir)

    decoy_ids = {rec["handoff_id"] for rec in fixture.decoy_records}
    found_ids = {fields.get("handoff_id") for fields in result.records.values()}
    assert not (decoy_ids & found_ids)


def test_read_live_corpus_read_count_is_exactly_one_per_record(tmp_path):
    fixture = build_corpus(tmp_path)

    calls = 0

    def _counting_reader(path: Any, keys: Iterable[str]) -> Dict[str, Any]:
        nonlocal calls
        calls += 1
        return corpus.scan_keys(path, keys)

    result = corpus.read_live_corpus(fixture.live_dir, reader=_counting_reader)

    assert calls == TOTAL_LIVE
    assert result.read_count == TOTAL_LIVE
    # Re-reading the same corpus a second time in the same process must not
    # reuse or double-count the first call's reads -- each call is its own
    # single pass, matching "nothing else re-reads it" for THIS cycle.
    second = corpus.read_live_corpus(fixture.live_dir, reader=_counting_reader)
    assert second.read_count == TOTAL_LIVE
    assert calls == TOTAL_LIVE * 2


def test_read_live_corpus_partial_listing_gap_does_not_suppress_readable_records(
    tmp_path, monkeypatch
):
    fixture = build_corpus(tmp_path)

    real_next = next
    real_scandir = corpus.os.scandir

    def _scandir_then_fail(path):
        it = real_scandir(path)

        class _FailingIter:
            def __init__(self, inner):
                self._inner = inner
                self._count = 0

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                self._inner.close()
                return False

            def __iter__(self):
                return self

            def __next__(self):
                self._count += 1
                if self._count > 5:
                    raise PermissionError(13, "permission denied mid-listing")
                return real_next(self._inner)

        return _FailingIter(it)

    monkeypatch.setattr(corpus.os, "scandir", _scandir_then_fail)

    result = corpus.read_live_corpus(fixture.live_dir)

    assert result.scan_gaps != []
    assert 0 < len(result.records) <= 5


_N_OUTER = 5
_K_INNER = 20
"""Windows' `time.process_time()` tick-quantises at ~15.6ms -- a single
un-batched call reads as a full quantised tick or two by pure rounding
noise, which fails a real, in-budget implementation. Batching K_INNER calls
inside one process_time() bracket and dividing amortises the tick away,
matching test_archive_index.py :: test_revalidate_leg_budget_on_full_scale_
corpus's own convention in this same package."""


@pytest.mark.skipif(
    os.environ.get("CLAUDE_KLABAUTER_SKIP_TIMING_TESTS") == "1",
    reason="timing budgets are unreliable on a loaded shared box",
)
def test_read_live_corpus_leg_budget(tmp_path):
    fixture = build_corpus(tmp_path)

    # Warm the OS page cache / directory entries before measuring, matching
    # the plan's own convention that a cold first pass is not what the
    # budget governs.
    corpus.read_live_corpus(fixture.live_dir)

    samples_ms = []
    for _ in range(_N_OUTER):
        start = time.process_time()
        for _inner in range(_K_INNER):
            result = corpus.read_live_corpus(fixture.live_dir)
            assert len(result.records) == TOTAL_LIVE
        elapsed_ms = (time.process_time() - start) * 1000.0 / _K_INNER
        samples_ms.append(elapsed_ms)

    samples_ms.sort()
    median_ms = samples_ms[len(samples_ms) // 2]
    assert median_ms <= corpus.LEG_BUDGET_MS, (
        f"live corpus read median {median_ms:.2f}ms exceeded the "
        f"{corpus.LEG_BUDGET_MS}ms budget over {_N_OUTER}x{_K_INNER} reps "
        f"(samples: {samples_ms}ms)"
    )
