"""Standing gate: the workday-start day envelope's cold first call under the 500ms bar.

The run pays one call per interpreter, with the run stack and the module already imported
(`runner._dispatch_inproc` starts its clock after the import). So each sample is a fresh child
interpreter that imports the run stack and `day_envelope` untimed, then times the first call with
`time.process_time` against the live working tree. Process time and spawn count only, never wall
clock; the child spawn is the harness, not the op. A reader error fails the gate so a failed reader
can never pass as a fast one.
"""
from __future__ import annotations

import json
import statistics
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

from coordinator_core.win_portability import no_console_creationflags

SAMPLES = 5
BUDGET_MS = 500.0
REPO_ROOT = Path(__file__).resolve().parents[3]

_CHILD = r"""
import json, os, subprocess, sys, time
from pathlib import Path

import coordinator_core.ceremony_common.run.runner  # noqa: F401  run stack, untimed like the step
from coordinator_core.workday_start.day_envelope import day_envelope

spawns = []

def _no_spawn(*a, **k):
    spawns.append(1)
    raise AssertionError("day_envelope spawned a process")

subprocess.Popen = _no_spawn
os.system = _no_spawn

root = Path(sys.argv[1])
t0 = time.process_time()
code, text = day_envelope(root)
ms = (time.process_time() - t0) * 1000.0
envelope = json.loads(text)
print(json.dumps({
    "ms": ms,
    "code": code,
    "spawns": len(spawns),
    "reader_errors": envelope.get("reader_errors"),
    "keys": sorted(envelope),
}))
"""


def _sample() -> dict:
    done = subprocess.run(
        [sys.executable, "-c", _CHILD, str(REPO_ROOT)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
        **no_console_creationflags(),
    )
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout.strip().splitlines()[-1])


@pytest.mark.spawns_process
def test_day_envelope_cold_first_call_process_budget():
    results = [_sample() for _ in range(SAMPLES)]
    samples = [r["ms"] for r in results]
    median = statistics.median(samples)
    spawns = sum(r["spawns"] for r in results)
    print(
        f"budget: workday-start day-envelope cold first-call median {median:.1f}ms of "
        f"{BUDGET_MS:.0f}ms (samples {[round(s, 1) for s in samples]}), spawns {spawns}"
    )
    for r in results:
        assert r["code"] == 0
        assert not r["reader_errors"], r["reader_errors"]
    assert spawns == 0
    assert median < BUDGET_MS
