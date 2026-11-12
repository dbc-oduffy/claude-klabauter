"""A non-Windows sender's named send to a Windows-box session goes through the cross-box relay mirror by session id.

Hermetic: tmp_path sessions dir, `_off_windows` / `_windows` forced to a Mac, delivery monkeypatched. The
Windows pipe address is never opened off Windows, and no frame reaches the G-EM or a decoy.
"""
import asyncio
import importlib.util
import json
import os
from pathlib import Path

import pytest

BIN = Path(__file__).resolve().parents[1]


def _load(filename, modname):
    spec = importlib.util.spec_from_file_location(modname, BIN / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


router = _load("unimatrix-peer-router.py", "mac_to_windows_router")
send_cli = _load("unimatrix-send.py", "mac_to_windows_send")

GEM, TARGET, DECOY = "group-em-56", "machine-a-addon-83", "claude-klabauter-c6"
SID = "sid-machine-a-83"
PIPE = r"\\.\pipe\LOCAL\cc-msg-machine-a-83"
MIRROR_EP = "/tmp/cc-mac/mirror-machine-a-83.sock"
GEM_EP, DECOY_EP = "/tmp/cc-mac/gem.sock", "/tmp/cc-mac/decoy.sock"


def write_session(directory, filename, name, endpoint, **extra):
    directory.mkdir(parents=True, exist_ok=True)
    (directory / filename).write_text(json.dumps(
        {"pid": os.getpid(), "name": name, "messagingSocketPath": endpoint, **extra}), encoding="utf-8")


def roster(base, with_mirror=True):
    """Live G-EM, a decoy, the Windows row of the target (pipe endpoint) and optionally its local bridge mirror."""
    sessions = base / "sessions"
    write_session(sessions, "gem.json", GEM, GEM_EP, sessionId="sid-gem")
    write_session(sessions, "decoy.json", DECOY, DECOY_EP, sessionId="sid-decoy")
    write_session(sessions, "target.json", TARGET, PIPE, sessionId=SID)
    if with_mirror:
        write_session(sessions, "mirror.json", "machine-a-mirror-1", MIRROR_EP, sessionId=SID,
                      unimatrixRemote="machine-a", unimatrixFarName="machine-a-far-1", macSocketPath="/far/1.sock",
                      farPid=7, box="machine-a")
    return sessions


@pytest.fixture
def mac(monkeypatch):
    monkeypatch.setattr(router, "_off_windows", lambda: True)
    monkeypatch.setattr(send_cli, "_windows", lambda: False)


def make_router(tmp_path, sessions):
    r = router.Router([], sessions_dir=sessions, state_dir=tmp_path / "state", endpoint_dir=tmp_path / "eps")
    sent = []

    async def deliver(peer, endpoint, frame):
        sent.append(endpoint)

    async def face(source, destination):
        return "transport-face"
    r._deliver_as_peer, r._transport_face = deliver, face
    return r, sent


def dispatch(r, to):
    return asyncio.run(r._dispatch({"op": "send", "as": "cli", "to": to, "text": "hello"}))


class RouterOverDispatch:
    def __init__(self, r):
        self._r = r

    def __getattr__(self, attr):
        return getattr(router, attr)

    def request_line(self, ctl, req, *a):
        return asyncio.run(self._r._dispatch(req))


def test_mac_named_send_to_a_windows_row_goes_through_the_relay_mirror_only(tmp_path, mac):
    r, sent = make_router(tmp_path, roster(tmp_path))
    response = dispatch(r, TARGET)
    assert response["ok"] and response["written"] is True
    assert sent == [MIRROR_EP]


def test_mac_peer_send_to_a_windows_row_goes_through_the_relay_mirror_only(tmp_path, mac):
    r, sent = make_router(tmp_path, roster(tmp_path))
    r.peers["agy-a"] = {"name": "agy-a", "vendor": "agy", "target": "conv-1"}
    assert asyncio.run(r._peer_send_to_session("agy-a", TARGET, "hello"))["ok"]
    assert sent == [MIRROR_EP]


def test_mac_send_to_a_windows_row_without_a_relay_route_is_refused_and_opens_nothing(tmp_path, mac, monkeypatch):
    r, sent = make_router(tmp_path, roster(tmp_path, with_mirror=False))
    opened = []
    monkeypatch.setattr(router, "request_line", lambda *a, **k: opened.append(a) or {"ok": True})
    response = dispatch(r, TARGET)
    assert not response["ok"] and "no relay route" in response["error"]
    assert sent == [] and opened == []


@pytest.mark.parametrize("to", [f"{TARGET} [d92c0d]", "[d92c0d]", GEM + "-x"])
def test_mac_name_plus_ref_or_unknown_name_never_reaches_the_gem_or_the_relay(tmp_path, mac, to):
    r, sent = make_router(tmp_path, roster(tmp_path))
    response = dispatch(r, to)
    assert not response["ok"] and sent == []


def test_windows_sender_keeps_the_pipe_endpoint(tmp_path, monkeypatch):
    monkeypatch.setattr(router, "_off_windows", lambda: False)
    r, sent = make_router(tmp_path, roster(tmp_path, with_mirror=False))
    assert dispatch(r, TARGET)["ok"]
    assert sent == [PIPE]


def test_send_cli_on_a_mac_reports_sent_via_the_mirror_and_exit_1_without_a_route(tmp_path, mac, monkeypatch, capsys):
    monkeypatch.setattr(send_cli, "_session_entries", lambda: [])
    for with_mirror, code in ((True, 0), (False, 1)):
        r, sent = make_router(tmp_path / str(with_mirror), roster(tmp_path / str(with_mirror), with_mirror))
        monkeypatch.setattr(send_cli, "_router", lambda r=r: RouterOverDispatch(r))
        argv = ["--as", "cli", "--to", TARGET, "--endpoint-dir", str(tmp_path / "eps"), "hello"]
        assert send_cli.main(argv) == code
        err = capsys.readouterr().err
        if with_mirror:
            assert sent == [MIRROR_EP] and not err
        else:
            assert sent == [] and err.startswith(f"undelivered: {TARGET}:")
