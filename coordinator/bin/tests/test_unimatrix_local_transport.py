"""Native local transport checks run on Windows named pipes and POSIX sockets alike."""
import asyncio
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import uuid

import pytest


BIN = Path(__file__).resolve().parents[1]


def load(name):
    spec = importlib.util.spec_from_file_location(name.replace("-", "_"), BIN / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.parametrize("size", [1, 150000])
def test_native_control_roundtrip_without_eof_or_size_loss(size):
    router = load("unimatrix-peer-router")
    tmp = tempfile.mkdtemp(prefix="um-", dir=None if os.name == "nt" else "/tmp")

    async def go():
        endpoint = router.endpoint_path(f"transport-test-{uuid.uuid4().hex}", tmp)

        async def answer(reader, writer):
            request = json.loads(await reader.readline())
            writer.write((json.dumps({"ok": True, "size": len(request["text"])}) + "\n").encode())
            await writer.drain()
            writer.close()

        server = await router._transport().start_server(answer, endpoint, router.FRAME_LIMIT)
        try:
            reply = await asyncio.to_thread(router.request_line, endpoint, {"text": "x" * size}, 2)
            assert reply == {"ok": True, "size": size}
        finally:
            server.close()

    try:
        asyncio.run(go())
    finally:
        shutil.rmtree(tmp)


def test_router_list_uses_native_transport_on_this_os(tmp_path):
    router = load("unimatrix-peer-router")
    short = tempfile.mkdtemp(prefix="ur-", dir=None if os.name == "nt" else "/tmp")

    async def go():
        instance = router.Router([], sessions_dir=tmp_path / "sessions", state_dir=tmp_path / "state",
                                 endpoint_dir=short, adapters={"codex": None})
        await instance.start()
        try:
            result = await asyncio.to_thread(router.request_line, instance.ctl, {"op": "list"}, 2)
            assert result["ok"] is True and result["peers"] == [] and result["reachable"] == []
            assert router._transport().is_pipe(instance.ctl) == (sys.platform == "win32")
        finally:
            await instance.stop()

    try:
        asyncio.run(go())
    finally:
        shutil.rmtree(short)


def test_second_router_cannot_replace_a_live_listener(tmp_path):
    router = load("unimatrix-peer-router")
    short = tempfile.mkdtemp(prefix="ul-", dir=None if os.name == "nt" else "/tmp")

    async def go():
        kwargs = dict(sessions_dir=tmp_path / "sessions", state_dir=tmp_path / "state",
                      endpoint_dir=short, adapters={"codex": None})
        first, second = router.Router([], **kwargs), router.Router([], **kwargs)
        await first.start()
        try:
            with pytest.raises(RuntimeError, match="already owns"):
                await second.start()
            assert (await asyncio.to_thread(router.request_line, first.ctl, {"op": "list"}, 2))["ok"]
        finally:
            await first.stop()
        await second.start()
        try:
            assert (await asyncio.to_thread(router.request_line, second.ctl, {"op": "list"}, 2))["ok"]
        finally:
            await second.stop()

    try:
        asyncio.run(go())
    finally:
        shutil.rmtree(short)


@pytest.mark.parametrize("error,expected", [(None, True), (2, False), (121, True), (231, True), (5, False)])
def test_pipe_probe_does_not_open_or_write(monkeypatch, error, expected):
    from types import SimpleNamespace
    transport = load("unimatrix-local-transport")
    calls = []

    def wait(endpoint, timeout):
        calls.append((endpoint, timeout))
        if error is not None:
            exc = OSError("pipe status")
            exc.winerror = error
            raise exc

    monkeypatch.setitem(sys.modules, "_winapi", SimpleNamespace(WaitNamedPipe=wait))
    endpoint = "\\\\.\\pipe\\LOCAL\\cc-msg-" + "a" * 32
    assert transport.endpoint_exists(endpoint) is expected
    assert calls == [(endpoint, 1)]


def test_windows_lock_uses_nonblocking_range_at_zero(monkeypatch, tmp_path):
    from types import SimpleNamespace
    transport = load("unimatrix-local-transport")
    calls = []
    fake = SimpleNamespace(LK_NBLCK=2, LK_UNLCK=0, locking=lambda fd, mode, count: calls.append((mode, count)))
    monkeypatch.setitem(sys.modules, "msvcrt", fake)
    with (tmp_path / "lock").open("a+b") as file:
        assert transport.lock_file(file, windows=True)
        transport.unlock_file(file, windows=True)
    assert calls == [(2, 1), (0, 1)]


def test_native_writer_lock_is_live_only_while_held(tmp_path):
    router = load("unimatrix-peer-router")
    path = tmp_path / "writer.lock"
    with path.open("a+b") as held:
        held.write(b"0")
        held.flush()
        assert router.lock_held(path) is False
        assert router._transport().lock_file(held)
        try:
            assert router.lock_held(path) is True
        finally:
            router._transport().unlock_file(held)
        assert router.lock_held(path) is False


def test_windows_main_dispatches_router_instead_of_declining(monkeypatch):
    router = load("unimatrix-peer-router")
    monkeypatch.setattr(router.sys, "platform", "win32")
    monkeypatch.setattr(router, "cmd_serve", lambda argv: (argv == ["--peers", "p.json"]) and 0)
    assert router.main(["--peers", "p.json"]) == 0


def test_refused_named_pipe_delivery_is_not_reported_as_sent(monkeypatch):
    router = load("unimatrix-peer-router")
    monkeypatch.setattr(router._transport(), "pipe_exchange", lambda *args: b'{"ok":false,"error":"ENOINBOX"}\n')
    with pytest.raises(RuntimeError, match="ENOINBOX"):
        router.deliver("\\\\.\\pipe\\LOCAL\\cc-msg-" + "a" * 32, b'{}\n')


@pytest.mark.skipif(os.name == "nt", reason="Unix socket/file preservation")
def test_socket_helper_preserves_regular_file_and_live_listener(tmp_path):
    transport = load("unimatrix-local-transport")
    short = tempfile.mkdtemp(prefix="up-", dir="/tmp")

    async def go():
        endpoint = str(Path(short) / "endpoint.sock")
        Path(endpoint).write_text("must survive")
        with pytest.raises(FileExistsError, match="not a socket"):
            await transport.start_server(None, endpoint, 65536)
        assert Path(endpoint).read_text() == "must survive"
        Path(endpoint).unlink()

        async def reply(reader, writer):
            if await reader.readline():
                writer.write(b'{"ok":true}\n')
                await writer.drain()
            writer.close()

        server = await transport.start_server(reply, endpoint, 65536)
        try:
            with pytest.raises(FileExistsError, match="already has a listener"):
                await transport.start_server(reply, endpoint, 65536)
            router = load("unimatrix-peer-router")
            assert await asyncio.to_thread(router.request_line, endpoint, {"op": "list"}, 1) == {"ok": True}
        finally:
            server.close()

    try:
        asyncio.run(go())
    finally:
        shutil.rmtree(short)


@pytest.mark.skipif(os.name == "nt", reason="Unix relative socket and stale socket reuse")
def test_relative_socket_and_stale_socket_are_supported(tmp_path, monkeypatch):
    import socket
    transport = load("unimatrix-local-transport")
    monkeypatch.chdir(tmp_path)
    with socket.socket(socket.AF_UNIX) as stale:
        stale.bind("relative.sock")

    async def go():
        server = await transport.start_server(lambda reader, writer: writer.close(), "relative.sock", 65536)
        assert Path("relative.sock").exists()
        server.close()

    asyncio.run(go())


def test_windows_reply_command_is_executable_in_powershell(monkeypatch):
    from pathlib import PureWindowsPath
    router = load("unimatrix-peer-router")
    monkeypatch.setattr(router.sys, "platform", "win32")
    monkeypatch.setattr(router.sys, "executable", r"C:\Program Files\Python\python.exe")
    monkeypatch.setattr(router, "here", lambda: PureWindowsPath(r"C:\agent tools"))
    text = router.wrap_inbound("peer's-name", "question")
    assert "Reply to them with: & 'C:\\Program Files\\Python\\python.exe' 'C:\\agent tools\\unimatrix-send.py'" in text
    assert "--to 'peer''s-name' '<your reply>'" in text


def test_windows_codex_uses_persistent_queue_without_unix_socket(monkeypatch):
    codex = load("unimatrix-peer-codex")
    calls = []

    async def queue(thread, text, timeout):
        calls.append((thread, text, timeout))
        return "answer"

    monkeypatch.setattr(codex.sys, "platform", "win32")
    monkeypatch.setattr(codex, "queue_turn", queue)
    monkeypatch.setattr(codex.WsClient, "connect", lambda *args: pytest.fail("used Unix transport"))
    assert asyncio.run(codex.codex_turn("ignored.sock", "t1", "question", timeout=2)) == "answer"
    assert calls == [("t1", "question", 2)]


def test_rollout_tail_does_not_reread_history_and_keeps_a_torn_row(tmp_path):
    codex = load("unimatrix-peer-codex")
    path = tmp_path / "rollout.jsonl"
    path.write_bytes(b'{"old":1}\n')
    offset = path.stat().st_size
    with path.open("ab") as file:
        file.write(b'{"new":2}\n{"partial":')
    rows, offset = codex.appended_rows(path, offset)
    assert rows == [{"new": 2}]
    with path.open("ab") as file:
        file.write(b'3}\n')
    assert codex.appended_rows(path, offset)[0] == [{"partial": 3}]


def test_face_health_repairs_only_exited_children(tmp_path, monkeypatch):
    from types import SimpleNamespace
    mod = load("unimatrix-peer-router")
    router = mod.Router([], sessions_dir=str(tmp_path), state_dir=str(tmp_path))
    router.faces = {"live": SimpleNamespace(poll=lambda: None), "dead": SimpleNamespace(poll=lambda: 1)}
    router.peers = {name: {} for name in router.faces}
    repaired = []
    async def restart(name):
        repaired.append(name)
    monkeypatch.setattr(router, "restart_face", restart)
    asyncio.run(router._repair_faces())
    assert repaired == ["dead"]


@pytest.mark.parametrize("case", ["orphan", "owner-live", "recycled-face", "unstamped", "foreign-endpoint", "foreign-domain"])
def test_orphan_reconciliation_requires_local_ownership_and_birth(tmp_path, monkeypatch, case):
    mod = load("unimatrix-peer-router")
    router = mod.Router([], sessions_dir=str(tmp_path), state_dir=str(tmp_path), endpoint_dir=str(tmp_path))
    roster = mod._roster()
    entry = {"pid": 1234, "name": "test", "sessionId": "peer-test-1234", "pidDomain": "test-domain",
             "procStart": "face-birth", "unimatrixRouterPid": 1235, "unimatrixRouterStart": "owner-birth",
             "messagingSocketPath": router.endpoint_of("test")}
    if case == "unstamped":
        entry.pop("unimatrixRouterStart")
    if case == "foreign-endpoint":
        entry["messagingSocketPath"] = "other"
    if case == "foreign-domain":
        entry["pidDomain"] = "other"
    (tmp_path / "1234.json").write_text(json.dumps(entry))
    killed = []
    monkeypatch.setattr(roster, "pid_domain", lambda: "test-domain")
    monkeypatch.setattr(roster, "pid_alive", lambda pid: pid == 1234 or case == "owner-live")
    monkeypatch.setattr(roster, "proc_start", lambda pid: "owner-birth" if pid == 1235 else
                        "recycled" if case == "recycled-face" else "face-birth")
    monkeypatch.setattr(roster, "tree_kill", killed.append)
    router._reconcile_orphan_faces()
    assert killed == ([1234] if case == "orphan" else [])
    assert (tmp_path / "1234.json").exists() == (case != "orphan")


def test_face_health_retries_missing_face_after_failed_restart(tmp_path, monkeypatch):
    mod = load("unimatrix-peer-router")
    router = mod.Router([], sessions_dir=str(tmp_path), state_dir=str(tmp_path))
    router.peers = {"missing": {}}
    spawned = []
    async def spawn(name):
        spawned.append(name)
    monkeypatch.setattr(router, "_spawn_face", spawn)
    asyncio.run(router._repair_faces())
    assert spawned == ["missing"]


def test_failed_face_bind_stop_preserves_unowned_endpoint(tmp_path):
    from types import SimpleNamespace
    mod = load("unimatrix-peer-router")
    router = mod.Router([], sessions_dir=str(tmp_path), state_dir=str(tmp_path), endpoint_dir=str(tmp_path))
    endpoint = Path(router.endpoint_of("existing"))
    endpoint.write_text("unowned")
    router.faces["existing"] = SimpleNamespace(pid=1234, poll=lambda: 1, wait=lambda timeout: 1)
    asyncio.run(router.stop())
    assert endpoint.read_text() == "unowned"
