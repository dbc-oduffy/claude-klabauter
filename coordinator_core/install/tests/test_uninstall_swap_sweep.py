"""
Regression test for docs/plans/2026-08-18-retire-coordinator-venv.md chunk C3.

Before this chunk, `uninstall_legs.uninstall_remove_substrate` reclaimed
`.coordinator-venv.build-*`/`.coordinator-venv.stale-*` orphan siblings by
lazily importing `_sweep_orphaned_swap_dirs` FROM
`coordinator_core.install.ensure_venv` at call time -- the uninstall leg
depended on the venv builder it is meant to outlive. `_sweep_orphaned_swap_dirs`
now lives in `uninstall_legs` itself (`ensure_venv` imports it back for its
own internal use), so the uninstall leg's sweep no longer requires
`ensure_venv` to be importable at all.

This module asserts that with `coordinator_core.install.ensure_venv` blocked
from import entirely, `uninstall_remove_substrate` still reclaims orphaned
`.build-*`/`.stale-*` siblings on BOTH tree targets it sweeps (settings-home
and the legacy `<claude_home>/.claude/.coordinator-venv`) -- the exact two call sites
`uninstall_legs.py` carries. Fails against the pre-C3 tree (`HEAD`), where the
call sites' lazy `from coordinator_core.install.ensure_venv import
_sweep_orphaned_swap_dirs` raises `ImportError` the moment `ensure_venv`
cannot be imported, aborting the leg before either tree target's siblings are
swept.

No `cadence`/`pending_fix`/`designed_red` module marks -- this file runs on
the fast gate.
"""

from __future__ import annotations

import builtins
import sys

import pytest

from coordinator_core.install import uninstall_legs


def _block_ensure_venv_import(monkeypatch: pytest.MonkeyPatch) -> None:
    """Simulate `ensure_venv` being unimportable (e.g. deleted by a later
    retirement chunk) -- both by evicting any already-cached module entry
    and by making a fresh `import coordinator_core.install.ensure_venv`
    raise, so a lazy `from coordinator_core.install.ensure_venv import ...`
    anywhere in the call path fails loud rather than silently resolving a
    stale cached module."""
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
    """The helper is now a first-class symbol of `uninstall_legs` itself,
    not merely reachable through a lazy cross-module import -- the
    structural fix this chunk makes (relocation, not a new fallback)."""
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
    # declares that path), so the fixture builds it where substrate put it.
    dot_claude_dir = tmp_path / ".claude"
    dot_claude_dir.mkdir()
    settings_home = tmp_path / ".coordinator-claude-settings"

    # ---- settings-home tree target ----
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

    # ---- legacy tree target ----
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

    # Return value is not asserted here: registry-key clearing (surface #3)
    # can fail independently of the venv-sweep surfaces this test targets
    # (e.g. no `machine-local` CLI implementation resolvable in this test
    # sandbox) without that failure bearing on whether the swap-sibling
    # sweep itself ran to completion on both tree targets.
    uninstall_legs.uninstall_remove_substrate(force=True)

    assert not sh_venv_dir.exists()
    assert not sh_build.exists()
    assert not sh_stale.exists()
    assert sh_unrelated.exists()

    assert not legacy_venv_dir.exists()
    assert not legacy_build.exists()
    assert not legacy_stale.exists()
    assert legacy_unrelated.exists()
