"""The far relay records a per-line receipt for each line it wrote and serves them through `receipts`."""
import hashlib
import importlib.util
import io
import json
from pathlib import Path

import pytest

BIN = Path(__file__).resolve().parents[1]
SID = "11111111-2222-3333-4444-555555555555"


@pytest.fixture
def relay(tmp_path, monkeypatch):
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path / "settings"))
    spec = importlib.util.spec_from_file_location("unimatrix_relay_receipt", BIN / "unimatrix-relay.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setattr(mod, "_session_path", lambda *a, **k: "/tmp/cc-socks/x.sock")
    return mod


def run_batch(relay, monkeypatch, capsys, lines, write, session=SID):
    class Stdin:
        buffer = io.BytesIO(b"".join(lines))

    monkeypatch.setattr(relay.sys, "stdin", Stdin)
    monkeypatch.setattr(relay, "_write_line", write)
    relay.send_batch(7, "d" * 16, "e" * 16, session)
    return [json.loads(x) for x in capsys.readouterr().out.splitlines()]


def rows(relay):
    return [json.loads(x) for x in relay.receipts_path().read_text(encoding="utf-8").splitlines()]


def test_an_ok_line_appends_a_receipt_with_the_digest_and_no_text(relay, monkeypatch, capsys):
    line = b'{"from":"uds:/a","message":"secret words"}\n'
    replies = run_batch(relay, monkeypatch, capsys, [line], lambda p, l: None)
    assert replies == [{"ok": True, "written": True, "digest": hashlib.sha256(line.rstrip(b"\n")).hexdigest()}]
    (row,) = rows(relay)
    assert row["session"] == SID
    assert row["digest"] == hashlib.sha256(line.rstrip(b"\n")).hexdigest()
    assert row["at"] > 0 and row["ts"]
    assert "secret words" not in relay.receipts_path().read_text(encoding="utf-8")


def test_a_receipt_row_names_the_box_that_wrote_it(relay, monkeypatch):
    monkeypatch.setattr(relay, "local_box", lambda: "machine-a")
    relay.record_receipt(SID, b"hi\n")
    (row,) = rows(relay)
    assert row["box"] == "machine-a"
    assert relay.receipts(SID, 0.0)["rows"][0]["box"] == "machine-a"


def test_a_failed_or_blank_line_leaves_no_receipt(relay, monkeypatch, capsys):
    def write(path, line):
        if b"boom" in line:
            raise OSError("down")

    replies = run_batch(relay, monkeypatch, capsys, [b'{"m":"boom"}\n', b"\n", b'{"m":"fine"}\n'], write)
    assert [r["ok"] for r in replies] == [False, False, True]
    (row,) = rows(relay)
    assert row["digest"] == hashlib.sha256(b'{"m":"fine"}').hexdigest()


def test_no_session_id_records_nothing(relay, monkeypatch, capsys):
    replies = run_batch(relay, monkeypatch, capsys, [b'{"m":"x"}\n'], lambda p, l: None, session=None)
    assert replies == [{"ok": True, "written": True}]
    assert not relay.receipts_path().exists()


def test_a_receipt_write_fault_fails_the_line(relay, monkeypatch, capsys):
    monkeypatch.setattr(relay, "receipts_path", lambda: relay.settings_home() / "unimatrix" / "receipts.jsonl" / "x")
    (relay.settings_home() / "unimatrix").mkdir(parents=True)
    (relay.settings_home() / "unimatrix" / "receipts.jsonl").write_text("file, not a dir")
    (reply,) = run_batch(relay, monkeypatch, capsys, [b'{"m":"x"}\n'], lambda p, l: None)
    assert reply["ok"] is False and "digest" not in reply


def test_a_permission_error_on_trim_replace_fails_the_line_after_bounded_retries(relay, monkeypatch, capsys):
    monkeypatch.setattr(relay, "RECEIPT_KEEP", 1)
    calls = []

    def deny(src, dst):
        calls.append(1)
        raise PermissionError("held open")

    monkeypatch.setattr(relay.os, "replace", deny)
    replies = run_batch(relay, monkeypatch, capsys, [b'{"m":"1"}\n'] * 4, lambda p, l: None)
    assert [r["ok"] for r in replies[:2]] == [True, True]
    assert replies[-1]["ok"] is False and "PermissionError" in replies[-1]["error"]
    assert len(calls) % 4 == 0 and calls


def test_a_transient_permission_error_on_replace_is_retried(relay, monkeypatch):
    monkeypatch.setattr(relay, "RECEIPT_KEEP", 1)
    real, seen = relay.os.replace, []

    def flaky(src, dst):
        seen.append(1)
        if len(seen) == 1:
            raise PermissionError("held open")
        real(src, dst)

    monkeypatch.setattr(relay.os, "replace", flaky)
    for i in range(4):
        relay.record_receipt(SID, f"line{i}\n".encode(), now=1000 + i)
    assert len(seen) == 2 and [r["at"] for r in rows(relay)] == [1002, 1003]


def test_the_receipt_file_is_trimmed_to_the_bound_once_past_twice_it(relay, monkeypatch):
    monkeypatch.setattr(relay, "RECEIPT_KEEP", 3)
    for i in range(6):
        relay.record_receipt(SID, f"line{i}\n".encode(), now=1000 + i)
    assert len(rows(relay)) == 6
    relay.record_receipt(SID, b"line6\n", now=1006)
    assert [r["at"] for r in rows(relay)] == [1004, 1005, 1006]


def test_concurrent_receipts_lose_no_row(relay):
    import threading
    n = 40
    ts = [threading.Thread(target=relay.record_receipt, args=(SID, f"l{i}\n".encode())) for i in range(n)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert sorted(r["digest"] for r in rows(relay)) == sorted(hashlib.sha256(f"l{i}".encode()).hexdigest() for i in range(n))


def test_a_flush_that_never_returns_raises_within_the_deadline(relay):
    import threading
    import time
    release = threading.Event()
    t0 = time.monotonic()
    with pytest.raises(TimeoutError):
        relay._flush_bounded(release.wait, deadline=0.05)
    release.set()
    assert time.monotonic() - t0 < 0.5


def test_a_flush_that_returns_or_raises_passes_through(relay):
    relay._flush_bounded(lambda: None)
    with pytest.raises(OSError):
        relay._flush_bounded(lambda: (_ for _ in ()).throw(OSError("pipe broken")))


def test_a_never_reading_pipe_replies_delivered_unconfirmed_through_send_batch(relay, monkeypatch, capsys):
    # The write completed into the pipe buffer: a dead session's pipe fails the write, a stalled one reads it later.
    import sys as _sys
    import threading
    import types
    release = threading.Event()
    fake = types.SimpleNamespace(
        GENERIC_READ=1, GENERIC_WRITE=2, OPEN_EXISTING=3,
        CreateFile=lambda *a: "h", WriteFile=lambda h, line: None,
        FlushFileBuffers=lambda h: release.wait(), CloseHandle=lambda h: None)
    monkeypatch.setitem(_sys.modules, "win32file", fake)
    monkeypatch.setattr(relay, "endpoint_kind", lambda platform=None: "pipe")
    monkeypatch.setattr(relay, "local_reply_address", lambda line: line)
    monkeypatch.setattr(relay, "load_roster", lambda: types.SimpleNamespace(authenticated_data=lambda p, l, s: l))
    monkeypatch.setattr(relay, "PIPE_FLUSH_DEADLINE_S", 0.05)
    monkeypatch.setattr(relay, "record_receipt", lambda sid, line: "digest")

    class Stdin:
        buffer = io.BytesIO(b'{"m":"x"}\n')

    monkeypatch.setattr(relay.sys, "stdin", Stdin)
    relay.send_batch(7, "d" * 16, "e" * 16, SID)
    release.set()
    (reply,) = [json.loads(x) for x in capsys.readouterr().out.splitlines()]
    assert reply["ok"] is True and reply["advisory"].startswith("delivered-unconfirmed")


def deliveries(relay):
    return [json.loads(x) for x in relay.deliveries_path().read_text(encoding="utf-8").splitlines()]


def test_every_relay_in_row_names_the_sender_box(relay, monkeypatch, capsys):
    monkeypatch.setitem(relay._PEER, "box", "mini")
    lines = [b'{"m":"a"}\n', b'{"m":"b"}\n']
    run_batch(relay, monkeypatch, capsys, lines, lambda p, l: None)
    got = deliveries(relay)
    assert [r["sender_box"] for r in got] == ["mini", "mini"]


def test_a_refused_target_row_still_names_the_sender_box(relay, monkeypatch):
    monkeypatch.setitem(relay._PEER, "box", "mini")
    relay.record_delivery({"ok": False, "error": "target mismatch"})
    (row,) = deliveries(relay)
    assert row["sender_box"] == "mini" and "sender_name" in row


def test_main_pins_the_forced_commands_peer_as_the_sender_box(relay, monkeypatch, capsys):
    monkeypatch.setenv("SSH_ORIGINAL_COMMAND", f"receipts {SID} 0")
    monkeypatch.setattr(relay.sys, "argv", ["unimatrix-relay.py", "--peer", "mini"])
    relay.main()
    assert relay._PEER["box"] == "mini"


def test_receipts_mode_returns_the_sessions_rows_since_the_timestamp(relay):
    relay.record_receipt(SID, b"old\n", now=1000)
    relay.record_receipt(SID, b"new\n", now=2000)
    relay.record_receipt("other-session", b"new\n", now=2000)
    got = relay.receipts(SID, 1500.0)
    assert got["ok"] is True
    assert [r["digest"] for r in got["rows"]] == [hashlib.sha256(b"new").hexdigest()]


def test_receipts_resolve_by_every_id_the_roster_prints(relay, monkeypatch):
    monkeypatch.setattr(relay, "local_box", lambda: "mini")
    monkeypatch.setattr(relay, "_REACHED", {"pid": 4242, "name": "fe", "session": SID})
    relay.record_receipt(SID, b"hello\n", now=2000)
    monkeypatch.setattr(relay, "_REACHED", {"pid": 9, "name": "zed", "session": "other-session"})
    relay.record_receipt("other-session", b"x\n", now=2000)
    for ident in (SID, "fe", "fe.mini", "4242"):
        assert [r["session"] for r in relay.receipts(ident, 0.0)["rows"]] == [SID], ident
    assert relay.receipts("nobody", 0.0)["rows"] == [] and relay.receipts("4243", 0.0)["rows"] == []
    assert relay.parse_mode("receipts fe.mini 0") == ("receipts", ("fe.mini", 0.0))
    assert relay.parse_mode("receipts 4242 0") == ("receipts", ("4242", 0.0))
    assert "roster prints" in relay.__doc__


def test_an_inbound_receipt_names_the_normalised_sender_box_and_the_session(relay):
    entry = {"sessionId": SID, "name": "fe", "pid": 4242}
    digest = relay.record_inbound_receipt(entry, b"frame\n", "example-operator@Mac-mini.local")
    assert digest == hashlib.sha256(b"frame").hexdigest()
    (row,) = relay.receipts("fe", 0.0)["rows"]
    assert (row["session"], row["sender_box"], row["pid"], row["digest"]) == (SID, "mac-mini", 4242, digest)
    assert relay.receipts(SID, 0.0)["rows"] == [row] and relay.receipts("4242", 0.0)["rows"] == [row]


def test_an_inbound_receipt_never_raises_and_skips_an_entry_with_no_session(relay, monkeypatch):
    assert relay.record_inbound_receipt({"pid": 1}, b"x\n", "far") is None
    assert relay.record_inbound_receipt("not-an-entry", b"x\n", "far") is None
    assert not relay.receipts_path().exists()
    monkeypatch.setattr(relay, "record_receipt", lambda *a, **k: (_ for _ in ()).throw(OSError("full")))
    assert relay.record_inbound_receipt({"sessionId": SID}, b"x\n", "far") is None


@pytest.fixture
def inbox(relay, tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("unimatrix_inbox_receipt_t", BIN / "unimatrix-inbox.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    (sessions / "77.json").write_text(json.dumps({"sessionId": SID, "name": "fe", "pid": 77}), encoding="utf-8")
    monkeypatch.setattr(mod, "SESSIONS", str(sessions))
    monkeypatch.setattr(mod, "log", io.StringIO())
    monkeypatch.setattr(mod, "localize", lambda frame, line, box=None: line)
    return mod


FRAME = {"from": "uds:/far", "message": "m"}
LINE = json.dumps(FRAME).encode() + b"\n"


def test_a_mux_frame_delivered_to_a_local_session_is_receipted_with_its_sender_box(inbox, relay, monkeypatch):
    monkeypatch.setattr(inbox, "write_local", lambda endpoint, data: None)
    inbox.deliver_frame(FRAME, LINE, b"", "/ep", "Mac-mini.local", pid=77)
    (row,) = relay.receipts(SID, 0.0)["rows"]
    assert row["sender_box"] == "mac-mini" and row["digest"] == hashlib.sha256(LINE.rstrip(b"\n")).hexdigest()


def test_a_frame_the_session_refused_leaves_no_receipt_and_still_fails_the_mirror(inbox, relay, monkeypatch):
    def refuse(endpoint, data):
        raise OSError("gone")

    failed = []
    monkeypatch.setattr(inbox, "write_local", refuse)
    inbox.deliver_frame(FRAME, LINE, b"", "/ep", "Mac-mini.local", on_fail=failed.append, pid=77)
    assert failed and relay.receipts(SID, 0.0)["rows"] == []


def test_a_bridge_handoff_is_receipted_only_when_it_connects(inbox, relay, monkeypatch):
    import socket as _socket
    sent = []

    class Conn:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def sendall(self, data):
            sent.append(data)

    monkeypatch.setattr(inbox.socket, "create_connection", lambda addr: Conn())
    inbox.deliver_frame(FRAME, LINE, b"", None, "gipsy", pid=77)
    assert [r["sender_box"] for r in relay.receipts(SID, 0.0)["rows"]] == ["gipsy"] and sent

    def down(addr):
        raise _socket.error("refused")

    monkeypatch.setattr(inbox.socket, "create_connection", down)
    inbox.deliver_frame(FRAME, LINE, b"", None, "gipsy", pid=77)
    assert len(relay.receipts(SID, 0.0)["rows"]) == 1


def test_receipts_with_no_file_is_an_empty_ok_reply(relay):
    assert relay.receipts(SID, 0.0) == {"ok": True, "rows": []}


def test_parse_mode_admits_only_a_session_id_and_a_numeric_since(relay):
    assert relay.parse_mode(f"receipts {SID} 1700000000") == ("receipts", (SID, 1700000000.0))
    assert relay.parse_mode(f"receipts {SID} 17.5") == ("receipts", (SID, 17.5))
    assert relay.parse_mode(f"receipts {SID}") is None
    assert relay.parse_mode(f"receipts {SID} 1; id") is None
    assert relay.parse_mode("receipts a/b 1") is None


def test_main_receipts_mode_prints_the_rows_and_usage_names_it(relay, monkeypatch, capsys):
    relay.record_receipt(SID, b"hello\n", now=2000)
    monkeypatch.setenv("SSH_ORIGINAL_COMMAND", f"receipts {SID} 0")
    relay.main()
    reply = json.loads(capsys.readouterr().out)
    assert reply["rows"][0]["digest"] == hashlib.sha256(b"hello").hexdigest()
    monkeypatch.setenv("SSH_ORIGINAL_COMMAND", "bogus")
    with pytest.raises(SystemExit) as ex:
        relay.main()
    assert "receipts <session-id|name|ref|pid> <since>" in str(ex.value)
