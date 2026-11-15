"""Inbox `localize` hands the receiver the sending box's reply-inbox endpoint, else the sender's name."""
import importlib.util
import json
from pathlib import Path

import pytest

pytestmark = pytest.mark.complete

INBOX = Path(__file__).resolve().parents[1] / "unimatrix-inbox.py"
PIPE = "\\\\.\\pipe\\LOCAL\\cc-msg-" + "e" * 32
POSIX_FAR = "/tmp/cc-socks/cc-msg-" + "d" * 32 + ".sock"
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


def run(mirrors, box, src=PIPE):
    inbox = load()
    inbox._mirrors = lambda: iter(mirrors)
    line = json.dumps(frame(src)).encode()
    return line, inbox.localize(frame(src), line, box)


def assert_name_routed(out, src=PIPE):
    """No endpoint maps: the attribute a reply copies is the sender's name; the far path is not shown."""
    body = json.loads(out)
    assert 'from="example-fleet-1"' in body["message"]["content"]
    assert src not in body["message"]["content"]
    assert body["from"] == "uds:" + src


def test_pipe_sender_with_no_mirror_gets_its_boxs_reply_inbox():
    line, out = run([reply_inbox("10.0.0.1", EP_A), reply_inbox("10.0.0.2", EP_B)], "10.0.0.1")
    body = json.loads(out)
    assert body["from"] == "uds:" + EP_A
    assert f'from="uds:{EP_A}"' in body["message"]["content"]
    assert PIPE not in out.decode()


def test_another_boxs_reply_inbox_is_never_picked():
    line, out = run([reply_inbox("10.0.0.2", EP_B)], "10.0.0.1")
    assert_name_routed(out)


def test_two_reply_inboxes_for_one_box_fall_back_to_the_sender_name():
    line, out = run([reply_inbox("10.0.0.1", EP_A), reply_inbox("10.0.0.1", EP_B)], "10.0.0.1")
    assert_name_routed(out)


def test_a_plain_mirror_is_not_a_reply_inbox():
    plain = {"unimatrixMirror": True, "unimatrixClient": "10.0.0.1", "messagingSocketPath": EP_A}
    line, out = run([plain], "10.0.0.1")
    assert_name_routed(out)


@pytest.mark.parametrize("src", [PIPE, POSIX_FAR])
def test_unmapped_far_path_of_either_os_shape_never_reaches_the_reply_attribute(src):
    line, out = run([], "10.0.0.1", src)
    assert_name_routed(out, src)


def test_a_mapped_sender_still_wins_over_the_name_fallback():
    mirror = {"name": "example-fleet-1.10.0.0.1", "macSocketPath": PIPE, "messagingSocketPath": EP_A}
    line, out = run([mirror], "10.0.0.1")
    assert json.loads(out)["from"] == "uds:" + EP_A
