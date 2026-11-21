"""`warm.request_status` answers a dispatch whose pool worker died with the terminal `unknowable/worker_dead`."""

from __future__ import annotations

import json
import subprocess
import sys
import time

import pytest

from coordinator_core.warm import dispatch_ack, server

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

BOOT_NS = 1_000_000
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


class _FakeIO:
    def __init__(self, lines) -> None:
        self._lines = list(lines)
        self.written: list = []

    def readline(self) -> bytes:
        return self._lines.pop(0) if self._lines else b""

    def write(self, data: bytes) -> None:
        self.written.append(data)

    def flush(self) -> None:
        pass

    def close(self) -> None:
        pass


class _VersionState:
    def is_skewed(self, client_token: str) -> bool:
        return False

    def is_token_skewed(self, client_token: str) -> bool:
        return False


def _poll(key: str) -> dict:
    """The answer the resident server gives `warm.request_status` for `key`."""
    msg = {"jsonrpc": "2.0", "id": 1, "method": "warm.request_status", "params": {"key": key}, "_engine_token": "t"}
    io_obj = _FakeIO([(json.dumps(msg) + "\n").encode("utf-8")])

    def _never(*_a, **_k):  # pragma: no cover
        raise AssertionError("request_status must not reach dispatch")

    server._handle_connection(
        io_obj, version_state=_VersionState(), server_sha="x", close_listener=lambda: None,
        drain=lambda: None, in_flight=server.InFlightCounter(), dispatch=_never,
    )
    [line] = io_obj.written
    result = json.loads(line)["result"]
    return {k: v for k, v in result.items() if k not in ("engine_boot_ns", "engine_pid")}


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch) -> dispatch_ack.AckStore:
    ack = dispatch_ack.AckStore(boot_ns=BOOT_NS, executing_bound=lambda method: 600.0)
    monkeypatch.setattr(server, "_ack_store", ack)
    return ack


@pytest.fixture
def worker():
    """A real child process standing in for a pool worker; killed at teardown."""
    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(120)"],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=_NO_WINDOW,
    )
    yield proc
    if proc.poll() is None:
        proc.kill()
    proc.wait(timeout=10)


def _admit(store: dispatch_ack.AckStore, n: int) -> str:
    key = f"1-{n}-{BOOT_NS + 100 + n}"
    assert store.admit(key, "ceremony.commit_v2")
    return key


def _bind(store: dispatch_ack.AckStore, key: str, proc: subprocess.Popen) -> None:
    store.bind_worker(key, lambda: (proc.pid, lambda: proc.poll() is None))


def test_dead_worker_reads_terminal_worker_dead_on_every_poll(store, worker) -> None:
    key = _admit(store, 1)
    _bind(store, key, worker)
    assert _poll(key) == {"state": dispatch_ack.STATE_EXECUTING}
    worker.kill()
    worker.wait(timeout=10)
    for _ in range(3):
        answer = _poll(key)
        assert answer["state"] == dispatch_ack.STATE_UNKNOWABLE
        assert answer["reason"] == "worker_dead"
        assert answer["worker_alive"] is False
        assert answer["worker_pid"] == worker.pid


def test_live_worker_still_reads_executing_past_the_overdue_bound(store, worker, monkeypatch) -> None:
    key = _admit(store, 2)
    _bind(store, key, worker)
    clock = time.monotonic()
    monkeypatch.setattr(time, "monotonic", lambda: clock + 601.0)
    assert _poll(key) == {"state": dispatch_ack.STATE_EXECUTING, "overdue": True}


def test_pid_reused_by_another_process_reads_dead(store, worker, monkeypatch) -> None:
    """The probe still reads alive, but the pid's start time is no longer the one bound."""
    key = _admit(store, 3)
    _bind(store, key, worker)
    assert store.status(key)["state"] == dispatch_ack.STATE_EXECUTING
    real = dispatch_ack.pid_start_time(worker.pid)
    assert real is not None
    monkeypatch.setattr(dispatch_ack, "pid_start_time", lambda pid: real + 3600.0)
    assert store.status(key)["reason"] == dispatch_ack.REASON_WORKER_DEAD


def test_mark_worker_dead_flags_only_the_records_that_pid_owned(store, worker) -> None:
    mine, theirs, unbound = _admit(store, 4), _admit(store, 5), _admit(store, 6)
    _bind(store, mine, worker)
    store.bind_worker(theirs, lambda: (worker.pid + 1_000_000, None))
    assert store.mark_worker_dead(worker.pid) == 1
    assert store.status(mine)["reason"] == dispatch_ack.REASON_WORKER_DEAD
    assert store.status(theirs) == {"state": dispatch_ack.STATE_EXECUTING}
    assert store.status(unbound) == {"state": dispatch_ack.STATE_EXECUTING}


def test_a_late_stamp_after_worker_dead_still_lands(store, worker) -> None:
    key = _admit(store, 7)
    _bind(store, key, worker)
    store.mark_worker_dead(worker.pid)
    store.stamp(key, dispatch_ack.OUTCOME_RESULT, result={"ok": True})
    assert store.status(key)["state"] == dispatch_ack.STATE_FINISHED
