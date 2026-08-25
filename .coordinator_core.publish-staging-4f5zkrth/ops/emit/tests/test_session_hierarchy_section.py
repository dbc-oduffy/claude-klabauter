"""Unit tests for session_hierarchy.collect() — unreadable-state-dir silent-success guard,
and the session_id-uniqueness quarantine guard.

Pins the fix for the state/audits/2026-07-22 silent-success audit: ``glob.glob()``'s
selector silently swallows ``PermissionError`` while walking (an unreadable dir yields an
empty match list, no exception), which previously made a permission-denied
``central_state_root`` indistinguishable from "no session-hierarchy files exist here" —
both collapsed to the same graceful-absent ``([], [])`` shape. ``collect()`` now probes
the dir via ``os.scandir`` before trusting the glob and routes a scan failure into the
malformed bucket instead.

Also pins the b8a8339a duplicate-``session_id`` fix (reported in
cross-repo/inbox/2026-07-26-project-opticon-em-makima-duplicate-session-hierarchy-entries.md):
``session_id`` is the natural key of ``session_hierarchies`` and must be unique within a
single emission; the first-admitted entry wins and every later duplicate is quarantined into
``malformed``, whether the duplicate lives in the same source file or a different one, and
without shadowing an entry's own pre-existing validation failure.

Spec backlink: pln-tc-3-emission-stack-python-por-c9595b § P16
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from coordinator_core.ops.emit.context import EmitContext
from coordinator_core.ops.emit.sections import session_hierarchy


def _make_ctx(central_state_root: Path) -> EmitContext:
    return EmitContext(
        repo_root=central_state_root,
        coordinator_root=central_state_root,
        central_state_root=central_state_root,
        git_branch="main",
        git_sha="0" * 40,
        git_sha_short="00000000",
        observed_at="2026-07-22T00:00:00Z",
        hostname="test-host",
        repo_name="test-org/test-repo",
    )


def test_absent_state_dir_is_graceful_empty(tmp_path: Path) -> None:
    """A genuinely-absent central_state_root yields ([], []) — never a failure."""
    ctx = _make_ctx(tmp_path / "does-not-exist")
    records, malformed = session_hierarchy.collect(ctx)
    assert records == []
    assert malformed == []


def test_valid_entry_is_collected(tmp_path: Path) -> None:
    """Sanity check: a well-formed entry is collected before testing the failure path."""
    (tmp_path / "session-hierarchy.machine-a.json").write_text(
        json.dumps({
            "session_id": "sess-1",
            "session_type": "session",
            "workstream": "some-workstream",
        }),
        encoding="utf-8",
    )
    ctx = _make_ctx(tmp_path)
    records, malformed = session_hierarchy.collect(ctx)
    assert malformed == []
    assert len(records) == 1
    assert records[0]["session_id"] == "sess-1"


@pytest.mark.skipif(
    sys.platform == "win32" or (hasattr(os, "geteuid") and os.geteuid() == 0),
    reason="chmod 0o000 permission denial is not reliable on Windows or as root",
)
def test_unreadable_state_dir_is_malformed_not_graceful_empty(tmp_path: Path) -> None:
    """An unreadable central_state_root must land in the malformed bucket with a
    'state directory unreadable' reason, never the graceful-absent ([], []) shape a
    naive glob() read would silently produce for the same failure."""
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    (state_dir / "session-hierarchy.machine-a.json").write_text(
        json.dumps({
            "session_id": "sess-1",
            "session_type": "session",
            "workstream": "some-workstream",
        }),
        encoding="utf-8",
    )
    ctx = _make_ctx(state_dir)

    original_mode = state_dir.stat().st_mode
    os.chmod(state_dir, 0o000)
    try:
        records, malformed = session_hierarchy.collect(ctx)
    finally:
        os.chmod(state_dir, original_mode)

    assert records == [], (
        "no records should be collected from an unscannable state dir"
    )
    assert malformed, (
        "expected a non-empty malformed bucket for an unscannable state dir; "
        "a naive glob() read would silently see zero matches and wrongly return "
        "the graceful-absent ([], []) shape"
    )
    assert any(
        "unreadable" in entry.get("reason", "") for entry in malformed
    ), f"expected a 'state directory unreadable' reason, got {malformed!r}"


def test_duplicate_session_id_within_one_file_quarantines_the_loser(tmp_path: Path) -> None:
    """Two entries in ONE source file sharing a session_id: the first is admitted to
    ``valid``, the second is quarantined into ``malformed`` with the duplicate reason —
    pins the b8a8339a fix (cross-repo/inbox/2026-07-26-project-opticon-em-makima-duplicate-
    session-hierarchy-entries.md): session_id is the natural key of session_hierarchies and
    must be unique within a single emission."""
    (tmp_path / "session-hierarchy.machine-a.json").write_text(
        json.dumps([
            {
                "session_id": "dup-1",
                "session_type": "session",
                "workstream": "first-workstream",
            },
            {
                "session_id": "dup-1",
                "session_type": "workstream",
                "workstream": "second-workstream",
            },
        ]),
        encoding="utf-8",
    )
    ctx = _make_ctx(tmp_path)
    records, malformed = session_hierarchy.collect(ctx)

    assert len(records) == 1
    assert records[0]["session_id"] == "dup-1"
    assert records[0]["workstream"] == "first-workstream", (
        "first-admitted entry must win — no new sort or tiebreak"
    )

    dup_entries = [m for m in malformed if m.get("session_id") == "dup-1"]
    assert len(dup_entries) == 1
    assert "duplicate session_id" in dup_entries[0]["reason"]


def test_duplicate_session_id_across_two_files_first_sorted_file_wins(tmp_path: Path) -> None:
    """Two entries sharing a session_id but living in TWO different source files: the
    duplicate-detection set spans the whole collect() call, not just one file — the entry
    in the first-sorted file wins, the one in the later file is quarantined."""
    (tmp_path / "session-hierarchy.machine-a.json").write_text(
        json.dumps({
            "session_id": "dup-2",
            "session_type": "session",
            "workstream": "workstream-from-a",
        }),
        encoding="utf-8",
    )
    (tmp_path / "session-hierarchy.machine-b.json").write_text(
        json.dumps({
            "session_id": "dup-2",
            "session_type": "session",
            "workstream": "workstream-from-b",
        }),
        encoding="utf-8",
    )
    ctx = _make_ctx(tmp_path)
    records, malformed = session_hierarchy.collect(ctx)

    assert len(records) == 1
    assert records[0]["workstream"] == "workstream-from-a", (
        "sorted(glob.glob(...)) puts machine-a.json before machine-b.json; the "
        "first-sorted-file entry must win"
    )

    dup_entries = [m for m in malformed if m.get("session_id") == "dup-2"]
    assert len(dup_entries) == 1
    assert dup_entries[0]["path"] == "state/session-hierarchy.machine-b.json"
    assert "duplicate session_id" in dup_entries[0]["reason"]


def test_distinct_session_ids_no_quarantine(tmp_path: Path) -> None:
    """Regression guard: distinct session_ids across entries/files never trip the
    duplicate check."""
    (tmp_path / "session-hierarchy.machine-a.json").write_text(
        json.dumps([
            {
                "session_id": "distinct-1",
                "session_type": "session",
                "workstream": "workstream-1",
            },
            {
                "session_id": "distinct-2",
                "session_type": "workstream",
                "workstream": "workstream-2",
            },
        ]),
        encoding="utf-8",
    )
    (tmp_path / "session-hierarchy.machine-b.json").write_text(
        json.dumps({
            "session_id": "distinct-3",
            "session_type": "blitz",
            "workstream": "workstream-3",
        }),
        encoding="utf-8",
    )
    ctx = _make_ctx(tmp_path)
    records, malformed = session_hierarchy.collect(ctx)

    assert malformed == []
    assert {r["session_id"] for r in records} == {"distinct-1", "distinct-2", "distinct-3"}


def test_duplicate_session_id_that_also_fails_enum_keeps_original_reason(tmp_path: Path) -> None:
    """A duplicate session_id whose entry ALSO fails an existing validation gate (bad
    session_type) is quarantined with the EXISTING reason, not shadowed by the duplicate
    check — the duplicate guard only fires on a fully-validated record."""
    (tmp_path / "session-hierarchy.machine-a.json").write_text(
        json.dumps([
            {
                "session_id": "dup-3",
                "session_type": "session",
                "workstream": "good-workstream",
            },
            {
                "session_id": "dup-3",
                "session_type": "not-a-real-type",
                "workstream": "good-workstream",
            },
        ]),
        encoding="utf-8",
    )
    ctx = _make_ctx(tmp_path)
    records, malformed = session_hierarchy.collect(ctx)

    assert len(records) == 1
    assert records[0]["session_id"] == "dup-3"

    dup_entries = [m for m in malformed if m.get("session_id") == "dup-3"]
    assert len(dup_entries) == 1
    assert "session_type" in dup_entries[0]["reason"]
    assert "duplicate session_id" not in dup_entries[0]["reason"]
