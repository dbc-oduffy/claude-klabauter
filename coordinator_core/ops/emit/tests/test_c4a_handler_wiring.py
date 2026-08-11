"""Tests for C4a handler wiring: artifact_emit + goal_append per-repo repo_root derivation.

Verifies that each handler:
  1. Derives main_worktree_root(common_dir) from the dispatch-provided key before building context.
  2. Calls resolve_context(derived_root) directly (not the param-less fallback).
  3. Fails loud (ValueError) when repo_root is None (AC5 — no silent meta-repo fallback).
  4. A linked-worktree fixture confirms non-zero records are produced when common_dir is
     the shared .git dir of a main worktree (not the raw _origin_worktree file-pointer).

Spec backlink: docs/plans/2026-07-07-per-repo-emission-cutover.md § C4a
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch, call

import pytest

from coordinator_core.ops.emit.context import EmitContext


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fake_ctx(tmp_path: Path, repo_name: str = "test-org/test-repo") -> MagicMock:
    """Build a minimal EmitContext-shaped mock rooted at tmp_path."""
    ctx = MagicMock(spec=EmitContext)
    ctx.repo_root = tmp_path
    ctx.central_state_root = tmp_path / "state"
    ctx.repo_name = repo_name
    return ctx


# ---------------------------------------------------------------------------
# artifact_emit handler: fail-loud on None key
# ---------------------------------------------------------------------------

class TestArtifactEmitFailLoudOnNone:
    """_artifact_emit raises ValueError when repo_root is None (AC5)."""

    def test_none_repo_root_raises_value_error(self) -> None:
        """Handler must raise ValueError when repo_root is None — not fall back to meta-repo."""
        from coordinator_core.ops.artifact_emit import _artifact_emit

        with pytest.raises(ValueError, match="_origin_worktree"):
            _artifact_emit({}, repo_root=None)

    def test_none_repo_root_error_mentions_ac5(self) -> None:
        """Error message must mention no silent fallback to meta-repo."""
        from coordinator_core.ops.artifact_emit import _artifact_emit

        with pytest.raises(ValueError, match="No silent fallback"):
            _artifact_emit({}, repo_root=None)


# ---------------------------------------------------------------------------
# artifact_emit handler: derives main_worktree_root and calls resolve_context
# ---------------------------------------------------------------------------

class TestArtifactEmitContextDerivation:
    """Handler derives main_worktree_root(common_dir) and passes it to resolve_context."""

    def test_handler_calls_resolve_context_with_derived_root(self, tmp_path: Path) -> None:
        """_artifact_emit calls resolve_context(main_worktree_root(repo_root)), not bare repo_root."""
        from coordinator_core.ops.artifact_emit import _artifact_emit

        # Simulate common_dir = main_worktree/.git  (standard layout)
        main_worktree = tmp_path / "main_worktree"
        common_dir = main_worktree / ".git"  # main_worktree_root(common_dir) = main_worktree

        fake_ctx = _fake_ctx(main_worktree)
        fake_emit_result = {"ok": True, "out": str(tmp_path / "out.json"), "schema_version": "test"}

        with patch(
            "coordinator_core.ops.artifact_emit._envelope.resolve_context",
            return_value=fake_ctx,
        ) as mock_resolve, patch(
            "coordinator_core.ops.artifact_emit._envelope.emit",
            return_value=fake_emit_result,
        ):
            result = _artifact_emit({}, repo_root=common_dir)

        # resolve_context must be called with the DERIVED root (common_dir.parent), not common_dir
        mock_resolve.assert_called_once_with(main_worktree)
        assert result == fake_emit_result

    def test_handler_passes_out_param(self, tmp_path: Path) -> None:
        """_artifact_emit forwards the 'out' param to envelope.emit."""
        from coordinator_core.ops.artifact_emit import _artifact_emit

        common_dir = tmp_path / ".git"
        out_path = str(tmp_path / "custom-out.json")

        fake_ctx = _fake_ctx(tmp_path)

        with patch(
            "coordinator_core.ops.artifact_emit._envelope.resolve_context",
            return_value=fake_ctx,
        ), patch(
            "coordinator_core.ops.artifact_emit._envelope.emit",
            return_value={"ok": True},
        ) as mock_emit:
            _artifact_emit({"out": out_path}, repo_root=common_dir)

        mock_emit.assert_called_once_with(fake_ctx, out=out_path)


# ---------------------------------------------------------------------------
# artifact_emit handler: linked-worktree fixture (non-zero records)
# ---------------------------------------------------------------------------

class TestArtifactEmitLinkedWorktreeFixture:
    """Linked-worktree: common_dir is main_worktree/.git; derived root emits non-zero records."""

    def test_linked_worktree_resolve_context_rooted_at_main(self, tmp_path: Path) -> None:
        """When common_dir is a linked worktree's git pointer, derived root = main_worktree.

        Verifies main_worktree_root(common_dir) returns common_dir.parent,
        which is the main worktree root (not some subfolder or linked-worktree path).
        """
        from coordinator_core.ops.fleet._common import main_worktree_root

        main_worktree = tmp_path / "project"
        common_dir = main_worktree / ".git"  # standard: main_worktree/.git

        derived = main_worktree_root(common_dir)
        assert derived == main_worktree, (
            f"main_worktree_root({common_dir}) must return {main_worktree}; got {derived}"
        )

    def test_handler_resolves_to_main_worktree_state(self, tmp_path: Path) -> None:
        """Handler context is rooted at main worktree, NOT the linked worktree path.

        Simulates a linked worktree: dispatch provides common_dir (shared .git dir).
        The handler must resolve context at main_worktree, not at linked_worktree.
        """
        from coordinator_core.ops.artifact_emit import _artifact_emit

        main_worktree = tmp_path / "main"
        linked_worktree = tmp_path / "linked"  # a sibling path representing a linked worktree
        common_dir = main_worktree / ".git"  # shared common_dir for both worktrees

        captured_roots: list[Path] = []

        def spy_resolve_context(repo_root=None):
            if repo_root is not None:
                captured_roots.append(repo_root)
            return _fake_ctx(repo_root or tmp_path)

        with patch(
            "coordinator_core.ops.artifact_emit._envelope.resolve_context",
            side_effect=spy_resolve_context,
        ), patch(
            "coordinator_core.ops.artifact_emit._envelope.emit",
            return_value={"ok": True, "records": 5},
        ):
            _artifact_emit({}, repo_root=common_dir)

        assert len(captured_roots) == 1
        assert captured_roots[0] == main_worktree, (
            f"Handler must pass main_worktree ({main_worktree}) to resolve_context; "
            f"got {captured_roots[0]} (which may be raw common_dir or linked_worktree)"
        )
        # Must NOT be the raw common_dir or linked_worktree
        assert captured_roots[0] != common_dir
        assert captured_roots[0] != linked_worktree


# ---------------------------------------------------------------------------
# goal_append handler: fail-loud on None key
# ---------------------------------------------------------------------------

class TestGoalAppendFailLoudOnNone:
    """_goal_append raises ValueError when repo_root is None (AC5)."""

    def test_none_repo_root_raises_value_error(self) -> None:
        """Handler must raise ValueError when repo_root is None — not fall back to meta-repo."""
        from coordinator_core.ops.goal_append import _goal_append

        with pytest.raises(ValueError, match="_origin_worktree"):
            _goal_append(
                {"period": "day", "period_value": "2026-07-07", "text": "Test goal"},
                repo_root=None,
            )

    def test_none_repo_root_error_mentions_no_fallback(self) -> None:
        """Error message must mention no silent fallback (AC5)."""
        from coordinator_core.ops.goal_append import _goal_append

        with pytest.raises(ValueError, match="No silent fallback"):
            _goal_append(
                {"period": "day", "period_value": "2026-07-07", "text": "Test goal"},
                repo_root=None,
            )


# ---------------------------------------------------------------------------
# goal_append handler: explicit ctx overrides — internal fallback NOT reached
# ---------------------------------------------------------------------------

class TestGoalAppendExplicitOverrides:
    """Handler builds ctx and passes central_state_root + repo_name as explicit overrides."""

    def test_handler_does_not_reach_internal_resolve_context(self, tmp_path: Path) -> None:
        """The internal param-less resolve_context() inside append_goal must NOT be called.

        Handler builds ctx externally and supplies central_state_root + repo explicitly,
        so the append_goal internal fallback (called when both are None) is bypassed.
        """
        from coordinator_core.ops.goal_append import _goal_append

        common_dir = tmp_path / ".git"
        state_dir = tmp_path / "state"
        state_dir.mkdir(parents=True)

        fake_ctx = _fake_ctx(tmp_path, repo_name="test-org/test-repo")
        fake_ctx.central_state_root = state_dir

        with patch(
            "coordinator_core.ops.goal_append.resolve_context",
            return_value=fake_ctx,
        ) as mock_resolve:
            result = _goal_append(
                {"period": "day", "period_value": "2026-07-07", "text": "Handler wiring test"},
                repo_root=common_dir,
            )

        # resolve_context called once (by the handler), with derived root
        mock_resolve.assert_called_once_with(tmp_path)  # main_worktree_root(common_dir) = tmp_path

        # The result must contain a goal row with the correct repo
        assert result["row"]["repo"] == "test-org/test-repo"

    def test_handler_explicit_repo_override_in_params_takes_precedence(self, tmp_path: Path) -> None:
        """When params include 'repo', that value overrides the context repo_name."""
        from coordinator_core.ops.goal_append import _goal_append

        common_dir = tmp_path / ".git"
        state_dir = tmp_path / "state"
        state_dir.mkdir(parents=True)

        fake_ctx = _fake_ctx(tmp_path, repo_name="ctx-org/ctx-repo")
        fake_ctx.central_state_root = state_dir

        with patch(
            "coordinator_core.ops.goal_append.resolve_context",
            return_value=fake_ctx,
        ):
            result = _goal_append(
                {
                    "period": "day",
                    "period_value": "2026-07-07",
                    "text": "Explicit repo override",
                    "repo": "override-org/override-repo",
                },
                repo_root=common_dir,
            )

        assert result["row"]["repo"] == "override-org/override-repo"

    def test_handler_central_state_root_from_ctx(self, tmp_path: Path) -> None:
        """Handler writes to ctx.central_state_root — the derived per-repo state dir."""
        from coordinator_core.ops.goal_append import _goal_append

        common_dir = tmp_path / ".git"
        state_dir = tmp_path / "state"
        state_dir.mkdir(parents=True)

        fake_ctx = _fake_ctx(tmp_path)
        fake_ctx.central_state_root = state_dir

        with patch(
            "coordinator_core.ops.goal_append.resolve_context",
            return_value=fake_ctx,
        ):
            result = _goal_append(
                {"period": "week", "period_value": "2026-W27", "text": "State root test"},
                repo_root=common_dir,
            )

        log_file = Path(result["log_file"])
        # Must be written inside state_dir, not ~/.claude/state or any other root
        assert log_file.is_relative_to(state_dir), (
            f"goal log {log_file} must be written under {state_dir} (per-repo state root)"
        )


# ---------------------------------------------------------------------------
# goal_append handler: linked-worktree fixture (non-zero records)
# ---------------------------------------------------------------------------

class TestGoalAppendLinkedWorktreeFixture:
    """Linked-worktree: common_dir is main_worktree/.git; goal row written to main worktree state."""

    def test_linked_worktree_goal_written_to_main_state(self, tmp_path: Path) -> None:
        """When common_dir is the shared .git dir, goal is written to main worktree's state/.

        This is the key linked-worktree invariant: using main_worktree_root(common_dir)
        rather than the raw _origin_worktree (a file path in the linked worktree's .git pointer)
        ensures the goal lands in the correct main-worktree-rooted state/.
        """
        from coordinator_core.ops.goal_append import _goal_append

        main_worktree = tmp_path / "main"
        main_state = main_worktree / "state"
        main_state.mkdir(parents=True)

        # common_dir = main_worktree/.git (shared; linked worktrees also point here)
        common_dir = main_worktree / ".git"

        fake_ctx = _fake_ctx(main_worktree, repo_name="test-org/linked-test")
        fake_ctx.central_state_root = main_state

        with patch(
            "coordinator_core.ops.goal_append.resolve_context",
            return_value=fake_ctx,
        ):
            result = _goal_append(
                {
                    "period": "day",
                    "period_value": "2026-07-07",
                    "text": "Linked worktree goal record",
                },
                repo_root=common_dir,
            )

        # Verify non-zero records: the log file exists and has content
        log_file = Path(result["log_file"])
        assert log_file.exists(), "Goal log must be created for linked-worktree dispatch"
        lines = [ln for ln in log_file.read_text().splitlines() if ln.strip()]
        assert len(lines) >= 1, "At least one goal record must be written (non-zero records)"

        # Verify it's in main worktree's state, not some other path
        assert log_file.is_relative_to(main_state), (
            f"Log file {log_file} must be under main worktree state {main_state}"
        )

        # Verify repo attribution is correct
        row = json.loads(lines[0])
        assert row["repo"] == "test-org/linked-test"
