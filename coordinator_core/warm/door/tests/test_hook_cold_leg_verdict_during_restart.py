"""A PreToolUse Bash payload gets a guard verdict while the warm engine is down or restarting.

The door's cold leg runs the real `coordinator/bin/hook-run.py` guard chain (the stub engine root's
cold entrypoint is a shim onto it). Case A: no warm listener. Case B: a warm listener binds 300-1500ms
after the door starts. Every run must carry a verdict, never the "engine unreachable ... no verdict in
time" loud pass; an irreversible payload is warned about by name and allowed, never passed silently.

Instrument: `python -m coordinator_core.warm.door.tests.test_hook_cold_leg_verdict_during_restart --measure K [--command C]`
prints the cold chain's process time (user+sys, job-wide on Windows, children rusage on POSIX) beside wall
time.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from coordinator_core.warm.tests.door_test_support import (
    DOOR_EXE,
    door_under_default_name,
    make_stub_engine_root,
    pipe_name_for,
)
from coordinator_core.warm.tests.test_door_read_deadline_posix import (
    _make_socket_dir,
    _make_stub_engine_root as _make_posix_stub_engine_root,
    _socket_path_for,
    runtime_base,  # noqa: F401 -- fixture
)
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [
    pytest.mark.spawns_process,
    pytest.mark.cadence,
    pytest.mark.warm_tier,
]

_DOOR_DIR = Path(__file__).resolve().parents[1]
_REAL_COLD_ENTRYPOINT = Path(__file__).resolve().parents[4] / "coordinator" / "bin" / "hook-run.py"
_OP = "hooks.preuse_bash_dispatch"
_RUNS = 10
_WARM_BIND_DELAYS_MS = (300, 1500)
_NO_VERDICT_MARKERS = ("engine unreachable", "no verdict", "did not run")
_WARM_ALLOW = (
    '{"jsonrpc":"2.0","id":1,"result":{"stdout":"{\\"hookSpecificOutput\\":{\\"hookEventName\\":'
    '\\"PreToolUse\\",\\"permissionDecision\\":\\"allow\\"}}\\n","stderr":"","exit_code":0}}\n'
).encode()


def _payload(command: str) -> bytes:
    return json.dumps(
        {"hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_input": {"command": command}}
    ).encode()


def _cold_shim(entry: Path = _REAL_COLD_ENTRYPOINT) -> str:
    return (
        "import os, runpy, sys\n"
        f"_real = {str(entry)!r}\n"
        "sys.argv = [_real] + sys.argv[1:]\n"
        "sys.path.insert(0, os.path.dirname(_real))\n"
        "runpy.run_path(_real, run_name='__main__')\n"
    )


class _DelayedWarmListener:
    """Binds the warm endpoint `delay_ms` after construction (the restart's new server coming up),
    answers one request with an allow, and can be stopped whether or not it ever bound."""

    def __init__(self, root: Path, runtime_base: Path, delay_ms: int) -> None:
        self._root = root
        self._runtime_base = runtime_base
        self._delay_s = delay_ms / 1000.0
        self._stop = threading.Event()
        self._bound = threading.Event()
        self._path: Path | None = None
        self._server: socket.socket | None = None
        self._handle: int | None = None
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        if self._stop.wait(self._delay_s):
            return
        try:
            if os.name == "nt":
                self._serve_pipe()
            else:
                self._serve_socket()
        except OSError:
            pass

    def _serve_pipe(self) -> None:
        import _winapi

        from coordinator_core.warm import election

        self._handle = election.elect(pipe_name_for(self._root))
        self._bound.set()
        _winapi.ConnectNamedPipe(self._handle, False)
        request = b""
        while b"\n" not in request and not self._stop.is_set():
            chunk, _ = _winapi.ReadFile(self._handle, 65536)
            if not chunk:
                return
            request += chunk
        if not self._stop.is_set():
            _winapi.WriteFile(self._handle, _WARM_ALLOW)

    def _serve_socket(self) -> None:
        self._path = _socket_path_for(self._runtime_base, self._root)
        _make_socket_dir(self._path)
        self._server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._server.bind(str(self._path))
        self._server.listen(1)
        self._bound.set()
        conn, _ = self._server.accept()
        try:
            request = b""
            while b"\n" not in request and not self._stop.is_set():
                chunk = conn.recv(65536)
                if not chunk:
                    return
                request += chunk
            if not self._stop.is_set():
                conn.sendall(_WARM_ALLOW)
        finally:
            conn.close()

    def _nudge(self) -> None:
        # A bound listener blocks in accept/ConnectNamedPipe; one throwaway client releases it.
        try:
            if os.name == "nt":
                import _winapi

                client = _winapi.CreateFile(pipe_name_for(self._root), 0xC0000000, 0, 0, 3, 0, 0)
                _winapi.CloseHandle(client)
            elif self._path is not None:
                client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                client.connect(str(self._path))
                client.close()
        except OSError:
            pass

    def close(self) -> None:
        self._stop.set()
        if self._bound.is_set():
            self._nudge()
        self._thread.join(timeout=3)
        if os.name == "nt":
            if self._handle is not None:
                import _winapi

                try:
                    _winapi.CloseHandle(self._handle)
                except OSError:
                    pass
        else:
            if self._server is not None:
                self._server.close()
            if self._path is not None:
                self._path.unlink(missing_ok=True)


@pytest.fixture(scope="module")
def door(tmp_path_factory) -> Path:
    if os.name == "nt":
        if not DOOR_EXE.exists():
            pytest.skip(f"no built door at {DOOR_EXE}")
        return DOOR_EXE
    compiler = next((shutil.which(c) for c in ("cc", "clang", "gcc") if shutil.which(c)), None)
    if compiler is None:
        pytest.skip("no C compiler")
    out = tmp_path_factory.mktemp("door") / "coordinator-invoke"
    subprocess.run(
        [compiler, "-O2", "-std=c11", '-DPYTHON_BIN="python3"', "-o", str(out),
         str(_DOOR_DIR / "door_posix.c"), str(_DOOR_DIR / "door_core.c")],
        check=True, capture_output=True, **no_console_creationflags(),
    )
    return out


@pytest.fixture(scope="module")
def cold_engine(tmp_path_factory):
    """(entrypoint, env) of a stamped hardlink clone of this tree: the cold child under test is the tree's own
    chain, never the ambient engine root (the suite pins COORDINATOR_ENGINE_ROOT at this repo, which refuses)."""
    from coordinator_core.benchmarks import declare_benchmark_origin, isolated_clone

    declare_benchmark_origin()
    repo = _REAL_COLD_ENTRYPOINT.parents[2]
    parent = isolated_clone.mkdtemp_for_clone(repo, prefix="hcl-")
    try:
        clone = parent / "clone"
        isolated_clone.clone_engine(repo, clone, entrypoint="hook-run")
        shim = Path("coordinator") / "lib" / "resolve-claude-klabauter"
        isolated_clone._link_tree(repo / shim, clone / shim)
        clone = clone.resolve()
        settings = parent / "settings"
        settings.mkdir()
        env = isolated_clone.engine_env(clone)
        env.update({"COORDINATOR_ENGINE_ROOT": str(clone), "COORDINATOR_SETTINGS_HOME": str(settings)})
        yield clone / "coordinator" / "bin" / "hook-run.py", env
    finally:
        isolated_clone.rmtree_or_raise(parent, label="cold_leg_verdict_during_restart")


def _stub_root(tmp_path: Path, entry: Path = _REAL_COLD_ENTRYPOINT) -> Path:
    root = make_stub_engine_root(tmp_path) if os.name == "nt" else _make_posix_stub_engine_root(tmp_path)
    (root / "coordinator" / "bin" / "coordinator-invoke.py").write_text(_cold_shim(entry), encoding="utf-8")
    return root


def _run_door(door: Path, root: Path, runtime_base: Path, command: str, cold_env: dict | None = None):
    env = dict(cold_env if cold_env is not None else os.environ)
    env.update(
        COORDINATOR_DOOR_ENGINE_ROOT=str(root),
        COORDINATOR_WARM_RUNTIME_BASE=str(runtime_base),
        COORDINATOR_DOOR_STDIN_MODE="hook",
    )
    exe = door_under_default_name(root) if os.name == "nt" else door
    return subprocess.run(
        [str(exe), _OP], input=_payload(command), capture_output=True, env=env, cwd=str(root),
        timeout=60, **no_console_creationflags(),
    )


def _assert_a_verdict(proc: subprocess.CompletedProcess) -> dict:
    text = proc.stdout.decode("utf-8", "replace")
    assert proc.returncode == 0, proc.stderr
    assert not any(m in text for m in _NO_VERDICT_MARKERS), f"no-verdict loud pass: {text!r}"
    return json.loads(text)


_CASES = [pytest.param(None, id="A-engine-stopped")] + [
    pytest.param(d, id=f"B-warm-binds-after-{d}ms") for d in _WARM_BIND_DELAYS_MS
]


@pytest.mark.parametrize("bind_delay_ms", _CASES)
def test_a_bash_payload_gets_a_guard_verdict_on_every_run_while_warm_is_down(
    door, cold_engine, tmp_path, runtime_base, bind_delay_ms
):
    entry, cold_env = cold_engine
    root = _stub_root(tmp_path, entry)
    for run in range(_RUNS):
        listener = _DelayedWarmListener(root, runtime_base, bind_delay_ms) if bind_delay_ms else None
        try:
            proc = _run_door(door, root, runtime_base, "echo hi", cold_env)
        finally:
            if listener is not None:
                listener.close()
        _assert_a_verdict(proc)


def test_a_cold_guard_that_never_answers_keeps_the_loud_pass_floor_and_the_irreversible_deny(
    door, tmp_path, runtime_base
):
    """The timeout arm survives any fix: a dead box still passes loudly, and an irreversible payload
    (`git push`) is denied by name, never passed silently."""
    root = _stub_root(tmp_path)
    (root / "coordinator" / "bin" / "coordinator-invoke.py").write_text(
        "import time\ntime.sleep(60)\n", encoding="utf-8"
    )
    loud = _run_door(door, root, runtime_base, "echo hi")
    assert "returned no verdict in time" in loud.stdout.decode("utf-8", "replace")
    denied = _run_door(door, root, runtime_base, "git push origin main")
    body = json.loads(denied.stdout.decode("utf-8", "replace"))
    reason = body["hookSpecificOutput"]["permissionDecisionReason"]
    assert body["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "DENIED" in reason
    assert "git push origin main" in reason


def _measure_cold_chain(runs: int, command: str = "echo hi") -> dict:
    """Process time (user+sys, children included) and wall of the real cold entrypoint on the Bash payload.

    Trap: on Windows the child joins the job after `Popen` returns, so a grandchild spawned in that
    window is uncounted; the figure is a floor, not an exact total.
    """
    rows = []
    env = dict(os.environ, COORDINATOR_DOOR_STDIN_MODE="hook")
    for _ in range(runs):
        started = time.perf_counter()
        if os.name == "nt":
            import ctypes
            from ctypes import wintypes

            class _Basic(ctypes.Structure):
                _fields_ = [("user", ctypes.c_longlong), ("kernel", ctypes.c_longlong),
                            ("pu", ctypes.c_longlong), ("pk", ctypes.c_longlong),
                            ("faults", wintypes.DWORD), ("total", wintypes.DWORD),
                            ("active", wintypes.DWORD), ("terminated", wintypes.DWORD)]

            k32 = ctypes.WinDLL("kernel32", use_last_error=True)
            k32.CreateJobObjectW.restype = wintypes.HANDLE
            k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
            k32.QueryInformationJobObject.argtypes = [
                wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p,
            ]
            k32.CloseHandle.argtypes = [wintypes.HANDLE]
            job = k32.CreateJobObjectW(None, None)
            proc = subprocess.Popen(
                [sys.executable, str(_REAL_COLD_ENTRYPOINT), _OP], stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, **no_console_creationflags(),
            )
            k32.AssignProcessToJobObject(job, wintypes.HANDLE(int(proc._handle)))
            out, _ = proc.communicate(_payload(command))
            wall = (time.perf_counter() - started) * 1000
            info = _Basic()
            if not k32.QueryInformationJobObject(job, 1, ctypes.byref(info), ctypes.sizeof(info), None):
                raise OSError(ctypes.get_last_error(), "QueryInformationJobObject failed")
            cpu = (info.user + info.kernel) / 10000.0
            k32.CloseHandle(job)
        else:
            import resource

            before = resource.getrusage(resource.RUSAGE_CHILDREN)
            proc = subprocess.run(
                [sys.executable, str(_REAL_COLD_ENTRYPOINT), _OP], input=_payload(command),
                capture_output=True, env=env,
            )
            out = proc.stdout
            wall = (time.perf_counter() - started) * 1000
            after = resource.getrusage(resource.RUSAGE_CHILDREN)
            cpu = ((after.ru_utime - before.ru_utime) + (after.ru_stime - before.ru_stime)) * 1000
        rows.append({"cpu_ms": round(cpu, 1), "wall_ms": round(wall, 1), "out": out.decode("utf-8", "replace")[:80]})

    def pct(key: str, q: float) -> float:
        vals = sorted(r[key] for r in rows)
        return vals[min(len(vals) - 1, int(round(q * (len(vals) - 1))))]

    return {
        "runs": runs,
        **{key: {"p50": pct(key, .5), "p90": pct(key, .9), "max": max(r[key] for r in rows)}
           for key in ("cpu_ms", "wall_ms")},
        "rows": rows,
    }


if __name__ == "__main__":
    k = int(sys.argv[sys.argv.index("--measure") + 1]) if "--measure" in sys.argv else 12
    cmd = sys.argv[sys.argv.index("--command") + 1] if "--command" in sys.argv else "echo hi"
    print(json.dumps(_measure_cold_chain(k, cmd), indent=1))
