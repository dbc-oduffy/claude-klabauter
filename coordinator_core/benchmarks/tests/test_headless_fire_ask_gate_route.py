"""The headless-fire ask_gate route benchmark classifies route and error code and never hangs.

Behaviour pinned: a spawn of the cold-operation module is `cold` and its absence is `warm`; engine error
envelopes, subprocess timeouts and other exceptions map to their error codes; the production entry
point (`measure`) bills a real `_invoke_source` call, reports a bounded-timeout call as
`error_code='timeout'` on the cold route with process time and a spawn, instead of hanging.

The real-server cases boot a real warm server child at a stamped temp engine root and drive the
`headless.py` copy living at that root through `_invoke_source('dispatch.ask_gate', ...)`: the client,
the endpoint election and the token are production code; only the server's op body (`_pool_dispatch`)
is a stub, as in the warm suite's other real-server harnesses.
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import psutil
import pytest

from coordinator_core.benchmarks import headless_fire_ask_gate_route as bench
from coordinator_core.ops.workflow_fire import headless as real_headless
from coordinator_core.warm import breadcrumb, client, election, engine_root as engine_root_mod, skew

pytestmark = pytest.mark.cadence

_BOOT_DEADLINE_SECS = 30.0
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
ASK = {"sizing_path": "x.yaml", "run_id": "ask-bench"}

_SERVER_CHILD = r'''
import os, sys
from pathlib import Path

repo, root = sys.argv[1], Path(sys.argv[2])
sys.path.insert(0, repo)
from coordinator_core.warm import breadcrumb, server, skew


class _Version:
    server_sha = "headless-route"
    last_skew_axes = ()

    def is_skewed(self, client_token):
        return False

    def is_token_skewed(self, client_token):
        return False


def _gated(self, msg, **_kw):
    return {"jsonrpc": "2.0", "id": msg["id"], "result": {"status": "gated", "served_by": "warm-server"}}


server._ServerContext._pool_dispatch = _gated
token = skew.compute_client_token(root)
on_windows = sys.platform == "win32"
elected = server._elect_windows_pipe(root, token) if on_windows else server._elect_unix_socket_endpoint(root, token)
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
'''


def test_route_is_cold_only_when_cold_operation_is_spawned():
    cold = [["python", "-m", bench.COLD_MODULE, "req.json", "resp.json"]]
    assert bench.classify_route(cold) == "cold"
    assert bench.classify_route([]) == "warm"
    assert bench.classify_route([["git", "status"]]) == "warm"


def test_error_codes():
    assert bench.error_code_of(None) is None
    assert bench.error_code_of(subprocess.TimeoutExpired("x", 1)) == "timeout"
    assert bench.error_code_of(RuntimeError(json.dumps({"code": -32004, "message": "hung"}))) == -32004
    assert bench.error_code_of(RuntimeError("plain")) == "RuntimeError"
    assert bench.error_code_of(ValueError("v")) == "ValueError"


def test_unstamped_refusal_code_is_the_engine_constant():
    from coordinator_core import ipc

    assert bench.UNSTAMPED_ENGINE_ROOT_CODE == ipc.UNSTAMPED_ENGINE_ROOT_ERROR == -32005


def test_route_note_is_read_and_absent_note_is_empty(tmp_path):
    assert bench.read_route_note(tmp_path) == {}
    (tmp_path / "warm_route.json").write_text("not json", encoding="utf-8")
    assert bench.read_route_note(tmp_path) == {}
    (tmp_path / "warm_route.json").write_text(json.dumps({"route": "cold", "reason": "unstamped_root"}), encoding="utf-8")
    assert bench.read_route_note(tmp_path) == {"route": "cold", "reason": "unstamped_root"}


def test_bounded_call_reports_timeout_on_cold_route(tmp_path, monkeypatch):
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("COORDINATOR_WARM", "0")
    report = bench.measure(timeout=0.05)
    assert report["route"] == "cold"
    assert report["error_code"] == "timeout"
    assert report["spawns"] >= 1
    assert report["process_ms"] > 0
    assert set(report) >= {"route", "process_ms", "spawns", "error_code", "fallthrough_reason"}


def _stamped_root(base: Path, name: str, stamp: str) -> Path:
    """A stamped engine root holding its own copy of `headless.py`, so `parents[3]` of that copy is the root."""
    root = base / name
    wf = root / "coordinator_core" / "ops" / "workflow_fire"
    wf.mkdir(parents=True)
    (root / "coordinator_core" / "warm").mkdir()
    (root / ".git").mkdir()
    (root / "coordinator_core" / "_engine_stamp").write_text(stamp, encoding="utf-8")
    shutil.copy(real_headless.__file__, wf / "headless.py")
    return root


def _load_headless_at(root: Path):
    path = root / "coordinator_core" / "ops" / "workflow_fire" / "headless.py"
    spec = importlib.util.spec_from_file_location(f"headless_at_{root.name}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run_as_interpreter_at(monkeypatch, root: Path) -> None:
    """Make every engine-clone resolver the warm client consults answer `root`, as an interpreter running from it would.

    The token stays the real `skew.compute_client_token` over `root`'s stamp: only the root resolution moves.
    """
    monkeypatch.setattr(engine_root_mod, "current_engine_clone", lambda: root)
    monkeypatch.setattr(client, "_engine_clone_root", lambda: root)
    monkeypatch.setattr(breadcrumb, "_default_engine_clone", lambda: root)
    monkeypatch.setattr(election, "_default_engine_clone", lambda: root)
    monkeypatch.setattr(client, "_configured_root_is_foreign", lambda: False)
    monkeypatch.setattr(client, "_sidecar_keyed_clone", lambda: None)


@pytest.fixture()
def warm_world(tmp_path, monkeypatch):
    """Real-client env on a short runtime base, a `_spawn_once` the client cannot use to start servers, and teardown."""
    rt = Path(tempfile.mkdtemp(prefix="hf-", dir=None if os.name == "nt" else "/tmp"))
    monkeypatch.setenv("COORDINATOR_WARM", "1")
    monkeypatch.setenv(breadcrumb.RUNTIME_BASE_ENV, str(rt))
    monkeypatch.setattr(client, "_spawn_once", lambda: None)
    procs: list[subprocess.Popen] = []
    yield procs
    for proc in procs:
        if proc.poll() is None:
            proc.kill()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            pass
    shutil.rmtree(rt, ignore_errors=True)


def _boot_server(root: Path, procs: list) -> subprocess.Popen:
    script = root / "server_child.py"
    script.write_text(_SERVER_CHILD, encoding="utf-8")
    repo = str(Path(real_headless.__file__).resolve().parents[3])
    proc = subprocess.Popen(
        [sys.executable, str(script), repo, str(root)],
        stdout=open(root / "server.log", "ab"), stderr=subprocess.STDOUT, creationflags=_NO_WINDOW,
    )
    procs.append(proc)
    deadline = time.monotonic() + _BOOT_DEADLINE_SECS
    while time.monotonic() < deadline:
        crumb = breadcrumb.read_breadcrumb(root)
        if crumb and crumb.get("pid") == proc.pid:
            return proc
        if proc.poll() is not None:
            pytest.fail(f"server child exited {proc.returncode}: {(root / 'server.log').read_text(errors='replace')[-800:]}")
        time.sleep(0.1)
    pytest.fail(f"server child not ready within {_BOOT_DEADLINE_SECS}s")


def _no_cold(module, monkeypatch) -> list:
    ran: list = []
    monkeypatch.setattr(module.subprocess, "run", lambda command, **kw: ran.append(command) or pytest.fail("cold_operation spawned"))
    return ran


def test_real_warm_server_at_executor_root_serves_ask_gate_warm_inside_the_bar(tmp_path, monkeypatch, warm_world):
    root = _stamped_root(tmp_path, "executor", "sha:" + "a" * 40 + "\n")
    _run_as_interpreter_at(monkeypatch, root)
    headless = _load_headless_at(root)
    server = _boot_server(root, warm_world)
    _no_cold(headless, monkeypatch)
    srv = psutil.Process(server.pid)
    srv_before = sum(srv.cpu_times()[:2])
    started = time.process_time()
    result = headless._invoke_source("dispatch.ask_gate", ASK, str(tmp_path), tmp_path / "op", 15)
    process_ms = ((time.process_time() - started) + (sum(srv.cpu_times()[:2]) - srv_before)) * 1000
    assert result == {"status": "gated", "served_by": "warm-server"}
    note = json.loads((tmp_path / "op" / "warm_route.json").read_text(encoding="utf-8"))
    assert note["route"] == "warm" and note["reason"] is None
    assert note["token"] == skew.compute_client_token(root)
    assert note["engine_root"] == str(root.resolve())
    assert process_ms < bench.BAR_MS, f"client plus server process time {process_ms:.1f}ms over the brightline"
    assert bench.read_route_note(tmp_path / "op")["route"] == "warm"


def test_server_at_a_different_stamp_is_never_reached_and_the_fallthrough_is_recorded(tmp_path, monkeypatch, warm_world, capsys):
    server_root = _stamped_root(tmp_path, "server", "sha:" + "b" * 40 + "\n")
    _run_as_interpreter_at(monkeypatch, server_root)
    _boot_server(server_root, warm_world)
    executor = _stamped_root(tmp_path, "executor", "sha:" + "c" * 40 + "\n")
    assert skew.compute_client_token(executor) != skew.compute_client_token(server_root)
    _run_as_interpreter_at(monkeypatch, executor)
    headless = _load_headless_at(executor)
    ran: list = []
    monkeypatch.setattr(headless.subprocess, "run", lambda command, **kw: ran.append(command))
    with pytest.raises(FileNotFoundError):
        headless._invoke_source("dispatch.ask_gate", ASK, str(tmp_path), tmp_path / "op", 15)
    assert len(ran) == 1 and "coordinator_core.ops.workflow_fire.cold_operation" in ran[0]
    note = bench.read_route_note(tmp_path / "op")
    assert note["route"] == "cold" and note["reason"] == "no_server"
    assert "routed cold: no_server" in capsys.readouterr().err


def test_private_engine_root_with_the_shared_stamp_still_falls_through_cold(tmp_path, monkeypatch, warm_world):
    stamp = "sha:" + "d" * 40 + "\n"
    shared = _stamped_root(tmp_path, "shared", stamp)
    _run_as_interpreter_at(monkeypatch, shared)
    _boot_server(shared, warm_world)
    private = _stamped_root(tmp_path, "private", stamp)
    assert skew.compute_client_token(private) == skew.compute_client_token(shared)
    _run_as_interpreter_at(monkeypatch, private)
    headless = _load_headless_at(private)
    ran: list = []
    monkeypatch.setattr(headless.subprocess, "run", lambda command, **kw: ran.append(command))
    with pytest.raises(FileNotFoundError):
        headless._invoke_source("dispatch.ask_gate", ASK, str(tmp_path), tmp_path / "op", 15)
    assert len(ran) == 1
    note = bench.read_route_note(tmp_path / "op")
    assert note["route"] == "cold" and note["reason"] == "no_server"
    assert note["token"] == skew.compute_client_token(shared)
