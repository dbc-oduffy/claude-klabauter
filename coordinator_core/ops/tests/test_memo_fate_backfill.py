
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

pytestmark = [
    pytest.mark.spawns_process,
    pytest.mark.cadence,
]


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
@pytest.mark.skipif(
    hasattr(os, "geteuid") and os.geteuid() == 0,
    reason="root bypasses POSIX permission bits -- chmod 0o000 does not make a file unreadable to root",
)
def test_backfill_unreadable_memo_surfaces_read_error_not_silently_dropped(tmp_path: Path):
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
    assert outcome["counts"]["total"] == 1
    assert outcome["degraded"] is False
