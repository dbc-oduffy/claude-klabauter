import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import threading

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "darwin", reason="darwin-only endpoint")


def exchange(path, request):
    with socket.socket(socket.AF_UNIX) as client:
        client.settimeout(3)
        client.connect(str(path))
        client.sendall(json.dumps(request).encode() + b"\n")
        return json.loads(client.makefile("rb").readline())


@pytest.fixture
def endpoint(tmp_path, monkeypatch):
    from coordinator_core.warm.restricted_endpoint import bound_listener, serve_one
    import psutil

    monkeypatch.chdir(tmp_path)
    Path("private").mkdir(mode=0o700)
    with bound_listener(Path("private")) as listener:
        def request(payload):
            thread = threading.Thread(target=serve_one, args=(listener, psutil.Process()))
            thread.start()
            try:
                return exchange(listener.getsockname(), payload)
            finally:
                thread.join(3)
                assert not thread.is_alive()
        yield listener, request


def test_allowed(endpoint):
    _, request = endpoint
    assert request({"op": "ping", "params": {}})["ok"] is True
    assert request({"op": "engine_ask.submit", "params": {
        "ask": "probe", "sizing": "also-supplied", "fire": False,
    }}) == {"refused": "bad-request", "reason": "Submit exactly one of ask, sizing."}


def test_real_mutating_dispatch_is_excluded(endpoint, tmp_path):
    import asyncio
    from coordinator_core.ipc import dispatch_message
    from coordinator_core.ops import scratch_record
    from coordinator_core.ops.dispatch_emit.run_scratch import ledger_path

    _, request = endpoint
    params = {"run_id": "run1", "paths": ["scratch/example"], "repo_root": str(tmp_path)}
    method = scratch_record.RECORD_OP_KEY
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    record = ledger_path(tmp_path, "run1")
    assert request({"op": method, "params": params}) == {"refused": "operation-denied"}
    assert not record.exists()
    result = asyncio.run(dispatch_message({
        "jsonrpc": "2.0", "id": 1, "method": method, "params": params, "_origin_worktree": str(tmp_path),
    }))
    assert result.get("result") == {"recorded": ["scratch/example"]}, result
    assert record.is_file()


@pytest.mark.parametrize("payload", [
    {"op": "ceremony.scoped_git_commit", "params": {}},
    {"op": "engine_ask.submit", "params": {"ask": "probe", "fire": True}},
    {"op": "engine_ask.submit", "params": {"ask": "probe", "sizing": "also-supplied", "fire": 0}},
    [{"op": "ping", "params": {}}],
    {"op": "Ping", "params": {}}, {"op": "pіng", "params": {}},
])
def test_denied_payload(endpoint, payload):
    assert endpoint[1](payload) == {"refused": "operation-denied"}


def test_other_peer_denied_before_request(endpoint):
    from coordinator_core.warm.restricted_endpoint import serve_one
    import psutil

    listener, _ = endpoint
    thread = threading.Thread(target=serve_one, args=(listener, psutil.Process()))
    thread.start()
    try:
        response = subprocess.check_output([
            sys.executable, "-c",
            "import socket,sys; s=socket.socket(socket.AF_UNIX); "
            "s.connect(sys.argv[1]); print(s.recv(256).decode())",
            listener.getsockname(),
        ], timeout=4)
        assert json.loads(response)["refused"] == "peer-denied"
        assert thread.is_alive()
        assert exchange(listener.getsockname(), {"op": "ping", "params": {}})["ok"]
    finally:
        thread.join(3)

def test_uid_checked_before_read(endpoint, monkeypatch):
    from coordinator_core.warm import restricted_endpoint as rx

    listener, _ = endpoint
    peer = rx.psutil.Process()
    client, server = socket.socketpair(socket.AF_UNIX)
    with client, server:
        monkeypatch.setattr(rx, "_peer_uid", lambda _: os.getuid() + 1)
        monkeypatch.setattr(rx, "_read_request", lambda _: pytest.fail("foreign UID read"))
        assert not rx._serve_connection(server, peer)
        assert json.loads(client.recv(256)) == {"refused": "peer-denied"}


@pytest.mark.parametrize("frame", [b"{bad}\n", b"x" * 1024, b"{}\n{}\n",
    b'{"op":"old","op":"ping","params":{}}\n', b"\xff\n"])
def test_refuses_invalid_frame(endpoint, frame):
    from coordinator_core.warm import restricted_endpoint as rx
    listener, _ = endpoint
    t = threading.Thread(target=rx.serve_one, args=(listener, rx.psutil.Process()))
    t.start()
    try:
        with socket.socket(socket.AF_UNIX) as client:
            client.settimeout(3)
            client.connect(listener.getsockname())
            client.sendall(frame)
            assert json.loads(client.recv(256)) == {"refused": "invalid-frame"}
    finally:
        t.join(3)


def test_handler_exception_replies(endpoint, monkeypatch):
    from coordinator_core.ops import ping
    def broken(_):
        raise RuntimeError("private diagnostic detail")
    monkeypatch.setattr(ping, "_ping", broken)
    assert endpoint[1]({"op": "ping", "params": {}}) == {"refused": "handler-error"}


def test_recover_dead_socket_only(tmp_path, monkeypatch):
    from coordinator_core.warm.restricted_endpoint import bound_listener
    monkeypatch.chdir(tmp_path)
    directory = Path("private")
    directory.mkdir(mode=0o700)
    address = directory / "diagnostic.sock"
    code = "import os, socket; s=socket.socket(socket.AF_UNIX); s.bind('private/diagnostic.sock'); s.listen(); os._exit(0)"
    subprocess.run([sys.executable, "-c", code], check=True)
    assert address.is_socket()
    with bound_listener(directory):
        with pytest.raises(OSError):
            with bound_listener(directory):
                pytest.fail("replaced active socket")
    address.write_text("operator")
    with pytest.raises(OSError):
        with bound_listener(directory):
            pytest.fail("replaced regular file")
    assert address.read_text() == "operator"


def test_darwin_socket_path_budget(tmp_path, monkeypatch):
    import errno
    from coordinator_core.warm.restricted_endpoint import bound_listener
    monkeypatch.chdir(tmp_path)
    directory = Path("é" * 46)
    directory.mkdir(mode=0o700)
    assert len(os.fsencode(directory / "diagnostic.sock")) >= 104
    with pytest.raises(OSError) as error:
        with bound_listener(directory):
            pytest.fail("accepted long socket path")
    assert error.value.errno == errno.ENAMETOOLONG


def test_native_parent_can_bind_its_child_at_spawn(tmp_path, monkeypatch):
    from coordinator_core.warm.restricted_endpoint import diagnostic_spawn

    monkeypatch.chdir(tmp_path)
    directory = Path("private")
    directory.mkdir(mode=0o700)
    worker = """
import json, socket, sys
with socket.socket(socket.AF_UNIX) as data:
    data.connect("private/diagnostic.sock")
    data.sendall(b'{"op":"ping","params":{}}\\n')
    print("queued", file=sys.stderr, flush=True)
    print(data.makefile("rb").readline().decode(), flush=True)
"""
    parent = """
import socket, struct, subprocess, sys, time
with socket.socket(socket.AF_UNIX) as host:
    host.settimeout(3)
    host.connect("private/spawn.sock")
    host.sendall(b"B")
    assert host.recv(1) == b"Y"
    # Delay exec in the child so Popen itself returns after the old 400 ms deadline.
    with subprocess.Popen([sys.executable, "-c", sys.argv[1]], stderr=subprocess.PIPE, preexec_fn=lambda: time.sleep(0.55)) as child:
        assert child.stderr.readline() == b"queued\\n"
        # Registration is deliberately later than the previous 100 ms race window.
        time.sleep(.2)
        host.sendall(struct.pack("!I", child.pid))
        assert host.recv(1) == b"Y"
        sys.exit(child.wait(timeout=3))
"""
    with diagnostic_spawn(directory, [sys.executable, "-c", parent, worker], os.environ) as child:
        output, _ = child.communicate(timeout=5)
        assert child.returncode == 0
        assert json.loads(output)["ok"] is True
    assert list(directory.iterdir()) == []


def test_live_unlocked_socket_is_preserved(tmp_path, monkeypatch):
    from coordinator_core.warm.restricted_endpoint import bound_listener

    monkeypatch.chdir(tmp_path)
    directory = Path("private")
    directory.mkdir(mode=0o700)
    address = directory / "diagnostic.sock"
    with socket.socket(socket.AF_UNIX) as existing:
        existing.bind(str(address))
        existing.listen()
        before = address.lstat()
        with pytest.raises(OSError):
            with bound_listener(directory):
                pytest.fail("replaced a live listener")
        assert os.path.samestat(before, address.lstat())


def test_unserializable_handler_reply_is_refused(endpoint, monkeypatch):
    from coordinator_core.ops import ping

    monkeypatch.setattr(ping, "_ping", lambda _: object())
    assert endpoint[1]({"op": "ping", "params": {}}) == {"refused": "handler-error"}


def test_serve_one_stops_when_idle_peer_exits(endpoint):
    import psutil
    from coordinator_core.warm.restricted_endpoint import serve_one

    listener, _ = endpoint
    with subprocess.Popen([sys.executable, "-c", "input()"], stdin=subprocess.PIPE) as child:
        peer = psutil.Process(child.pid)
        thread = threading.Thread(target=serve_one, args=(listener, peer), daemon=True)
        thread.start()
        try:
            import time
            time.sleep(.05)
            child.communicate(b"done\n", timeout=3)
            thread.join(1)
            assert not thread.is_alive()
        finally:
            if thread.is_alive():
                with socket.socket(socket.AF_UNIX) as wake:
                    wake.connect(listener.getsockname())
                thread.join(2)


def test_closed_registration_does_not_authorize_child(tmp_path, monkeypatch):
    from coordinator_core.warm.restricted_endpoint import diagnostic_spawn

    monkeypatch.chdir(tmp_path)
    directory = Path("private")
    directory.mkdir(mode=0o700)
    worker = """
import json, socket
with socket.socket(socket.AF_UNIX) as data:
    data.connect("private/diagnostic.sock")
    print(data.recv(256).decode(), flush=True)
"""
    parent = """
import socket, subprocess, sys
with socket.socket(socket.AF_UNIX) as host:
    host.settimeout(2)
    host.connect("private/spawn.sock")
    host.sendall(b"B")
    assert host.recv(1) == b"Y"
    host.sendall(b"\\x00")
subprocess.run([sys.executable, "-c", sys.argv[1]], check=True)
"""
    with diagnostic_spawn(directory, [sys.executable, "-c", parent, worker], os.environ) as child:
        output, _ = child.communicate(timeout=4)
        assert child.returncode == 0
        assert json.loads(output) == {"refused": "peer-denied"}


def test_unregistered_pty_child_is_refused(tmp_path, monkeypatch):
    from coordinator_core.warm.restricted_endpoint import diagnostic_spawn

    monkeypatch.chdir(tmp_path)
    Path("private").mkdir(mode=0o700)
    child = """
import os, socket
assert os.isatty(1)
with socket.socket(socket.AF_UNIX) as conn:
    conn.connect("private/diagnostic.sock")
    print(conn.recv(256).decode(), flush=True)
"""
    parent = """
import os, pty, subprocess, sys
master, slave = pty.openpty()
with subprocess.Popen([sys.executable, "-c", sys.argv[1]], stdout=slave) as child:
    os.close(slave)
    sys.stdout.buffer.write(os.read(master, 1024))
    os.close(master)
    sys.exit(child.wait(timeout=2))
"""
    with diagnostic_spawn(Path("private"), [sys.executable, "-c", parent, child], os.environ) as proc:
        output, _ = proc.communicate(timeout=4)
        assert proc.returncode == 0
        assert json.loads(output) == {"refused": "peer-denied"}


def test_diagnostic_stream_listens_before_popen(tmp_path, monkeypatch):
    from coordinator_core.warm.restricted_endpoint import diagnostic_spawn

    monkeypatch.chdir(tmp_path)
    directory = Path("rx")
    directory.mkdir(mode=0o700)
    popen = subprocess.Popen
    observed = []

    def launch(*args, **kwargs):
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
            probe.connect(str(directory / "diagnostic.sock"))
        observed.append("listening")
        return popen(*args, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", launch)
    with diagnostic_spawn(directory, [sys.executable, "-c", "pass"], os.environ) as child:
        assert child.wait(timeout=3) == 0
    assert observed == ["listening"]


def test_concurrent_launch_starts_second_process_without_diagnostics(tmp_path, monkeypatch, capsys):
    from coordinator_core.warm.restricted_endpoint import diagnostic_spawn

    monkeypatch.chdir(tmp_path)
    Path("rx").mkdir(mode=0o700)
    wait = "import sys; print('started',flush=True); import time; time.sleep(0.8)"
    with diagnostic_spawn(Path("rx"), [sys.executable, "-c", wait], os.environ) as first:
        assert first.stdout.readline() == b"started\n"
        own_socket = Path("rx/diagnostic.sock").stat()
        with diagnostic_spawn(Path("rx"), [sys.executable, "-c", "print('second')"], os.environ) as second:
            assert second.communicate(timeout=2)[0] == b"second\n"
            assert second.returncode == 0
        assert capsys.readouterr().err == "headless-em-launch: diagnostic endpoint busy; starting without diagnostics\n"
        assert first.poll() is None
        assert os.path.samestat(own_socket, Path("rx/diagnostic.sock").stat())


def test_partial_pid_deadline_releases_control_for_next_spawn(tmp_path, monkeypatch):
    from coordinator_core.warm.restricted_endpoint import diagnostic_spawn

    monkeypatch.chdir(tmp_path)
    Path("rx").mkdir(mode=0o700)
    parent = """
import socket
def begin():
    s=socket.socket(socket.AF_UNIX)
    s.settimeout(1.5)
    s.connect("rx/spawn.sock")
    s.sendall(b"B")
    assert s.recv(1)==b"Y"
    return s
with begin() as first:
    first.sendall(b"\\x00")
    with begin():
        assert first.recv(1)==b""
"""
    with diagnostic_spawn(Path("rx"), [sys.executable, "-c", parent], os.environ) as child:
        assert child.wait(timeout=4) == 0


def test_dead_service_listener_reports_failure(endpoint, capsys):
    import psutil
    from coordinator_core.warm.restricted_endpoint import _serve_spawned

    listener, _ = endpoint
    with socket.socket(socket.AF_UNIX) as control:
        control.close()
        _serve_spawned(listener, control, psutil.Process(), threading.Event())
    assert "diagnostic listener stopped" in capsys.readouterr().err
