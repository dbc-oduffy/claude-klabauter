"""A Mac send to a Machine-a mirror by bare name or `name [ref]` reaches only that mirror, never the claude-klabauter-a1 mirror.

Hermetic: tmp_path sessions dir holding rows shaped like the live Mac sessions dir (Machine-a mirrors carry
name == unimatrixFarName, a unix mirror socket, a far pipe in macSocketPath, farPid and unimatrixRemote;
the decoy is a second Machine-a mirror), delivery monkeypatched.
Hops pinned: Router.resolve_sender, _transport_send_to_session, unimatrix-send.py main.
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


router = _load("unimatrix-peer-router.py", "mac_to_machine_a_router")
send_cli = _load("unimatrix-send.py", "mac_to_machine_a_send")

TARGET, DECOY = "example-retrieval-repo-ue-addon-a1", "claude-klabauter-a1"
REF = "[b9ae48]"
ENDPOINT_STYLES = {
    "pipe-far": {"target": r"\\.\pipe\LOCAL\cc-msg-8060", "decoy": r"\\.\pipe\LOCAL\cc-msg-3a8d"},
    "unix-far": {"target": "/far/target.sock", "decoy": "/far/decoy.sock"},
}


@pytest.fixture(params=sorted(ENDPOINT_STYLES))
def far(request):
    return ENDPOINT_STYLES[request.param]


def mirror(sessions, filename, name, mirror_socket, far_endpoint, far_pid):
    sessions.mkdir(parents=True, exist_ok=True)
    (sessions / filename).write_text(json.dumps({
        "pid": os.getpid(), "name": name, "unimatrixFarName": name, "messagingSocketPath": mirror_socket,
        "macSocketPath": far_endpoint, "farPid": far_pid, "unimatrixRemote": "machine-a-lan", "box": "machine-a",
        "vendor": "claude", "sessionId": f"sid-{name}"}), encoding="utf-8")


def machine_a_roster(tmp_path, far):
    sessions = tmp_path / "sessions"
    mirror(sessions, "target.json", TARGET, "/tmp/cc-socks/cc-msg-target.sock", far["target"], 35484)
    mirror(sessions, "decoy.json", DECOY, "/tmp/cc-socks/cc-msg-decoy.sock", far["decoy"], 26920)
    return sessions


def make_router(tmp_path, sessions):
    return router.Router([], sessions_dir=sessions, state_dir=tmp_path / "state", endpoint_dir=tmp_path / "eps")


def capture(r, monkeypatch):
    sent = []

    async def deliver(peer, endpoint, frame):
        sent.append(endpoint)

    async def face(source, destination):
        return "transport-face"
    monkeypatch.setattr(r, "_deliver_as_peer", deliver)
    monkeypatch.setattr(r, "_transport_face", face)
    return sent


def dispatch(r, to):
    return asyncio.run(r._dispatch({"op": "send", "as": "cli", "to": to, "text": "hello"}))


def test_bare_name_delivers_to_the_named_machine_a_mirror_only(tmp_path, far, monkeypatch):
    r = make_router(tmp_path, machine_a_roster(tmp_path, far))
    sent = capture(r, monkeypatch)
    assert dispatch(r, TARGET)["ok"]
    assert sent == ["/tmp/cc-socks/cc-msg-target.sock"]


@pytest.mark.parametrize("to", [f"{TARGET} {REF}", f"{DECOY} {REF}", f"{TARGET}{REF}"])
def test_name_plus_ref_is_refused_naming_the_address_and_never_reaches_the_decoy(tmp_path, far, monkeypatch, to):
    r = make_router(tmp_path, machine_a_roster(tmp_path, far))
    sent = capture(r, monkeypatch)
    response = dispatch(r, to)
    assert not response["ok"] and to in response["error"]
    assert sent == []


class RouterOverDispatch:
    """The router module as unimatrix-send.py sees it, with request_line answered by an in-process router."""

    def __init__(self, r):
        self._r = r

    def __getattr__(self, attr):
        return getattr(router, attr)

    def request_line(self, ctl, req, *a):
        return asyncio.run(self._r._dispatch(req))


@pytest.mark.parametrize("to", [f"{TARGET} {REF}", f"{DECOY} {REF}"])
def test_send_cli_with_a_ref_exits_nonzero_naming_the_address_and_delivers_nothing(tmp_path, far, monkeypatch, capsys, to):
    r = make_router(tmp_path, machine_a_roster(tmp_path, far))
    sent = capture(r, monkeypatch)
    monkeypatch.setattr(send_cli, "_router", lambda: RouterOverDispatch(r))
    assert send_cli.main(["--to", to, "--endpoint-dir", str(tmp_path / "eps"), "hello"]) == 1
    assert to in capsys.readouterr().err and sent == []
