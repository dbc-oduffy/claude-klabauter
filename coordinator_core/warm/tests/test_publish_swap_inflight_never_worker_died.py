"""Contract: a publish handover's stop of the superseded warm server never kills an admitted op.

Each case boots a real warm server whose pool workers are real processes (only the op body is a
sleep), puts ops in flight, then runs the production handover stop
`pair_stop.stop_warm_pair(root, keep_pids=..., server_pipe=old_pipe)` exactly as
`round.start_successor_pair_in_hold` calls it: the breadcrumb already names the successor, so the
old server is asked over its own pipe. Every admitted op must end in its result or, when it never
started, the resumable not-started envelope; `diagnosis.case == "worker_died"` is never an answer.

The drain ceiling is narrowed with COORDINATOR_DISPATCH_TIMEOUT_SECS (narrow-only) and every sleep
is derived from the live ceiling and confirm grace, so the cases run in seconds on any machine.
Long ops use `push.outstanding`: an op without an `ipc._OP_TIMEOUT_OVERRIDES` row would have its
own pool deadline narrowed with the dispatch timeout and read as an overdue-worker timeout.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from coordinator_core.warm import breadcrumb, lifecycle, pair_stop, server
from coordinator_core.warm.client import ENGINE_SKEW

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_DISPATCH_TIMEOUT_SECS = "2"
_LONG_OP = "push.outstanding"
_BOOT_DEADLINE_SECS = 90.0
_ENTER_DEADLINE_SECS = 60.0
_REPLY_DEADLINE_SECS = 60.0
_STOP_BOUND_SECS = 90.0
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

_SERVER_CHILD = r'''
import os, sys, time
from pathlib import Path

sys.path.insert(0, os.environ["PSW_REPO"])
root = Path(os.environ["PSW_ROOT"])
stop_token = os.environ["PSW_STOP_TOKEN"]
from coordinator_core.warm import breadcrumb, server


def _body(msg, caller):
    params = msg.get("params") or {}
    (root / ("entered-" + str(params.get("tag", "op")) + ".flag")).write_text(str(os.getpid()))
    time.sleep(float(params.get("sleep", 0)))
    return {"jsonrpc": "2.0", "id": msg["id"], "result": "served"}


# Module level, so a spawned pool worker re-importing this script as __mp_main__ is patched too.
server._pool_dispatch_worker_body = _body
server.DISPATCH_PROCESS_POOL_SIZE = 2


class _Version:
    server_sha = "swap-inflight"
    last_skew_axes = ()

    def is_skewed(self, client_token):
        return client_token == stop_token

    def is_token_skewed(self, client_token):
        return self.is_skewed(client_token)


def main():
    on_windows = sys.platform == "win32"
    token = "swap-inflight-token"
    elected = (
        server._elect_windows_pipe(root, token) if on_windows else server._elect_unix_socket_endpoint(root, token)
    )
    ctx = server._ServerContext(
        name=elected.endpoint,
        sid=elected.identity,
        version_state=_Version(),
        engine_root=root,
        listen_socket=elected.listen_socket,
        endpoint_path=elected.endpoint_path,
    )
    breadcrumb.write_breadcrumb(
        pipe=elected.endpoint,
        pid=os.getpid(),
        stable_pid_start_epoch=server._self_stable_pid_start_epoch() or 0,
        engine_sha=_Version.server_sha,
        engine_root=root,
        transport=breadcrumb.TRANSPORT_PIPE if on_windows else breadcrumb.TRANSPORT_UNIX,
    )
    if elected.first_handle is not None:
        ctx.serve_forever(elected.first_handle)
    else:
        ctx.serve_forever_unix(elected.listen_socket)


if __name__ == "__main__":
    main()
'''


@pytest.fixture()
def engine_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("COORDINATOR_DISPATCH_TIMEOUT_SECS", _DISPATCH_TIMEOUT_SECS)
    root = tmp_path / "eng"
    (root / "coordinator_core" / "warm").mkdir(parents=True)
    (root / ".git").mkdir()
    (root / "coordinator_core" / "_engine_stamp").write_text("sha:swap-inflight\n", encoding="utf-8")
    return root


@pytest.fixture()
def spawned(engine_root: Path):
    """Popen handles to kill at teardown, plus a sweep of anything still matching the root."""
    procs: list[subprocess.Popen] = []
    yield procs
    import psutil

    for p in pair_stop.find_pair_processes(engine_root):
        try:
            psutil.Process(p.pid).kill()
        except psutil.Error:
            pass
    for proc in procs:
        if proc.poll() is None:
            proc.kill()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            pass


def _repo_dir() -> str:
    import coordinator_core

    return str(Path(coordinator_core.__file__).resolve().parents[1])


def _wait_for(predicate, what: str, *, deadline_secs: float, proc: "subprocess.Popen | None" = None):
    deadline = time.monotonic() + deadline_secs
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        if proc is not None and proc.poll() is not None:
            pytest.fail(f"{what}: child exited with {proc.returncode} before it was ready")
        time.sleep(0.05)
    pytest.fail(f"{what}: not ready within {deadline_secs}s")


class _OldServer:
    """The superseded generation: its pid and pipe, with the breadcrumb already naming a successor."""

    def __init__(self, root: Path, pid: int, pipe: str, keep: frozenset) -> None:
        self.root = root
        self.pid = pid
        self.pipe = pipe
        self.keep = keep

    def entered(self, tag: str) -> bool:
        return (self.root / f"entered-{tag}.flag").exists()

    def worker_pid(self, tag: str) -> int:
        return int((self.root / f"entered-{tag}.flag").read_text())


def _boot_old_server_with_successor_crumb(root: Path, spawned: list) -> _OldServer:
    script = root / "coordinator_core" / "warm" / "server.py"
    script.write_text(_SERVER_CHILD, encoding="utf-8")
    env = {
        **os.environ,
        "PSW_REPO": _repo_dir(),
        "PSW_ROOT": str(root),
        "PSW_STOP_TOKEN": pair_stop._STOP_REQUEST_TOKEN,
    }
    proc = subprocess.Popen(
        [sys.executable, str(script)],
        env=env,
        stdout=open(root / "server.log", "wb"),
        stderr=subprocess.STDOUT,
        creationflags=_NO_WINDOW,
    )
    spawned.append(proc)
    # Production pairs are detached and reaped by init; a child of this process stays a zombie after exit.
    threading.Thread(target=proc.wait, daemon=True).start()
    crumb = _wait_for(
        lambda: breadcrumb.read_breadcrumb(root), "server breadcrumb", deadline_secs=_BOOT_DEADLINE_SECS, proc=proc
    )
    successor = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)"], creationflags=_NO_WINDOW)
    spawned.append(successor)
    on_windows = sys.platform == "win32"
    breadcrumb.write_breadcrumb(
        pipe="successor-stub",
        pid=successor.pid,
        stable_pid_start_epoch=0,
        engine_sha="successor",
        engine_root=root,
        transport=breadcrumb.TRANSPORT_PIPE if on_windows else breadcrumb.TRANSPORT_UNIX,
    )
    return _OldServer(root, crumb["pid"], crumb["pipe"], frozenset({successor.pid}))


def _open_client(endpoint: str):
    if sys.platform == "win32":
        return open(endpoint, "r+b", buffering=0)
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(_REPLY_DEADLINE_SECS)
    sock.connect(endpoint)
    return sock.makefile("rwb")


class _Leg:
    """One admitted op on its own thread; `.line` is the raw reply (b"" on EOF), `.error` a transport failure."""

    def __init__(self, old: _OldServer, tag: str, sleep_secs: float) -> None:
        self.tag = tag
        self.line: "bytes | None" = None
        self.error: "BaseException | None" = None
        self._old = old
        self._frame = {
            "jsonrpc": "2.0",
            "id": tag,
            "method": _LONG_OP,
            "params": {"sleep": sleep_secs, "tag": tag},
            "_engine_token": "client-ok",
        }
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self) -> "_Leg":
        self._thread.start()
        return self

    def _run(self) -> None:
        try:
            fh = _open_client(self._old.pipe)
            try:
                fh.write(json.dumps(self._frame).encode("utf-8") + b"\n")
                fh.flush()
                self.line = fh.readline()
            finally:
                fh.close()
        except OSError as exc:
            self.error = exc

    def kind(self) -> str:
        """result | not_started | worker_died | skew_refusal | error | cut (no reply: EOF or transport failure)."""
        self._thread.join(_REPLY_DEADLINE_SECS)
        assert not self._thread.is_alive(), f"leg {self.tag}: no answer and no EOF"
        if self.error is not None or not self.line:
            return "cut"
        reply = json.loads(self.line)
        err = reply.get("error")
        if err is None:
            return "result" if reply.get("result") == "served" else "error"
        diagnosis = (err.get("data") or {}).get("diagnosis") or {}
        if diagnosis.get("case") == "worker_died":
            return "worker_died"
        if err.get("code") == ENGINE_SKEW:
            return "skew_refusal"
        if err.get("message") == server._POOL_NOT_STARTED_MESSAGE and diagnosis.get("phase") == "queued":
            return "not_started"
        return "error"

    def describe(self) -> str:
        return f"leg {self.tag}: error={self.error!r} line={self.line!r}"


class _Stop:
    """The production handover stop, on its own thread."""

    def __init__(self, old: _OldServer) -> None:
        self.result: "pair_stop.PairStopResult | None" = None
        self._old = old
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self) -> "_Stop":
        self._thread.start()
        return self

    def _run(self) -> None:
        self.result = pair_stop.stop_warm_pair(self._old.root, keep_pids=self._old.keep, server_pipe=self._old.pipe)

    def finish(self) -> "pair_stop.PairStopResult":
        self._thread.join(_STOP_BOUND_SECS)
        assert not self._thread.is_alive(), f"stop_warm_pair still running after {_STOP_BOUND_SECS}s"
        assert self.result is not None
        return self.result


def _wait_entered(old: _OldServer, *tags: str) -> None:
    for tag in tags:
        _wait_for(lambda t=tag: old.entered(t), f"op {tag} reaching a pool worker", deadline_secs=_ENTER_DEADLINE_SECS)


def _assert_op_survives_the_stop(old: _OldServer, leg: _Leg, stop: _Stop) -> None:
    result = stop.finish()
    assert leg.kind() == "result", leg.describe()
    assert result.ok(), result.render_survivors()


def test_op_running_past_the_drain_ceiling_replies_from_the_old_server(engine_root, spawned) -> None:
    """Mechanism 1: the op outlives `lifecycle._drain_ceiling_secs()` but not its own executing bound."""
    secs = lifecycle._drain_ceiling_secs() + 2.0
    assert secs < server._executing_bound_secs(_LONG_OP)
    old = _boot_old_server_with_successor_crumb(engine_root, spawned)
    leg = _Leg(old, "long", secs).start()
    _wait_entered(old, "long")
    _assert_op_survives_the_stop(old, leg, _Stop(old).start())


def test_op_admitted_after_the_stop_ask_is_answered_or_never_started(engine_root, spawned) -> None:
    """Mechanism 2: a leg arriving while the old server drains either runs to its result or never starts."""
    old = _boot_old_server_with_successor_crumb(engine_root, spawned)
    running = _Leg(old, "running", 3.0).start()
    _wait_entered(old, "running")
    stop = _Stop(old).start()
    time.sleep(0.5)
    late = _Leg(old, "late", 1.0).start()
    result = stop.finish()
    assert running.kind() == "result", running.describe()
    late_kind = late.kind()
    if old.entered("late"):
        assert late_kind == "result", late.describe()
    else:
        assert late_kind in {"not_started", "skew_refusal", "cut"}, late.describe()
    assert result.ok(), result.render_survivors()


def test_leg_queued_behind_busy_workers_gets_its_result_or_the_not_started_reply(engine_root, spawned) -> None:
    """Mechanism 3: every heavy slot is held past the drain ceiling, so the third leg is still queued when it lapses."""
    secs = lifecycle._drain_ceiling_secs() + 4.0
    assert secs < server._executing_bound_secs(_LONG_OP)
    old = _boot_old_server_with_successor_crumb(engine_root, spawned)
    holders = []
    for n in range(2):
        holders.append(_Leg(old, f"holder{n}", secs).start())
        _wait_entered(old, f"holder{n}")
    queued = _Leg(old, "queued", 1.0).start()
    time.sleep(0.5)
    assert not old.entered("queued"), "the queue was not saturated: the third leg started at once"
    stop = _Stop(old).start()
    result = stop.finish()
    kinds = {leg.tag: leg.kind() for leg in [*holders, queued]}
    assert kinds["holder0"] == "result" and kinds["holder1"] == "result", kinds
    assert kinds["queued"] in {"result", "not_started"}, queued.describe()
    assert result.ok(), result.render_survivors()


def test_pool_child_running_past_the_confirm_grace_is_never_terminated_while_its_server_lives(
    engine_root, spawned
) -> None:
    """Mechanism 4: the op outlives `pair_stop._confirm_grace_secs()`, so the resweep meets a live pool child."""
    secs = pair_stop._confirm_grace_secs() + 2.0
    assert secs < server._executing_bound_secs(_LONG_OP)
    old = _boot_old_server_with_successor_crumb(engine_root, spawned)
    leg = _Leg(old, "grace", secs).start()
    _wait_entered(old, "grace")
    worker = old.worker_pid("grace")
    stop = _Stop(old).start()
    kind = leg.kind()
    result = stop.finish()
    assert kind == "result", f"{leg.describe()}; worker pid {worker}"
    assert result.ok(), result.render_survivors()
