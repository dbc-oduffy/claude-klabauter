"""Inbox `localize` hands the receiver the sending box's reply-inbox endpoint, or leaves the frame refused-visible."""
import importlib.util
import json
from pathlib import Path

import pytest

pytestmark = pytest.mark.complete

INBOX = Path(__file__).resolve().parents[1] / "unimatrix-inbox.py"
PIPE = "\\\\.\\pipe\\LOCAL\\cc-msg-" + "e" * 32
EP_A = "/tmp/cc-socks/unimatrix-transport-on-a.sock"
EP_B = "/tmp/cc-socks/unimatrix-transport-on-b.sock"


def load():
    spec = importlib.util.spec_from_file_location("unimatrix_inbox_localize", INBOX)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def frame(src=PIPE):
    content = f'<cross-session-message from="uds:{src}" from-name="example-fleet-1">\nhi\n</cross-session-message>'
    return {"from": "uds:" + src, "message": {"role": "user", "content": content}}


def reply_inbox(client, endpoint):
    return {"unimatrixMirror": True, "unimatrixReplyInbox": True, "unimatrixClient": client,
            "messagingSocketPath": endpoint}


def run(mirrors, box):
    inbox = load()
    inbox._mirrors = lambda: iter(mirrors)
    line = json.dumps(frame()).encode()
    return line, inbox.localize(frame(), line, box)


def test_pipe_sender_with_no_mirror_gets_its_boxs_reply_inbox():
    line, out = run([reply_inbox("10.0.0.1", EP_A), reply_inbox("10.0.0.2", EP_B)], "10.0.0.1")
    body = json.loads(out)
    assert body["from"] == "uds:" + EP_A
    assert f'from="uds:{EP_A}"' in body["message"]["content"]
    assert PIPE not in out.decode()


def test_another_boxs_reply_inbox_is_never_picked():
    line, out = run([reply_inbox("10.0.0.2", EP_B)], "10.0.0.1")
    assert out == line


def test_two_reply_inboxes_for_one_box_leave_the_frame_refused_visible():
    line, out = run([reply_inbox("10.0.0.1", EP_A), reply_inbox("10.0.0.1", EP_B)], "10.0.0.1")
    assert out == line


def test_a_plain_mirror_is_not_a_reply_inbox():
    plain = {"unimatrixMirror": True, "unimatrixClient": "10.0.0.1", "messagingSocketPath": EP_A}
    line, out = run([plain], "10.0.0.1")
    assert out == line
