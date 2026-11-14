"""A cross-box mirror entry carries the far session's liveness stamps, not the face's own activity."""
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

BIN = Path(__file__).resolve().parents[1]
FAR = "/tmp/cc-socks/cc-msg-" + "a" * 32 + ".sock"


@pytest.fixture
def roster():
    spec = importlib.util.spec_from_file_location("unimatrix_roster_bare", BIN / "unimatrix-roster.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def far_entry(**extra):
    return {"pid": 7, "name": "example-game-workbench-repo-78", "messagingSocketPath": FAR, **extra}


NAME = "example-game-workbench-repo-78"
OLD = "/tmp/cc-socks/cc-msg-" + "b" * 32 + ".sock"


def test_a_faces_own_earlier_entries_never_force_the_label(roster):
    remote = "me@macmini-lan"
    key = roster.mirror_scan_key(remote, FAR)
    scans = [
        [(NAME, roster.mirror_scan_key("me@MacMini-LAN.local", FAR))],  # same session, another alias
        [(NAME, roster.mirror_scan_key(remote, OLD))],  # pre-restart socket of the same session
        [(NAME, key)],  # itself
    ]
    for scan in scans:
        assert roster.mirror_name(NAME, remote, key, scan) == NAME


def test_a_real_competitor_still_forces_the_label(roster):
    remote = "me@macmini-lan"
    key = roster.mirror_scan_key(remote, FAR)
    for scan in ([(NAME, "/tmp/local.sock")], [(NAME, roster.mirror_scan_key("me@otherbox", OLD))]):
        assert roster.mirror_name(NAME, remote, key, scan) == f"{NAME}.macmini-lan"


def test_the_mirror_entry_carries_the_far_sessions_refreshed_liveness(roster, tmp_path, monkeypatch):
    sess = tmp_path / "sessions"
    sess.mkdir()
    monkeypatch.setattr(roster, "sessions_dir", lambda: str(sess))
    monkeypatch.setattr(roster, "remote_ip", lambda remote: "")
    monkeypatch.setattr(roster, "refresh_mirror_key", lambda *a: None)
    cfg = SimpleNamespace(remote="me@macmini-lan")
    mirror = {"pid": 31337, "name": "example-game-workbench-repo-78", "unimatrixFarName": "example-game-workbench-repo-78",
              "updatedAt": 1000, "statusUpdatedAt": 1000, "status": "idle", "farPid": 7,
              "unimatrixRemote": cfg.remote, "unimatrixFarSeen": 10**13, "box": "macmini-lan",
              "unimatrixBox": "macmini-lan", "vendor": "claude", "cwd": "",
              **roster.supervisor_fields()}
    (sess / "31337.json").write_text(json.dumps(mirror))
    row = {"entry": far_entry(updatedAt=5000, statusUpdatedAt=5000, status="busy"), "key": "k"}
    roster.rename_live_mirror(31337, FAR, row, cfg, lambda m: None, scan=[])
    after = json.loads((sess / "31337.json").read_text())
    assert (after["updatedAt"], after["statusUpdatedAt"], after["status"]) == (5000, 5000, "busy")
