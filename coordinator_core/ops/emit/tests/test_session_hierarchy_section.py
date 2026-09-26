
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
    ctx = _make_ctx(tmp_path / "does-not-exist")
    records, malformed = session_hierarchy.collect(ctx)
    assert records == []
    assert malformed == []


def test_valid_entry_is_collected(tmp_path: Path) -> None:
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
