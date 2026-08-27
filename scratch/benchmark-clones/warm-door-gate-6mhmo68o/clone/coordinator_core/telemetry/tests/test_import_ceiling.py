"""
coordinator_core.telemetry.tests.test_import_ceiling — hot-path import-cost
regression ceiling.

Purpose: pins the per-invocation import self-time paid by every engine
invocation below a generous CEILING, not an exact figure — this box's own
ambient jitter spans 75.7-114ms spawn-to-exit (docs/plans/2026-08-08-seven-
measured-levers-load-norm.md § C1), so an exact-figure assert would be
noise-flaky on a machine carrying 50-70 concurrent LLM sessions (docs/wiki/
machine-load-norm.md). The ceiling exists to catch a REGRESSION (e.g. a
future edit re-adding psutil, or something equally heavy, to session.core's
module scope) — not to chase a tight bound.

Mechanism: spawns `python -X importtime -m coordinator_core.invoke ping
'{}'` in a subprocess (never in-process — see
coordinator_core.benchmarks._import_probe's docstring for why an in-process
measurement silently undercounts) and sums every line's SELF-time column
(microseconds), converted to milliseconds. This is the same total the C1
chunk body's "66.6ms across 203 modules" baseline was measured against.

Spec backlink: pln-seven-measured-levers-against-f1ee97 § C1
               (AC1: "add an importtime regression assert — a ceiling, not
               an exact figure").

Negative-spec:
    - Does NOT assert an exact self-time figure, nor a module count — see
      the ceiling-not-exact-figure rationale above.
    - Does NOT run under `-n auto` / any fixture — this measures the real
      `coordinator_core.invoke` entrypoint against the real interpreter.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

from coordinator_core.win_portability import no_console_creationflags

import pytest

# Declared, not excused: this file spawns a real process (git/python) because
# the property under test is that binary's own behaviour, which no fixture
# stands in for. The spawn ratchet's `_BASELINE` is shrink-only pre-existing
# residue and is explicitly not the route for a new file --
# coordinator_core/tests/test_no_new_spawning_tests.py Rule 2.
pytestmark = [
    pytest.mark.cadence,
    pytest.mark.spawns_process,
]

REPO_ROOT = Path(__file__).resolve().parents[3]

_SELF_TIME_RE = re.compile(r"^import time:\s+(\d+)\s+\|\s+\d+\s+\|\s+")

# Generous ceiling (ms): the measured post-fix median on this box sits
# ~64-70ms, with ambient spikes into the 80s under load; the pre-fix
# baseline (psutil resident at module scope) sits ~7-8ms higher plus
# whatever regressed it back. 100ms gives headroom against ambient jitter
# while still catching a psutil-sized (or larger) regression.
_SELF_TIME_CEILING_MS = 100.0


def _measure_import_self_time_ms() -> float:
    proc = subprocess.run(
        [sys.executable, "-X", "importtime", "-m", "coordinator_core.invoke", "ping", "{}"],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        timeout=30,
        **no_console_creationflags(),
    )
    total_us = 0
    for line in proc.stderr.splitlines():
        m = _SELF_TIME_RE.match(line)
        if m:
            total_us += int(m.group(1))
    return total_us / 1000.0


def test_import_self_time_stays_below_ceiling():
    self_time_ms = _measure_import_self_time_ms()
    assert self_time_ms < _SELF_TIME_CEILING_MS, (
        f"per-invocation import self-time regressed to {self_time_ms:.2f}ms "
        f"(ceiling {_SELF_TIME_CEILING_MS}ms) — see docs/plans/2026-08-08-"
        "seven-measured-levers-load-norm.md § C1"
    )


def test_psutil_not_imported_by_bare_ping():
    """psutil (6.31ms self on this box) has no reason to be resident for a
    bare `ping` — coordinator_core.session.core defers it behind
    `_psutil()`, called only from liveness paths ping never reaches."""
    proc = subprocess.run(
        [sys.executable, "-c", "import coordinator_core.invoke.__main__; import sys; print('psutil' in sys.modules)"],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        timeout=30,
        **no_console_creationflags(),
    )
    assert proc.stdout.strip() == "False", proc.stdout + proc.stderr
