"""Claude/anonymous CLI callers can reach mirrored peers without claiming a vendor identity."""
import asyncio
import importlib.util
import json
import os
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "cli_transport_router", Path(__file__).resolve().parents[1] / "unimatrix-peer-router.py")
router = importlib.util.module_from_spec(spec)
spec.loader.exec_module(router)


def write_session(directory, filename, name, endpoint, **extra):
    directory.mkdir(parents=True, exist_ok=True)
    entry = {"pid": os.getpid(), "name": name, "messagingSocketPath": endpoint, **extra}
    (directory / filename).write_text(json.dumps(entry))
    key = directory / router._roster().key_name(os.getpid(), endpoint)
    key.write_text(json.dumps({"peerToken": "a" * 32}))


@pytest.mark.skipif(os.name != "nt", reason="Real Windows pipes and registered transport child")
@pytest.mark.parametrize("source", ["cli", "group-em-09"])
def test_cli_with_no_vendor_peers_sends_to_mac_mirror_and_returns_named_replies(tmp_path, source):
    import win32pipe

    async def run():
        received, returned, client_pids = [], [], []

        class Receiver(router.PipeServer):
            def _read(self, handle):
                client_pids.append(win32pipe.GetNamedPipeClientProcessId(handle))
                super()._read(handle)

        async def receive(reader, writer):
            received.append(json.loads(await reader.readline()))
            writer.close()

        async def receive_return(reader, writer):
            returned.append(json.loads(await reader.readline()))
            writer.close()

        sessions = tmp_path / "sessions"
        target = router.endpoint_path("mac-mirror", tmp_path)
        caller = router.endpoint_path("local-caller", tmp_path)
        write_session(sessions, "mirror.json", "mac-peer", target,
                      macSocketPath="/tmp/cc-socks/mac-peer.sock", farPid=80954)
        if source != "cli":
            write_session(sessions, "caller.json", source, caller)
        r = router.Router([], sessions_dir=sessions, state_dir=tmp_path / "state", endpoint_dir=tmp_path)
        receiver, return_receiver = Receiver(target, receive), Receiver(caller, receive_return)
        try:
            await r.start()
            reply = await asyncio.to_thread(router.request_line, r.ctl,
                {"op": "send", "as": source, "to": "mac-peer", "text": "coordinate across boxes"})
            assert reply["ok"], reply
            for _ in range(100):
                if received:
                    break
                await asyncio.sleep(0.01)
            assert len(received) == 1
            transport = reply["sender"]
            assert r.peers == {} and transport in r.faces
            assert transport not in r.session_names()
            registration = json.loads((sessions / f"{r.faces[transport].pid}.json").read_text())
            assert registration["unimatrixTransport"] is True and registration["vendor"] == "transport"
            assert client_pids[0] == r.faces[transport].pid
            assert received[0]["from"] == "uds:" + r.endpoint_of(transport)
            assert received[0]["provenance"]["vendor"] == "transport"
            assert source in received[0]["message"]["content"]
            if source != "cli":
                frame = router._contract().build_frame(target, source, "coordination reply")
                await asyncio.to_thread(router.deliver, r.endpoint_of(transport),
                                        (json.dumps(frame) + "\n").encode(), sessions)
                for _ in range(100):
                    if returned:
                        break
                    await asyncio.sleep(0.01)
                assert len(returned) == 1
                assert "coordination reply" in returned[0]["message"]["content"]
                assert client_pids[-1] == r.faces[transport].pid
                # A native Reply from the original caller must travel back to Mac,
                # rather than being reflected into the caller's own conversation.
                response_frame = router._contract().build_frame(caller, source, "follow-up for Mac")
                await asyncio.to_thread(router.deliver, r.endpoint_of(transport),
                                        (json.dumps(response_frame) + "\n").encode(), sessions)
                for _ in range(100):
                    if len(received) == 2:
                        break
                    await asyncio.sleep(0.01)
                assert len(received) == 2 and len(returned) == 1
                assert "follow-up for Mac" in received[-1]["message"]["content"]
        finally:
            receiver.close()
            return_receiver.close()
            await r.stop()
    asyncio.run(run())


def test_ambiguous_names_are_refused_and_old_far_aliases_resolve_only_when_unique(tmp_path):
    write_session(tmp_path, "one.json", "readable-mac-peer", "one", unimatrixFarName="old-opaque-peer")
    r = router.Router([], sessions_dir=tmp_path, state_dir=tmp_path / "state")
    assert r.resolve_sender("old-opaque-peer") == "one"
    write_session(tmp_path, "two.json", "other-readable-peer", "two", unimatrixFarName="old-opaque-peer")
    assert r.resolve_sender("old-opaque-peer", "fallback") == ""
    response = asyncio.run(r._dispatch({"op": "send", "as": "cli", "to": "old-opaque-peer", "text": "message"}))
    assert not response["ok"] and "unique" in response["error"]
    assert r.faces == {}



def test_cli_explicit_as_overrides_inherited_codex_target(monkeypatch):
    from types import SimpleNamespace
    spec = importlib.util.spec_from_file_location(
        "cli_send_override", Path(__file__).resolve().parents[1] / "unimatrix-send.py")
    send = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(send)
    requests = []
    fake = SimpleNamespace(
        ROUTER_ENDPOINT_SEED="seed", endpoint_path=lambda *a: "ctl",
        request_line=lambda endpoint, request: requests.append(request) or {"ok": True})
    monkeypatch.setattr(send, "_router", lambda: fake)
    monkeypatch.setenv("CODEX_THREAD_ID", "inherited-thread")
    monkeypatch.delenv("UNIMATRIX_PEER_NAME", raising=False)
    assert send.main(["--as", "group-em-09", "--to", "mac-peer", "message"]) == 0
    assert requests[-1]["as"] == "group-em-09" and requests[-1]["as_target"] == ""
    assert send.main(["--to", "mac-peer", "message"]) == 0
    assert requests[-1]["as_target"] == "inherited-thread"
