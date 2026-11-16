"""Warm-test hermeticity: the session's COORDINATOR_CLONE_ROOT must not steer the client."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _clear_clone_root_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("COORDINATOR_CLONE_ROOT", raising=False)


@pytest.fixture(autouse=True)
def _clear_hang_progress(monkeypatch: pytest.MonkeyPatch) -> None:
    """Trap: any in-process server test calls `hang_restart.note_progress()`, and the module-global
    stamp then reads as live progress in a later test, so its restart verdict flips to
    `suppressed reason=progress` depending on how fast the run is."""
    from coordinator_core.warm import hang_restart

    monkeypatch.setattr(hang_restart, "_progress_last", None)
