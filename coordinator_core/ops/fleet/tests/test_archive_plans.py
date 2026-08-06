"""
coordinator_core.ops.fleet.tests.test_archive_plans — Tests for
fleet.archive_completed_plans op.

Coverage:
  (a) dry_run:true → candidates[] with all required fields; mutates nothing.
  (b) dry_run:false → archives terminal plans; source gone; dest under
      archive/specs/YYYY-MM/; git log -1 --name-only shows BOTH src+dst;
      git status --porcelain clean.
  (c) sidecar co-move alongside the primary plan.
  (d) D1 drift: plan flipped non-terminal between preview and act → skipped,
      not moved.
  (e) LIVE-REFERENCE skip: terminal plan cited by a live handoff → skipped
      reason "live-reference"; absent from the preview entirely.
  (f) IDEMPOTENT-REPLAY (AC12): re-dispatch same candidate_ids after act →
      already-archived items in skipped reason "already-archived", exit_code:0,
      mutates nothing.
  (g) CANNOT-DERIVE-DATE skip: terminal plan whose filename has no YYYY-MM-DD
      prefix → absent from preview entirely; skipped reason "cannot-derive-date"
      on act (NOT failed); exit_code:0; file not moved.

In-process dispatch pattern:
  asyncio.run() via _run() helper, no pytest-asyncio (engine is stdlib-only).
  Real handlers invoked — no _RegistryScope stub needed for fleet ops.
  _ORIGIN_WORKTREE_FIELD passes the fleet_repo root so the common_dir scope
  routes correctly to the handler.

Import guard (lesson state/lessons/2026-07-04-universal-registry-completeness-tests-ov.yaml):
  coordinator_core.ops.fleet is imported at module top so @register_op fires
  before any registry assertion.

Spec backlinks:
  - Plan (C6): docs/plans/2026-07-04-pcore-11-fleet-invoke-ops.md
  - Wire contract: coordinator_core/contract/cockpit-invoke-producer-contract.md §2.1
  - DR-211: docs/decisions/DR-211-fleet-op-substrate-write-boundary.md
"""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

# Import guard: must fire @register_op for fleet ops before any registry assertion.
import coordinator_core.ops.fleet  # noqa: F401 — side-effect: registers fleet.* ops
import coordinator_core.ops.fleet.archive_plans  # noqa: F401 — ensure the handler is registered

from coordinator_core.ipc import (
    _ORIGIN_WORKTREE_FIELD,
    _REGISTRY,
    dispatch_message,
)
from coordinator_core.ops.fleet._common import plan_claim_dir

# Patch target — name-bound in archive_plans at import time
# (``from coordinator_core.session import liveness as _session_liveness``).
_CLAIM_LIVE_PATCH = "coordinator_core.ops.fleet.archive_plans._session_liveness.claim_holder_live"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------



def _run(coro):
    """Run an async coroutine synchronously — no pytest-asyncio needed."""
    return asyncio.run(coro)


def _make_msg(method: str, params: dict, worktree: Path) -> dict:
    """Build a JSON-RPC 2.0 message for a fleet op with the given worktree."""
    return {
        "jsonrpc": "2.0",
        "id": 1,
        "method": method,
        _ORIGIN_WORKTREE_FIELD: str(worktree),
        "params": params,
    }


def _preview_msg(worktree: Path) -> dict:
    return _make_msg(
        "fleet.archive_completed_plans",
        {"mode": "already-terminal", "dry_run": True},
        worktree,
    )


def _act_msg(candidate_ids: list, worktree: Path) -> dict:
    return _make_msg(
        "fleet.archive_completed_plans",
        {
            "mode": "already-terminal",
            "dry_run": False,
            "candidate_ids": candidate_ids,
        },
        worktree,
    )


def _seed_plan_with_ac_table(
    fleet_repo, name: str, status: str, ac_rows: list, title: str = "Test Plan"
) -> Path:
    """Seed a plan (via fleet_repo.seed_plan) then append a ``## Acceptance
    Criteria`` table with the given (id, criterion, status_cell) rows, and
    commit the appended body.

    Mirrors the fleet_repo._git direct-call pattern already used elsewhere in
    this suite (e.g. test_archive_handoffs.py's rev-parse calls) — seed_plan
    itself has no body-injection param, so the AC table is appended and
    re-committed as a second step.
    """
    path = fleet_repo.seed_plan(name, status, title=title)
    table_lines = ["", "## Acceptance Criteria", "", "| ID | Criterion | Status |", "|---|---|---|"]
    for rid, criterion, stat in ac_rows:
        table_lines.append(f"| {rid} | {criterion} | {stat} |")
    table_lines.append("")
    text = path.read_text(encoding="utf-8") + "\n".join(table_lines)
    path.write_text(text, encoding="utf-8")
    fleet_repo._git("add", str(path))
    fleet_repo._git("commit", "-m", f"add AC table to {name}")
    return path


# ---------------------------------------------------------------------------
# Import-guard / floor assertion
# ---------------------------------------------------------------------------

def test_fleet_ops_registered():
    """Import guard: fleet.* ops registered; at least 1 (the plans op) must be present.

    Without the module-top import, @register_op side-effects have not fired and
    the completeness test would pass vacuously over an empty registry.
    """
    fleet_registered_ops = [k for k in _REGISTRY if k.startswith("fleet.")]
    assert len(fleet_registered_ops) >= 1, (
        f"Expected at least 1 registered fleet.* op; got {fleet_registered_ops}. "
        f"Is coordinator_core.ops.fleet imported at module top?"
    )
    assert "fleet.archive_completed_plans" in _REGISTRY, (
        "fleet.archive_completed_plans must be registered before any dispatch test"
    )


# ---------------------------------------------------------------------------
# (a) dry_run:true — candidates with required fields, mutates nothing
# ---------------------------------------------------------------------------

def test_dry_run_returns_candidates_with_required_fields(fleet_repo):
    """dry_run:true returns candidates[] each with id/title/status/family/terminal_since/note."""
    fleet_repo.seed_plan("2026-01-01-impl-plan.md", "implemented", title="Impl Plan")
    fleet_repo.seed_plan("2026-02-01-abandoned-plan.md", "abandoned", title="Abandoned Plan")
    fleet_repo.seed_plan("2026-03-01-live-plan.md", "in_progress", title="Live Plan")

    msg = _preview_msg(fleet_repo.root)
    d = _run(dispatch_message(msg))

    assert "result" in d, f"Expected result, got error: {d.get('error')}"
    result = d["result"]
    assert result["exit_code"] == 0
    assert result["dry_run"] is True
    assert result["mode"] == "already-terminal"

    candidates = result["candidates"]
    # Only terminal plans (implemented, abandoned) should appear; live plan excluded.
    candidate_ids = {c["id"] for c in candidates}
    assert "docs/plans/2026-01-01-impl-plan.md" in candidate_ids
    assert "docs/plans/2026-02-01-abandoned-plan.md" in candidate_ids
    assert "docs/plans/2026-03-01-live-plan.md" not in candidate_ids

    # Verify all required fields are present on each candidate.
    required_fields = {"id", "title", "status", "family", "terminal_since", "note"}
    for cand in candidates:
        missing = required_fields - set(cand.keys())
        assert not missing, (
            f"Candidate {cand.get('id')!r} missing required fields: {missing}"
        )
        assert cand["family"] == "plan"
        assert cand["note"] is None   # handoff-only per contract §2.1
        # terminal_since may be null for plans (contract allows it)
        assert "terminal_since" in cand  # key must be present even if null


def test_dry_run_mutates_nothing(fleet_repo):
    """dry_run:true must not move any files or create commits."""
    p = fleet_repo.seed_plan("2026-01-01-plan.md", "implemented")

    commits_before = fleet_repo.git_log_names(n=5)
    msg = _preview_msg(fleet_repo.root)
    d = _run(dispatch_message(msg))

    assert "result" in d
    # File must still be in its original location.
    assert fleet_repo.path_exists("docs/plans/2026-01-01-plan.md"), (
        "dry_run:true must NOT move the source file"
    )
    # Index must be clean.
    assert fleet_repo.git_status_clean(), (
        "dry_run:true must not dirty the git index"
    )
    # No new commits — commit count unchanged.
    commits_after = fleet_repo.git_log_names(n=5)
    assert commits_after == commits_before, (
        "dry_run:true must not create any git commits"
    )


# ---------------------------------------------------------------------------
# (b) dry_run:false — archives terminal plans, git assertions
# ---------------------------------------------------------------------------

def test_act_archives_terminal_plan(fleet_repo):
    """dry_run:false moves a terminal plan to archive/specs/YYYY-MM/; clean index."""
    fleet_repo.seed_plan("2026-07-04-terminal.md", "implemented", title="Terminal")
    fleet_repo.seed_plan("2026-07-05-live.md", "in_progress", title="Live")

    candidate_id = "docs/plans/2026-07-04-terminal.md"
    msg = _act_msg([candidate_id], fleet_repo.root)
    d = _run(dispatch_message(msg))

    assert "result" in d, f"Expected result, got: {d.get('error')}"
    result = d["result"]
    assert result["exit_code"] == 0
    assert result["dry_run"] is False

    # Source must be gone.
    assert not fleet_repo.path_exists("docs/plans/2026-07-04-terminal.md"), (
        "Source plan must be moved (not present at original path)"
    )
    # Destination must exist under archive/specs/YYYY-MM/.
    assert fleet_repo.path_exists("archive/specs/2026-07/2026-07-04-terminal.md"), (
        "Archived plan must be at archive/specs/2026-07/2026-07-04-terminal.md"
    )
    # Live plan must be untouched.
    assert fleet_repo.path_exists("docs/plans/2026-07-05-live.md"), (
        "Live plan must not be touched"
    )
    # Index must be clean.
    assert fleet_repo.git_status_clean(), (
        "git status --porcelain must be empty after archive"
    )
    # acted[] must contain the archived plan.
    assert any(a["id"] == candidate_id for a in result["acted"]), (
        f"acted[] must contain {candidate_id!r}"
    )


def test_act_git_log_shows_both_src_and_dst(fleet_repo):
    """git log -1 must enumerate BOTH src AND dst paths of the rename in the commit.

    AC4 requires the commit pathspec to include BOTH src AND dst — a dst-only
    pathspec would leave the staged deletion of src orphaned in the index
    (scoped-safety-commits.md § "Rename pathspec must include both sides").

    Verification uses --name-status which shows R100<tab>old<tab>new for renames
    (git log --name-only with git rename detection enabled shows only the new path;
    --name-status surfaces both the source and destination regardless of rename
    detection).
    """
    fleet_repo.seed_plan("2026-05-10-archived.md", "superseded", title="Superseded")

    candidate_id = "docs/plans/2026-05-10-archived.md"
    msg = _act_msg([candidate_id], fleet_repo.root)
    d = _run(dispatch_message(msg))

    assert "result" in d
    assert d["result"]["exit_code"] == 0

    # Use --name-status to see both sides of the rename: "R100\told_path\tnew_path"
    # (git log --name-only with rename detection only emits the destination path).
    result = subprocess.run(
        ["git", "log", "-1", "--name-status", "--format="],
        cwd=str(fleet_repo.root),
        capture_output=True,
        check=True,
    )
    log_output = result.stdout.decode(errors="replace").strip()

    # Both src and dst paths must appear in the --name-status output.
    assert "docs/plans/2026-05-10-archived.md" in log_output, (
        f"git log --name-status must reference the source path; output:\n{log_output}"
    )
    assert "archive/specs/2026-05/2026-05-10-archived.md" in log_output, (
        f"git log --name-status must reference the destination path; output:\n{log_output}"
    )


# ---------------------------------------------------------------------------
# (c) sidecar co-move
# ---------------------------------------------------------------------------

def test_sidecar_co_move(fleet_repo):
    """Sidecar (<stem>.*.md) is co-moved alongside the primary plan."""
    stem = "2026-06-01-with-sidecar"
    fleet_repo.seed_plan(f"{stem}.md", "implemented", title="With Sidecar")
    sidecar_path = fleet_repo.seed_plan_sidecar(stem, sidecar_suffix=".prior-art-check.md")

    candidate_id = f"docs/plans/{stem}.md"
    msg = _act_msg([candidate_id], fleet_repo.root)
    d = _run(dispatch_message(msg))

    assert "result" in d, f"Expected result: {d.get('error')}"
    result = d["result"]
    assert result["exit_code"] == 0, (
        f"exit_code must be 0 when plan + sidecar both moved; got {result['exit_code']}"
    )

    # Primary must be gone and archived.
    assert not fleet_repo.path_exists(f"docs/plans/{stem}.md")
    assert fleet_repo.path_exists(f"archive/specs/2026-06/{stem}.md")

    # Sidecar must also be moved.
    sidecar_name = f"{stem}.prior-art-check.md"
    assert not fleet_repo.path_exists(f"docs/plans/{sidecar_name}"), (
        "Sidecar must be moved from docs/plans/"
    )
    assert fleet_repo.path_exists(f"archive/specs/2026-06/{sidecar_name}"), (
        "Sidecar must land in the same archive/specs/YYYY-MM/ as the primary"
    )

    # Git index clean.
    assert fleet_repo.git_status_clean()

    # acted[] contains the primary (sidecars are supplementary, not in wire output).
    assert any(a["id"] == candidate_id for a in result["acted"])


# ---------------------------------------------------------------------------
# (d) D1 drift: flipped non-terminal between preview and act
# ---------------------------------------------------------------------------

def test_d1_drift_skipped_not_moved(fleet_repo):
    """D1 re-verify: a plan flipped non-terminal between preview and act → skipped.

    The plan appears in the preview (dry_run:true) candidates because it is terminal
    at T1.  Between T1 and T3 its status is mutated to a non-terminal value.
    The act (dry_run:false) must classify it as skipped (never moved, never failed).
    """
    fleet_repo.seed_plan("2026-04-01-drift.md", "implemented", title="Drift Test")

    # T1: preview — plan appears as a candidate.
    preview_msg = _preview_msg(fleet_repo.root)
    preview_d = _run(dispatch_message(preview_msg))
    assert "result" in preview_d
    preview_ids = [c["id"] for c in preview_d["result"]["candidates"]]
    assert "docs/plans/2026-04-01-drift.md" in preview_ids, (
        "Drift plan must appear in T1 preview while still terminal"
    )

    # Drift: mutate the plan back to a live status (simulates concurrent edit).
    fleet_repo.update_file_status(
        fleet_repo.root / "docs" / "plans" / "2026-04-01-drift.md",
        "in_progress",
    )

    # T3: act — plan must be skipped due to terminality drift.
    act_msg = _act_msg(["docs/plans/2026-04-01-drift.md"], fleet_repo.root)
    act_d = _run(dispatch_message(act_msg))

    assert "result" in act_d, f"Expected result: {act_d.get('error')}"
    result = act_d["result"]
    assert result["exit_code"] == 0, (
        f"All skipped → exit_code:0; got {result['exit_code']}"
    )

    skipped_ids = [s["id"] for s in result["skipped"]]
    assert "docs/plans/2026-04-01-drift.md" in skipped_ids, (
        "Drifted plan must appear in skipped[]"
    )
    skipped_reasons = {s["id"]: s["reason"] for s in result["skipped"]}
    assert "terminality-drift" in skipped_reasons["docs/plans/2026-04-01-drift.md"], (
        "skipped reason must mention terminality-drift"
    )
    assert not result["acted"], "acted[] must be empty when only candidate drifted"
    assert not result["failed"], "failed[] must be empty for a drift (it's a skip, not a failure)"

    # File must not have been moved.
    assert fleet_repo.path_exists("docs/plans/2026-04-01-drift.md"), (
        "Drifted plan must remain at original location"
    )


# ---------------------------------------------------------------------------
# (e) LIVE-REFERENCE skip
# ---------------------------------------------------------------------------

def test_live_reference_skip_absent_from_preview(fleet_repo):
    """Terminal plan cited in a live handoff → absent from preview and skipped on act.

    Per Key Decision 4: T1-filtering removes live-referenced plans from the human preview
    entirely (safe-default behavioral divergence — documented in plan §4).
    """
    # Seed a terminal plan.
    plan_name = "2026-03-15-live-ref.md"
    fleet_repo.seed_plan(plan_name, "abandoned", title="Live Referenced Plan")

    # Seed a live handoff that references the plan filename in its body.
    fleet_repo.seed_handoff(
        "2026-03-15-live-handoff.md",
        "active",
        extra_frontmatter=f"## references\n- {plan_name}",
    )

    # T1 preview: live-referenced plan must be absent entirely.
    preview_msg = _preview_msg(fleet_repo.root)
    preview_d = _run(dispatch_message(preview_msg))
    assert "result" in preview_d
    preview_ids = {c["id"] for c in preview_d["result"]["candidates"]}
    assert f"docs/plans/{plan_name}" not in preview_ids, (
        "Live-referenced plan must be excluded from the T1 preview (safe-default)"
    )

    # T3 act: if the caller attempts to archive it anyway (using the known path),
    # it must land in skipped reason "live-reference", not archived.
    act_msg = _act_msg([f"docs/plans/{plan_name}"], fleet_repo.root)
    act_d = _run(dispatch_message(act_msg))
    assert "result" in act_d, f"Expected result: {act_d.get('error')}"
    result = act_d["result"]

    skipped_ids = {s["id"] for s in result["skipped"]}
    assert f"docs/plans/{plan_name}" in skipped_ids, (
        "Live-referenced plan must appear in skipped[] on act"
    )
    skipped_reasons = {s["id"]: s["reason"] for s in result["skipped"]}
    assert skipped_reasons[f"docs/plans/{plan_name}"] == "live-reference", (
        f"skipped reason must be 'live-reference'; got "
        f"{skipped_reasons.get(f'docs/plans/{plan_name}')!r}"
    )
    assert not result["acted"], "acted[] must be empty"
    # File must not have been moved.
    assert fleet_repo.path_exists(f"docs/plans/{plan_name}"), (
        "Live-referenced plan must not be moved"
    )
    assert fleet_repo.git_status_clean()


# ---------------------------------------------------------------------------
# LIVE-REFERENCE scan-incomplete — glob-swallows-PermissionError regression
# guard (Key Decision 4 fail-closed extension)
#
# _collect_live_reference_text previously had NO except at all around its two
# globs (state/handoffs/*.md and docs/plans/*.md); Path.glob() silently
# swallows PermissionError while walking an unreadable directory (yields an
# empty iterator, no exception). A truncated live-reference corpus reads a
# terminal plan cited only in the unreadable subtree as "not
# live-referenced" — exactly what Key Decision 4 exists to prevent. The fix
# swaps to iterdir() and fails closed (skip archival) whenever either
# subtree cannot be fully enumerated.
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    sys.platform == "win32" or (hasattr(os, "geteuid") and os.geteuid() == 0),
    reason="chmod 0o000 permission denial is not reliable on Windows or as root",
)
def test_unreadable_handoffs_dir_fails_closed_on_preview(fleet_repo, caplog):
    """A terminal plan is NOT archived on dry-run when state/handoffs/ (part of
    the live-reference corpus) cannot be enumerated — even though the plan is
    referenced only in a handoff that lives inside the unreadable subtree.
    """
    plan_name = "2026-03-15-hidden-ref.md"
    fleet_repo.seed_plan(plan_name, "abandoned", title="Hidden Reference Plan")
    fleet_repo.seed_handoff(
        "2026-03-15-hidden-handoff.md",
        "active",
        extra_frontmatter=f"## references\n- {plan_name}",
    )

    handoffs_dir = fleet_repo.root / "state" / "handoffs"
    original_mode = handoffs_dir.stat().st_mode
    os.chmod(handoffs_dir, 0o000)
    try:
        with caplog.at_level(
            logging.WARNING, logger="coordinator_core.ops.fleet.archive_plans"
        ):
            preview_d = _run(dispatch_message(_preview_msg(fleet_repo.root)))
    finally:
        os.chmod(handoffs_dir, original_mode)

    assert "result" in preview_d, f"Expected result: {preview_d.get('error')}"
    preview_ids = {c["id"] for c in preview_d["result"]["candidates"]}
    assert f"docs/plans/{plan_name}" not in preview_ids, (
        "a plan must NOT be offered for archival when the live-reference "
        "corpus scan was incomplete — cannot rule out a live reference in "
        "the unreadable subtree"
    )
    assert any(str(handoffs_dir) in r.message for r in caplog.records), (
        "expected a logged WARNING naming the unreadable handoffs dir; "
        f"none found in: {[r.message for r in caplog.records]}"
    )


@pytest.mark.skipif(
    sys.platform == "win32" or (hasattr(os, "geteuid") and os.geteuid() == 0),
    reason="chmod 0o000 permission denial is not reliable on Windows or as root",
)
def test_unreadable_handoffs_dir_fails_closed_on_act(fleet_repo):
    """The same terminal plan, explicitly candidate_id'd on the act path while
    state/handoffs/ is unreadable, must be skipped — never archived.
    """
    plan_name = "2026-03-15-hidden-ref-act.md"
    fleet_repo.seed_plan(plan_name, "abandoned", title="Hidden Reference Plan (act)")
    fleet_repo.seed_handoff(
        "2026-03-15-hidden-handoff-act.md",
        "active",
        extra_frontmatter=f"## references\n- {plan_name}",
    )

    handoffs_dir = fleet_repo.root / "state" / "handoffs"
    original_mode = handoffs_dir.stat().st_mode
    os.chmod(handoffs_dir, 0o000)
    try:
        act_d = _run(dispatch_message(_act_msg([f"docs/plans/{plan_name}"], fleet_repo.root)))
    finally:
        os.chmod(handoffs_dir, original_mode)

    assert "result" in act_d, f"Expected result: {act_d.get('error')}"
    result = act_d["result"]
    assert result["acted"] == [], (
        "plan must NOT be archived while the live-reference corpus scan is incomplete"
    )
    skipped_reasons = {s["id"]: s["reason"] for s in result["skipped"]}
    assert skipped_reasons.get(f"docs/plans/{plan_name}") == "live-reference-scan-incomplete", (
        f"expected 'live-reference-scan-incomplete' skip reason; got {skipped_reasons!r}"
    )
    assert fleet_repo.path_exists(f"docs/plans/{plan_name}"), (
        "plan must remain in place — never moved on an incomplete scan"
    )


# Review: code-reviewer Finding 4 (2026-07-22 slicefleet-archive-silent-enum
# slice3 sidecar) — only the state/handoffs/-unreadable branch of
# _collect_live_reference_text had a chmod(0o000)-based regression test; the
# structurally-identical docs/plans/-unreadable branch was unproven. The two
# tests below mirror the pair above but chmod docs/plans/ instead.
#
# NOTE: chmod 0o100 (execute-only, no read), NOT 0o000. docs/plans/ is the
# SAME directory that houses the candidate plan under test (unlike
# state/handoffs/, which is a distinct subtree from the candidate's own
# docs/plans/ home) — a fully-unreadable-and-untraversable 0o000 would make
# Path.resolve()/`.exists()` on the candidate's own path raise/misbehave
# before the intended live-reference-scan-incomplete branch is even reached.
# 0o100 keeps traversal-by-known-name working (act's explicit candidate_id
# path) while still defeating iterdir()/glob()'s directory LISTING — which is
# exactly the failure mode _collect_live_reference_text's docs/plans/ branch
# guards against.


@pytest.mark.skipif(
    sys.platform == "win32" or (hasattr(os, "geteuid") and os.geteuid() == 0),
    reason="chmod 0o100 permission denial is not reliable on Windows or as root",
)
def test_unreadable_plans_dir_fails_closed_on_preview(fleet_repo, caplog):
    """A terminal plan is NOT offered for archival on dry-run when docs/plans/
    itself (the OTHER live-reference-corpus subtree) cannot be enumerated —
    exercises the docs/plans/ try/except in _collect_live_reference_text,
    left untested by the state/handoffs/-only pair above.
    """
    plan_name = "2026-03-16-hidden-self-ref.md"
    fleet_repo.seed_plan(plan_name, "abandoned", title="Hidden Self-Reference Plan")

    plans_dir = fleet_repo.root / "docs" / "plans"
    original_mode = plans_dir.stat().st_mode
    os.chmod(plans_dir, 0o100)
    try:
        with caplog.at_level(
            logging.WARNING, logger="coordinator_core.ops.fleet.archive_plans"
        ):
            preview_d = _run(dispatch_message(_preview_msg(fleet_repo.root)))
    finally:
        os.chmod(plans_dir, original_mode)

    assert "result" in preview_d, f"Expected result: {preview_d.get('error')}"
    preview_ids = {c["id"] for c in preview_d["result"]["candidates"]}
    assert f"docs/plans/{plan_name}" not in preview_ids, (
        "a plan must NOT be offered for archival when the live-reference "
        "corpus scan was incomplete — cannot rule out a live reference in "
        "the unreadable subtree"
    )
    assert any(str(plans_dir) in r.message for r in caplog.records), (
        "expected a logged WARNING naming the unreadable plans dir; "
        f"none found in: {[r.message for r in caplog.records]}"
    )


@pytest.mark.skipif(
    sys.platform == "win32" or (hasattr(os, "geteuid") and os.geteuid() == 0),
    reason="chmod 0o100 permission denial is not reliable on Windows or as root",
)
def test_unreadable_plans_dir_fails_closed_on_act(fleet_repo):
    """The same terminal plan, explicitly candidate_id'd on the act path
    while docs/plans/ itself cannot be enumerated, must be skipped — never
    archived. See the preview sibling's docstring for why 0o100 (not 0o000)
    is used here.
    """
    plan_name = "2026-03-16-hidden-self-ref-act.md"
    fleet_repo.seed_plan(plan_name, "abandoned", title="Hidden Self-Reference Plan (act)")

    plans_dir = fleet_repo.root / "docs" / "plans"
    original_mode = plans_dir.stat().st_mode
    os.chmod(plans_dir, 0o100)
    try:
        act_d = _run(dispatch_message(_act_msg([f"docs/plans/{plan_name}"], fleet_repo.root)))
    finally:
        os.chmod(plans_dir, original_mode)

    assert "result" in act_d, f"Expected result: {act_d.get('error')}"
    result = act_d["result"]
    assert result["acted"] == [], (
        "plan must NOT be archived while the live-reference corpus scan is incomplete"
    )
    skipped_reasons = {s["id"]: s["reason"] for s in result["skipped"]}
    assert skipped_reasons.get(f"docs/plans/{plan_name}") == "live-reference-scan-incomplete", (
        f"expected 'live-reference-scan-incomplete' skip reason; got {skipped_reasons!r}"
    )
    assert fleet_repo.path_exists(f"docs/plans/{plan_name}"), (
        "plan must remain in place — never moved on an incomplete scan"
    )


# ---------------------------------------------------------------------------
# SECURITY — path traversal containment (CRITICAL 1)
# ---------------------------------------------------------------------------

def test_path_traversal_absolute_rejected(fleet_repo):
    """Absolute-path candidate_id is rejected into failed[] before any file read or git op.

    Path('/x')/'/etc/passwd' → /etc/passwd (absolute override); the containment
    guard must catch this before parse_frontmatter_status reads it and before
    git-mv is attempted.
    """
    # Plant a valid terminal plan so there is something git-archivable in the repo —
    # confirms the rejection is not due to an empty candidate set but to the guard.
    fleet_repo.seed_plan("2026-07-01-legit.md", "implemented", title="Legit Plan")

    malicious_id = "/etc/passwd"
    msg = _act_msg([malicious_id], fleet_repo.root)
    d = _run(dispatch_message(msg))

    assert "result" in d, f"Expected result, got error: {d.get('error')}"
    result = d["result"]

    # Must land in failed[], not acted[] or skipped[].
    failed_ids = [f["id"] for f in result["failed"]]
    assert malicious_id in failed_ids, (
        f"Absolute-path candidate_id must appear in failed[]; got failed={result['failed']}"
    )
    failed_reasons = {f["id"]: f["reason"] for f in result["failed"]}
    assert "path-traversal" in failed_reasons[malicious_id], (
        f"Failure reason must mention path-traversal; got {failed_reasons[malicious_id]!r}"
    )

    # Must NOT appear in acted[].
    acted_ids = [a["id"] for a in result["acted"]]
    assert malicious_id not in acted_ids, "Malicious candidate must NOT be archived"

    # git status must be clean — no file was moved.
    assert fleet_repo.git_status_clean(), "git status must be clean after path-traversal rejection"


def test_path_traversal_dotdot_rejected(fleet_repo):
    """../ traversal candidate_id is rejected into failed[] before any file read or git op.

    A candidate_id like 'docs/plans/../../state/handoffs/secret.md' resolves outside
    docs/plans/ and must be caught by the containment guard.
    """
    fleet_repo.seed_plan("2026-07-01-legit2.md", "implemented", title="Legit Plan 2")

    # Craft a traversal that would escape docs/plans/ relative to worktree_root.
    traversal_id = "docs/plans/../../state/handoffs/escape.md"
    msg = _act_msg([traversal_id], fleet_repo.root)
    d = _run(dispatch_message(msg))

    assert "result" in d, f"Expected result, got error: {d.get('error')}"
    result = d["result"]

    failed_ids = [f["id"] for f in result["failed"]]
    assert traversal_id in failed_ids, (
        f"../ traversal candidate_id must appear in failed[]; got failed={result['failed']}"
    )
    failed_reasons = {f["id"]: f["reason"] for f in result["failed"]}
    assert "path-traversal" in failed_reasons[traversal_id], (
        f"Failure reason must mention path-traversal; got {failed_reasons[traversal_id]!r}"
    )

    # Must NOT appear in acted[].
    assert traversal_id not in [a["id"] for a in result["acted"]], (
        "../ traversal candidate must NOT be archived"
    )
    assert fleet_repo.git_status_clean(), "git status must be clean after path-traversal rejection"


def test_path_traversal_does_not_read_file(fleet_repo):
    """Traversal candidate_id must NOT cause parse_frontmatter_status to read the target.

    We seed a terminal plan and verify it is still acted correctly alongside the
    rejected traversal — confirms the traversal guard fires early (continue) and
    does not silently swallow the whole batch.
    """
    fleet_repo.seed_plan("2026-07-02-safe.md", "implemented", title="Safe Plan")

    safe_id = "docs/plans/2026-07-02-safe.md"
    malicious_id = "/etc/hostname"  # absolute path — should not be read

    msg = _act_msg([safe_id, malicious_id], fleet_repo.root)
    d = _run(dispatch_message(msg))

    assert "result" in d, f"Expected result: {d.get('error')}"
    result = d["result"]

    # Safe plan must be archived.
    acted_ids = [a["id"] for a in result["acted"]]
    assert safe_id in acted_ids, f"Safe plan must be archived; acted={acted_ids}"

    # Malicious must land in failed[].
    failed_ids = [f["id"] for f in result["failed"]]
    assert malicious_id in failed_ids, (
        f"Malicious candidate must appear in failed[]; got {result['failed']}"
    )


# ---------------------------------------------------------------------------
# SECURITY — glob injection containment (MEDIUM)
# ---------------------------------------------------------------------------

def test_glob_injection_escaped_in_sidecar_search(fleet_repo):
    """Glob metacharacters in plan filename stem do not capture unintended sidecar files.

    Plants a plan with a stem containing '[' (a glob metachar) and a decoy sidecar
    that would be captured by an unescaped glob but must NOT be matched.  Verifies
    that only the exact <stem>.*.md pattern matches (no unintended expansion).
    """
    # Primary plan with a stem that contains a glob metacharacter in the name.
    # Use a bracket character which expands as a character class in unescaped globs.
    evil_stem = "2026-07-03-plan[evil]"
    primary_name = f"{evil_stem}.md"
    fleet_repo.seed_plan(primary_name, "implemented", title="Glob Injection Test")

    # Decoy file that an unescaped glob "[evil]" might spuriously match (e.g. "e", "v", "i", "l").
    # We seed a file whose name starts with one of those chars to trigger an unescaped expand.
    decoy_name = "2026-07-03-planeXTRA.md"
    fleet_repo.seed_plan(decoy_name, "in_progress", title="Decoy")

    candidate_id = f"docs/plans/{primary_name}"
    msg = _act_msg([candidate_id], fleet_repo.root)
    d = _run(dispatch_message(msg))

    assert "result" in d, f"Expected result: {d.get('error')}"
    result = d["result"]

    # Primary plan must be archived.
    acted_ids = [a["id"] for a in result["acted"]]
    assert candidate_id in acted_ids, f"Primary plan must be archived; acted={acted_ids}"

    # Decoy must still be in docs/plans/ (not mistakenly co-moved as a sidecar).
    assert fleet_repo.path_exists(f"docs/plans/{decoy_name}"), (
        "Decoy plan must NOT be captured by sidecar glob (glob injection guard)"
    )


# ---------------------------------------------------------------------------
# (f) IDEMPOTENT REPLAY (AC12)
# ---------------------------------------------------------------------------

def test_idempotent_replay(fleet_repo):
    """AC12 — re-dispatch same candidate_ids after act → already-archived skips.

    First act: plans are moved and committed.
    Second act with identical candidate_ids: source is gone → each lands in
    skipped reason "already-archived", exit_code:0, no new moves or commits.
    """
    fleet_repo.seed_plan("2026-01-15-replay.md", "implemented", title="Replay Plan")
    fleet_repo.seed_plan("2026-01-20-replay2.md", "superseded", title="Replay Plan 2")

    candidate_ids = [
        "docs/plans/2026-01-15-replay.md",
        "docs/plans/2026-01-20-replay2.md",
    ]

    # First act: archive both plans.
    msg1 = _act_msg(candidate_ids, fleet_repo.root)
    d1 = _run(dispatch_message(msg1))
    assert "result" in d1, f"First act failed: {d1.get('error')}"
    r1 = d1["result"]
    assert r1["exit_code"] == 0
    acted_ids = {a["id"] for a in r1["acted"]}
    assert acted_ids == set(candidate_ids), (
        f"First act must archive both plans; got acted={acted_ids}"
    )

    # Snapshot git log count before replay.
    log_before = fleet_repo.git_log_names(n=1)

    # Second act (idempotent replay): same candidate_ids, sources are gone.
    msg2 = _act_msg(candidate_ids, fleet_repo.root)
    d2 = _run(dispatch_message(msg2))
    assert "result" in d2, f"Replay dispatch failed: {d2.get('error')}"
    r2 = d2["result"]

    assert r2["exit_code"] == 0, (
        f"Idempotent replay must return exit_code:0; got {r2['exit_code']}"
    )
    assert not r2["acted"], "Replay must not add new acted[] entries"
    assert not r2["failed"], "Replay must not produce failed[] entries"

    skipped_ids = {s["id"] for s in r2["skipped"]}
    assert skipped_ids == set(candidate_ids), (
        f"All previously-archived plans must appear in skipped[]; got {skipped_ids}"
    )
    skipped_reasons = {s["id"]: s["reason"] for s in r2["skipped"]}
    for cid in candidate_ids:
        assert skipped_reasons[cid] == "already-archived", (
            f"Replay skip reason must be 'already-archived' for {cid!r}; "
            f"got {skipped_reasons[cid]!r}"
        )

    # Index must be clean — replay must not dirty the working tree.
    assert fleet_repo.git_status_clean(), (
        "Idempotent replay must leave a clean git index"
    )

    # Replay must not create a new commit (git log unchanged from snapshot).
    log_after = fleet_repo.git_log_names(n=1)
    assert log_after == log_before, (
        "Idempotent replay must not create any new git commits"
    )


# ---------------------------------------------------------------------------
# (g) CANNOT-DERIVE-DATE skip
# ---------------------------------------------------------------------------

def test_cannot_derive_date_skip_absent_from_preview_and_skipped_on_act(fleet_repo):
    """Terminal plan with no YYYY-MM-DD filename prefix → skip class, not failure.

    Mirrors the live-reference skip class (case (e)): filtered at T1 preview,
    skip-guarded at T3 act, reason "cannot-derive-date", exit_code:0 (NOT 2),
    file never moved.
    """
    plan_name = "pcore-12-migration-order.md"
    fleet_repo.seed_plan(plan_name, "implemented", title="Un-dateable Plan")

    # T1 preview: plan must be absent entirely (no archive destination to preview).
    preview_msg = _preview_msg(fleet_repo.root)
    preview_d = _run(dispatch_message(preview_msg))
    assert "result" in preview_d, f"Expected result: {preview_d.get('error')}"
    preview_ids = {c["id"] for c in preview_d["result"]["candidates"]}
    assert f"docs/plans/{plan_name}" not in preview_ids, (
        "Un-dateable terminal plan must be excluded from the T1 preview"
    )

    # T3 act: dispatching act with the candidate_id directly (as if the caller
    # knew the path out-of-band) must classify it as skipped, not failed.
    act_msg = _act_msg([f"docs/plans/{plan_name}"], fleet_repo.root)
    act_d = _run(dispatch_message(act_msg))
    assert "result" in act_d, f"Expected result: {act_d.get('error')}"
    result = act_d["result"]

    assert result["exit_code"] == 0, (
        f"cannot-derive-date must be a skip, not a failure — exit_code must be 0; "
        f"got {result['exit_code']}"
    )

    skipped_ids = {s["id"] for s in result["skipped"]}
    assert f"docs/plans/{plan_name}" in skipped_ids, (
        "Un-dateable plan must appear in skipped[]"
    )
    skipped_reasons = {s["id"]: s["reason"] for s in result["skipped"]}
    assert skipped_reasons[f"docs/plans/{plan_name}"].startswith("cannot-derive-date"), (
        f"skipped reason must start with 'cannot-derive-date'; got "
        f"{skipped_reasons.get(f'docs/plans/{plan_name}')!r}"
    )

    failed_ids = {f["id"] for f in result["failed"]}
    assert f"docs/plans/{plan_name}" not in failed_ids, (
        "Un-dateable plan must NOT appear in failed[]"
    )
    acted_ids = {a["id"] for a in result["acted"]}
    assert f"docs/plans/{plan_name}" not in acted_ids, (
        "Un-dateable plan must NOT appear in acted[]"
    )

    # File must not have been moved.
    assert fleet_repo.path_exists(f"docs/plans/{plan_name}"), (
        "Un-dateable plan must remain at its original location"
    )
    assert fleet_repo.git_status_clean()


# ---------------------------------------------------------------------------
# (h) DIRTY-TREE skip guard (Zone-A)
# ---------------------------------------------------------------------------

def test_dirty_tree_excluded_from_preview(fleet_repo):
    """A terminal plan with uncommitted working-tree changes is absent from
    the T1 preview entirely (mirrors the live-reference T1-filter shape)."""
    plan_path = fleet_repo.seed_plan(
        "2026-07-19-dirty-preview.md", "implemented", title="Dirty Preview"
    )
    cid = "docs/plans/2026-07-19-dirty-preview.md"

    # Dirty the working tree WITHOUT committing (unstaged modification).
    plan_path.write_text(plan_path.read_text(encoding="utf-8") + "\nuncommitted edit\n")

    preview_d = _run(dispatch_message(_preview_msg(fleet_repo.root)))
    assert "result" in preview_d, f"Expected result: {preview_d.get('error')}"
    preview_ids = {c["id"] for c in preview_d["result"]["candidates"]}
    assert cid not in preview_ids, (
        "A plan with uncommitted working-tree changes must be excluded from "
        f"T1 preview; got candidates={preview_d['result']['candidates']!r}"
    )


def test_dirty_tree_defer_blocks_act(fleet_repo):
    """DIRTY-TREE-DEFER: a terminal plan with uncommitted working-tree changes
    is skipped (never archived) on T3 act, reason mentions 'dirty-tree'.

    Incident: 2026-07-11 — a plan was git-mv'd while it held uncommitted
    review-integration edits; this guard is the regression net.
    """
    plan_path = fleet_repo.seed_plan(
        "2026-07-19-dirty-act.md", "implemented", title="Dirty Act"
    )
    cid = "docs/plans/2026-07-19-dirty-act.md"

    plan_path.write_text(plan_path.read_text(encoding="utf-8") + "\nuncommitted edit\n")

    result = _run(dispatch_message(_act_msg([cid], fleet_repo.root)))["result"]

    assert result["exit_code"] == 0, (
        f"dirty-tree defer is a skip, not a failure — exit_code must be 0; got {result}"
    )
    assert result["acted"] == [], "A dirty-tree plan must NOT be archived"
    assert result["failed"] == [], "dirty-tree defer is a skip, not a failure"

    skip_map = {s["id"]: s["reason"] for s in result["skipped"]}
    assert cid in skip_map, f"{cid!r} must appear in skipped[]; got {result['skipped']!r}"
    assert "dirty-tree" in skip_map[cid], (
        f"skip reason must mention 'dirty-tree'; got {skip_map[cid]!r}"
    )

    # Source must still be at its original location (uncommitted edit intact).
    assert fleet_repo.path_exists(cid), "Dirty-tree plan must not have been moved"
    assert not fleet_repo.path_exists("archive/specs/2026-07/2026-07-19-dirty-act.md"), (
        "Dirty-tree plan must not have been archived"
    )


def test_clean_plan_not_excluded_by_dirty_tree_guard(fleet_repo):
    """Sanity-check: a fully-committed terminal plan (clean working tree) is
    NOT excluded by the dirty-tree guard — the guard fires ONLY on genuine
    uncommitted changes, not on every candidate."""
    fleet_repo.seed_plan("2026-07-19-clean.md", "implemented", title="Clean Plan")
    cid = "docs/plans/2026-07-19-clean.md"

    preview_d = _run(dispatch_message(_preview_msg(fleet_repo.root)))
    preview_ids = {c["id"] for c in preview_d["result"]["candidates"]}
    assert cid in preview_ids, "A clean, terminal plan must appear in preview candidates"

    result = _run(dispatch_message(_act_msg([cid], fleet_repo.root)))["result"]
    assert any(a["id"] == cid for a in result["acted"]), (
        f"A clean, terminal plan must be archived; acted={result['acted']!r}"
    )


# ---------------------------------------------------------------------------
# (i) CLAIM-LIVENESS skip guard (Zone-A)
# ---------------------------------------------------------------------------

def test_claim_liveness_live_claim_excludes_from_preview(fleet_repo):
    """A plan held by a LIVE plan-execution claim is absent from T1 preview."""
    plan_path = fleet_repo.seed_plan(
        "2026-07-19-claimed-preview.md", "implemented", title="Claimed Preview"
    )
    cid = "docs/plans/2026-07-19-claimed-preview.md"

    claim_dir = plan_claim_dir(fleet_repo.common_dir, plan_path)
    claim_dir.mkdir(parents=True, exist_ok=True)

    with patch(_CLAIM_LIVE_PATCH, return_value=True):
        preview_d = _run(dispatch_message(_preview_msg(fleet_repo.root)))

    preview_ids = {c["id"] for c in preview_d["result"]["candidates"]}
    assert cid not in preview_ids, (
        "A plan held by a live plan-execution claim must be excluded from "
        f"T1 preview; got {preview_d['result']['candidates']!r}"
    )


def test_claim_liveness_dead_claim_included_in_preview_and_archived(fleet_repo):
    """A plan whose claim dir exists but the holder is DEAD (not live) is a
    normal candidate and IS archived — the claim-liveness guard only defers
    on genuinely LIVE holders."""
    plan_path = fleet_repo.seed_plan(
        "2026-07-19-dead-claim.md", "implemented", title="Dead Claim"
    )
    cid = "docs/plans/2026-07-19-dead-claim.md"

    claim_dir = plan_claim_dir(fleet_repo.common_dir, plan_path)
    claim_dir.mkdir(parents=True, exist_ok=True)

    with patch(_CLAIM_LIVE_PATCH, return_value=False):
        preview_d = _run(dispatch_message(_preview_msg(fleet_repo.root)))
        preview_ids = {c["id"] for c in preview_d["result"]["candidates"]}
        assert cid in preview_ids, (
            "A plan with a dead claim holder must appear in preview candidates"
        )

        result = _run(dispatch_message(_act_msg([cid], fleet_repo.root)))["result"]

    assert any(a["id"] == cid for a in result["acted"]), (
        f"A plan with a dead claim holder must be archived; acted={result['acted']!r}"
    )


def test_claim_liveness_no_claim_dir_included_and_archived(fleet_repo):
    """A plan with NO claim dir on disk (the common, unclaimed case) is a
    normal candidate and archives normally — claim_holder_live is never
    consulted when the claim dir is absent (fast path)."""
    fleet_repo.seed_plan("2026-07-19-no-claim.md", "implemented", title="No Claim")
    cid = "docs/plans/2026-07-19-no-claim.md"

    # No claim dir created; no patching of claim_holder_live — if the guard
    # ever calls it despite an absent claim dir, the real (unmocked)
    # implementation would raise trying to read a live session registry that
    # doesn't exist for this claim, surfacing as an unexpected exception.
    result = _run(dispatch_message(_act_msg([cid], fleet_repo.root)))["result"]
    assert any(a["id"] == cid for a in result["acted"]), (
        f"An unclaimed plan must be archived; acted={result['acted']!r}"
    )


def test_claim_liveness_blocks_act_with_live_claim(fleet_repo):
    """T3 re-verify: a claim acquired between preview and act still blocks
    archival, reason mentions 'live-claim'."""
    plan_path = fleet_repo.seed_plan(
        "2026-07-19-claimed-act.md", "implemented", title="Claimed Act"
    )
    cid = "docs/plans/2026-07-19-claimed-act.md"

    claim_dir = plan_claim_dir(fleet_repo.common_dir, plan_path)
    claim_dir.mkdir(parents=True, exist_ok=True)

    with patch(_CLAIM_LIVE_PATCH, return_value=True):
        result = _run(dispatch_message(_act_msg([cid], fleet_repo.root)))["result"]

    assert result["exit_code"] == 0
    assert result["acted"] == []
    skip_map = {s["id"]: s["reason"] for s in result["skipped"]}
    assert cid in skip_map
    assert "live-claim" in skip_map[cid], (
        f"skip reason must mention 'live-claim'; got {skip_map[cid]!r}"
    )
    assert fleet_repo.path_exists(cid)


# ---------------------------------------------------------------------------
# (j) FAIL-CLOSED-ON-LIVENESS-ERROR — the ⚠ load-bearing safety property.
#
# An ambiguous or unavailable claim-liveness read must NEVER be treated as
# "not live, archive" — it must defer.  Incident: 2026-07-11.  ANY exception
# raised while evaluating claim_holder_live is treated as "unavailable".
# ---------------------------------------------------------------------------

def test_fail_closed_on_liveness_error_excludes_from_preview(fleet_repo):
    """claim_holder_live raising an arbitrary exception must exclude the plan
    from T1 preview — fail CLOSED (defer), never fail OPEN (archive)."""
    plan_path = fleet_repo.seed_plan(
        "2026-07-19-liveness-error-preview.md", "implemented", title="Liveness Error Preview"
    )
    cid = "docs/plans/2026-07-19-liveness-error-preview.md"

    claim_dir = plan_claim_dir(fleet_repo.common_dir, plan_path)
    claim_dir.mkdir(parents=True, exist_ok=True)

    with patch(_CLAIM_LIVE_PATCH, side_effect=RuntimeError("simulated liveness-read failure")):
        preview_d = _run(dispatch_message(_preview_msg(fleet_repo.root)))

    preview_ids = {c["id"] for c in preview_d["result"]["candidates"]}
    assert cid not in preview_ids, (
        "An unevaluable (erroring) claim-liveness read must fail CLOSED and "
        f"exclude the plan from preview; got {preview_d['result']['candidates']!r}"
    )


def test_fail_closed_on_liveness_error_blocks_act(fleet_repo):
    """claim_holder_live raising an arbitrary exception at T3 must skip the
    plan (never archive it) — fail CLOSED, never open."""
    plan_path = fleet_repo.seed_plan(
        "2026-07-19-liveness-error-act.md", "implemented", title="Liveness Error Act"
    )
    cid = "docs/plans/2026-07-19-liveness-error-act.md"

    claim_dir = plan_claim_dir(fleet_repo.common_dir, plan_path)
    claim_dir.mkdir(parents=True, exist_ok=True)

    with patch(_CLAIM_LIVE_PATCH, side_effect=RuntimeError("simulated liveness-read failure")):
        result = _run(dispatch_message(_act_msg([cid], fleet_repo.root)))["result"]

    assert result["exit_code"] == 0, (
        f"fail-closed defer is a skip, not a failure — exit_code must be 0; got {result}"
    )
    assert result["acted"] == [], (
        "A plan whose liveness read errored must NOT be archived (fail-closed)"
    )
    assert result["failed"] == [], "fail-closed defer is a skip, not a failure[] entry"

    skip_map = {s["id"]: s["reason"] for s in result["skipped"]}
    assert cid in skip_map, f"{cid!r} must appear in skipped[]; got {result['skipped']!r}"
    assert "live-claim" in skip_map[cid], (
        f"skip reason must mention 'live-claim' (the fail-closed verdict reads as "
        f"'live'); got {skip_map[cid]!r}"
    )
    assert fleet_repo.path_exists(cid), (
        "A plan whose liveness read errored must remain at its original location"
    )


def test_fail_closed_on_empty_claim_dir_value_error(fleet_repo):
    """The native claim_holder_live raises ValueError on an empty claim_dir
    string — confirm the guard's own except-Exception catches THIS specific
    exception type too (not just a generic stand-in), fail-closed.

    Regression net for a narrow except clause (e.g. ``except RuntimeError``)
    that would let ValueError propagate uncaught instead of deferring.
    """
    plan_path = fleet_repo.seed_plan(
        "2026-07-19-value-error.md", "implemented", title="Value Error"
    )
    cid = "docs/plans/2026-07-19-value-error.md"

    claim_dir = plan_claim_dir(fleet_repo.common_dir, plan_path)
    claim_dir.mkdir(parents=True, exist_ok=True)

    with patch(_CLAIM_LIVE_PATCH, side_effect=ValueError("claim_dir required")):
        result = _run(dispatch_message(_act_msg([cid], fleet_repo.root)))["result"]

    assert result["exit_code"] == 0
    assert result["acted"] == []
    skip_map = {s["id"]: s["reason"] for s in result["skipped"]}
    assert cid in skip_map
    assert "live-claim" in skip_map[cid]
    assert fleet_repo.path_exists(cid)


# ---------------------------------------------------------------------------
# (k) UNRECONCILED-AC — sixth skip guard (bookkeeping-hole detector, NOT an
# AC-completion gate; docs/plans/2026-06-30-retire-acceptance-oracle.md's
# retirement of AC-as-execution-gate stands). Applied at BOTH T1 and T3,
# mirroring the claim-liveness pair immediately above.
# ---------------------------------------------------------------------------

def test_unreconciled_ac_excluded_from_preview(fleet_repo):
    """A terminal plan with a bare-pending AC row is excluded from T1 preview
    entirely (mirrors the live-reference/dirty-tree/claim-liveness T1 filters)."""
    _seed_plan_with_ac_table(
        fleet_repo,
        "2026-07-19-unreconciled-preview.md",
        "implemented",
        [("AC-1", "does the thing", "pending"), ("AC-2", "does another thing", "done")],
        title="Unreconciled Preview",
    )
    cid = "docs/plans/2026-07-19-unreconciled-preview.md"

    preview_d = _run(dispatch_message(_preview_msg(fleet_repo.root)))
    preview_ids = {c["id"] for c in preview_d["result"]["candidates"]}
    assert cid not in preview_ids, (
        "A plan with a bare-pending AC row must be excluded from T1 preview; "
        f"got {preview_d['result']['candidates']!r}"
    )


def test_unreconciled_ac_blocks_act(fleet_repo):
    """The same plan blocks at T3 act with a skip reason naming
    'unreconciled-ac' and the pending-row count; file is not moved."""
    _seed_plan_with_ac_table(
        fleet_repo,
        "2026-07-19-unreconciled-act.md",
        "implemented",
        [
            ("AC-1", "does the thing", "☐"),
            ("AC-2", "does another thing", "[ ]"),
            ("AC-3", "does a third thing", "done"),
        ],
        title="Unreconciled Act",
    )
    cid = "docs/plans/2026-07-19-unreconciled-act.md"

    result = _run(dispatch_message(_act_msg([cid], fleet_repo.root)))["result"]

    assert result["exit_code"] == 0
    assert result["acted"] == []
    assert result["failed"] == [], "a bookkeeping hole is a skip, not a failure"
    skip_map = {s["id"]: s["reason"] for s in result["skipped"]}
    assert cid in skip_map, f"{cid!r} must appear in skipped[]; got {result['skipped']!r}"
    assert "unreconciled-ac" in skip_map[cid], (
        f"skip reason must mention 'unreconciled-ac'; got {skip_map[cid]!r}"
    )
    assert "2" in skip_map[cid], (
        f"skip reason must name the pending-row count (2 bare-pending rows, "
        f"one 'done' row excluded); got {skip_map[cid]!r}"
    )
    assert fleet_repo.path_exists(cid)


def test_all_rows_reconciled_non_done_archives_cleanly(fleet_repo):
    """Regression test for the core distinction: unreconciled != not done. A
    plan whose every AC row carries a non-done-but-terminal disposition
    (abandoned, spun-off, backlogged, deferred, superseded) is a CORRECTLY
    reconciled plan and must archive cleanly through both preview and act —
    this guard must never re-litigate AC completion
    (docs/plans/2026-06-30-retire-acceptance-oracle.md)."""
    _seed_plan_with_ac_table(
        fleet_repo,
        "2026-07-19-all-reconciled.md",
        "implemented",
        [
            ("AC-1", "does the thing", "abandoned"),
            ("AC-2", "does another thing", "spun-off"),
            ("AC-3", "does a third thing", "backlogged"),
            ("AC-4", "does a fourth thing", "deferred"),
            ("AC-5", "does a fifth thing", "superseded"),
            ("AC-6", "does a sixth thing", "pending (external)"),
        ],
        title="All Reconciled",
    )
    cid = "docs/plans/2026-07-19-all-reconciled.md"

    preview_d = _run(dispatch_message(_preview_msg(fleet_repo.root)))
    preview_ids = {c["id"] for c in preview_d["result"]["candidates"]}
    assert cid in preview_ids, (
        "A plan with only reconciled (non-bare-pending) AC dispositions must "
        f"be a normal T1 candidate; got {preview_d['result']['candidates']!r}"
    )

    result = _run(dispatch_message(_act_msg([cid], fleet_repo.root)))["result"]
    assert any(a["id"] == cid for a in result["acted"]), (
        f"A fully-reconciled plan must archive cleanly; result={result!r}"
    )
    assert not fleet_repo.path_exists(cid)


def test_no_ac_table_archives_cleanly(fleet_repo):
    """A plan with no ## Acceptance Criteria section at all is unaffected by
    this guard — AC tables are optional prose, never made mandatory."""
    plan_path = fleet_repo.seed_plan(
        "2026-07-19-no-ac-table.md", "implemented", title="No AC Table"
    )
    cid = fleet_repo.repo_rel(plan_path)

    preview_d = _run(dispatch_message(_preview_msg(fleet_repo.root)))
    preview_ids = {c["id"] for c in preview_d["result"]["candidates"]}
    assert cid in preview_ids

    result = _run(dispatch_message(_act_msg([cid], fleet_repo.root)))["result"]
    assert any(a["id"] == cid for a in result["acted"])
    assert not fleet_repo.path_exists(cid)
