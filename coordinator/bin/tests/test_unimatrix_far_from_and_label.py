"""A far `from=` is never left as the far /tmp/cc-socks path, and a mirror's session name carries its box."""
import importlib.util
import json
from pathlib import Path

import pytest

BIN = Path(__file__).resolve().parents[1]
FAR_SOCK = "/tmp/cc-socks/4242.sock"
FAR_CC_MSG = "/tmp/cc-socks/cc-msg-" + "f" * 32 + ".sock"
WIN_PIPE = "\\\\.\\pipe\\LOCAL\\cc-msg-" + "a" * 32
POSIX_EP = "/tmp/cc-socks/cc-msg-" + "b" * 32 + ".sock"


def load(name):
    spec = importlib.util.spec_from_file_location(f"unimatrix_{name}", BIN / f"unimatrix-{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def frame_from(src, name="far-1"):
    content = f'<cross-session-message from="uds:{src}" from-name="{name}">\nhi\n</cross-session-message>'
    return {"from": "uds:" + src, "message": {"role": "user", "content": content}}


def localized(inbox, frame, mirrors, box="boxlan"):
    inbox._mirrors = lambda: iter(mirrors)
    return json.loads(inbox.localize(frame, b"raw", box))


@pytest.mark.parametrize("endpoint", [WIN_PIPE, POSIX_EP])
def test_unmatched_socket_resolves_to_the_mirror_of_the_same_named_session(endpoint):
    inbox = load("inbox")
    mirror = {"name": "far-1@boxlan", "macSocketPath": FAR_SOCK, "messagingSocketPath": endpoint}
    out = localized(inbox, frame_from(FAR_CC_MSG), [mirror])
    assert out["from"] == "uds:" + endpoint
    assert f'from="uds:{endpoint}"' in out["message"]["content"]
    assert "/tmp/cc-socks/cc-msg-" + "f" * 32 not in json.dumps(out)


def test_name_match_is_per_box():
    inbox = load("inbox")
    other = {"name": "far-1@otherbox", "macSocketPath": FAR_SOCK, "messagingSocketPath": POSIX_EP}
    out = localized(inbox, frame_from(FAR_CC_MSG), [other])
    assert out["from"] == "far-1@boxlan"


def test_no_resolvable_endpoint_leaves_a_name_at_box_label_never_the_far_path():
    inbox = load("inbox")
    out = localized(inbox, frame_from(FAR_CC_MSG), [])
    assert out["from"] == "far-1@boxlan"
    assert 'from="far-1@boxlan"' in out["message"]["content"]
    assert "cc-socks" not in json.dumps(out)


def test_frame_without_from_name_or_box_is_untouched():
    inbox = load("inbox")
    bare = {"from": "uds:" + FAR_CC_MSG, "message": {"content": "no attrs"}}
    inbox._mirrors = lambda: iter([])
    assert inbox.localize(bare, b"raw", "boxlan") == b"raw"
    assert inbox.localize(frame_from(FAR_CC_MSG), b"raw") == b"raw"


@pytest.fixture
def roster_env(tmp_path, monkeypatch):
    roster = load("roster")
    sessions = tmp_path / "sessions"
    sessions.mkdir()

    class P:
        pid = 31337

    monkeypatch.setattr(roster, "sessions_dir", lambda: str(sessions))
    monkeypatch.setattr(roster, "spawn", lambda cmd: P())
    monkeypatch.setattr(roster, "proc_start", lambda pid, platform=None: "1")
    monkeypatch.setattr(roster.time, "sleep", lambda s: None)
    return roster, sessions


@pytest.mark.parametrize("win,endpoint", [(True, WIN_PIPE), (False, POSIX_EP)])
@pytest.mark.parametrize("remote,box", [("boxlan", "boxlan"), ("me@boxlan", "boxlan")])
def test_mirror_session_name_ends_in_at_box(roster_env, monkeypatch, tmp_path, win, endpoint, remote, box):
    roster, sessions = roster_env
    monkeypatch.setattr(roster, "_is_win", lambda platform=None: win)
    monkeypatch.setattr(roster, "local_endpoint", lambda sock, platform=None: endpoint)
    if not win:
        monkeypatch.setattr(roster.os, "makedirs", lambda *a, **k: None)
    cfg = roster.parse_args([str(tmp_path / "b.log"), "--remote", remote, "--key", "k"])
    row = {"entry": {"pid": 4242, "name": "far-1", "messagingSocketPath": FAR_SOCK}, "key": "K"}
    roster.mirror(row, cfg, lambda m: None)
    entry = json.loads((sessions / "31337.json").read_text())
    assert entry["name"] == f"far-1@{box}"
    assert entry["macSocketPath"] == FAR_SOCK


def test_remirroring_a_labelled_name_never_stacks_the_label(roster_env, monkeypatch, tmp_path):
    roster, sessions = roster_env
    monkeypatch.setattr(roster, "_is_win", lambda platform=None: True)
    monkeypatch.setattr(roster, "local_endpoint", lambda sock, platform=None: WIN_PIPE)
    cfg = roster.parse_args([str(tmp_path / "b.log"), "--remote", "boxlan", "--key", "k"])
    row = {"entry": {"pid": 4242, "name": "far-1@boxlan", "messagingSocketPath": FAR_SOCK}, "key": "K"}
    roster.mirror(row, cfg, lambda m: None)
    assert json.loads((sessions / "31337.json").read_text())["name"] == "far-1@boxlan"


def test_labelled_mirror_still_suppresses_a_duplicate_of_its_session(roster_env, monkeypatch):
    roster, sessions = roster_env
    monkeypatch.setattr(roster, "pid_alive", lambda pid: True)
    (sessions / "50.json").write_text(json.dumps(
        {"pid": 50, "name": "far-1@boxlan", "unimatrixMirror": True, "macSocketPath": FAR_SOCK}))
    assert roster.inbound_names() == {"far-1"}
    rows = [{"entry": {"pid": 1, "name": "far-1", "messagingSocketPath": "/tmp/cc-socks/1.sock"}},
            {"entry": {"pid": 2, "name": "far-2", "messagingSocketPath": "/tmp/cc-socks/2.sock"}}]
    assert list(roster.mirrorable(rows, set(), roster.inbound_names())) == ["/tmp/cc-socks/2.sock"]
    labelled_row = [{"entry": {"pid": 1, "name": "far-1@boxlan", "messagingSocketPath": "/tmp/cc-socks/1.sock"}}]
    assert roster.mirrorable(labelled_row, set(), {"far-1"}) == {}
