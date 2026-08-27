"""
coordinator_core.ops.tests.test_memo_fate_backfill

Unit tests for coordinator_core.ops.memo_fate_backfill (ruling (a) of the
2026-08-06 cross-repo ask, item 1).

Coverage:
  derive_fate:
    (a) decision in {noop, fyi-ack} -> ephemeral
    (b) decision=accepted + resolvable realized_by (inline) -> commitment
    (c) decision=accepted + unresolvable realized_by -> quarantined
    (d) decision=accepted + absent realized_by -> quarantined
    (e) decision absent -> quarantined
    (f) decision=partial/declined/superseded -> quarantined (not silently
        mapped to ratification — no confirmed vocabulary for that rule)
    (g) each malformed prose-fragment literal from the source ask is
        quarantined, never coerced or crashed on
  backfill_fates (integration over collect_memo_records):
    (h) already-stamped memo -> skipped_already_stamped, not re-derived
    (i) counts partition invariant: every record lands in exactly one bucket
    (j) quarantined set is never truncated/capped

Spec backlink: cross-repo/inbox/2026-08-06-example-retrieval-repo-em-distill-fate-coverage-and-legacy-log-reader.md § 1(a)
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from coordinator_core.ops.memo_fate_backfill import (
    backfill_fates,
    collect_memo_records,
    derive_fate,
)

# Spawns a real external process; runs at cadence gates, not per-commit.
# Spawn ratchet: coordinator_core/tests/test_no_new_spawning_tests.py
pytestmark = [
    pytest.mark.spawns_process,
    pytest.mark.cadence,
]

# ---------------------------------------------------------------------------
# derive_fate
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("decision", ["noop", "fyi-ack"])
def test_derive_fate_ephemeral_decisions(decision, tmp_path: Path):
    fate, reason = derive_fate({"decision": decision}, tmp_path)
    assert fate == "ephemeral"
    assert decision in reason


def test_derive_fate_accepted_resolvable_realized_by_commitment(tmp_path: Path):
    fate, reason = derive_fate({"decision": "accepted", "realized_by": "inline"}, tmp_path)
    assert fate == "commitment"


def test_derive_fate_accepted_unresolvable_realized_by_quarantined(tmp_path: Path):
    fate, reason = derive_fate(
        {"decision": "accepted", "realized_by": "docs/does-not-exist.md"}, tmp_path
    )
    assert fate is None
    assert "does not resolve" in reason


def test_derive_fate_accepted_absent_realized_by_quarantined(tmp_path: Path):
    fate, reason = derive_fate({"decision": "accepted"}, tmp_path)
    assert fate is None
    assert "realized_by absent" in reason


def test_derive_fate_decision_absent_quarantined(tmp_path: Path):
    fate, reason = derive_fate({}, tmp_path)
    assert fate is None
    assert "decision absent" in reason


@pytest.mark.parametrize("decision", ["partial", "declined", "superseded"])
def test_derive_fate_boundary_shape_decisions_quarantined_not_guessed(decision, tmp_path: Path):
    # No confirmed decision:-value vocabulary for "boundary/shape ruling" ->
    # never coerced into ratification; quarantined for a human read instead.
    fate, reason = derive_fate({"decision": decision}, tmp_path)
    assert fate is None
    assert "outside the closed backfill mapping" in reason


@pytest.mark.parametrize(
    "malformed",
    ['"Seam', '"Both', '"Diagnosis', '"Adopted', '"Consumed,', '"Fixed,'],
)
def test_derive_fate_malformed_literals_quarantined(malformed, tmp_path: Path):
    fate, reason = derive_fate({"decision": malformed}, tmp_path)
    assert fate is None
    assert "outside the closed backfill mapping" in reason


# ---------------------------------------------------------------------------
# collect_memo_records / backfill_fates — integration
# ---------------------------------------------------------------------------


def _write_memo(archive_dir: Path, name: str, body: str) -> None:
    archive_dir.mkdir(parents=True, exist_ok=True)
    (archive_dir / name).write_text(body, encoding="utf-8")


def test_backfill_already_stamped_memo_is_skipped(tmp_path: Path):
    archive_dir = tmp_path / "cross-repo" / "archive"
    _write_memo(
        archive_dir,
        "stamped.md",
        "---\nfrom: a\nto: b\ndistill_fate: ratification\n---\nbody\n",
    )
    records, degraded, read_errors = collect_memo_records(archive_dir, tmp_path)
    outcome = backfill_fates(records, worktree_root=tmp_path, degraded=degraded, read_errors=read_errors)
    assert outcome["skipped_already_stamped"] == [
        {"memo_id": "stamped", "path": "cross-repo/archive/stamped.md", "distill_fate": "ratification"}
    ]
    assert outcome["counts"]["total"] == 1
    assert outcome["quarantined"] == []


def test_backfill_counts_partition_invariant(tmp_path: Path):
    archive_dir = tmp_path / "cross-repo" / "archive"
    _write_memo(archive_dir, "eph.md", "---\nfrom: a\nto: b\ndecision: noop\n---\nbody\n")
    _write_memo(
        archive_dir,
        "commit.md",
        "---\nfrom: a\nto: b\ndecision: accepted\nrealized_by: inline\n---\nbody\n",
    )
    _write_memo(archive_dir, "quarantine.md", '---\nfrom: a\nto: b\ndecision: mystery-value\n---\nbody\n')
    _write_memo(
        archive_dir,
        "stamped.md",
        "---\nfrom: a\nto: b\ndistill_fate: ephemeral\n---\nbody\n",
    )

    records, degraded, read_errors = collect_memo_records(archive_dir, tmp_path)
    outcome = backfill_fates(records, worktree_root=tmp_path, degraded=degraded, read_errors=read_errors)

    c = outcome["counts"]
    assert c["total"] == 4
    assert (
        c["derived_ephemeral"] + c["derived_commitment"] + c["quarantined"] + c["skipped_already_stamped"]
        == c["total"]
    )
    assert c["derived_ephemeral"] == 1
    assert c["derived_commitment"] == 1
    assert c["quarantined"] == 1
    assert c["skipped_already_stamped"] == 1
    assert len(outcome["quarantined"]) == 1
    assert outcome["quarantined"][0]["memo_id"] == "quarantine"


def test_backfill_quarantined_set_never_truncated(tmp_path: Path):
    archive_dir = tmp_path / "cross-repo" / "archive"
    for i in range(25):
        _write_memo(
            archive_dir,
            f"quarantine-{i:02d}.md",
            f"---\nfrom: a\nto: b\ndecision: mystery-{i}\n---\nbody\n",
        )
    records, degraded, read_errors = collect_memo_records(archive_dir, tmp_path)
    outcome = backfill_fates(records, worktree_root=tmp_path, degraded=degraded, read_errors=read_errors)
    assert len(outcome["quarantined"]) == 25
    assert outcome["counts"]["quarantined"] == 25


@pytest.mark.skipif(sys.platform == "win32", reason="chmod-based unreadable-file simulation is POSIX-only")
def test_backfill_unreadable_memo_surfaces_read_error_not_silently_dropped(tmp_path: Path):
    # Review: code-reviewer Finding (2026-08-06) — a per-file OSError used to
    # `continue` with only a stderr log; the memo vanished from the corpus
    # with zero trace in the returned outcome. Pins that it now surfaces via
    # `read_errors`, distinct from (and never folded into) the four-way
    # counts partition — this op's purpose is surfacing candidates for human
    # review, so a silently-dropped memo is itself a review-visibility gap.
    archive_dir = tmp_path / "cross-repo" / "archive"
    _write_memo(archive_dir, "readable.md", "---\nfrom: a\nto: b\ndecision: noop\n---\nbody\n")
    unreadable = archive_dir / "unreadable.md"
    unreadable.write_text("---\nfrom: a\nto: b\ndecision: noop\n---\nbody\n", encoding="utf-8")
    os.chmod(unreadable, 0o000)
    try:
        records, degraded, read_errors = collect_memo_records(archive_dir, tmp_path)
        outcome = backfill_fates(
            records, worktree_root=tmp_path, degraded=degraded, read_errors=read_errors
        )
    finally:
        os.chmod(unreadable, 0o644)

    assert len(outcome["read_errors"]) == 1
    assert outcome["read_errors"][0]["path"] == "cross-repo/archive/unreadable.md"
    assert "PermissionError" in outcome["read_errors"][0]["reason"]
    assert outcome["counts"]["read_errors"] == 1
    # The readable memo still made it through — only the unreadable one dropped.
    assert outcome["counts"]["total"] == 1
    assert outcome["degraded"] is False
