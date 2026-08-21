"""
coordinator_core.review_assemble.tests.test_residue_import_budget -- process-time
regression gate for `coordinator_core.review_assemble.residue`'s import cost.

WHY THIS FILE EXISTS. `residue.py` used to import `resolve_repo_root` from
`coordinator_core.pickup_assemble` -- a 12,070-line module pulled in for one
function, measured at 443.8ms to import, against 40-58ms for this module's
other engine imports. C1 re-homed an equivalent `resolve_repo_root` locally
so `residue.py` sits at the interpreter floor. This gate asserts PROCESS
TIME (never wall clock -- see `coordinator_core.benchmarks.process_time`
module docstring for the three measurement traps that avoids), batched to
beat the ~15.6ms scheduler-tick quantisation a single sample near a sub-
200ms bar cannot resolve, matching this repo's own
`test_warm_door_process_time_gate.py` precedent for wrapping
`batched_process_time_ms` into a standing ceiling gate.

THE THRESHOLD, AND WHY. `PROCESS_TIME_CEILING_MS = 200` gives wide headroom
above a bare-interpreter-plus-this-module's-own-imports floor (this repo's
own convention, measured well under 100ms for a lean import closure) while
sitting far below the regression this gate exists to catch: reintroducing
the `pickup_assemble` import alone costs +443.8ms on its own account,
clearing 200ms outright. `assert_never_imports_pickup_assemble` below is
the second, structural leg (not by inspection -- this gate is a real
subprocess import, `sys.modules` checked in that fresh interpreter) that
catches the exact regression class C1's brief names, independent of
whatever the measured number happens to be on a given box.

Spec backlink: state/dispatch-briefs/2026-08-21-engine-half-of-the-roadmap-
sprint-spine-split/C1.md
"""
from __future__ import annotations

import subprocess
import sys

import pytest

from coordinator_core.benchmarks.process_time import IS_WINDOWS, batched_process_time_ms

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

PROCESS_TIME_CEILING_MS = 200
K_INVOCATIONS = 20

_IMPORT_ARGV = [
    sys.executable,
    "-c",
    "import coordinator_core.review_assemble.residue",
]

_MODULES_CHECK_ARGV = [
    sys.executable,
    "-c",
    (
        "import sys\n"
        "import coordinator_core.review_assemble.residue\n"
        "assert 'coordinator_core.pickup_assemble' not in sys.modules, "
        "'residue.py must not pull in coordinator_core.pickup_assemble'\n"
    ),
]


def test_residue_module_does_not_import_pickup_assemble():
    """Structural leg: importing `residue.py` in a fresh interpreter must
    never bring `coordinator_core.pickup_assemble` into `sys.modules` --
    the exact regression this gate exists to catch, checked directly rather
    than inferred from timing alone."""
    result = subprocess.run(
        _MODULES_CHECK_ARGV,
        capture_output=True,
        text=True,
        timeout=30,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    assert result.returncode == 0, (
        f"residue.py import pulled in pickup_assemble (or otherwise failed): "
        f"rc={result.returncode}, stderr={result.stderr!r}"
    )


def test_residue_import_process_time_is_under_the_ceiling():
    """Primary assertion: the process-time cost of importing
    `coordinator_core.review_assemble.residue` alone, batched over
    K_INVOCATIONS runs, must not exceed PROCESS_TIME_CEILING_MS. See this
    file's module docstring for the ceiling's derivation."""
    if not IS_WINDOWS:
        pytest.skip("process-time job-object accounting is a Windows-only primitive")

    result = batched_process_time_ms(_IMPORT_ARGV, k=K_INVOCATIONS)
    assert result["rc"] == 0, (
        f"residue.py import invocation exited rc={result['rc']} -- a failing "
        "invocation cannot stand in for a valid process-time sample"
    )
    assert result["process_time_ms"] <= PROCESS_TIME_CEILING_MS, (
        f"residue.py import process time regressed: {result['process_time_ms']}ms "
        f"exceeds the {PROCESS_TIME_CEILING_MS}ms ceiling (k={result['k']}, "
        f"wall_ms={result['wall_ms']} for context only). See this file's module "
        "docstring for the ceiling's derivation."
    )
