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
    return pg._clean(pg._contract().box_id(socket.gethostname()))


@pytest.mark.parametrize("host", ["Machine-b.local", "machine-b", "Machine-b"])
def test_peer_name_suffix_is_one_box_across_hostname_spellings(monkeypatch, host):
    monkeypatch.setattr(socket, "gethostname", lambda: host)
    assert pg.peer_name(SID, "/nowhere/myrepo") == "grok-myrepo-01a1c000-on-machine-b"


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


def test_own_session_matches_an_ancestor_pid_to_the_registry_pid(tmp_path):
    home = registry(tmp_path, [{"session_id": SID, "pid": 100, "cwd": str(tmp_path)}])
    parents = {900: 500, 500: 100, 100: 1}
    peer = pg.own_session(home, pid_alive=lambda p: True, parent_of=parents.get, start=900)
    assert peer["target"] == SID and peer["name"] == pg.peer_name(SID, str(tmp_path))


def test_own_session_none_when_no_ancestor_is_a_registry_pid(tmp_path):
    home = registry(tmp_path, [{"session_id": SID, "pid": 100, "cwd": str(tmp_path)}])
    parents = {900: 500, 500: 1}
    assert pg.own_session(home, pid_alive=lambda p: True, parent_of=parents.get, start=900) is None


def test_own_session_ignores_a_dead_registry_row_and_survives_a_parent_cycle(tmp_path):
    home = registry(tmp_path, [{"session_id": SID, "pid": 100, "cwd": str(tmp_path)}])
    assert pg.own_session(home, pid_alive=lambda p: False, parent_of={900: 100}.get, start=900) is None
    live = registry(tmp_path, [{"session_id": SID, "pid": 7, "cwd": str(tmp_path)}])
    assert pg.own_session(live, pid_alive=lambda p: True, parent_of={900: 500, 500: 900}.get, start=900) is None


def test_own_session_empty_registry_never_walks_processes(tmp_path):
    def boom(pid):
        raise AssertionError("walked")

    assert pg.own_session(registry(tmp_path, []), parent_of=boom, start=1) is None
    assert pg.own_session(tmp_path / "missing", parent_of=boom, start=1) is None


def test_real_parent_pid_finds_this_processes_parent():
    assert pg.parent_pid(os.getpid()) == os.getppid()


def test_own_session_real_walk_from_a_child_process_finds_the_registered_ancestor(tmp_path):
    home = registry(tmp_path, [{"session_id": SID, "pid": os.getpid(), "cwd": str(tmp_path)}])
    code = ("import importlib.util,sys;"
            f"s=importlib.util.spec_from_file_location('g',r'{BIN / 'unimatrix-peer-grok.py'}');"
            "m=importlib.util.module_from_spec(s);s.loader.exec_module(m);"
            f"p=m.own_session(r'{home}');print(p['target'] if p else '')")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=30)
    assert out.stdout.strip() == SID
