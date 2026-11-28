"""A server thread parked at `_pool_dispatch:result` with no worker claim reaches the restart decision.

Drives the real `_arm_accept_stall` watcher with a recorder in place of `hang_restart.maybe_restart`;
stall dirs live under `tmp_path`, no live warm engine is touched.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from coordinator_core.warm import hang_restart, server

pytestmark = pytest.mark.complete

STALL = 0.05


@pytest.fixture
def decisions(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    calls: list = []
    monkeypatch.setattr(server, "_WORKER_STALL_DIR", None)
    monkeypatch.setattr(server, "_holder_drive_probe_ms", lambda _root: 1.0)
    monkeypatch.setattr(hang_restart, "maybe_restart", lambda stall_dir, **kw: calls.append(str(stall_dir)))
    monkeypatch.setattr(hang_restart, "note_stall", lambda *a, **k: None)
    return calls


def _arm(tmp_path: Path, phase: str, claim) -> tuple:
    stall_dir = tmp_path / "svc" / "stall-dumps"
    disarm = server._arm_accept_stall(stall_dir, STALL, claim, "k1", [phase], tmp_path)
    return stall_dir, disarm


def _wait(predicate, secs: float = 10.0) -> bool:
    deadline = time.monotonic() + secs
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def test_result_phase_with_no_claim_reaches_the_restart_decision(tmp_path: Path, decisions: list) -> None:
    stall_dir, disarm = _arm(tmp_path, "result", tmp_path / "claims" / "never-claimed")
    try:
        assert _wait(lambda: bool(decisions))
    finally:
        disarm()
    assert decisions
    assert len(list(stall_dir.glob("*accept-k1.txt"))) >= 1


def test_io_wait_on_a_slow_drive_never_decides_restart(
    tmp_path: Path, decisions: list, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(server, "_holder_drive_probe_ms", lambda _root: server._SLOW_DRIVE_PROBE_MS * 20)
    stall_dir, disarm = _arm(tmp_path, "result", tmp_path / "claims" / "never-claimed")
    try:
        assert _wait(lambda: len(list(stall_dir.glob("*.txt"))) >= 2)
        time.sleep(STALL * 3)
    finally:
        disarm()
    assert decisions == []
    assert all(hang_restart.IO_WAIT_HEADER in p.read_text(encoding="utf-8") for p in stall_dir.glob("*.txt"))


def test_a_request_answered_inside_the_second_wait_decides_nothing(tmp_path: Path, decisions: list) -> None:
    stall_dir, disarm = _arm(tmp_path, "result", tmp_path / "claims" / "never-claimed")
    assert _wait(lambda: bool(list(stall_dir.glob("*.txt"))))
    disarm()
    time.sleep(STALL * 4)
    assert decisions == []


def test_gate_phase_stays_excluded(tmp_path: Path, decisions: list) -> None:
    stall_dir, disarm = _arm(tmp_path, "gate", tmp_path / "claims" / "never-claimed")
    try:
        assert _wait(lambda: bool(list(stall_dir.glob("*.txt"))))
        time.sleep(STALL * 4)
    finally:
        disarm()
    assert decisions == []


def test_a_claimless_op_stays_excluded(tmp_path: Path, decisions: list) -> None:
    stall_dir, disarm = _arm(tmp_path, "result", None)
    try:
        assert _wait(lambda: bool(list(stall_dir.glob("*.txt"))))
        time.sleep(STALL * 4)
    finally:
        disarm()
    assert decisions == []


def test_held_pool_capacity_wait_stays_excluded(tmp_path: Path, decisions: list) -> None:
    stall_dir = tmp_path / "svc" / "stall-dumps"
    held = threading.Event()
    held.set()
    disarm = server._arm_accept_stall(
        stall_dir, STALL, tmp_path / "claims" / "never-claimed", "k1", ["result"], tmp_path,
        capacity_wait=held.is_set,
    )
    try:
        time.sleep(STALL * 6)
    finally:
        disarm()
    assert decisions == [] and not list(stall_dir.glob("*.txt"))
