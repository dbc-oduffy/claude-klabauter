"""
coordinator_core.ops.tests.test_handoff_children

Tests for the "handoff.has_live_children" op — focused on the path-containment
guard added to the `candidate` param (op-family path-containment sweep,
2026-07-08). This op has no pre-existing test file; coverage here is scoped to
the containment guard, not a full functional test suite for the op.

Import guard: coordinator_core.ops.handoff_children MUST be imported at module
load time so @register_op("handoff.has_live_children") fires and populates
_REGISTRY.

Coverage:
  (a) registry assertion — op name present in _REGISTRY after import
  (b) traversal-path candidate rejected — exit_code=2 (indeterminate/fail-closed)
  (c) out-of-tree absolute-path candidate rejected — exit_code=2
  (d) in-tree candidate (state/handoffs/) resolves and proceeds past the guard

Spec backlink: docs/problems/2026-07-08-op-family-path-containment-investigation.md § 4
"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

# ---------------------------------------------------------------------------
# Import guard — fires @register_op("handoff.has_live_children") as a side-effect.
# MUST precede any test function so the registry is populated before assertions.
# ---------------------------------------------------------------------------
import coordinator_core.ops.handoff_children  # noqa: F401 — fires @register_op

from coordinator_core.ipc import _REGISTRY
from coordinator_core.ops.handoff_children import _handoff_has_live_children

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_OP_NAME = "handoff.has_live_children"
assert _OP_NAME in _REGISTRY, (
    f"import guard failed: {_OP_NAME!r} not in _REGISTRY — "
    "coordinator_core.ops.handoff_children @register_op did not fire"
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run(coro):
    """Run an async coroutine synchronously — no pytest-asyncio needed."""
    return asyncio.run(coro)


def _make_git_repo(tmp_path: Path) -> Path:
    """Create a minimal git repo and return the repo root (main worktree root)."""
    repo = tmp_path / "repo"
    repo.mkdir()

    def _git(*args: str) -> None:
        subprocess.run(
            ["git"] + list(args),
            cwd=str(repo),
            capture_output=True,
            check=True,
        )

    _git("init", "-b", "main")
    _git("config", "user.email", "children-test@claude-klabauter.test")
    _git("config", "user.name", "Children Test")
    _git("config", "commit.gpgsign", "false")
    return repo


def _seed_handoff(repo: Path, subdir: str, name: str) -> Path:
    """Write a minimal handoff markdown file at repo/subdir/name."""
    path = repo / subdir / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("---\ntitle: \"Test\"\nstatus: open\n---\n\nBody.\n", encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# (a) Registry assertion
# ---------------------------------------------------------------------------


def test_op_registered():
    """handoff.has_live_children must appear in _REGISTRY (import guard validates)."""
    assert _OP_NAME in _REGISTRY


# ---------------------------------------------------------------------------
# (b)/(c)/(d) Path-containment guard on `candidate`
# ---------------------------------------------------------------------------


def test_candidate_traversal_rejected(tmp_path):
    """A candidate with '../' traversal segments escaping state/handoffs/ and
    archive/handoffs/ is rejected — exit_code=2 (fail-closed indeterminate)."""
    repo = _make_git_repo(tmp_path)
    (repo / "state" / "handoffs").mkdir(parents=True)
    secret = repo / "secret.md"
    secret.write_text("---\ntitle: \"Secret\"\n---\n", encoding="utf-8")

    with patch(
        "coordinator_core.ops.handoff_children.resolve_live_session_ids",
        return_value=frozenset(),
    ):
        result = _run(
            _handoff_has_live_children(
                {"candidate": "state/handoffs/../../secret.md"},
                repo_root=repo / ".git",
            )
        )

    assert result["exit_code"] == 2, f"traversal candidate must be rejected; got {result!r}"
    assert "referenced" not in result


def test_candidate_out_of_tree_absolute_rejected(tmp_path):
    """An out-of-tree absolute candidate (outside state/handoffs/ and
    archive/handoffs/) is rejected — exit_code=2."""
    repo = _make_git_repo(tmp_path)
    (repo / "state" / "handoffs").mkdir(parents=True)
    outside = tmp_path / "outside" / "secret.md"
    outside.parent.mkdir(parents=True)
    outside.write_text('---\ntitle: "Secret"\n---\n', encoding="utf-8")

    with patch(
        "coordinator_core.ops.handoff_children.resolve_live_session_ids",
        return_value=frozenset(),
    ):
        result = _run(
            _handoff_has_live_children(
                {"candidate": str(outside)},
                repo_root=repo / ".git",
            )
        )

    assert result["exit_code"] == 2, f"out-of-tree candidate must be rejected; got {result!r}"
    assert "referenced" not in result


def test_candidate_in_state_handoffs_passes_guard(tmp_path):
    """An in-tree candidate under state/handoffs/ resolves and proceeds past the
    containment guard (reaches the real live-set / reverse-membership logic)."""
    repo = _make_git_repo(tmp_path)
    candidate_path = _seed_handoff(repo, "state/handoffs", "candidate.md")
    _seed_handoff(repo, "state/handoffs", "other.md")

    with patch(
        "coordinator_core.ops.handoff_children.resolve_live_session_ids",
        return_value=frozenset(),
    ):
        result = _run(
            _handoff_has_live_children(
                {"candidate": str(candidate_path)},
                repo_root=repo / ".git",
            )
        )

    # No handoff references candidate.md -> not indeterminate, safe-to-archive verdict.
    assert result["exit_code"] in (0, 1), (
        f"in-tree candidate must pass the containment guard and reach a definite "
        f"verdict, not exit_code=2; got {result!r}"
    )


def test_candidate_in_archive_handoffs_passes_guard(tmp_path):
    """An in-tree candidate under archive/handoffs/ also resolves — this
    COMPUTE_ONLY op's live-set spans both state/handoffs/ and archive/handoffs/."""
    repo = _make_git_repo(tmp_path)
    (repo / "state" / "handoffs").mkdir(parents=True)
    candidate_path = _seed_handoff(repo, "archive/handoffs/2026-01", "old.md")

    with patch(
        "coordinator_core.ops.handoff_children.resolve_live_session_ids",
        return_value=frozenset(),
    ):
        result = _run(
            _handoff_has_live_children(
                {"candidate": str(candidate_path)},
                repo_root=repo / ".git",
            )
        )

    assert result["exit_code"] in (0, 1), (
        f"archive/handoffs/ candidate must pass the containment guard; got {result!r}"
    )
