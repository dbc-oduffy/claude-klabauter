"""The warm client's unstamped-clone decline must stay cheap.

Hop question answered: is this clone a warm host? An unstamped one is not, by
ruling, so the decline must not import the election/caller/env machinery or
touch the pipe, while still printing the live-tree-cold notice once and
recording the client-cold telemetry row.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_REPO = Path(__file__).resolve().parents[3]

_PROBE = r"""
import json, sys
sys.path.insert(0, REPO)
import coordinator_core.warm.client as client
from coordinator_core.warm import skew, telemetry

def _unstamped():
    raise skew.UnstampedEngineRootError("stub root carries no engine build stamp")

skew.compute_client_token = _unstamped
rows = []
telemetry.record_client_cold_fallback = lambda **kw: rows.append(kw)
client.is_warm_enabled = lambda: True
msg = {"jsonrpc": "2.0", "id": 1, "method": "memo.list_outbox"}
r1 = client.try_warm_dispatch(msg)
r2 = client.try_warm_dispatch(msg)
deferred = [
    m for m in (
        "coordinator_core.warm.election",
        "coordinator_core.warm.caller_context",
        "coordinator_core.warm.env_forwarding",
    ) if m in sys.modules
]
print(json.dumps({
    "r": [r1, r2],
    "deferred_loaded": deferred,
    "rows": [{k: v for k, v in row.items() if k != "engine_root"} for row in rows],
    "cold_reason": client.last_cold_reason(),
}))
"""


def _run_probe() -> tuple[dict, str]:
    proc = subprocess.run(
        [sys.executable, "-c", f"REPO = {str(_REPO)!r}\n{_PROBE}"],
        capture_output=True,
        text=True,
        cwd=str(_REPO),
        timeout=60,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1]), proc.stderr


def test_decline_leaves_deferred_modules_unloaded() -> None:
    out, _ = _run_probe()
    assert out["r"] == [None, None]
    assert out["deferred_loaded"] == []


def test_decline_prints_notice_once_and_records_cold_row_each_call() -> None:
    out, stderr = _run_probe()
    assert stderr.count("carries no engine build stamp") == 1
    assert len(out["rows"]) == 2
    assert all(r["reason"] == "spawn-triggering-miss" for r in out["rows"])
    assert all(r["op"] == "memo.list_outbox" for r in out["rows"])
    assert "carries no engine build stamp" in out["cold_reason"]


def test_election_stays_resolvable_as_client_attribute() -> None:
    from coordinator_core.warm import client

    assert client.election.pipe_name is not None
