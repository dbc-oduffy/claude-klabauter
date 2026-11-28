"""Spawn ledger store round-trip, reap, session-id validation and the detach-shape table."""

from __future__ import annotations

import json
import os
import subprocess
import sys

import psutil
import pytest

from coordinator_core.ops import _spawn_ledger as ledger
from coordinator_core.ops._spawn_ledger import LedgerRow

SESSION = "11111111-2222-3333-4444-555555555555"
OTHER = "99999999-2222-3333-4444-555555555555"


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path))
    return tmp_path


def _self_row(session=SESSION, agent="agent-1", argv=None):
    pid = os.getpid()
    return LedgerRow(pid, psutil.Process(pid).create_time(), session, agent, argv or ["python", "x.py"])


def test_round_trip(_home):
    row = _self_row()
    assert ledger.write_row(row) is True
    assert (_home / "spawn-ledger" / SESSION / f"{row.pid}.json").is_file()
    assert ledger.read_row(SESSION, row.pid) == row


def test_foreign_session_read_is_none_but_find_row_names_it():
    row = _self_row()
    assert ledger.write_row(row)
    assert ledger.read_row(OTHER, row.pid) is None
    assert ledger.find_row(row.pid) == row
    assert ledger.find_row(row.pid + 7919) is None


def test_delete_row():
    row = _self_row()
    ledger.write_row(row)
    ledger.delete_row(SESSION, row.pid)
    assert ledger.read_row(SESSION, row.pid) is None
    ledger.delete_row(SESSION, row.pid)  # absent row is a no-op


def test_reap_deletes_dead_pid_and_reused_ctime_on_write():
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait()
    dead = LedgerRow(child.pid, 1.0, SESSION, "", ["python"])
    reused = LedgerRow(os.getpid(), 1.0, SESSION, "", ["python"])
    # Seed stale rows directly: write_row itself reaps, so write them through a path it cannot reap yet.
    for r in (dead, reused):
        p = ledger.ledger_root() / SESSION / f"{r.pid}.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(r.__dict__), encoding="utf-8")
    live = _self_row()
    assert ledger.write_row(live)  # overwrites the reused-ctime row with the true one
    assert ledger.read_row(SESSION, dead.pid) is None
    assert ledger.read_row(SESSION, live.pid) == live


def test_reap_removes_ctime_mismatch_sibling(tmp_path):
    parent = psutil.Process()
    sibling = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        p = ledger.ledger_root() / SESSION / f"{sibling.pid}.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(
            json.dumps(LedgerRow(sibling.pid, 1.0, SESSION, "", ["x"]).__dict__),
            encoding="utf-8",
        )
        assert ledger.write_row(_self_row())
        assert not p.exists()
        assert parent.is_running()
    finally:
        sibling.kill()
        sibling.wait()


@pytest.mark.parametrize("bad", ["", "../x", "a/b", "a.b", "C:x", "a" * 65, "x y"])
def test_bad_session_id_refused(bad, _home):
    row = LedgerRow(os.getpid(), 1.0, bad, "", [])
    assert ledger.write_row(row) is False
    assert ledger.read_row(bad, os.getpid()) is None
    ledger.delete_row(bad, os.getpid())
    assert not (_home / "spawn-ledger").exists()


def test_unresolved_settings_home_is_typed_refusal_naming_the_key(monkeypatch):
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", "relative/not/rooted")
    with pytest.raises(ledger.SpawnLedgerUnavailable, match="COORDINATOR_SETTINGS_HOME"):
        ledger.ledger_root()
    row = _self_row()
    assert ledger.write_row(row) is False
    assert ledger.read_row(SESSION, row.pid) is None
    assert ledger.find_row(row.pid) is None


def test_corrupt_row_is_none():
    p = ledger.ledger_root() / SESSION / "5.json"
    p.parent.mkdir(parents=True)
    p.write_text("{not json", encoding="utf-8")
    assert ledger.read_row(SESSION, 5) is None


@pytest.mark.parametrize(
    "command",
    [
        "ls -la",
        "echo hi && git status",
        "echo a > b 2>&1",
        "echo nohup",
        "grep nohup file.txt",
        "echo hi 2>&1 | cat",
        "cat a && echo $HOME",
        "",
    ],
)
def test_detach_shape_none(command):
    assert ledger.detach_shape(command) == "none"


@pytest.mark.parametrize(
    "command",
    [
        "nohup python loop.py > out.log 2>&1 &",
        "python loop.py &",
        "sleep 5 & echo started",
        "setsid python srv.py",
        "FOO=1 nohup python a.py",
        "nohup bash -c 'python a.py' &",
        "cd d && nohup python a.py &",
        "python a.py & disown",
        "python a.py; disown %1",
        "Start-Process -FilePath python -ArgumentList x.py",
        "Start-Process python x.py",
        "saps notepad",
        "/usr/bin/nohup python a.py",
        "nohup python a.py 2>&1 &",
    ],
)
def test_detach_shape_recordable(command):
    assert ledger.detach_shape(command) == "recordable"


@pytest.mark.parametrize(
    "command",
    [
        "eval \"$CMD\" &",
        "nohup eval foo &",
        "$RUNNER arg &",
        "nohup $RUNNER arg &",
        "$(which python) a.py &",
        "bash -c \"$x\" &",
        "nohup bash -c \"$x\" &",
        "sh -c $x &",
        "bash -c &",
        "nohup bash -c 'eval foo' &",
        "setsid $X",
        "Start-Process -FilePath $exe",
        "Start-Process $exe -ArgumentList x",
        "nohup &",
        "python a.py & disown ; $X & ",
        "nohup 'unterminated &",
    ],
)
def test_detach_shape_opaque(command):
    assert ledger.detach_shape(command) == "opaque"


def test_opaque_head_without_detach_word_is_none():
    assert ledger.detach_shape("eval \"$CMD\"") == "none"
    assert ledger.detach_shape("$RUNNER arg") == "none"


def test_run_in_background_marks_the_whole_command_detached():
    assert ledger.detach_shape("python a.py", run_in_background=True) == "recordable"
    assert ledger.detach_shape("eval \"$X\"", run_in_background=True) == "opaque"
    assert ledger.detach_shape("python a.py", run_in_background=False) == "none"
