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
    assert replies == [{"ok": True, "digest": hashlib.sha256(line.rstrip(b"\n")).hexdigest()}]
    (row,) = rows(relay)
    assert row["session"] == SID
    assert row["digest"] == hashlib.sha256(line.rstrip(b"\n")).hexdigest()
    assert row["at"] > 0 and row["ts"]
    assert "secret words" not in relay.receipts_path().read_text(encoding="utf-8")


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
    assert replies == [{"ok": True}]
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


def test_a_never_reading_pipe_replies_ok_false_through_send_batch(relay, monkeypatch, capsys):
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

    class Stdin:
        buffer = io.BytesIO(b'{"m":"x"}\n')

    monkeypatch.setattr(relay.sys, "stdin", Stdin)
    relay.send_batch(7, "d" * 16, "e" * 16, SID)
    release.set()
    (reply,) = [json.loads(x) for x in capsys.readouterr().out.splitlines()]
    assert reply["ok"] is False and "TimeoutError" in reply["error"]
    assert not relay.receipts_path().exists()


def test_receipts_mode_returns_the_sessions_rows_since_the_timestamp(relay):
    relay.record_receipt(SID, b"old\n", now=1000)
    relay.record_receipt(SID, b"new\n", now=2000)
    relay.record_receipt("other-session", b"new\n", now=2000)
    got = relay.receipts(SID, 1500.0)
    assert got["ok"] is True
    assert [r["digest"] for r in got["rows"]] == [hashlib.sha256(b"new").hexdigest()]


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
    assert "receipts <session-id> <since>" in str(ex.value)
