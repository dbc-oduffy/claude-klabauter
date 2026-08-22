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

THE THRESHOLD, AND WHY -- PER PLATFORM (C10). Import cost is one of the
places macOS and Windows diverge most (no conhost, different filesystem,
different interpreter start), so this gate does not assume the Windows
`200ms` bar transfers -- it measures Darwin separately via
`batched_process_time_quantiles` (n=15 batches of k=20, reporting p50/p90,
per that primitive's own AC8-derived default methodology) and pins its own
ceiling off what was actually observed on the fleet floor, not a copied
number.

Measured on this box (2026-08-22, Darwin, k=20, n=15):
    p50=28.838ms, p90=30.608ms
    samples_ms=[28.895, 28.151, 28.558, 31.635, 28.838, 28.228, 28.705,
                30.598, 28.445, 28.41, 29.397, 30.023, 28.825, 29.067, 30.608]

`DARWIN_PROCESS_TIME_CEILING_MS = 100` gives ~3.3x headroom above the
observed p90 (30.608ms) -- wide enough to absorb this repo's normal
run-to-run jitter -- while sitting far below the regression this gate
exists to catch: reintroducing the `pickup_assemble` import alone costs
+443.8ms on its own account, clearing 100ms outright just as it clears the
existing Windows `PROCESS_TIME_CEILING_MS = 200` bar.
`assert_never_imports_pickup_assemble` below is the second, structural leg
(not by inspection -- this gate is a real subprocess import, `sys.modules`
checked in that fresh interpreter) that catches the exact regression class
C1's brief names, independent of whatever the measured number happens to
be on a given box, on either platform.

Spec backlink: state/dispatch-briefs/2026-08-21-engine-half-of-the-roadmap-
sprint-spine-split/C1.md
Spec backlink (per-platform ceiling): state/dispatch-briefs/2026-08-22-the-
brightlines-instrument-exists-on-the-fleet-floor/C10.md
"""
from __future__ import annotations

import subprocess
import sys

import pytest

from coordinator_core.benchmarks.process_time import (
    IS_DARWIN,
    IS_WINDOWS,
    batched_process_time_ms,
    batched_process_time_quantiles,
)

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

PROCESS_TIME_CEILING_MS = 200
DARWIN_PROCESS_TIME_CEILING_MS = 100
K_INVOCATIONS = 20
N_BATCHES = 15

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
        pytest.skip(
            "Windows job-object accounting path -- see "
            "test_residue_import_process_time_is_under_the_ceiling_darwin "
            "for the macOS leg of this gate"
        )

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


def test_residue_import_process_time_is_under_the_ceiling_darwin():
    """macOS leg of the primary assertion (C10): measures n=N_BATCHES
    batches of k=K_INVOCATIONS via `batched_process_time_quantiles` (this
    primitive's own AC8-derived default methodology -- a single batch's
    mean is not trusted for a gate this close to real regressions), and
    gates on p90 against DARWIN_PROCESS_TIME_CEILING_MS -- a ceiling
    measured and pinned for this platform, not copied from Windows. See
    this file's module docstring for the measured n/p50/p90 this ceiling
    was derived from."""
    if not IS_DARWIN:
        pytest.skip(
            "process-time kqueue/EVFILT_PROC accounting is a Darwin-only "
            "primitive on this module"
        )

    result = batched_process_time_quantiles(_IMPORT_ARGV, k=K_INVOCATIONS, n=N_BATCHES)
    assert result["p90_ms"] <= DARWIN_PROCESS_TIME_CEILING_MS, (
        f"residue.py import process time regressed on Darwin: "
        f"p90={result['p90_ms']}ms exceeds the "
        f"{DARWIN_PROCESS_TIME_CEILING_MS}ms ceiling "
        f"(p50={result['p50_ms']}ms, n={result['n']}, k={result['k']}, "
        f"samples_ms={result['samples']}). See this file's module docstring "
        "for the ceiling's derivation."
    )
