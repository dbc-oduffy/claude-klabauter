"""`proc.stop_own_child` is bound to the transport's `_caller.pid`, never a client-supplied `caller_pid`.

Python-client frames reach the op directly (`_bind_trusted_caller_pid`); native-door frames arrive as
`invoke.from_argv` and bind through `CLAUDE_PID` in the isolated pool-worker body. The door test runs
the shipped `door.exe` against a bridge pipe that feeds its real frame through `_serve_line`.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading

import pytest

from coordinator_core.warm import server
from coordinator_core.warm.tests.test_server_loop import _FakeVersionState, _frame
from coordinator_core.win_portability import no_console_creationflags

psutil = pytest.importorskip("psutil")

_OP = "proc.stop_own_child"


def _spawn_sleeper() -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(120)"], **no_console_creationflags()
    )


def _frame_with(params: dict, caller: dict | None) -> bytes:
    msg = json.loads(_frame(id_=1, method=_OP))
    msg["params"] = params
    if caller is not None:
        msg["_caller"] = caller
    return (json.dumps(msg) + "\n").encode()


def _serve(frame: bytes) -> dict:
    out: list[bytes] = []

    def dispatch(msg, *, caller=None, isolated=False):
        return server._pool_dispatch_worker_body(msg, caller)

    server._serve_line(
        frame,
        write=out.append,
        version_state=_FakeVersionState(skewed=False),
        server_sha="deadbeef",
        close_listener=lambda: pytest.fail("no close"),
        drain=lambda: pytest.fail("no drain"),
        release_in_flight=lambda: None,
        dispatch=dispatch,
    )
    return json.loads(b"".join(out).decode())


def test_transport_pid_stops_a_real_descendant_without_a_param():
    child = _spawn_sleeper()
    try:
        reply = _serve(_frame_with({"pid": child.pid, "root_pid": os.getpid()}, {"pid": str(os.getpid())}))
        assert reply["result"] == {"stopped": child.pid}
        assert child.wait(timeout=10) is not None
    finally:
        child.kill()


def test_user_supplied_caller_pid_is_overridden_by_the_transport_pid():
    child = _spawn_sleeper()
    other = _spawn_sleeper()
    try:
        # A forged caller_pid naming the child itself would hit "pid is the caller" if trusted.
        reply = _serve(
            _frame_with(
                {"pid": child.pid, "root_pid": os.getpid(), "caller_pid": child.pid},
                {"pid": str(os.getpid())},
            )
        )
        assert reply["result"] == {"stopped": child.pid}
        assert other.poll() is None
    finally:
        child.kill()
        other.kill()


def test_no_caller_envelope_drops_caller_pid_and_refuses():
    child = _spawn_sleeper()
    try:
        reply = _serve(_frame_with({"pid": child.pid, "root_pid": os.getpid(), "caller_pid": os.getpid()}, None))
        assert "caller pid unknown" in reply["result"]["refused"]
        assert child.poll() is None
    finally:
        child.kill()


@pytest.mark.skipif(os.name != "nt", reason="door.exe is a Windows binary")
@pytest.mark.spawns_process
def test_native_door_exe_stops_a_real_descendant(tmp_path):
    from coordinator_core.warm import election
    from coordinator_core.warm.tests.door_test_support import (
        door_under_default_name,
        make_stub_engine_root,
        pipe_name_for,
    )
    import _winapi

    root = make_stub_engine_root(tmp_path)
    handle = election.elect(pipe_name_for(root))

    def bridge() -> None:
        try:
            _winapi.ConnectNamedPipe(handle, False)
            buf = b""
            while b"\n" not in buf:
                chunk, _ = _winapi.ReadFile(handle, 65536)
                if not chunk:
                    return
                buf += chunk
            out: list[bytes] = []
            server._serve_line(
                buf.split(b"\n", 1)[0] + b"\n",
                write=out.append,
                version_state=_FakeVersionState(skewed=False),
                server_sha="deadbeef",
                close_listener=lambda: None,
                drain=lambda: None,
                release_in_flight=lambda: None,
                dispatch=lambda msg, *, caller=None, isolated=False: server._pool_dispatch_worker_body(msg, caller),
            )
            _winapi.WriteFile(handle, b"".join(out))
        except OSError:
            pass

    thread = threading.Thread(target=bridge, daemon=True)
    thread.start()
    # A launcher process L spawns a sleeper and the door as siblings: only the door's own pid
    # (`_caller.pid`) has L as an ancestor, never this bridge process, so a stale in-process pid refuses.
    launcher = (
        "import json,subprocess,sys,os\n"
        "kid=subprocess.Popen([sys.executable,'-c','import time;time.sleep(120)'])\n"
        "print(kid.pid,flush=True)\n"
        "r=subprocess.run([sys.argv[1],sys.argv[2],json.dumps({'pid':kid.pid,'root_pid':os.getpid()})],"
        "capture_output=True,text=True,timeout=80)\n"
        "print(r.stdout,r.stderr,flush=True)\n"
    )
    child = None
    try:
        env = dict(os.environ, COORDINATOR_DOOR_ENGINE_ROOT=str(root))
        proc = subprocess.run(
            [sys.executable, "-c", launcher, str(door_under_default_name(root)), _OP],
            capture_output=True, text=True, env=env, timeout=90, cwd=str(root), **no_console_creationflags(),
        )
        child = int(proc.stdout.split()[0])
        thread.join(timeout=10)
        assert "stopped" in proc.stdout, (proc.stdout, proc.stderr, proc.returncode)
        assert not psutil.pid_exists(child)
    finally:
        if child is not None and psutil.pid_exists(child):
            psutil.Process(child).kill()
        try:
            _winapi.CloseHandle(handle)
        except OSError:
            pass
