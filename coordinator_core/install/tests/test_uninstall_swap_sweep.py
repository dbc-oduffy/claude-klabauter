
from __future__ import annotations

import builtins
import sys

import pytest

from coordinator_core.install import uninstall_legs


def _block_ensure_venv_import(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in list(sys.modules):
        if name == "coordinator_core.install.ensure_venv" or name.startswith(
            "coordinator_core.install.ensure_venv."
        ):
            monkeypatch.delitem(sys.modules, name, raising=False)

    real_import = builtins.__import__

    def _guarded_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "coordinator_core.install.ensure_venv":
            raise ImportError(
                "coordinator_core.install.ensure_venv is unimportable in this test "
                "(simulating its retirement) -- uninstall must not depend on it"
            )
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", _guarded_import)


def _isolate_settings_home(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    for var in ("CLAUDE_HOME", "HOME", "USERPROFILE", "COORDINATOR_SETTINGS_HOME"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path))
    monkeypatch.delenv("COORDINATOR_DISABLE_MACHINE_MUTATION", raising=False)
    monkeypatch.setenv("MACHINE_LOCAL_REGISTRY_DIR", str(tmp_path / "no-such-ml-dir"))


def test_sweep_orphaned_swap_dirs_lives_on_uninstall_legs() -> None:
    assert hasattr(uninstall_legs, "_sweep_orphaned_swap_dirs")
    assert (
        uninstall_legs._sweep_orphaned_swap_dirs.__module__
        == "coordinator_core.install.uninstall_legs"
    )


def test_uninstall_sweeps_both_tree_targets_with_ensure_venv_unimportable(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    _isolate_settings_home(monkeypatch, tmp_path)
    _block_ensure_venv_import(monkeypatch)

    # CLAUDE_HOME names the PARENT of `.claude`; the legacy venv tree target is
    # `<install_base>/.claude/.coordinator-venv` (substrate's own WRITE_SURFACE
    dot_claude_dir = tmp_path / ".claude"
    dot_claude_dir.mkdir()
    settings_home = tmp_path / ".coordinator-claude-settings"

    sh_venv_dir = settings_home / ".coordinator-venv"
    sh_venv_dir.mkdir(parents=True)
    (sh_venv_dir / "marker").write_text("live", encoding="utf-8")
    sh_build = settings_home / ".coordinator-venv.build-1111-aaaa1111"
    sh_stale = settings_home / ".coordinator-venv.stale-2222-bbbb2222"
    sh_build.mkdir()
    sh_stale.mkdir()
    (sh_build / "marker").write_text("orphan", encoding="utf-8")
    (sh_stale / "marker").write_text("orphan", encoding="utf-8")
    sh_unrelated = settings_home / ".coordinator-venv-unrelated"
    sh_unrelated.mkdir()

    legacy_venv_dir = dot_claude_dir / ".coordinator-venv"
    legacy_venv_dir.mkdir(parents=True)
    (legacy_venv_dir / "marker").write_text("live", encoding="utf-8")
    legacy_build = dot_claude_dir / ".coordinator-venv.build-3333-cccc3333"
    legacy_stale = dot_claude_dir / ".coordinator-venv.stale-4444-dddd4444"
    legacy_build.mkdir()
    legacy_stale.mkdir()
    (legacy_build / "marker").write_text("orphan", encoding="utf-8")
    (legacy_stale / "marker").write_text("orphan", encoding="utf-8")
    legacy_unrelated = dot_claude_dir / ".coordinator-venv-unrelated"
    legacy_unrelated.mkdir()

    uninstall_legs.uninstall_remove_substrate(force=True)

    assert not sh_venv_dir.exists()
    assert not sh_build.exists()
    assert not sh_stale.exists()
    assert sh_unrelated.exists()

    assert not legacy_venv_dir.exists()
    assert not legacy_build.exists()
    assert not legacy_stale.exists()
    assert legacy_unrelated.exists()
