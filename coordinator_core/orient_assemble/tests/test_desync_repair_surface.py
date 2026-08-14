"""
coordinator_core.orient_assemble.tests.test_desync_repair_surface — C3:
`_read_auto_reconcile` also reads `reconciled[]` and surfaces a rejected
desync repair (`exit_code != 0`) as a judgment point, while a clean
dry-run entry (`exit_code: 0, applied: False`) stays silent.

Spec backlink: pln-unwritable-handoff-records-fai-66a69f, chunk C3

Negative-spec:
    - A `reconciled[]` entry with `exit_code: 0` (dry-run-clean OR applied)
      does NOT surface — `applied` is never the discriminator.
    - `surfaced[]`-derived judgment points are unchanged by this addition.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import coordinator_core.orient_assemble.readers_branch_reconcile as rbr


def _patch_response(monkeypatch, result):
    import coordinator_core.ops.check_auto_reconcile as check_auto_reconcile

    monkeypatch.setattr(check_auto_reconcile, "get_response", lambda: {"result": result})


def test_failed_reconcile_entry_surfaces_with_message(monkeypatch):
    _patch_response(
        monkeypatch,
        {
            "surfaced": [],
            "reconciled": [
                {
                    "handoff_id": "h-42",
                    "applied": False,
                    "exit_code": 1,
                    "message": "summary exceeds 140 characters (got 201)",
                }
            ],
        },
    )

    result = rbr._read_auto_reconcile()

    assert len(result.judgment_points) == 1
    jp = result.judgment_points[0]
    assert jp["id"] == "j-desync-repair-failed-1"
    assert "summary exceeds 140 characters (got 201)" in jp["evidence"]
    assert "h-42" in jp["question"]


def test_dry_run_clean_entry_does_not_surface(monkeypatch):
    _patch_response(
        monkeypatch,
        {
            "surfaced": [],
            "reconciled": [
                {"applied": False, "exit_code": 0, "message": "dry-run: would re-stamp"}
            ],
        },
    )

    result = rbr._read_auto_reconcile()

    assert result.judgment_points == []
    assert result.directives == []


def test_applied_successful_entry_does_not_surface(monkeypatch):
    _patch_response(
        monkeypatch,
        {
            "surfaced": [],
            "reconciled": [
                {"applied": True, "exit_code": 0, "message": "re-stamped claimed_by"}
            ],
        },
    )

    result = rbr._read_auto_reconcile()

    assert result.judgment_points == []


def test_surfaced_behaviour_unchanged_alongside_reconciled(monkeypatch):
    _patch_response(
        monkeypatch,
        {
            "surfaced": [
                {"handoff_id": "h-1", "reason": "gate_eval verdict=surface", "evidence": "x"}
            ],
            "reconciled": [
                {"applied": False, "exit_code": 0, "message": "dry-run: would re-stamp"}
            ],
        },
    )

    result = rbr._read_auto_reconcile()

    ids = [jp["id"] for jp in result.judgment_points]
    assert ids == ["j-auto-reconcile-1"]


def test_no_surfaced_and_no_reconciled_returns_empty():
    import coordinator_core.ops.check_auto_reconcile as check_auto_reconcile

    orig = check_auto_reconcile.get_response
    try:
        check_auto_reconcile.get_response = lambda: {"result": {"surfaced": [], "reconciled": []}}
        result = rbr._read_auto_reconcile()
        assert result.judgment_points == []
        assert result.directives == []
    finally:
        check_auto_reconcile.get_response = orig


def test_raised_repair_exception_surfaces_end_to_end(monkeypatch, tmp_path):
    """`_handle_ledger_mirror_desync`'s exception branch must produce an entry
    that `_read_auto_reconcile` actually surfaces — pins that `exit_code` stays
    set to a nonzero value on the raised-exception path (previously unset and
    silently indistinguishable from a healthy dry-run entry)."""
    import coordinator_core.ops.handoff_reconcile as handoff_reconcile

    async def _raising_transition_handler(params, repo_root):
        raise RuntimeError("boom")

    monkeypatch.setattr(
        handoff_reconcile, "_handoff_transition_handler", _raising_transition_handler
    )

    handoff_path = tmp_path / "state" / "handoffs" / "h-99.md"
    handoff_path.parent.mkdir(parents=True)
    handoff_path.write_text("---\n---\n")
    handoff = {"id": "h-99", "_path": str(handoff_path)}

    reconciled: list = []
    asyncio.run(
        handoff_reconcile._handle_ledger_mirror_desync(
            handoff=handoff,
            ledger_holder="session-abc",
            ledger_claimed_at="2026-08-13T00:00:00+00:00",
            worktree_root=tmp_path,
            repo_root=tmp_path,
            dry_run=False,
            reconciled=reconciled,
        )
    )

    assert len(reconciled) == 1
    entry = reconciled[0]
    assert entry["exit_code"] != 0
    assert "boom" in entry["message"]

    _patch_response(monkeypatch, {"surfaced": [], "reconciled": reconciled})
    result = rbr._read_auto_reconcile()

    assert len(result.judgment_points) == 1
    jp = result.judgment_points[0]
    assert "h-99" in jp["question"]
    assert "boom" in jp["evidence"]
