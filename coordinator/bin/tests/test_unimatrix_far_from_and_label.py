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
    mirror = {"name": "far-1.boxlan", "macSocketPath": FAR_SOCK, "messagingSocketPath": endpoint}
    out = localized(inbox, frame_from(FAR_CC_MSG), [mirror])
    assert out["from"] == "uds:" + endpoint
    assert f'from="uds:{endpoint}"' in out["message"]["content"]
    assert "/tmp/cc-socks/cc-msg-" + "f" * 32 not in json.dumps(out)


def test_name_match_is_per_box():
    inbox = load("inbox")
    other = {"name": "far-1.otherbox", "macSocketPath": FAR_SOCK, "messagingSocketPath": POSIX_EP}
    out = localized(inbox, frame_from(FAR_CC_MSG), [other])
    assert out["from"] == "far-1.boxlan"


def test_no_resolvable_endpoint_leaves_a_name_at_box_label_never_the_far_path():
    inbox = load("inbox")
    out = localized(inbox, frame_from(FAR_CC_MSG), [])
    assert out["from"] == "far-1.boxlan"
    assert 'from="far-1.boxlan"' in out["message"]["content"]
    assert "cc-socks" not in json.dumps(out)


def test_frame_without_from_name_or_box_is_untouched():
    inbox = load("inbox")
    bare = {"from": "uds:" + FAR_CC_MSG, "message": {"content": "no attrs"}}
    inbox._mirrors = lambda: iter([])
    assert inbox.localize(bare, b"raw", "boxlan") == b"raw"
    assert inbox.localize(frame_from(FAR_CC_MSG), b"raw") == b"raw"


@pytest.mark.parametrize("remote", ["boxlan", "me@boxlan", "me@192.168.0.175"])
def test_collision_label_has_no_at_and_bare_name_recovers_a_hyphenated_session_name(remote):
    roster = load("roster")
    label = roster.labelled("example-retrieval-repo-8c", remote)
    assert "@" not in label
    assert label.endswith(remote.rpartition("@")[2])
    assert roster.bare_name(label) == "example-retrieval-repo-8c"
    assert roster.labelled(label, remote) == label
    assert roster.bare_name("example-retrieval-repo-8c") == "example-retrieval-repo-8c"


def test_inbox_localize_label_equals_roster_label():
    roster, inbox = load("roster"), load("inbox")
    out = localized(inbox, frame_from(FAR_CC_MSG, "example-retrieval-repo-8c"), [], box="192.168.0.175")
    assert out["from"] == roster.labelled("example-retrieval-repo-8c", "me@192.168.0.175")


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
def test_mirror_session_name_is_bare_without_a_collision_and_boxed_with_one(roster_env, monkeypatch, tmp_path, win, endpoint, remote, box):
    roster, sessions = roster_env
    monkeypatch.setattr(roster, "_is_win", lambda platform=None: win)
    monkeypatch.setattr(roster, "local_endpoint", lambda sock, platform=None: endpoint)
    monkeypatch.setattr(roster, "pid_alive", lambda pid: True)
    if not win:
        monkeypatch.setattr(roster.os, "makedirs", lambda *a, **k: None)
    cfg = roster.parse_args([str(tmp_path / "b.log"), "--remote", remote, "--key", "k"])
    row = {"entry": {"pid": 4242, "name": "far-1", "messagingSocketPath": FAR_SOCK}, "key": "K"}
    roster.mirror(row, cfg, lambda m: None)
    entry = json.loads((sessions / "31337.json").read_text())
    assert entry["name"] == "far-1"
    assert entry["macSocketPath"] == FAR_SOCK

    (sessions / "31337.json").unlink()
    (sessions / "77.json").write_text(json.dumps({"pid": 77, "name": "far-1"}))
    roster.mirror(row, cfg, lambda m: None)
    assert json.loads((sessions / "31337.json").read_text())["name"] == f"far-1.{box}"


def _mirror_once(roster, monkeypatch, tmp_path, name):
    monkeypatch.setattr(roster, "_is_win", lambda platform=None: True)
    monkeypatch.setattr(roster, "local_endpoint", lambda sock, platform=None: WIN_PIPE)
    monkeypatch.setattr(roster, "pid_alive", lambda pid: True)
    cfg = roster.parse_args([str(tmp_path / "b.log"), "--remote", "boxlan", "--key", "k"])
    roster.mirror({"entry": {"pid": 4242, "name": name, "messagingSocketPath": FAR_SOCK}, "key": "K"}, cfg, lambda m: None)


def test_remirroring_a_labelled_name_never_stacks_the_label(roster_env, monkeypatch, tmp_path):
    roster, sessions = roster_env
    (sessions / "77.json").write_text(json.dumps({"pid": 77, "name": "far-1"}))
    _mirror_once(roster, monkeypatch, tmp_path, "far-1.boxlan")
    assert json.loads((sessions / "31337.json").read_text())["name"] == "far-1.boxlan"


def test_a_stale_mirror_of_the_same_far_session_is_not_a_collision(roster_env, monkeypatch, tmp_path):
    roster, sessions = roster_env
    (sessions / "88.json").write_text(json.dumps({"pid": 88, "name": "far-1", "macSocketPath": FAR_SOCK}))
    _mirror_once(roster, monkeypatch, tmp_path, "far-1")
    assert json.loads((sessions / "31337.json").read_text())["name"] == "far-1"


def test_a_dead_session_with_the_same_name_is_not_a_collision(roster_env, monkeypatch, tmp_path):
    roster, sessions = roster_env
    (sessions / "99.json").write_text(json.dumps({"pid": 99, "name": "far-1"}))
    monkeypatch.setattr(roster, "_is_win", lambda platform=None: True)
    monkeypatch.setattr(roster, "local_endpoint", lambda sock, platform=None: WIN_PIPE)
    monkeypatch.setattr(roster, "pid_alive", lambda pid: pid != 99)
    cfg = roster.parse_args([str(tmp_path / "b.log"), "--remote", "boxlan", "--key", "k"])
    roster.mirror({"entry": {"pid": 4242, "name": "far-1", "messagingSocketPath": FAR_SOCK}, "key": "K"}, cfg, lambda m: None)
    assert json.loads((sessions / "31337.json").read_text())["name"] == "far-1"


def test_a_mirror_on_another_box_with_the_same_name_forces_labels(roster_env, monkeypatch, tmp_path):
    roster, sessions = roster_env
    (sessions / "66.json").write_text(json.dumps(
        {"pid": 66, "name": "far-1", "macSocketPath": "/tmp/cc-socks/other.sock", "unimatrixMirror": True}))
    _mirror_once(roster, monkeypatch, tmp_path, "far-1")
    assert json.loads((sessions / "31337.json").read_text())["name"] == "far-1.boxlan"


def test_labelled_mirror_still_suppresses_a_duplicate_of_its_session(roster_env, monkeypatch):
    roster, sessions = roster_env
    monkeypatch.setattr(roster, "pid_alive", lambda pid: True)
    (sessions / "50.json").write_text(json.dumps(
        {"pid": 50, "name": "far-1.boxlan", "unimatrixMirror": True, "macSocketPath": FAR_SOCK}))
    assert roster.inbound_names() == {"far-1"}
    rows = [{"entry": {"pid": 1, "name": "far-1", "messagingSocketPath": "/tmp/cc-socks/1.sock"}},
            {"entry": {"pid": 2, "name": "far-2", "messagingSocketPath": "/tmp/cc-socks/2.sock"}}]
    assert list(roster.mirrorable(rows, set(), roster.inbound_names())) == ["/tmp/cc-socks/2.sock"]
    labelled_row = [{"entry": {"pid": 1, "name": "far-1.boxlan", "messagingSocketPath": "/tmp/cc-socks/1.sock"}}]
    assert roster.mirrorable(labelled_row, set(), {"far-1"}) == {}


def test_sync_relabels_a_live_mirror_when_a_local_session_takes_its_bare_name_and_unlabels_when_it_leaves(roster_env, monkeypatch, tmp_path):
    roster, sessions = roster_env
    monkeypatch.setattr(roster, "pid_alive", lambda pid: pid != 99)
    cfg = roster.parse_args([str(tmp_path / "b.log"), "--remote", "boxlan", "--key", "k"])

    class Live:
        pid = 31337

        def poll(self):
            return None

    (sessions / "31337.json").write_text(json.dumps(
        {"pid": 31337, "name": "far-1", "macSocketPath": FAR_SOCK, "unimatrixMirror": True}))
    rows = {FAR_SOCK: {"entry": {"pid": 4242, "name": "far-1", "messagingSocketPath": FAR_SOCK}, "key": "K"}}
    bridges = {FAR_SOCK: Live()}
    name = lambda: json.loads((sessions / "31337.json").read_text())["name"]

    roster.sync(bridges, rows, cfg, lambda m: None)
    assert name() == "far-1"
    (sessions / "77.json").write_text(json.dumps({"pid": 77, "name": "far-1"}))
    roster.sync(bridges, rows, cfg, lambda m: None)
    assert name() == "far-1.boxlan"
    (sessions / "77.json").write_text(json.dumps({"pid": 99, "name": "far-1"}))
    roster.sync(bridges, rows, cfg, lambda m: None)
    assert name() == "far-1"


def test_a_far_name_with_a_foreign_dot_is_hyphenated_and_this_boxs_label_is_kept(roster_env, monkeypatch):
    roster, sessions = roster_env
    monkeypatch.setattr(roster, "pid_alive", lambda pid: True)
    assert roster.mirror_name("far.1", "boxlan") == "far-1"
    assert roster.mirror_name("far-1.boxlan", "boxlan") == "far-1"
    (sessions / "77.json").write_text(json.dumps({"pid": 77, "name": "far-1"}))
    assert roster.mirror_name("far.1", "boxlan") == "far-1.boxlan"


def test_sync_scans_the_sessions_dir_once_per_pass_and_a_new_mirror_counts_as_a_taken_name(roster_env, monkeypatch, tmp_path):
    roster, sessions = roster_env
    monkeypatch.setattr(roster, "pid_alive", lambda pid: True)
    cfg = roster.parse_args([str(tmp_path / "b.log"), "--remote", "boxlan", "--key", "k"])
    scans = []
    real = roster.live_sessions
    monkeypatch.setattr(roster, "live_sessions", lambda: scans.append(1) or real())
    names = {}

    class Live:
        def __init__(self, pid):
            self.pid = pid

        def poll(self):
            return None

    socks = ["/tmp/cc-socks/a.sock", "/tmp/cc-socks/b.sock"]
    rows = {}
    bridges = {}
    for i, s in enumerate(socks):
        (sessions / f"{500 + i}.json").write_text(json.dumps(
            {"pid": 500 + i, "name": "far-1", "macSocketPath": s, "unimatrixMirror": True}))
        rows[s] = {"entry": {"pid": 40 + i, "name": "far-1", "messagingSocketPath": s}, "key": "K"}
        bridges[s] = Live(500 + i)
    roster.sync(bridges, rows, cfg, lambda m: None)
    assert len(scans) == 1
