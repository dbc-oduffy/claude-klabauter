"""Tests for `_claimed_by`'s ledger-first migration in
`coordinator_core.session_hierarchy.derive`.

Purpose: prove the module's one claim-bridge helper now routes through
`coordinator_core.claim_state.resolve_claim_state` (ledger-first, DR-084
dual-tolerant mirror fallback) instead of reading only the already-queried
in-memory frontmatter mirror — so a branch-switch-desynced baton (a live
ledger claim whose tracked-frontmatter mirror reverted to `open`) no longer
makes the session node, and its children's `parent_session_id`, vanish from
this projection.

Spec backlink: pln-claim-state-make-the-ledger-th-6641e3
§ Tasks, chunk C6b (AC5).

Negative-spec:
- Does NOT re-test `resolve_claim_state` itself (see
  coordinator_core/tests/test_claim_state_accessor.py) — only that
  `derive()`/`_claimed_by` route through it and degrade correctly.
"""
from __future__ import annotations

from unittest import mock

from coordinator_core import claim_state
from coordinator_core.session_hierarchy.derive import derive


def _write_claim_dir(common_dir, handoff_name, session_id, claimed_at=""):
    claim_dir = common_dir / "coordinator-sessions" / "handoff-claims" / handoff_name
    claim_dir.mkdir(parents=True, exist_ok=True)
    (claim_dir / "session_id").write_text(session_id, encoding="utf-8")
    if claimed_at:
        (claim_dir / "claimed_at").write_text(claimed_at, encoding="utf-8")
    return claim_dir


def _write_handoff(path, *, status="open"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\nstatus: {status}\n---\n\n# body\n", encoding="utf-8")


def test_desynced_baton_session_node_survives_via_ledger(tmp_path):
    """AC5: a live ledger claim with a mirror reverted to open (this plan's
    own 2026-08-07 branch-switch-desync incident) must still surface the
    session node — and a child handoff's predecessor lookup (keyed off the
    same claim-bridge read) must still resolve `parent_session_id`."""
    common_dir = tmp_path / "gitdir"
    common_dir.mkdir()

    handoff_rel = "state/handoffs/2026-08-07-desynced.md"
    handoff = tmp_path / handoff_rel
    _write_handoff(handoff, status="open")  # mirror reverted — no claimed_by

    sid = "ffff0000-0000-0000-0000-000000000001"
    _write_claim_dir(common_dir, handoff.name, sid, "2026-08-07T09:00:00Z")

    child_rel = "state/handoffs/2026-08-07-child.md"
    child = tmp_path / child_rel
    _write_handoff(child, status="open")

    rec = {
        "path": handoff_rel,
        "frontmatter": {"workstream": "workstream-a"},
    }
    child_rec = {
        "path": child_rel,
        "frontmatter": {
            "workstream": "workstream-a",
            "predecessor": handoff.name,
            "claimed_by": "next-session",
        },
    }

    with mock.patch.object(claim_state, "git_common_dir", return_value=common_dir), mock.patch.object(
        claim_state, "cs_claim_holder_live", return_value=True
    ):
        result = derive([rec, child_rec], [], repo_root=tmp_path)

    session_records = {r["session_id"]: r for r in result if r["session_type"] != "workstream"}

    assert sid in session_records, "desynced session node must survive the ledger-first read"
    assert session_records[sid]["linked_handoffs"] == [handoff_rel]

    child_node = session_records["next-session"]
    assert child_node["parent_session_id"] == sid


def test_legacy_consumed_by_only_still_resolves_dual_tolerance():
    """DR-084: a legacy record carrying only `consumed_by` (never migrated to
    `claimed_by`) must still resolve — the ledger-first accessor's own
    mirror-read failure (no such file on disk here) falls back to the
    already-queried in-memory frontmatter, which stays dual-tolerant."""
    rec = {
        "path": "state/handoffs/2026-08-07-legacy.md",
        "frontmatter": {"consumed_by": "legacy-session", "workstream": "workstream-a"},
    }

    result = derive([rec], [])

    session_records = [r for r in result if r["session_type"] != "workstream"]
    assert len(session_records) == 1
    assert session_records[0]["session_id"] == "legacy-session"
