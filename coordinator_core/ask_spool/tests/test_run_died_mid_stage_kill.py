"""A run whose process is killed after stamping `plan_started_at` is halted `died_mid_stage` by the default drain."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from coordinator_core.ask_spool import drain
from coordinator_core.ask_spool.tests.test_drain import _notices
from coordinator_core.ops.dispatch_emit.ask_contract import RECORD_DIR_ROOT, write_run_stage_at

LATE = drain.MINTED_DISPATCH_DEADLINE_SECONDS + 60
READY_WAIT_SECONDS = 5.0
REPO_ROOT = Path(__file__).resolve().parents[3]

SIBLING_A = "ask-20261122T100000-aaaaaa"
SIBLING_B = "ask-20261122T100001-bbbbbb"

_CHILD = (
    "import sys\n"
    "from pathlib import Path\n"
    "from coordinator_core.ops.dispatch_emit import ask_contract\n"
    "rec, ready = Path(sys.argv[1]), Path(sys.argv[2])\n"
    "assert ask_contract.stamp_plan_phase_at(rec)\n"
    "ready.write_text('ready', encoding='utf-8')\n"
    "sys.stdin.read()\n"
)


@pytest.fixture(autouse=True)
def _isolated_settings_home(tmp_path, monkeypatch):
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path / "settings-home"))


def _record(root, run_id, session):
    rec = root / RECORD_DIR_ROOT / run_id
    rec.mkdir(parents=True)
    write_run_stage_at(rec, "minted", session_id=session)
    return rec


def _spawn(rec, ready):
    return subprocess.Popen(
        [sys.executable, "-c", _CHILD, str(rec), str(ready)],
        stdin=subprocess.PIPE,
        cwd=str(REPO_ROOT),
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def _await_ready(ready, proc):
    deadline = time.monotonic() + READY_WAIT_SECONDS
    while not ready.exists():
        assert proc.poll() is None, "sibling exited before reaching the plan stage"
        assert time.monotonic() < deadline, "sibling never wrote its ready file"
        time.sleep(0.02)


def _marker(root, run_id):
    path = root / RECORD_DIR_ROOT / run_id / "halted.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def _halts(root, session):
    return [n for n in _notices(root, session) if n.get("status") == "halted"]


def test_a_killed_sibling_is_halted_died_mid_stage_and_a_live_one_is_untouched(tmp_path):
    rec_a = _record(tmp_path, SIBLING_A, "sess-a")
    rec_b = _record(tmp_path, SIBLING_B, "sess-b")
    ready_a, ready_b = tmp_path / "ready-a", tmp_path / "ready-b"
    proc_a = proc_b = None
    try:
        proc_a = _spawn(rec_a, ready_a)
        proc_b = _spawn(rec_b, ready_b)
        _await_ready(ready_a, proc_a)
        _await_ready(ready_b, proc_b)
        proc_a.kill()
        proc_a.wait()

        now = time.time() + LATE
        recent = now - 30
        run_stage = rec_b / "run-stage.json"
        data = json.loads(run_stage.read_text(encoding="utf-8"))
        assert data["stage"] == "minted" and data.get("plan_started_at") and "phase" not in data
        data["last_activity_at"] = datetime.fromtimestamp(recent, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        run_stage.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        for path in (*rec_b.iterdir(), rec_b):
            os.utime(path, (recent, recent))

        drain.run(str(tmp_path), now=now)

        marker = _marker(tmp_path, SIBLING_A)
        assert marker["status"] == "halted" and marker["halted"] == drain.RUN_DIED_MID_STAGE
        assert "cause unknown" in marker["reason"]
        (halt,) = _halts(tmp_path, "sess-a")
        assert halt["run_id"] == SIBLING_A and halt["reason"] == marker["reason"]
        assert _marker(tmp_path, SIBLING_B) is None
        assert _halts(tmp_path, "sess-b") == []
    finally:
        for proc in (proc_a, proc_b):
            if proc is not None and proc.poll() is None:
                proc.kill()
            if proc is not None:
                proc.wait()
                if proc.stdin:
                    proc.stdin.close()
