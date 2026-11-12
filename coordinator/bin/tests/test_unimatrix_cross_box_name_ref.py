"""A cross-box send to a bare name or name+ref reaches only the named session, never the G-EM, a decoy or a raw frame endpoint.

Hermetic: tmp_path sessions dir, fixture endpoints (pipe and unix-socket forms), delivery monkeypatched.
Hops pinned: Router.resolve_sender, _transport_send_to_session, _peer_send_to_session, handle_inbound,
unimatrix-send.py main and unimatrix-relay.py _session_path.
"""
import asyncio
import importlib.util
import json
import os
import time
from pathlib import Path

import pytest

BIN = Path(__file__).resolve().parents[1]


def _load(filename, modname):
    spec = importlib.util.spec_from_file_location(modname, BIN / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


router = _load("unimatrix-peer-router.py", "cross_box_name_ref_router")
send_cli = _load("unimatrix-send.py", "cross_box_name_ref_send")
relay = _load("unimatrix-relay.py", "cross_box_name_ref_relay")

STYLES = {
    "pipe": {"gem": r"\\.\pipe\LOCAL\cc-msg-gem", "far": r"\\.\pipe\LOCAL\cc-msg-far",
             "far_gem": r"\\.\pipe\LOCAL\cc-msg-fargem", "decoy": r"\\.\pipe\LOCAL\cc-msg-decoy",
             "frame": "uds:/tmp/cc-socks/1986.sock"},
    "unix": {"gem": "/tmp/cc-test/gem.sock", "far": "/tmp/cc-test/far.sock",
             "far_gem": "/tmp/cc-test/fargem.sock", "decoy": "/tmp/cc-test/decoy.sock",
             "frame": "/tmp/cc-socks/1986.sock"},
}
GEM, TARGET, FAR_GEM, DECOY = "group-em-ad", "example-game-repo-19", "group-em-ad.mini", "claude-klabauter-c6"
REF = "[d92c0d]"
MIRROR = {"unimatrixRemote": "mini", "macSocketPath": "/far/1.sock", "farPid": 7, "box": "mini"}


@pytest.fixture(params=sorted(STYLES))
def eps(request):
    return STYLES[request.param]


def write_session(directory, filename, name, endpoint, **extra):
    directory.mkdir(parents=True, exist_ok=True)
    (directory / filename).write_text(json.dumps(
        {"pid": os.getpid(), "name": name, "messagingSocketPath": endpoint, **extra}), encoding="utf-8")


def roster(tmp_path, eps, with_far_gem=False):
    """A live local G-EM, the far mirror of the named target, a decoy, and optionally the far box's own group-em-ad."""
    sessions = tmp_path / "sessions"
    write_session(sessions, "gem.json", GEM, eps["gem"])
    write_session(sessions, "target.json", f"{TARGET}.mini", eps["far"], unimatrixFarName=TARGET, **MIRROR)
    write_session(sessions, "decoy.json", DECOY, eps["decoy"])
    if with_far_gem:
        write_session(sessions, "fargem.json", FAR_GEM, eps["far_gem"], unimatrixFarName=GEM, **MIRROR)
    return sessions


def make_router(tmp_path, sessions, peers=()):
    return router.Router(list(peers), sessions_dir=sessions, state_dir=tmp_path / "state",
                         endpoint_dir=tmp_path / "eps")


def capture(r, monkeypatch):
    """Record every (sender, endpoint) the router would deliver to; nothing is sent."""
    sent = []

    async def deliver(peer, endpoint, frame):
        sent.append((peer, endpoint))

    async def face(source, destination):
        return "transport-face"
    monkeypatch.setattr(r, "_deliver_as_peer", deliver)
    monkeypatch.setattr(r, "_transport_face", face)
    return sent


def dispatch(r, to, text="hello"):
    return asyncio.run(r._dispatch({"op": "send", "as": "cli", "to": to, "text": text}))


def test_bare_far_name_resolves_to_its_own_mirror_only(tmp_path, eps):
    r = make_router(tmp_path, roster(tmp_path, eps))
    assert r.resolve_sender(TARGET) == eps["far"]
    assert r.resolve_sender(f"{TARGET}.mini") == eps["far"]


@pytest.mark.parametrize("name", [f"{GEM} {REF}", f"{TARGET} {REF}", f"{GEM}{REF}", REF])
def test_name_plus_ref_never_resolves_to_another_session(tmp_path, eps, name):
    r = make_router(tmp_path, roster(tmp_path, eps, with_far_gem=True))
    assert r.resolve_sender(name, "") == ""
    assert r.resolve_sender(name) not in {eps["gem"], eps["decoy"], eps["far"], eps["far_gem"]}


@pytest.mark.parametrize("name", [f"{GEM} {REF}", f"{TARGET} {REF}", "no-such-session"])
def test_unresolved_send_delivers_nothing_and_errors_by_name(tmp_path, eps, monkeypatch, name):
    r = make_router(tmp_path, roster(tmp_path, eps))
    sent = capture(r, monkeypatch)
    response = dispatch(r, name)
    assert not response["ok"] and name in response["error"]
    assert sent == []


def test_bare_name_carried_by_local_gem_and_far_mirror_is_refused_not_won_by_the_gem(tmp_path, eps):
    r = make_router(tmp_path, roster(tmp_path, eps, with_far_gem=True))
    assert r.resolve_sender(GEM) == ""


def test_transport_send_to_a_name_two_live_sessions_carry_never_reaches_the_gem(tmp_path, eps, monkeypatch):
    r = make_router(tmp_path, roster(tmp_path, eps, with_far_gem=True))
    sent = capture(r, monkeypatch)
    response = dispatch(r, GEM)
    assert not response["ok"] and sent == []


def test_peer_send_to_a_name_two_live_sessions_carry_never_reaches_the_gem(tmp_path, eps, monkeypatch):
    peer = {"name": "agy-a", "vendor": "agy", "target": "conv-1"}
    r = make_router(tmp_path, roster(tmp_path, eps, with_far_gem=True), peers=[peer])
    sent = capture(r, monkeypatch)
    response = asyncio.run(r._peer_send_to_session("agy-a", GEM, "hello"))
    assert not response["ok"] and sent == []


def test_peer_send_to_a_bare_far_name_reaches_only_that_mirror(tmp_path, eps, monkeypatch):
    peer = {"name": "agy-a", "vendor": "agy", "target": "conv-1"}
    r = make_router(tmp_path, roster(tmp_path, eps), peers=[peer])
    sent = capture(r, monkeypatch)
    # Off Windows relay_route refuses a pipe endpoint by design; this test pins name resolution, not relaying.
    monkeypatch.setattr(router, "_off_windows", lambda: False)
    assert asyncio.run(r._peer_send_to_session("agy-a", TARGET, "hello"))["ok"]
    assert sent == [("agy-a", eps["far"])]


def reply_endpoints(r, monkeypatch, sender_name, frame_endpoint):
    """Run handle_inbound for a peer whose frame names `sender_name` at `frame_endpoint`; return the delivery endpoints."""
    sent = capture(r, monkeypatch)

    async def turn(peer, text, addressed=None):
        return "answer"
    monkeypatch.setattr(r, "turn", turn)
    frame = router._contract().build_frame(frame_endpoint, sender_name, "question")
    asyncio.run(r.handle_inbound("agy-a", frame))
    return [endpoint for _, endpoint in sent]


def test_reply_to_a_far_sender_sharing_a_local_session_name_never_lands_on_that_local_session(tmp_path, eps, monkeypatch):
    peer = {"name": "agy-a", "vendor": "agy", "target": "conv-1"}
    r = make_router(tmp_path, roster(tmp_path, eps), peers=[peer])
    endpoints = reply_endpoints(r, monkeypatch, GEM, eps["frame"])
    assert eps["gem"] not in endpoints and eps["decoy"] not in endpoints


def test_reply_to_a_sender_with_no_roster_row_goes_to_its_own_frame_endpoint(tmp_path, eps, monkeypatch):
    peer = {"name": "agy-a", "vendor": "agy", "target": "conv-1"}
    r = make_router(tmp_path, roster(tmp_path, eps), peers=[peer])
    assert reply_endpoints(r, monkeypatch, "cli-sender", eps["frame"]) == [eps["frame"]]


class RouterOverDispatch:
    """The router module as unimatrix-send.py sees it, with request_line answered by an in-process router."""

    def __init__(self, r):
        self._r = r

    def __getattr__(self, attr):
        return getattr(router, attr)

    def request_line(self, ctl, req, *a):
        return asyncio.run(self._r._dispatch(req))


@pytest.mark.parametrize("to", [f"{GEM} {REF}", f"{TARGET} {REF}", "no-such-session"])
def test_send_cli_exits_nonzero_naming_an_unresolved_target(tmp_path, eps, monkeypatch, capsys, to):
    r = make_router(tmp_path, roster(tmp_path, eps))
    sent = capture(r, monkeypatch)
    monkeypatch.setattr(send_cli, "_router", lambda: RouterOverDispatch(r))
    assert send_cli.main(["--to", to, "--endpoint-dir", str(tmp_path / "eps"), "hello"]) == 1
    assert to in capsys.readouterr().err and sent == []


def test_send_cli_to_a_name_two_live_sessions_carry_exits_nonzero_and_delivers_nothing(tmp_path, eps, monkeypatch, capsys):
    r = make_router(tmp_path, roster(tmp_path, eps, with_far_gem=True))
    sent = capture(r, monkeypatch)
    monkeypatch.setattr(send_cli, "_router", lambda: RouterOverDispatch(r))
    assert send_cli.main(["--to", GEM, "--endpoint-dir", str(tmp_path / "eps"), "hello"]) == 1
    assert GEM in capsys.readouterr().err and sent == []


def relay_entries(eps, monkeypatch, rows):
    monkeypatch.setattr(relay, "entries", lambda: iter((pid, d, f"{pid}.key") for pid, d in rows))


def test_far_relay_refuses_a_pid_that_no_longer_holds_the_addressed_session(eps, monkeypatch):
    relay_entries(eps, monkeypatch, [(11, {"name": DECOY, "sessionId": "sid-decoy", "messagingSocketPath": eps["decoy"]})])
    with pytest.raises(SystemExit) as refused:
        relay._session_path(11, expected_name_digest=relay.name_digest(GEM))
    assert str(refused.value).startswith("target mismatch")
    with pytest.raises(SystemExit) as refused:
        relay._session_path(11, expected_session_id="sid-gem")
    assert str(refused.value).startswith("target mismatch")


def test_far_relay_refuses_a_name_two_live_far_sessions_carry(eps, monkeypatch):
    rows = [(11, {"name": GEM, "sessionId": "sid-1", "messagingSocketPath": eps["gem"]}),
            (12, {"name": GEM, "sessionId": "sid-2", "messagingSocketPath": eps["far_gem"]})]
    relay_entries(eps, monkeypatch, rows)
    with pytest.raises(SystemExit) as refused:
        relay._session_path(11, expected_name_digest=relay.name_digest(GEM))
    assert str(refused.value).startswith("ambiguous")


def home_roster(tmp_path, eps, monkeypatch):
    """The roster at the home-relative sessions dir `unimatrix-send.py :: _is_mirror` reads, plus a failed far-relay record for the mirror."""
    home = tmp_path / "home"
    for var in ("HOME", "USERPROFILE"):
        monkeypatch.setenv(var, str(home))
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path / "settings"))
    sessions = roster(home / ".claude", eps)
    deliveries = tmp_path / "settings" / "unimatrix" / "deliveries.jsonl"
    deliveries.parent.mkdir(parents=True)
    deliveries.write_text(json.dumps({"at": time.time() + 30, "ok": False, "error": "relay: far refused",
                                      "target": f"{TARGET}.mini (pid 7, session sid-far)"}) + "\n", encoding="utf-8")
    return sessions


@pytest.mark.parametrize("to", [f"{TARGET}.mini", TARGET])
def test_send_cli_surfaces_a_far_relay_failure_for_the_mirror_by_full_or_bare_name(tmp_path, eps, monkeypatch, capsys, to):
    r = make_router(tmp_path, home_roster(tmp_path, eps, monkeypatch))
    capture(r, monkeypatch)
    monkeypatch.setattr(send_cli, "_router", lambda: RouterOverDispatch(r))
    assert send_cli.main(["--as", "cli", "--to", to, "--endpoint-dir", str(tmp_path / "eps"), "hello"]) == 1
    captured = capsys.readouterr()
    assert captured.err.startswith(f"undelivered: {to}:") and "sent to" not in captured.out


def test_pipe_endpoint_off_windows_with_no_relay_route_is_refused_never_opened(tmp_path, monkeypatch):
    far = STYLES["pipe"]
    r = make_router(tmp_path, roster(tmp_path, far))
    sent = capture(r, monkeypatch)
    monkeypatch.setattr(router, "_off_windows", lambda: True)
    response = dispatch(r, TARGET)
    assert not response["ok"] and "no relay route" in response["error"]
    assert sent == []
