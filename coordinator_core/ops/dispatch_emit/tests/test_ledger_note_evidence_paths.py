"""A revise note naming a foreign plan path yields no write for it; the minted row still derives."""

from __future__ import annotations

from pathlib import Path

from coordinator_core.ops.dispatch_emit.ledger_remaining_paths import derive_ledger_rows

_CODE = "coordinator_core/ops/dispatch_emit/ledger_remaining_paths.py"
_FOREIGN = "docs/plans/2026-10-23-pipelined-review-c2-c6.md"
_NOTE = f"Context only, see {_FOREIGN} for the DoE side."


def _touch(root: Path, rel: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("", encoding="utf-8")


def test_note_only_plan_path_is_not_a_write(tmp_path):
    _touch(tmp_path, _CODE)
    (tmp_path / "docs/plans").mkdir(parents=True)
    rows, under = derive_ledger_rows(
        {"R1": {"remaining": [f"fix {_CODE}"]}}, [_NOTE], ["R1"], {}, [], tmp_path)
    assert under == [] and len(rows) == 1
    assert rows[0]["writes"] == [_CODE]
    assert _FOREIGN in rows[0]["body"]


def test_step_named_plan_path_stays_a_write(tmp_path):
    _touch(tmp_path, _CODE)
    _touch(tmp_path, _FOREIGN)
    rows, _ = derive_ledger_rows(
        {"R1": {"remaining": [f"amend {_FOREIGN}"]}}, [], ["R1"], {}, [], tmp_path)
    assert _FOREIGN in rows[0]["writes"]


def _snapshot(changed: set):
    from coordinator_core.ops.dispatch_emit.worktree_snapshot import Snapshot

    return Snapshot(set(changed), [], set(changed) | {"c.py"}, [])


def test_a_changed_declared_file_the_commit_omits_is_reported_withheld(tmp_path):
    from coordinator_core.ops.dispatch_emit.terminal_commit import _withheld_product_files

    snap = _snapshot({"a.py", "b.py"})
    assert _withheld_product_files(tmp_path, snap, {"a.py", "b.py", "c.py"}, {"a.py"}) == ["b.py"]


def test_every_changed_declared_file_committed_reports_nothing_withheld(tmp_path):
    from coordinator_core.ops.dispatch_emit.terminal_commit import _withheld_product_files

    snap = _snapshot({"a.py", "b.py"})
    assert _withheld_product_files(tmp_path, snap, {"a.py", "b.py", "c.py"}, {"a.py", "b.py"}) == []
