"""
coordinator_core.distill.tests.test_curation_status

Unit tests for coordinator_core.distill.curation_status — the derived per-artifact
curation-ledger computation (C11; DEC-1).

Coverage:
  compute_curation_status:
    (a) fixture-repo golden — a seeded docs/plans/ + archive/specs/ + canonical log +
        improvement-queue tree, asserting the exact per-artifact ledger
    (b) unharvested_ripe_count counts only docs/plans artifacts with ripe=True and
        harvested=False
    (c) a harvested, unreferenced archive/specs artifact is prunable with reason
        "harvested, unreferenced"; an actively-referenced one is not
    (d) a sidecar-suffixed archive/specs artifact is always prunable with reason
        "sidecar", regardless of log content
    (e) an open improvement-queue entry naming an artifact's surface blocks it
        (blocked_by non-empty, prunable forced False even if otherwise eligible)
    (f) a closed improvement-queue entry does NOT block (only status: open counts)
    (g) absent plans_dir / specs_dir degrades to an empty cohort for that tree, not
        an error; absent log_path still fails loud via harvest_debt's own contract

Spec backlink: pln-makima-driven-ceremony-redesig-c7fe9a § C11
"""

from __future__ import annotations

from pathlib import Path

import pytest

from coordinator_core.distill.curation_status import (
    ARCHIVE_TREE,
    PLANS_TREE,
    compute_curation_status,
)
from coordinator_core.distill.harvest_debt import DistillationLogMissingError

CANONICAL_LOG_FIXTURE = """\
## Run r-2026-07-23a
- archive/specs/2026-07/harvested-plan.md -> DISTILLED, folded into wiki (run: r-2026-07-23a)
- archive/specs/2026-07/blocked-plan.md -> DISTILLED, folded into wiki (run: r-2026-07-23a)
- archive/specs/2026-07/referenced-plan.md -> PROMOTE, promoted to decision (run: r-2026-07-23a)
"""


def _seed_repo(tmp_path: Path) -> Path:
    repo_root = tmp_path
    specs_dir = repo_root / "archive" / "specs" / "2026-07"
    specs_dir.mkdir(parents=True)
    (specs_dir / "harvested-plan.md").write_text("dummy content\n")
    (specs_dir / "blocked-plan.md").write_text("dummy content\n")
    (specs_dir / "referenced-plan.md").write_text("dummy content\n")
    (specs_dir / "unharvested-plan.md").write_text("dummy content\n")
    (specs_dir / "foo.review.md").write_text("sidecar scaffolding\n")

    plans_dir = repo_root / "docs" / "plans"
    plans_dir.mkdir(parents=True)
    (plans_dir / "ripe-plan.md").write_text("---\nstatus: implemented\n---\nbody\n")
    (plans_dir / "draft-plan.md").write_text("---\nstatus: draft\n---\nbody\n")

    state_dir = repo_root / "state"
    state_dir.mkdir(parents=True)
    (state_dir / "distillation-log.md").write_text(CANONICAL_LOG_FIXTURE)

    # Referenced-plan.md is actively referenced from a tasks/ file — this is what
    # active_reference_guard's default scope (docs/tasks/archive) picks up.
    tasks_dir = repo_root / "tasks"
    tasks_dir.mkdir(parents=True)
    (tasks_dir / "note.md").write_text("see referenced-plan for details\n")

    queue_dir = state_dir / "improvement-queue"
    queue_dir.mkdir(parents=True)
    (queue_dir / "2026-07-23-open-blocks-it.yaml").write_text(
        "created: 2026-07-23\n"
        "title: still concerns this artifact\n"
        "status: open\n"
        "surface: archive/specs/2026-07/blocked-plan.md\n"
    )
    (queue_dir / "2026-07-23-closed-does-not-block.yaml").write_text(
        "created: 2026-07-23\n"
        "title: resolved already\n"
        "status: closed\n"
        "surface: archive/specs/2026-07/harvested-plan.md\n"
    )

    return repo_root


def test_fixture_repo_golden(tmp_path):
    repo_root = _seed_repo(tmp_path)
    result = compute_curation_status(repo_root)

    harvested_plan = result.artifacts["archive/specs/2026-07/harvested-plan.md"]
    assert harvested_plan.tree == ARCHIVE_TREE
    assert harvested_plan.harvested is True
    assert harvested_plan.ripe is False
    assert harvested_plan.prunable is True
    assert harvested_plan.reasons == ["harvested, unreferenced"]
    assert harvested_plan.blocked_by == []

    unharvested_plan = result.artifacts["archive/specs/2026-07/unharvested-plan.md"]
    assert unharvested_plan.harvested is False
    assert unharvested_plan.prunable is False

    sidecar = result.artifacts["archive/specs/2026-07/foo.review.md"]
    assert sidecar.harvested is False
    assert sidecar.prunable is True
    assert sidecar.reasons == ["sidecar"]

    ripe_plan = result.artifacts["docs/plans/ripe-plan.md"]
    assert ripe_plan.tree == PLANS_TREE
    assert ripe_plan.ripe is True
    assert ripe_plan.harvested is False
    assert ripe_plan.prunable is False

    draft_plan = result.artifacts["docs/plans/draft-plan.md"]
    assert draft_plan.ripe is False


def test_unharvested_ripe_count_only_counts_ripe_and_unharvested(tmp_path):
    repo_root = _seed_repo(tmp_path)
    result = compute_curation_status(repo_root)
    # Only docs/plans/ripe-plan.md is ripe=True and harvested=False.
    assert result.unharvested_ripe_count == 1


def test_referenced_archive_artifact_is_not_prunable(tmp_path):
    repo_root = _seed_repo(tmp_path)
    result = compute_curation_status(repo_root)
    referenced_plan = result.artifacts["archive/specs/2026-07/referenced-plan.md"]
    assert referenced_plan.harvested is True
    assert referenced_plan.prunable is False
    assert referenced_plan.reasons == []


def test_open_queue_entry_blocks_and_forces_not_prunable(tmp_path):
    repo_root = _seed_repo(tmp_path)
    result = compute_curation_status(repo_root)
    blocked_plan = result.artifacts["archive/specs/2026-07/blocked-plan.md"]
    assert blocked_plan.harvested is True
    assert blocked_plan.blocked_by == ["2026-07-23-open-blocks-it"]
    assert blocked_plan.prunable is False
    assert blocked_plan.reasons == []


def test_closed_queue_entry_does_not_block(tmp_path):
    repo_root = _seed_repo(tmp_path)
    result = compute_curation_status(repo_root)
    harvested_plan = result.artifacts["archive/specs/2026-07/harvested-plan.md"]
    assert harvested_plan.blocked_by == []


def test_prunable_list_matches_prunable_artifacts(tmp_path):
    repo_root = _seed_repo(tmp_path)
    result = compute_curation_status(repo_root)
    prunable_paths = {p["path"] for p in result.prunable}
    assert prunable_paths == {
        "archive/specs/2026-07/harvested-plan.md",
        "archive/specs/2026-07/foo.review.md",
    }
    by_path = {p["path"]: p["reasons"] for p in result.prunable}
    assert by_path["archive/specs/2026-07/harvested-plan.md"] == ["harvested, unreferenced"]
    assert by_path["archive/specs/2026-07/foo.review.md"] == ["sidecar"]


def test_absent_plans_dir_degrades_to_empty_cohort(tmp_path):
    repo_root = _seed_repo(tmp_path)
    import shutil

    shutil.rmtree(repo_root / "docs" / "plans")
    result = compute_curation_status(repo_root)
    assert not any(e.tree == PLANS_TREE for e in result.artifacts.values())
    # archive/specs cohort is unaffected.
    assert "archive/specs/2026-07/harvested-plan.md" in result.artifacts


def test_absent_specs_dir_degrades_to_empty_cohort(tmp_path):
    repo_root = _seed_repo(tmp_path)
    import shutil

    shutil.rmtree(repo_root / "archive" / "specs")
    result = compute_curation_status(repo_root)
    assert not any(e.tree == ARCHIVE_TREE for e in result.artifacts.values())
    assert "docs/plans/ripe-plan.md" in result.artifacts


def test_absent_log_still_fails_loud_via_harvest_debt(tmp_path):
    repo_root = _seed_repo(tmp_path)
    (repo_root / "state" / "distillation-log.md").unlink()
    with pytest.raises(DistillationLogMissingError):
        compute_curation_status(repo_root)
