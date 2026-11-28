"""scratch.remove and proc.stop_own_child refuse out-of-scope targets; the stage clause and kill denial name them."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time

import psutil
import pytest

from coordinator_core.bash_guards import block_kill_by_port_or_tree as kill_guard
from coordinator_core.codex_runner import workflow
from coordinator_core.ops import _spawn_ledger as ledger
from coordinator_core.ops import worker_cleanup as wc


def test_scratch_remove_admits_only_codex_tmp_and_tmp(tmp_path):
    for rel in ("scratch/codex-tmp/a/b", "scratch/tmp/c", "src/keep", "scratch/other"):
        (tmp_path / rel).mkdir(parents=True)
    out = wc.remove_scratch(tmp_path, [
        "scratch/codex-tmp/a", str(tmp_path / "scratch/tmp/c"), "src/keep", "scratch/other",
        "scratch/codex-tmp", "scratch/codex-tmp/../other", "scratch/codex-tmp/gone",
    ])
    assert sorted(out["removed"]) == sorted(["scratch/codex-tmp/a", str(tmp_path / "scratch/tmp/c")])
    assert out["absent"] == ["scratch/codex-tmp/gone"]
    assert {r["path"] for r in out["refused"]} == {
        "src/keep", "scratch/other", "scratch/codex-tmp", "scratch/codex-tmp/../other"}
    assert (tmp_path / "src/keep").is_dir() and (tmp_path / "scratch/other").is_dir()
    assert not (tmp_path / "scratch/codex-tmp/a").exists()


def _child():
    return subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])


def test_stop_own_child_stops_a_descendant():
    child = _child()
    try:
        out = wc.stop_own_child(child.pid, os.getpid(), root_pid=os.getpid())
        assert out == {"stopped": child.pid}
        assert child.wait(5) is not None
    finally:
        child.kill()


def test_stop_own_child_refuses_non_descendant_and_ancestor():
    parent = psutil.Process().parent().pid
    child, sibling = _child(), _child()
    try:
        # Caller is `child` (its own root); `sibling` is a live process outside that root.
        assert "not a descendant" in wc.stop_own_child(sibling.pid, child.pid, root_pid=child.pid)["refused"]
        assert sibling.poll() is None
        # root_pid naming the caller's own child is admitted as root; an ancestor stays refused.
        assert "ancestors" in wc.stop_own_child(parent, os.getpid(), root_pid=child.pid)["refused"]
        assert "ancestors" in wc.stop_own_child(os.getpid(), os.getpid(), root_pid=os.getpid())["refused"]
        assert child.poll() is None
    finally:
        child.kill()
        sibling.kill()


def test_stop_own_child_at_default_params_stops_a_launched_python_child():
    child = _child()
    try:
        assert wc.stop_own_child(child.pid, os.getpid()) == {"stopped": child.pid}
        assert child.wait(5) is not None
    finally:
        child.kill()


def test_stop_own_child_at_default_params_refuses_the_callers_ancestor():
    parent = psutil.Process().parent().pid
    assert "ancestors" in wc.stop_own_child(parent, os.getpid())["refused"]


def test_stop_own_child_rejects_bad_pid():
    assert wc.stop_own_child(1, os.getpid())["refused"]
    assert wc.stop_own_child("9", os.getpid())["refused"]


class _FakeProc:
    def __init__(self, table, pid, ppid, name, created):
        self._table, self.pid, self._ppid, self._name, self._created = table, pid, ppid, name, created
        self.stopped = False
        self.env = {}
        self.env_error = None
        self.env_reads = 0

    def environ(self):
        self.env_reads += 1
        if self.env_error:
            raise self.env_error
        return self.env

    def name(self):
        return self._name

    def create_time(self):
        return self._created

    def parent(self):
        return self._table.procs.get(self._ppid)

    def terminate(self):
        self.stopped = True

    def wait(self, timeout):
        return 0

    def kill(self):
        self.stopped = True


class _FakePs:
    class NoSuchProcess(Exception):
        pass

    class AccessDenied(Exception):
        pass

    class TimeoutExpired(Exception):
        pass

    def __init__(self):
        self.procs = {}

    def add(self, pid, ppid, name, created):
        self.procs[pid] = _FakeProc(self, pid, ppid, name, created)
        return self.procs[pid]

    def Process(self, pid):
        if pid not in self.procs:
            raise self.NoSuchProcess(pid)
        return self.procs[pid]


def _serve_world():
    """codex(10) > shell(20) caller(30); serve(50) is a sibling tree; child(60) hangs off the serve."""
    ps = _FakePs()
    ps.add(10, 1, "codex.exe", 100.0)
    ps.add(20, 10, "pwsh.exe", 101.0)
    ps.add(30, 20, "python.exe", 102.0)
    ps.add(50, 1, "serve.exe", 90.0)
    ps.add(60, 50, "server.exe", 110.0)
    return ps


def test_recorded_serve_parented_child_is_stopped(tmp_path):
    ps = _serve_world()
    ask = dict(caller_pid=30, ps=ps, repo_root=tmp_path)
    assert "no spawn record" in wc.stop_own_child(60, **ask)["refused"]
    assert not ps.procs[60].stopped
    assert wc.stop_own_child(60, record=True, **ask) == {"recorded": 60}
    assert wc.stop_own_child(60, **ask) == {"stopped": 60}
    assert ps.procs[60].stopped


def test_unrecorded_and_other_owner_pid_refused(tmp_path):
    ps = _serve_world()
    ps.add(31, 20, "python.exe", 103.0)
    ps.add(40, 1, "codex.exe", 100.0)
    ps.add(41, 40, "python.exe", 104.0)
    assert wc.stop_own_child(60, 41, ps=ps, repo_root=tmp_path, record=True) == {"recorded": 60}
    out = wc.stop_own_child(60, 30, ps=ps, repo_root=tmp_path)
    assert "no spawn record" in out["refused"] and not ps.procs[60].stopped
    assert wc.stop_own_child(50, 30, ps=ps, repo_root=tmp_path)["refused"]
    assert not ps.procs[50].stopped


def test_reused_pid_with_new_create_time_is_refused(tmp_path):
    ps = _serve_world()
    ask = dict(caller_pid=30, ps=ps, repo_root=tmp_path)
    assert wc.stop_own_child(60, record=True, **ask) == {"recorded": 60}
    ps.add(60, 50, "other.exe", 120.0)
    assert "pid reused" in wc.stop_own_child(60, **ask)["refused"]
    assert not ps.procs[60].stopped


def test_record_refuses_a_pid_older_than_the_root_and_the_callers_ancestors(tmp_path):
    ps = _serve_world()
    ask = dict(caller_pid=30, ps=ps, repo_root=tmp_path, record=True)
    assert "before the worker root" in wc.stop_own_child(50, **ask)["refused"]
    assert "ancestors" in wc.stop_own_child(20, **ask)["refused"]
    assert "no repo root" in wc.stop_own_child(60, caller_pid=30, ps=ps, record=True)["refused"]
    assert not (tmp_path / "scratch").exists()


def test_record_refuses_a_pid_under_another_codex_root(tmp_path):
    ps = _serve_world()
    ps.add(40, 1, "codex.exe", 100.0)
    ps.add(41, 40, "python.exe", 104.0)
    out = wc.stop_own_child(41, caller_pid=30, ps=ps, repo_root=tmp_path, record=True)
    assert "another agent session" in out["refused"]
    assert not (tmp_path / "scratch").exists()


def test_record_refuses_a_pid_under_a_claude_session_but_admits_a_plain_serve_child(tmp_path):
    ps = _serve_world()
    ps.add(70, 1, "claude.exe", 95.0)
    ps.add(71, 70, "python.exe", 111.0)
    ask = dict(caller_pid=30, ps=ps, repo_root=tmp_path, record=True)
    assert "another agent session" in wc.stop_own_child(71, **ask)["refused"]
    assert wc.stop_own_child(60, **ask) == {"recorded": 60}


def test_records_are_one_file_per_pid_and_dead_roots_are_reaped(tmp_path):
    ps = _serve_world()
    ps.add(61, 50, "server.exe", 111.0)
    ask = dict(caller_pid=30, ps=ps, repo_root=tmp_path, record=True)
    dead = tmp_path / "scratch" / "spawn-records" / "999-5"
    dead.mkdir(parents=True)
    (dead / "7").write_text("1.0")
    assert wc.stop_own_child(60, **ask) == {"recorded": 60}
    assert wc.stop_own_child(61, **ask) == {"recorded": 61}
    live = tmp_path / "scratch" / "spawn-records" / "10-100"
    assert sorted(f.name for f in live.iterdir()) == ["60", "61"]
    assert not dead.exists()


def test_invoke_stamps_caller_pid_over_a_spoofed_one():
    root = subprocess.Popen([sys.executable, "-c",
        "import subprocess,sys,time;g=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)']);"
        "print(g.pid,flush=True);time.sleep(60)"], stdout=subprocess.PIPE, text=True)
    grand = int(root.stdout.readline())
    try:
        params = json.dumps({"pid": grand, "caller_pid": root.pid, "root_pid": root.pid})
        out = subprocess.run([sys.executable, "-m", "coordinator_core.invoke", "proc.stop_own_child", params],
                             capture_output=True, text=True, timeout=60)
        assert "neither an ancestor nor a descendant" in out.stdout + out.stderr
        assert "REFUSED" in out.stderr
        assert psutil.pid_exists(grand)
    finally:
        for pid in (grand, root.pid):
            try:
                psutil.Process(pid).kill()
            except psutil.NoSuchProcess:
                pass


def _em_world():
    """claude(10) > shell(20) > caller(30); nohup(55) is orphaned, child(60) under it."""
    ps = _FakePs()
    ps.add(10, 1, "claude.exe", 100.0)
    ps.add(20, 10, "bash.exe", 101.0)
    ps.add(30, 20, "python.exe", 102.0)
    ps.add(55, 4242, "nohup.exe", 110.0)
    ps.add(60, 55, "python.exe", 111.0)
    return ps


@pytest.fixture
def ledger_home(tmp_path, monkeypatch):
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path))
    monkeypatch.setattr(ledger, "_reap", lambda _d: None)
    return tmp_path


def _ledgered(session, pid=60, created=111.0):
    assert ledger.write_row(ledger.LedgerRow(pid, created, session, "subA", ["python", "loop.py"]))


def test_ledgered_reparented_pid_of_the_callers_session_is_stopped(ledger_home):
    ps = _em_world()
    _ledgered("sessA")
    ask = dict(caller_pid=30, ps=ps, caller_session_id="sessA")
    assert wc.stop_own_child(60, **ask) == {"stopped": 60}
    assert ps.procs[60].stopped
    assert ledger.read_row("sessA", 60) is None


def test_ledgered_pid_of_a_foreign_session_is_refused(ledger_home):
    ps = _em_world()
    _ledgered("sessB")
    out = wc.stop_own_child(60, caller_pid=30, ps=ps, caller_session_id="sessA")
    assert "another session's spawn ledger" in out["refused"]
    assert not ps.procs[60].stopped
    assert ledger.read_row("sessB", 60) is not None


def test_ledgered_pid_with_a_reused_create_time_is_refused_and_its_row_deleted(ledger_home):
    ps = _em_world()
    _ledgered("sessA", created=5.0)
    out = wc.stop_own_child(60, caller_pid=30, ps=ps, caller_session_id="sessA")
    assert "pid reused" in out["refused"]
    assert not ps.procs[60].stopped
    assert ledger.read_row("sessA", 60) is None


def test_unledgered_pid_keeps_the_original_refusal(ledger_home):
    ps = _em_world()
    out = wc.stop_own_child(60, caller_pid=30, ps=ps, caller_session_id="sessA")
    assert "not a descendant" in out["refused"]
    assert not ps.procs[60].stopped


def test_empty_caller_session_is_refused_even_for_a_ledgered_pid(ledger_home):
    ps = _em_world()
    _ledgered("sessA")
    out = wc.stop_own_child(60, caller_pid=30, ps=ps, caller_session_id="")
    assert "caller session unknown" in out["refused"]
    out = wc.stop_own_child(60, caller_pid=30, ps=ps)
    assert "not a descendant" in out["refused"]
    assert not ps.procs[60].stopped


def test_ledger_never_admits_a_caller_ancestor(ledger_home):
    ps = _em_world()
    _ledgered("sessA", pid=20, created=101.0)
    out = wc.stop_own_child(20, caller_pid=30, ps=ps, caller_session_id="sessA")
    assert "ancestors" in out["refused"]
    assert not ps.procs[20].stopped


def test_ledger_admits_beside_a_worker_root_when_the_record_path_refuses(ledger_home, tmp_path):
    ps = _serve_world()
    ps.add(70, 50, "server.exe", 120.0)
    _ledgered("sessA", pid=70, created=120.0)
    ask = dict(caller_pid=30, ps=ps, repo_root=tmp_path)
    assert wc.stop_own_child(70, caller_session_id="sessB", **ask)["refused"]
    assert wc.stop_own_child(70, caller_session_id="sessA", **ask) == {"stopped": 70}


def test_record_true_never_consults_the_ledger(ledger_home):
    ps = _em_world()
    _ledgered("sessA")
    out = wc.stop_own_child(60, caller_pid=30, ps=ps, record=True, caller_session_id="sessA")
    assert "no repo root" in out["refused"] and not ps.procs[60].stopped


def _orphan() -> int:
    """A sleeping process whose parent has exited, so no agent session sits above it."""
    # Trap: the sleeper must not inherit the captured stdout pipe, or capture_output waits for it to exit.
    code = ("import subprocess,sys;p=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)'],"
            "stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL);"
            "print(p.pid,flush=True)")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60)
    return int(out.stdout.strip())


def test_invoke_stamps_caller_session_over_a_spoofed_one(tmp_path):
    victim = _orphan()
    created = psutil.Process(victim).create_time()
    env = dict(os.environ, COORDINATOR_SETTINGS_HOME=str(tmp_path), COORDINATOR_SESSION_ID="mine")
    row = ledger.LedgerRow(victim, created, "victim-sess", "", ["python"])
    path = tmp_path / "spawn-ledger" / "victim-sess" / f"{victim}.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(row.__dict__), encoding="utf-8")
    params = json.dumps({"pid": victim, "caller_session_id": "victim-sess"})
    out = subprocess.run([sys.executable, "-m", "coordinator_core.invoke", "proc.stop_own_child", params],
                         capture_output=True, text=True, timeout=60, env=env)
    assert "stopped" not in out.stdout
    assert psutil.pid_exists(victim)


def _session_world():
    """claude(10) > shell(20) > caller(30), pytest(31); peer claude(40) > shell(41) > job(42); a nested
    session claude(32) under the caller's shell > job(33); the caller's own job(34) under shell(20)."""
    ps = _FakePs()
    ps.add(10, 1, "claude.exe", 100.0)
    ps.add(20, 10, "bash.exe", 101.0)
    ps.add(30, 20, "python.exe", 102.0)
    ps.add(31, 20, "pytest.exe", 103.0)
    ps.add(34, 31, "python.exe", 104.0)
    ps.add(32, 20, "claude.exe", 105.0)
    ps.add(33, 32, "python.exe", 106.0)
    ps.add(40, 1, "claude.exe", 100.0)
    ps.add(41, 40, "bash.exe", 101.0)
    ps.add(42, 41, "python.exe", 102.0)
    return ps


def test_claude_session_descendant_is_stopped_without_a_ledger_row(tmp_path):
    ps = _session_world()
    assert wc.stop_own_child(34, caller_pid=30, ps=ps, repo_root=tmp_path) == {"stopped": 34}
    assert ps.procs[34].stopped


def test_unix_style_claude_name_resolves_the_session_root(tmp_path):
    ps = _session_world()
    ps.procs[10]._name = "claude"
    assert wc.stop_own_child(34, caller_pid=30, ps=ps, repo_root=tmp_path) == {"stopped": 34}


def test_peer_session_pid_and_the_session_process_are_refused(tmp_path):
    ps = _session_world()
    ask = dict(caller_pid=30, ps=ps, repo_root=tmp_path)
    for pid in (42, 41, 40):
        assert "not a descendant" in wc.stop_own_child(pid, **ask)["refused"]
    assert "ancestors" in wc.stop_own_child(10, **ask)["refused"]
    assert "ancestors" in wc.stop_own_child(20, **ask)["refused"]
    assert "another agent session" in wc.stop_own_child(32, **ask)["refused"]
    assert not any(ps.procs[p].stopped for p in (10, 20, 32, 40, 41, 42))


def test_session_root_is_the_nearest_so_a_nested_session_is_scoped(tmp_path):
    ps = _session_world()
    # Caller inside the nested session 32: its own job is stoppable, the outer session's jobs are not.
    ps.add(35, 32, "python.exe", 107.0)
    assert wc.stop_own_child(33, caller_pid=35, ps=ps, repo_root=tmp_path) == {"stopped": 33}
    assert "not a descendant" in wc.stop_own_child(34, caller_pid=35, ps=ps, repo_root=tmp_path)["refused"]
    # From the outer session, a job under the nested session is another session's tree.
    out = wc.stop_own_child(35, caller_pid=30, ps=ps, repo_root=tmp_path)
    assert "another agent session" in out["refused"] and not ps.procs[35].stopped


def _detached_world():
    """bash(20) > guard(30) > run(34) > worker(35); guard also has an unrelated sibling shell(21) > job(36);
    peer claude(40) > job(42)."""
    ps = _FakePs()
    ps.add(20, 1, "bash.exe", 101.0)
    ps.add(30, 20, "python.exe", 102.0)
    ps.add(34, 30, "python.exe", 103.0)
    ps.add(35, 34, "python.exe", 104.0)
    ps.add(21, 1, "bash.exe", 101.0)
    ps.add(36, 21, "python.exe", 105.0)
    ps.add(40, 1, "claude.exe", 100.0)
    ps.add(42, 40, "python.exe", 102.0)
    return ps


def test_detached_caller_stops_its_own_descendant_with_only_pid(tmp_path):
    ps = _detached_world()
    assert wc.stop_own_child(35, caller_pid=30, ps=ps, repo_root=tmp_path) == {"stopped": 35}
    assert ps.procs[35].stopped


def test_detached_caller_refuses_foreign_ancestor_and_session_pids(tmp_path):
    ps = _detached_world()
    ask = dict(caller_pid=30, ps=ps, repo_root=tmp_path)
    for pid in (36, 42, 21):
        assert "not a descendant" in wc.stop_own_child(pid, **ask)["refused"]
    assert "ancestors" in wc.stop_own_child(20, **ask)["refused"]
    assert "ancestors" in wc.stop_own_child(30, **ask)["refused"]
    assert not any(ps.procs[p].stopped for p in (20, 21, 30, 36, 40, 42))


def test_root_pid_naming_the_callers_child_admits_it_and_its_tree_only(tmp_path):
    ps = _detached_world()
    ps.add(37, 30, "python.exe", 106.0)
    ask = dict(caller_pid=30, root_pid=34, ps=ps, repo_root=tmp_path)
    assert wc.stop_own_child(35, **ask) == {"stopped": 35}
    assert wc.stop_own_child(34, **ask) == {"stopped": 34}
    assert "not a descendant" in wc.stop_own_child(37, **ask)["refused"]
    assert not ps.procs[37].stopped


def test_root_pid_outside_the_callers_lineage_or_under_a_session_is_refused(tmp_path):
    ps = _detached_world()
    ps.add(38, 30, "claude.exe", 107.0)
    ps.add(39, 38, "python.exe", 108.0)
    for root in (36, 42, 39, 4040):
        out = wc.stop_own_child(35, caller_pid=30, root_pid=root, ps=ps, repo_root=tmp_path)
        assert "refused" in out
    assert not any(p.stopped for p in ps.procs.values())


def test_handler_refusal_is_a_loud_non_success_reply(monkeypatch):
    monkeypatch.setattr(wc, "stop_own_child", lambda *a, **k: {"refused": "pid is not a descendant"})
    out = wc._stop_handler({"pid": 5, "caller_pid": 30})
    assert out["exit_code"] == 1 and "REFUSED" in out["error"] and "pid is not a descendant" in out["error"]
    monkeypatch.setattr(wc, "stop_own_child", lambda *a, **k: {"stopped": 5})
    assert wc._stop_handler({"pid": 5, "caller_pid": 30}) == {"stopped": 5}


def _env_ask(ps, pid=60, session="sessA"):
    return wc.stop_own_child(pid, caller_pid=30, ps=ps, caller_session_id=session)


def test_env_marker_naming_the_callers_session_admits_an_orphan(ledger_home):
    ps = _em_world()
    ps.procs[60].env = {"COORDINATOR_SESSION_ID": "sessA"}
    assert _env_ask(ps) == {"stopped": 60}
    assert ps.procs[60].stopped and ps.procs[60].env_reads == 1


def test_env_marker_of_another_session_keeps_the_original_refusal(ledger_home):
    ps = _em_world()
    ps.procs[60].env = {"COORDINATOR_SESSION_ID": "sessB"}
    assert "not a descendant" in _env_ask(ps)["refused"]
    assert not ps.procs[60].stopped


def test_env_without_a_session_variable_keeps_the_original_refusal(ledger_home):
    ps = _em_world()
    ps.procs[60].env = {"PATH": "x"}
    assert "not a descendant" in _env_ask(ps)["refused"]
    assert not ps.procs[60].stopped


def test_env_access_denied_refuses_cleanly(ledger_home):
    ps = _em_world()
    ps.procs[60].env_error = ps.AccessDenied()
    assert _env_ask(ps) == {"refused": "access denied"}
    assert not ps.procs[60].stopped
    ps.procs[60].env_error = psutil.AccessDenied(60)
    assert wc._env_admits(ps.procs[60], "sessA", root=ps.procs[10]) == (False, "access denied")


def test_env_target_exiting_mid_check_refuses_without_raising(ledger_home):
    ps = _em_world()
    for exc in (psutil.NoSuchProcess(60), psutil.ZombieProcess(60)):
        ps.procs[60].env_error = exc
        assert wc._env_admits(ps.procs[60], "sessA") == (False, "")
    assert not ps.procs[60].stopped


def test_env_admission_is_skipped_without_a_caller_session(ledger_home):
    ps = _em_world()
    ps.procs[60].env = {"COORDINATOR_SESSION_ID": "sessA"}
    for session in (None, ""):
        assert wc._env_admits(ps.procs[60], session) == (False, "")
    assert ps.procs[60].env_reads == 0


def test_env_admission_never_overrides_a_foreign_ledger_or_reused_pid_refusal(ledger_home):
    ps = _em_world()
    ps.procs[60].env = {"COORDINATOR_SESSION_ID": "sessA"}
    _ledgered("sessB")
    assert "another session's spawn ledger" in _env_ask(ps)["refused"]
    _ledgered("sessA", created=5.0)
    assert "pid reused" in _env_ask(ps)["refused"]
    assert not ps.procs[60].stopped and ps.procs[60].env_reads == 0


def test_env_admission_never_reaches_an_agent_session_target(ledger_home):
    ps = _session_world()
    for p in (32, 33, 40):
        ps.procs[p].env = {"COORDINATOR_SESSION_ID": "sessA"}
    for pid in (32, 33, 40):
        assert "refused" in _env_ask(ps, pid)
    assert not any(ps.procs[p].stopped for p in (32, 33, 40))
    assert all(ps.procs[p].env_reads == 0 for p in (32, 33, 40))


def test_a_session_process_beside_the_root_is_not_admitted_by_environment(ledger_home, tmp_path):
    ps = _serve_world()
    ps.add(70, 1, "claude.exe", 120.0).env = {"COORDINATOR_SESSION_ID": "sessA"}
    assert "refused" in wc.stop_own_child(70, caller_pid=30, ps=ps, repo_root=tmp_path, caller_session_id="sessA")
    assert not ps.procs[70].stopped and ps.procs[70].env_reads == 0


def test_env_session_resolves_by_precedence_not_one_variable(ledger_home):
    ps = _em_world()
    ps.procs[60].env = {"CLAUDE_CODE_SESSION_ID": "sessA", "COORDINATOR_SESSION_ID": "sessB"}
    assert "refused" in _env_ask(ps) and not ps.procs[60].stopped
    assert _env_ask(ps, session="sessB") == {"stopped": 60}
    ps = _em_world()
    ps.procs[60].env = {"CLAUDE_CODE_SESSION_ID": "sessA"}
    assert _env_ask(ps) == {"stopped": 60}


def test_env_native_session_id_is_canonicalised_like_a_request_env(ledger_home):
    from coordinator_core.session.core import canonical_session_env_value

    ps = _em_world()
    ps.procs[60].env = {"CODEX_THREAD_ID": "opaque-thread", "COORDINATOR_AGENT_VENDOR": "codex"}
    canon = canonical_session_env_value("CODEX_THREAD_ID", "opaque-thread", vendor="codex")
    assert _env_ask(ps, session="opaque-thread").get("refused")
    assert _env_ask(ps, session=canon) == {"stopped": 60}


def test_env_admission_never_admits_the_serving_engine_or_its_ancestors(ledger_home):
    ps = _em_world()
    for pid in (os.getpid(), os.getppid()):
        ps.add(pid, 4242, "python.exe", 111.0).env = {"COORDINATOR_SESSION_ID": "sessA"}
        assert wc._env_admits(ps.procs[pid], "sessA") == (False, "")
        assert "refused" in _env_ask(ps, pid) and ps.procs[pid].env_reads == 0


def test_clause_invocations_are_not_dangerous_shapes():
    clause = workflow.CODEX_CLEANUP_CLAUSE
    assert "coordinator-invoke scratch.remove" in clause and "coordinator-invoke proc.stop_own_child" in clause
    invocations = re.findall(r"coordinator-invoke \S+ '[^']*'", clause)
    assert len(invocations) == 2
    for text in invocations:
        assert not re.search(r"remove-item|\brm\b|stop-process|taskkill|-recurse|-force", text, re.I)
        assert kill_guard._decide(text, os.getcwd()) is None


def test_kill_denial_names_the_rule_and_the_op():
    cmd = 'Get-CimInstance Win32_Process | ForEach-Object { Stop-Process -Id $_.ProcessId }'
    label, where = kill_guard._decide(cmd, os.getcwd())
    text = str(kill_guard._deny_record(label, where))
    assert "DENY-KILL-BY-PORT-OR-TREE" in text and "coordinator-invoke proc.stop_own_child" in text
