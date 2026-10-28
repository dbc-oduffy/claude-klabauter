"""ensure_listener's COORDINATOR_WARM_LISTENER_AUTOSTART=0 gate: no detached spawn."""

from __future__ import annotations

from pathlib import Path

import pytest

from coordinator_core.warm import supervisor


@pytest.fixture
def spawns(monkeypatch: pytest.MonkeyPatch) -> list:
    calls: list = []
    monkeypatch.setattr(supervisor, "is_engine_root", lambda root: True)
    monkeypatch.setattr(supervisor, "read_discovery", lambda root: None)
    monkeypatch.setattr(supervisor, "should_spawn", lambda root, now=None: True)
    monkeypatch.setattr(supervisor, "spawn_detached", lambda *a, **k: calls.append(a) or True)
    return calls


def test_gate_zero_never_spawns(spawns: list, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("COORDINATOR_WARM_LISTENER_AUTOSTART", "0")
    assert supervisor.ensure_listener(tmp_path) is None
    assert spawns == []


def test_gate_absent_still_spawns(spawns: list, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("COORDINATOR_WARM_LISTENER_AUTOSTART", raising=False)
    assert supervisor.ensure_listener(tmp_path) is None
    assert len(spawns) == 1


def test_gate_other_value_still_spawns(spawns: list, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("COORDINATOR_WARM_LISTENER_AUTOSTART", "1")
    supervisor.ensure_listener(tmp_path)
    assert len(spawns) == 1
