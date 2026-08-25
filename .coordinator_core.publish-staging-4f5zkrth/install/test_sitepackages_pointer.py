"""
coordinator_core.install.test_sitepackages_pointer — Tier T coverage for the
RETIREMENT of the site-packages pointer file `ensure_coordinator_venv` used
to publish at `<settings-home>/bin/hook-sitepackages.txt`
(docs/plans/2026-08-18-retire-coordinator-venv.md chunk C2, superseding
2026-08-10-interpreter-surface-four-asks.md chunk C5, ask (c)).

Purpose: pin the retirement contract -- `ensure_coordinator_venv` no longer
writes the pointer on any exit, `_write_sitepackages_pointer` no longer
exists as a callable, and a pointer left on disk from a pre-migration box is
left alone by `ensure_coordinator_venv` itself (its removal is the orphan
prune's job, not this function's -- see
`test_substrate_write_surface.py`/`_prune_orphaned_static_bin_names`).

Negative-spec:
  - Does NOT test reader-side (DoE `_hook_venv_inject.py`) behaviour -- that
    is verified live, not via this repo's Tier T suite (chunk C2's AC3
    real-hook verification step).
  - Does NOT exercise a real venv build/pip install -- monkeypatches
    `_venv_healthy` the same way the rest of this module's test suite fakes
    venv mechanics.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import coordinator_core.install.ensure_venv as ensure_venv_mod
from coordinator_core.install.ensure_venv import (
    SITEPACKAGES_POINTER_NAME,
    ensure_coordinator_venv,
)


@pytest.fixture(autouse=True)
def _trust_any_root(monkeypatch):
    # Mirrors test_ensure_venv.py's fixture: bypasses
    # coordinator_trusted_root_guard so an arbitrary tmp_path plugin_root
    # can be used without registering it as a real coordinator/DoE/makima
    # anchor.
    monkeypatch.setenv("COORDINATOR_PLUGIN_ROOT_TRUSTED", "1")


def _settings_home(tmp_path: Path) -> Path:
    sh = tmp_path / "settings-home"
    sh.mkdir()
    return sh


def test_write_sitepackages_pointer_no_longer_exists():
    """Chunk C2 deletes `_write_sitepackages_pointer` outright -- keeping a
    dead function around after its only call site is removed is exactly the
    "unread computation is cost" case the plan's north star names."""
    assert not hasattr(ensure_venv_mod, "_write_sitepackages_pointer")


def test_sitepackages_pointer_name_constant_still_present():
    """The constant survives the authorship retirement -- `substrate.py`'s
    Step 3e orphan-prune union still needs it to recognize a stale
    pre-migration pointer as a prune candidate rather than an orphan outside
    the mechanism meant to clean it up."""
    assert SITEPACKAGES_POINTER_NAME == "hook-sitepackages.txt"


def test_fast_path_already_healthy_does_not_write_pointer(tmp_path, monkeypatch):
    """The already-healthy fast path (no lock, no rebuild) used to be the
    exit this chunk's predecessor most easily missed. Post-retirement it
    must not write the pointer on any real success exit."""
    settings_home = _settings_home(tmp_path)
    venv_dir = settings_home / ".coordinator-venv"
    venv_dir.mkdir()

    monkeypatch.setattr(ensure_venv_mod, "_venv_healthy", lambda venv_py: True)
    monkeypatch.setattr(ensure_venv_mod, "_resolve_ml_cli", lambda plugin_root: None)

    result = ensure_coordinator_venv(tmp_path, settings_home, site="test")

    assert result == "ready"
    pointer = settings_home / "bin" / SITEPACKAGES_POINTER_NAME
    assert not pointer.exists()


def test_preexisting_pointer_from_prior_run_is_left_in_place(tmp_path, monkeypatch):
    """A pointer left on disk by a pre-migration box is not this function's
    concern to remove -- `ensure_coordinator_venv` neither writes nor
    deletes it; the self-cleaning orphan prune in `substrate.py` owns
    removal via the Step 3e union (see `SITEPACKAGES_POINTER_NAME`'s
    docstring)."""
    settings_home = _settings_home(tmp_path)
    venv_dir = settings_home / ".coordinator-venv"
    venv_dir.mkdir()
    bin_dir = settings_home / "bin"
    bin_dir.mkdir()
    stale_pointer = bin_dir / SITEPACKAGES_POINTER_NAME
    stale_pointer.write_text(str(venv_dir / "Lib" / "site-packages") + "\n", encoding="utf-8")

    monkeypatch.setattr(ensure_venv_mod, "_venv_healthy", lambda venv_py: True)
    monkeypatch.setattr(ensure_venv_mod, "_resolve_ml_cli", lambda plugin_root: None)

    result = ensure_coordinator_venv(tmp_path, settings_home, site="test")

    assert result == "ready"
    # Left untouched -- ensure_coordinator_venv is not the removal mechanism.
    assert stale_pointer.exists()


def test_check_only_still_writes_nothing(tmp_path, monkeypatch):
    """A dry-run/check-only invocation must not mutate disk -- unaffected by
    the retirement, still asserted here so a future re-add of pointer
    authorship can't slip past check_only."""
    settings_home = _settings_home(tmp_path)
    venv_dir = settings_home / ".coordinator-venv"
    venv_dir.mkdir()

    monkeypatch.setattr(ensure_venv_mod, "_venv_healthy", lambda venv_py: True)
    monkeypatch.setattr(ensure_venv_mod, "_resolve_ml_cli", lambda plugin_root: None)

    result = ensure_coordinator_venv(tmp_path, settings_home, site="test", check_only=True)

    assert result == "ready"
    pointer = settings_home / "bin" / SITEPACKAGES_POINTER_NAME
    assert not pointer.exists()
