"""Windows live acceptance of the TaskStop reaper against real orphaned Git Bash trees.

Drives the recorder and reaper handler bodies with synthetic hook payloads. Every tree is launched by
this test and torn down by its recorded (pid, ctime); nothing is ever killed by name.
"""

import json
import os
import secrets
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from coordinator_core.bash_guards import _host_probe as hp
from coordinator_core.bash_guards import _process_kill as pk
from coordinator_core.bash_guards import _taskstop_launch_store as store
from coordinator_core.hooks import taskstop_launch_recorder as recorder
from coordinator_core.hooks import taskstop_reaper as reaper

pytestmark = [
    pytest.mark.spawns_process,
    pytest.mark.cadence,
    pytest.mark.skipif(sys.platform != "win32", reason="Windows-only: Git Bash orphan trees"),
]

SESSION = "live-session"
CPU_BUDGET_S = 0.5
S = 10_000_000
BEYOND_WINDOW_S = 2.6


def _git_bash() -> str:
    """bash.exe beside git.exe's install; PATH `bash` may be the WSL launcher."""
    git = shutil.which("git")
    if not git:
        pytest.skip("git not on PATH")
    for parent in Path(git).resolve().parents:
        cand = parent / "bin" / "bash.exe"
        if cand.is_file():
            return str(cand)
    pytest.skip("Git Bash not found beside git.exe")


class Launched:
    def __init__(self, task_id: str, command: str, procs):
        self.task_id = task_id
        self.command = command
        self.procs = list(procs)


_all_launched: list = []


def _tree_of(token: str, since: int):
    """Live (pid, ctime) of every process created at or after `since` whose command line carries
    token, plus descendants. Reading command lines of only fresh processes keeps the recorder
    mark close behind the tree's creation, as it is for a real launch."""
    rows = hp.snapshot() or []
    hit = {}
    for r in rows:
        if r.ctime < since:
            continue
        text = pk.command_line(r.pid, r.ctime)
        if text and token in text:
            hit[r.pid] = (r.pid, r.ctime)
    grew = True
    while grew:
        grew = False
        for r in rows:
            if r.ppid in hit and r.pid not in hit and r.ctime >= hit[r.ppid][1]:
                hit[r.pid] = (r.pid, r.ctime)
                grew = True
    return list(hit.values())


def _launch(task_id: str) -> Launched:
    """A real orphan: the outer Git Bash backgrounds a uniquely-tokened sleep and exits."""
    token = f"3001.{secrets.randbelow(10**9):09d}"
    command = f"sleep {token}"
    since = recorder._filetime_now() - S
    outer = subprocess.Popen(
        [_git_bash(), "-c", f"{command} &"],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    outer.wait(timeout=30)
    deadline = time.monotonic() + 10
    leaf = []
    while time.monotonic() < deadline and not leaf:
        leaf = [pc for pc in _tree_of(token, since) if "sleep.exe" in (pk.command_line(*pc) or "")]
        if not leaf:
            time.sleep(0.02)
    assert leaf, f"launched tree for {token} never appeared"
    launched = Launched(task_id, command, leaf)
    launched.token, launched.since = token, since
    _all_launched.append(launched)
    return launched


def _stragglers(launched: "Launched") -> list:
    """Live processes still carrying this launch's unique token, stub bash included."""
    return _tree_of(launched.token, launched.since)


def _record(launched: Launched):
    payload = {"payload": {
        "tool_name": "Bash", "session_id": SESSION, "tool_use_id": "tu-" + launched.task_id,
        "tool_input": {"command": launched.command, "run_in_background": True},
        "tool_response": {"backgroundTaskId": launched.task_id},
    }}
    return _timed(recorder._handler, payload)


def _reap(task_id: str):
    payload = {"payload": {
        "tool_name": "TaskStop", "session_id": SESSION, "tool_input": {"task_id": task_id},
    }}
    return _timed(reaper._handler, payload)


def _timed(handler, payload):
    """Run a handler under a spawn trap; (result, process-time seconds)."""
    def _trap(*a, **k):
        raise AssertionError("handler spawned a process")

    saved = (subprocess.Popen, os.system)
    subprocess.Popen, os.system = _trap, _trap
    try:
        t0 = time.process_time()
        result = handler(payload)
        spent = time.process_time() - t0
    finally:
        subprocess.Popen, os.system = saved
    return result, spent


def _alive(tree) -> list:
    return [pc for pc in tree if hp.alive(*pc)]


def _log_rows() -> list:
    path = store._log_path()
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path))
    yield
    for launched in _all_launched:
        for pid, ctime in launched.procs + _stragglers(launched):
            pk.terminate_verified(pid, ctime)
    leftover = [pc for launched in _all_launched for pc in _alive(launched.procs) + _stragglers(launched)]
    _all_launched.clear()
    assert not leftover, f"teardown left test-launched processes alive: {leftover}"


class _PeerNoise(Exception):
    """A stranger's orphan root sat in the launch window; the reaper correctly refused to kill."""


def _discard(*launched: Launched) -> None:
    for item in launched:
        for pid, ctime in item.procs + _stragglers(item):
            pk.terminate_verified(pid, ctime)


def _own_row(task_id: str) -> dict:
    rows = [r for r in _log_rows() if r["task_id"] == task_id]
    assert len(rows) == 1, rows
    return rows[0]


def _retry_on_peer_noise(scenario) -> None:
    """The box is shared: a peer's process landing in the window turns a clean reap ambiguous,
    which is the reaper's fail-safe. Retry the scenario on a fresh tree; a real fault repeats."""
    for _ in range(5):
        try:
            return scenario()
        except _PeerNoise:
            continue
    pytest.fail("peer noise on every attempt")


def _require_survivors(row: dict, *trees: Launched) -> None:
    """Every process of each tree must be alive after the reap. A death the reaper's row does not
    name was done by someone else on the shared box: discard the attempt as noise."""
    logged = {tuple(k) for k in row["killed"]}
    for tree in trees:
        assert not (logged & set(tree.procs)), f"reaper killed {tree.task_id}: row={row}"
        if _alive(tree.procs) != tree.procs:
            _discard(*trees)
            raise _PeerNoise


def _scenario_reap_own_tree():
    a = _launch("liveA" + secrets.token_hex(3))
    _record(a)
    time.sleep(BEYOND_WINDOW_S)
    b = _launch("liveB" + secrets.token_hex(3))
    _record(b)
    assert _alive(a.procs) == a.procs and _alive(b.procs) == b.procs

    _reap(a.task_id)

    row = _own_row(a.task_id)
    if row["outcome"] == "ambiguous" and not row["competing"]:
        _discard(a, b)
        store.delete_record(a.task_id)
        store.delete_record(b.task_id)
        raise _PeerNoise
    _require_survivors(row, b)
    assert _alive(a.procs) + _stragglers(a) == [], f"AC1: A's tree must be fully dead; row={row}"
    assert len(b.procs) > 0, "AC2: sibling B must be untouched"
    assert row["outcome"] == "killed"
    assert set(a.procs) <= {tuple(k) for k in row["killed"]}
    assert store.read_record(a.task_id) is None
    assert store.read_record(b.task_id) is not None


def test_reap_kills_own_tree_and_spares_sibling_outside_window():
    _retry_on_peer_noise(_scenario_reap_own_tree)


def _scenario_ambiguous():
    c = _launch("liveC" + secrets.token_hex(3))
    d = _launch("liveD" + secrets.token_hex(3))
    _record(c)
    _record(d)

    _reap(c.task_id)

    row = _own_row(c.task_id)
    _require_survivors(row, c, d)
    assert row["outcome"] == "ambiguous"
    assert d.task_id in row["competing"]
    assert row["killed"] == []


def test_overlapping_launch_windows_are_ambiguous_and_nothing_dies():
    _retry_on_peer_noise(_scenario_ambiguous)


def _scenario_cost():
    e = _launch("liveE" + secrets.token_hex(3))
    _, record_s = _record(e)
    _, reap_s = _reap(e.task_id)
    row = _own_row(e.task_id)
    if row["outcome"] == "ambiguous":
        _discard(e)
        store.delete_record(e.task_id)
        raise _PeerNoise
    assert _alive(e.procs) + _stragglers(e) == [], row
    assert record_s < CPU_BUDGET_S, f"recorder process time {record_s * 1000:.1f}ms"
    assert reap_s < CPU_BUDGET_S, f"reaper process time {reap_s * 1000:.1f}ms"
    print(f"AC4 figures: recorder={record_s * 1000:.1f}ms reaper={reap_s * 1000:.1f}ms")


def test_handlers_spawn_nothing_and_stay_under_cpu_budget():
    _retry_on_peer_noise(_scenario_cost)
