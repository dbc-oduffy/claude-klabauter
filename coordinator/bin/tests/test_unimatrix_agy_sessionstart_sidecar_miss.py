"""A SessionStart that finds no live sidecar records why in agy-sidecar-misses.log, never in agy-sidecars.log."""
import importlib.util
import json
import os
from pathlib import Path

import pytest

pytestmark = pytest.mark.complete

BIN = Path(__file__).resolve().parents[1]
CONV = "0cf351a3-6538-4a9b-814c-52b1b75a596c"


def load(name):
    spec = importlib.util.spec_from_file_location(name.replace("-", "_"), BIN / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


sess = load("unimatrix-agy-hook-sessionstart")
launch = load("unimatrix-agy-hook-launch")
side = load("unimatrix-agy-sidecar")


class FakeProc:
    pid = os.getpid()


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path))
    monkeypatch.delenv("ANTIGRAVITY_CSRF_TOKEN", raising=False)
    monkeypatch.delenv("ANTIGRAVITY_LS_ADDRESS", raising=False)
    monkeypatch.setattr(sess, "load_sibling", lambda name: side if name == "unimatrix-agy-sidecar" else load(name))
    monkeypatch.setattr(launch.subprocess, "Popen", lambda cmd, **kw: FakeProc())
    return tmp_path


def misses(home):
    path = home / "unimatrix" / "agy-sidecar-misses.log"
    return [json.loads(x) for x in path.read_text().splitlines()] if path.exists() else []


def test_a_miss_appends_conversation_pid_and_reason_and_the_pending_leg_still_starts(home, monkeypatch):
    monkeypatch.setattr(side, "handoff_why", lambda *a, **k: "dead-sidecar")
    sess.register({"conversationId": CONV, "cwd": str(home)}, 5)
    rows = misses(home)
    assert len(rows) == 1 and rows[0]["reason"] == "dead-sidecar"
    assert rows[0]["conversationId"] == CONV and rows[0]["agy_pid"] == 5 and "ts" in rows[0]
    assert json.loads((home / "unimatrix" / "agy-legs" / f"{CONV}.json").read_text())["mode"] == "pending"
    assert not (home / "unimatrix" / "agy-sidecars.log").exists()


def test_a_real_handoff_with_no_sidecar_record_records_no_record(home):
    sess.register({"conversationId": CONV, "cwd": str(home)}, 5)
    assert [r["reason"] for r in misses(home)] == ["no-record"]


def test_an_ok_handoff_records_no_miss(home, monkeypatch):
    monkeypatch.setattr(side, "handoff_why", lambda *a, **k: None)
    sess.register({"conversationId": CONV, "cwd": str(home)}, 5)
    assert misses(home) == []


def test_a_missing_sidecar_module_records_no_module(home, monkeypatch):
    def gone(name):
        if name == "unimatrix-agy-sidecar":
            raise FileNotFoundError(name)
        return load(name)

    monkeypatch.setattr(sess, "load_sibling", gone)
    sess.register({"conversationId": CONV, "cwd": str(home)}, 5)
    rows = misses(home)
    assert [r["reason"] for r in rows] == ["no-module"]
    assert json.loads((home / "unimatrix" / "agy-legs" / f"{CONV}.json").read_text())["mode"] == "pending"


@pytest.mark.parametrize("pid,conv", [(None, CONV), (5, "../x"), ("5", CONV)])
def test_no_handoff_attempted_records_nothing(home, pid, conv):
    sess.register({"conversationId": conv, "cwd": str(home)}, pid)
    assert misses(home) == []


def test_the_misses_log_is_trimmed_by_the_trace_cap(home, monkeypatch):
    monkeypatch.setattr(side, "TRACE_MAX_BYTES", 600)
    monkeypatch.setattr(side, "TRACE_KEEP", 3)
    for i in range(30):
        sess.record_sidecar_miss(home, CONV, i, "no-record", side)
    pids = [r["agy_pid"] for r in misses(home)]
    assert 3 <= len(pids) < 30 and pids == list(range(30 - len(pids), 30))


def test_recording_a_miss_never_raises(home):
    (home / "unimatrix").write_text("a file where the directory should be")
    sess.record_sidecar_miss(home, CONV, 5, "no-record", side)
