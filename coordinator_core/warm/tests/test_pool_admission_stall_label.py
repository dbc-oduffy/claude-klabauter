"""A pool-admission wait writes a stall record labelled by its real leg, never `accept`."""

from __future__ import annotations

import json
import threading
import time

import pytest

from coordinator_core.warm import server

pytestmark = [pytest.mark.complete]


def _wait_for(predicate, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def _record_for(tmp_path, phase_name):
    stall_dir = tmp_path / "stall-dumps"
    disarm = server._arm_accept_stall(stall_dir, 0.05, tmp_path / "unclaimed", "k1", [phase_name])
    try:
        assert _wait_for(lambda: stall_dir.is_dir() and any(stall_dir.glob("*.txt")))
    finally:
        disarm()
    return next(stall_dir.glob("*.txt"))


@pytest.mark.parametrize(
    ("phase_name", "prefix"),
    [("gate", "pool-admission"), ("submit", "pool-admission"), ("result", "pool-result")],
)
def test_wait_record_key_and_leg_name_the_real_leg_never_accept(tmp_path, phase_name, prefix):
    record = _record_for(tmp_path, phase_name)
    assert record.name.endswith(f"-{prefix}-k1.txt")
    assert "accept" not in record.name
    leg_line = next(
        line for line in record.read_text(encoding="utf-8").splitlines() if line.startswith("leg:")
    )
    leg = json.loads(leg_line.split(":", 1)[1].strip())["leg"]
    assert f"_pool_dispatch:{phase_name}" in leg
    assert "accept" not in leg


def test_watcher_thread_is_named_for_pool_admission(tmp_path):
    stall_dir = tmp_path / "stall-dumps"
    disarm = server._arm_accept_stall(stall_dir, 30.0, tmp_path / "unclaimed", "k1", ["gate"])
    try:
        names = {t.name for t in threading.enumerate()}
        assert "pool-admission-stall" in names
        assert "accept-stall" not in names
    finally:
        disarm()
