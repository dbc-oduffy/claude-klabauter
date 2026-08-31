"""
coordinator_core.ops.tests.test_repair_deployment_state_live_tree

C2 (docs/plans/2026-08-31-a-close-disposes-the-baton-it-closed.md) — the
state/handoffs/-contained sibling of the archive/handoffs/-only
`_repair_archived_deployment_state_handler`. Both share
`handoff_stamp._repair_deployment_state_impl` behind a `_DeploymentStateRepairPolicy`
object; this door's own carve-out additionally permits a `continued` record
to repair back to `ready_to_fire` — refused by the archived door by design.

Spec backlink: coordinator_core/ops/handoff_stamp.py
  ::_repair_live_deployment_state_handler, ::_repair_deployment_state_impl,
  ::_live_door_carveout, § C2 live-door carve-out

Coverage:
  (a) a `continued` record under state/handoffs/ repairs back to
      `ready_to_fire`, clearing `continued_into` and restoring
      `pickup_ready: true`, in one call.
  (b) a path outside state/handoffs/ is refused (path-containment).
  (c) `continued` requires `continued_into` — inherited cross-field rule,
      still enforced through this door.
  (d) `closed` requires `closed_reason` — inherited cross-field rule, still
      enforced through this door.
  (e) `closed` stays unconditionally terminal for this door too — only
      `continued -> ready_to_fire` is carved out, not every terminal state.
  (f) the archived door's own existing test suite (test_repair_archived_verbs.py,
      test_archive_stamp.py::TestRepairArchivedDeploymentState) is untouched
      by this file — no fixtures or handlers here are shared with it beyond
      the module import.
"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import pytest

# Import guard — MUST precede any test so @register_op fires first (mirrors
# test_repair_archived_verbs.py's own guard; this module reaches the same
# registry, including the new "handoff.repair_deployment_state" op).
import coordinator_core.ops.handoff_stamp  # noqa: F401 — fires @register_op

from coordinator_core.ops.handoff_stamp import _repair_live_deployment_state_handler
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


def _run(coro):
    """Run an async coroutine synchronously — no pytest-asyncio needed."""
    return asyncio.run(coro)


def _make_git_repo(tmp_path: Path) -> Path:
    """Minimal git repo with a committed state/handoffs/ skeleton.

    Returns repo_root (the main worktree root, NOT the .git dir) — callers
    pass repo_root / ".git" as the handler's repo_root (P9 WORKTREE
    DERIVATION: repo_root arrives as <worktree>/.git).
    """
    repo = tmp_path / "repo"
    repo.mkdir()

    def _git(*args: str) -> None:
        subprocess.run(
            ["git"] + list(args), cwd=str(repo), capture_output=True, check=True,
            **no_console_creationflags(),
        )

    _git("init", "-b", "main")
    _git("config", "user.email", "repair-live-test@claude-klabauter.test")
    _git("config", "user.name", "Repair Live Test")
    _git("config", "commit.gpgsign", "false")

    (repo / "state" / "handoffs").mkdir(parents=True, exist_ok=True)
    (repo / "state" / "handoffs" / ".gitkeep").write_text("", encoding="utf-8")
    _git("add", "-A")
    _git("commit", "-m", "chore: initial skeleton")

    return repo


def _seed_live_handoff(
    repo: Path,
    name: str,
    *,
    deployment_state: str,
    extra_fm: str = "",
) -> Path:
    """A live (state/handoffs/, never archived) handoff at the given state."""
    path = repo / "state" / "handoffs" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    fm = (
        'title: "Live Repair Target"\n'
        "created: 2026-08-31\n"
        'branch: "work/test/2026-08-31"\n'
        "status: open\n"
        "predecessor: none\n"
        f"deployment_state: {deployment_state}\n"
    )
    if extra_fm.strip():
        fm += extra_fm.strip() + "\n"
    content = f"---\n{fm}---\n\n# Handoff body.\n"
    path.write_text(content, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# (a) continued -> ready_to_fire clears continued_into, restores pickup_ready
# ---------------------------------------------------------------------------


def test_continued_to_ready_to_fire_clears_continued_into_and_restores_pickup_ready(tmp_path):
    repo = _make_git_repo(tmp_path)
    hpath = _seed_live_handoff(
        repo,
        "wrongly-continued.md",
        deployment_state="continued",
        extra_fm=(
            "continued_into: hnd-placeholder-abc123\n"
            "pickup_ready: false\n"
        ),
    )

    result = _run(_repair_live_deployment_state_handler(
        {
            "handoff_path": str(hpath),
            "reason": "same-session-race: superseded by a same-session /pickup, no real successor",
            "deployment_state": "ready_to_fire",
        },
        repo_root=repo / ".git",
    ))

    assert result["exit_code"] == 0, result
    assert result["applied"] is True
    assert result["prior_state"] == "continued"
    assert result["new_state"] == "ready_to_fire"
    assert "continued_into" in result["provenance_cleared"]
    assert result["pickup_ready_restored"] is True

    text = hpath.read_text(encoding="utf-8")
    assert "deployment_state: ready_to_fire" in text
    assert "continued_into:" not in text
    assert "pickup_ready: true" in text


# ---------------------------------------------------------------------------
# (b) path-containment: state/handoffs/-only, an outside path is refused
# ---------------------------------------------------------------------------


def test_path_outside_state_handoffs_is_refused(tmp_path):
    repo = _make_git_repo(tmp_path)
    outside = repo / "archive" / "handoffs" / "2026-08" / "outside.md"
    outside.parent.mkdir(parents=True, exist_ok=True)
    outside.write_text(
        "---\ndeployment_state: continued\ncontinued_into: hnd-x\n---\n\nBody.\n",
        encoding="utf-8",
    )

    result = _run(_repair_live_deployment_state_handler(
        {
            "handoff_path": str(outside),
            "reason": "test: path escape",
            "deployment_state": "ready_to_fire",
        },
        repo_root=repo / ".git",
    ))

    assert result["exit_code"] == 1
    assert "state/handoffs/" in result["error"]
    assert outside.read_text(encoding="utf-8") == (
        "---\ndeployment_state: continued\ncontinued_into: hnd-x\n---\n\nBody.\n"
    )


# ---------------------------------------------------------------------------
# (c) continued requires continued_into — inherited cross-field rule
# ---------------------------------------------------------------------------


def test_continued_without_continued_into_still_refuses(tmp_path):
    repo = _make_git_repo(tmp_path)
    hpath = _seed_live_handoff(repo, "no-successor.md", deployment_state="in_flight")

    result = _run(_repair_live_deployment_state_handler(
        {
            "handoff_path": str(hpath),
            "reason": "test: missing continued_into",
            "deployment_state": "continued",
        },
        repo_root=repo / ".git",
    ))

    assert result["exit_code"] == 1
    assert "continued_into" in result["error"]


# ---------------------------------------------------------------------------
# (d) closed requires closed_reason — inherited cross-field rule
# ---------------------------------------------------------------------------


def test_closed_without_closed_reason_still_refuses(tmp_path):
    repo = _make_git_repo(tmp_path)
    hpath = _seed_live_handoff(repo, "no-reason.md", deployment_state="in_flight")

    result = _run(_repair_live_deployment_state_handler(
        {
            "handoff_path": str(hpath),
            "reason": "test: missing closed_reason",
            "deployment_state": "closed",
        },
        repo_root=repo / ".git",
    ))

    assert result["exit_code"] == 1
    assert "closed_reason" in result["error"]


# ---------------------------------------------------------------------------
# (e) closed stays unconditionally terminal — the carve-out is ONE named
#     transition, not "every terminal state on this door"
# ---------------------------------------------------------------------------


def test_closed_record_still_unconditionally_terminal_on_live_door(tmp_path):
    repo = _make_git_repo(tmp_path)
    hpath = _seed_live_handoff(
        repo,
        "already-closed.md",
        deployment_state="closed",
        extra_fm="closed_reason: stale\n",
    )
    original = hpath.read_text(encoding="utf-8")

    result = _run(_repair_live_deployment_state_handler(
        {
            "handoff_path": str(hpath),
            "reason": "test: closed stays terminal even on the live door",
            "deployment_state": "ready_to_fire",
        },
        repo_root=repo / ".git",
    ))

    assert result["exit_code"] == 1
    assert hpath.read_text(encoding="utf-8") == original


def test_continued_record_targeting_closed_still_refuses(tmp_path):
    """The carve-out is narrow: continued -> ready_to_fire only. continued ->
    closed (sideways between two terminal states, not the named repair) must
    still refuse."""
    repo = _make_git_repo(tmp_path)
    hpath = _seed_live_handoff(
        repo,
        "continued-sideways.md",
        deployment_state="continued",
        extra_fm="continued_into: hnd-placeholder-xyz\n",
    )
    original = hpath.read_text(encoding="utf-8")

    result = _run(_repair_live_deployment_state_handler(
        {
            "handoff_path": str(hpath),
            "reason": "test: continued -> closed is not the carved-out transition",
            "deployment_state": "closed",
            "closed_reason": "stale",
        },
        repo_root=repo / ".git",
    ))

    assert result["exit_code"] == 1
    assert hpath.read_text(encoding="utf-8") == original
