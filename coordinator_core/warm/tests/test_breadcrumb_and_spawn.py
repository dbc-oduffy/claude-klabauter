"""Tests for `coordinator_core.warm.breadcrumb` and `coordinator/bin/
warm-engine-stop.py`.

Spec backlink: docs/plans/2026-08-16-one-engine-for-the-whole-box.md § C18

Never kills a real process: `warm-engine-stop.py`'s `_terminate_pid` takes
an injectable `psutil_module` seam exactly so this file can exercise the
terminate/escalate-to-kill logic against a fake process object instead.
Nothing here opens a real Windows named pipe either -- `_ask_server_to_stop`
is exercised through its own injectable `_open_pipe` seam, mirroring
`coordinator_core.warm.tests.test_client_fallback`'s own convention for the
sibling `warm.client` module.
"""

from __future__ import annotations

import importlib.util
import json
import time
from pathlib import Path

import pytest

from coordinator_core.warm import breadcrumb

# ---------------------------------------------------------------------------
# Import warm-engine-stop.py via importlib -- filename has dashes, not
# importable as a regular identifier (mirrors coordinator/bin/tests/
# file-attribution/test_derive_file_attribution.py's own convention).
# ---------------------------------------------------------------------------
_REPO_ROOT = Path(__file__).resolve().parents[3]
_STOP_SCRIPT_PATH = _REPO_ROOT / "coordinator" / "bin" / "warm-engine-stop.py"

_spec = importlib.util.spec_from_file_location("warm_engine_stop", _STOP_SCRIPT_PATH)
stop_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(stop_mod)


# ---------------------------------------------------------------------------
# breadcrumb.py
# ---------------------------------------------------------------------------


def test_svc_dir_and_breadcrumb_path_under_state_warm(tmp_path: Path) -> None:
    assert breadcrumb.svc_dir(tmp_path) == tmp_path / "state" / "warm"
    assert breadcrumb.breadcrumb_path(tmp_path) == tmp_path / "state" / "warm" / "warm.json"


def test_read_breadcrumb_missing_returns_none(tmp_path: Path) -> None:
    assert breadcrumb.read_breadcrumb(tmp_path) is None


def test_write_then_read_roundtrips(tmp_path: Path) -> None:
    breadcrumb.write_breadcrumb(
        pipe=r"\\.\pipe\fake",
        pid=4242,
        stable_pid_start_epoch=1234567890,
        engine_sha="deadbeef",
        engine_root=tmp_path,
    )
    record = breadcrumb.read_breadcrumb(tmp_path)
    assert record is not None
    assert record["pipe"] == r"\\.\pipe\fake"
    assert record["pid"] == 4242
    assert record["stable_pid_start_epoch"] == 1234567890
    assert record["engine_sha"] == "deadbeef"
    assert isinstance(record["started_at"], str)

    path = breadcrumb.breadcrumb_path(tmp_path)
    on_disk = json.loads(path.read_text(encoding="utf-8"))
    assert on_disk == record


def test_read_breadcrumb_corrupt_json_returns_none(tmp_path: Path) -> None:
    path = breadcrumb.breadcrumb_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("not json{{{", encoding="utf-8")
    assert breadcrumb.read_breadcrumb(tmp_path) is None


def test_read_breadcrumb_non_object_json_returns_none(tmp_path: Path) -> None:
    path = breadcrumb.breadcrumb_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("[1, 2, 3]", encoding="utf-8")
    assert breadcrumb.read_breadcrumb(tmp_path) is None


def test_unlink_breadcrumb_removes_file(tmp_path: Path) -> None:
    breadcrumb.write_breadcrumb(
        pipe="p", pid=1, stable_pid_start_epoch=1, engine_sha=None, engine_root=tmp_path
    )
    assert breadcrumb.breadcrumb_path(tmp_path).exists()
    breadcrumb.unlink_breadcrumb(tmp_path)
    assert not breadcrumb.breadcrumb_path(tmp_path).exists()


def test_unlink_breadcrumb_missing_is_a_noop(tmp_path: Path) -> None:
    breadcrumb.unlink_breadcrumb(tmp_path)  # must not raise


def test_should_spawn_true_when_no_breadcrumb(tmp_path: Path) -> None:
    assert breadcrumb.should_spawn(tmp_path) is True


def _write_started_at(tmp_path: Path, *, pid: int, stable_epoch: int, started_at: str) -> None:
    path = breadcrumb.breadcrumb_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "pipe": "p",
        "pid": pid,
        "stable_pid_start_epoch": stable_epoch,
        "engine_sha": "x",
        "started_at": started_at,
    }
    path.write_text(json.dumps(record), encoding="utf-8")


def test_should_spawn_false_when_young_and_alive(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    now = 2_000_000_000.0
    started_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - 0.5))
    _write_started_at(tmp_path, pid=999, stable_epoch=111, started_at=started_at)
    monkeypatch.setattr(breadcrumb, "stable_pid_alive", lambda pid, stored_start_epoch="": True)
    assert breadcrumb.should_spawn(tmp_path, now=now) is False


def test_should_spawn_true_when_young_but_dead(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    now = 2_000_000_000.0
    started_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - 0.5))
    _write_started_at(tmp_path, pid=999, stable_epoch=111, started_at=started_at)
    monkeypatch.setattr(breadcrumb, "stable_pid_alive", lambda pid, stored_start_epoch="": False)
    assert breadcrumb.should_spawn(tmp_path, now=now) is True


def test_should_spawn_true_when_older_than_debounce_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = 2_000_000_000.0
    started_at = time.strftime(
        "%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - breadcrumb.SPAWN_DEBOUNCE_SECS - 1)
    )
    _write_started_at(tmp_path, pid=999, stable_epoch=111, started_at=started_at)
    # Even an alive pid does not suppress spawn once the window has elapsed.
    monkeypatch.setattr(breadcrumb, "stable_pid_alive", lambda pid, stored_start_epoch="": True)
    assert breadcrumb.should_spawn(tmp_path, now=now) is True


def test_should_spawn_true_on_malformed_started_at(tmp_path: Path) -> None:
    _write_started_at(tmp_path, pid=999, stable_epoch=111, started_at="not-a-timestamp")
    assert breadcrumb.should_spawn(tmp_path) is True


def test_should_spawn_true_on_missing_psutil(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    now = 2_000_000_000.0
    started_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - 0.5))
    _write_started_at(tmp_path, pid=999, stable_epoch=111, started_at=started_at)

    def _raise(pid, stored_start_epoch=""):
        raise RuntimeError("psutil is not installed")

    monkeypatch.setattr(breadcrumb, "stable_pid_alive", _raise)
    assert breadcrumb.should_spawn(tmp_path, now=now) is True


# ---------------------------------------------------------------------------
# warm-engine-stop.py
# ---------------------------------------------------------------------------


def test_main_exits_no_breadcrumb_when_absent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(stop_mod.breadcrumb, "breadcrumb_path", lambda: tmp_path / "state" / "warm" / "warm.json")
    monkeypatch.setattr(stop_mod.breadcrumb, "read_breadcrumb", lambda: None)
    assert stop_mod.main([]) == stop_mod._EXIT_NO_BREADCRUMB


def test_main_exits_stale_breadcrumb_when_pid_dead(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    record = {
        "pipe": r"\\.\pipe\fake",
        "pid": 999999,
        "stable_pid_start_epoch": 1,
        "engine_sha": "x",
        "started_at": "2026-01-01T00:00:00Z",
    }
    monkeypatch.setattr(stop_mod.breadcrumb, "read_breadcrumb", lambda: record)
    monkeypatch.setattr(stop_mod, "stable_pid_alive", lambda pid, stored_start_epoch="": False)
    assert stop_mod.main([]) == stop_mod._EXIT_STALE_BREADCRUMB


def test_main_asks_server_and_unlinks_breadcrumb_on_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    record = {
        "pipe": r"\\.\pipe\fake",
        "pid": 4242,
        "stable_pid_start_epoch": 1,
        "engine_sha": "x",
        "started_at": "2026-01-01T00:00:00Z",
    }
    monkeypatch.setattr(stop_mod.breadcrumb, "read_breadcrumb", lambda: record)
    monkeypatch.setattr(stop_mod, "stable_pid_alive", lambda pid, stored_start_epoch="": True)
    monkeypatch.setattr(stop_mod, "_ask_server_to_stop", lambda pipe: True)
    unlinked = []
    monkeypatch.setattr(stop_mod.breadcrumb, "unlink_breadcrumb", lambda: unlinked.append(True))
    assert stop_mod.main([]) == stop_mod._EXIT_OK
    assert unlinked == [True]


def test_main_falls_back_to_terminate_when_ask_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    record = {
        "pipe": r"\\.\pipe\fake",
        "pid": 4242,
        "stable_pid_start_epoch": 1,
        "engine_sha": "x",
        "started_at": "2026-01-01T00:00:00Z",
    }
    monkeypatch.setattr(stop_mod.breadcrumb, "read_breadcrumb", lambda: record)
    monkeypatch.setattr(stop_mod, "stable_pid_alive", lambda pid, stored_start_epoch="": True)
    monkeypatch.setattr(stop_mod, "_ask_server_to_stop", lambda pipe: False)
    terminated = []
    monkeypatch.setattr(stop_mod, "_terminate_pid", lambda pid: terminated.append(pid) or True)
    unlinked = []
    monkeypatch.setattr(stop_mod.breadcrumb, "unlink_breadcrumb", lambda: unlinked.append(True))
    assert stop_mod.main([]) == stop_mod._EXIT_OK
    assert terminated == [4242]
    assert unlinked == [True]


def test_main_reports_failure_when_terminate_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    record = {
        "pipe": r"\\.\pipe\fake",
        "pid": 4242,
        "stable_pid_start_epoch": 1,
        "engine_sha": "x",
        "started_at": "2026-01-01T00:00:00Z",
    }
    monkeypatch.setattr(stop_mod.breadcrumb, "read_breadcrumb", lambda: record)
    monkeypatch.setattr(stop_mod, "stable_pid_alive", lambda pid, stored_start_epoch="": True)
    monkeypatch.setattr(stop_mod, "_ask_server_to_stop", lambda pipe: False)
    monkeypatch.setattr(stop_mod, "_terminate_pid", lambda pid: False)
    assert stop_mod.main([]) == stop_mod._EXIT_COULD_NOT_STOP


def test_ask_server_to_stop_false_on_missing_pipe(monkeypatch: pytest.MonkeyPatch) -> None:
    def _raise(pipe):
        raise FileNotFoundError(2, "no such pipe")

    monkeypatch.setattr(stop_mod, "_open_pipe", _raise)
    assert stop_mod._ask_server_to_stop(r"\\.\pipe\fake") is False


def test_ask_server_to_stop_true_when_write_succeeds() -> None:
    class _FakePipe:
        def __init__(self) -> None:
            self.written = []
            self.closed = False

        def write(self, data: bytes) -> None:
            self.written.append(data)

        def flush(self) -> None:
            pass

        def readline(self):
            return b""  # dropped connection -- expected shape of an exiting server

        def close(self) -> None:
            self.closed = True

    fake = _FakePipe()
    import pytest as _pytest

    mp = _pytest.MonkeyPatch()
    try:
        mp.setattr(stop_mod, "_open_pipe", lambda pipe: fake)
        assert stop_mod._ask_server_to_stop(r"\\.\pipe\fake") is True
        assert fake.closed is True
        sent = json.loads(fake.written[0].decode("utf-8"))
        assert sent["_engine_token"] == stop_mod._STOP_REQUEST_TOKEN
    finally:
        mp.undo()


class _FakeProcess:
    def __init__(self, alive_after_terminate: bool = False, alive_after_kill: bool = False):
        self._alive_after_terminate = alive_after_terminate
        self._alive_after_kill = alive_after_kill
        self.terminated = False
        self.killed = False

    def terminate(self) -> None:
        self.terminated = True

    def kill(self) -> None:
        self.killed = True

    def is_running(self) -> bool:
        if self.killed:
            return self._alive_after_kill
        if self.terminated:
            return self._alive_after_terminate
        return True


class _FakePsutilModule:
    class NoSuchProcess(Exception):
        pass

    class AccessDenied(Exception):
        pass

    def __init__(self, process):
        self._process = process

    def Process(self, pid):
        return self._process


def test_terminate_pid_succeeds_on_plain_terminate() -> None:
    proc = _FakeProcess(alive_after_terminate=False)
    fake_psutil = _FakePsutilModule(proc)
    assert stop_mod._terminate_pid(1234, psutil_module=fake_psutil) is True
    assert proc.terminated is True
    assert proc.killed is False


def test_terminate_pid_escalates_to_kill(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(stop_mod, "_TERMINATE_GRACE_SECS", 0.01)
    monkeypatch.setattr(stop_mod, "_TERMINATE_POLL_INTERVAL_SECS", 0.005)
    proc = _FakeProcess(alive_after_terminate=True, alive_after_kill=False)
    fake_psutil = _FakePsutilModule(proc)
    assert stop_mod._terminate_pid(1234, psutil_module=fake_psutil) is True
    assert proc.terminated is True
    assert proc.killed is True


def test_terminate_pid_reports_failure_if_kill_does_not_stick(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(stop_mod, "_TERMINATE_GRACE_SECS", 0.01)
    monkeypatch.setattr(stop_mod, "_TERMINATE_POLL_INTERVAL_SECS", 0.005)
    proc = _FakeProcess(alive_after_terminate=True, alive_after_kill=True)
    fake_psutil = _FakePsutilModule(proc)
    assert stop_mod._terminate_pid(1234, psutil_module=fake_psutil) is False
