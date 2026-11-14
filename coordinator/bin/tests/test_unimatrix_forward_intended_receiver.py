"""A forwarded transport frame names its intended receiver; delivery is refused when the resolved session lacks it.

Reproduces the most-recent-correspondent misroute: a third session writing to a face opened for
(source, destination) used to be forwarded to the face source with only a hop marker.
Hops pinned: Router.handle_transport_inbound, unimatrix-peer-contract build_frame/intended_receiver.
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


pr = _load("unimatrix-peer-router.py", "forward_receiver_router")
pc = pr._contract()


def run(coro):
    return asyncio.run(coro)


def _session(sessions, name, sock):
    (sessions / f"{name}.json").write_text(json.dumps(
        {"name": name, "pid": os.getpid(), "messagingSocketPath": sock, "startedAt": 1}))


@pytest.fixture
def face(tmp_path, monkeypatch):
    """Face `t` opened by source example-stats-repo-0d toward destination group-em-c1; example-game-repo-78 is a third session."""
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    socks = {n: str(tmp_path / f"{n}.sock") for n in ("example-stats-repo-0d", "group-em-c1", "example-game-repo-78")}
    for name, sock in socks.items():
        _session(sessions, name, sock)
    r = pr.Router([], sessions_dir=sessions, state_dir=tmp_path / "state", endpoint_dir=tmp_path / "eps", adapters={})
    r.transport_sources["t"], r.transport_targets["t"] = "example-stats-repo-0d", "group-em-c1"
    sent = []

    async def capture(peer, endpoint, frame):
        sent.append((endpoint, frame))
    monkeypatch.setattr(r, "_deliver_as_peer", capture)
    return r, sent, socks


def _inbound(socks, sender, text, to_name=None):
    return pc.build_frame(socks[sender], sender, text, to_name=to_name)


def test_third_session_frame_naming_no_receiver_is_not_forwarded_to_the_face_source(face):
    r, sent, socks = face
    run(r.handle_transport_inbound("t", _inbound(socks, "example-game-repo-78", "Saliva C5 probe slot: waiting for your slot")))
    assert sent == []


def test_third_session_slot_ask_resolves_to_group_em_not_the_face_source(face):
    r, sent, socks = face
    run(r.handle_transport_inbound("t", _inbound(socks, "example-game-repo-78", "waiting for your slot", to_name="slot")))
    assert sent == []  # the resolved session is example-stats-repo-0d, which holds no slot-admission role
    assert pr.Router._holds_receiver(r, "group-em-c1", "slot")
    assert not pr.Router._holds_receiver(r, "example-stats-repo-0d", "slot")


def test_a_frame_naming_the_resolved_session_is_delivered_with_its_receiver(face):
    r, sent, socks = face
    run(r.handle_transport_inbound("t", _inbound(socks, "example-game-repo-78", "hello", to_name="example-stats-repo")))
    assert [e for e, _ in sent] == [socks["example-stats-repo-0d"]]
    assert pc.intended_receiver(sent[0][1]) == "example-stats-repo"


def test_the_destinations_own_reply_still_reaches_the_source_naming_it(face):
    r, sent, socks = face
    run(r.handle_transport_inbound("t", _inbound(socks, "group-em-c1", "granted")))
    assert [e for e, _ in sent] == [socks["example-stats-repo-0d"]]
    assert pc.intended_receiver(sent[0][1]) == "example-stats-repo-0d"


def test_the_source_frame_forwarded_to_the_destination_names_the_destination(face):
    r, sent, socks = face
    run(r.handle_transport_inbound("t", _inbound(socks, "example-stats-repo-0d", "request")))
    assert [e for e, _ in sent] == [socks["group-em-c1"]]
    assert pc.intended_receiver(sent[0][1]) == "group-em-c1"


def test_a_slot_ask_from_the_source_reaches_the_group_em_destination(face):
    r, sent, socks = face
    run(r.handle_transport_inbound("t", _inbound(socks, "example-stats-repo-0d", "need a slot", to_name="slot")))
    assert [e for e, _ in sent] == [socks["group-em-c1"]]
    assert pc.intended_receiver(sent[0][1]) == "slot"


def test_a_receiver_that_the_destination_does_not_hold_is_refused_and_logged(face):
    r, sent, socks = face
    logged = []
    r.log = lambda *a: logged.append(a)
    run(r.handle_transport_inbound("t", _inbound(socks, "example-stats-repo-0d", "request", to_name="example-game-repo")))
    assert sent == []
    assert any(a[0] == "TRANSPORT-RECEIVER-REFUSED" for a in logged)


def test_contract_round_trips_to_name_and_role_matching():
    f = pc.build_frame("/s", "me", "hi", to_name="group-em-c1")
    assert pc.intended_receiver(f) == "group-em-c1"
    assert pc.parse_inbound(f) == ("/s", "me", "hi")
    assert pc.intended_receiver(pc.build_frame("/s", "me", "hi")) == ""
    assert pc.holds_receiver("group-em-c1.machine-b", "group-em")
    assert pc.holds_receiver("group-em-c1", "group-em-c1")
    assert not pc.holds_receiver("example-stats-repo-0d", "group-em")
    assert not pc.holds_receiver("example-stats-repo-0d", "")
