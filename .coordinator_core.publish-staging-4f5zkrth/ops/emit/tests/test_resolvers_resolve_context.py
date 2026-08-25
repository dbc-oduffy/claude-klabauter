"""Tests for resolvers.resolve_context(repo_root=None) and _empty_skeleton (C2).

Verifies the per-repo-emission-cutover (2026-07-07) invariants on the resolvers module:
  1. resolve_context(repo_root=<path>) roots central_state_root at repo_root/state (NOT ~/.claude).
  2. resolve_context() with no args (legacy/backward-compat) still resolves without TypeError.
  3. param-less callers (artifact_emit, goal_append, recorder) import cleanly (no TypeError at import).

Note: the former AC12 top-level ``coordinator_root_path`` skeleton scalar was removed
2026-07-21 (zero readers; forbidden by the SnapshotEnvelope schema's additionalProperties:false),
so the tests that asserted its presence were removed with it.

Spec backlink: pln-per-repo-emission-cutover-un-h-03f05e § C2 / AC12
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from coordinator_core.ops.emit.resolvers import resolve_context


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_FAKE_REMOTE = "git@github.com:dbc-oduffy/project-makima.git"
_FAKE_SHA = "a" * 40


def _mock_run_git(repo_root: Path, *args: str):
    """Minimal _run_git mock: returns a valid remote, branch, and sha."""
    if "remote" in args:
        return _FAKE_REMOTE
    if "--abbrev-ref" in args:
        return "main"
    if "rev-parse" in args:
        return _FAKE_SHA
    return None


# ---------------------------------------------------------------------------
# Tests: resolve_context(repo_root=<path>) — per-repo rooting
# ---------------------------------------------------------------------------

class TestResolveContextWithRepoRoot:
    """When repo_root is passed, central_state_root is rooted at repo_root/state (not ~/.claude)."""

    def test_central_state_root_is_repo_root_state(self, tmp_path: Path) -> None:
        """resolve_context(repo_root) sets central_state_root = repo_root/state directly."""
        fake_coordinator = tmp_path / "coordinator"
        fake_coordinator.mkdir()
        (fake_coordinator / "bin").mkdir()
        (fake_coordinator / "bin" / "query-records.py").touch()

        with patch(
            "coordinator_core.ops.emit.resolvers.resolve_coordinator_root",
            return_value=fake_coordinator,
        ), patch(
            "coordinator_core.ops.emit.context._run_git",
            side_effect=_mock_run_git,
        ):
            ctx = resolve_context(repo_root=tmp_path)

        assert ctx.central_state_root == tmp_path / "state", (
            "central_state_root must be repo_root/state when repo_root is supplied"
        )

    def test_repo_root_matches_passed_path(self, tmp_path: Path) -> None:
        """ctx.repo_root equals the supplied repo_root, NOT ~/.claude."""
        fake_coordinator = tmp_path / "coordinator"
        fake_coordinator.mkdir()
        (fake_coordinator / "bin").mkdir()
        (fake_coordinator / "bin" / "query-records.py").touch()

        with patch(
            "coordinator_core.ops.emit.resolvers.resolve_coordinator_root",
            return_value=fake_coordinator,
        ), patch(
            "coordinator_core.ops.emit.context._run_git",
            side_effect=_mock_run_git,
        ):
            ctx = resolve_context(repo_root=tmp_path)

        assert ctx.repo_root == tmp_path
        # Must NOT be ~/.claude
        from pathlib import Path as _P
        import os
        claude_home = _P(os.environ.get("CLAUDE_HOME", str(_P.home()))) / ".claude"
        assert ctx.repo_root != claude_home, (
            "repo_root must be the supplied path, not ~/.claude"
        )

    def test_central_state_root_not_call_shell_seam(self, tmp_path: Path) -> None:
        """resolve_context(repo_root) does NOT call _resolve_central_state_root (shell seam)."""
        fake_coordinator = tmp_path / "coordinator"
        fake_coordinator.mkdir()
        (fake_coordinator / "bin").mkdir()
        (fake_coordinator / "bin" / "query-records.py").touch()

        with patch(
            "coordinator_core.ops.emit.resolvers.resolve_coordinator_root",
            return_value=fake_coordinator,
        ), patch(
            "coordinator_core.ops.emit.context._run_git",
            side_effect=_mock_run_git,
        ), patch(
            "coordinator_core.ops.emit.resolvers._resolve_central_state_root"
        ) as mock_seam:
            resolve_context(repo_root=tmp_path)

        mock_seam.assert_not_called()


# ---------------------------------------------------------------------------
# Tests: resolve_context() backward-compat (no repo_root arg)
# ---------------------------------------------------------------------------

@pytest.mark.real_home  # live-tree parity oracle: resolves the real coordinator root
class TestResolveContextLegacyNoArg:
    """resolve_context() with no args remains backward-compatible (no TypeError)."""

    def test_no_arg_does_not_raise_type_error(self, tmp_path: Path) -> None:
        """Calling resolve_context() without args does not raise TypeError."""
        fake_coordinator = tmp_path / "coordinator"
        fake_coordinator.mkdir()
        (fake_coordinator / "bin").mkdir()
        (fake_coordinator / "bin" / "query-records.py").touch()

        with patch(
            "coordinator_core.ops.emit.resolvers.resolve_coordinator_root",
            return_value=fake_coordinator,
        ), patch(
            "coordinator_core.ops.emit.context._run_git",
            side_effect=_mock_run_git,
        ), patch(
            "coordinator_core.ops.emit.resolvers._resolve_central_state_root",
            return_value=tmp_path / "state",
        ):
            # Must not raise TypeError — param-less call is backward-compatible.
            ctx = resolve_context()

        # Verify resolve_context() returned a real EmitContext, not just a truthy object.
        # Review: code-reviewer (Slice-4 F10) — ctx is not None is too weak; check structural fields.
        assert hasattr(ctx, "central_state_root"), "resolve_context() must return an EmitContext with central_state_root"
        assert hasattr(ctx, "repo_name"), "resolve_context() must return an EmitContext with repo_name"


# ---------------------------------------------------------------------------
# Tests: build() emits NO top-level coordinator_root_path scalar (removed 2026-07-21)
# ---------------------------------------------------------------------------



# ---------------------------------------------------------------------------
# Tests: param-less callers still import cleanly (no TypeError at import)
# ---------------------------------------------------------------------------

class TestParamlessCallersImportCleanly:
    """Param-less callers (goal_append, recorder) import without TypeError.

    `artifact_emit` was the third until 2026-08-22, when the emission artifact was
    CUT — docs/problems/2026-08-22-artifact-emit-cannot-be-earned-back-in-its-current-
    shape.md.
    """

    def test_goal_append_imports(self) -> None:
        """goal_append.py imports cleanly — resolve_context() is param-less there."""
        import coordinator_core.ops.goal_append as ga  # noqa: F401
        # Review: code-reviewer (Slice-4 F8) — 'or True' was vacuously always-true; assert real callable.
        assert callable(getattr(ga, "append_goal", None)), (
            "goal_append module must expose a callable append_goal"
        )

    def test_recorder_imports(self) -> None:
        """recorder.py imports cleanly — resolve_context() is param-less there."""
        import coordinator_core.ops.emit.recorder as rec  # noqa: F401
        # Review: code-reviewer (Slice-4 F8) — 'or True' was vacuously always-true; assert real callable.
        assert callable(getattr(rec, "record", None)), (
            "recorder module must expose a callable record function"
        )


# ---------------------------------------------------------------------------
# Tests: _registry_coordinator_root — per-key two-file registry precedence
# ---------------------------------------------------------------------------

class TestRegistryCoordinatorRoot:
    """Registry rungs 3-4: registry.local.toml wins per key, tracked
    registry.toml fills gaps (machine_resolver.registry_get semantics)."""

    def test_doe_key_only_in_tracked_registry_resolves(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """repos.doe_claude declared only in the tracked registry.toml resolves
        even when a registry.local.toml exists without the key (previously
        .local-only -- the tracked declaration was invisible)."""
        from coordinator_core.ops.emit.resolvers import _registry_coordinator_root

        doe = tmp_path / "doe"
        (doe / "coordinator" / "bin").mkdir(parents=True)
        (doe / "coordinator" / "bin" / "query-records.py").write_text("# stub\n")
        ml = tmp_path / "settings-home" / "machine-local"
        ml.mkdir(parents=True)
        (ml / "registry.local.toml").write_text("schema = 1\n", encoding="utf-8")
        # TOML LITERAL string (single quotes), never a basic one: a Windows path
        # interpolated into a basic string makes "C:\Users\..." an invalid escape, tomllib
        # raises, and the lookup silently degrades to the quoted-key regex fallback. The live
        # machine-local/registry.local.toml writes backslash paths this way for that reason —
        # the fixture must match the real on-disk shape.
        (ml / "registry.toml").write_text(f"[repos]\ndoe_claude = '{doe}'\n", encoding="utf-8")
        monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path / "settings-home"))
        assert _registry_coordinator_root() == doe / "coordinator"

    def test_local_live_path_wins_over_tracked(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A live_path declared in registry.local.toml wins over the tracked
        file's declaration of the same key."""
        from coordinator_core.ops.emit.resolvers import _registry_coordinator_root

        local_root = tmp_path / "local-root"
        (local_root / "bin").mkdir(parents=True)
        (local_root / "bin" / "query-records.py").write_text("# stub\n")
        tracked_root = tmp_path / "tracked-root"
        (tracked_root / "bin").mkdir(parents=True)
        (tracked_root / "bin" / "query-records.py").write_text("# stub\n")
        ml = tmp_path / "settings-home" / "machine-local"
        ml.mkdir(parents=True)
        # Literal (single-quoted) TOML strings — see the sibling test's note: a basic string
        # cannot carry a Windows backslash path without an invalid-escape parse failure.
        (ml / "registry.local.toml").write_text(
            "[plugin.mirrors.coordinator-claude]\n"
            f"live_path = '{local_root}'\n",
            encoding="utf-8",
        )
        (ml / "registry.toml").write_text(
            "[plugin.mirrors.coordinator-claude]\n"
            f"live_path = '{tracked_root}'\n",
            encoding="utf-8",
        )
        monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path / "settings-home"))
        assert _registry_coordinator_root() == local_root
