
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from coordinator_core.ops.emit.resolvers import resolve_context


_FAKE_REMOTE = "git@github.com:dbc-oduffy/claude-klabauter.git"
_FAKE_SHA = "a" * 40


def _mock_run_git(repo_root: Path, *args: str):
    if "remote" in args:
        return _FAKE_REMOTE
    if "--abbrev-ref" in args:
        return "main"
    if "rev-parse" in args:
        return _FAKE_SHA
    return None


class TestResolveContextWithRepoRoot:

    def test_central_state_root_is_repo_root_state(self, tmp_path: Path) -> None:
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
        from pathlib import Path as _P
        import os
        claude_home = _P(os.environ.get("CLAUDE_HOME", str(_P.home()))) / ".claude"
        assert ctx.repo_root != claude_home, (
            "repo_root must be the supplied path, not ~/.claude"
        )

    def test_central_state_root_not_call_shell_seam(self, tmp_path: Path) -> None:
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


@pytest.mark.real_home
class TestResolveContextLegacyNoArg:

    def test_no_arg_does_not_raise_type_error(self, tmp_path: Path) -> None:
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
            ctx = resolve_context()

        assert hasattr(ctx, "central_state_root"), "resolve_context() must return an EmitContext with central_state_root"
        assert hasattr(ctx, "repo_name"), "resolve_context() must return an EmitContext with repo_name"


class TestParamlessCallersImportCleanly:

    def test_goal_append_imports(self) -> None:
        import coordinator_core.ops.goal_append as ga  # noqa: F401
        assert callable(getattr(ga, "append_goal", None)), (
            "goal_append module must expose a callable append_goal"
        )


class TestRegistryCoordinatorRoot:

    def test_doe_key_only_in_tracked_registry_resolves(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from coordinator_core.ops.emit.resolvers import _registry_coordinator_root

        doe = tmp_path / "doe"
        (doe / "coordinator" / "bin").mkdir(parents=True)
        (doe / "coordinator" / "bin" / "query-records.py").write_text("# stub\n")
        ml = tmp_path / "settings-home" / "machine-local"
        ml.mkdir(parents=True)
        (ml / "registry.local.toml").write_text("schema = 1\n", encoding="utf-8")
        (ml / "registry.toml").write_text(f"[repos]\ndoe_claude = '{doe}'\n", encoding="utf-8")
        monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path / "settings-home"))
        assert _registry_coordinator_root() == doe / "coordinator"

    def test_local_live_path_wins_over_tracked(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from coordinator_core.ops.emit.resolvers import _registry_coordinator_root

        local_root = tmp_path / "local-root"
        (local_root / "bin").mkdir(parents=True)
        (local_root / "bin" / "query-records.py").write_text("# stub\n")
        tracked_root = tmp_path / "tracked-root"
        (tracked_root / "bin").mkdir(parents=True)
        (tracked_root / "bin" / "query-records.py").write_text("# stub\n")
        ml = tmp_path / "settings-home" / "machine-local"
        ml.mkdir(parents=True)
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
