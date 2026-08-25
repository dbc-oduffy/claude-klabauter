"""
coordinator_core.ops.tests.test_completion_ops_chain_scan_warnings — failure-path
coverage for _collect_chain_session_ids' unreadable-file handling.

BEHAVIOUR: diagnostic-only fix (2026-07-22) — an unreadable archive/completed/
entry or state/handoffs/ file was previously dropped from the chain-widening
scan via a bare `continue`, silently under-widening the chain (a missed
sibling session id's commits would be excluded from delta_shorts with zero
trail). Both sites now append a diagnostic string to the returned `warnings`
list instead of dropping silently. Not a policy change — chain_sids
computed from every OTHER readable file are unaffected.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from coordinator_core.ops.completion_ops import _collect_chain_session_ids


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


@pytest.mark.skipif(os.name == "nt", reason="chmod-based unreadable-file fixture is POSIX-only")
def test_unreadable_completed_entry_warns_but_scan_continues(tmp_path):
    root = tmp_path / "repo"

    # A readable sibling that legitimately widens the chain.
    _write(
        root / "archive" / "completed" / "2026-07" / "sibling.md",
        "---\nchain: my-chain\nauthored_by: sibling-session\n---\nbody\n",
    )

    # An unreadable entry that would ALSO match, if it could be read.
    blocked = root / "archive" / "completed" / "2026-07" / "blocked.md"
    _write(blocked, "---\nchain: my-chain\nauthored_by: blocked-session\n---\nbody\n")
    os.chmod(blocked, 0o000)

    try:
        chain_sids, warnings = _collect_chain_session_ids(root, "my-chain", "seed-session")
    finally:
        os.chmod(blocked, 0o644)

    assert "sibling-session" in chain_sids
    assert "blocked-session" not in chain_sids  # scan of it failed — expected
    assert any("unreadable completion entry" in w and "blocked.md" in w for w in warnings)


@pytest.mark.skipif(os.name == "nt", reason="chmod-based unreadable-file fixture is POSIX-only")
def test_unreadable_handoff_warns_but_scan_continues(tmp_path):
    root = tmp_path / "repo"

    _write(
        root / "state" / "handoffs" / "sibling.md",
        "---\nworkstream: my-chain\nclaimed_by: sibling-session\n---\nbody\n",
    )

    blocked = root / "state" / "handoffs" / "blocked.md"
    _write(blocked, "---\nworkstream: my-chain\nclaimed_by: blocked-session\n---\nbody\n")
    os.chmod(blocked, 0o000)

    try:
        chain_sids, warnings = _collect_chain_session_ids(root, "my-chain", "seed-session")
    finally:
        os.chmod(blocked, 0o644)

    assert "sibling-session" in chain_sids
    assert "blocked-session" not in chain_sids
    assert any("unreadable handoff" in w and "blocked.md" in w for w in warnings)


@pytest.mark.skipif(os.name == "nt", reason="chmod-based unreadable-file fixture is POSIX-only")
def test_unreadable_handoff_legacy_consumed_by_still_warns(tmp_path):
    # Exercises the reader's old-name tolerance (consumed_by fallback) for
    # pre-migration handoffs — deliberately kept on old vocabulary.
    root = tmp_path / "repo"

    _write(
        root / "state" / "handoffs" / "sibling.md",
        "---\nworkstream: my-chain\nconsumed_by: sibling-session\n---\nbody\n",
    )

    blocked = root / "state" / "handoffs" / "blocked.md"
    _write(blocked, "---\nworkstream: my-chain\nconsumed_by: blocked-session\n---\nbody\n")
    os.chmod(blocked, 0o000)

    try:
        chain_sids, warnings = _collect_chain_session_ids(root, "my-chain", "seed-session")
    finally:
        os.chmod(blocked, 0o644)

    assert "sibling-session" in chain_sids
    assert "blocked-session" not in chain_sids
    assert any("unreadable handoff" in w and "blocked.md" in w for w in warnings)
