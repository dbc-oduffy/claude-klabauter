"""The bridge return leg stamps a from-address the receiving box can reply to, routing back to the far sender."""
import importlib.util
import json
import types
from pathlib import Path

import pytest

pytestmark = pytest.mark.complete

BIN = Path(__file__).resolve().parents[1]
FAR_CC_MSG = "/tmp/cc-socks/cc-msg-" + "f" * 32 + ".sock"
REPLY_EP = "/tmp/cc-socks/cc-msg-" + "b" * 32 + ".sock"
MIRROR_EP = "/tmp/cc-socks/cc-msg-" + "c" * 32 + ".sock"


@pytest.fixture
def bridge(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("unimatrix_bridge_reply", BIN / "unimatrix-bridge.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    monkeypatch.setattr(mod, "SESSIONS", str(sessions))
    mod.cfg = types.SimpleNamespace(remote="me@boxlan")
    mod.log = open(tmp_path / "b.log", "a", encoding="utf-8")
    mod.sessions_dir = sessions
    yield mod
    mod.log.close()


def frame(src, name="far-1"):
    content = f'<cross-session-message from="uds:{src}" from-name="{name}">\nhi\n</cross-session-message>'
    return {"from": "uds:" + src, "message": {"role": "user", "content": content}}


def localize(bridge, f):
    out, port = bridge.localize_from(json.dumps(f).encode() + b"\n")
    return json.loads(out.splitlines()[-1]), port


def reply_inbox(sessions, pid=9, client="boxlan", endpoint=REPLY_EP):
    (sessions / f"{pid}.json").write_text(json.dumps(
        {"pid": pid, "unimatrixMirror": True, "unimatrixReplyInbox": True, "unimatrixClient": client,
         "messagingSocketPath": endpoint}))


def test_unmatched_sender_gets_the_sending_boxs_reply_inbox_endpoint_in_frame_and_attribute(bridge):
    reply_inbox(bridge.sessions_dir)
    out, port = localize(bridge, frame(FAR_CC_MSG))
    assert out["from"] == "uds:" + REPLY_EP
    assert f'from="uds:{REPLY_EP}"' in out["message"]["content"]
    assert FAR_CC_MSG not in json.dumps(out)
    assert port is None


def test_reply_inbox_of_another_box_is_not_used(bridge):
    reply_inbox(bridge.sessions_dir, client="otherbox")
    out, _ = localize(bridge, frame(FAR_CC_MSG))
    assert REPLY_EP not in json.dumps(out)
    assert 'from="far-1"' in out["message"]["content"]


def test_two_reply_inboxes_for_the_box_are_ambiguous_and_fall_back_to_the_name(bridge):
    reply_inbox(bridge.sessions_dir, pid=9)
    reply_inbox(bridge.sessions_dir, pid=10, endpoint="/tmp/cc-socks/cc-msg-" + "d" * 32 + ".sock")
    out, _ = localize(bridge, frame(FAR_CC_MSG))
    assert 'from="far-1"' in out["message"]["content"]


def test_same_session_mirror_found_by_from_name_wins_over_the_reply_inbox(bridge):
    reply_inbox(bridge.sessions_dir)
    (bridge.sessions_dir / "8.json").write_text(json.dumps(
        {"pid": 8, "name": "far-1.boxlan", "unimatrixMirror": True, "unimatrixRemote": "me@boxlan",
         "macSocketPath": "/tmp/cc-socks/4242.sock", "messagingSocketPath": MIRROR_EP}))
    out, _ = localize(bridge, frame(FAR_CC_MSG))
    assert out["from"] == "uds:" + MIRROR_EP
    assert f'from="uds:{MIRROR_EP}"' in out["message"]["content"]


def test_a_from_that_is_already_a_local_address_is_left_alone(bridge):
    reply_inbox(bridge.sessions_dir)
    sent = frame(REPLY_EP)
    out, _ = localize(bridge, sent)
    assert out == sent
