"""A pool worker blocked forever cannot strand an `invoke.from_argv` handler: the caller gets a typed
error envelope inside the hook budget and the next op is answered by a different worker process.

Drives the real `_ServerContext._pool_dispatch` over the real default `ProcessPoolExecutor` builds
(`_ensure_dispatch_pool`); only the worker target is swapped for a module-level stand-in the spawned
worker can import.
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

import psutil
import pytest

from coordinator_core.warm import dispatch_ack, server

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

_SLACK_SECS = 1.0
_NEXT_OP_CEILING_SECS = 30.0


class _FakeIO:
    def __init__(self, lines) -> None:
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


class _VersionState:
    def is_skewed(self, client_token: str) -> bool:
        return False

    def is_token_skewed(self, client_token: str) -> bool:
        return False


def _frame(params: dict, id_: int) -> bytes:
    params = {"entrypoint": "hook-run", **params}
    msg = {"jsonrpc": "2.0", "id": id_, "method": "invoke.from_argv", "params": params, "_engine_token": "t"}
    return (json.dumps(msg) + "\n").encode("utf-8")


def _hang_or_answer(msg, caller, dispatch_key=None, claim_dir=None, stall_secs: float = 10.0):
    """Module-level so the spawned worker can unpickle it. Takes the claim like the real worker."""
    claim = server._claim_path(claim_dir, dispatch_key)
    if claim is not None:
        server._take_claim(claim, os.getpid())
    params = msg["params"]
    Path(params["pid_file"]).write_text(str(os.getpid()))
    if params.get("hang"):
        threading.Event().wait()
    return {"jsonrpc": "2.0", "id": msg.get("id"), "result": {"stdout": "ok\n", "stderr": "", "exit_code": 0}}


def _serve(frame: bytes, dispatch) -> list[dict]:
    io_obj = _FakeIO([frame])
    server._handle_connection(
        io_obj,
        version_state=_VersionState(),
        server_sha="x",
        close_listener=lambda: None,
        drain=lambda: None,
        in_flight=server.InFlightCounter(),
        dispatch=dispatch,
    )
    return [json.loads(line) for line in io_obj.written]


def _serve_in_thread(frame: bytes, dispatch) -> tuple[threading.Thread, list]:
    out: list = []
    thread = threading.Thread(target=lambda: out.extend(_serve(frame, dispatch)), daemon=True)
    thread.start()
    return thread, out


def _pid(path: Path) -> int:
    return int(path.read_text())


@pytest.mark.deliberate_wall_clock(reason="the caller's wait on a wedged pool worker is the behaviour under test")
@pytest.mark.parametrize("read_only", [False, True], ids=["mutating-class", "read-only"])
def test_a_wedged_pool_worker_answers_the_caller_inside_the_hook_budget_and_the_next_op_uses_another_worker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, read_only: bool
) -> None:
    monkeypatch.setattr(server, "_ack_store", dispatch_ack.AckStore(boot_ns=0))
    if read_only:
        monkeypatch.setattr(server, "_op_may_mutate", lambda _m: False)
    monkeypatch.setattr(server.breadcrumb, "svc_dir", lambda _root: tmp_path / "svc")
    monkeypatch.setattr(server, "_pool_dispatch_worker", _hang_or_answer)
    ctx = server._ServerContext(name="pipe-wedge", sid="sid-wedge", version_state=_VersionState(), engine_root=tmp_path)
    warm_pid, hung_pid_file, next_pid_file = (tmp_path / n for n in ("warm.pid", "hung.pid", "next.pid"))
    pids: list[int] = []
    try:
        (warm,) = _serve(_frame({"argv": ["x"], "pid_file": str(warm_pid)}, 1), ctx._pool_dispatch)
        assert warm["result"]["stdout"] == "ok\n"
        pids.append(_pid(warm_pid))

        budget = server._HOOK_LANE_BUDGET_SECS
        started = time.monotonic()
        thread, out = _serve_in_thread(
            _frame({"argv": ["x"], "hang": True, "pid_file": str(hung_pid_file)}, 2), ctx._pool_dispatch
        )
        thread.join(budget + _SLACK_SECS)
        elapsed = time.monotonic() - started
        if hung_pid_file.exists():
            pids.append(_pid(hung_pid_file))
        assert not thread.is_alive() and elapsed < budget + _SLACK_SECS, (
            f"the handler was still waiting on the wedged worker after {elapsed:.1f}s (hook budget {budget}s)"
        )
        (reply,) = out
        if read_only:
            assert "error" not in reply and reply["result"]["exit_code"] == 0, reply
            assert "hook-timeout" in reply["result"]["stderr"], reply
        else:
            assert "result" not in reply and isinstance(reply["error"]["code"], int), reply
            assert reply["error"]["message"], reply

        thread, out = _serve_in_thread(_frame({"argv": ["y"], "pid_file": str(next_pid_file)}, 3), ctx._pool_dispatch)
        thread.join(_NEXT_OP_CEILING_SECS)
        assert not thread.is_alive(), "the op after a wedged worker was never answered"
        assert out[0]["result"]["stdout"] == "ok\n", out
        pids.append(_pid(next_pid_file))
        assert _pid(next_pid_file) != _pid(hung_pid_file), "the next op ran on the wedged worker's process"
    finally:
        for pool in (ctx.__dict__.get("_dispatch_pool"), ctx.__dict__.get("_guard_pool")):
            if pool is not None:
                pool.shutdown(wait=False, cancel_futures=True)
        for pid in set(pids):
            try:
                psutil.Process(pid).kill()
            except psutil.Error:
                pass
