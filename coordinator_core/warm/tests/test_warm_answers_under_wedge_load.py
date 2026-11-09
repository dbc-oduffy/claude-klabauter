"""The warm server keeps answering ping and a door guard, and decides to restart itself, while the svc-dir
filesystem stalls and 8 peer clients hold heavy ops (the 2026-11-09 11:45Z wedge shape).

Drives the real accept path (`_enqueue_connection` -> `_worker_loop` / `_guard_lane_loop` -> `_serve_line` ->
`_pool_dispatch`) with the stall watchers armed. Every call that touches the svc dir (stat, open, create,
replace, glob) or `tempfile.TemporaryFile` blocks until released. Budgets are the server's own handling time
read from its request-telemetry row (`end_ts - start_ts`), never the client wall clock.

Trap: `_pool_dispatch_worker_body` is stubbed, so no real pool worker process is exercised.
"""

from __future__ import annotations

import builtins
import concurrent.futures
import io
import json
import os
import pathlib
import tempfile
import threading
import time

from coordinator_core.warm import hang_restart, server, telemetry

_GUARD_OP = "hooks.preuse_write_dispatch"
_HEAVY_OP = "dispatch.ask_stage"
_PING_BUDGET_SECS = 0.3
_GUARD_BUDGET_SECS = 0.5
_CLIENTS = 8
_HEAVY_PER_CLIENT = 2
_ARM_SECS = 0.3
_WINDOW_SECS = 4.0
_SAMPLE_PAUSE_SECS = 0.1
_ANSWER_WAIT_SECS = 1.5
_RESTART_WITHIN_SECS = 4.0


class _VersionState:
    server_sha = "deadbeef"

    def is_skewed(self, client_token: str) -> bool:
        return False


class _FakeIO:
    def __init__(self, method: str, request_id: int):
        frame = {"jsonrpc": "2.0", "id": request_id, "method": method, "params": {}, "_engine_token": "client-token"}
        self._lines = [(json.dumps(frame) + "\n").encode("utf-8")]
        self.written: list = []

    def readline(self) -> bytes:
        return self._lines.pop(0) if self._lines else b""

    def write(self, data: bytes) -> None:
        self.written.append(data)

    def flush(self) -> None:
        pass

    def close(self) -> None:
        pass


def _wait(predicate, secs: float) -> bool:
    deadline = time.monotonic() + secs
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


class _DiskStall:
    """Blocks filesystem calls on paths under `root` (and every `tempfile.TemporaryFile`) until `release`."""

    _PATH_METHODS = ("exists", "stat", "mkdir", "glob", "iterdir", "write_text", "read_text", "unlink")
    _FUNCS = ((os, "open"), (os, "replace"), (os, "unlink"), (os, "stat"), (os, "scandir"), (os, "mkdir"))

    def __init__(self, root: pathlib.Path):
        self._root = str(root)
        self.release = threading.Event()
        self.engaged = threading.Event()
        self.blocked = 0
        self._undo: list = []

    def _held(self, path) -> bool:
        try:
            return os.fspath(path).startswith(self._root)
        except TypeError:
            return False

    def _gate(self, path) -> None:
        if self._held(path) and self.engaged.is_set():
            self.blocked += 1
            self.release.wait()

    def _patch(self, owner, name, make) -> None:
        real = getattr(owner, name)
        self._undo.append((owner, name, real))
        setattr(owner, name, make(real))

    def install(self) -> None:
        for name in self._PATH_METHODS:
            self._patch(pathlib.Path, name, lambda real: self._wrap_self(real))
        for owner, name in self._FUNCS:
            self._patch(owner, name, lambda real: self._wrap_arg(real))
        for owner in (builtins, io):
            self._patch(owner, "open", lambda real: self._wrap_arg(real))
        self._patch(tempfile, "TemporaryFile", lambda real: self._wrap_temp(real))

    def uninstall(self) -> None:
        self.release.set()
        for owner, name, real in reversed(self._undo):
            setattr(owner, name, real)
        self._undo.clear()

    def _wrap_self(self, real):
        def call(path, *a, **k):
            self._gate(path)
            return real(path, *a, **k)

        return call

    def _wrap_arg(self, real):
        def call(path, *a, **k):
            self._gate(path)
            return real(path, *a, **k)

        return call

    def _wrap_temp(self, real):
        def call(*a, **k):
            if self.engaged.is_set():
                self.blocked += 1
                self.release.wait()
            return real(*a, **k)

        return call


def test_ping_and_guard_answer_and_restart_decides_while_the_svc_dir_stalls_under_peer_load(tmp_path, monkeypatch):
    svc = tmp_path / "svc"
    stall_dir = svc / "stall-dumps"
    stall_dir.mkdir(parents=True)
    claim_dir = svc / "claims"
    claim_dir.mkdir()

    release_ops = threading.Event()

    def _body(msg, caller):
        if msg["method"] == _HEAVY_OP:
            release_ops.wait(60)
        return {"jsonrpc": "2.0", "id": msg.get("id"), "result": {"ok": msg["method"]}}

    exits: list = []
    monkeypatch.setattr(hang_restart, "exit_server", exits.append)
    monkeypatch.setattr(server, "_pool_dispatch_worker_body", _body)
    monkeypatch.setattr(server, "_stall_arm_secs", lambda _deadline: _ARM_SECS)
    monkeypatch.setattr(server, "DISPATCH_PROCESS_POOL_SIZE", 4)
    monkeypatch.setattr(telemetry, "flush_requests", lambda *_a, **_k: None)
    telemetry._REQUESTS.clear()

    ctx = server._ServerContext(
        name="pipe-wedge-load", sid="sid-wedge-load", version_state=_VersionState(), engine_root=tmp_path
    )
    ctx._claim_dir = claim_dir
    ctx._stall_dir = stall_dir
    ctx._dispatch_pool = concurrent.futures.ThreadPoolExecutor(
        max_workers=server.WORKER_POOL_SIZE + server._LIGHT_RESERVE_WORKERS
    )
    ctx._start_worker_pool(pool_size=server.WORKER_POOL_SIZE)

    stall = _DiskStall(svc)
    stall.install()
    heavy: list = []
    clients: list = []
    samples: list = []
    sampling_done = threading.Event()

    def _client(first_id: int) -> None:
        mine = [_FakeIO(_HEAVY_OP, first_id + n) for n in range(_HEAVY_PER_CLIENT)]
        heavy.extend(mine)
        for io_ in mine:
            ctx._enqueue_connection(io_, admitted=False)
        _wait(lambda: all(i.written for i in mine), _WINDOW_SECS + 30.0)

    def _sampler() -> None:
        n = 0
        end = time.monotonic() + _WINDOW_SECS
        while time.monotonic() < end:
            for op in ("ping", _GUARD_OP):
                n += 1
                probe = _FakeIO(op, 100000 + n)
                ctx._enqueue_connection(probe, admitted=False)
                answered = _wait(lambda: bool(probe.written), _ANSWER_WAIT_SECS)
                samples.append((op, answered))
            time.sleep(_SAMPLE_PAUSE_SECS)
        sampling_done.set()

    try:
        stall.engaged.set()
        for c in range(_CLIENTS):
            t = threading.Thread(target=_client, args=(1000 * (c + 1),), daemon=True)
            clients.append(t)
            t.start()
        assert _wait(lambda: ctx._busy_workers() >= _CLIENTS * _HEAVY_PER_CLIENT, 5.0), ctx._busy_workers()
        sampler = threading.Thread(target=_sampler, daemon=True)
        sampler.start()
        restarted = _wait(lambda: bool(exits), _RESTART_WITHIN_SECS)
        assert sampling_done.wait(_WINDOW_SECS + 30.0)
        rows = telemetry.request_samples()
        report = {
            "restart_decided": restarted,
            "blocked_fs_calls": stall.blocked,
            "unanswered": [op for op, ok in samples if not ok],
            "samples": len(samples),
        }

        failures = []
        for op, budget in (("ping", _PING_BUDGET_SECS), (_GUARD_OP, _GUARD_BUDGET_SECS)):
            mine = [r for r in rows if r.get("op") == op]
            slow = [round(r["end_ts"] - r["start_ts"], 3) for r in mine if r["end_ts"] - r["start_ts"] > budget]
            report[op] = {"rows": len(mine), "over_budget_s": slow, "budget_s": budget}
            if slow:
                failures.append(f"{op} handling over {budget}s: {slow}")
        if report["unanswered"]:
            failures.append(f"unanswered samples: {report['unanswered']}")
        if not restarted:
            failures.append(f"no restart decision within {_RESTART_WITHIN_SECS}s with the svc dir stalled")
        assert not failures, f"{failures}; {report}"
    finally:
        stall.release.set()
        release_ops.set()
        for t in clients:
            t.join(10)
        for t in threading.enumerate():
            if t.name in ("accept-stall", "stall-dump"):
                t.join(10)
        stall.uninstall()
        ctx._dispatch_pool.shutdown(wait=False, cancel_futures=True)
