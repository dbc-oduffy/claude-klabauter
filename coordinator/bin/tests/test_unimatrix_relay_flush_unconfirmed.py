"""A flush timeout after a completed pipe write is delivered-unconfirmed, never a failure that invites a resend."""
import importlib.util
import io
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

BIN = Path(__file__).resolve().parents[1]
PATH = r"\\.\pipe\target"


@pytest.fixture
def relay(tmp_path, monkeypatch):
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path / "settings"))
    spec = importlib.util.spec_from_file_location("unimatrix_relay_flush_unconfirmed", BIN / "unimatrix-relay.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setattr(mod, "_session_path", lambda *a, **k: PATH)
    monkeypatch.setattr(mod, "endpoint_kind", lambda platform=None: "pipe")
    monkeypatch.setattr(mod, "local_reply_address", lambda line: line)
    monkeypatch.setattr(mod, "load_roster", lambda: SimpleNamespace(authenticated_data=lambda p, line, s: line))
    monkeypatch.setattr(mod, "_pipe_server_pid", lambda h: None)
    return mod


def fake_win32file(monkeypatch, write):
    wrote = []
    fake = SimpleNamespace(
        GENERIC_READ=1, GENERIC_WRITE=2, OPEN_EXISTING=3,
        CreateFile=lambda *a: "handle", CloseHandle=lambda h: None,
        WriteFile=lambda h, line: (write(line), wrote.append(line)),
        FlushFileBuffers=lambda h: None)
    monkeypatch.setitem(sys.modules, "win32file", fake)
    return wrote


def batch(relay, monkeypatch, capsys, line):
    class Stdin:
        buffer = io.BytesIO(line)

    monkeypatch.setattr(relay.sys, "stdin", Stdin)
    relay.send_batch(7)
    return [json.loads(x) for x in capsys.readouterr().out.splitlines()]


def timing_out(*a, **k):
    raise TimeoutError("pipe flush: no live reader drained the write")


LINE = b'{"from":"uds:/mini-a","message":"one"}\n'


def test_flush_timeout_after_a_completed_write_is_ok_with_an_advisory(relay, monkeypatch, capsys):
    wrote = fake_win32file(monkeypatch, lambda line: None)
    monkeypatch.setattr(relay, "_flush_bounded", timing_out)
    (reply,) = batch(relay, monkeypatch, capsys, LINE)
    assert wrote == [LINE]
    assert reply["ok"] is True and reply["advisory"].startswith("delivered-unconfirmed")
    assert "error" not in reply


def test_a_drained_flush_has_no_advisory(relay, monkeypatch, capsys):
    fake_win32file(monkeypatch, lambda line: None)
    monkeypatch.setattr(relay, "_flush_bounded", lambda flush, deadline=None: None)
    (reply,) = batch(relay, monkeypatch, capsys, LINE)
    assert reply["ok"] is True and "advisory" not in reply


def test_a_write_that_fails_stays_an_error(relay, monkeypatch, capsys):
    def broken(line):
        raise OSError("pipe broken")

    fake_win32file(monkeypatch, broken)
    monkeypatch.setattr(relay, "_flush_bounded", timing_out)  # never reached: the write raised first
    (reply,) = batch(relay, monkeypatch, capsys, LINE)
    assert reply["ok"] is False and "pipe broken" in reply["error"]
