"""A stale engine makes the door spawn one `warm-engine-stop.py --restart`, and the next hook gets a warm verdict.

The fixture engine root carries a stub `coordinator/bin/warm-engine-stop.py` that records its argv, then binds
the warm endpoint (retrying until the dead engine's endpoint is released, as a make-before-break successor
would) and answers one request with an allow. The "dead" engine is a stale listener that answers the door's
request with a refusal the door can prove undispatched (-32601); the cold entrypoint never answers, so the
door kills its cold leg: the restart trigger. Without the script in the engine root nothing is spawned and no
warm verdict comes back.

Trap: a hook that finds no endpoint at all spawns the warm server (`door_maybe_spawn_server`) and claims the
shared boot stamp, which debounces the restart for that hook, so no `--restart` follows; and an engine that
accepts the request and stays silent is waited on for 8s and never restarted. Neither route is asserted here.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
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
)
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.warm_tier]

_DOOR_DIR = Path(__file__).resolve().parents[1]
_REPO = Path(__file__).resolve().parents[4]
_OP = "hooks.preuse_bash_dispatch"
_HANG = "import time\ntime.sleep(60)\n"
_WARM_MARK = "warm-restart-stub"
_BIND_BOUND_S = 15.0
_REFUSAL = b'{"jsonrpc":"2.0","id":1,"error":{"code":-32601,"message":"Method not found"}}\n'
_WARM_ALLOW = json.dumps(
    {
        "jsonrpc": "2.0",
        "id": 1,
        "result": {
            "stdout": json.dumps(
                {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "allow",
                                        "permissionDecisionReason": _WARM_MARK}}
            ) + "\n",
            "stderr": "",
            "exit_code": 0,
        },
    }
) + "\n"

_STUB_RESTART = '''\
import json, os, sys, time
from pathlib import Path

sys.path.insert(0, {repo!r})
root = Path(__file__).resolve().parents[2]
with open({record!r}, "a", encoding="utf-8") as fh:
    fh.write(json.dumps(sys.argv[1:]) + "\\n")
if "--restart" not in sys.argv:
    raise SystemExit(0)

from coordinator_core.warm import election, skew

reply = {reply!r}.encode()
deadline = time.time() + 30
if os.name == "nt":
    import _winapi
    name = election.pipe_name(skew.compute_client_token(root), engine_clone=root)
    handle = None
    while handle is None and time.time() < deadline:
        try:
            handle = election.elect(name)
        except OSError:
            time.sleep(0.02)
    if handle is None:
        raise SystemExit(3)
    Path({bound!r}).write_text(str(time.time()))
    _winapi.ConnectNamedPipe(handle, False)
    request = b""
    while b"\\n" not in request:
        chunk, _ = _winapi.ReadFile(handle, 65536)
        if not chunk:
            raise SystemExit(4)
        request += chunk
    _winapi.WriteFile(handle, reply)
    time.sleep(0.5)
else:
    import socket
    from coordinator_core.warm.tests.test_door_read_deadline_posix import _make_socket_dir
    path = election.socket_path(skew.compute_client_token(root), engine_clone=root)
    server = None
    while server is None and time.time() < deadline:
        try:
            _make_socket_dir(path)
            server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            server.bind(str(path))
        except OSError:
            server = None
            time.sleep(0.02)
    if server is None:
        raise SystemExit(3)
    server.listen(1)
    Path({bound!r}).write_text(str(time.time()))
    conn, _ = server.accept()
    request = b""
    while b"\\n" not in request:
        chunk = conn.recv(65536)
        if not chunk:
            raise SystemExit(4)
        request += chunk
    conn.sendall(reply)
    conn.close()
    server.close()
    path.unlink(missing_ok=True)
'''


def _payload(command: str) -> bytes:
    return json.dumps(
        {"hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_input": {"command": command}}
    ).encode()


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


class _StaleEngine:
    """A listener holding the warm endpoint that refuses the door's request (provably undispatched)
    and then keeps the endpoint until closed."""

    def __init__(self, root: Path, runtime_base: Path) -> None:
        self._root = root
        self._runtime_base = runtime_base
        self._stop = threading.Event()
        self._handle: int | None = None
        self._server: socket.socket | None = None
        self._path: Path | None = None
        self._ready = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()
        assert self._ready.wait(10), "the stale engine never bound"

    def _serve(self) -> None:
        try:
            if os.name == "nt":
                import _winapi

                from coordinator_core.warm import election

                self._handle = election.elect(pipe_name_for(self._root))
                self._ready.set()
                _winapi.ConnectNamedPipe(self._handle, False)
                request = b""
                while b"\n" not in request and not self._stop.is_set():
                    chunk, _ = _winapi.ReadFile(self._handle, 65536)
                    if not chunk:
                        break
                    request += chunk
                _winapi.WriteFile(self._handle, _REFUSAL)
            else:
                self._path = _socket_path_for(self._runtime_base, self._root)
                _make_socket_dir(self._path)
                self._server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                self._server.bind(str(self._path))
                self._server.listen(1)
                self._ready.set()
                conn, _ = self._server.accept()
                conn.recv(65536)
                conn.sendall(_REFUSAL)
                conn.close()
            self._stop.wait(60)
        except OSError:
            self._ready.set()

    def close(self) -> None:
        self._stop.set()
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
        self._thread.join(timeout=3)


class _Scenario:
    """Fixture engine root with an optional recording stub restart script."""

    def __init__(self, door: Path, tmp_path: Path, with_restart_script: bool) -> None:
        self.root = make_stub_engine_root(tmp_path) if os.name == "nt" else _make_posix_stub_engine_root(tmp_path)
        bin_dir = self.root / "coordinator" / "bin"
        for script in bin_dir.glob("*.py"):
            script.unlink()
        (bin_dir / "coordinator-invoke.py").write_text(_HANG, encoding="utf-8")
        self.record = tmp_path / "restart-argv.jsonl"
        self.bound = tmp_path / "restart-bound.txt"
        if with_restart_script:
            (bin_dir / "warm-engine-stop.py").write_text(
                _STUB_RESTART.format(repo=str(_REPO), record=str(self.record), bound=str(self.bound),
                                     reply=_WARM_ALLOW),
                encoding="utf-8",
            )
        self.home = tmp_path / "settings-home"
        self.home.mkdir()
        self.runtime = Path(tempfile.mkdtemp(prefix="der", dir="/tmp" if os.name != "nt" else None))
        self.exe = door_under_default_name(self.root) if os.name == "nt" else door

    def close(self) -> None:
        shutil.rmtree(self.runtime, ignore_errors=True)

    def hook(self, command: str = "echo hi") -> subprocess.CompletedProcess:
        env = dict(os.environ)
        env.update(
            COORDINATOR_DOOR_ENGINE_ROOT=str(self.root),
            COORDINATOR_WARM_RUNTIME_BASE=str(self.runtime),
            COORDINATOR_SETTINGS_HOME=str(self.home),
            COORDINATOR_DOOR_STDIN_MODE="hook",
        )
        return subprocess.run(
            [str(self.exe), _OP], input=_payload(command), capture_output=True, env=env,
            cwd=str(self.root), timeout=60, **no_console_creationflags(),
        )

    def spawns(self) -> list[list[str]]:
        if not self.record.exists():
            return []
        return [json.loads(line) for line in self.record.read_text(encoding="utf-8").splitlines() if line]

    def wait_bound(self, timeout: float) -> bool:
        end = time.time() + timeout
        while time.time() < end:
            if self.bound.exists() and self.bound.read_text().strip():
                return True
            time.sleep(0.05)
        return False


@pytest.fixture
def scenario(door, tmp_path):
    made = []

    def make(with_restart_script: bool) -> _Scenario:
        made.append(_Scenario(door, tmp_path, with_restart_script))
        return made[-1]

    yield make
    for item in made:
        item.close()


def _kill_the_warm_engine_and_send_one_hook(sc: _Scenario) -> tuple[subprocess.CompletedProcess, float, float]:
    """The stale engine refuses the request; the door's cold leg is killed; the hook returns. Then the
    dead engine's endpoint is released, as the make-before-break swap does. Returns the hook result, its
    wall seconds and the release time."""
    engine = _StaleEngine(sc.root, sc.runtime)
    try:
        started = time.time()
        proc = sc.hook()
        elapsed = time.time() - started
    finally:
        engine.close()
    return proc, elapsed, time.time()


def test_a_dead_engine_hook_spawns_exactly_one_restart_and_the_next_hook_is_warm(scenario):
    sc = scenario(True)
    first, elapsed, released_at = _kill_the_warm_engine_and_send_one_hook(sc)
    assert first.returncode == 0, first.stderr
    assert elapsed < 5, f"the restart held the hook for {elapsed:.1f}s"

    assert sc.wait_bound(_BIND_BOUND_S), f"no restart bound a warm endpoint; spawns={sc.spawns()}"
    assert float(sc.bound.read_text()) - released_at < _BIND_BOUND_S

    second = sc.hook()
    out = second.stdout.decode("utf-8", "replace")
    assert second.returncode == 0, second.stderr
    assert _WARM_MARK in out, f"the next hook did not get the warm verdict: {out!r}"
    assert json.loads(out)["hookSpecificOutput"]["permissionDecision"] == "allow"

    assert sc.spawns() == [["--restart"]]


def test_a_hook_with_no_endpoint_at_all_spawns_the_engine(scenario):
    """No listener exists: the no-server branch must still launch the engine (server start, the restart's
    counterpart for an absent endpoint), never fall through with no spawn. The stub server records its launch."""
    sc = scenario(True)
    server_dir = sc.root / "coordinator_core" / "warm"
    server_dir.mkdir(parents=True, exist_ok=True)
    (server_dir / "server.py").write_text(
        "import json, sys\n"
        f"with open({str(sc.record)!r}, 'a', encoding='utf-8') as fh:\n"
        "    fh.write(json.dumps(['server'] + sys.argv[1:]) + '\\n')\n",
        encoding="utf-8",
    )
    first = sc.hook()
    assert first.returncode == 0, first.stderr
    deadline = time.time() + _BIND_BOUND_S
    while time.time() < deadline and not sc.spawns():
        time.sleep(0.05)
    assert sc.spawns() == [["server"]], "the no-endpoint path spawned nothing"


def test_without_the_restart_script_nothing_spawns_and_no_warm_verdict_returns(scenario):
    sc = scenario(False)
    first, _, _ = _kill_the_warm_engine_and_send_one_hook(sc)
    assert first.returncode == 0, first.stderr

    assert not sc.wait_bound(3.0)
    assert sc.spawns() == []
    out = sc.hook().stdout.decode("utf-8", "replace")
    assert _WARM_MARK not in out
