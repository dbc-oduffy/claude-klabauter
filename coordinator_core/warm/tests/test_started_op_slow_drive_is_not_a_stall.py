"""A drive that answers slowly under box load never fails an op already running on it; a probe that
never finishes still does. Admission keeps its short bound.

Trap: `_probe_drive` is stubbed to a timed or hung probe; the op body is a thread stand-in released
by an event.
"""

from __future__ import annotations

import concurrent.futures
import threading
import time

import pytest

from coordinator_core.warm import server

pytestmark = pytest.mark.deliberate_wall_clock(reason="probe age is the behaviour under test")


@pytest.fixture(autouse=True)
def _fast_drive_checks(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(server, "_DRIVE_WAIT_SLICE_SECS", 0.05)
    monkeypatch.setattr(server, "_DRIVE_VERDICT_TTL_SECS", 0.0)
    monkeypatch.setattr(server, "_DRIVE_PROBE_SECS", 0.05)
    monkeypatch.setattr(server, "_DRIVE_STARTED_STALL_AFTER_SECS", 0.6)
    server._drive_probes.clear()
    server._drive_ok_until.clear()
    yield
    server._drive_probes.clear()
    server._drive_ok_until.clear()


def _slow_probe(secs: float):
    def probe(root, box):
        time.sleep(secs)
        box["free"] = 10 * 1024**3
        box["done"] = True

    return probe


def _running_op(finish_after: float) -> "concurrent.futures.Future":
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    future = pool.submit(lambda: (time.sleep(finish_after), {"result": "ok"})[1])
    pool.shutdown(wait=False)
    return future


def test_slow_but_answering_probe_does_not_fail_a_running_op(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(server, "_probe_drive", _slow_probe(0.3))
    future = _running_op(1.0)
    assert server._await_pool_result(future, 5.0, str(tmp_path)) == {"result": "ok"}


def test_hung_probe_still_fails_a_running_op_as_drive_stall(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    release = threading.Event()
    monkeypatch.setattr(server, "_probe_drive", lambda root, box: (release.wait(10), box.update(done=True)))
    future = _running_op(5.0)
    t0 = time.monotonic()
    try:
        with pytest.raises(server._DriveFault) as fault:
            server._await_pool_result(future, 5.0, str(tmp_path))
        assert fault.value.verdict["case"] == "drive_stall"
        assert time.monotonic() - t0 >= 0.6
    finally:
        release.set()


def test_admission_probe_keeps_the_short_bound(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(server, "_probe_drive", _slow_probe(0.3))
    verdict = server._drive_verdict(str(tmp_path))
    assert verdict is not None and verdict["case"] == "drive_stall"
