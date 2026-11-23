"""A relay send releases the sender before the slow ssh leg; a failed leg still reaches the sender as a notice."""
import hashlib
import importlib.util
import json
import os
import subprocess
import threading
import types
from pathlib import Path

import pytest

BIN = Path(__file__).resolve().parents[1]
SENT = b'{"from": "uds:/s.sock", "message": "hi"}'
RECEIPTED = json.dumps({"ok": True, "digest": hashlib.sha256(SENT).hexdigest()}).encode() + b"\n"


def load_bridge():
    spec = importlib.util.spec_from_file_location("unimatrix_bridge_send_result", BIN / "unimatrix-bridge.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class Conn:
    def __init__(self, events, deliveries) -> None:
        self.chunks = [b'{"from": "uds:/s.sock", "message": "hi"}\n', b""]
        self.events, self.deliveries = events, deliveries
        self.replies = []

    def read(self):
        return self.chunks.pop(0)

    def write(self, data) -> None:
        self.events.append("write")
        self.replies.append(json.loads(data))

    def close(self) -> None:
        rows = self.deliveries.read_text().splitlines() if self.deliveries.exists() else []
        self.events.append(("close", len(rows)))


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    bridge = load_bridge()
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    (sessions / f"{os.getpid()}.json").write_text(json.dumps(
        {"pid": os.getpid(), "name": "far", "messagingSocketPath": "/m.sock", "macSocketPath": "/far/9.sock", "unimatrixRemote": "lan",
         "unimatrixFarName": "far", "sessionId": "sid-far"}))
    monkeypatch.setattr(bridge, "SESSIONS", str(sessions))
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path / "settings"))
    bridge.cfg = types.SimpleNamespace(endpoint="/m.sock", key="k", remote="lan", tunnel_remote=None, port=None)
    bridge.log = open(tmp_path / "b.log", "a", encoding="utf-8")
    monkeypatch.setattr(bridge, "pick_remote", lambda lan, tun: lan)
    mux = bridge._box_mux()
    mux._FAR_PLATFORM.clear()
    far = tmp_path / "settings" / "unimatrix" / "control-far"
    far.mkdir(parents=True)
    (far / "lan.json").write_text(json.dumps({"platform": "linux"}))
    monkeypatch.setattr(mux, "IS_WIN", False)  # one ssh per send; the Windows broker leg is not under test here
    bridge.notices = []
    monkeypatch.setattr(bridge, "notify_sender", lambda line, target, error: bridge.notices.append(error))
    yield bridge, bridge.deliveries_path()
    bridge.log.close()
    mux._FAR_PLATFORM.clear()


def drive(bridge, deliveries, monkeypatch, returncode):
    argvs = []

    def run(argv, **kw):
        argvs.append(argv)
        return subprocess.CompletedProcess(argv, returncode, b"" if returncode else RECEIPTED, b"denied" if returncode else b"")

    monkeypatch.setattr(subprocess, "run", run)
    events = []
    bridge.serve_one(Conn(events, deliveries))
    rows = [json.loads(l) for l in deliveries.read_text().splitlines()]
    return events, rows, argvs


def test_a_mirror_whose_name_disagrees_with_its_far_name_is_refused_naming_name_far_name_and_session(env, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    bridge, deliveries = env
    row_path = Path(bridge.SESSIONS) / f"{os.getpid()}.json"
    row = json.loads(row_path.read_text())
    row.update(name="group-em-54.machine-a", unimatrixFarName="example-game-repo-fc", sessionId="fc000000-0000-4000-8000-000000000003")
    row_path.write_text(json.dumps(row))
    ssh = []
    monkeypatch.setattr(subprocess, "run", lambda argv, **kw: ssh.append(argv))
    conn = Conn([], deliveries)
    bridge.serve_one(conn)
    rows = [json.loads(l) for l in deliveries.read_text().splitlines()]
    assert conn.replies == [{"ok": False, "error": rows[0]["error"]}]  # refused before custody: a synchronous failure
    assert ssh == [] and [r["ok"] for r in rows] == [False]
    for part in ("group-em-54.machine-a", "example-game-repo-fc", "fc000000-0000-4000-8000-000000000003"):
        assert part in rows[0]["error"]
    assert bridge.notices == [rows[0]["error"]]
    assert (Path(bridge.SESSIONS) / f"{os.getpid()}.misroute").exists()


@pytest.mark.parametrize("returncode, ok", [(0, True), (255, False)])
def test_every_send_records_its_outcome_and_a_failure_notifies_the_sender(env, monkeypatch: pytest.MonkeyPatch, returncode, ok) -> None:
    bridge, deliveries = env
    events, rows, _ = drive(bridge, deliveries, monkeypatch, returncode)
    assert events == [("close", 0)]  # custody closes silently, before any verdict row
    assert [r["ok"] for r in rows] == [ok]
    assert isinstance(rows[0]["ms"], int)
    assert ("error" in rows[0]) == (not ok)
    assert bool(bridge.notices) == (not ok)


def test_custody_closes_silently_before_the_ssh_leg_runs_and_writes_nothing(env, monkeypatch: pytest.MonkeyPatch) -> None:
    bridge, deliveries = env
    events = []

    def run(argv, **kw):
        events.append("ssh")
        return subprocess.CompletedProcess(argv, 0, b"", b"")

    monkeypatch.setattr(subprocess, "run", run)
    conn = Conn(events, deliveries)
    bridge.serve_one(conn)
    # The second ssh is the `receipts` reconcile a reply-less exit 0 earns.
    assert events == [("close", 0), "ssh", "ssh"]
    assert conn.replies == []  # silence is acceptance: nothing is written on custody


def test_a_far_leg_slower_than_the_client_deadline_is_closed_silently_and_a_later_failure_still_lands(env, monkeypatch: pytest.MonkeyPatch) -> None:
    bridge, deliveries = env
    answered, release = threading.Event(), threading.Event()

    class SlowConn(Conn):
        def close(self) -> None:
            super().close()
            answered.set()

    def run(argv, **kw):
        assert release.wait(10)  # the far leg outlasts the client's wait window
        return subprocess.CompletedProcess(argv, 255, b"", b"Connection timed out")

    monkeypatch.setattr(subprocess, "run", run)
    conn = SlowConn([], deliveries)
    worker = threading.Thread(target=bridge.serve_one, args=(conn,))
    worker.start()
    try:
        assert answered.wait(5), "client verdict waited on the far ssh leg"
        assert conn.replies == []
        assert not deliveries.exists() and bridge.notices == []
    finally:
        release.set()
        worker.join(10)
    rows = [json.loads(l) for l in deliveries.read_text().splitlines()]
    assert [r["ok"] for r in rows] == [False] and "Connection timed out" in rows[0]["error"]
    assert bridge.notices == [rows[0]["error"]]
    assert conn.replies == []  # the failure is never an answer


def test_ssh_argv_multiplexes_off_windows_only(env, monkeypatch: pytest.MonkeyPatch) -> None:
    bridge, deliveries = env
    mux = bridge._box_mux()
    roster = mux._roster()
    monkeypatch.setattr(roster, "_is_win", lambda: False)
    _, _, argvs = drive(bridge, deliveries, monkeypatch, 0)
    assert "ControlMaster=auto" in argvs[-1] and argvs[-1][-2] == "lan" and argvs[-1][-1].startswith("send-session 9 ") and argvs[-1][-1].endswith(" sid-far")
    monkeypatch.setattr(roster, "_is_win", lambda: True)
    _, _, argvs = drive(bridge, deliveries, monkeypatch, 0)
    assert not any("Control" in a for a in argvs[-1])


def test_far_relay_lacking_send_session_is_a_failed_outcome_the_sender_can_read(env, monkeypatch: pytest.MonkeyPatch) -> None:
    bridge, deliveries = env

    def run(argv, **kw):
        return subprocess.CompletedProcess(argv, 2, b"", b"usage: unimatrix-relay.py ...")

    monkeypatch.setattr(subprocess, "run", run)
    bridge.serve_one(Conn([], deliveries))
    rows = [json.loads(l) for l in deliveries.read_text().splitlines()]
    assert [r["ok"] for r in rows] == [False]
    assert rows[0]["error"] == "far relay lacks send-session; upgrade it (version skew)"
    assert rows[0]["target"].startswith("far (pid ") and isinstance(rows[0]["at"], float)
    assert bridge.notices == ["far relay lacks send-session; upgrade it (version skew)"]


def test_misdelivery_notice_names_the_reached_session_and_never_says_not_delivered(env, monkeypatch: pytest.MonkeyPatch) -> None:
    bridge, _ = env
    frames = []
    monkeypatch.setattr(bridge, "write_local", lambda endpoint, frame: frames.append(json.loads(frame)["message"]))
    bridge.notify_misdelivered(SENT, "far.lan (far)", "addon-38", "Machine-b")
    assert frames == ["Misdelivered: your message to far.lan (far) was written to addon-38 on Machine-b, not the addressed session."]


def test_queued_reply_is_an_accepted_delivery_acked_as_queued_never_a_failure(env, monkeypatch: pytest.MonkeyPatch) -> None:
    bridge, deliveries = env
    told = []
    monkeypatch.setattr(bridge, "_tell_sender", lambda line, text: told.append(text))
    queued = json.dumps({"ok": True, "queued": True, "digest": hashlib.sha256(SENT).hexdigest(),
                         "reached": "far", "reached_box": "Machine-b"}).encode() + b"\n"
    monkeypatch.setattr(subprocess, "run", lambda argv, **kw: subprocess.CompletedProcess(argv, 0, queued, b""))
    bridge.serve_one(Conn([], deliveries))
    rows = [json.loads(l) for l in deliveries.read_text().splitlines()]
    assert [r["ok"] for r in rows] == [True] and rows[0]["queued"] is True and "error" not in rows[0]
    assert bridge.notices == []
    assert told == ["Queued for far on Machine-b; delivered when its reader drains."]


def test_a_delivered_reply_is_not_marked_queued_and_a_refused_one_still_fails(env, monkeypatch: pytest.MonkeyPatch) -> None:
    bridge, deliveries = env
    told = []
    monkeypatch.setattr(bridge, "_tell_sender", lambda line, text: told.append(text))
    monkeypatch.setattr(subprocess, "run", lambda argv, **kw: subprocess.CompletedProcess(argv, 0, RECEIPTED, b""))
    bridge.serve_one(Conn([], deliveries))
    refused = b'{"ok": false, "queued": true, "error": "TimeoutError: x; queue QueueFull: full"}\n'
    monkeypatch.setattr(subprocess, "run", lambda argv, **kw: subprocess.CompletedProcess(argv, 0, refused, b""))
    bridge.serve_one(Conn([], deliveries))
    rows = [json.loads(l) for l in deliveries.read_text().splitlines()]
    assert [r["ok"] for r in rows] == [True, False] and "queued" not in rows[0] and "queued" not in rows[1]
    assert not any(t.startswith("Queued") for t in told) and len(bridge.notices) == 1


@pytest.mark.parametrize("stdout", [
    b'{"ok": true}\n',
    b'{"ok": true, "digest": "deadbeef"}\n',
])
def test_ok_reply_without_the_matching_far_receipt_is_unconfirmed_and_notifies(env, monkeypatch: pytest.MonkeyPatch, stdout) -> None:
    bridge, deliveries = env
    monkeypatch.setattr(subprocess, "run", lambda argv, **kw: subprocess.CompletedProcess(argv, 0, stdout, b""))
    bridge.serve_one(Conn([], deliveries))
    rows = [json.loads(l) for l in deliveries.read_text().splitlines()]
    assert [r["ok"] for r in rows] == [False]
    assert rows[0]["error"] == "unconfirmed: no far receipt for this line"
    assert bridge.notices == ["unconfirmed: no far receipt for this line"]
