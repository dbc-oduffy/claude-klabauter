"""coordinator-external-session bridge: one warm round trip, no engine imports, loud miss."""
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

BRIDGE = Path(__file__).resolve().parents[1] / "coordinator-external-session.py"
STAMP = b"test-stamp\n"


@pytest.fixture
def engine(tmp_path, monkeypatch):
    root = tmp_path / "engine"
    (root / "coordinator_core").mkdir(parents=True)
    (root / "coordinator_core" / "_engine_stamp").write_bytes(STAMP)
    base = tmp_path / "runtime"
    base.mkdir()
    monkeypatch.setenv("COORDINATOR_WARM_RUNTIME_BASE", str(base))
    from coordinator_core.warm import skew
    return root, skew.stamp_token(STAMP)


class FakeServer:
    """One-connection warm server on the endpoint the real election derives for `root`."""

    def __init__(self, root, token, reply):
        self.reply, self.request = reply, None
        if sys.platform == "win32":
            self._init_pipe(root, token)
        else:
            self._init_socket(root, token)
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _init_pipe(self, root, token):
        import ctypes
        from ctypes import wintypes
        from coordinator_core.warm import election
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateNamedPipeW.restype = wintypes.HANDLE
        k32.CreateNamedPipeW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD,
                                         wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p]
        name = election.pipe_name(token, engine_clone=root, user_sid=election.current_user_sid())
        self.k32, self.handle = k32, k32.CreateNamedPipeW(name, 3, 0, 1, 65536, 65536, 0, None)
        assert self.handle not in (None, -1, 0xFFFFFFFFFFFFFFFF)

    def _init_socket(self, root, token):
        import socket
        from coordinator_core.warm import election
        path = election.socket_path(token, engine_clone=root)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.listener.bind(str(path))
        self.listener.listen(1)

    def _serve(self):
        if sys.platform == "win32":
            import ctypes
            from ctypes import wintypes
            k32, h = self.k32, wintypes.HANDLE(self.handle)
            k32.ConnectNamedPipe(h, None)
            data, buf, n = b"", ctypes.create_string_buffer(65536), wintypes.DWORD()
            while not data.endswith(b"\n"):
                if not k32.ReadFile(h, buf, 65536, ctypes.byref(n), None):
                    break
                data += buf.raw[:n.value]
            self.request = json.loads(data)
            if self.reply is not None:
                out = json.dumps(self.reply).encode() + b"\n"
                k32.WriteFile(h, out, len(out), ctypes.byref(n), None)
                k32.FlushFileBuffers(h)
            else:
                time.sleep(2.5)
            k32.CloseHandle(h)
        else:
            conn, _ = self.listener.accept()
            stream = conn.makefile("rwb")
            self.request = json.loads(stream.readline())
            if self.reply is not None:
                stream.write(json.dumps(self.reply).encode() + b"\n")
                stream.flush()
            else:
                time.sleep(2.5)
            conn.close()
            self.listener.close()


def run_bridge(root, event="UserPromptSubmit", payload=None, cwd=None, extra=()):
    env = {**os.environ, "BRIDGE_TEST_MARKER": "marker-value"}
    proc = subprocess.run(
        [sys.executable, str(BRIDGE), "--event", event, "--engine-root", str(root), *extra],
        input=json.dumps(payload if payload is not None else {"session_id": "s-1", "cwd": str(cwd)}),
        capture_output=True, text=True, cwd=cwd, env=env, timeout=30)
    return proc


def test_hit_writes_op_output_and_sends_env_event_and_pid(engine, tmp_path):
    root, token = engine
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    output = {"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": "ctx"}}
    server = FakeServer(root, token, {"jsonrpc": "2.0", "id": 1, "result": {"output": output, "notices": ["note-one"]}})
    proc = run_bridge(root, cwd=foreign)
    server.thread.join(5)
    assert proc.returncode == 0
    assert json.loads(proc.stdout) == output
    assert "note-one" in proc.stderr
    request = server.request
    assert request["method"] == "external_session.handle"
    assert request["_engine_token"] == token
    params = request["params"]
    assert params["event"] == "UserPromptSubmit"
    assert params["payload"]["session_id"] == "s-1"
    assert params["env"]["BRIDGE_TEST_MARKER"] == "marker-value"
    assert isinstance(params["caller_pid"], int) and params["caller_pid"] > 0


def _degraded(stdout: str) -> bool:
    """A miss answers the typed advisory, never a silent `{}`: the operator must see the hook degraded."""
    out = json.loads(stdout)
    return list(out) == ["systemMessage"] and out["systemMessage"].startswith("coordinator-external-session: degraded: ")


def test_no_server_is_a_typed_advisory_exit_zero(engine, tmp_path):
    root, _ = engine
    proc = run_bridge(root, cwd=tmp_path)
    assert proc.returncode == 0
    assert _degraded(proc.stdout)
    assert "external session context unavailable" in proc.stderr


def test_error_response_is_a_typed_advisory(engine, tmp_path):
    root, token = engine
    server = FakeServer(root, token, {"jsonrpc": "2.0", "id": 1, "error": {"code": -32601, "message": "no op"}})
    proc = run_bridge(root, cwd=tmp_path)
    server.thread.join(5)
    assert proc.returncode == 0 and _degraded(proc.stdout)
    assert "external session context unavailable" in proc.stderr


def test_silent_server_misses_within_the_hook_deadline(engine, tmp_path):
    root, token = engine
    server = FakeServer(root, token, None)
    proc = run_bridge(root, cwd=tmp_path)
    server.thread.join(10)
    assert proc.returncode == 0 and _degraded(proc.stdout)
    assert "external session context unavailable" in proc.stderr
    import importlib.util
    spec = importlib.util.spec_from_file_location("coordinator_external_session_bridge", BRIDGE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert f"no warm response within {module.DEADLINE_SECS}s" in proc.stderr


@pytest.mark.parametrize("argv_event,stdin", [("NotAnEvent", "{}"), ("Stop", "[]"), ("Stop", "not json")])
def test_bad_input_is_a_typed_advisory(engine, tmp_path, argv_event, stdin):
    root, _ = engine
    proc = subprocess.run([sys.executable, str(BRIDGE), "--event", argv_event, "--engine-root", str(root)],
                          input=stdin, capture_output=True, text=True, cwd=tmp_path, timeout=30)
    assert proc.returncode == 0 and _degraded(proc.stdout)
    assert "external session context unavailable" in proc.stderr


def test_bridge_imports_no_coordinator_core_module(engine, tmp_path):
    root, _ = engine
    proc = subprocess.run(
        [sys.executable, "-X", "importtime", str(BRIDGE), "--event", "Stop", "--engine-root", str(root)],
        input="{}", capture_output=True, text=True, cwd=tmp_path, timeout=30)
    assert _degraded(proc.stdout)
    imported = [line.rpartition("|")[2].strip() for line in proc.stderr.splitlines() if "|" in line]
    assert [name for name in imported if name.startswith("coordinator_core")] == []
