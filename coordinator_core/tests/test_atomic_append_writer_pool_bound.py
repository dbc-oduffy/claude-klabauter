"""append_line's live writer-thread count is bounded, independent of the number of distinct sinks."""
import os
import threading
import time

import pytest

from coordinator_core import atomic_append

pytestmark = pytest.mark.complete

_N_SINKS = 200
_LINES_PER_SINK = 3


@pytest.fixture(autouse=True)
def _no_spurious_wedge_discount(monkeypatch):
    """Steady-state bound tests must not see a GIL-starved write counted as wedged; wedge tests override this."""
    monkeypatch.setattr(atomic_append, "APPEND_DEADLINE_SECS", 30.0)


def _writer_threads() -> int:
    return sum(1 for t in threading.enumerate() if t.name.startswith("atomic-append-writer"))


def _settles_to_pool_bound() -> bool:
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        if _writer_threads() <= atomic_append._POOL_MAX:
            return True
        time.sleep(0.02)
    return False


def test_distinct_sinks_never_exceed_the_pool_bound_and_keep_order(tmp_path):
    sinks = [tmp_path / f"s{i}.txt" for i in range(_N_SINKS)]
    peak = 0
    for n in range(_LINES_PER_SINK):
        for sink in sinks:
            atomic_append.append_line(sink, f"{n}\n".encode(), wait_secs=None)
            peak = max(peak, _writer_threads())
    assert 0 < peak <= atomic_append._POOL_MAX
    for sink in sinks:
        assert sink.read_bytes() == b"0\n1\n2\n"


def test_unwaited_burst_to_many_sinks_stays_bounded(tmp_path):
    sinks = [tmp_path / f"b{i}.txt" for i in range(_N_SINKS)]
    peak = 0
    for sink in sinks:
        for n in range(_LINES_PER_SINK):
            assert atomic_append.append_line(sink, f"{n}\n".encode(), wait_secs=0) in (True, False)
            peak = max(peak, _writer_threads())
    assert peak <= atomic_append._POOL_MAX
    for sink in sinks:
        atomic_append.append_line(sink, b"end\n", wait_secs=None)
        assert sink.read_bytes() == b"0\n1\n2\nend\n"


def _wedge_pool(tmp_path, monkeypatch):
    release = threading.Event()
    entered = threading.Semaphore(0)
    real_write = atomic_append._write_once

    def gated(sink_str, encoded):
        if os.path.basename(sink_str).startswith("wedge"):
            entered.release()
            release.wait(10)
        real_write(sink_str, encoded)

    monkeypatch.setattr(atomic_append, "_write_once", gated)
    monkeypatch.setattr(atomic_append, "APPEND_DEADLINE_SECS", 0.05)
    wedged = [tmp_path / f"wedge{i}.txt" for i in range(atomic_append._POOL_MAX)]
    for sink in wedged:
        assert atomic_append.append_line(sink, b"w\n", wait_secs=0) is False
    for _ in wedged:
        assert entered.acquire(timeout=5)
    return release, wedged


def test_pool_size_wedged_sinks_do_not_starve_a_ninth_droppable_sink(tmp_path, monkeypatch):
    release, wedged = _wedge_pool(tmp_path, monkeypatch)
    ninth = tmp_path / "ninth.txt"
    try:
        time.sleep(0.15)
        atomic_append.append_line(ninth, b"x\n", droppable=True)
        assert ninth.read_bytes() == b"x\n"
        assert str(ninth) not in atomic_append.stalled_sinks()
        assert _writer_threads() <= atomic_append._POOL_MAX + len(wedged)
    finally:
        release.set()
    for sink in wedged:
        assert atomic_append.append_line(sink, b"z\n", wait_secs=5) is True
        assert sink.read_bytes() == b"w\nz\n"
    assert _settles_to_pool_bound()


def test_unwaited_sink_queued_behind_young_wedge_is_rescued(tmp_path, monkeypatch):
    release, wedged = _wedge_pool(tmp_path, monkeypatch)
    ninth = tmp_path / "ninth.txt"
    try:
        assert atomic_append.append_line(ninth, b"x\n", wait_secs=0) is False
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and not (ninth.exists() and ninth.read_bytes() == b"x\n"):
            time.sleep(0.02)
        assert ninth.read_bytes() == b"x\n"
    finally:
        release.set()
    for sink in wedged:
        assert atomic_append.append_line(sink, b"z\n", wait_secs=5) is True
    assert _settles_to_pool_bound()


def test_failure_on_one_sink_does_not_affect_others(tmp_path):
    with pytest.raises(OSError):
        atomic_append.append_line(tmp_path / "missing" / "s.txt", b"a\n", wait_secs=None)
    good = tmp_path / "good.txt"
    assert atomic_append.append_line(good, b"a\n", wait_secs=None) is True
