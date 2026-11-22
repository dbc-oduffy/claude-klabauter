"""A codex stage silent on stdout and stderr past the stall window is killed with its tree, retried once, then halted by name."""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import psutil
import pytest

from coordinator_core.codex_runner import workflow as agents

# One JSONL event, then silence (or stderr-only progress when `stderr_only` exists) on the first attempt (every attempt when `always_hang` exists); its child sleeper
# proves the whole tree dies, not just the direct child.
_CLI = """
import json, os, subprocess, sys, time
args = sys.argv[1:]
HERE = os.path.dirname(os.path.abspath(__file__))
COUNT = HERE + '/count'
PIDS = HERE + '/pids'
sys.stdin.read()
n = (int(open(COUNT).read()) if os.path.exists(COUNT) else 0) + 1
open(COUNT, 'w').write(str(n))
print(json.dumps({'type': 'thread.started', 'thread_id': '8f3c1c1e-0d6e-4f64-9a53-3f2f5c1d9a11'}), flush=True)
if os.path.exists(HERE + '/stderr_only'):
    for _ in range(15):
        sys.stderr.write('progress' + chr(10)); sys.stderr.flush()
        time.sleep(0.2)
elif n == 1 or os.path.exists(HERE + '/always_hang'):
    kid = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
    open(PIDS, 'a').write(f'{os.getpid()} {kid.pid}' + chr(10))
    time.sleep(60)
open(args[args.index('-o') + 1], 'w', encoding='utf-8').write('{"ok":true}')
print(json.dumps({'type': 'turn.completed'}), flush=True)
"""


@pytest.fixture
def idle_stage(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    (tmp_path / ".git").mkdir()
    monkeypatch.setenv("COORDINATOR_SESSION_ID", "01a1b63b-acfb-78d1-86f2-a74ad6ca4572")
    host = SimpleNamespace(admission_file=lambda: "unused", read_recorded_peak=lambda *_a: None)
    monkeypatch.setattr(agents.capacity, "_load_box_memory_host", lambda: host)
    monkeypatch.setattr(agents.capacity, "admit_before_spawn",
                        lambda key, peak, **_kw: SimpleNamespace(admitted=True, peak_gib=peak, reason="ok"))
    monkeypatch.setattr(agents.capacity, "release_spawn", lambda *a, **kw: None)
    monkeypatch.setattr(agents.slot_grant, "check_slot_grant", lambda *a, **kw: (False, None))
    monkeypatch.setattr(agents.admission_post, "post", lambda *a, **kw: {"delivered": True})
    script = tmp_path / "cli.py"
    script.write_text(_CLI, encoding="utf-8")
    monkeypatch.setattr(agents, "resolve_codex", lambda **_kw: [sys.executable, str(script)])
    monkeypatch.setattr(agents, "preflight_auth", lambda **_kw: None)
    monkeypatch.setattr(agents, "_STALL_POLL_S", 0.05)
    return tmp_path


_OPTS = {"stallWindowS": 1.0}


def _killed(pids_file: Path) -> bool:
    pids = [int(p) for line in pids_file.read_text().splitlines() for p in line.split()]
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and any(psutil.pid_exists(p) and psutil.Process(p).status() != psutil.STATUS_ZOMBIE for p in pids):
        time.sleep(0.05)
    return not any(psutil.pid_exists(p) and psutil.Process(p).status() != psutil.STATUS_ZOMBIE for p in pids)


def test_idle_stage_is_killed_by_pid_then_retried_once_and_succeeds(idle_stage: Path) -> None:
    assert agents.run_agent("x", _OPTS, repo_root=idle_stage, artifact_dir=idle_stage / "agent") == '{"ok":true}'
    assert (idle_stage / "count").read_text() == "2"
    assert _killed(idle_stage / "pids")


def test_second_idle_halts_with_the_named_cause_and_does_not_retry_again(
    idle_stage: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    (idle_stage / "always_hang").write_text("")
    with pytest.raises(agents.StageStalled):
        agents.run_agent("x", _OPTS, repo_root=idle_stage, artifact_dir=idle_stage / "agent")
    assert (idle_stage / "count").read_text() == "2"
    assert _killed(idle_stage / "pids")
    cause = json.loads((idle_stage / "agent" / "stage-idle-timeout.json").read_text(encoding="utf-8"))
    assert cause["cause"] == "stage_idle_timeout" and cause["attempts"] == 2


def test_stage_writing_only_stderr_within_the_window_is_not_killed(idle_stage: Path) -> None:
    """Silence on stdout alone is not a stall: stderr progress for longer than the window keeps the stage alive."""
    (idle_stage / "stderr_only").write_text("")
    assert agents.run_agent("x", {"stallWindowS": 1.5}, repo_root=idle_stage, artifact_dir=idle_stage / "agent") == '{"ok":true}'
    assert (idle_stage / "count").read_text() == "1"
    assert not (idle_stage / "agent" / "stage-idle-timeout.json").exists()
