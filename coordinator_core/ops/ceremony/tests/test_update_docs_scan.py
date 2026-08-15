"""
coordinator_core.ops.ceremony.tests.test_update_docs_scan

Tests for the "ceremony.update_docs_scan" op (C17; AC8).

Import guard: coordinator_core.ops.ceremony.update_docs_scan MUST be imported at
module load time so @register_op("ceremony.update_docs_scan") fires and populates
_REGISTRY.

Coverage:
  (a) registry assertion — op name present in _REGISTRY after import
  (b) repo_root=None raises ValueError (no silent fallback, AC5-style)
  (c) fixture-repo manifest golden — schema_version, phase1 tracker/plan-status
      fields, phase8b prune rows shaped as expected
  (d) ripeness-safety negative fixture (AC8 negative spec): a ripe-but-unharvested
      plan is NEVER flagged prunable even when its status/age would otherwise
      qualify it
  (e) lineage-backstop: a predecessor referenced by an active (non-abandoned)
      successor's `supersedes:` field is never prunable regardless of age/status
  (f) tasks/ cohort: UUID-shaped dirs are immune; status:superseded file is
      prunable immediately (no age threshold)
  (g) threshold-constant grep-assert: PLANS_PRUNE_AGE_DAYS /
      CROSSREPO_ARCHIVE_ACTIONED_FLOOR_DAYS are named module constants, not
      inline literals scattered through the classification bodies

Spec backlink: pln-claude-klabauter-driven-ceremony-redesig-c7fe9a § C17
"""

from __future__ import annotations

import datetime as _dt
import os
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Import guard — fires @register_op("ceremony.update_docs_scan") as a side-effect.
# ---------------------------------------------------------------------------
import coordinator_core.ops.ceremony.update_docs_scan as uds  # noqa: F401

from coordinator_core.ipc import _REGISTRY

# Spawns a real external process; runs at cadence gates, not per-commit.
# Spawn ratchet: coordinator_core/tests/test_no_new_spawning_tests.py
pytestmark = [
    pytest.mark.spawns_process,
    pytest.mark.cadence,
]

_OP_NAME = "ceremony.update_docs_scan"
assert _OP_NAME in _REGISTRY, (
    f"import guard failed: {_OP_NAME!r} not in _REGISTRY — "
    "coordinator_core.ops.ceremony.update_docs_scan @register_op did not fire"
)


def _run(result):
    return result


def _set_mtime(path: Path, when: _dt.datetime) -> None:
    ts = when.timestamp()
    os.utime(path, (ts, ts))


def _seed_repo(tmp_path: Path, *, now: _dt.datetime) -> Path:
    core_dir = tmp_path / "coordinator_core"
    core_dir.mkdir(parents=True)
    tracker = core_dir / "DIRECTORY.md"
    tracker.write_text("# module map\n")
    _set_mtime(tracker, now - _dt.timedelta(days=30))

    plans_dir = tmp_path / "docs" / "plans"
    plans_dir.mkdir(parents=True)

    # Old superseded plan, no active successor referencing it via supersedes —
    # prune-eligible by status+age.
    old_superseded = plans_dir / "old-superseded-plan.md"
    old_superseded.write_text("---\nstatus: superseded\n---\nbody\n")
    _set_mtime(old_superseded, now - _dt.timedelta(days=30))

    # A ripe-but-unharvested plan whose status alone (if disposed) would look
    # prunable — but ripe statuses (implemented/shipped) aren't in the disposed
    # set to begin with; the negative fixture below exercises the *guard*
    # itself directly via monkeypatched curation data instead.
    ripe_plan = plans_dir / "ripe-unharvested-plan.md"
    ripe_plan.write_text("---\nstatus: implemented\n---\nbody\n")
    _set_mtime(ripe_plan, now - _dt.timedelta(days=30))

    # Predecessor + active successor lineage pair.
    predecessor = plans_dir / "predecessor-plan.md"
    predecessor.write_text("---\nstatus: superseded\n---\nbody\n")
    _set_mtime(predecessor, now - _dt.timedelta(days=30))

    successor = plans_dir / "successor-plan.md"
    successor.write_text(
        "---\nstatus: implemented\nsupersedes: docs/plans/predecessor-plan.md\n---\nbody\n"
    )
    _set_mtime(successor, now - _dt.timedelta(days=1))

    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True)
    (state_dir / "distillation-log.md").write_text("## Run r-1\n")

    return tmp_path


def _seed_git_repo(tmp_path: Path, *, now: _dt.datetime) -> Path:
    import subprocess

    repo_root = _seed_repo(tmp_path, now=now)
    subprocess.run(["git", "init", "-q"], cwd=repo_root, check=True)
    return repo_root


def test_op_is_registered():
    assert _OP_NAME in _REGISTRY


def test_repo_root_none_raises():
    with pytest.raises(ValueError):
        _run(uds._ceremony_update_docs_scan({}, repo_root=None))


def test_fixture_repo_manifest_golden(tmp_path, monkeypatch):
    now = _dt.datetime(2026, 7, 23, 12, 0, 0, tzinfo=_dt.timezone.utc)
    repo_root = _seed_repo(tmp_path, now=now)
    common_dir = repo_root / ".git"
    monkeypatch.setattr(uds, "_now_utc", lambda: now)

    reply = _run(uds._ceremony_update_docs_scan({}, repo_root=common_dir))

    assert reply["schema_version"] == 1
    assert reply["phase1"]["tracker_present"] is True
    assert reply["phase1"]["tracker_path"] == "coordinator_core/DIRECTORY.md"
    assert reply["phase1"]["git_log_window"]["available"] is False  # not a git repo

    plan_status_scan = reply["phase1"]["plan_status_scan"]
    assert plan_status_scan["docs/plans/old-superseded-plan.md"] == "superseded"
    assert plan_status_scan["docs/plans/ripe-unharvested-plan.md"] == "implemented"

    prune_by_path = {row["path"]: row for row in reply["phase8b_prune"]}
    old_row = prune_by_path["docs/plans/old-superseded-plan.md"]
    assert old_row["cohort"] == "plans"
    assert old_row["prunable"] is True
    assert "status:superseded" in old_row["reasons"]


def test_tracker_reconcile_preview_reports_without_writing(tmp_path, monkeypatch):
    """AC7 preview leg: a stale `N of M` tracker claim, joined to its plan
    via a `**Specs:**`-referenced `docs/plans/*.md` file carrying
    `deliverable_id`, is reported in `tracker_reconcile_preview` — and the
    on-disk tracker is left byte-identical, since this scan is read-only
    (the write goes through `close_out_and_stamp.apply_tracker_
    reconciliation`, never this op)."""
    import subprocess

    now = _dt.datetime(2026, 7, 23, 12, 0, 0, tzinfo=_dt.timezone.utc)
    repo_root = _seed_repo(tmp_path, now=now)

    def _git(args):
        subprocess.run(["git", *args], cwd=repo_root, check=True, capture_output=True)

    _git(["init", "-q"])
    _git(["config", "user.email", "t@t"])
    _git(["config", "user.name", "test"])
    _git(["add", "-A"])
    _git(["commit", "-q", "-m", "seed"])

    plan_rel = "docs/plans/tracker-reconcile-fixture.md"
    plan_path = repo_root / plan_rel
    plan_path.write_text(
        "---\n"
        'title: "Tracker reconcile fixture"\n'
        "status: executing\n"
        'deliverable_id: "dlv-update-docs-scan-fixture-000001"\n'
        "---\n\n"
        "## Tasks\n\n"
        "```yaml plan-tasks\n"
        "- id: C1\n"
        "  title: First chunk\n"
        "  change_kind: code-edit\n"
        "  surface: fixture.py\n"
        "  deferred: false\n"
        "  disposition: open\n"
        "  body: |\n"
        "    First chunk.\n"
        "- id: C2\n"
        "  title: Second chunk\n"
        "  change_kind: code-edit\n"
        "  surface: fixture.py\n"
        "  deferred: false\n"
        "  disposition: open\n"
        "  body: |\n"
        "    Second chunk.\n"
        "```\n",
        encoding="utf-8",
    )
    _git(["add", plan_rel])
    _git(["commit", "-q", "-m", "seed plan"])

    with plan_path.open("a", encoding="utf-8") as fh:
        fh.write("\n<!-- C1 landed -->\n")
    _git(["add", plan_rel])
    _git(
        [
            "commit",
            "-q",
            "-m",
            "C1: land chunk",
            "-m",
            "Deliverable-Id: dlv-update-docs-scan-fixture-000001",
        ]
    )

    tracker_path = repo_root / "docs" / "project-tracker.md"
    stale_text = (
        "# Project Tracker\n\n"
        "### 1. Tracker reconcile fixture workstream\n"
        "**Status:** In progress — 0 of 2 chunks landed, boundary narrative "
        "untouched.\n"
        f"**Specs:** `{plan_rel}`\n\n"
    )
    tracker_path.write_text(stale_text, encoding="utf-8")

    monkeypatch.setattr(uds, "_now_utc", lambda: now)
    common_dir = repo_root / ".git"
    reply = _run(uds._ceremony_update_docs_scan({}, repo_root=common_dir))

    assert reply["tracker_reconcile_preview"] == [
        {
            "section": "Tracker reconcile fixture workstream",
            "plan_path": plan_rel,
            "old": "0 of 2",
            "new": "1 of 2",
        }
    ]
    # Read-only: the tracker file itself is untouched by this scan.
    assert tracker_path.read_text(encoding="utf-8") == stale_text


def test_ripeness_safety_guard_never_prunable(tmp_path, monkeypatch):
    """A ripe-and-unharvested plan is NEVER prunable — asserted by forcing the
    status+age signals to look disposed-and-old, then checking the guard
    (sourced from compute_curation_status) still wins."""
    now = _dt.datetime(2026, 7, 23, 12, 0, 0, tzinfo=_dt.timezone.utc)
    repo_root = _seed_repo(tmp_path, now=now)
    plans_dir = repo_root / "docs" / "plans"

    # Overwrite ripe-unharvested-plan.md's status to a disposed value the
    # classifier WOULD prune, then monkeypatch compute_curation_status to
    # report it ripe+unharvested regardless — isolating the guard itself.
    target = plans_dir / "ripe-unharvested-plan.md"
    target.write_text("---\nstatus: superseded\n---\nbody\n")
    _set_mtime(target, now - _dt.timedelta(days=30))

    from coordinator_core.distill.curation_status import ArtifactEntry, CurationStatusResult

    real_compute = uds.compute_curation_status

    def _fake_compute(worktree_root):
        result = real_compute(worktree_root)
        forced = dict(result.artifacts)
        forced["docs/plans/ripe-unharvested-plan.md"] = ArtifactEntry(
            path="docs/plans/ripe-unharvested-plan.md",
            tree="plans",
            harvested=False,
            ripe=True,
            prunable=False,
            blocked_by=[],
            last_touched=now.isoformat(),
        )
        return CurationStatusResult(
            artifacts=forced,
            unharvested_ripe_count=result.unharvested_ripe_count,
            prunable=result.prunable,
        )

    monkeypatch.setattr(uds, "compute_curation_status", _fake_compute)
    monkeypatch.setattr(uds, "_now_utc", lambda: now)

    common_dir = repo_root / ".git"
    reply = _run(uds._ceremony_update_docs_scan({}, repo_root=common_dir))

    prune_by_path = {row["path"]: row for row in reply["phase8b_prune"]}
    row = prune_by_path["docs/plans/ripe-unharvested-plan.md"]
    assert row["prunable"] is False
    assert "ripeness-safety-guard" in row["reasons"][0]


def test_lineage_backstop_blocks_predecessor(tmp_path, monkeypatch):
    now = _dt.datetime(2026, 7, 23, 12, 0, 0, tzinfo=_dt.timezone.utc)
    repo_root = _seed_repo(tmp_path, now=now)
    monkeypatch.setattr(uds, "_now_utc", lambda: now)
    common_dir = repo_root / ".git"

    reply = _run(uds._ceremony_update_docs_scan({}, repo_root=common_dir))

    edges = reply["phase8_lineage_backstop"]
    edge = next(
        e for e in edges if e["predecessor"] == "docs/plans/predecessor-plan.md"
    )
    assert edge["successor"] == "docs/plans/successor-plan.md"
    assert edge["blocked"] is True

    prune_by_path = {row["path"]: row for row in reply["phase8b_prune"]}
    predecessor_row = prune_by_path["docs/plans/predecessor-plan.md"]
    assert predecessor_row["prunable"] is False
    assert "lineage-backstop" in predecessor_row["reasons"][0]


def test_tasks_cohort_uuid_dir_immunity_and_superseded_immediate(tmp_path, monkeypatch):
    now = _dt.datetime(2026, 7, 23, 12, 0, 0, tzinfo=_dt.timezone.utc)
    repo_root = _seed_repo(tmp_path, now=now)
    monkeypatch.setattr(uds, "_now_utc", lambda: now)

    tasks_dir = repo_root / "tasks"
    tasks_dir.mkdir()
    uuid_dir = tasks_dir / "a1b861ef-0b0e-4c1a-9c3d-202607222340"
    uuid_dir.mkdir()
    (uuid_dir / "flight-recorder.md").write_text("status: superseded\n")

    superseded_file = tasks_dir / "superseded-report.md"
    superseded_file.write_text("---\nstatus: superseded\n---\nbody\n")
    _set_mtime(superseded_file, now)  # fresh — immediate rule has NO age threshold

    live_file = tasks_dir / "live-report.md"
    live_file.write_text("---\nstatus: open\n---\nbody\n")

    common_dir = repo_root / ".git"
    reply = _run(uds._ceremony_update_docs_scan({}, repo_root=common_dir))

    prune_by_path = {row["path"]: row for row in reply["phase8b_prune"]}
    assert "tasks/superseded-report.md" in prune_by_path
    assert prune_by_path["tasks/superseded-report.md"]["prunable"] is True
    assert prune_by_path["tasks/live-report.md"]["prunable"] is False
    # The UUID-dir's own contained file is never enumerated as a tasks/ row at all.
    assert not any(
        row["path"].startswith("tasks/a1b861ef-0b0e-4c1a-9c3d-202607222340")
        for row in reply["phase8b_prune"]
    )


def test_crossrepo_archive_actioned_floor(tmp_path, monkeypatch):
    now = _dt.datetime(2026, 7, 23, 12, 0, 0, tzinfo=_dt.timezone.utc)
    repo_root = _seed_repo(tmp_path, now=now)
    monkeypatch.setattr(uds, "_now_utc", lambda: now)

    archive_dir = repo_root / "cross-repo" / "archive"
    archive_dir.mkdir(parents=True)

    old_actioned = archive_dir / "old-actioned-memo.md"
    old_actioned_at = (now - _dt.timedelta(days=91)).isoformat().replace("+00:00", "Z")
    old_actioned.write_text(
        f"---\nstatus: actioned\npicked_up_at: '{old_actioned_at}'\n---\nbody\n"
    )

    recent_actioned = archive_dir / "recent-actioned-memo.md"
    recent_actioned_at = (now - _dt.timedelta(days=5)).isoformat().replace("+00:00", "Z")
    recent_actioned.write_text(
        f"---\nstatus: actioned\npicked_up_at: '{recent_actioned_at}'\n---\nbody\n"
    )

    common_dir = repo_root / ".git"
    reply = _run(uds._ceremony_update_docs_scan({}, repo_root=common_dir))

    prune_by_path = {row["path"]: row for row in reply["phase8b_prune"]}
    assert prune_by_path["cross-repo/archive/old-actioned-memo.md"]["prunable"] is True
    assert prune_by_path["cross-repo/archive/recent-actioned-memo.md"]["prunable"] is False


def test_threshold_constants_are_named_module_constants():
    """AC8 grep-assert: thresholds are data (module constants), not inline
    literals — this pins the constant NAMES so a future edit that inlines the
    number back into the classification body fails loud here."""
    assert uds.PLANS_PRUNE_AGE_DAYS == 14
    assert uds.CROSSREPO_ARCHIVE_ACTIONED_FLOOR_DAYS == 90
    assert uds.GIT_LOG_WINDOW_DAYS == 14
    assert uds.TASKS_STATUS_SUPERSEDED == "superseded"
    assert uds.TASKS_UUID_DIR_RE.match("a1b861ef-0b0e-4c1a-9c3d-202607222340")
    assert not uds.TASKS_UUID_DIR_RE.match("not-a-uuid-dir")
