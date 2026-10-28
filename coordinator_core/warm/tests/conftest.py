"""Warm-test hermeticity: the session's COORDINATOR_CLONE_ROOT must not steer the client."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _clear_clone_root_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("COORDINATOR_CLONE_ROOT", raising=False)
