"""A name or `name [ref]` held by two boxes' rosters reaches the session it denotes or is refused, never another session.

Two boxes under tmp_path: the far box (Machine-a) holds the real sessions; the near box (Mini) holds a
sessions dir of mirror rows built from them. A stale mirror row carries the name of one far session and the
far name or socket of another (a reused far socket renamed in place), so a send by that name would land on
the wrong session. Hops pinned: Router.resolve_sender, _transport_send_to_session, _peer_send_to_session.
"""
import asyncio
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

BIN = Path(__file__).resolve().parents[1]


def _load(filename, modname):
    spec = importlib.util.spec_from_file_location(modname, BIN / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


router = _load("unimatrix-peer-router.py", "two_box_misroute_router")

TARGET, EXAMPLE_GAME_REPO, GROUP, ADDON = "example-retrieval-repo-17", "example-game-workbench-repo-66", "group-em-56", "addon-1a"
TARGET_SID, EXAMPLE_GAME_REPO_SID = "2d8074aa-0000-4000-8000-000000000001", "09e536bb-0000-4000-8000-000000000002"
SOCKS = {"target": "/near/cc-msg-target.sock", "example-game-repo": "/near/cc-msg-example-game-repo.sock",
         "group": "/near/cc-msg-group.sock", "addon": "/near/cc-msg-addon.sock"}


def dead_pid():
    """A pid that belonged to a process that has exited."""
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    child = subprocess.Popen([sys.executable, "-c", "pass"], creationflags=flags)
    child.wait()
    return child.pid


def far_session(name, sid, sock):
    """A row as the far box's own sessions dir holds it."""
    return {"name": name, "sessionId": sid, "vendor": "claude", "messagingSocketPath": sock, "pid": 4242}


def mirror(far_row, mirror_name, near_sock, far_name=None, sid=None, live=True):
    """The near box's mirror row for `far_row`; name, far name and session id may be made to disagree."""
    return {"name": mirror_name, "unimatrixFarName": far_name or far_row["name"], "vendor": "claude",
            "sessionId": sid or far_row["sessionId"], "messagingSocketPath": near_sock,
            "macSocketPath": far_row["messagingSocketPath"], "farPid": far_row["pid"],
            "unimatrixRemote": "machine-a-lan", "box": "machine-a", "pid": os.getpid() if live else dead_pid()}


def near_box(tmp_path, rows):
    sessions = tmp_path / "near" / "sessions"
    sessions.mkdir(parents=True)
    for i, row in enumerate(rows):
        (sessions / f"{i}.json").write_text(json.dumps(row), encoding="utf-8")
    return router.Router([], sessions_dir=sessions, state_dir=tmp_path / "state", endpoint_dir=tmp_path / "eps")


FAR_TARGET = far_session(TARGET, TARGET_SID, r"\\.\pipe\LOCAL\cc-msg-2d80")
FAR_EXAMPLE_GAME_REPO = far_session(EXAMPLE_GAME_REPO, EXAMPLE_GAME_REPO_SID, r"\\.\pipe\LOCAL\cc-msg-09e5")
FAR_GROUP = far_session(GROUP, "aa0000aa-0000-4000-8000-000000000003", r"\\.\pipe\LOCAL\cc-msg-g56")
FAR_ADDON = far_session(ADDON, "bb0000bb-0000-4000-8000-000000000004", r"\\.\pipe\LOCAL\cc-msg-a1a")


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


# A stale mirror: it answers to the name of one far session but its far name (the session it relays to) is another's.
STALE_CASES = [
    pytest.param(TARGET, EXAMPLE_GAME_REPO, "example-game-repo", FAR_EXAMPLE_GAME_REPO, id="example-retrieval-repo-17-lands-on-example-game-repo-66"),
    pytest.param(GROUP, ADDON, "addon", FAR_ADDON, id="group-em-56-lands-on-addon-1a"),
]


@pytest.mark.parametrize("asked, far_name, sock, far_row", STALE_CASES)
def test_stale_mirror_named_for_one_session_and_relaying_to_another_is_refused(
        tmp_path, monkeypatch, asked, far_name, sock, far_row):
    # The true session's own mirror is down (dead pid); the stale row is the only live carrier of the name.
    true_row = {TARGET: FAR_TARGET, GROUP: FAR_GROUP}[asked]
    r = near_box(tmp_path, [mirror(true_row, asked, "/near/cc-msg-true.sock", live=False),
                            mirror(far_row, asked, SOCKS[sock], far_name=far_name)])
    sent = capture(r, monkeypatch)
    assert r.resolve_sender(asked) == ""
    assert r.last_refusal == "refused identity"
    response = dispatch(r, asked)
    assert not response["ok"] and asked in response["error"] and "written" not in response
    assert sent == []


@pytest.mark.parametrize("asked, far_name, sock, far_row", STALE_CASES)
def test_the_far_name_a_stale_mirror_relays_to_is_refused_through_it_too(
        tmp_path, monkeypatch, asked, far_name, sock, far_row):
    r = near_box(tmp_path, [mirror(far_row, asked, SOCKS[sock], far_name=far_name)])
    sent = capture(r, monkeypatch)
    assert r.resolve_sender(far_name) == ""
    assert not dispatch(r, far_name)["ok"] and sent == []


def test_stale_mirror_beside_the_live_true_mirror_never_wins_the_name(tmp_path, monkeypatch):
    r = near_box(tmp_path, [mirror(FAR_TARGET, TARGET, SOCKS["target"]),
                            mirror(FAR_EXAMPLE_GAME_REPO, TARGET, SOCKS["example-game-repo"], far_name=EXAMPLE_GAME_REPO)])
    sent = capture(r, monkeypatch)
    assert r.resolve_sender(TARGET) == ""
    assert not dispatch(r, TARGET)["ok"] and sent == []


def test_box_qualified_stale_mirror_is_refused_by_its_full_name(tmp_path, monkeypatch):
    r = near_box(tmp_path, [mirror(FAR_EXAMPLE_GAME_REPO, f"{TARGET}.machine-a", SOCKS["example-game-repo"], far_name=EXAMPLE_GAME_REPO)])
    sent = capture(r, monkeypatch)
    assert r.resolve_sender(f"{TARGET}.machine-a") == ""
    assert not dispatch(r, f"{TARGET}.machine-a")["ok"] and sent == []


@pytest.mark.parametrize("name", [TARGET, f"{TARGET}.machine-a"])
def test_consistent_mirrors_still_deliver_to_their_own_session(tmp_path, monkeypatch, name):
    r = near_box(tmp_path, [mirror(FAR_TARGET, name, SOCKS["target"]),
                            mirror(FAR_EXAMPLE_GAME_REPO, EXAMPLE_GAME_REPO, SOCKS["example-game-repo"])])
    sent = capture(r, monkeypatch)
    assert r.resolve_sender(name) == SOCKS["target"]
    assert dispatch(r, name)["ok"] and sent == [SOCKS["target"]]


def ref_name(name, sid):
    return f"{name} [{sid[:6]}]"


def test_name_ref_for_a_session_the_bare_name_row_does_not_carry_is_refused(tmp_path, monkeypatch):
    r = near_box(tmp_path, [mirror(FAR_EXAMPLE_GAME_REPO, EXAMPLE_GAME_REPO, SOCKS["example-game-repo"]),
                            mirror(FAR_TARGET, TARGET, SOCKS["target"], live=False)])
    sent = capture(r, monkeypatch)
    for asked in (ref_name(TARGET, TARGET_SID), ref_name(TARGET, EXAMPLE_GAME_REPO_SID)):
        response = dispatch(r, asked)
        assert not response["ok"] and asked in response["error"]
    assert sent == []


def test_name_ref_row_whose_session_id_is_another_sessions_is_refused(tmp_path, monkeypatch):
    asked = ref_name(TARGET, TARGET_SID)
    r = near_box(tmp_path, [mirror(FAR_EXAMPLE_GAME_REPO, asked, SOCKS["example-game-repo"], far_name=asked, sid=EXAMPLE_GAME_REPO_SID)])
    sent = capture(r, monkeypatch)
    assert r.resolve_sender(asked) == ""
    assert r.last_refusal == "refused identity"
    assert not dispatch(r, asked)["ok"] and sent == []


def test_name_ref_row_carrying_that_ref_delivers(tmp_path, monkeypatch):
    asked = ref_name(TARGET, TARGET_SID)
    r = near_box(tmp_path, [mirror(FAR_TARGET, asked, SOCKS["target"], far_name=asked)])
    sent = capture(r, monkeypatch)
    assert dispatch(r, asked)["ok"] and sent == [SOCKS["target"]]


def test_peer_send_by_a_stale_name_is_refused_and_writes_nothing(tmp_path, monkeypatch):
    r = near_box(tmp_path, [mirror(FAR_EXAMPLE_GAME_REPO, TARGET, SOCKS["example-game-repo"], far_name=EXAMPLE_GAME_REPO)])
    r.peers["agy-a"] = {"name": "agy-a", "vendor": "agy", "target": "conv-1"}
    sent = capture(r, monkeypatch)
    response = asyncio.run(r._peer_send_to_session("agy-a", TARGET, "hello"))
    assert not response["ok"] and "written" not in response and sent == []


def test_pipe_address_off_windows_with_no_relay_route_is_refused_not_opened(tmp_path, monkeypatch):
    row = mirror(FAR_TARGET, TARGET, FAR_TARGET["messagingSocketPath"])
    r = near_box(tmp_path, [row])
    sent = capture(r, monkeypatch)
    monkeypatch.setattr(router, "_off_windows", lambda: True)
    response = dispatch(r, TARGET)
    assert not response["ok"] and "no relay route" in response["error"] and sent == []
