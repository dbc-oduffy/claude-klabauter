"""
Tests for coordinator_core.housekeeping.resolve — the blocker resolver
(plan chunk C5), rebuilt from `_resolve_blocker_deployment_state`'s 3,300 ms
full-corpus double-scan down to a live-corpus dict lookup, an archive-index
dict lookup, and an act-time re-read of at most a couple of files.

Covers: live-only resolution (zero I/O), archive-only resolution (act-time
re-read), the stale-index guard, both historical collapse-direction bugs
named in the plan body (a chain whose archived records sort after the live
head; a genuine post-collapse duplicate), the unresolved case, and the
5ms-per-call independent leg budget on the real-shaped corpus fixture (C1).

Spec backlink: docs/plans/2026-08-29-the-housekeeping-cycle-stops-committing.md
  § C5.

Negative-spec: this file does not test C3's live-corpus read or C4's archive
index mechanics on their own (test_corpus.py / test_archive_index.py own
those) — only what this module does with their outputs.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Dict, Iterable

import pytest

from coordinator_core.housekeeping.archive_index import build_index
from coordinator_core.housekeeping.resolve import (
    AMBIGUOUS_BLOCKER_SENTINEL,
    LEG_BUDGET_MS,
    make_resolver,
    resolve_blocker_id,
)
from coordinator_core.housekeeping.tests.corpus_fixture import build_corpus


def _write_record(path: Path, fields: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = ["---"]
    for key, value in fields.items():
        if value is None:
            continue
        lines.append(f"{key}: {value}")
    lines.append("---")
    lines.append("")
    lines.append("body\n")
    path.write_text("\n".join(lines), encoding="utf-8")


# ---------------------------------------------------------------------------
# live-only resolution — zero I/O
# ---------------------------------------------------------------------------


def test_live_match_resolves_without_any_archive_lookup(tmp_path):
    live_path = tmp_path / "state" / "handoffs" / "live.md"
    live_records = {
        live_path: {
            "handoff_id": "hnd-live",
            "stub_id": "sat-live",
            "deployment_state": "ready_to_fire",
        }
    }
    archive_dir = tmp_path / "archive" / "handoffs"
    archive_dir.mkdir(parents=True)
    index = build_index(archive_dir)

    def _reader_must_not_be_called(path, keys):
        raise AssertionError("archive re-read must not fire for a pure live hit")

    state = resolve_blocker_id(
        "sat-live", live_records, index, reader=_reader_must_not_be_called
    )

    assert state.resolved is True
    assert state.deployment_state == "ready_to_fire"


def test_unresolved_id_returns_unresolved_sentinel(tmp_path):
    archive_dir = tmp_path / "archive" / "handoffs"
    archive_dir.mkdir(parents=True)
    index = build_index(archive_dir)

    state = resolve_blocker_id("hnd-nowhere", {}, index)

    assert state.resolved is False
    assert state.deployment_state is None


# ---------------------------------------------------------------------------
# archive-only resolution — act-time re-read
# ---------------------------------------------------------------------------


def test_archive_match_re_reads_fresh_from_disk(tmp_path):
    archive_dir = tmp_path / "archive" / "handoffs"
    p = archive_dir / "rec.md"
    _write_record(p, {"handoff_id": "hnd-arch", "stub_id": "sat-arch", "deployment_state": "shipped"})
    index = build_index(archive_dir)

    calls = []

    def _counting_reader(path, keys):
        calls.append(path)
        from coordinator_core.housekeeping.head_scan import scan_keys

        return scan_keys(path, keys)

    state = resolve_blocker_id("sat-arch", {}, index, reader=_counting_reader)

    assert calls == [p]
    assert state.deployment_state == "shipped"


def test_stale_index_entry_is_dropped_not_trusted(tmp_path):
    """An index entry whose file has since changed id must not be trusted —
    contract 1's act-time re-read guards exactly this."""
    archive_dir = tmp_path / "archive" / "handoffs"
    p = archive_dir / "rec.md"
    _write_record(p, {"handoff_id": "hnd-old", "stub_id": "sat-old", "deployment_state": "shipped"})
    index = build_index(archive_dir)
    assert index.lookup("sat-old") == [p]

    # File mutated on disk after the index was built -- the index is stale
    # but has not yet been revalidated.
    _write_record(p, {"handoff_id": "hnd-new", "stub_id": "sat-new", "deployment_state": "closed"})

    state = resolve_blocker_id("sat-old", {}, index)

    assert state.resolved is False, "a stale index candidate must never be trusted as a match"


def test_blocker_id_matching_handoff_id_but_not_stub_id_does_not_resolve(tmp_path):
    """The exact bug this change fixes: `blocked_by` names `stub_id`, never
    `handoff_id` -- a blocker id that happens to equal a record's
    `handoff_id` but not its `stub_id` must not resolve."""
    archive_dir = tmp_path / "archive" / "handoffs"
    p = archive_dir / "rec.md"
    _write_record(p, {"handoff_id": "hnd-twin", "stub_id": "sat-twin", "deployment_state": "shipped"})
    index = build_index(archive_dir)

    state = resolve_blocker_id("hnd-twin", {}, index)

    assert state.resolved is False, (
        "a blocker id matching a record's handoff_id (not its stub_id) must not resolve"
    )


# ---------------------------------------------------------------------------
# Contract 2 — collapse to chain head BEFORE deciding, both historical
# failure directions.
# ---------------------------------------------------------------------------


def test_chain_whose_archived_predecessor_sorts_after_the_live_head(tmp_path):
    """Historical bug direction 1: a naive resolver that appends archive
    matches after live matches and takes "whichever came last" returns the
    SUPERSEDED archived predecessor instead of the live head. The archived
    predecessor is stamped deployment_state:continued -- collapse must drop
    it, leaving the live head as the sole survivor."""
    live_path = tmp_path / "state" / "handoffs" / "aaa-live.md"
    live_records = {
        live_path: {
            "handoff_id": "hnd-chain",
            "stub_id": "sat-chain",
            "deployment_state": "ready_to_fire",
        }
    }

    archive_dir = tmp_path / "archive" / "handoffs"
    predecessor_path = archive_dir / "zzz-archived-predecessor.md"
    _write_record(
        predecessor_path,
        {
            "handoff_id": "hnd-chain-predecessor",
            "stub_id": "sat-chain",
            "deployment_state": "continued",
            "continued_into": str(live_path),
        },
    )
    index = build_index(archive_dir)

    state = resolve_blocker_id("sat-chain", live_records, index)

    assert state.resolved is True
    assert state.deployment_state == "ready_to_fire", (
        "collapse must drop the superseded (continued) archived predecessor "
        "and resolve to the live chain head, not whichever candidate sorted last"
    )


def test_genuine_post_collapse_duplicate_is_ambiguous(tmp_path):
    """Historical bug direction 2: a real handoff_id collision (two
    unrelated records sharing an id, no supersession relationship between
    them) must still fail loud after collapsing -- collapse only removes
    records the chain has moved past, never resolves a genuine collision."""
    archive_dir = tmp_path / "archive" / "handoffs"
    p1 = archive_dir / "dup-1.md"
    p2 = archive_dir / "dup-2.md"
    _write_record(p1, {"handoff_id": "hnd-dup-1", "stub_id": "sat-dup", "deployment_state": "ready_to_fire"})
    _write_record(p2, {"handoff_id": "hnd-dup-2", "stub_id": "sat-dup", "deployment_state": "shipped"})
    index = build_index(archive_dir)
    assert sorted(index.lookup("sat-dup")) == sorted([p1, p2])

    state = resolve_blocker_id("sat-dup", {}, index)

    assert state.resolved is True
    assert state.deployment_state == AMBIGUOUS_BLOCKER_SENTINEL


# ---------------------------------------------------------------------------
# make_resolver — the bound closure shape C6 consumes
# ---------------------------------------------------------------------------


def test_make_resolver_returns_a_bound_closure(tmp_path):
    live_path = tmp_path / "state" / "handoffs" / "live.md"
    live_records = {live_path: {"handoff_id": "hnd-x", "stub_id": "sat-x", "deployment_state": "closed"}}
    archive_dir = tmp_path / "archive" / "handoffs"
    archive_dir.mkdir(parents=True)
    index = build_index(archive_dir)

    resolve = make_resolver(live_records, index)

    assert resolve("sat-x").deployment_state == "closed"
    assert resolve("hnd-nope").resolved is False


# ---------------------------------------------------------------------------
# Leg budget — 5ms independent, per gate clear, on the real-shaped corpus.
# ---------------------------------------------------------------------------

_N_OUTER = 5
_K_INNER = 20
"""Windows' `time.process_time()` tick-quantises at ~15.6ms -- batching
K_INNER calls inside one process_time() bracket and dividing amortises the
tick away, matching this package's other leg-budget tests' own convention."""


@pytest.fixture(scope="module")
def scaled_fixture(tmp_path_factory):
    root = tmp_path_factory.mktemp("resolve_bench")
    fixture = build_corpus(root)
    index = build_index(fixture.archive_dir)
    return fixture, index


def test_resolve_leg_budget_on_full_scale_corpus(scaled_fixture):
    """Leg budget, asserted independently (chunk C5 body): 5ms per gate
    clear -- a dict lookup plus 1-2 file head-scans, never a corpus walk,
    measured against the ~250-live/~1,470-archived real-shaped fixture."""
    fixture, index = scaled_fixture
    live_records = {
        rec["path"]: {
            "handoff_id": rec["handoff_id"],
            "stub_id": rec["stub_id"],
            "deployment_state": rec["deployment_state"],
        }
        for rec in fixture.live_records
    }
    # Resolve by stub_id: that is what a gate's `blocked_by` names, and
    # what the archive index keys on. Its handoff_id resolves to nothing.
    archived_id = fixture.archived_records[0]["stub_id"]

    resolve = make_resolver(live_records, index)
    # Warm the OS page cache before measuring.
    resolve(archived_id)

    samples_ms = []
    for _ in range(_N_OUTER):
        start = time.process_time()
        for _inner in range(_K_INNER):
            state = resolve(archived_id)
            assert state.resolved is True
        elapsed_ms = (time.process_time() - start) * 1000.0 / _K_INNER
        samples_ms.append(elapsed_ms)

    samples_ms.sort()
    median_ms = samples_ms[len(samples_ms) // 2]
    assert median_ms <= LEG_BUDGET_MS, (
        f"resolve() leg median {median_ms:.2f}ms exceeded the independent "
        f"{LEG_BUDGET_MS}ms budget (samples: {samples_ms}ms)"
    )
