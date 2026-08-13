"""
Tests for coordinator_core.ops.session.boot_sweep._is_consumed_at_too_recent
resolving claimed_at ledger-first.

Spec backlink: pln-claim-state-make-the-ledger-th-6641e3
§ Tasks, chunk C5 / C5a (AC5).

Negative-spec: does not touch resolve_chain_terminal_disposition.py (C5b) or
stale_claims.py (C5c) — those are peer sites migrated by other chunks. Does
not exercise the DR-084 skip-and-surface disposition itself — this suite only
covers the (a) 30-minute recency-floor read, which is a read-only migration.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from coordinator_core.claim_state import ClaimState
from coordinator_core.ops.session import boot_sweep


def _write_handoff(path: Path, *, status: str = "open") -> None:
    lines = ["---", f"status: {status}", "---", "", "# body"]
    path.write_text("\n".join(lines), encoding="utf-8")


def _recent_iso(minutes_ago: int = 5) -> str:
    dt = datetime.now(tz=timezone.utc) - timedelta(minutes=minutes_ago)
    return dt.isoformat().replace("+00:00", "Z")


def _old_iso(minutes_ago: int = 60) -> str:
    dt = datetime.now(tz=timezone.utc) - timedelta(minutes=minutes_ago)
    return dt.isoformat().replace("+00:00", "Z")


def test_ledger_recent_claim_holds_floor_despite_reverted_mirror(tmp_path):
    """AC5 core case: the branch-switch-revert desync. Ledger claimed_at is
    recent; the tracked-frontmatter mirror has reverted to no claim fields at
    all (as happens on a branch switch). The 30-minute anti-false-abandon
    floor must still hold — i.e. still return True — because it resolves
    ledger-first, not mirror-only."""
    handoff = tmp_path / "state" / "handoffs" / "2026-08-07-example.md"
    handoff.parent.mkdir(parents=True)
    _write_handoff(handoff, status="open")

    ledger_state = ClaimState(
        holder="sess-live",
        claimed_at=_recent_iso(),
        source="ledger",
        disagreement=True,
        ledger_holder="sess-live",
        mirror_holder=None,
    )

    with mock.patch.object(boot_sweep, "resolve_claim_state", return_value=ledger_state):
        assert boot_sweep._is_consumed_at_too_recent(handoff) is True


def test_ledger_old_claim_does_not_bypass_reap(tmp_path):
    """A ledger claim older than 30 minutes must NOT be treated as recent —
    the floor is a bounded window, not a blanket ledger-presence bypass."""
    handoff = tmp_path / "state" / "handoffs" / "2026-08-07-old.md"
    handoff.parent.mkdir(parents=True)
    _write_handoff(handoff, status="open")

    ledger_state = ClaimState(
        holder="sess-old",
        claimed_at=_old_iso(),
        source="ledger",
        disagreement=True,
        ledger_holder="sess-old",
        mirror_holder=None,
    )

    with mock.patch.object(boot_sweep, "resolve_claim_state", return_value=ledger_state):
        assert boot_sweep._is_consumed_at_too_recent(handoff) is False


def test_no_ledger_no_mirror_falls_back_to_legacy_consumed_at(tmp_path):
    """When resolve_claim_state finds neither ledger nor mirror claimed_at,
    the legacy frontmatter-only consumed_at field is still consulted (matches
    the pre-existing dual-field-name tolerance)."""
    handoff = tmp_path / "state" / "handoffs" / "2026-08-07-legacy.md"
    handoff.parent.mkdir(parents=True)
    lines = ["---", "status: open", f"consumed_at: {_recent_iso()}", "---", "", "# body"]
    handoff.write_text("\n".join(lines), encoding="utf-8")

    none_state = ClaimState(
        holder=None,
        claimed_at=None,
        source="none",
        disagreement=False,
        ledger_holder=None,
        mirror_holder=None,
    )

    with mock.patch.object(boot_sweep, "resolve_claim_state", return_value=none_state):
        assert boot_sweep._is_consumed_at_too_recent(handoff) is True


def test_live_ledger_holder_empty_claimed_at_ignores_stale_mirror(tmp_path):
    """C5a P2 regression: a live ledger holder resolves with no usable
    claimed_at (e.g. crash between the session_id and claimed_at writes).
    Even though the tracked-frontmatter mirror carries a stale claimed_at,
    the function must NOT fall through to that unrelated mirror read — it
    must treat the live-holder-but-missing-timestamp case as too-recent
    (conservative: block archival) rather than trusting the stale mirror
    value, which could otherwise push the decision either way on a baton a
    live holder still owns."""
    handoff = tmp_path / "state" / "handoffs" / "2026-08-07-live-no-ts.md"
    handoff.parent.mkdir(parents=True)
    lines = ["---", "status: open", f"claimed_at: {_old_iso(minutes_ago=120)}", "---", "", "# body"]
    handoff.write_text("\n".join(lines), encoding="utf-8")

    live_no_ts_state = ClaimState(
        holder="sess-live",
        claimed_at=None,
        source="ledger",
        disagreement=False,
        ledger_holder="sess-live",
        mirror_holder=None,
    )

    with mock.patch.object(boot_sweep, "resolve_claim_state", return_value=live_no_ts_state):
        assert boot_sweep._is_consumed_at_too_recent(handoff) is True


def test_no_claim_anywhere_returns_false(tmp_path):
    handoff = tmp_path / "state" / "handoffs" / "2026-08-07-none.md"
    handoff.parent.mkdir(parents=True)
    _write_handoff(handoff, status="open")

    none_state = ClaimState(
        holder=None,
        claimed_at=None,
        source="none",
        disagreement=False,
        ledger_holder=None,
        mirror_holder=None,
    )

    with mock.patch.object(boot_sweep, "resolve_claim_state", return_value=none_state):
        assert boot_sweep._is_consumed_at_too_recent(handoff) is False
