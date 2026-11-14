"""Windows live acceptance of the SubagentStop reaper against a real orphaned bash -> child tree.

Every process is launched by this test and torn down by its (pid, ctime); nothing is killed by name.
The child is a python stand-in for `grep` carrying a unique token; the bash parent is killed so the
child is an orphan, the shape a harness auto-backgrounded pipeline leaves behind.
"""

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
from coordinator_core.bash_guards._taskstop_contract import FILETIME_UNIX_EPOCH, LaunchRecord
from coordinator_core.hooks import agent_stop_reaper as reaper

pytestmark = [
    pytest.mark.spawns_process,
    pytest.mark.cadence,
    pytest.mark.skipif(sys.platform != "win32", reason="Windows-only: Git Bash orphan trees"),
]

SESSION, AGENT = "live-session", "live-agent"
S = 10_000_000
_spawned: list = []


def _git_bash() -> str:
    git = shutil.which("git")
    if not git:
        pytest.skip("git not on PATH")
    for parent in Path(git).resolve().parents:
        cand = parent / "bin" / "bash.exe"
        if cand.is_file():
            return str(cand)
    pytest.skip("Git Bash not found beside git.exe")


def _filetime_now() -> int:
    return int(time.time() * S) + FILETIME_UNIX_EPOCH


def _live_with_token(token: str):
    """Live (pid, ctime) of every process whose command line carries token."""
    out = []
    for r in hp.snapshot() or []:
        text = pk.command_line(r.pid, r.ctime)
        if text and token in text:
            out.append((r.pid, r.ctime))
    return out


def _launch(parent_alive: bool):
    """bash running a python stand-in 'grep'; with parent_alive False the bash is killed (orphan)."""
    token = f"agentstop.{secrets.randbelow(10**9):09d}"
    mark = _filetime_now() - S
    bash = subprocess.Popen(
        [_git_bash(), "-c", f'"{sys.executable}" -c "import time; time.sleep(120)" {token}; sleep 120'],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    deadline = time.monotonic() + 15
    procs = []
    while time.monotonic() < deadline and not procs:
        procs = _live_with_token(token)
        if not procs:
            time.sleep(0.05)
    assert procs, f"stand-in for {token} never appeared"
    _spawned.append(token)
    if not parent_alive:
        bash.kill()
        bash.wait(timeout=10)
    return token, mark, procs, bash


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path))
    yield
    for token in _spawned:
        for pid, ctime in _live_with_token(token):
            pk.terminate_verified(pid, ctime)
    leftover = [pc for t in _spawned for pc in _live_with_token(t)]
    _spawned.clear()
    assert not leftover, f"teardown left test-launched processes alive: {leftover}"


def _stop():
    return reaper._handler({"payload": {"session_id": SESSION, "agent_id": AGENT}})


def test_orphaned_standin_grep_is_reaped_and_unrelated_process_survives():
    bystander = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    try:
        token, mark, procs, _ = _launch(parent_alive=False)
        by = next(r for r in hp.snapshot() if r.pid == bystander.pid)
        store.write_record(LaunchRecord(
            "liveStop" + secrets.token_hex(3), SESSION, "tu", f"python -c x {token}", mark,
            agent_id=AGENT, timed_out=True,
        ))
        assert _stop() == {}
        rows = [r for r in (hp.snapshot() or []) if (r.pid, r.ctime) in set(procs)]
        outcome_rows = store._log_path().read_text(encoding="utf-8") if store._log_path().is_file() else ""
        if '"ambiguous"' in outcome_rows:
            pytest.skip("peer launch in the attribution window: reaper correctly refused")
        assert rows == [], f"orphan stand-in survived: {rows}; log={outcome_rows}"
        assert hp.alive(by.pid, by.ctime), "unrelated live process was killed"
    finally:
        bystander.kill()
        bystander.wait(timeout=10)


def test_live_parent_tree_is_not_reaped():
    token, mark, procs, bash = _launch(parent_alive=True)
    try:
        store.write_record(LaunchRecord(
            "liveKeep" + secrets.token_hex(3), SESSION, "tu", f"python -c x {token}", mark,
            agent_id=AGENT, timed_out=True,
        ))
        assert _stop() == {}
        assert [pc for pc in procs if hp.alive(*pc)] == procs, "a tree with a live parent was killed"
    finally:
        bash.kill()
        bash.wait(timeout=10)
