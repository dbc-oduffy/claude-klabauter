"""plan_layout on a fixture holding flat, month, per-handoff-dir, terminal and claimed batons."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from coordinator_core.housekeeping.handoff_layout import plan_layout


def _baton(root: Path, rel: str, state: str = "ready_to_fire", claimed: str | None = None) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    fm = f"---\ndeployment_state: {state}\n"
    if claimed is not None:
        fm += f"claimed_by: {claimed}\n"
    p.write_text(fm + "---\nbody\n", encoding="utf-8")


def _apply(root: Path, plan) -> None:
    for m in plan.moves:
        dst = root / m.dst
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(root / m.src), str(dst))
    for d in plan.remove_dirs:
        os.rmdir(root / d)


def _fixture(root: Path) -> None:
    _baton(root, "state/handoffs/2026-10-01-flat-live.md")
    _baton(root, "state/handoffs/2026-10-02-flat-done.md", "shipped")
    _baton(root, "state/handoffs/2026-10-03-flat-cont.md", "continued")
    _baton(root, "state/handoffs/2026-10-04-flat-claimed.md", claimed="sess-1")
    _baton(root, "state/handoffs/2026-10-05-flat-term-claimed.md", "closed", claimed="sess-2")
    _baton(root, "state/handoffs/2026-10/2026-10-06-month-live.md")
    _baton(root, "state/handoffs/2026-10/2026-10-07-month-done.md", "shipped")
    _baton(root, "state/handoffs/2026-10/2026-09-08-misfiled.md")
    _baton(root, "state/handoffs/2026-10-09-perdir/2026-10-09-perdir.md")
    _baton(root, "state/handoffs/2026-10-10-perdir-done/2026-10-10-perdir-done.md", "shipped")
    _baton(root, "state/handoffs/.archive/2026-08-01-old.md", "shipped")
    _baton(root, "state/handoffs/evidence/a.md")
    _baton(root, "state/handoffs/evidence/b.md")
    _baton(root, "state/handoffs/undated-flat.md")
    _baton(root, "archive/handoffs/2026-07-01-archived-flat.md", "shipped")
    _baton(root, "archive/handoffs/2026-07/2026-07-02-archived-ok.md", "shipped")
    _baton(root, "archive/handoffs/undated-archived.md", "shipped")


def _moves(plan) -> dict[str, str]:
    return {m.src: m.dst for m in plan.moves}


def test_fixture_plan_classifies_every_shape(tmp_path: Path) -> None:
    _fixture(tmp_path)
    plan = plan_layout(str(tmp_path))
    mv = _moves(plan)
    s = "state/handoffs"
    assert mv[f"{s}/2026-10-01-flat-live.md"] == f"{s}/2026-10/2026-10-01-flat-live.md"
    assert mv[f"{s}/2026-10-02-flat-done.md"] == "archive/handoffs/2026-10/2026-10-02-flat-done.md"
    assert mv[f"{s}/2026-10-03-flat-cont.md"] == "archive/handoffs/2026-10/2026-10-03-flat-cont.md"
    assert mv[f"{s}/2026-10-05-flat-term-claimed.md"].startswith("archive/handoffs/2026-10/")
    assert mv[f"{s}/2026-10/2026-10-07-month-done.md"] == "archive/handoffs/2026-10/2026-10-07-month-done.md"
    assert mv[f"{s}/2026-10/2026-09-08-misfiled.md"] == f"{s}/2026-09/2026-09-08-misfiled.md"
    assert mv[f"{s}/2026-10-09-perdir/2026-10-09-perdir.md"] == f"{s}/2026-10/2026-10-09-perdir.md"
    assert mv[f"{s}/2026-10-10-perdir-done/2026-10-10-perdir-done.md"].startswith("archive/handoffs/2026-10/")
    assert mv[f"{s}/.archive/2026-08-01-old.md"] == "archive/handoffs/2026-08/2026-08-01-old.md"
    assert mv["archive/handoffs/2026-07-01-archived-flat.md"] == "archive/handoffs/2026-07/2026-07-01-archived-flat.md"
    assert f"{s}/2026-10/2026-10-06-month-live.md" not in mv
    assert plan.unclassified_dirs == (f"{s}/evidence",)
    assert set(plan.remove_dirs) == {
        f"{s}/2026-10-09-perdir",
        f"{s}/2026-10-10-perdir-done",
        f"{s}/.archive",
    }
    assert plan.counts["move:flat-live"] == 1
    assert plan.counts["move:flat-terminal"] == 3
    assert plan.counts["move:archive-flat-terminal"] == 1


def test_claimed_live_baton_is_skipped_and_terminal_claimed_archives(tmp_path: Path) -> None:
    _fixture(tmp_path)
    plan = plan_layout(str(tmp_path))
    assert ("state/handoffs/2026-10-04-flat-claimed.md", "claimed") in plan.skipped
    assert "state/handoffs/2026-10-04-flat-claimed.md" not in _moves(plan)


def test_undated_never_targets_a_flat_root(tmp_path: Path) -> None:
    _fixture(tmp_path)
    plan = plan_layout(str(tmp_path))
    assert ("state/handoffs/undated-flat.md", "undated") in plan.skipped
    assert ("archive/handoffs/undated-archived.md", "undated") in plan.skipped
    for m in plan.moves:
        parent = m.dst.rsplit("/", 1)[0]
        assert parent not in ("state/handoffs", "archive/handoffs"), m


def test_applied_plan_replans_empty(tmp_path: Path) -> None:
    _fixture(tmp_path)
    _apply(tmp_path, plan_layout(str(tmp_path)))
    again = plan_layout(str(tmp_path))
    assert again.moves == ()
    assert again.remove_dirs == ()
    assert plan_layout(str(tmp_path)) == again


def test_destination_collision_is_skipped_never_overwritten(tmp_path: Path) -> None:
    _baton(tmp_path, "state/handoffs/2026-10-01-a.md")
    _baton(tmp_path, "state/handoffs/2026-10/2026-10-01-a.md")
    plan = plan_layout(str(tmp_path))
    assert plan.moves == ()
    assert ("state/handoffs/2026-10-01-a.md", "collision") in plan.skipped


def test_dir_with_live_claimed_baton_is_left_alone(tmp_path: Path) -> None:
    _baton(tmp_path, "state/handoffs/2026-10-01-x/2026-10-01-x.md", claimed="sess")
    plan = plan_layout(str(tmp_path))
    assert plan.moves == ()
    assert plan.remove_dirs == ()


def test_absent_trees_plan_nothing(tmp_path: Path) -> None:
    plan = plan_layout(str(tmp_path))
    assert plan.moves == () and plan.skipped == () and plan.unclassified_dirs == ()
