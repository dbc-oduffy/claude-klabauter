"""run_bounded: returns in-deadline results, drops on stall, fast-drops a stalled key."""

import threading

import pytest

from coordinator_core import bounded_io


@pytest.fixture(autouse=True)
def _clean():
    bounded_io._stalled.clear()
    bounded_io._inflight.clear()
    yield
    bounded_io._stalled.clear()
    bounded_io._inflight.clear()


def test_concurrent_callers_on_a_wedged_key_spawn_one_worker():
    release = threading.Event()
    started = []

    def wedged():
        started.append(1)
        release.wait(10)

    results = []
    callers = [
        threading.Thread(
            target=lambda: results.append(
                bounded_io.run_bounded("k-conc", wedged, deadline=0.1, default="d")
            )
        )
        for _ in range(8)
    ]
    try:
        for t in callers:
            t.start()
        for t in callers:
            t.join(3.0)
        assert results == ["d"] * 8
        assert len(started) == 1
        assert len([t for t in threading.enumerate() if t.name == "bounded-io:k-conc"]) == 1
    finally:
        release.set()


def test_result_inside_deadline():
    assert bounded_io.run_bounded("k-ok", lambda: 7, deadline=2.0, default=0) == 7


def test_exception_inside_deadline_reraises():
    def boom():
        raise ValueError("x")

    with pytest.raises(ValueError):
        bounded_io.run_bounded("k-err", boom, deadline=2.0, default=None)


def test_stall_returns_default_and_marks_key(capsys):
    release = threading.Event()
    out = bounded_io.run_bounded("k-stall", lambda: release.wait(10) and 1, deadline=0.05, default="d")
    try:
        assert out == "d"
        assert "k-stall" in bounded_io._stalled
        assert capsys.readouterr().err.count("bounded_io: stalled") == 1
    finally:
        release.set()


def test_stalled_key_drops_without_running_fn_then_recovers():
    release = threading.Event()
    bounded_io.run_bounded("k-rec", lambda: release.wait(10), deadline=0.05, default=None)
    ran = []
    assert bounded_io.run_bounded("k-rec", lambda: ran.append(1) or 5, deadline=2.0, default=-1) == -1
    assert ran == []
    worker = [t for t in threading.enumerate() if t.name == "bounded-io:k-rec"]
    release.set()
    for t in worker:
        t.join(2.0)
    assert "k-rec" not in bounded_io._stalled
    assert bounded_io.run_bounded("k-rec", lambda: 5, deadline=2.0, default=-1) == 5


def test_other_keys_unaffected_by_a_stalled_key():
    release = threading.Event()
    bounded_io.run_bounded("k-a", lambda: release.wait(10), deadline=0.05, default=None)
    try:
        assert bounded_io.run_bounded("k-b", lambda: 3, deadline=2.0, default=0) == 3
    finally:
        release.set()
