"""A far-box mirror never carries one far session's name while its pipe answers as another, and one far session is never mirrored twice.

The crossed state: a mirror named for session A is bridged to the pipe of session B (which just
re-registered, its row carrying no session id), beside a mirror of A held by another spelling of the
same box (`-lan` vs `.local`). One roster sync retires the crossed mirror and mirrors A no second time.
"""
import importlib.util
import json
import os
from pathlib import Path

import pytest

BIN = Path(__file__).resolve().parents[1]
PIPE_A, PIPE_B = r"\\.\pipe\LOCAL\cc-msg-9c327d27", r"\\.\pipe\LOCAL\cc-msg-a7c4697f"
SID_A = "639e3e00-0000-4000-8000-000000000001"
SID_B = "a7c4697f-0000-4000-8000-000000000002"
NEAR, TWIN = "example-operator@machine-a.local", "example-operator@machine-a-lan"


def load_roster():
    spec = importlib.util.spec_from_file_location("unimatrix_roster_crossed", BIN / "unimatrix-roster.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class FakeProc:
    def __init__(self, pid):
        self.pid = pid

    def poll(self):
        return None


@pytest.fixture
def env(tmp_path, monkeypatch):
    roster = load_roster()
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    killed, built = [], []
    monkeypatch.setattr(roster, "sessions_dir", lambda: str(sessions))
    monkeypatch.setattr(roster, "tree_kill", killed.append)
    monkeypatch.setattr(roster, "remote_ip", lambda remote: "")
    monkeypatch.setattr(roster, "mirror", lambda row, cfg, say, scan=None: built.append(row["entry"]["name"]) or FakeProc(70))
    cfg = roster.parse_args(["bridge.log", "--remote", NEAR, "--key", "k"])
    return roster, sessions, killed, built, cfg


def put_mirror(roster, sessions, fname, remote, name, far_name, sid, sock, far_pid):
    """A live, supervised, recently seen mirror record: the shape `listed_row` lists."""
    me = os.getpid()
    d = {"pid": me, "procStart": roster.proc_start(me), "name": name, "unimatrixFarName": far_name,
         "sessionId": sid, "farPid": far_pid, "unimatrixRemote": remote, roster.FAR_SOCKET_FIELD: sock,
         "unimatrixBox": "machine-a", **roster.supervisor_fields(), **roster.far_seen_fields()}
    (sessions / fname).write_text(json.dumps(d))


def far_row(name, pid, sock, sid=None):
    entry = {"pid": pid, "name": name, "messagingSocketPath": sock}
    if sid:
        entry["sessionId"] = sid
    return {"entry": entry, "key": "K"}


def listed_mirrors(roster):
    rows = roster.session_entries()
    return [d for d in rows if d.get(roster.FAR_SOCKET_FIELD) and roster.listed_row(d, rows)]


def crossed_state(roster, sessions):
    put_mirror(roster, sessions, "60.json", NEAR, "group-em-54.machine-a.local", "group-em-54", SID_A, PIPE_B, 55)
    put_mirror(roster, sessions, "61.json", TWIN, "group-em-54.machine-a-lan", "group-em-54", SID_A, PIPE_A, 55)
    rows = {PIPE_A: far_row("group-em-54", 55, PIPE_A, SID_A), PIPE_B: far_row("claude-klabauter-07", 77, PIPE_B)}
    return {PIPE_B: FakeProc(60)}, rows


def test_one_sync_retires_the_mirror_named_for_a_to_bs_pipe_and_leaves_one_mirror_of_a(env):
    roster, sessions, killed, built, cfg = env
    bridges, rows = crossed_state(roster, sessions)
    assert len([d for d in listed_mirrors(roster) if d["sessionId"] == SID_A]) == 2
    lines = []
    roster.sync(bridges, rows, cfg, lines.append)
    assert killed == [60] and bridges == {}
    assert not (sessions / "60.json").exists()
    assert any(m.startswith("MISROUTE group-em-54.machine-a.local") for m in lines)
    assert built == []
    mirrors_of_a = [d for d in listed_mirrors(roster) if d["sessionId"] == SID_A]
    assert [(d["name"], d[roster.FAR_SOCKET_FIELD]) for d in mirrors_of_a] == [("group-em-54.machine-a-lan", PIPE_A)]


def test_a_far_row_that_reregisters_cannot_rebind_the_mirrors_name_to_its_pipe(env):
    roster, sessions, killed, built, cfg = env
    put_mirror(roster, sessions, "60.json", NEAR, "group-em-54.machine-a.local", "group-em-54", SID_A, PIPE_B, 55)
    roster.rename_live_mirror(60, PIPE_B, far_row("claude-klabauter-07", 77, PIPE_B), cfg, lambda m: None)
    d = json.loads((sessions / "60.json").read_text())
    assert d["name"] == "group-em-54.machine-a.local" and d["unimatrixFarName"] == "group-em-54"
    assert roster.far_identity_crossed(d, far_row("claude-klabauter-07", 77, PIPE_B), PIPE_B)


def test_a_mirror_bridged_under_another_far_socket_than_it_records_is_crossed(env):
    roster, sessions, killed, built, cfg = env
    put_mirror(roster, sessions, "60.json", NEAR, "claude-klabauter-07.machine-a.local", "claude-klabauter-07", SID_B, PIPE_A, 77)
    d = json.loads((sessions / "60.json").read_text())
    assert roster.far_identity_crossed(d, far_row("claude-klabauter-07", 77, PIPE_B, SID_B), PIPE_B)
    assert not roster.far_identity_crossed(d, far_row("claude-klabauter-07", 77, PIPE_A, SID_B), PIPE_A)


def test_a_rename_the_far_session_id_vouches_for_still_follows_and_is_not_crossed(env):
    roster, sessions, killed, built, cfg = env
    put_mirror(roster, sessions, "60.json", NEAR, "old-name.machine-a.local", "old-name", SID_A, PIPE_A, 55)
    row = far_row("new-name", 55, PIPE_A, SID_A)
    roster.sync({PIPE_A: FakeProc(60)}, {PIPE_A: row}, cfg, lambda m: None)
    d = json.loads((sessions / "60.json").read_text())
    assert killed == [] and d["unimatrixFarName"] == "new-name" and d["name"] == "new-name.machine-a.local"


def test_a_mirror_whose_name_and_row_agree_is_left_alone(env):
    roster, sessions, killed, built, cfg = env
    put_mirror(roster, sessions, "60.json", NEAR, "claude-klabauter-07.machine-a.local", "claude-klabauter-07", SID_B, PIPE_B, 77)
    lines = []
    roster.sync({PIPE_B: FakeProc(60)}, {PIPE_B: far_row("claude-klabauter-07", 77, PIPE_B, SID_B)}, cfg, lines.append)
    assert killed == [] and not any("MISROUTE" in m for m in lines)


@pytest.mark.parametrize("alias", ["example-operator@machine-a-lan", "example-operator@Machine-a.local", "machine-a", "machine-a-lan.local"])
def test_every_spelling_of_one_box_shares_a_box_key(alias):
    assert load_roster().box_key(alias) == "machine-a"


@pytest.mark.parametrize("alias, key", [("lan", "lan"), ("example-operator@-lan", "-lan"), ("10.0.0.7", "10.0.0.7"), ("fe80::1", "fe80::1")])
def test_box_key_keeps_addresses_and_a_bare_lan_label_whole(alias, key):
    assert load_roster().box_key(alias) == key
