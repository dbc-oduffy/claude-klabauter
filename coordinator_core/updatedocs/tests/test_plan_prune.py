"""
Tests for coordinator_core.updatedocs.plan_prune.

Spec backlink: pln-bucket-2-extraction-four-deter-e121fa (chunk C3)
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from coordinator_core.updatedocs._common import UpdatedocsTargetMissing
from coordinator_core.updatedocs.plan_prune import (
    DEFAULT_AGE_FLOOR_DAYS,
    TERMINAL_PLAN_STATUSES,
    compute_plan_prune_candidates,
)

OLD_MTIME_DAYS = 30
FRESH_MTIME_DAYS = 1


def _write_plan(root: Path, name: str, fm_lines: list[str], age_days: float) -> Path:
    plans_dir = root / "docs" / "plans"
    plans_dir.mkdir(parents=True, exist_ok=True)
    p = plans_dir / name
    body = "---\n" + "\n".join(fm_lines) + "\n---\n\nBody text.\n"
    p.write_text(body, encoding="utf-8")
    ts = time.time() - age_days * 86400.0
    os.utime(p, (ts, ts))
    return p


def test_no_status_key_lands_in_indeterminate_never_prunable(tmp_path):
    """The exact failure this row exists to prevent: collapsing a plan with
    no `status:` key into either prunable or retained instead of a distinct
    indeterminate bucket."""
    _write_plan(
        tmp_path,
        "2026-01-01-no-status-plan.md",
        ["plan_id: \"pln-no-status-aaaaaa\"", "title: no status"],
        age_days=OLD_MTIME_DAYS,
    )

    result = compute_plan_prune_candidates(tmp_path)

    assert result.indeterminate == ["docs/plans/2026-01-01-no-status-plan.md"]
    assert result.prunable == []
    assert result.retained == []


def test_terminal_status_old_and_unreferenced_is_prunable(tmp_path):
    status = sorted(TERMINAL_PLAN_STATUSES)[0]
    name = "2026-01-01-terminal-unreferenced.md"
    _write_plan(
        tmp_path,
        name,
        ['plan_id: "pln-terminal-unref-bbbbbb"', f"status: {status}"],
        age_days=OLD_MTIME_DAYS,
    )

    result = compute_plan_prune_candidates(tmp_path)

    assert result.prunable == [f"docs/plans/{name}"]
    assert result.retained == []
    assert result.indeterminate == []


def test_terminal_status_but_too_fresh_is_retained(tmp_path):
    status = sorted(TERMINAL_PLAN_STATUSES)[0]
    _write_plan(
        tmp_path,
        "2026-01-01-terminal-fresh.md",
        [f"status: {status}"],
        age_days=FRESH_MTIME_DAYS,
    )

    result = compute_plan_prune_candidates(tmp_path)

    assert result.prunable == []
    assert len(result.retained) == 1


def test_non_terminal_status_is_retained_not_indeterminate(tmp_path):
    _write_plan(
        tmp_path,
        "2026-01-01-draft-plan.md",
        ["status: draft"],
        age_days=OLD_MTIME_DAYS,
    )

    result = compute_plan_prune_candidates(tmp_path)

    assert result.prunable == []
    assert result.indeterminate == []
    assert result.retained == ["docs/plans/2026-01-01-draft-plan.md"]


def test_terminal_status_but_referenced_is_retained(tmp_path):
    status = sorted(TERMINAL_PLAN_STATUSES)[0]
    plan_name = "2026-01-01-terminal-referenced.md"
    _write_plan(
        tmp_path,
        plan_name,
        [f"status: {status}"],
        age_days=OLD_MTIME_DAYS,
    )

    handoffs_dir = tmp_path / "state" / "handoffs"
    handoffs_dir.mkdir(parents=True)
    (handoffs_dir / "some-handoff.md").write_text(
        "---\n"
        f'governing_plan: "docs/plans/{plan_name}"\n'
        "status: open\n"
        "---\n\nBody.\n",
        encoding="utf-8",
    )

    result = compute_plan_prune_candidates(tmp_path)

    assert result.prunable == []
    assert result.retained == [f"docs/plans/{plan_name}"]


def test_referenced_via_origin_plan_id_in_tasks(tmp_path):
    status = sorted(TERMINAL_PLAN_STATUSES)[0]
    plan_id = "pln-referenced-by-id-cccccc"
    name = "2026-01-01-terminal-id-referenced.md"
    _write_plan(
        tmp_path,
        name,
        [f'plan_id: "{plan_id}"', f"status: {status}"],
        age_days=OLD_MTIME_DAYS,
    )

    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir(parents=True)
    (tasks_dir / "some-task.md").write_text(
        "---\n"
        f'origin_plan_id: "{plan_id}"\n'
        "status: open\n"
        "---\n\nBody.\n",
        encoding="utf-8",
    )

    result = compute_plan_prune_candidates(tmp_path)

    assert result.prunable == []
    assert result.retained == [f"docs/plans/{name}"]


def test_age_floor_is_a_parameter_not_a_hardcoded_literal(tmp_path):
    status = sorted(TERMINAL_PLAN_STATUSES)[0]
    _write_plan(
        tmp_path,
        "2026-01-01-terminal-five-days.md",
        [f"status: {status}"],
        age_days=5,
    )

    default_result = compute_plan_prune_candidates(tmp_path)
    assert default_result.prunable == []

    lowered_result = compute_plan_prune_candidates(tmp_path, age_days=1)
    assert len(lowered_result.prunable) == 1


def test_absent_plans_dir_raises_rather_than_returning_a_clean_empty_result(tmp_path):
    """An absent docs/plans/ is UNAVAILABLE, never CLEAN.

    Regression guard: this returned an empty PlanPruneResult, which the gate
    layer would have reported as "nothing to prune" — indistinguishable from a
    real zero. The whole package exists to keep those two apart.
    """
    with pytest.raises(UpdatedocsTargetMissing) as excinfo:
        compute_plan_prune_candidates(tmp_path)
    assert excinfo.value.missing_path == tmp_path / "docs" / "plans"


def test_plan_vanishing_between_glob_and_stat_is_indeterminate_not_dropped(tmp_path, monkeypatch):
    """The three buckets must account for every file the glob returned.

    A plan that vanishes mid-walk used to be dropped from all three, so the
    totals silently failed to reconcile with no evidence trail for the gap.
    """
    plans = tmp_path / "docs" / "plans"
    plans.mkdir(parents=True)
    (plans / "vanisher.md").write_text("---\nstatus: implemented\n---\n", encoding="utf-8")

    import pathlib

    real_stat = pathlib.Path.stat

    def _boom(self, *a, **kw):
        if self.name == "vanisher.md":
            raise OSError("simulated vanish between glob and stat")
        return real_stat(self, *a, **kw)

    monkeypatch.setattr(pathlib.Path, "stat", _boom)

    result = compute_plan_prune_candidates(tmp_path)

    assert "docs/plans/vanisher.md" in result.indeterminate
    assert result.prunable == []
    assert result.retained == []
