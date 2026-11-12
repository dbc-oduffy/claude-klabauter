"""A session's inbox leg and far registration deliver only to the session they name.

Observed: far senders on the Mini addressed `group-em-56` and `claude-klabauter-66`, and the
frames landed on example-retrieval-repo-ue-addon-1a's pipe `\\\\.\\pipe\\LOCAL\\cc-msg-d07d...`.

Which hop reproduces (hermetic, at HEAD):
  * `unimatrix-inbox.py :: main` is a per-session leg. It reads `{pid}.json` and writes every
    frame to its one `deliver_endpoint` without checking that the entry names `a.name` or holds
    that endpoint: a leg launched as `group-em-56` over addon-1a's entry delivers into addon-1a.
    RED here (the refusal tests).
  * `unimatrix-relay.py :: inbox` registers the pid entry under the leg's `name` through
    `mirror_entry`, which overwrites the entry's own `name`, hiding a mismatch. RED here.
  * A frame carries only `from`/`from-name`, never an addressee; the socket the far sender
    connected to is the addressee, so the leg cannot test a frame per message and a wrong
    outbound socket is a sender-side resolution defect. Pinned green (frame_has_no_addressee).
  * `unimatrix-mirror-all.py :: live_sessions` keys each session by its own `d["name"]`, so
    `inbox_cmd` never pairs one name with another's entry on the live path. Pinned green.
  * 6d006c33af's guards (`relay_send` addressed != far_name; peer-router `_far_alias_is_stale`)
    sit on the outbound bridge and peer-router hops; none runs in the inbox leg or the relay's
    `inbox`, so they do not fire on any test below.
  * The far sender's name-to-socket resolution (`Router.resolve_sender`) never maps another name
    to addon-1a's socket, stale far alias included. 6d006c33af's `_far_alias_is_stale` covers this
    hop: the stale-alias test is red against the router at 6d006c33af^ and green at HEAD, so the
    sender tests at the end land without a router change.
"""
import importlib.util
import io
import json
import sys
from pathlib import Path

import pytest

BIN = Path(__file__).resolve().parents[1]


def _load(modname, filename):
    spec = importlib.util.spec_from_file_location(modname, BIN / filename)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[modname] = mod
    spec.loader.exec_module(mod)
    return mod


inbox_leg = _load("name_collapse_inbox_leg", "unimatrix-inbox.py")
relay = _load("name_collapse_relay", "unimatrix-relay.py")
mirror_all = _load("name_collapse_mirror_all", "unimatrix-mirror-all.py")

ADDON = "example-retrieval-repo-ue-addon-1a"
ADDON_PIPE = r"\\.\pipe\LOCAL\cc-msg-d07d9f0d4bd8ab47b5e3187d7b3bd0fb"
GROUP_EM = "group-em-56"
GROUP_EM_PIPE = r"\\.\pipe\LOCAL\cc-msg-8a74c379b5ea7ab9c5e159287ad5288b"
CLAUDE_KLABAUTER = "claude-klabauter-66"
PID = 4242
SENDERS = {GROUP_EM: "example-game-workbench-repo-78", CLAUDE_KLABAUTER: "example-retrieval-repo-ue-addon-83"}


def _frame(sender, body="hello"):
    content = f'<cross-session-message from="uds:/tmp/cc-socks/{sender}.sock" from-name="{sender}">\n{body}\n</cross-session-message>'
    return {"from": f"uds:/tmp/cc-socks/{sender}.sock", "message": {"content": content}}


class _FakeProc:
    def __init__(self, frames):
        self.pid = 1
        self.stdout = [json.dumps(f).encode() + b"\n" for f in frames]
        self.stderr = []

    def wait(self):
        return 0


class _Harness:
    def __init__(self, tmp_path, monkeypatch):
        self.sessions = tmp_path / "sessions"
        self.sessions.mkdir()
        self.writes = []
        self.subscribed = []
        self.frames = []
        monkeypatch.setattr(inbox_leg, "SESSIONS", str(self.sessions))
        monkeypatch.setattr(inbox_leg, "write_local", lambda endpoint, data: self.writes.append((endpoint, data)))
        monkeypatch.setattr(inbox_leg, "pick_remote", lambda lan, tunnel: lan)
        harness = self

        class _Mux:
            def for_remote(self, remote, key=None):
                return self

            def subscribe(self, mode, payload):
                harness.subscribed.append(mode)
                return _FakeProc(harness.frames)

        monkeypatch.setattr(inbox_leg, "_box_mux", lambda: _Mux())
        self.log = tmp_path / "inbox.log"

    def register(self, entry_name, entry_endpoint):
        entry = {"pid": PID, "name": entry_name}
        if entry_endpoint is not None:
            entry["messagingSocketPath"] = entry_endpoint
        (self.sessions / f"{PID}.json").write_text(json.dumps(entry), encoding="utf-8")
        (self.sessions / f"{PID}.abc123.key").write_text("k", encoding="utf-8")

    def run(self, name, endpoint):
        argv = [str(PID), name, str(self.log), endpoint, "--remote", "u@mini", "--key", "k"]
        return inbox_leg.main(argv)


@pytest.fixture
def leg(tmp_path, monkeypatch):
    return _Harness(tmp_path, monkeypatch)


def test_leg_delivers_its_own_session_entry_to_its_own_endpoint(leg):
    """The accepted side: entry name and endpoint match the leg, frames are delivered."""
    leg.register(GROUP_EM, GROUP_EM_PIPE)
    leg.frames = [_frame(SENDERS[GROUP_EM])]
    leg.run(GROUP_EM, GROUP_EM_PIPE)
    assert [e for e, _ in leg.writes] == [GROUP_EM_PIPE]


@pytest.mark.parametrize("name", [GROUP_EM, CLAUDE_KLABAUTER])
def test_leg_launched_for_a_name_refuses_another_sessions_entry_and_endpoint(leg, name):
    """A leg paired with addon-1a's entry and pipe under another name must not deliver into addon-1a."""
    leg.register(ADDON, ADDON_PIPE)
    leg.frames = [_frame(SENDERS[name])]
    with pytest.raises(SystemExit) as refused:
        leg.run(name, ADDON_PIPE)
    assert refused.value.code not in (0, None)
    assert not leg.subscribed
    assert ADDON_PIPE not in [e for e, _ in leg.writes]


def test_leg_refuses_an_entry_naming_another_session_even_at_its_own_endpoint(leg):
    leg.register(ADDON, GROUP_EM_PIPE)
    leg.frames = [_frame(SENDERS[GROUP_EM])]
    with pytest.raises(SystemExit):
        leg.run(GROUP_EM, GROUP_EM_PIPE)
    assert not leg.writes and not leg.subscribed


def test_leg_refuses_an_entry_whose_endpoint_is_not_the_deliver_endpoint(leg):
    leg.register(GROUP_EM, ADDON_PIPE)
    leg.frames = [_frame(SENDERS[GROUP_EM])]
    with pytest.raises(SystemExit):
        leg.run(GROUP_EM, GROUP_EM_PIPE)
    assert not leg.writes and not leg.subscribed


def test_leg_refuses_an_entry_holding_no_endpoint(leg):
    leg.register(GROUP_EM, None)
    leg.frames = [_frame(SENDERS[GROUP_EM])]
    with pytest.raises(SystemExit):
        leg.run(GROUP_EM, GROUP_EM_PIPE)
    assert not leg.writes and not leg.subscribed


def test_frame_has_no_addressee_so_the_leg_cannot_filter_per_frame(leg):
    """The far relay forwards raw bytes accepted on the leg's socket; the frame names only its sender."""
    frames = [_frame(SENDERS[GROUP_EM]), _frame(SENDERS[CLAUDE_KLABAUTER])]
    assert all(set(f) == {"from", "message"} for f in frames)
    leg.register(ADDON, ADDON_PIPE)
    leg.frames = frames
    leg.run(ADDON, ADDON_PIPE)
    assert [e for e, _ in leg.writes] == [ADDON_PIPE, ADDON_PIPE]


def _stub_relay_inbox(monkeypatch, tmp_path, raw_entry):
    bound = []
    monkeypatch.setattr(relay, "endpoint_kind", lambda: "socket")
    monkeypatch.setattr(relay, "client_address", lambda: "client")
    monkeypatch.setattr(relay, "check_name_free", lambda name, client: None)
    monkeypatch.setattr(relay, "drop_stale", lambda x: None)
    monkeypatch.setattr(relay, "stale_marker_path", lambda name: str(tmp_path / f"stale-{name}"))
    monkeypatch.setattr(relay, "SOCKS", str(tmp_path / "socks"))
    monkeypatch.setattr(relay, "SESS", str(tmp_path / "sess"))
    (tmp_path / "sess").mkdir()
    monkeypatch.setattr(relay, "pid_domain", lambda: "dom")
    monkeypatch.setattr(relay, "load_roster", lambda: pytest.fail("registered a mismatched entry"))
    monkeypatch.setattr(relay, "serve", lambda *a: bound.append("served"))

    class _Sock:
        def __init__(self, *a):
            bound.append("socket")

        def bind(self, p):
            Path(p).write_text("", encoding="utf-8")

        def listen(self, n):
            pass

    class _Socket:
        AF_UNIX = 1
        socket = _Sock

    monkeypatch.setattr(relay, "socket", _Socket)
    monkeypatch.setattr(relay.os, "chmod", lambda *a: None)
    monkeypatch.setattr(relay.subprocess, "run",
                        lambda *a, **k: type("R", (), {"stdout": "Thu Nov 12 05:00:00 2026"})())
    monkeypatch.setattr(relay.sys, "stdin", io.StringIO(json.dumps(raw_entry) + "\nkey\n"))
    return bound


@pytest.mark.parametrize("name", [GROUP_EM, CLAUDE_KLABAUTER])
def test_relay_inbox_refuses_a_registration_naming_another_session(monkeypatch, tmp_path, name):
    """`mirror_entry` overwrites the entry's name today; a registration for another session must be refused."""
    bound = _stub_relay_inbox(monkeypatch, tmp_path, {"name": ADDON, "messagingSocketPath": ADDON_PIPE})
    with pytest.raises(SystemExit) as refused:
        relay.inbox(name)
    assert refused.value.code not in (0, None, relay.UNSUPPORTED_HOST_RC)
    assert "served" not in bound and "socket" not in bound
    assert not list((tmp_path / "sess").iterdir())


def test_mirror_all_pairs_each_name_with_its_own_entry(tmp_path, monkeypatch):
    """`live_sessions` keys by `d["name"]`, so `inbox_cmd` cannot pair one name with another's endpoint."""
    sess = tmp_path / "sess"
    sess.mkdir()
    pipes = {ADDON: ADDON_PIPE, GROUP_EM: GROUP_EM_PIPE}
    for pid, (name, pipe) in enumerate(pipes.items(), start=100):
        (sess / f"{pid}.json").write_text(
            json.dumps({"pid": pid, "name": name, "messagingSocketPath": pipe}), encoding="utf-8")
    roster = mirror_all._roster()
    monkeypatch.setattr(roster, "sessions_dir", lambda: str(sess))
    monkeypatch.setattr(roster, "pid_alive", lambda pid: True)
    live = mirror_all.live_sessions(skip=set())
    assert set(live) == set(pipes)
    for name, d in live.items():
        cmd = mirror_all.inbox_cmd(d, name, str(tmp_path), "u@mini", "k")
        assert cmd[3] == name and cmd[5] == pipes[name]


router = _load("name_collapse_router", "unimatrix-peer-router.py")
ADDON_SOCK = "/tmp/cc-socks/unimatrix-example-retrieval-repo-ue-addon-1a.sock"
GROUP_EM_SOCK = "/tmp/cc-socks/unimatrix-group-em-56.sock"
MIRROR = {"unimatrixMirror": True, "unimatrixRemote": "machine-a", "farPid": 7}


def _write_session(directory, filename, name, endpoint, **extra):
    """A live-pid roster row with its peer key, as the relay's `inbox` registers it."""
    (directory / filename).write_text(json.dumps(
        {"pid": __import__("os").getpid(), "name": name, "messagingSocketPath": endpoint, **extra}))
    (directory / router._roster().key_name(__import__("os").getpid(), endpoint)).write_text(
        json.dumps({"peerToken": "a" * 32}))


@pytest.mark.parametrize("name", [GROUP_EM, CLAUDE_KLABAUTER])
def test_sender_resolving_another_name_never_connects_to_addon_socket(tmp_path, name):
    """The addressee is the socket the far sender connects to: `name` must never resolve to addon-1a's."""
    _write_session(tmp_path, "a.json", ADDON, ADDON_SOCK, **MIRROR)
    r = router.Router([], sessions_dir=tmp_path, state_dir=tmp_path / "state")
    assert r.resolve_sender(name, "") == ""
    assert r.resolve_sender(name, "fallback") == "fallback"
    _write_session(tmp_path, "b.json", name, GROUP_EM_SOCK, **MIRROR)
    assert r.resolve_sender(name, "") == GROUP_EM_SOCK
    assert r.resolve_sender(ADDON, "") == ADDON_SOCK


@pytest.mark.parametrize("name", [GROUP_EM, CLAUDE_KLABAUTER])
def test_stale_far_alias_on_addon_socket_is_refused_not_resolved(tmp_path, name):
    """addon-1a's row carrying another session's far name must resolve that name to nothing."""
    _write_session(tmp_path, "a.json", ADDON, ADDON_SOCK, unimatrixFarName=name, **MIRROR)
    r = router.Router([], sessions_dir=tmp_path, state_dir=tmp_path / "state")
    assert r.resolve_sender(name, "") == ""
    assert r.resolve_sender(name, "fallback") == ""


FAR_ENDPOINTS = {GROUP_EM: GROUP_EM_SOCK, CLAUDE_KLABAUTER: "/tmp/cc-socks/unimatrix-claude-klabauter-66.sock",
                 ADDON: ADDON_SOCK}


def _mini_view(tmp_path, stale_for=None):
    """The Mini's roster: group-em-56, claude-klabauter-66 and addon-1a all far names bridged to Machine-a.

    `stale_for` adds a second addon-1a-derived mirror row carrying that other session's far name.
    """
    for i, (far, sock) in enumerate(FAR_ENDPOINTS.items()):
        _write_session(tmp_path, f"m{i}.json", f"{far}.machine-a", sock, unimatrixFarName=far, **MIRROR)
    if stale_for:
        _write_session(tmp_path, "stale.json", f"{ADDON}.machine-a", "/tmp/cc-socks/stale.sock",
                       unimatrixFarName=stale_for, **MIRROR)
    return router.Router([], sessions_dir=tmp_path, state_dir=tmp_path / "state")


@pytest.mark.parametrize("name", [GROUP_EM, CLAUDE_KLABAUTER, ADDON])
def test_mini_view_each_far_name_resolves_to_its_own_endpoint(tmp_path, name):
    r = _mini_view(tmp_path)
    assert r.resolve_sender(name, "") == FAR_ENDPOINTS[name]
    assert r.resolve_sender(f"{name}.machine-a", "") == FAR_ENDPOINTS[name]


@pytest.mark.parametrize("name", [GROUP_EM, CLAUDE_KLABAUTER])
def test_mini_view_stale_addon_row_under_another_far_name_fails_closed(tmp_path, name):
    """An addon-1a-derived mirror carrying `name` as its far name never lends addon-1a's endpoint to `name`."""
    r = _mini_view(tmp_path, stale_for=name)
    for resolved in (r.resolve_sender(name, ""), r.resolve_sender(name, "fallback")):
        assert resolved != "/tmp/cc-socks/stale.sock" and resolved != ADDON_SOCK
    assert r.resolve_sender(name, "") == ""
    assert r.resolve_sender(ADDON, "") == ADDON_SOCK
