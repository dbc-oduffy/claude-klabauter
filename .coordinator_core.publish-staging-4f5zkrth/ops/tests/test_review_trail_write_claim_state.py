"""
Tests for `review_trail_write._resolve_workstream` / `_scan_workstream`
resolving handoff ownership ledger-first via `claim_state.resolve_claim_state`.

Spec backlink: pln-claim-state-make-the-ledger-th-6641e3
§ Tasks, chunk C6a5 (AC5).

Covers the desynced-baton case this chunk exists to fix: the tracked
frontmatter mirror reverted to `status: open` (no `claimed_by`) — e.g. via a
branch switch that never carried the claiming commit — while the
branch-independent claim ledger still holds a live claim for this session.
Before this fix, `_scan_workstream` read the mirror's `claimed_by` directly
and would not attribute the handoff to the claiming session, so the
review-trail record would be written unpartitioned (landed, but to the wrong
workstream — worse than not landing, since it looks successful).
"""

from __future__ import annotations

from pathlib import Path
from unittest import mock

import pytest

from coordinator_core import claim_state as claim_state_module
from coordinator_core.ops.review_trail_write import _resolve_workstream, _scan_workstream


def _write_claim_dir(common_dir: Path, handoff_name: str, session_id: str, claimed_at: str = "") -> Path:
    claim_dir = common_dir / "coordinator-sessions" / "handoff-claims" / handoff_name
    claim_dir.mkdir(parents=True, exist_ok=True)
    (claim_dir / "session_id").write_text(session_id, encoding="utf-8")
    if claimed_at:
        (claim_dir / "claimed_at").write_text(claimed_at, encoding="utf-8")
    return claim_dir


def _write_handoff(path: Path, *, claimed_by: str = "", workstream: str = "", status: str = "open") -> None:
    lines = ["---", f"status: {status}"]
    if claimed_by:
        lines.append(f"claimed_by: {claimed_by}")
    if workstream:
        lines.append(f"workstream: {workstream}")
    lines.append("---")
    lines.append("")
    lines.append("# body")
    path.write_text("\n".join(lines), encoding="utf-8")


@pytest.fixture
def workspace(tmp_path):
    common_dir = tmp_path / "gitdir"
    common_dir.mkdir()
    handoffs_dir = tmp_path / "state" / "handoffs"
    handoffs_dir.mkdir(parents=True)
    handoff = handoffs_dir / "2026-08-07-example.md"
    return tmp_path, common_dir, handoffs_dir, handoff


def test_desynced_baton_resolves_via_ledger(workspace):
    """Mirror reverted to `status: open` (no claimed_by), ledger still holds
    a live claim for this session — the handoff must still be attributed to
    this session, and its workstream returned (AC5)."""
    caller_worktree, common_dir, handoffs_dir, handoff = workspace
    _write_claim_dir(common_dir, handoff.name, "sess-mine", "2026-08-07T13:00:00Z")
    _write_handoff(handoff, status="open", workstream="my-workstream")

    def fake_resolve(handoff_path, *, common_dir=None, repo_root=None):
        return claim_state_module.resolve_claim_state(handoff_path, common_dir=common_dir_fixture)

    common_dir_fixture = common_dir
    with mock.patch.object(claim_state_module, "cs_claim_holder_live", return_value=True), mock.patch(
        "coordinator_core.ops.review_trail_write.resolve_claim_state", side_effect=fake_resolve
    ):
        result = _scan_workstream(handoffs_dir, "sess-mine", caller_worktree)

    assert result == "my-workstream"


def test_desynced_baton_other_session_not_attributed(workspace):
    """Ledger holds a live claim for a DIFFERENT session — this session must
    not be attributed the handoff even though the mirror also carries no
    claimed_by."""
    caller_worktree, common_dir, handoffs_dir, handoff = workspace
    _write_claim_dir(common_dir, handoff.name, "sess-other", "2026-08-07T13:00:00Z")
    _write_handoff(handoff, status="open", workstream="other-workstream")

    def fake_resolve(handoff_path, *, common_dir=None, repo_root=None):
        return claim_state_module.resolve_claim_state(handoff_path, common_dir=common_dir_fixture)

    common_dir_fixture = common_dir
    with mock.patch.object(claim_state_module, "cs_claim_holder_live", return_value=True), mock.patch(
        "coordinator_core.ops.review_trail_write.resolve_claim_state", side_effect=fake_resolve
    ):
        result = _scan_workstream(handoffs_dir, "sess-mine", caller_worktree)

    assert result is None


def test_resolve_workstream_threads_through_scan(workspace):
    """`_resolve_workstream` (the public entry point) reaches the same
    ledger-first attribution via `_scan_workstream` when no explicit
    workstream param or env var is set."""
    caller_worktree, common_dir, handoffs_dir, handoff = workspace
    _write_claim_dir(common_dir, handoff.name, "sess-mine", "2026-08-07T13:00:00Z")
    _write_handoff(handoff, status="open", workstream="my-workstream")

    def fake_resolve(handoff_path, *, common_dir=None, repo_root=None):
        return claim_state_module.resolve_claim_state(handoff_path, common_dir=common_dir_fixture)

    common_dir_fixture = common_dir
    with mock.patch.object(claim_state_module, "cs_claim_holder_live", return_value=True), mock.patch(
        "coordinator_core.ops.review_trail_write.resolve_claim_state", side_effect=fake_resolve
    ):
        result = _resolve_workstream(None, caller_worktree, "sess-mine")

    assert result == "my-workstream"
