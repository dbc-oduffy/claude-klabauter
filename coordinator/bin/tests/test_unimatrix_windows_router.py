"""Real Windows pipe round trips, registration children, discovery and naming."""
import asyncio
import importlib.util
import json
import os
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "windows_router", Path(__file__).resolve().parents[1] / "unimatrix-peer-router.py")
router = importlib.util.module_from_spec(spec)
spec.loader.exec_module(router)


def test_names_distinguish_sessions_and_normalize_foreign_paths():
    name = router.readable_peer_name("codex", "thread-one", r"E:\work\claude-klabauter")
    assert name.startswith("codex-claude-klabauter-")
    assert "-on-" in name and "@" not in name
    assert name != router.readable_peer_name("codex", "thread-two", "/work/claude-klabauter")
    assert name == router.readable_peer_name("codex", "thread-one", "/work/claude-klabauter")


@pytest.mark.skipif(os.name != "nt", reason="Windows named pipes")
def test_pipe_router_faces_and_private_acl(tmp_path):
    async def run():
        calls = []
        async def answer(peer, text):
            calls.append(text)
            return "received " + text.splitlines()[0]
        r = router.Router([{"name": "codex-test", "vendor": "codex", "target": "test"}],
                          sessions_dir=tmp_path / "sessions", state_dir=tmp_path / "state",
                          adapters={"codex": answer}, endpoint_dir=tmp_path / "endpoints")
        try:
            await r.start()
            response = await asyncio.to_thread(router.request_line, r.ctl, {"op": "list"})
            assert response["peers"] == ["codex-test"]
            response = await asyncio.to_thread(router.request_line, r.ctl,
                {"op": "send", "to": "codex-test", "text": "test message"})
            assert response["ok"] and "test message" in response["reply"]
            import win32security
            sd = r.server.security.SECURITY_DESCRIPTOR
            assert sd.GetSecurityDescriptorDacl().GetAceCount() == 1
            assert sd.GetSecurityDescriptorControl()[0] & win32security.SE_DACL_PROTECTED
            frame = router._contract().build_frame("unused", "test-sender", "face message")
            def send_split_headers():
                import time
                import win32file
                ep = r.endpoint_of("codex-test")
                h = win32file.CreateFile(ep, win32file.GENERIC_READ | win32file.GENERIC_WRITE,
                                         0, None, win32file.OPEN_EXISTING, 0, None)
                try:
                    win32file.WriteFile(h, b'{"type":"auth"}\n')
                    time.sleep(0.02)
                    win32file.WriteFile(h, b'{"type":"auth"}\n' + (json.dumps(frame) + "\n").encode())
                finally:
                    h.Close()
            await asyncio.to_thread(send_split_headers)
            for _ in range(100):
                if len(calls) == 2:
                    break
                await asyncio.sleep(0.02)
            assert len(calls) == 2 and "face message" in calls[-1]
            assert all(p.poll() is None for p in r.faces.values())
        finally:
            await r.stop()
        assert not list((tmp_path / "sessions").glob("*.json"))
    asyncio.run(run())


@pytest.mark.skipif(os.name != "nt", reason="Windows byte-range locks")
def test_windows_lock_discovery(tmp_path):
    import msvcrt
    lock_path = tmp_path / "thread.lock"
    with open(lock_path, "w+b") as held:
        msvcrt.locking(held.fileno(), msvcrt.LK_NBLCK, 1)
        assert router.lock_held(lock_path)
        assert len(router.load_lock_peers(tmp_path, "codex")) == 1
        msvcrt.locking(held.fileno(), msvcrt.LK_UNLCK, 1)
        assert not router.lock_held(lock_path)
        assert router.load_lock_peers(tmp_path, "codex") == []


@pytest.mark.skipif(os.name != "nt", reason="Windows named pipes")
def test_missing_pipe_uses_socket_error_contract(tmp_path):
    endpoint = router.endpoint_path("missing", tmp_path)
    with pytest.raises(OSError):
        router.request_line(endpoint, {"op": "list"}, timeout=0.01)
    with pytest.raises(OSError):
        router.deliver(endpoint, b"test\n")


@pytest.mark.skipif(os.name != "nt", reason="Windows named pipes")
def test_endpoint_directory_namespaces_windows_pipes(tmp_path):
    one = router.endpoint_path("test", tmp_path / "one")
    two = router.endpoint_path("test", tmp_path / "two")
    assert one != two
    assert one.startswith("\\\\.\\pipe\\") and two.startswith("\\\\.\\pipe\\")


def test_codex_queue_fallback_without_unix_streams(monkeypatch):
    spec = importlib.util.spec_from_file_location(
        "codex_windows_adapter", Path(__file__).resolve().parents[1] / "unimatrix-peer-codex.py")
    codex = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(codex)
    monkeypatch.delattr(asyncio, "open_unix_connection", raising=False)
    calls = []

    async def queue(thread, text, timeout):
        calls.append((thread, text, timeout))
        return "queued reply"

    monkeypatch.setattr(codex, "queue_turn", queue)
    assert asyncio.run(codex.codex_turn("unused", "thread", "message", timeout=2)) == "queued reply"
    assert calls == [("thread", "message", 2)]


@pytest.mark.skipif(os.name != "nt", reason="Windows named-pipe client PID authentication")
def test_outbound_frame_is_written_by_its_registered_face(tmp_path):
    import win32pipe

    async def run():
        seen = []
        client_pids = []

        class Receiver(router.PipeServer):
            def _read(self, handle):
                import win32file
                import pywintypes
                try:
                    client_pids.append(win32pipe.GetNamedPipeClientProcessId(handle))
                    raw = bytearray()
                    while raw.count(b"\n") < 2:
                        raw.extend(win32file.ReadFile(handle, 65536)[1])
                    auth, frame = raw.splitlines()[:2]
                    if json.loads(auth) == {"type": "auth", "token": "recipient-test-token"}:
                        seen.append(json.loads(frame))
                except pywintypes.error:
                    pass
                finally:
                    handle.Close()

        async def receive(reader, writer):
            seen.append(json.loads(await reader.readline()))
            writer.close()

        async def answer(peer, text):
            return "reply from adapter"

        r = router.Router([{"name": "codex-test", "vendor": "codex", "target": "test"}],
                          sessions_dir=tmp_path / "sessions", state_dir=tmp_path / "state",
                          adapters={"codex": answer}, endpoint_dir=tmp_path / "endpoints")
        destination = router.endpoint_path("receiver", tmp_path / "endpoints")
        receiver = Receiver(destination, receive)
        try:
            await r.start()
            target = tmp_path / "sessions" / "receiver.json"
            target.write_text(json.dumps({"pid": os.getpid(), "name": "claude-receiver",
                                          "messagingSocketPath": destination}))
            key = tmp_path / "sessions" / router._roster().key_name(os.getpid(), destination)
            key.write_text(json.dumps({"peerToken": "recipient-test-token", "procStart": "test",
                                       "pidDomain": "win32:test"}))
            face_pid = r.faces["codex-test"].pid
            registration = json.loads((tmp_path / "sessions" / f"{face_pid}.json").read_text())
            face_key = tmp_path / "sessions" / router._roster().key_name(face_pid, r.endpoint_of("codex-test"))
            face_credentials = json.loads(face_key.read_text())
            import re
            assert re.fullmatch(r"[0-9a-f]{32}", face_credentials["peerToken"])
            assert face_credentials["procStartFt"] == registration["procStart"]
            assert "procStart" not in face_credentials
            assert face_credentials["pidDomain"] == registration["pidDomain"]
            response = await r._peer_send_to_session("codex-test", "claude-receiver", "direct message")
            assert response["ok"]
            inbound = router._contract().build_frame(destination, "claude-receiver", "question")
            await r.handle_inbound("codex-test", inbound)
            for _ in range(100):
                if len(seen) == 2:
                    break
                await asyncio.sleep(0.01)
            assert len(seen) == 2
            assert client_pids == [r.faces["codex-test"].pid] * 2
            assert all(frame["from"] == "uds:" + r.endpoint_of("codex-test") for frame in seen)
            assert "direct message" in seen[0]["message"]["content"]
            assert "reply from adapter" in seen[1]["message"]["content"]
        finally:
            receiver.close()
            await r.stop()
    asyncio.run(run())
