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


def test_svc_dir_lives_outside_the_engine_clone(tmp_path: Path) -> None:
    """PM ruling 2026-08-19: `state/` is for active repos and must not exist
    in a publish mirror. The warm engine runs from the klabauter publish
    clone, so its runtime state moved OUT of `<clone>/state/warm/` — which
    had also made the mirror unpublishable, the end-of-run
    unscanned-published check walking the filesystem rather than git."""
    resolved = breadcrumb.svc_dir(tmp_path)
    assert tmp_path not in resolved.parents and resolved != tmp_path, (
        f"warm runtime state resolved inside the engine clone: {resolved}"
    )
    assert "state" not in resolved.parts[len(resolved.parts) - 3 :], (
        f"warm runtime state must not land under a `state/` segment: {resolved}"
    )
    assert breadcrumb.breadcrumb_path(tmp_path) == resolved / "warm.json"


def test_runtime_base_honours_the_test_only_override(tmp_path: Path, monkeypatch) -> None:
    """The seam that keeps a suite run out of the operator's real
    `%LOCALAPPDATA%`. Passing `tmp_path` as `engine_root` varies only the
    clone-hash component -- without this override the BASE stays real, so
    every run minted a fresh real directory that nothing removed."""
    override = tmp_path / "runtime-base"
    monkeypatch.setenv(breadcrumb.RUNTIME_BASE_ENV, str(override))
    assert breadcrumb._runtime_base() == override
    assert override in breadcrumb.svc_dir(tmp_path).parents


def test_runtime_base_default_is_unchanged_when_the_override_is_unset(
    tmp_path: Path, monkeypatch
) -> None:
    """The operator's real resolution, untouched: `%LOCALAPPDATA%` first,
    `~/.cache` when it is absent (the non-Windows importability fallback)."""
    monkeypatch.delenv(breadcrumb.RUNTIME_BASE_ENV, raising=False)

    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local-app-data"))
    assert breadcrumb._runtime_base() == tmp_path / "local-app-data"

    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    assert breadcrumb._runtime_base() == Path.home() / ".cache"


def test_an_empty_override_falls_through_rather_than_resolving_to_cwd(
    tmp_path: Path, monkeypatch
) -> None:
    """`Path("")` is the current directory -- an override set to empty or
    whitespace must read as UNSET, not as "write beside wherever the
    engine happens to be running from"."""
    monkeypatch.setenv(breadcrumb.RUNTIME_BASE_ENV, "   ")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local-app-data"))
    assert breadcrumb._runtime_base() == tmp_path / "local-app-data"


def test_a_breadcrumb_write_lands_under_the_quarantined_base(tmp_path: Path) -> None:
    """The suite-wide quarantine (`coordinator_core/conftest.py`'s
    `_quarantine_real_home`) is what makes this hold for every warm test
    rather than only the ones that remember to set the env themselves."""
    import os as _os

    quarantined = _os.environ.get(breadcrumb.RUNTIME_BASE_ENV, "")
    assert quarantined, "the suite-wide warm runtime-base quarantine is not applied"

    breadcrumb.write_breadcrumb(
        pipe="p",
        pid=1,
        stable_pid_start_epoch=1,
        engine_sha=None,
        engine_root=tmp_path,
    )
    written = breadcrumb.breadcrumb_path(tmp_path)
    assert written.exists()
    assert Path(quarantined) in written.parents


def test_svc_dir_is_deterministic_and_per_clone(tmp_path: Path) -> None:
    """Per-clone keying is the property the old in-clone path actually
    bought, and `warm-engine-stop.py` depends on it: it passes its OWN
    containing clone's root and must resolve exactly the directory that
    clone's server writes."""
    a, b = tmp_path / "clone-a", tmp_path / "clone-b"
    a.mkdir()
    b.mkdir()
    assert breadcrumb.svc_dir(a) == breadcrumb.svc_dir(a)
    assert breadcrumb.svc_dir(a) != breadcrumb.svc_dir(b)


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
    monkeypatch.setattr(breadcrumb, "_pipe_is_alive", lambda pipe, timeout_ms=5: True)
    assert breadcrumb.should_spawn(tmp_path, now=now) is False


def test_should_spawn_true_when_pid_alive_but_pipe_is_dead(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """THE FIX this pins: a young, alive-by-PID breadcrumb must NOT
    debounce a respawn when the pipe itself proves dead -- a process can
    be alive and wedged (accepting no new connections) while its
    breadcrumb still reads as young and alive by pid alone. Proving the
    PIPE, not the PROCESS, is what should_spawn's own docstring now
    describes."""
    now = 2_000_000_000.0
    started_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - 0.5))
    _write_started_at(tmp_path, pid=999, stable_epoch=111, started_at=started_at)
    monkeypatch.setattr(breadcrumb, "stable_pid_alive", lambda pid, stored_start_epoch="": True)
    monkeypatch.setattr(breadcrumb, "_pipe_is_alive", lambda pipe, timeout_ms=5: False)
    assert breadcrumb.should_spawn(tmp_path, now=now) is True


def test_should_spawn_false_when_pipe_field_missing_falls_back_to_pid_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No recorded pipe to prove -- the debounce falls back to the
    pid-only answer that predates this check, rather than crashing or
    treating absence as "dead"."""
    path = breadcrumb.breadcrumb_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    now = 2_000_000_000.0
    started_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - 0.5))
    record = {
        "pid": 999,
        "stable_pid_start_epoch": 111,
        "engine_sha": "x",
        "started_at": started_at,
    }
    path.write_text(json.dumps(record), encoding="utf-8")
    monkeypatch.setattr(breadcrumb, "stable_pid_alive", lambda pid, stored_start_epoch="": True)
    assert breadcrumb.should_spawn(tmp_path, now=now) is False


@pytest.mark.skipif(
    __import__("os").name != "nt", reason="WinDLL is only reachable on the Windows arm"
)
def test_pipe_is_alive_fails_open_on_error_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    """On an unanticipated `ctypes` failure, the Windows arm must fail OPEN
    (True) -- a caller that cannot evaluate liveness gets the pid-only
    answer that predates this check, never a wrongly-triggered respawn or a
    propagated exception."""
    import ctypes as _ctypes

    def _raise(*args, **kwargs):
        raise OSError("no such DLL on this platform")

    monkeypatch.setattr(_ctypes, "WinDLL", _raise, raising=False)
    assert breadcrumb._pipe_is_alive(r"\\.\pipe\fake") is True


def test_pipe_is_alive_fails_open_on_error_posix(monkeypatch: pytest.MonkeyPatch) -> None:
    """`_pipe_is_alive` now branches on `os.name != "nt"` straight to
    `_unix_socket_is_alive` -- on THIS platform, a Windows-side `WinDLL`
    patch is dead code and proves nothing about it. The same fail-open
    property must hold on the POSIX arm's own error path: an unanticipated
    error building/using the probe socket (never `ConnectionRefusedError`/
    `FileNotFoundError`/`NotADirectoryError`, which read DEAD by design)
    degrades to True, not a propagated exception."""
    import socket as _socket

    def _raise(*args, **kwargs):
        raise OSError("no sockets for you")

    monkeypatch.setattr(_socket, "socket", _raise)
    assert breadcrumb._pipe_is_alive(r"\\.\pipe\fake") is True


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


# ---------------------------------------------------------------------------
# Ownership-checked unlink
#
# There is ONE breadcrumb per clone and every winning server overwrites it at
# boot, so a departing server is not necessarily the one the current breadcrumb
# describes. `warm.idle`'s superseded-generation arm retires a predecessor
# within one watchdog poll of its successor binding -- i.e. exactly when the
# successor's breadcrumb is freshest -- so this is the ordinary case, not a
# corner one.
# ---------------------------------------------------------------------------


def test_unlink_does_not_delete_a_successors_breadcrumb(tmp_path: Path) -> None:
    """THE DEFECT: generation A exits after generation B has already bound and
    written its own breadcrumb. A must not delete B's entry."""
    breadcrumb.write_breadcrumb(
        pipe="pipe-T1", pid=1111, stable_pid_start_epoch=1, engine_sha="aaa",
        engine_root=tmp_path,
    )
    # Publish rotates the token; B wins the new pipe and overwrites the file.
    breadcrumb.write_breadcrumb(
        pipe="pipe-T2", pid=2222, stable_pid_start_epoch=2, engine_sha="bbb",
        engine_root=tmp_path,
    )

    breadcrumb.unlink_breadcrumb(tmp_path, owner_pid=1111)

    current = breadcrumb.read_breadcrumb(tmp_path)
    assert current is not None, "A deleted the live successor's breadcrumb"
    assert current["pid"] == 2222


def test_successors_debounce_survives_a_predecessor_exit(tmp_path: Path, monkeypatch) -> None:
    """The consequence that makes it more than cosmetic: a deleted breadcrumb
    reads as 'no spawn in flight', so the debounce silently stops debouncing
    while a server is in fact running.

    `stable_pid_alive` is stubbed True because the successor's liveness is a
    SEPARATE precondition of `should_spawn` and not what this test pins -- the
    fixture pids are synthetic, so without the stub the debounce would read
    True for the honest reason "that pid is not running", masking whether the
    breadcrumb survived at all.
    """
    monkeypatch.setattr(breadcrumb, "stable_pid_alive", lambda pid, **kw: True)
    monkeypatch.setattr(breadcrumb, "_pipe_is_alive", lambda pipe, timeout_ms=5: True)

    breadcrumb.write_breadcrumb(
        pipe="pipe-T1", pid=1111, stable_pid_start_epoch=1, engine_sha="aaa",
        engine_root=tmp_path,
    )
    breadcrumb.write_breadcrumb(
        pipe="pipe-T2", pid=2222, stable_pid_start_epoch=2, engine_sha="bbb",
        engine_root=tmp_path,
    )
    assert breadcrumb.should_spawn(tmp_path) is False  # debounce armed by B

    breadcrumb.unlink_breadcrumb(tmp_path, owner_pid=1111)

    assert breadcrumb.should_spawn(tmp_path) is False, (
        "A's exit disarmed the debounce while B is still serving"
    )


def test_unlink_removes_the_file_when_the_owner_matches(tmp_path: Path) -> None:
    """The ownership check must not become a leak: the server that DOES own the
    breadcrumb still clears it on the way out."""
    breadcrumb.write_breadcrumb(
        pipe="p", pid=4242, stable_pid_start_epoch=1, engine_sha=None,
        engine_root=tmp_path,
    )
    breadcrumb.unlink_breadcrumb(tmp_path, owner_pid=4242)
    assert not breadcrumb.breadcrumb_path(tmp_path).exists()


def test_owner_checked_unlink_on_a_corrupt_breadcrumb_is_a_noop(tmp_path: Path) -> None:
    """An unreadable breadcrumb names no owner, so no caller can establish
    ownership of it -- refuse to delete rather than force it. A check that
    falls back to deleting on doubt is not a check."""
    path = breadcrumb.breadcrumb_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("[1, 2, 3]", encoding="utf-8")

    breadcrumb.unlink_breadcrumb(tmp_path, owner_pid=1111)

    assert path.exists()


def test_unlink_without_owner_pid_stays_unconditional(tmp_path: Path) -> None:
    """`warm-engine-stop.py` establishes ownership by stopping the pid this
    same file named, then clears it -- the default must stay unconditional so
    that caller is unchanged."""
    breadcrumb.write_breadcrumb(
        pipe="p", pid=9999, stable_pid_start_epoch=1, engine_sha=None,
        engine_root=tmp_path,
    )
    breadcrumb.unlink_breadcrumb(tmp_path)
    assert not breadcrumb.breadcrumb_path(tmp_path).exists()


def test_ctx_shutdown_passes_its_own_pid_as_the_owner() -> None:
    """Pins the wiring, not just the primitive: `_ctx_shutdown` must supply an
    owner so a superseded generation's exit cannot clear a live successor."""
    import inspect
    import os as _os

    from coordinator_core.warm import server as _server

    source = inspect.getsource(_server._ServerContext._ctx_shutdown)
    assert "owner_pid=os.getpid()" in source
    assert _os.getpid  # the symbol the wiring depends on exists
