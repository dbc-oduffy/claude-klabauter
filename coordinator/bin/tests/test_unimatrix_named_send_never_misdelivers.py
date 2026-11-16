"""A named cross-box send reaches only the live session named X, else the bridge records an error naming the cause.

Two-box, hermetic, default path: unimatrix-send.py --to X -> router -> near mirror socket -> bridge
relay_send -> `send-session` run in-process against the far relay's real parse_mode, send_batch,
_session_path and _confirm_server over a tmp_path registry. Only the OS edges are faked: the far
registry rows, which process serves each endpoint, and the endpoint write that lands in an inbox.
The far peer router is taken as absent, so the relay's local checks alone must refuse.
"""
import asyncio
import contextlib
import importlib.util
import io
import json
import os
import subprocess
import sys
import types
from pathlib import Path

import pytest

BIN = Path(__file__).resolve().parents[1]
X, Y = "group-em-54", "example-game-repo-fc"
SID_X = "38d698f0-0000-4000-8000-000000000001"
SID_X_STALE = "639e3e00-0000-4000-8000-000000000002"
SID_Y = "fc000000-0000-4000-8000-000000000003"
PID_X, PID_Y, PID_MUX = 100, 200, 300
REMOTE = "machine-a-lan"
FAR_STYLES = {
    "pipe-far": {"kind": "pipe", "X": r"\\.\pipe\LOCAL\cc-msg-100", "Y": r"\\.\pipe\LOCAL\cc-msg-200"},
    "unix-far": {"kind": "socket", "X": "/tmp/cc-socks/cc-msg-100.sock", "Y": "/tmp/cc-socks/cc-msg-200.sock"},
}


def _load(filename, modname):
    spec = importlib.util.spec_from_file_location(modname, BIN / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


router = _load("unimatrix-peer-router.py", "named_send_router")
send_cli = _load("unimatrix-send.py", "named_send_cli")
roster = _load("unimatrix-roster.py", "named_send_roster")


@pytest.fixture(params=sorted(FAR_STYLES))
def far(request):
    return FAR_STYLES[request.param]


class World:
    """The far box: its registry, the process serving each endpoint, and each session's inbox."""

    def __init__(self, far):
        self.kind = far["kind"]
        self.ep = {X: far["X"], Y: far["Y"]}
        self.registry = [(PID_X, self.entry(X, SID_X), "x.key"), (PID_Y, self.entry(Y, SID_Y), "y.key")]
        self.served = {self.ep[X]: PID_X, self.ep[Y]: PID_Y}
        self.owner = {self.ep[X]: X, self.ep[Y]: Y}
        self.inbox = {X: [], Y: []}

    def entry(self, name, sid, endpoint=None):
        return {"name": name, "sessionId": sid, "messagingSocketPath": endpoint or self.ep[name]}


def mirror_row(world, name, far_name, sid, far_endpoint, far_pid, mirror_socket, target=None):
    return {"pid": os.getpid(), "name": name, "unimatrixFarName": far_name, "messagingSocketPath": mirror_socket,
            "macSocketPath": far_endpoint, "farPid": far_pid, "unimatrixRemote": REMOTE, "box": "machine-a",
            "unimatrixBox": "machine-a", "vendor": "claude", "sessionId": sid, "target": target,
            **roster.supervisor_fields(), **roster.far_seen_fields()}


def default_rows(world):
    return {
        X: mirror_row(world, X, X, SID_X, world.ep[X], PID_X, "/tmp/cc-socks/cc-msg-mirror-x.sock"),
        Y: mirror_row(world, Y, Y, SID_Y, world.ep[Y], PID_Y, "/tmp/cc-socks/cc-msg-mirror-y.sock"),
    }


class Conn:
    def __init__(self, data):
        self.chunks = [data, b""]

    def read(self):
        return self.chunks.pop(0)

    def close(self):
        pass


class Run:
    def __init__(self):
        self.exit_code = None
        self.stderr = ""
        self.outcomes = []
        self.notices = []
        self.frames = 0


@pytest.fixture
def relay(tmp_path, monkeypatch):
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path / "settings"))
    return _load("unimatrix-relay.py", "named_send_relay")


@pytest.fixture
def bridge(tmp_path, monkeypatch):
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path / "settings"))
    return _load("unimatrix-bridge.py", "named_send_bridge")


def wire_far_relay(relay, world, monkeypatch):
    fake_os = types.SimpleNamespace(**vars(relay.os))
    fake_os.stat = lambda p: types.SimpleNamespace(st_mode=0)
    monkeypatch.setattr(relay, "os", fake_os)
    monkeypatch.setattr(relay, "SOCKS", "/tmp/cc-socks")
    monkeypatch.setattr(relay.stat, "S_ISSOCK", lambda mode: True)
    monkeypatch.setattr(relay, "endpoint_kind", lambda platform=None: world.kind)
    monkeypatch.setattr(relay, "local_box", lambda: "machine-a")
    monkeypatch.setattr(relay, "entries", lambda: iter(list(world.registry)))
    monkeypatch.setattr(relay, "_router_resolve", lambda name: None)
    monkeypatch.setattr(relay, "local_reply_address", lambda line: line)
    monkeypatch.setattr(relay, "load_roster", lambda: types.SimpleNamespace(
        session_identity=lambda d: {}, authenticated_data=lambda path, line, sessions: line))

    def land(path, data):
        world.inbox[world.owner[path]].append(data)

    class Sock:
        def __init__(self, *a):
            self.path = None

        def connect(self, path):
            self.path = path

        def sendall(self, data):
            land(self.path, data)

        def close(self):
            pass

    monkeypatch.setattr(relay.socket, "socket", Sock)
    monkeypatch.setattr(relay.socket, "AF_UNIX", 1, raising=False)
    monkeypatch.setattr(relay, "_socket_peer_pid", lambda s: world.served.get(s.path), raising=False)
    monkeypatch.setattr(relay, "_pipe_server_pid", lambda handle: world.served.get(handle), raising=False)
    try:
        import win32file as real
        base = dict(vars(real))  # the receipt log opens its own file through the real module
    except ImportError:
        base = {}

    def create_file(path, *a):
        return path if path in world.owner else base["CreateFile"](path, *a)

    monkeypatch.setitem(sys.modules, "win32file", types.SimpleNamespace(**{
        "GENERIC_READ": 1, "GENERIC_WRITE": 2, "OPEN_EXISTING": 3, **base, "CreateFile": create_file, "WriteFile": land,
        "FlushFileBuffers": lambda handle: None, "CloseHandle": lambda handle: None}))


def far_ssh(relay, monkeypatch):
    def run(command, data):
        mode = relay.parse_mode(command)
        assert mode and mode[0] == "send-session", command
        monkeypatch.setattr(relay.sys, "stdin", types.SimpleNamespace(buffer=io.BytesIO(data)))
        out, code, err = io.StringIO(), 0, b""
        try:
            with contextlib.redirect_stdout(out):
                relay.send_batch(*mode[1])
        except SystemExit as refused:
            code, err = 1, str(refused.code).encode()
        result = subprocess.CompletedProcess([], code, out.getvalue().encode(), err)
        result.transport = "ssh"
        return result
    return run


def send(tmp_path, monkeypatch, relay, bridge, world, rows, to):
    """One `unimatrix-send.py --to <to>` on the near box; the far relay is the real one over `world`."""
    near = tmp_path / "near"
    near.mkdir()
    for key, row in rows.items():
        (near / f"{key}.json").write_text(json.dumps(row), encoding="utf-8")
    r = router.Router([], sessions_dir=near, state_dir=tmp_path / "state", endpoint_dir=tmp_path / "eps")
    sent = []

    async def deliver(peer, endpoint, frame):
        sent.append((endpoint, frame))

    async def face(source, destination):
        return "transport-face"

    monkeypatch.setattr(r, "_deliver_as_peer", deliver)
    monkeypatch.setattr(r, "_transport_face", face)

    class RouterOverDispatch:
        def __getattr__(self, attr):
            return getattr(router, attr)

        def request_line(self, ctl, req, *a):
            return asyncio.run(r._dispatch(req))

    monkeypatch.setattr(send_cli, "_router", RouterOverDispatch)
    run = Run()
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        run.exit_code = send_cli.main(["--to", to, "--endpoint-dir", str(tmp_path / "eps"), "hello"])
    run.stderr = err.getvalue()
    run.frames = len(sent)

    wire_far_relay(relay, world, monkeypatch)  # after the router: the fake socket would break asyncio's loop
    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()
    monkeypatch.setattr(bridge, "SESSIONS", str(bridge_dir))
    monkeypatch.setattr(bridge, "record_delivery", lambda row: run.outcomes.append(row))
    monkeypatch.setattr(bridge, "notify_sender", lambda line, target, error: run.notices.append((target, error)))
    monkeypatch.setattr(bridge, "_ssh_relay", far_ssh(relay, monkeypatch))
    bridge.log = open(tmp_path / "bridge.log", "a", encoding="utf-8")
    for endpoint, frame in sent:
        (mine,) = [row for row in rows.values() if row["messagingSocketPath"] == endpoint]
        (bridge_dir / f"{os.getpid()}.json").write_text(json.dumps(mine), encoding="utf-8")
        bridge.cfg = types.SimpleNamespace(endpoint=endpoint, key="k", remote=REMOTE, port=None, tunnel_remote=None)
        bridge.relay_send(Conn(json.dumps({**frame, "from": "uds:/tmp/cc-socks/sender.sock"}).encode() + b"\n"))
    bridge.log.close()
    return run


def assert_reaches_only(run, world, expected_session):
    """The frame is in `expected_session`'s inbox alone and the bridge recorded success."""
    assert world.inbox[Y if expected_session == X else X] == [], "frame landed on a session not named in the send"
    assert len(world.inbox[expected_session]) == 1
    assert run.outcomes and all(o["ok"] for o in run.outcomes) and not run.notices


def assert_refused(run, world, cause):
    """No inbox received the frame and the bridge recorded an error naming `cause`."""
    assert world.inbox[Y] == [], "frame landed on a session not named in the send"
    assert world.inbox[X] == []
    errors = [o.get("error") for o in run.outcomes]
    assert errors and all(errors), f"bridge recorded success or the frame never reached the bridge: {run.outcomes}"
    assert all(cause in e for e in errors), errors
    assert run.notices and all(cause in n[1] for n in run.notices)


def test_control_a_consistent_mirror_delivers_to_the_named_session_only(tmp_path, far, relay, bridge, monkeypatch):
    world = World(far)
    run = send(tmp_path, monkeypatch, relay, bridge, world, default_rows(world), X)
    assert_reaches_only(run, world, X)


def test_a_stand_in_with_a_null_target_on_a_mux_pid_reaches_its_own_session_only(tmp_path, far, relay, bridge, monkeypatch):
    world = World(far)
    world.registry = [(PID_MUX, world.entry(X, SID_X), "x.key"), (PID_MUX, world.entry(Y, SID_Y), "y.key")]
    world.served = {world.ep[X]: PID_MUX, world.ep[Y]: PID_MUX}
    rows = default_rows(world)
    rows[X]["farPid"], rows[Y]["farPid"] = PID_MUX, PID_MUX
    assert rows[X]["target"] is None
    run = send(tmp_path, monkeypatch, relay, bridge, world, rows, X)
    assert_reaches_only(run, world, X)


def test_a_null_target_stand_in_whose_far_socket_is_a_sibling_on_the_mux_pid_is_refused(tmp_path, far, relay, bridge, monkeypatch):
    world = World(far)
    world.registry = [(PID_MUX, world.entry(X, SID_X), "x.key"), (PID_MUX, world.entry(Y, SID_Y), "y.key")]
    world.served = {world.ep[X]: PID_MUX, world.ep[Y]: PID_MUX}
    rows = default_rows(world)
    rows[X].update(farPid=PID_MUX, macSocketPath=world.ep[Y])
    run = send(tmp_path, monkeypatch, relay, bridge, world, rows, X)
    assert_refused(run, world, "target mismatch")


def test_a_null_target_stand_in_whose_far_pid_now_holds_only_the_sibling_is_refused(tmp_path, far, relay, bridge, monkeypatch):
    world = World(far)
    world.registry = [(PID_MUX, world.entry(Y, SID_Y), "y.key")]
    world.served = {world.ep[X]: PID_MUX, world.ep[Y]: PID_MUX}
    rows = default_rows(world)
    rows[X]["farPid"] = PID_MUX
    run = send(tmp_path, monkeypatch, relay, bridge, world, rows, X)
    assert_refused(run, world, "target mismatch")


def test_a_stale_row_whose_pid_now_holds_the_sibling_on_another_endpoint_is_refused(tmp_path, far, relay, bridge, monkeypatch):
    world = World(far)
    world.registry = [(PID_X, world.entry(Y, SID_Y), "y.key")]
    world.served = {world.ep[X]: PID_X, world.ep[Y]: PID_X}
    run = send(tmp_path, monkeypatch, relay, bridge, world, default_rows(world), X)
    assert_refused(run, world, "target mismatch")


def test_a_stale_row_whose_recycled_pid_now_serves_the_sibling_on_the_same_endpoint_is_refused(tmp_path, far, relay, bridge, monkeypatch):
    world = World(far)
    world.registry = [(PID_X, world.entry(Y, SID_Y, endpoint=world.ep[X]), "y.key")]
    world.owner[world.ep[X]] = Y
    run = send(tmp_path, monkeypatch, relay, bridge, world, default_rows(world), X)
    assert_refused(run, world, "target mismatch")


def test_an_endpoint_for_x_served_by_the_siblings_process_is_refused(tmp_path, far, relay, bridge, monkeypatch):
    world = World(far)
    world.served[world.ep[X]] = PID_Y
    world.owner[world.ep[X]] = Y
    run = send(tmp_path, monkeypatch, relay, bridge, world, default_rows(world), X)
    assert_refused(run, world, "target mismatch")


@pytest.mark.parametrize("carried", [
    {"unimatrixFarName": Y, "sessionId": SID_Y, "macSocketPath": "Y", "farPid": PID_Y},
    {"sessionId": SID_Y, "macSocketPath": "Y", "farPid": PID_Y},
    {"sessionId": SID_Y},
    {"unimatrixFarName": Y},
], ids=["far-name-sid-endpoint", "sid-endpoint", "sid-only", "far-name-only"])
def test_a_mirror_named_x_carrying_the_siblings_identity_is_refused(tmp_path, far, relay, bridge, monkeypatch, carried):
    world = World(far)
    rows = default_rows(world)
    rows[X].update({k: (world.ep[Y] if v == "Y" else v) for k, v in carried.items()})
    run = send(tmp_path, monkeypatch, relay, bridge, world, rows, X)
    assert world.inbox[Y] == [], "frame landed on a session not named in the send"
    assert world.inbox[X] == []
    if not run.frames:
        assert run.exit_code == 1 and run.stderr, "refused before the bridge, but not loudly"
        return
    errors = [o.get("error") for o in run.outcomes]
    assert errors and all(errors), f"bridge recorded success: {run.outcomes}"
    assert all(("target mismatch" in e) or ("endpoint is" in e) for e in errors), errors


def test_a_row_whose_ref_does_not_match_the_live_session_at_its_pipe_is_never_delivered_to(tmp_path, far, relay, bridge, monkeypatch):
    """The live G-EM is [38d698] on pid 100; the near row named X carries ref [639e3e] and X's sibling's endpoint."""
    world = World(far)
    rows = default_rows(world)
    rows[X].update(sessionId=SID_X_STALE, macSocketPath=world.ep[Y], farPid=PID_Y)
    run = send(tmp_path, monkeypatch, relay, bridge, world, rows, X)
    assert_refused(run, world, "target mismatch")
    assert SID_X_STALE in run.outcomes[0]["error"]


def test_a_row_with_a_stale_ref_and_the_live_endpoint_is_never_delivered_to(tmp_path, far, relay, bridge, monkeypatch):
    world = World(far)
    rows = default_rows(world)
    rows[X]["sessionId"] = SID_X_STALE
    run = send(tmp_path, monkeypatch, relay, bridge, world, rows, X)
    assert_refused(run, world, "target mismatch")


def test_the_stale_ref_row_is_refused_when_the_pipe_is_served_by_the_sibling_too(tmp_path, far, relay, bridge, monkeypatch):
    world = World(far)
    world.served[world.ep[X]] = PID_Y
    world.owner[world.ep[X]] = Y
    rows = default_rows(world)
    rows[X]["sessionId"] = SID_X_STALE
    run = send(tmp_path, monkeypatch, relay, bridge, world, rows, X)
    assert_refused(run, world, "target mismatch")


def test_two_far_sessions_exporting_the_addressed_name_are_refused_as_ambiguous(tmp_path, far, relay, bridge, monkeypatch):
    world = World(far)
    world.registry[1] = (PID_Y, world.entry(X, SID_Y, endpoint=world.ep[Y]), "y.key")
    run = send(tmp_path, monkeypatch, relay, bridge, world, default_rows(world), X)
    assert_refused(run, world, "ambiguous")


def test_a_stale_twin_mirror_named_x_never_carries_the_send_to_the_sibling(tmp_path, far, relay, bridge, monkeypatch):
    """The near roster holds the live [38d698] mirror and a stale [639e3e] twin bound to the sibling's pipe."""
    world = World(far)
    rows = default_rows(world)
    rows["stale"] = mirror_row(world, X, X, SID_X_STALE, world.ep[Y], PID_Y, "/tmp/cc-socks/cc-msg-mirror-stale.sock")
    run = send(tmp_path, monkeypatch, relay, bridge, world, rows, X)
    assert world.inbox[Y] == [], "frame landed on a session not named in the send"
    if run.frames:
        errors = [o.get("error") for o in run.outcomes]
        assert (not any(errors) and len(world.inbox[X]) == 1) or all(errors), run.outcomes
    else:
        assert run.exit_code == 1 and run.stderr, "refused before the bridge, but not loudly"
