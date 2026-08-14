"""
Tests for coordinator_core.ops.fleet.archive_plans' C5 AC7 post-move
citation-surface gate wiring in `_handle_act`.

The existing `test_archive_plans_act_phase_wires_the_citation_gate` (in
`assert_no_dangling_plan_backlinks`'s own test module) only asserts identity
(`archive_plans._run_backlink_gate is run_gate`) — it never exercises the
`gate_rc != 0` warning branch or the `except Exception` crash-guard branch
inside `_handle_act` itself. These two tests close that gap
(Review: code-reviewer P2).

Git-free per `archive_git_free_seam`'s own discriminator: both assertions
depend only on the filesystem + the op's return envelope + a monkeypatched
`_run_backlink_gate`, never on git's own state.

Spec backlink: archive/specs/2026-06/2026-06-23-programmatic-terminal-plan-archival.md § AC9 / C6
"""
from __future__ import annotations

import logging
from pathlib import Path

import pytest

from coordinator_core.ops.fleet import archive_plans
from coordinator_core.ops.fleet.tests.archive_git_free_seam import (
    patched_disposition_seam,
    run,
)


def _write(root: Path, rel: str, content: str) -> None:
    full = root / rel
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_text(content, encoding="utf-8")


def _seed_terminal_plan(root: Path) -> str:
    rel = "docs/plans/2026-05-01-gate-wiring-plan.md"
    _write(
        root,
        rel,
        "---\ntitle: Gate wiring plan\nstatus: implemented\n"
        "plan_id: pln-gate-wiring-plan-777001\n---\n# Gate wiring plan\n",
    )
    return rel


def test_act_phase_warns_and_does_not_block_when_gate_rc_nonzero(tmp_path, caplog):
    """A nonzero `_run_backlink_gate` return logs a warning but the archive
    still reports `acted` -- the gate is audit-only, never blocking."""
    root = tmp_path
    rel = _seed_terminal_plan(root)

    with patched_disposition_seam(archive_plans, worktree=root) as mover, \
         pytest.MonkeyPatch.context() as mp:
        mp.setattr(archive_plans, "_run_backlink_gate", lambda worktree_root: 1)
        with caplog.at_level(logging.WARNING, logger="coordinator_core.ops.fleet.archive_plans"):
            result = run(
                archive_plans._handle_act(
                    "already-terminal", root, root / "docs" / "plans", [rel], root
                )
            )

    assert result["acted"] == [{"id": rel, "archived": True}]
    assert mover.captured is not None
    assert any(
        "citation-surface gate" in r.message and "dangling" in r.message
        for r in caplog.records
    )


def test_act_phase_warns_and_does_not_block_when_gate_crashes(tmp_path, caplog):
    """A `_run_backlink_gate` crash is caught, logged, and never masks the
    already-landed archive."""
    root = tmp_path
    rel = _seed_terminal_plan(root)

    def _boom(worktree_root):
        raise RuntimeError("gate blew up")

    with patched_disposition_seam(archive_plans, worktree=root) as mover, \
         pytest.MonkeyPatch.context() as mp:
        mp.setattr(archive_plans, "_run_backlink_gate", _boom)
        with caplog.at_level(logging.WARNING, logger="coordinator_core.ops.fleet.archive_plans"):
            result = run(
                archive_plans._handle_act(
                    "already-terminal", root, root / "docs" / "plans", [rel], root
                )
            )

    assert result["acted"] == [{"id": rel, "archived": True}]
    assert mover.captured is not None
    assert any(
        "citation-surface gate errored" in r.message and "gate blew up" in r.message
        for r in caplog.records
    )
