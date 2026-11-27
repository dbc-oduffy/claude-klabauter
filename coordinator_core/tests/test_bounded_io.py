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


def test_is_stalled_true_while_the_overrun_worker_lives_then_false():
    release = threading.Event()
    assert bounded_io.is_stalled("k-acc") is False
    bounded_io.run_bounded("k-acc", lambda: release.wait(10), deadline=0.05, default=None)
    try:
        assert bounded_io.is_stalled("k-acc") is True
    finally:
        release.set()
        for t in [t for t in threading.enumerate() if t.name == "bounded-io:k-acc"]:
            t.join(2.0)
    assert bounded_io.is_stalled("k-acc") is False


@pytest.fixture()
def _clean_open_paths():
    bounded_io._open_paths.clear()
    yield
    bounded_io._open_paths.clear()


def test_read_bytes_bounded_returns_bytes_and_none_when_absent(tmp_path, _clean_open_paths):
    present = tmp_path / "a.json"
    present.write_bytes(b"{}")
    assert bounded_io.read_bytes_bounded(str(present)) == b"{}"
    assert bounded_io.read_bytes_bounded(str(tmp_path / "missing.json")) is None
    assert bounded_io.inflight_open_paths() == []


def test_blocked_open_raises_indeterminate_and_names_the_path(monkeypatch, tmp_path, _clean_open_paths):
    release = threading.Event()
    target = str(tmp_path / "halted.json")

    def blocked_open(path, mode="r"):
        release.wait(10)
        raise FileNotFoundError(path)

    monkeypatch.setattr(bounded_io, "open", blocked_open, raising=False)
    try:
        with pytest.raises(bounded_io.IoIndeterminate) as caught:
            bounded_io.read_bytes_bounded(target, deadline=0.05)
        assert not isinstance(caught.value, OSError)
        assert caught.value.path == target
        assert caught.value.waited_ms >= 40
        open_now = bounded_io.inflight_open_paths()
        assert [entry.path for entry in open_now] == [target]
        assert open_now[0].age_ms >= 0
        # the wedged key fast-drops a second read without another worker
        with pytest.raises(bounded_io.IoIndeterminate):
            bounded_io.read_bytes_bounded(target, deadline=0.05)
        assert len([t for t in threading.enumerate() if t.name == f"bounded-io:read:{target}"]) == 1
    finally:
        release.set()
        for t in [t for t in threading.enumerate() if t.name == f"bounded-io:read:{target}"]:
            t.join(2.0)
    assert bounded_io.inflight_open_paths() == []


def test_inflight_open_paths_is_oldest_first(_clean_open_paths):
    bounded_io._open_paths[1] = ("newer", 20.0)
    bounded_io._open_paths[2] = ("older", 10.0)
    assert [entry.path for entry in bounded_io.inflight_open_paths()] == ["older", "newer"]


def test_other_keys_unaffected_by_a_stalled_key():
    release = threading.Event()
    bounded_io.run_bounded("k-a", lambda: release.wait(10), deadline=0.05, default=None)
    try:
        assert bounded_io.run_bounded("k-b", lambda: 3, deadline=2.0, default=0) == 3
    finally:
        release.set()
