"""A mirror pushed onto a far box, or pulled by its roster, is named `<bare>.<sender box>` and keeps the near sessionId."""
import importlib.util
import json
import shutil
import socket
import tempfile
from pathlib import Path

import pytest

BIN = Path(__file__).resolve().parents[1]
SID = "a0b7a784-dbf2-4364-979e-437e652207b1"
CLIENT = "10.0.0.9"


def load(mod_name, file):
    spec = importlib.util.spec_from_file_location(mod_name, BIN / file)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


relay = load("relay_pushed_mirror_t", "unimatrix-relay.py")
roster = load("roster_pushed_mirror_t", "unimatrix-roster.py")

NEAR_ROW = {"unimatrixRemote": "example-operator@machine-a-lan", "unimatrixBox": "box-machine-a", "unimatrixRemoteIp": CLIENT}
RAW = {"name": "group-em-54", "sessionId": SID, "cwd": "/x", "pid": 1, "messagingSocketPath": "/near/s.sock"}


def test_push_label_comes_from_the_far_box_row_not_the_payload(monkeypatch):
    monkeypatch.setattr(relay, "entries", lambda: iter([(7, dict(NEAR_ROW), "k")]))
    assert relay.client_box(CLIENT) == ("box-machine-a", "machine-a-lan")
    assert relay.client_box("10.9.9.9") == (None, "10-9-9-9")
    assert relay.client_box("") == (None, None)


def test_mirror_entry_labels_stamps_box_and_carries_the_near_session_id():
    e = relay.mirror_entry(RAW, "group-em-54", "box-machine-a", "machine-a-lan")
    assert e["name"] == "group-em-54.machine-a-lan"
    assert e["unimatrixBox"] == "box-machine-a"
    assert e["sessionId"] == SID
    assert e[relay.MIRROR_NAME_FIELD] == "group-em-54"
    assert e["unimatrixRemoteSocket"] == "/near/s.sock"


def test_mirror_entry_label_is_idempotent_and_matches_the_pull_form():
    e = relay.mirror_entry(RAW, "group-em-54.machine-a-lan", "box-machine-a", "machine-a-lan")
    assert e["name"] == "group-em-54.machine-a-lan"
    assert e["name"] == roster.labelled("group-em-54", "example-operator@machine-a-lan")


def test_mirror_entry_without_a_known_sender_box_stays_bare():
    e = relay.mirror_entry(RAW, "group-em-54")
    assert e["name"] == "group-em-54" and "unimatrixBox" not in e and relay.MIRROR_NAME_FIELD not in e


def test_pulled_mirror_is_named_with_the_sender_box():
    assert roster.mirror_entry_name({"name": "group-em-54"}, "example-operator@machine-a-lan", "/far/s.sock", scan=[]) \
        == "group-em-54.machine-a-lan"


def test_pushed_mirror_exports_its_registered_name_for_send_named():
    e = relay.mirror_entry(RAW, "group-em-54", "box-machine-a", "machine-a-lan")
    assert relay._exported_name(e) == "group-em-54"


@pytest.fixture
def far(tmp_path, monkeypatch):
    scratch = Path(__file__).resolve().parents[3] / "scratch"
    scratch.mkdir(exist_ok=True)
    root = Path(tempfile.mkdtemp(prefix="pm", dir=scratch))
    if len(str(root / "s" / "unimatrix-group-em-54.sock")) > 100:
        shutil.rmtree(root, ignore_errors=True)
        pytest.skip("repo path too deep for an AF_UNIX socket")
    sess, socks = tmp_path / "sess", root / "s"
    sess.mkdir()

    class Proc:
        stdout = "Thu Nov 12 13:00:00 2026"

    class Helpers:
        local_peer_key = staticmethod(lambda body, ps, domain: f"{body}|{ps}|{domain}")
        write_peer_key = staticmethod(lambda path, value: Path(path).write_text(value, encoding="utf-8"))

    monkeypatch.setattr(relay, "SESS", str(sess))
    monkeypatch.setattr(relay, "SOCKS", str(socks))
    monkeypatch.setattr(relay, "client_address", lambda: CLIENT)
    monkeypatch.setattr(relay, "entries", lambda: iter([(7, dict(NEAR_ROW), "k")]))
    monkeypatch.setattr(relay, "check_name_free", lambda name, client="", own_pid=None: [])
    monkeypatch.setattr(relay, "load_roster", lambda: Helpers)
    monkeypatch.setattr(relay.subprocess, "run", lambda *a, **k: Proc())
    yield sess, socks
    shutil.rmtree(root, ignore_errors=True)


needs_unix = pytest.mark.skipif(not hasattr(socket, "AF_UNIX"), reason="AF_UNIX required")  # no AF_UNIX on win32 python


@needs_unix
def test_registering_a_push_writes_the_labelled_row_under_the_authenticated_box(far):
    sess, socks = far
    m = relay.register_name("group-em-54", dict(RAW), "KEY")
    try:
        row = json.loads(Path(m.entry_path).read_text())
        assert row["name"] == "group-em-54.machine-a-lan"
        assert row["unimatrixBox"] == "box-machine-a"
        assert row["sessionId"] == SID
        assert row["unimatrixClient"] == CLIENT
        assert (socks / "unimatrix-group-em-54.sock").exists()
    finally:
        m.release()


def test_stale_marker_lists_under_the_labelled_name_and_files_under_the_registered_one(far):
    relay.mark_stale("group-em-54", "dead", 4102444800, "box-machine-a")
    marker = json.loads(Path(relay.stale_marker_path("group-em-54")).read_text())
    assert marker["name"] == "group-em-54.machine-a-lan"


@needs_unix
def test_a_client_that_matches_no_far_row_registers_under_its_address_label(far, monkeypatch):
    sess, socks = far
    monkeypatch.setattr(relay, "client_address", lambda: "10.9.9.9")
    m = relay.register_name("group-em-54", dict(RAW), "KEY")
    try:
        row = json.loads(Path(m.entry_path).read_text())
        assert row["name"] == "group-em-54.10-9-9-9" and "unimatrixBox" not in row
    finally:
        m.release()


def test_send_named_accepts_the_shown_name_box_address_and_the_bare_name(monkeypatch):
    monkeypatch.setattr(relay, "_own_box", lambda: "machine-a")
    bare, shown = relay.name_digest("group-em-54"), relay.name_digest("group-em-54.machine-a")
    assert relay.shown_ref("group-em-54") == "group-em-54.machine-a"
    assert relay._name_matches("group-em-54", bare) and relay._name_matches("group-em-54", shown)
    assert relay._name_matches("group-em-54.machine-a", shown)


def test_send_named_refuses_another_session_or_another_box_label(monkeypatch):
    monkeypatch.setattr(relay, "_own_box", lambda: "machine-a")
    assert not relay._name_matches("group-em-54", relay.name_digest("example-retrieval-repo-95.machine-a"))
    assert not relay._name_matches("group-em-54", relay.name_digest("group-em-54.macmini"))
    assert not relay._name_matches(None, relay.name_digest("group-em-54.machine-a"))


def test_unknown_own_box_matches_only_the_bare_name(monkeypatch):
    monkeypatch.setattr(relay, "_own_box", lambda: None)
    assert relay.shown_ref("group-em-54") is None
    assert relay._name_matches("group-em-54", relay.name_digest("group-em-54"))
    assert not relay._name_matches("group-em-54", relay.name_digest("group-em-54.machine-a"))
