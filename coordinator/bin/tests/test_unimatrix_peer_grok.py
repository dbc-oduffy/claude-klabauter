"""Behavior pins for the unimatrix grok peer: registry discovery and the cwd-pinned headless resume."""
import asyncio
import importlib.util
import json
import os
import socket
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.complete

BIN = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("unimatrix_peer_grok", BIN / "unimatrix-peer-grok.py")
pg = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pg)

SID = "01a1c000-0000-7000-8000-00000000c0c0"


def registry(tmp_path, rows):
    (tmp_path / "active_sessions.json").write_text(json.dumps(rows), encoding="utf-8")
    return tmp_path


def box():
    return pg._clean(socket.gethostname()).lower()


def test_live_entry_yields_named_grok_peer(tmp_path):
    repo = tmp_path / "myrepo"
    (repo / ".git").mkdir(parents=True)
    sub = repo / "pkg"
    sub.mkdir()
    home = registry(tmp_path, [{"session_id": SID, "pid": 4242, "cwd": str(sub), "opened_at": "x"}])
    peers = pg.discover_peers(home, pid_alive=lambda pid: pid == 4242)
    assert peers == [{"name": f"grok-myrepo-01a1c000-on-{box()}", "vendor": "grok",
                      "target": SID, "cwd": str(sub)}]


def test_dead_pid_yields_no_peer(tmp_path):
    home = registry(tmp_path, [{"session_id": SID, "pid": 4242, "cwd": str(tmp_path)}])
    assert pg.discover_peers(home, pid_alive=lambda pid: False) == []


def test_default_liveness_uses_real_pid_check(tmp_path):
    rows = [{"session_id": SID, "pid": os.getpid(), "cwd": str(tmp_path)}]
    assert [p["target"] for p in pg.discover_peers(registry(tmp_path, rows))] == [SID]
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    rows = [{"session_id": SID, "pid": proc.pid, "cwd": str(tmp_path)}]
    assert pg.discover_peers(registry(tmp_path, rows)) == []


@pytest.mark.parametrize("content", ["not json", "{}", "[1, 'x', {\"session_id\": 3}]",
                                     '[{"session_id": "a", "cwd": "/x", "pid": "9"}]'])
def test_malformed_registry_yields_no_peers(tmp_path, content):
    (tmp_path / "active_sessions.json").write_text(content, encoding="utf-8")
    assert pg.discover_peers(tmp_path, pid_alive=lambda pid: True) == []


def test_absent_registry_yields_no_peers(tmp_path):
    assert pg.discover_peers(tmp_path, pid_alive=lambda pid: True) == []


def test_taken_name_and_target_are_skipped(tmp_path):
    home = registry(tmp_path, [{"session_id": SID, "pid": 1, "cwd": str(tmp_path)}])
    name = pg.peer_name(SID, str(tmp_path))
    assert pg.discover_peers(home, lambda p: True, taken_names={name}) == []
    assert pg.discover_peers(home, lambda p: True, taken_targets={SID}) == []


def test_grok_home_honours_env(tmp_path, monkeypatch):
    monkeypatch.setenv("GROK_HOME", str(tmp_path))
    assert pg.grok_home() == tmp_path


def test_argv_is_cwd_pinned_without_add_dir_or_fork(tmp_path):
    argv = pg.resume_argv("grok", SID, "hello", str(tmp_path))
    assert argv[argv.index("--cwd") + 1] == str(tmp_path)
    assert argv[argv.index("-r") + 1] == SID
    assert "--add-dir" not in argv and "--fork-session" not in argv
    assert argv[argv.index("--permission-mode") + 1] == "dontAsk"


def run_turn(runner, tmp_path, timeout=5):
    return asyncio.run(pg.grok_turn(SID, str(tmp_path), "hi", timeout=timeout, binary="grok", runner=runner))


def test_turn_returns_reply_and_pins_cwd(tmp_path):
    seen = {}

    def runner(argv, **kw):
        seen.update(argv=argv, **kw)
        return subprocess.CompletedProcess(argv, 0, json.dumps({"text": "pong", "sessionId": SID}), "")

    assert run_turn(runner, tmp_path) == "pong"
    assert seen["cwd"] == str(tmp_path)
    assert "--cwd" in seen["argv"] and "--add-dir" not in seen["argv"]


def test_turn_timeout_nonzero_and_garbage_return_no_reply(tmp_path):
    def timeout(argv, **kw):
        raise subprocess.TimeoutExpired(argv, 1)

    assert run_turn(timeout, tmp_path) == pg.NO_REPLY
    assert run_turn(lambda a, **k: subprocess.CompletedProcess(a, 1, "", "err"), tmp_path) == pg.NO_REPLY
    assert run_turn(lambda a, **k: subprocess.CompletedProcess(a, 0, "garbage", ""), tmp_path) == pg.NO_REPLY
    assert run_turn(lambda a, **k: (_ for _ in ()).throw(FileNotFoundError()), tmp_path) == pg.NO_REPLY
