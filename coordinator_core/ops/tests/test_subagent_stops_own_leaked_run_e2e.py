"""A subagent's leaked background run is ledgered by the PostToolUse recorder and stopped through
proc.stop_own_child at default params, entering through both registered op handlers on a real process."""

from __future__ import annotations

import os
import subprocess
import sys
import time
import uuid

import psutil
import pytest

from coordinator_core.bash_guards._taskstop_contract import FILETIME_UNIX_EPOCH
from coordinator_core.bash_guards import _host_probe
from coordinator_core.hooks import taskstop_launch_recorder as rec
from coordinator_core.ops import _spawn_ledger as ledger
from coordinator_core.ops import worker_cleanup as wc

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="the launch recorder captures on Windows only")

SESSION = "sessE2eParent"
OTHER_SESSION = "sessE2eOther"
AGENT = "subagentE2e"


@pytest.fixture
def sleeper(tmp_path):
    script = tmp_path / f"leaked_run_{uuid.uuid4().hex}.py"
    script.write_text("import time\ntime.sleep(600)\n", encoding="utf-8")
    script_text = str(script).replace("\\", "/")
    command = f'"{sys.executable.replace(chr(92), "/")}" "{script_text}"'
    holder = {}

    def launch() -> subprocess.Popen:
        proc = subprocess.Popen(
            [sys.executable, script_text],
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        holder["proc"] = proc
        return proc

    yield command, launch
    proc = holder.get("proc")
    if proc is not None and proc.poll() is None:
        proc.kill()
        proc.wait(5)


def _wait_cmdline(pid: int, needle: str) -> None:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            if needle in " ".join(psutil.Process(pid).cmdline()):
                return
        except psutil.Error:
            pass
        time.sleep(0.05)
    raise AssertionError("sleeper command line never became readable")


def _stop(pid: int, session: str) -> dict:
    return wc._stop_handler(
        {"pid": pid, "caller_pid": psutil.Process().pid, "caller_session_id": session}, repo_root=None
    )


def test_subagent_ledgered_background_run_is_stopped_by_its_session_and_refused_to_another(
    tmp_path, monkeypatch, sleeper
):
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path / "home"))
    # No codex ancestor: the ledger, not the descendant path, must be what admits the stop.
    monkeypatch.setattr(wc, "_ROOT_NAMES", frozenset({"no-such-root-name"}))
    command, launch = sleeper
    proc = launch()
    _wait_cmdline(proc.pid, "leaked_run_")
    # Pin the hook's launch mark just after the sleeper's start so a loaded box cannot slide the
    # attribution window past it; process table, command line and create_time stay real.
    launched = int(psutil.Process(proc.pid).create_time() * 1e7) + FILETIME_UNIX_EPOCH
    monkeypatch.setattr(rec, "_filetime_now", lambda: launched + 2_000_000)
    # Peers' processes born in the same window (or vanishing mid-read) would make a live box abort
    # or ambiguate the capture; rows stay the real probe's, narrowed to the sleeper and its parent.
    monkeypatch.setattr(
        rec,
        "_snapshot",
        lambda: [r for r in _host_probe.snapshot() if r.pid in (proc.pid, os.getpid())],
    )

    out = rec._handler(
        {
            "session_id": SESSION,
            "agent_id": AGENT,
            "tool_name": "Bash",
            "tool_use_id": "toolu_e2e",
            "tool_input": {"command": command, "run_in_background": True},
            "tool_response": {"backgroundTaskId": "bgE2e0001"},
        }
    )
    assert out is not None
    row = ledger.read_row(SESSION, proc.pid)
    assert row is not None, "recorder wrote no ledger row for the background launch"
    assert row.agent_id == AGENT
    assert row.create_time == psutil.Process(proc.pid).create_time()

    refused = _stop(proc.pid, OTHER_SESSION)
    assert "refused" in refused and proc.poll() is None
    assert "another session's spawn ledger" in refused["refused"]
    assert ledger.read_row(SESSION, proc.pid) is not None

    # The client stamps the session from its env: a subagent's shell carries the parent session's id.
    for slot in ("COORDINATOR_SESSION_ID", "CLAUDE_SESSION_ID"):
        monkeypatch.delenv(slot, raising=False)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", SESSION)
    from coordinator_core.session.core import resolve_session_id

    assert _stop(proc.pid, resolve_session_id()) == {"stopped": proc.pid}
    assert proc.wait(10) is not None
    assert not psutil.pid_exists(proc.pid) or psutil.Process(proc.pid).status() == psutil.STATUS_ZOMBIE
    assert ledger.read_row(SESSION, proc.pid) is None
