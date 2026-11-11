"""Relay `local_reply_address` hands the receiver a key-vouched local endpoint for a far sender."""
import importlib.util
import json
from pathlib import Path

import pytest

pytestmark = pytest.mark.complete

RELAY = Path(__file__).resolve().parents[1] / "unimatrix-relay.py"
FAR = "/tmp/cc-socks/1986.sock"
LOCAL = "\\\\.\\pipe\\cc-mirror-1"


def load():
    spec = importlib.util.spec_from_file_location("unimatrix_relay_reply_address", RELAY)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


R = load()


def frame(addr=f"uds:{FAR}"):
    content = f'<cross-session-message from="{addr}">hi</cross-session-message>'
    return json.dumps({"from": addr, "message": {"content": content}}).encode() + b"\n"


def serve(monkeypatch, rows, client=""):
    monkeypatch.setattr(R, "entries", lambda: iter([(i, d, f"{i}.k.key") for i, d in enumerate(rows, 1)]))
    monkeypatch.setattr(R, "client_address", lambda: client)


def roster_mirror(local=LOCAL, far=FAR):
    return {"messagingSocketPath": local, "macSocketPath": far, "name": "sender"}


def test_roster_mirror_rewrites_from_and_content(monkeypatch):
    serve(monkeypatch, [roster_mirror()])
    out = json.loads(R.local_reply_address(frame()))
    assert out["from"] == f"uds:{LOCAL}"
    assert f'from="uds:{LOCAL}"' in out["message"]["content"]
    assert FAR not in out["message"]["content"]


def test_two_roster_mirrors_leave_the_frame_unchanged(monkeypatch):
    serve(monkeypatch, [roster_mirror(), roster_mirror(local="\\\\.\\pipe\\cc-mirror-2")])
    line = frame()
    assert R.local_reply_address(line) == line


def test_no_candidate_or_other_socket_leaves_the_frame_unchanged(monkeypatch):
    serve(monkeypatch, [roster_mirror(far="/tmp/cc-socks/other.sock"), {"messagingSocketPath": LOCAL}])
    line = frame()
    assert R.local_reply_address(line) == line


def test_roster_mirror_without_a_local_endpoint_is_not_a_candidate(monkeypatch):
    serve(monkeypatch, [{"macSocketPath": FAR, "name": "sender"}])
    line = frame()
    assert R.local_reply_address(line) == line


def box_mirror(client, local):
    return {**roster_mirror(local=local), "unimatrixClient": client}


def test_same_socket_on_two_boxes_resolves_to_the_sending_boxs_mirror(monkeypatch):
    rows = [box_mirror("10.0.0.1", "\\\\.\\pipe\\cc-a"), box_mirror("10.0.0.2", "\\\\.\\pipe\\cc-b")]
    for client, local in (("10.0.0.1", "\\\\.\\pipe\\cc-a"), ("10.0.0.2", "\\\\.\\pipe\\cc-b")):
        serve(monkeypatch, rows, client=client)
        out = json.loads(R.local_reply_address(frame()))
        assert out["from"] == f"uds:{local}"
        assert f'from="uds:{local}"' in out["message"]["content"]


def test_another_boxs_only_mirror_is_never_picked(monkeypatch):
    serve(monkeypatch, [box_mirror("10.0.0.1", "\\\\.\\pipe\\cc-a")], client="10.0.0.3")
    line = frame()
    assert R.local_reply_address(line) == line


def test_unidentified_sender_never_picks_a_box_named_mirror(monkeypatch):
    serve(monkeypatch, [box_mirror("10.0.0.1", "\\\\.\\pipe\\cc-a"), box_mirror("10.0.0.2", "\\\\.\\pipe\\cc-b")])
    line = frame()
    assert R.local_reply_address(line) == line


def production_mirror(remote, box, local, ip=None):
    """A roster mirror row as unimatrix-roster mirror() writes it: hostname labels, the far box's IP when it resolved."""
    row = {**roster_mirror(local=local), "unimatrixRemote": remote, "unimatrixBox": box}
    return {**row, "unimatrixRemoteIp": ip} if ip else row


def serve_hosts(monkeypatch, rows, client):
    serve(monkeypatch, rows, client=client)

    def no_dns(*args, **kwargs):
        raise AssertionError("the relay path resolved a name")

    for fn in ("gethostbyaddr", "gethostbyname", "getaddrinfo"):
        monkeypatch.setattr(R.socket, fn, no_dns, raising=False)


def test_hostname_labelled_rows_resolve_by_recorded_ip(monkeypatch):
    rows = [production_mirror("me@far-a", "far-a", "\\\\.\\pipe\\cc-a", ip="10.0.0.1"),
            production_mirror("me@far-b.lan", "far-b", "\\\\.\\pipe\\cc-b", ip="10.0.0.2")]
    for client, local in (("10.0.0.1", "\\\\.\\pipe\\cc-a"), ("10.0.0.2", "\\\\.\\pipe\\cc-b")):
        serve_hosts(monkeypatch, rows, client)
        out = json.loads(R.local_reply_address(frame()))
        assert out["from"] == f"uds:{local}"
        assert f'from="uds:{local}"' in out["message"]["content"]


def test_hostname_labelled_row_with_a_different_recorded_ip_is_never_picked(monkeypatch):
    serve_hosts(monkeypatch, [production_mirror("me@far-a", "far-a", "\\\\.\\pipe\\cc-a", ip="10.0.0.1")], "10.0.0.9")
    line = frame()
    assert R.local_reply_address(line) == line


def test_hostname_labelled_row_without_a_recorded_ip_is_never_picked(monkeypatch):
    serve_hosts(monkeypatch, [production_mirror("me@far-a", "far-a", "\\\\.\\pipe\\cc-a")], "10.0.0.1")
    line = frame()
    assert R.local_reply_address(line) == line


def test_recorded_client_overrides_the_recorded_remote_ip(monkeypatch):
    row = {**production_mirror("me@far-a", "far-a", "\\\\.\\pipe\\cc-a", ip="10.0.0.1"), "unimatrixClient": "10.0.0.2"}
    serve_hosts(monkeypatch, [row], "10.0.0.1")
    line = frame()
    assert R.local_reply_address(line) == line


def test_remote_naming_the_client_address_matches(monkeypatch):
    serve_hosts(monkeypatch, [production_mirror("me@10.0.0.1", "", "\\\\.\\pipe\\cc-a")], "10.0.0.1")
    out = json.loads(R.local_reply_address(frame()))
    assert out["from"] == "uds:\\\\.\\pipe\\cc-a"


PIPE = "\\\\.\\pipe\\LOCAL\\cc-msg-e14fb8bec27ee43ee73204939557fa1a"


def reply_inbox(client, local="/tmp/cc-socks/unimatrix-transport-on-machine-a.sock"):
    return {"unimatrixMirror": True, "unimatrixReplyInbox": True, "unimatrixClient": client,
            "unimatrixRemoteSocket": "\\\\.\\pipe\\LOCAL\\transport", "messagingSocketPath": local}


def test_unmirrored_far_pipe_sender_gets_the_sending_boxs_reply_inbox(monkeypatch):
    serve(monkeypatch, [reply_inbox("10.0.0.1"), reply_inbox("10.0.0.2", "/tmp/cc-socks/other.sock")], client="10.0.0.1")
    out = json.loads(R.local_reply_address(frame(PIPE)))
    assert out["from"] == "/tmp/cc-socks/unimatrix-transport-on-machine-a.sock"
    assert PIPE not in out["message"]["content"]


def test_unmirrored_sender_from_an_unknown_box_stays_unchanged(monkeypatch):
    serve(monkeypatch, [reply_inbox("10.0.0.1")], client="10.0.0.9")
    line = frame(PIPE)
    assert R.local_reply_address(line) == line


def test_unmirrored_sender_with_no_client_stays_unchanged(monkeypatch):
    serve(monkeypatch, [reply_inbox("10.0.0.1")])
    line = frame(PIPE)
    assert R.local_reply_address(line) == line


def test_two_reply_inboxes_from_one_box_leave_the_frame_unchanged(monkeypatch):
    serve(monkeypatch, [reply_inbox("10.0.0.1"), reply_inbox("10.0.0.1", "/tmp/cc-socks/b.sock")], client="10.0.0.1")
    line = frame(PIPE)
    assert R.local_reply_address(line) == line


def test_relay_inbox_mirror_is_still_preferred(monkeypatch):
    inbox = {"unimatrixMirror": True, "unimatrixRemoteSocket": FAR, "messagingSocketPath": "\\\\.\\pipe\\cc-inbox"}
    serve(monkeypatch, [roster_mirror(), inbox])
    out = json.loads(R.local_reply_address(frame()))
    assert out["from"] == "uds:\\\\.\\pipe\\cc-inbox"
