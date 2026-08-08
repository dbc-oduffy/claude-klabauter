"""
Tests for coordinator_core.coverage's claim resolution routing through the
canonical ledger-first accessor (coordinator_core.claim_state.resolve_claim_state).

Spec backlink: docs/plans/2026-08-07-claim-state-ledger-first-authoritative-read.md
§ Tasks, chunk C2 (AC3).

AC3 requires proving a ledger-only claim (mirror reverted to open) resolves
correctly through BOTH `_get_handoff_consumed_by` and `_handoff_session_live`
— the migration lands at the shared leaf `_parse_handoff_consumed_by`, which
`_handoff_session_live` calls DIRECTLY, bypassing `_get_handoff_consumed_by`
entirely (verified on disk — see this module's own docstring).
"""

from __future__ import annotations

from pathlib import Path
from unittest import mock

import pytest

from coordinator_core import coverage


def _write_claim_dir(common_dir: Path, handoff_name: str, session_id: str, claimed_at: str = "") -> Path:
    claim_dir = common_dir / "coordinator-sessions" / "handoff-claims" / handoff_name
    claim_dir.mkdir(parents=True, exist_ok=True)
    (claim_dir / "session_id").write_text(session_id, encoding="utf-8")
    if claimed_at:
        (claim_dir / "claimed_at").write_text(claimed_at, encoding="utf-8")
    return claim_dir


def _write_handoff(path: Path, *, claimed_by: str = "", consumed_by: str = "", status: str = "open") -> None:
    lines = ["---", f"status: {status}"]
    if claimed_by:
        lines.append(f"claimed_by: {claimed_by}")
    if consumed_by:
        lines.append(f"consumed_by: {consumed_by}")
    lines.append("---")
    lines.append("")
    lines.append("# body")
    path.write_text("\n".join(lines), encoding="utf-8")


@pytest.fixture
def workspace(tmp_path):
    common_dir = tmp_path / "gitdir"
    common_dir.mkdir()
    handoff = tmp_path / "state" / "handoffs" / "2026-08-07-example.md"
    handoff.parent.mkdir(parents=True)
    return common_dir, handoff


def test_parse_handoff_consumed_by_ledger_only_mirror_reverted(workspace):
    """The shared leaf: ledger has a live claim, mirror does not (the
    branch-switch-revert desync). Ledger must win."""
    common_dir, handoff = workspace
    _write_claim_dir(common_dir, handoff.name, "sess-ledger", "2026-08-07T10:00:00Z")
    _write_handoff(handoff, status="open")

    with mock.patch("coordinator_core.claim_state.cs_claim_holder_live", return_value=True):
        result = coverage._parse_handoff_consumed_by(str(handoff), common_dir=common_dir)

    assert result == "sess-ledger"


def test_get_handoff_consumed_by_ledger_only_mirror_reverted(workspace):
    """AC3, path 1: _get_handoff_consumed_by must resolve the ledger-only
    claim, not the reverted-to-open mirror."""
    common_dir, handoff = workspace
    _write_claim_dir(common_dir, handoff.name, "sess-ledger", "2026-08-07T10:00:00Z")
    _write_handoff(handoff, status="open")

    with mock.patch("coordinator_core.claim_state.cs_claim_holder_live", return_value=True):
        result = coverage._get_handoff_consumed_by(str(handoff), common_dir=common_dir)

    assert result == "sess-ledger"


def test_handoff_session_live_ledger_only_mirror_reverted(workspace):
    """AC3, path 2: _handoff_session_live calls _parse_handoff_consumed_by
    DIRECTLY (bypassing _get_handoff_consumed_by) — must also resolve the
    ledger-only claim, not treat it as unclaimed/conservative-live via a
    frontmatter-only read."""
    common_dir, handoff = workspace
    _write_claim_dir(common_dir, handoff.name, "sess-ledger", "2026-08-07T10:00:00Z")
    _write_handoff(handoff, status="open")

    with mock.patch("coordinator_core.claim_state.cs_claim_holder_live", return_value=True):
        is_live, note = coverage._handoff_session_live(
            str(handoff), frozenset({"sess-ledger"}), common_dir=common_dir
        )

    assert is_live is True
    assert note is None

    with mock.patch("coordinator_core.claim_state.cs_claim_holder_live", return_value=True):
        is_live_other, note_other = coverage._handoff_session_live(
            str(handoff), frozenset({"some-other-session"}), common_dir=common_dir
        )

    assert is_live_other is False
    assert note_other is None


def test_get_handoff_consumed_by_mirror_only_still_works(workspace):
    """No ledger claim at all — falls back to the frontmatter mirror,
    unchanged from pre-migration behavior."""
    common_dir, handoff = workspace
    _write_handoff(handoff, claimed_by="sess-mirror", status="claimed")

    result = coverage._get_handoff_consumed_by(str(handoff), common_dir=common_dir)

    assert result == "sess-mirror"


def test_get_handoff_consumed_by_legacy_consumed_by_still_works(workspace):
    """DR-084 dual-tolerance survives the migration on the mirror-fallback leg."""
    common_dir, handoff = workspace
    _write_handoff(handoff, consumed_by="sess-legacy", status="claimed")

    result = coverage._get_handoff_consumed_by(str(handoff), common_dir=common_dir)

    assert result == "sess-legacy"


def test_get_handoff_consumed_by_unclaimed_returns_none(workspace):
    common_dir, handoff = workspace
    _write_handoff(handoff, status="open")

    result = coverage._get_handoff_consumed_by(str(handoff), common_dir=common_dir)

    assert result is None


def test_handoff_session_live_unclaimed_conservative_live(workspace):
    common_dir, handoff = workspace
    _write_handoff(handoff, status="open")

    is_live, note = coverage._handoff_session_live(
        str(handoff), frozenset(), common_dir=common_dir
    )

    assert is_live is True
    assert note is None


def test_get_handoff_consumed_by_unreadable_returns_none_conservative(tmp_path):
    """Read/parse failure degrades to None (conservative-live default) —
    resolve_claim_state itself swallows the OSError rather than raising, so
    this exercises the fallback branch through the real (non-mocked) path."""
    missing = tmp_path / "state" / "handoffs" / "does-not-exist.md"

    result = coverage._get_handoff_consumed_by(str(missing))

    assert result is None


def test_parse_handoff_consumed_by_unreadable_file_raises(tmp_path):
    """C2-fix: unlike an absent claim (degraded quietly), an unreadable
    handoff file must still RAISE at the shared leaf — this is the exact
    distinction test_coverage_dag_silent_fallback_guards.py depends on.
    _get_handoff_consumed_by (above) catches this and degrades to None for
    its own external contract; the leaf itself must not swallow it."""
    missing = tmp_path / "state" / "handoffs" / "does-not-exist.md"

    with pytest.raises(OSError):
        coverage._parse_handoff_consumed_by(str(missing))


def test_parse_handoff_consumed_by_absent_claim_degrades_quietly(workspace):
    """Contrast case for the test above: file IS readable, no claim in
    either ledger or mirror — must degrade to None, not raise."""
    common_dir, handoff = workspace
    _write_handoff(handoff, status="open")

    result = coverage._parse_handoff_consumed_by(str(handoff), common_dir=common_dir)

    assert result is None
