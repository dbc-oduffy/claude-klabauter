"""A dispatch-gate thread does no blocking filesystem work between `_pool_dispatch` entry and its return.

Every filesystem entry point is recorded by calling thread; the dispatching thread must appear in
none of them. Drive probes, recycle lines and claim release run on helper threads.
"""

from __future__ import annotations

import builtins
import concurrent.futures
import io
import os
import threading
import time
from pathlib import Path
from typing import Any, Callable

import pytest

from coordinator_core.warm import dispatch_ack, server, stall_writer

_OS_FUNCS = (
    "stat", "lstat", "open", "mkdir", "makedirs", "scandir", "listdir", "unlink", "remove",
    "replace", "rename", "rmdir",
)
_OSPATH_FUNCS = ("realpath", "exists", "isdir", "isfile", "getmtime")
_PATH_METHODS = ("exists", "is_dir", "is_file", "read_text", "write_text", "unlink", "mkdir", "resolve", "stat", "open")


class _Recorder:
    """Patches every filesystem entry point to log the calling thread's ident and the call name."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.calls: list[tuple[int, str]] = []
        self._mp = monkeypatch
        for name in _OS_FUNCS:
            self._wrap(os, name, f"os.{name}")
        for name in _OSPATH_FUNCS:
            self._wrap(os.path, name, f"os.path.{name}")
        for name in _PATH_METHODS:
            self._wrap(Path, name, f"Path.{name}")
        self._wrap(builtins, "open", "open")
        self._wrap(io, "open", "io.open")
        try:
            import msvcrt

            self._wrap(msvcrt, "locking", "msvcrt.locking")
        except ImportError:
            pass
        try:
            import fcntl

            self._wrap(fcntl, "flock", "fcntl.flock")
        except ImportError:
            pass

    def _wrap(self, owner: Any, attr: str, label: str) -> None:
        original = getattr(owner, attr, None)
        if original is None:
            return
        calls = self.calls

        def recording(*args: Any, **kwargs: Any) -> Any:
            calls.append((threading.get_ident(), label))
            return original(*args, **kwargs)

        self._mp.setattr(owner, attr, recording)

    def by(self, ident: int) -> list[str]:
        return [label for who, label in self.calls if who == ident]


def _done_future(result: dict) -> "concurrent.futures.Future":
    fut: concurrent.futures.Future = concurrent.futures.Future()
    fut.set_result(result)
    return fut


class _InstantPool:
    """Resolves every submit at once with an ok envelope; touches no filesystem."""

    def submit(self, fn: Callable, msg: dict, *rest: Any) -> "concurrent.futures.Future":
        return _done_future({"jsonrpc": "2.0", "id": msg.get("id"), "result": {"ok": True}})


def _ctx(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> server._ServerContext:
    monkeypatch.setattr(server, "_ack_store", dispatch_ack.AckStore(boot_ns=0))
    claim_dir = tmp_path / "claims"
    claim_dir.mkdir()
    (tmp_path / "stall").mkdir()
    ctx = server._ServerContext.__new__(server._ServerContext)
    ctx._pool_outstanding = server.InFlightCounter()
    ctx._dispatch_pool = _InstantPool()
    ctx._dispatch_pool_lock = threading.Lock()
    ctx._claim_dir = claim_dir
    ctx._stall_dir = tmp_path / "stall"
    ctx._worker_stderr_dir = tmp_path / "stderr"
    ctx.engine_root = tmp_path
    ctx._ensure_dispatch_pool = lambda: ctx._dispatch_pool
    return ctx


def _on_thread(fn: Callable[[], Any]) -> tuple[int, Any]:
    """Run `fn` on its own thread; returns (that thread's ident, fn's value)."""
    box: dict = {}

    def run() -> None:
        box["ident"] = threading.get_ident()
        box["value"] = fn()

    t = threading.Thread(target=run)
    t.start()
    t.join(10)
    assert not t.is_alive()
    return box["ident"], box["value"]


def _msg(worktree: Path | None = None) -> dict:
    msg: dict = {"jsonrpc": "2.0", "id": 1, "method": "invoke.from_argv", "params": {"argv": ["x"]}}
    if worktree is not None:
        msg["_origin_worktree"] = str(worktree)
    return msg


def test_default_pool_dispatch_does_no_filesystem_work_on_the_dispatching_thread(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx = _ctx(tmp_path, monkeypatch)
    rec = _Recorder(monkeypatch)
    ident, out = _on_thread(lambda: ctx._pool_dispatch(_msg(tmp_path), dispatch_key="key-1"))
    assert out["result"] == {"ok": True}
    assert rec.by(ident) == []


def test_claim_release_still_happens_off_thread(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ctx = _ctx(tmp_path, monkeypatch)
    claim = tmp_path / "claims" / "key-2"
    claim.write_text("1", encoding="ascii")
    _on_thread(lambda: ctx._pool_dispatch(_msg(), dispatch_key="key-2"))
    deadline = time.monotonic() + 5
    while claim.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert not claim.exists()


def test_recycle_line_goes_through_the_stall_writer_not_the_calling_thread(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx = _ctx(tmp_path, monkeypatch)
    assert stall_writer.flush(5)
    rec = _Recorder(monkeypatch)
    ident, _ = _on_thread(lambda: ctx._write_recycle_line(["op-a"], 3))
    assert rec.by(ident) == []
    assert stall_writer.flush(5)
    (line,) = list((tmp_path / "stall").glob("*-recycle.txt"))
    assert "holders=['op-a'] reconciled_keys=3" in line.read_text(encoding="utf-8")


def test_recycle_line_survives_a_missing_stall_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ctx = _ctx(tmp_path, monkeypatch)
    ctx._stall_dir = tmp_path / "gone" / "stall"
    ctx._write_recycle_line(["op-b"], 0)
    assert stall_writer.flush(5)
    assert len(list((tmp_path / "gone" / "stall").glob("*-recycle.txt"))) == 1


def test_a_stuck_stall_writer_does_not_slow_the_recycle_line(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ctx = _ctx(tmp_path, monkeypatch)
    release = threading.Event()
    monkeypatch.setattr(stall_writer, "_write", lambda path, text: release.wait(10))
    ctx._write_recycle_line(["wedge"], 0)
    t0 = time.perf_counter()
    ctx._write_recycle_line(["op-c"], 1)
    assert time.perf_counter() - t0 < 0.05
    release.set()
    assert stall_writer.flush(5)


def _stalled(ctx: server._ServerContext, root: str, op: str = "a") -> None:
    fut: concurrent.futures.Future = concurrent.futures.Future()
    ctx._track_pool_task(fut, op, f"key-{op}", repo_root=root)
    row = ctx._pool_tasks[fut]
    ctx._pool_tasks[fut] = (row[0], time.monotonic() - (server.STALL_DUMP_SECS + 5), row[2], row[3])


def test_classify_reads_a_cache_a_background_thread_refreshes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ctx = _ctx(tmp_path, monkeypatch)
    _stalled(ctx, "root-c")
    probers: list[int] = []

    def fake(root: Any) -> float:
        probers.append(threading.get_ident())
        return server._SLOW_DRIVE_PROBE_MS + 1

    monkeypatch.setattr(server, "_holder_drive_probe_ms", fake)
    ident, verdict = _on_thread(ctx._classify_stalled_holders)
    assert verdict == ([], ["a"])
    assert probers and ident not in probers


def test_classify_reuses_a_fresh_entry_and_serves_a_stale_one_without_waiting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx = _ctx(tmp_path, monkeypatch)
    _stalled(ctx, "root-c")
    release = threading.Event()
    calls: list[int] = []

    def fake(root: Any) -> float:
        calls.append(1)
        if len(calls) > 1:
            release.wait(10)
        return server._SLOW_DRIVE_PROBE_MS + 1

    monkeypatch.setattr(server, "_holder_drive_probe_ms", fake)
    assert ctx._classify_stalled_holders() == ([], ["a"])
    assert ctx._classify_stalled_holders() == ([], ["a"])
    assert len(calls) == 1
    cache = ctx._holder_probe_cache
    for key, (ms, _stamp) in list(cache._entries.items()):
        cache._entries[key] = (ms, -1e9)
    t0 = time.perf_counter()
    assert ctx._classify_stalled_holders() == ([], ["a"])
    assert time.perf_counter() - t0 < 0.05
    release.set()


def test_a_wedged_probe_classifies_the_holder_hung_inside_the_cold_wait(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx = _ctx(tmp_path, monkeypatch)
    _stalled(ctx, "root-c")
    release = threading.Event()
    monkeypatch.setattr(server, "_holder_drive_probe_ms", lambda root: release.wait(10))
    t0 = time.perf_counter()
    assert ctx._classify_stalled_holders() == (["a"], [])
    assert time.perf_counter() - t0 < 0.5
    release.set()


def test_pool_rebuild_prunes_run_off_thread_not_inline(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ctx = _ctx(tmp_path, monkeypatch)
    ctx._dispatch_pool = None
    ctx._dispatch_dirs_ready = True
    inline: list[str] = []
    offloaded: list[Any] = []
    monkeypatch.setattr(server, "_prune_worker_stderr_logs", lambda d: inline.append("stderr"))
    monkeypatch.setattr(server, "_prune_dispatch_claims", lambda d: inline.append("claims"))
    monkeypatch.setattr(server, "_offload_fs", lambda fn, *a: offloaded.append((fn, a)))
    monkeypatch.setattr(server, "_start_telemetry_drain", lambda q: threading.Event())
    monkeypatch.setattr(server.concurrent.futures, "ProcessPoolExecutor", lambda **kw: _InstantPool())
    server._ServerContext._ensure_dispatch_pool(ctx)
    assert inline == []
    assert [(fn.__name__ if hasattr(fn, "__name__") else fn) for fn, _ in offloaded] == ["<lambda>", "<lambda>"]
    assert [a for _, a in offloaded] == [(ctx._worker_stderr_dir,), (ctx._claim_dir,)]
