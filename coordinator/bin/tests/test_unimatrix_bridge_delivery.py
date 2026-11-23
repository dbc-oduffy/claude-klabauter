"""Relay send failure: sender inbox notice and the delivery status record (ssh stubbed, tmp_path only)."""
import hashlib
import importlib.util
import json
import os
import re
import subprocess
import types
from pathlib import Path

import pytest

BRIDGE = Path(__file__).resolve().parents[1] / "unimatrix-bridge.py"
RELAY = BRIDGE.with_name("unimatrix-relay.py")
MIRROR = "/tmp/cc-socks/mirror.sock"
SENDER = "/tmp/cc-socks/sender.sock"
FAR_SID = "5a1f0c2e-1111-4222-8333-444455556666"
SEND_SESSION = rf"send-session 4242 [0-9a-f]{{16}} [0-9a-f]{{16}} {FAR_SID}"


class FakeConn:
    def __init__(self, data):
        self.chunks = [data, b""]

    def read(self):
        return self.chunks.pop(0)

    def close(self):
        pass


@pytest.fixture
def bridge(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("unimatrix_bridge_delivery", BRIDGE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    (sessions / f"{os.getpid()}.json").write_text(json.dumps(
        {"pid": os.getpid(), "messagingSocketPath": MIRROR, "macSocketPath": "/far/4242.sock", "name": "far-session",
         "unimatrixFarName": "far-session", "sessionId": FAR_SID}))
    monkeypatch.setattr(mod, "SESSIONS", str(sessions))
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path / "settings"))
    monkeypatch.setattr(mod._box_mux(), "IS_WIN", False)  # these tests stub subprocess.run, the direct channel's spawn
    mod.cfg = types.SimpleNamespace(endpoint=MIRROR, key="k", remote="farbox", port=None)
    mod.log = open(tmp_path / "bridge.log", "a", encoding="utf-8")
    mod.written = []
    monkeypatch.setattr(mod, "write_local", lambda ep, data: mod.written.append((ep, data)))
    return mod


def test_supervisor_argument_is_parsed_and_optional(bridge):
    assert bridge.parse_args(["ep", "relay", "b.log", "--remote", "r", "--key", "k", "--supervisor", "77"]).supervisor == 77
    assert bridge.parse_args(["ep", "relay", "b.log", "--remote", "r", "--key", "k"]).supervisor is None


def test_bridge_exits_and_drops_its_files_when_the_supervisor_is_gone(bridge, tmp_path, monkeypatch):
    sessions = Path(bridge.SESSIONS)
    me = os.getpid()
    (sessions / f"{me}.json").write_text(json.dumps({"pid": me, "unimatrixRosterProcStart": "boot-1"}))
    (sessions / f"{me}.abc.key").write_text("k")
    (sessions / "99999999.json").write_text("{}")
    monkeypatch.setattr(bridge, "IS_WIN", True)
    bridge.cfg = types.SimpleNamespace(endpoint=MIRROR, supervisor=4321)
    lv = bridge._roster()._liveness()
    monkeypatch.setattr(lv, "pid_alive", lambda p: True)
    monkeypatch.setattr(lv, "proc_start", lambda p, platform=None: "boot-1")
    assert bridge.supervisor_gone() is False
    monkeypatch.setattr(lv, "proc_start", lambda p, platform=None: "recycled")
    assert bridge.supervisor_gone() is True
    monkeypatch.setattr(lv, "pid_alive", lambda p: False)
    assert bridge.supervisor_gone() is True
    bridge.drop_own_files()
    assert sorted(p.name for p in sessions.iterdir()) == ["99999999.json"]


def test_watcher_exits_the_process_once_the_supervisor_is_gone(bridge, monkeypatch):
    exits = []

    class Stop(Exception):
        pass

    def fake_exit(code):
        exits.append(code)
        raise Stop

    bridge.cfg = types.SimpleNamespace(endpoint=MIRROR, supervisor=4321)
    monkeypatch.setattr(bridge, "IS_WIN", True)
    monkeypatch.setattr(bridge.time, "sleep", lambda s: None)
    monkeypatch.setattr(bridge, "supervisor_gone", lambda: True)
    monkeypatch.setattr(bridge.os, "_exit", fake_exit)
    with pytest.raises(Stop):
        bridge.watch_supervisor()
    assert exits == [0]


def ack_replies(data, bad=()):
    """The far send-session replies for `data`: a digest ack per line, a refusal for the indexes in `bad`."""
    rows = [{"ok": False, "error": "ConnectionRefusedError: gone"} if i in bad
            else {"ok": True, "digest": hashlib.sha256(line).hexdigest()}
            for i, line in enumerate(data.splitlines())]
    return "".join(json.dumps(r) + "\n" for r in rows).encode()


def stub_ssh(monkeypatch, code, stderr=b""):
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(
        a, code, ack_replies(k.get("input", b"")) if code == 0 else b"", stderr))


def send(bridge):
    line = json.dumps({"from": "uds:" + SENDER, "message": "hi"}).encode()
    bridge.relay_send(FakeConn(line + b"\n"))


def test_failed_relay_notifies_the_sender_and_status_reports_failed(bridge, monkeypatch):
    stub_ssh(monkeypatch, 255, b"Too many authentication failures")
    send(bridge)
    (endpoint, frame), = bridge.written
    notice = json.loads(frame)
    assert endpoint == SENDER
    assert notice["from"] == "uds:" + MIRROR
    assert "far-session" in notice["message"] and "255" in notice["message"]
    assert "Too many authentication failures" in notice["message"]
    assert "It was not delivered" in notice["message"] and "unconfirmed" not in notice["message"]
    status = bridge.delivery_status()
    assert len(status) == 1 and status[0].startswith("FAILED") and "255" in status[0]


def test_successful_relay_sends_no_notice_and_status_reports_delivered(bridge, monkeypatch):
    stub_ssh(monkeypatch, 0)
    send(bridge)
    assert failure_notices(bridge) == []
    # A bare ok names no receiver, so the sender gets an unconfirmed-delivery ack, never a failure.
    (endpoint, frame), = bridge.written
    assert endpoint == SENDER and "receiver unconfirmed" in json.loads(frame)["message"]
    assert [s.split()[0] for s in bridge.delivery_status()] == ["delivered"]


def test_unknown_sender_logs_instead_of_raising(bridge, monkeypatch):
    stub_ssh(monkeypatch, 255, b"boom")
    bridge.relay_send(FakeConn(b'{"message": "hi"}\n'))
    assert bridge.written == []
    assert bridge.delivery_status()[0].startswith("FAILED")


def test_notice_write_failure_does_not_break_the_relay(bridge, monkeypatch):
    stub_ssh(monkeypatch, 255, b"x")

    def boom(ep, data):
        raise OSError("inbox refused")

    monkeypatch.setattr(bridge, "write_local", boom)
    send(bridge)
    assert bridge.delivery_status()[0].startswith("FAILED")


def test_status_is_bounded_and_empty_record_is_stated(bridge, monkeypatch):
    assert bridge.delivery_status() == ["no cross-box sends recorded"]
    stub_ssh(monkeypatch, 0)
    for _ in range(bridge.DELIVERY_KEEP + 5):
        send(bridge)
    assert len(bridge.deliveries_path().read_text().splitlines()) == bridge.DELIVERY_KEEP


def test_status_command_runs_at_default_arguments(bridge, monkeypatch, capsys):
    stub_ssh(monkeypatch, 255, b"denied")
    send(bridge)
    bridge.main(["status"])
    assert capsys.readouterr().out.startswith("FAILED")


def failure_notices(bridge):
    return [w for w in bridge.written if "Delivery failed" in json.loads(w[1])["message"]]


def send_many(bridge, n):
    lines = b"".join(json.dumps({"from": "uds:" + SENDER, "message": f"m{i}"}).encode() + b"\n" for i in range(n))
    bridge.relay_send(FakeConn(lines))


def record_ssh(monkeypatch, responder):
    calls = []

    def run(argv, **k):
        calls.append((argv[-1], k["input"]))
        return responder(argv[-1], k["input"])

    monkeypatch.setattr(subprocess, "run", run)
    return calls


def test_n_lines_go_in_one_ssh_and_each_gets_a_record(bridge, monkeypatch):
    calls = record_ssh(monkeypatch, lambda cmd, data: subprocess.CompletedProcess([], 0, ack_replies(data, bad={1}), b""))
    send_many(bridge, 3)
    assert len(calls) == 1 and re.fullmatch(SEND_SESSION, calls[0][0])
    assert calls[0][1].count(b"\n") == 3
    assert [s.split()[0] for s in bridge.delivery_status()] == ["delivered", "FAILED", "delivered"]
    (endpoint, frame), = failure_notices(bridge)
    assert endpoint == SENDER and "gone" in json.loads(frame)["message"]


def test_a_reply_less_exit_0_is_acked_from_the_far_receipts(bridge, monkeypatch):
    # macmini 2026-11-15: a pre-ack relay wrote every line and exited 0 with no reply; 24 sends it
    # received were recorded FAILED and each sender got a false failure notice.
    def far(cmd, data):
        if cmd.startswith("receipts"):
            rows = [{"digest": hashlib.sha256(l).hexdigest()} for l in sent[0].splitlines()]
            return subprocess.CompletedProcess([], 0, json.dumps({"ok": True, "rows": rows}).encode() + b"\n", b"")
        sent.append(data)
        # The macmini relay's real reply: one bare ok per line, no digest.
        return subprocess.CompletedProcess([], 0, b'{"ok": true}\n' * len(data.splitlines()), b"")
    sent = []
    calls = record_ssh(monkeypatch, far)
    send_many(bridge, 2)
    assert [c[0].split()[0] for c in calls] == ["send-session", "receipts"]
    assert [s.split()[0] for s in bridge.delivery_status()] == ["delivered", "delivered"]
    # A bare ok names no receiver, so each sender is told delivery is unconfirmed, not failed.
    assert failure_notices(bridge) == [] and len(bridge.written) == 2 and all(
        "receiver unconfirmed" in json.loads(f)["message"] for _, f in bridge.written)


def test_old_relay_usage_error_is_a_recorded_failure_not_a_per_line_resend(bridge, monkeypatch):
    calls = record_ssh(monkeypatch, lambda cmd, data: subprocess.CompletedProcess(
        [], 1, b"", b"usage: roster | send <pid> | inbox <name>\n"))
    send_many(bridge, 2)
    assert [c[0] for c in calls if c[0].startswith("send-session")] == [calls[0][0]] and re.fullmatch(SEND_SESSION, calls[0][0])
    assert [c[0] for c in calls[1:]] == ["capabilities"]
    status = bridge.delivery_status()
    assert [s.split()[0] for s in status] == ["FAILED", "FAILED"] and "lacks send-session" in status[0]
    assert len(bridge.written) == 2


def test_a_target_mismatch_is_recorded_failed_and_the_sender_notified(bridge, monkeypatch):
    calls = record_ssh(monkeypatch, lambda cmd, data: subprocess.CompletedProcess(
        [], 1, b"", b"target mismatch: pid 4242 holds other-session\n"))
    send_many(bridge, 1)
    assert len(calls) == 1
    status = bridge.delivery_status()
    assert status[0].split()[0] == "FAILED" and "target mismatch" in status[0]
    (endpoint, frame), = bridge.written
    assert endpoint == SENDER and "target mismatch" in json.loads(frame)["message"]


@pytest.mark.parametrize("stderr, marked", [
    (b"target mismatch: pid 4242 holds other-session, not the addressed session\n", True),
    (b"ambiguous: 2 live sessions named far-session (pids 4242, 4243)\n", False),
])
def test_a_named_refusal_is_recorded_for_the_sender_and_only_a_mismatch_marks_the_mirror(bridge, monkeypatch, stderr, marked):
    calls = record_ssh(monkeypatch, lambda cmd, data: subprocess.CompletedProcess([], 1, b"", stderr))
    send_many(bridge, 2)
    assert len(calls) == 1
    status = bridge.delivery_status()
    word = stderr.split()[0].decode()
    assert [s.split()[0] for s in status] == ["FAILED", "FAILED"] and word in status[0]
    assert len(bridge.written) == 2 and all(word in json.loads(f)["message"] for _, f in bridge.written)
    marker = Path(bridge.SESSIONS) / f"{os.getpid()}.misroute"
    assert marker.exists() is marked
    if marked:
        assert marker.read_bytes() == b""


def test_the_bridge_marker_is_the_one_the_roster_reads(bridge, tmp_path, monkeypatch):
    home = tmp_path / "home"
    sessions = home / ".claude" / "sessions"
    sessions.mkdir(parents=True)
    me = os.getpid()
    (sessions / f"{me}.json").write_text(json.dumps({"pid": me, "name": "far-session", "sessionId": FAR_SID}))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setattr(bridge, "SESSIONS", "~/.claude/sessions")
    bridge._mark_misroute()
    roster = bridge._roster()
    row = {"entry": {"name": "far-session", "sessionId": FAR_SID}}
    live = types.SimpleNamespace(pid=me, poll=lambda: None)
    assert roster.crossed_mirrors({"/far/4242.sock": live}, {"/far/4242.sock": row}) == [("far-session", "/far/4242.sock")]


def test_a_non_named_refusal_leaves_no_marker(bridge, monkeypatch):
    record_ssh(monkeypatch, lambda cmd, data: subprocess.CompletedProcess([], 1, b"", b"no live session with pid 4242\n"))
    send_many(bridge, 1)
    assert not (Path(bridge.SESSIONS) / f"{os.getpid()}.misroute").exists()


def test_a_mirror_without_far_name_sends_nothing_and_records_each_line(bridge, monkeypatch):
    path = Path(bridge.SESSIONS) / f"{os.getpid()}.json"
    entry = json.loads(path.read_text())
    del entry["unimatrixFarName"]
    path.write_text(json.dumps(entry))
    calls = record_ssh(monkeypatch, lambda cmd, data: subprocess.CompletedProcess([], 0, b"", b""))
    send_many(bridge, 2)
    assert calls == []
    status = bridge.delivery_status()
    assert [s.split()[0] for s in status] == ["FAILED", "FAILED"] and "re-mirror" in status[0]
    assert len(bridge.written) == 2


def test_a_session_id_mismatch_is_recorded_naming_both_ids_and_marks_the_mirror(bridge, monkeypatch):
    refusal = f"target mismatch: pid 4242 holds session other-sid (far-session), not addressed session {FAR_SID}\n".encode()
    calls = record_ssh(monkeypatch, lambda cmd, data: subprocess.CompletedProcess([], 1, b"", refusal))
    send_many(bridge, 2)
    assert len(calls) == 1
    status = bridge.delivery_status()
    assert [s.split()[0] for s in status] == ["FAILED", "FAILED"]
    assert "other-sid" in status[0] and f"not addressed session {FAR_SID}" in status[0]
    assert f"session {FAR_SID})" in status[0]
    assert len(bridge.written) == 2 and all("other-sid" in json.loads(f)["message"] for _, f in bridge.written)
    assert (Path(bridge.SESSIONS) / f"{os.getpid()}.misroute").read_bytes() == b""


@pytest.mark.parametrize("sid", [None, "", 7])
def test_a_mirror_without_far_session_id_sends_nothing_and_records_each_line(bridge, monkeypatch, sid):
    path = Path(bridge.SESSIONS) / f"{os.getpid()}.json"
    entry = json.loads(path.read_text())
    entry.pop("sessionId")
    if sid is not None:
        entry["sessionId"] = sid
    path.write_text(json.dumps(entry))
    calls = record_ssh(monkeypatch, lambda cmd, data: subprocess.CompletedProcess([], 0, b"", b""))
    send_many(bridge, 2)
    assert calls == []
    status = bridge.delivery_status()
    assert [s.split()[0] for s in status] == ["FAILED", "FAILED"] and "mirror lacks far session id; re-mirror" in status[0]
    assert len(bridge.written) == 2
    assert not (Path(bridge.SESSIONS) / f"{os.getpid()}.misroute").exists()


@pytest.mark.parametrize("name", ["group-em-56", None])
def test_an_addressed_name_that_differs_from_the_endpoint_identity_writes_nothing(bridge, monkeypatch, name):
    path = Path(bridge.SESSIONS) / f"{os.getpid()}.json"
    entry = json.loads(path.read_text())
    entry.pop("name")
    if name:
        entry["name"] = name
    path.write_text(json.dumps(entry))
    calls = record_ssh(monkeypatch, lambda cmd, data: subprocess.CompletedProcess([], 0, b'{"ok": true}\n', b""))
    send_many(bridge, 2)
    assert calls == []
    status = bridge.delivery_status()
    assert [s.split()[0] for s in status] == ["FAILED", "FAILED"]
    assert f"addressed {name}, endpoint is far-session" in status[0]
    assert len(bridge.written) == 2
    assert (Path(bridge.SESSIONS) / f"{os.getpid()}.misroute").exists()


def test_a_matching_addressed_name_still_delivers(bridge, monkeypatch):
    calls = record_ssh(monkeypatch, lambda cmd, data: subprocess.CompletedProcess([], 0, ack_replies(data), b""))
    send_many(bridge, 1)
    assert len(calls) == 1
    assert [s.split()[0] for s in bridge.delivery_status()] == ["delivered"]


def test_bridge_name_digest_equals_the_relays(bridge, monkeypatch):
    spec = importlib.util.spec_from_file_location("unimatrix_relay_digest", RELAY)
    relay = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(relay)
    calls = record_ssh(monkeypatch, lambda cmd, data: subprocess.CompletedProcess([], 0, b'{"ok": true}\n', b""))
    send_many(bridge, 1)
    assert calls[0][0].split()[-2] == relay.name_digest("far-session")


def test_a_relay_refusal_is_recorded_failed_and_not_retried_per_line(bridge, monkeypatch):
    calls = record_ssh(monkeypatch, lambda cmd, data: subprocess.CompletedProcess(
        [], 1, b"", b"no live session with pid 4242\n"))
    send_many(bridge, 2)
    assert [c[0] for c in calls if c[0].startswith("send-session")] == [calls[0][0]] and calls[0][0].startswith("send-session 4242 ")
    status = bridge.delivery_status()
    assert [s.split()[0] for s in status] == ["FAILED", "FAILED"] and "no live session" in status[0]
    assert len(bridge.written) == 2


def test_relay_mode_parses_send_batch_and_rejects_non_digit_pid():
    spec = importlib.util.spec_from_file_location("unimatrix_relay_batch", RELAY)
    relay = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(relay)
    assert relay.parse_mode("send-batch 77") == ("send-batch", 77)
    assert relay.parse_mode("send 77") == ("send", 77)
    assert relay.parse_mode("send-batch x") is None
    assert relay.parse_mode("send-batch") is None


def test_relay_send_batch_replies_per_line_and_continues_past_a_fault(monkeypatch, capsys):
    import io

    spec = importlib.util.spec_from_file_location("unimatrix_relay_batch2", RELAY)
    relay = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(relay)
    seen = []

    def write(path, line):
        if b"bad" in line:
            raise ConnectionRefusedError("gone")
        seen.append(line)

    monkeypatch.setattr(relay, "_session_path", lambda pid, expected_digest=None, expected_name_digest=None, expected_session_id=None: "/p")
    monkeypatch.setattr(relay, "_write_line", write)
    monkeypatch.setattr(relay.sys, "stdin", types.SimpleNamespace(buffer=io.BytesIO(b"a\nbad\n\nc\n")))
    relay.send_batch(1)
    out = [json.loads(l) for l in capsys.readouterr().out.splitlines()]
    assert [o["ok"] for o in out] == [True, False, False, True] and "ConnectionRefusedError" in out[1]["error"]
    assert "empty message line" in out[2]["error"]
    assert seen == [b"a\n", b"c\n"]


def test_ssh_timeout_is_a_failed_delivery_with_a_notice(bridge, monkeypatch):
    def hang(*a, **k):
        raise subprocess.TimeoutExpired("ssh", 30)

    monkeypatch.setattr(subprocess, "run", hang)
    send(bridge)
    assert len(bridge.written) == 1 and "TimeoutExpired" in json.loads(bridge.written[0][1])["message"]
    assert bridge.delivery_status()[0].startswith("FAILED")


def route(transport, endpoint, verification=None, reason=None):
    """A Route as `unimatrix_far_addresses.select_route` emits it: LAN is `verified`, fallback is strict-at-dial."""
    if verification is None:
        verification = "verified" if transport == "lan" else "ssh-strict-at-dial"
    return types.SimpleNamespace(peer="farbox", alias="farbox", endpoint=endpoint, transport=transport,
                                 relay_key="k", hostkey=None, verification=verification, fallback_reason=reason)


def routed(bridge, monkeypatch, picks, responder):
    """Route `bridge` through `picks` (one Route or string per selection, the last repeating) and a fake channel."""
    dialled, calls, queue = [], [], list(picks)

    class Chan:
        def run(self, command, data, timeout=None):
            calls.append(command.split()[0])
            return responder(command, data)

    def pick(lan, tunnel):
        return queue.pop(0) if len(queue) > 1 else queue[0]

    def for_remote(remote, **kw):
        dialled.append(remote)
        return Chan()

    monkeypatch.setattr(bridge, "pick_remote", pick)
    monkeypatch.setattr(bridge._box_mux(), "for_remote", for_remote)
    return dialled, calls


def last_row(bridge):
    return json.loads(bridge.deliveries_path().read_text().splitlines()[-1])


@pytest.mark.parametrize("transport,endpoint", [("lan", "u@192.0.2.7"), ("fallback", "u@farbox-wg")])
def test_a_verified_route_is_dialled_and_its_transport_is_recorded(bridge, monkeypatch, transport, endpoint):
    dialled, calls = routed(bridge, monkeypatch, [route(transport, endpoint)],
                            lambda cmd, data: subprocess.CompletedProcess([], 0, ack_replies(data), b""))
    send(bridge)
    assert dialled == [endpoint] and calls == ["send-session"]
    assert last_row(bridge)["transport"] == transport and last_row(bridge)["ok"] is True


@pytest.mark.parametrize("verification", ["not-this-box", "unreachable"])
def test_a_route_that_failed_identity_carries_nothing_and_fails_visibly(bridge, monkeypatch, verification):
    dialled, calls = routed(bridge, monkeypatch, [route(None, None, verification, "192.0.2.9: not-this-box")],
                            lambda cmd, data: pytest.fail("a frame crossed an unauthenticated route"))
    send(bridge)
    assert dialled == [] and calls == []
    row = last_row(bridge)
    assert row["ok"] is False and row["transport"] == "none" and "no authenticated route" in row["error"]
    assert "not-this-box" in row["error"] and not row.get("unconfirmed")
    (endpoint, frame), = bridge.written
    assert endpoint == SENDER and "It was not delivered" in json.loads(frame)["message"]


def test_a_string_route_keeps_the_ssh_and_tunnel_labels(bridge, monkeypatch):
    bridge.cfg.tunnel_remote = "farbox-wg"
    routed(bridge, monkeypatch, ["farbox-wg"], lambda cmd, data: subprocess.CompletedProcess([], 0, ack_replies(data), b""))
    send(bridge)
    assert last_row(bridge)["transport"] == "tunnel"


def far_with_lost_reply(sent, receipts_rc):
    """A far relay that writes the line on send-session but whose reply is lost (ssh 255); `receipts` answers `receipts_rc`."""
    def far(cmd, data):
        if cmd.startswith("send-session"):
            sent.append(data)
            return subprocess.CompletedProcess([], 255, b"", b"client_loop: send disconnect: Broken pipe")
        rows = [{"digest": hashlib.sha256(l).hexdigest()} for l in sent[0].splitlines()]
        return subprocess.CompletedProcess([], receipts_rc, json.dumps({"ok": True, "rows": rows}).encode() + b"\n" if receipts_rc == 0 else b"", b"")
    return far


def test_a_lost_ack_after_the_far_write_is_reconciled_over_the_switched_route_never_resent(bridge, monkeypatch):
    sent = []
    dialled, calls = routed(bridge, monkeypatch, [route("lan", "u@192.0.2.7"), route("fallback", "u@farbox-wg")],
                            far_with_lost_reply(sent, 0))
    send(bridge)
    assert calls == ["send-session", "receipts"] and len(sent) == 1
    assert dialled == ["u@192.0.2.7", "u@farbox-wg"]
    assert last_row(bridge)["ok"] is True and failure_notices(bridge) == []


def test_a_lost_ack_with_no_readable_receipt_is_unconfirmed_not_failed_and_never_resent(bridge, monkeypatch):
    sent = []
    dialled, calls = routed(bridge, monkeypatch, [route("lan", "u@192.0.2.7"), route("fallback", "u@farbox-wg")],
                            far_with_lost_reply(sent, 255))
    send(bridge)
    assert calls.count("send-session") == 1 and len(sent) == 1
    row = last_row(bridge)
    assert row["ok"] is False and row["unconfirmed"] is True and "check the far inbox before resending" in row["error"]
    (endpoint, frame), = bridge.written
    message = json.loads(frame)["message"]
    assert "Delivery unconfirmed" in message and "It was not delivered" not in message


class FakeSock:
    def __init__(self, fail=False):
        self.sent, self.fail = [], fail

    def sendall(self, data):
        if self.fail:
            raise OSError("broken pipe")
        self.sent.append(data)

    def shutdown(self, how):
        pass


def test_a_mirror_socket_send_writes_one_delivery_row_named_for_the_mirror(bridge):
    line = json.dumps({"from": "uds:" + SENDER, "message": "hi"}).encode()
    sock = FakeSock()
    bridge.pump_local_to_sock(FakeConn(b'{"type":"auth"}\n' + line + b"\n"), sock)
    assert sock.sent
    rows = [json.loads(r) for r in bridge.deliveries_path().read_text().splitlines()]
    assert len(rows) == 1
    assert rows[0]["target"].startswith("far-session ") and rows[0]["ok"] is True and rows[0]["transport"] == "socket"
    assert rows[0]["digest"] == hashlib.sha256(line).hexdigest()
    assert bridge.written == []


def test_a_failed_mirror_socket_write_records_the_failure_and_still_raises(bridge):
    line = json.dumps({"from": "uds:" + SENDER, "message": "hi"}).encode()
    with pytest.raises(OSError):
        bridge.pump_local_to_sock(FakeConn(line + b"\n"), FakeSock(fail=True))
    row = last_row(bridge)
    assert row["ok"] is False and "socket write failed" in row["error"] and row["target"].startswith("far-session ")


def test_a_route_marked_unavailable_by_select_route_is_refused_before_any_dial(bridge, monkeypatch):
    dialled, calls = routed(bridge, monkeypatch, [route(None, None, "unavailable", "no-pinned-hostkey")],
                            lambda *a: subprocess.CompletedProcess([], 0, b"", b""))
    send(bridge)
    assert dialled == []


def test_delivery_status_handles_relay_in_rows_without_ts_or_target(bridge):
    path = bridge.deliveries_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"ok": True, "at": 1700000000, "name": "relay-in-peer"}) + "\n"
        + json.dumps({"ok": False, "error": "timeout"}) + "\n",
        encoding="utf-8",
    )
    lines = bridge.delivery_status()
    assert len(lines) == 2
    assert lines[0] == "delivered 2023-11-14T22:13:20Z relay-in-peer"
    assert lines[1] == "FAILED    ? ?: timeout"
