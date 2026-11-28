"""A guard pool held inside import waits still answers hook verdicts and does not restart the engine.

Falsifier for the warm door held-pool plan: every guard-pool worker sits in a blocking import
finder (a stand-in for `importlib._path_stat` under disk contention, per worker-stacks/44136), the
stall dir holds import-wait records, and a `hooks.preuse_bash_dispatch` frame goes through the real
accept-path entry (`_serve_queued` -> `_serve_line` -> `_pool_dispatch`) against a real
`ProcessPoolExecutor` guard pool. Asserts: a verdict inside the door's hook read deadline, and at
most one `action=restart` line in the svc hang-restart log.

The blocking finder is installed in each spawned worker through a `sitecustomize` on `PYTHONPATH`
(a spawned worker inherits nothing of the parent's `sys.meta_path`); every worker records that it
entered the finder before the hook is driven, so a finder that never ran fails loudly instead of
passing vacuously. `hang_restart.exit_server` is a recorder: the test never ends the pytest process.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from pathlib import Path

import pytest

from coordinator_core.warm import hang_restart, server, telemetry
from coordinator_core.warm.tests.door_test_support import DOOR_CORE_H

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process, pytest.mark.designed_red]

_HOOK_OP = "hooks.preuse_bash_dispatch"
_BLOCKED_MODULE = "coordinator_core.bash_guards.dispatch"
_FINDER_CEILING_SECS = 45.0
_SPAWN_WAIT_SECS = 90.0
_PREDECESSOR_PID = 424242

_SITECUSTOMIZE = '''\
import importlib.abc
import os
import sys
import time

_TARGET = os.environ["HELD_POOL_BLOCKED_MODULE"]
_RELEASE = os.environ["HELD_POOL_RELEASE_FILE"]
_ENTERED_DIR = os.environ["HELD_POOL_ENTERED_DIR"]
_CEILING = float(os.environ["HELD_POOL_CEILING_SECS"])


def _path_stat():
    deadline = time.monotonic() + _CEILING
    while not os.path.exists(_RELEASE) and time.monotonic() < deadline:
        time.sleep(0.05)


class _HeldFinder(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == _TARGET:
            with open(os.path.join(_ENTERED_DIR, "entered-%d" % os.getpid()), "w") as fh:
                fh.write(fullname)
            _path_stat()
        return None


sys.meta_path.insert(0, _HeldFinder())
'''


class _VersionState:
    server_sha = "deadbeef"

    def is_skewed(self, client_token: str) -> bool:
        return False

    def is_token_skewed(self, client_token: str) -> bool:
        return False


class _FakeIO:
    def __init__(self, method: str, request_id: int, cwd: str) -> None:
        frame = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method,
            "params": {
                "payload": {
                    "hook_event_name": "PreToolUse",
                    "tool_name": "Bash",
                    "tool_input": {"command": "echo held-pool"},
                    "session_id": "s-held-pool",
                    "cwd": cwd,
                }
            },
            "_engine_token": "client-token",
        }
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


def _door_hook_deadline_secs() -> float:
    """The door's live-engine hook read deadline, read out of `door_core.h` so a literal here cannot drift."""
    source = DOOR_CORE_H.read_text(encoding="utf-8")
    match = re.search(r"^#define\s+DOOR_HOOK_LIVE_READ_MS\s+(\d+)\s*$", source, re.MULTILINE)
    assert match, f"DOOR_HOOK_LIVE_READ_MS not found in {DOOR_CORE_H} -- renamed or removed"
    return int(match.group(1)) / 1000.0


def _seed_import_wait_records(stall_dir: Path, now: float) -> None:
    """`HANG_DUMP_THRESHOLD` stall records whose op thread sits in an importlib `_path_stat` wait, stamped
    after the predecessor's restart line so `maybe_restart` counts them."""
    stall_dir.mkdir(parents=True, exist_ok=True)
    for i in range(hang_restart.HANG_DUMP_THRESHOLD):
        stamp = time.strftime(hang_restart._STAMP_FMT, time.gmtime(now - 12 + i))
        (stall_dir / f"{stamp}-{_PREDECESSOR_PID + 1}-import-held-{i}.txt").write_text(
            "engine-stamp: test\nsource-sha: unknown\nopen-path: none\nsinks: []\nleg: \n"
            "--- threads ---\n"
            "Thread 0x0001 (most recent call first):\n"
            '  File "<frozen importlib._bootstrap_external>", line 1593 in _path_stat\n'
            '  File "<frozen importlib._bootstrap_external>", line 1624 in find_spec\n'
            '  File "<frozen importlib._bootstrap>", line 1247 in _find_spec\n'
            '  File "<frozen importlib._bootstrap>", line 1304 in _find_and_load_unlocked\n'
            '  File "coordinator_core/hooks/preuse_bash_dispatch.py", line 228 in _run_chain\n',
            encoding="utf-8",
            newline="\n",
        )


def _seed_predecessor_restart(svc: Path, now: float) -> None:
    """The engine this one replaced restarted 20s ago, so this one is a successor inside the backoff."""
    hang_restart._append(
        svc / hang_restart.HANG_RESTART_LOG_FILENAME,
        now - 20,
        _PREDECESSOR_PID,
        "restart",
        hang_restart.HANG_DUMP_THRESHOLD,
        hang_restart.HANG_WINDOW_SECS,
        [],
    )


def _restart_lines(svc: Path, since_epoch: float) -> list:
    log = svc / hang_restart.HANG_RESTART_LOG_FILENAME
    if not log.exists():
        return []
    found = []
    for line in log.read_text(encoding="utf-8").splitlines():
        epoch = hang_restart._line_epoch(line)
        if " action=restart " in line + " " and epoch is not None and epoch >= since_epoch:
            found.append(line)
    return found


def _entered_pids(entered_dir: Path) -> set:
    return {p.name for p in entered_dir.glob("entered-*")}


def _wait_until(predicate, secs: float) -> bool:
    deadline = time.monotonic() + secs
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return predicate()


def _teardown(ctx, release_file: Path) -> None:
    """Release every held worker, then end each pool's processes so none outlives the test."""
    release_file.write_text("go", encoding="utf-8")
    ready = ctx.__dict__.get("_guard_standby_ready")
    if ready is not None:
        ready.wait(10.0)
    for name in ("_dispatch_pool", "_guard_pool", "_guard_standby"):
        pool = ctx.__dict__.get(name)
        if pool is None:
            continue
        procs = list((getattr(pool, "_processes", None) or {}).values())
        pool.shutdown(wait=False, cancel_futures=True)
        for proc in procs:
            try:
                proc.terminate()
            except Exception:  # noqa: BLE001 -- best-effort reap
                pass


def test_hook_verdict_within_door_deadline_and_no_restart_storm_while_every_guard_worker_is_held(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    svc = tmp_path / "svc"
    stall_dir = svc / "stall-dumps"
    claim_dir = svc / "claims"
    stderr_dir = svc / "worker-stderr"
    for d in (stall_dir, claim_dir, stderr_dir):
        d.mkdir(parents=True)
    entered_dir = tmp_path / "entered"
    entered_dir.mkdir()
    release_file = tmp_path / "release"
    shim_dir = tmp_path / "shim"
    shim_dir.mkdir()
    (shim_dir / "sitecustomize.py").write_text(_SITECUSTOMIZE, encoding="utf-8")

    monkeypatch.setenv("HELD_POOL_BLOCKED_MODULE", _BLOCKED_MODULE)
    monkeypatch.setenv("HELD_POOL_RELEASE_FILE", str(release_file))
    monkeypatch.setenv("HELD_POOL_ENTERED_DIR", str(entered_dir))
    monkeypatch.setenv("HELD_POOL_CEILING_SECS", str(_FINDER_CEILING_SECS))
    inherited = os.environ.get("PYTHONPATH")
    monkeypatch.setenv("PYTHONPATH", str(shim_dir) + (os.pathsep + inherited if inherited else ""))

    monkeypatch.setattr(server, "DISPATCH_PROCESS_POOL_SIZE", 1)
    monkeypatch.setattr(server, "_start_telemetry_drain", lambda _q: threading.Event())
    monkeypatch.setattr(server, "_journal_landed", lambda *_a, **_k: None)
    monkeypatch.setattr(telemetry, "flush_requests", lambda *_a, **_k: None)
    monkeypatch.setattr(hang_restart, "_stall_marks", {})
    exits: list = []
    monkeypatch.setattr(hang_restart, "exit_server", lambda pid: exits.append(pid))

    now = time.time()
    window_start = now - hang_restart.HANG_WINDOW_SECS
    _seed_predecessor_restart(svc, now)
    _seed_import_wait_records(stall_dir, now)

    ctx = server._ServerContext(
        name="pipe-held-pool", sid="sid-held-pool", version_state=_VersionState(), engine_root=tmp_path
    )
    ctx._worker_stderr_dir = stderr_dir
    ctx._stall_dir = stall_dir
    ctx._claim_dir = claim_dir
    ctx._dispatch_dirs_ready = True

    guard_workers = server._guard_pool_workers()
    holder_ios = [_FakeIO(_HOOK_OP, 100 + i, str(tmp_path)) for i in range(guard_workers)]
    probe_io = _FakeIO(_HOOK_OP, 900, str(tmp_path))
    try:
        holders = [
            threading.Thread(
                target=ctx._serve_queued, args=(io, (time.time(), 0)), kwargs={"lane": "guard"}, daemon=True
            )
            for io in holder_ios
        ]
        for t in holders:
            t.start()

        assert _wait_until(lambda: len(_entered_pids(entered_dir)) >= guard_workers, _SPAWN_WAIT_SECS), (
            f"only {len(_entered_pids(entered_dir))} of {guard_workers} guard workers entered the blocking "
            f"finder: the fixture did not hold the pool (a green here would be vacuous)"
        )
        for t in holders:
            t.join(30.0)

        started = time.monotonic()
        ctx._serve_queued(probe_io, (time.time(), 0), lane="guard")
        elapsed = time.monotonic() - started
        restarts = _restart_lines(svc, window_start)
    finally:
        _teardown(ctx, release_file)

    deadline = _door_hook_deadline_secs()
    failures = []
    if not probe_io.written:
        failures.append(f"no hook verdict written within {elapsed:.1f}s")
    else:
        reply = json.loads(probe_io.written[0])
        if "result" not in reply or "error" in reply:
            failures.append(f"hook answered a refusal, not a verdict: {json.dumps(reply.get('error'))[:240]}")
    if elapsed > deadline:
        failures.append(f"hook verdict took {elapsed:.1f}s, past the door hook deadline {deadline:g}s")
    if len(restarts) > 1:
        failures.append(
            f"{len(restarts)} action=restart lines in the {hang_restart.HANG_WINDOW_SECS:g}s window (max 1): "
            + " | ".join(restarts)
        )
    assert not failures, "; ".join(failures)
