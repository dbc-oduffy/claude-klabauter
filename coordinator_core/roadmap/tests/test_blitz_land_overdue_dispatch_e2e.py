"""
coordinator_core.roadmap.tests.test_blitz_land_overdue_dispatch_e2e

Purpose: a `roadmap.blitz_land` dispatch that outruns its pool deadline through the real
`_pool_dispatch` (spawned worker process, claim, kill fence, AckStore). A surviving worker reads
executing/overdue and its commit is refused; a dead worker reads killed; an unfenced landing
commits only the wave's own slot and `wave-<N>.*` files.
"""

from __future__ import annotations

import concurrent.futures
import json
import multiprocessing
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import psutil
import pytest

from coordinator_core.warm import dispatch_ack, server
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

_DEADLINE_SECS = 5.0
_RUN = "state/plan-blitz/20261015T000000Z"
_ENV_GATE = "BLITZ_E2E_RELEASE_FILE"
_ENV_SURVIVE = "BLITZ_E2E_SURVIVE_TERMINATE"
_ENV_OUT = "BLITZ_E2E_OUT_FILE"
_survivor_only = pytest.mark.skipif(
    sys.platform == "win32", reason="a worker can only ignore terminate() where it is a catchable signal"
)


class _FakeIO:
    def __init__(self, lines):
        self._lines = list(lines)
        self.written: list[bytes] = []

    def readline(self) -> bytes:
        return self._lines.pop(0) if self._lines else b""

    def write(self, data: bytes) -> None:
        self.written.append(data)

    def flush(self) -> None:
        pass

    def close(self) -> None:
        pass


class _FakeVersionState:
    def is_skewed(self, client_token: str) -> bool:
        return False


def _serve(frame, dispatch):
    io_obj = _FakeIO([frame])
    server._handle_connection(
        io_obj,
        version_state=_FakeVersionState(),
        server_sha="x",
        close_listener=lambda: None,
        drain=lambda: None,
        in_flight=server.InFlightCounter(),
        dispatch=dispatch,
    )
    return [json.loads(line) for line in io_obj.written]


def _held_blitz_worker(msg, caller, dispatch_key=None, claim_dir=None, stall_secs=10.0):
    """Module-level so the spawned worker unpickles it: the real worker's claim and fence hold,
    then the real `roadmap.blitz_land` body, held open until the release file appears."""
    from coordinator_core.ipc import allow_unstamped_dispatch
    from coordinator_core.warm import dispatch_fence

    allow_unstamped_dispatch()
    claim = server._claim_path(claim_dir, dispatch_key)
    if claim is not None and not server._take_claim(claim, os.getpid()):
        return {server._NOT_DISPATCHED_MARKER: True}
    dispatch_fence.hold(claim_dir, dispatch_key if claim is not None else None)
    try:
        if os.environ.get(_ENV_SURVIVE):
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
        gate = os.environ.get(_ENV_GATE)
        if gate:
            deadline = time.monotonic() + 60
            while not os.path.exists(gate) and time.monotonic() < deadline:
                time.sleep(0.05)
        response = server._pool_dispatch_worker_body(msg, caller)
        out = os.environ.get(_ENV_OUT)
        if out:
            Path(out).write_text(json.dumps(response), encoding="utf-8")
        return response
    finally:
        dispatch_fence.release()


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), *args], capture_output=True, text=True, check=True,
        **no_console_creationflags(),
    ).stdout


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "t@example.invalid")
    _git(root, "config", "user.name", "t")
    _git(root, "config", "core.autocrlf", "false")
    (root / "README.md").write_text("seed\n", encoding="utf-8")
    (root / "state" / "handoffs").mkdir(parents=True)
    (root / "state" / "handoffs" / ".gitkeep").write_text("", encoding="utf-8")
    _git(root, "add", "README.md", "state/handoffs/.gitkeep")
    _git(root, "commit", "-q", "-m", "seed")
    return root


def _wave_result(root: Path) -> dict:
    run = root / _RUN
    slot = run / "wave-0-abc"
    slot.mkdir(parents=True)
    (slot / "b-1.sizing.yaml").write_text("a: 1\n", encoding="utf-8")
    (run / "wave-0.gate-report.json").write_text("{}\n", encoding="utf-8")
    (run / "RUN-NOTES.md").write_text("notes\n", encoding="utf-8")
    other = run / "wave-1-x"
    other.mkdir()
    (other / "b-2.sizing.yaml").write_text("a: 2\n", encoding="utf-8")
    (run / "wave-1.gate-report.json").write_text("{}\n", encoding="utf-8")
    return {
        "waveIndex": 0, "ready": [], "pulled": [], "replan": [], "surfacedToPm": [],
        "trailSlotDir": str(slot),
    }


class _Rig:
    def __init__(self, tmp_path: Path, repo: Path, monkeypatch):
        self.store = dispatch_ack.AckStore(boot_ns=0)
        monkeypatch.setattr(server, "_ack_store", self.store)
        monkeypatch.setattr(server, "_pool_dispatch_worker", _held_blitz_worker)
        monkeypatch.setattr(server, "_POOL_RESULT_DEADLINE_SECS", _DEADLINE_SECS)
        self.repo = repo
        self.claim_dir = tmp_path / "dispatch-claims"
        self.claim_dir.mkdir()
        stderr_dir = tmp_path / "pool-worker-stderr"
        self.pools: list = []

        def _build():
            pool = concurrent.futures.ProcessPoolExecutor(
                max_workers=2,
                mp_context=multiprocessing.get_context("spawn"),
                initializer=server._worker_process_init,
                initargs=(str(stderr_dir),),
            )
            self.pools.append(pool)
            return pool

        ctx = server._ServerContext.__new__(server._ServerContext)
        ctx._pool_outstanding = server.InFlightCounter()
        ctx._dispatch_pool = _build()
        ctx._dispatch_pool_lock = threading.Lock()
        ctx._claim_dir = self.claim_dir
        ctx._worker_stderr_dir = stderr_dir
        ctx._stall_dir = tmp_path / "stall"

        def _ensure():
            with ctx._dispatch_pool_lock:
                if ctx._dispatch_pool is None:
                    ctx._dispatch_pool = _build()
                return ctx._dispatch_pool

        ctx._ensure_dispatch_pool = _ensure
        self.ctx = ctx

    def call(self, wave_result: dict) -> dict:
        msg = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "roadmap.blitz_land",
            "params": {"wave_result": wave_result},
            "_origin_worktree": str(self.repo),
            "_engine_token": "t",
        }
        (reply,) = _serve((json.dumps(msg) + "\n").encode("utf-8"), self.ctx._pool_dispatch)
        return reply

    def close(self) -> None:
        for pool in self.pools:
            pool.shutdown(wait=False, cancel_futures=True)


@pytest.fixture
def rig(tmp_path, repo, monkeypatch):
    built = _Rig(tmp_path, repo, monkeypatch)
    yield built
    built.close()


def _wait_for(predicate, secs: float = 60.0) -> bool:
    deadline = time.monotonic() + secs
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.1)
    return False


def _pid_gone(pid: int) -> bool:
    return _wait_for(lambda: not psutil.pid_exists(pid), 10.0)


def test_an_unfenced_landing_commits_only_the_waves_own_files(rig, repo):
    wave = _wave_result(repo)

    reply = rig.call(wave)

    assert "result" in reply, reply
    trail = reply["result"]["trail_commit"]
    assert trail["error"] is None and trail["committed"], reply
    names = _git(repo, "show", "--name-only", "--format=", trail["sha"]).split()
    assert sorted(names) == [
        f"{_RUN}/wave-0-abc/b-1.sizing.yaml",
        f"{_RUN}/wave-0.gate-report.json",
    ]
    assert _git(repo, "rev-parse", "HEAD").strip() == trail["sha"]


def test_a_dead_worker_reads_killed_and_nothing_commits(rig, repo, tmp_path, monkeypatch):
    before = _git(repo, "rev-parse", "HEAD")
    monkeypatch.setenv(_ENV_GATE, str(tmp_path / "never-released"))
    wave = _wave_result(repo)

    reply = rig.call(wave)

    error = reply["error"]
    assert error["code"] == server.WARM_DISPATCH_INDETERMINATE
    key = error["data"]["dispatch_key"]
    assert _wait_for(lambda: rig.store.status(key).get("reason") == dispatch_ack.OUTCOME_KILLED, 15.0)
    status = rig.store.status(key)
    assert status["state"] == dispatch_ack.STATE_UNKNOWABLE
    assert status["worker_alive"] is False
    assert _pid_gone(status["worker_pid"])
    assert (rig.claim_dir / f"{key}.killed").exists()
    assert _git(repo, "rev-parse", "HEAD") == before


@_survivor_only
def test_a_surviving_worker_reads_executing_and_its_commit_is_refused(rig, repo, tmp_path, monkeypatch):
    before = _git(repo, "rev-parse", "HEAD")
    release = tmp_path / "release"
    out = tmp_path / "worker-out.json"
    monkeypatch.setenv(_ENV_GATE, str(release))
    monkeypatch.setenv(_ENV_OUT, str(out))
    monkeypatch.setenv(_ENV_SURVIVE, "1")
    wave = _wave_result(repo)

    reply = rig.call(wave)

    key = reply["error"]["data"]["dispatch_key"]
    status = rig.store.status(key)
    assert status["state"] == dispatch_ack.STATE_EXECUTING
    assert status["overdue"] is True
    assert status["worker_alive"] is True
    assert (rig.claim_dir / f"{key}.killed").exists()
    pid = status["worker_pid"]
    try:
        release.write_text("go", encoding="utf-8")
        assert _wait_for(out.exists)
        landed = json.loads(out.read_text(encoding="utf-8"))
        trail = landed["result"]["trail_commit"]
        assert trail["committed"] is False
        assert "reported killed" in trail["error"]
        assert _git(repo, "rev-parse", "HEAD") == before
        assert "wave-0" not in _git(repo, "ls-files", _RUN)
    finally:
        try:
            psutil.Process(pid).kill()
        except psutil.Error:
            pass
