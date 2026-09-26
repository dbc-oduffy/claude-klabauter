"""Micro-benchmark: the read-only /pickup brief-path import cost.

Guards against import-time regressions on `coordinator_core.pickup_assemble` — a
read-only, stdlib-clean module whose own imports should never pull the heavy
pydantic/asyncio tree transitively via `coordinator_core.ops.*`'s eager-load loop
(`ops/__init__.py::_EAGER_OP_MODULES`).

Budget: docs/plans/2026-07-24-canonical-resolution-engine.md task W0-1 / AC-6 set an
≤20ms target. As of this test's authorship, leaf-level import deferral (moving heavy
imports from module scope into the functions that use them, in
`coordinator_core.ipc`, `coordinator_core.ops.emit.sections.handoffs`,
`coordinator_core.ops.goal_append`, `coordinator_core.ops.fleet._common`,
`coordinator_core.ops.coverage_gate`, `coordinator_core.ops.handoff_children`,
`coordinator_core.ops.emit.sections.cross_repo_memos`,
`coordinator_core.ops.ceremony.consumed_handoff_stamp`,
`coordinator_core.hooks.suggest_sonnet_research`,
`coordinator_core.hooks.nudge_em_code_dispatch`,
`coordinator_core.hooks.agent_completion_log`,
`coordinator_core.hooks.nudge_unauthorized_handoff`,
`coordinator_core.hooks.postuse_advisory_dispatch`,
`coordinator_core.hooks.track_dispatched_agents`,
`coordinator_core.hooks.track_touched_files`,
`coordinator_core.ops.fleet.archive_plans`, `coordinator_core.ops.fleet.archive_handoffs`)
brought the cumulative import cost from ~125-160ms down to a floor of ~80ms, NOT the
full ≤20ms target. The residual is the `ops/__init__.py::_EAGER_OP_MODULES` eager-import
loop itself (out of scope for this fix — a concurrent session owns uncommitted changes
in `ops/__init__.py` and `op_scopes.py`) plus a long tail of additional
`_EAGER_OP_MODULES` leaves (35+ more files) that still import `asyncio` at module scope;
once ANY eager-loaded module imports `asyncio`, the ~5-8ms cost is paid once regardless
of how many other eager leaves also import it — so closing the residual gap requires
either making the eager-load loop itself lazy, or exhaustively deferring `asyncio` in
every remaining eager leaf (diminishing returns without the loop-level fix).

This test asserts the ACHIEVED floor (generous margin below the current ~80ms
measurement), not the aspirational ≤20ms AC-6 target — closing the rest needs the
off-limits `ops/__init__.py` restructure.

Estimator note (SUPERSEDED — kept for history, see "Primary guard" below): an
earlier version of this test sampled the subprocess import cost N times and
asserted on the MINIMUM, reasoning that scheduler contention (parallel test workers
stealing CPU from the child interpreter) can only ever make a measured duration
LONGER than the true cost, never shorter — so the minimum across repeated samples
should be the best available estimate of actual import cost, robust to load. That
reasoning holds under transient contention but was measured to FAIL under sustained
load: with `pytest -n 8` running concurrently for the full duration of all N
samples, every sample inflates together (idle wall-clock ~94ms; under sustained
`-n 8` load, min-of-7 wall-clock measured 177-207ms, min 177ms — well past a 120ms
floor). CPU-time (`time.process_time`) fared a little better (idle ~98ms, loaded
~137ms, still +40%) but the floor's margin over the true idle cost (~1.22x) left no
room to absorb that drift either. A ratio against a stdlib control import held up
best (8.81 idle vs 9.96 loaded, ~13% drift) but is still a proxy with residual
flake risk, not a deterministic guard. Do not resurrect a tight wall-clock (or
CPU-time, or control-ratio) bound as the PRIMARY assertion — all three were tried
and measured insufficient under sustained parallel-worker contention; see "Primary
guard" below for what replaced it.

Primary guard: the elapsed-time number was always a PROXY for the real property
this test cares about — that `coordinator_core.pickup_assemble` never pulls the
heavy pydantic/asyncio tree transitively via `ops/__init__.py::_EAGER_OP_MODULES`.
A proxy that cannot be measured reliably on a loaded machine is the wrong primary
guard. The primary assertion is now on the imported module SET (deterministic,
sampled once, no timing involved): named-absence checks for heavy modules
confirmed absent today, a third-party-package allowlist, plus a count ceiling on
the EXTERNAL (non-`coordinator_core`) slice of that set to catch an unnamed new
subtree. The ceiling deliberately excludes `coordinator_core`'s own modules --
that count tracks `ops/__init__.py::_EAGER_OP_MODULES` growth, not import
cleanliness, and a whole-set ceiling went red on ordinary op growth alone
(522 -> 576 in a week) with the external slice unmoved. See
`_EXTERNAL_MODULE_COUNT_CEILING`. The timing check is retained only as a widened
catastrophic-regression sanity bound (see `test_pickup_assemble_import_floor`)
now that the module-set check carries the precision for *new-subtree*
regressions specifically — NOT for every import-cost regression. Reviewed and
accepted dead zone: an already-eager-loaded module's top-level work getting
heavier (more work at import time, a bigger literal built at module scope, a
slower C-extension init) adds no new module to the set and can push wall-clock
from the ~94ms idle baseline up to just under the 350ms catastrophic bound
without either guard tripping. Neither guard covers a same-module-set cost
regression; do not read the module-count ceiling as standing in for that case.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = [
    pytest.mark.spawns_process,
    pytest.mark.cadence,
]

_REPO_ROOT = Path(__file__).resolve().parents[2]

_SAMPLE_COUNT = 2

# docstring's "long tail of additional _EAGER_OP_MODULES leaves ... still import
_HEAVY_MODULES_EXPECTED_ABSENT = ("pydantic",)

_THIRD_PARTY_ALLOWED = frozenset({"yaml", "_cython_3_1_4", "cython_runtime"})

# Ceiling on the EXTERNAL (non-`coordinator_core`) module count -- 133 measured on
# `ops/__init__.py::_EAGER_OP_MODULES` -- a whole-set ceiling therefore rots by
_EXTERNAL_MODULE_COUNT_CEILING = 170


def _imported_module_names() -> list[str]:
    probe = (
        "import sys, json\n"
        "before = set(sys.modules)\n"
        "import coordinator_core.pickup_assemble\n"
        "after = set(sys.modules)\n"
        "print(json.dumps(sorted(after - before)))\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        check=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        cwd=_REPO_ROOT,
    )
    return json.loads(proc.stdout)


def _sample_import_cost_ms() -> float:
    probe = (
        "import time\n"
        "t0 = time.process_time()\n"
        "import coordinator_core.pickup_assemble\n"
        "print((time.process_time() - t0) * 1000.0)\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        check=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        cwd=_REPO_ROOT,
    )
    return float(proc.stdout.strip())


def test_pickup_assemble_import_modules() -> None:
    """PRIMARY guard: the imported module SET stays stdlib-clean, deterministically.

    See module docstring's "Primary guard" section. Three assertions, none
    timing-sensitive and all immune to scheduler contention:

    1. Named heavy modules confirmed absent today (`_HEAVY_MODULES_EXPECTED_ABSENT`)
       stay absent.
    2. No third-party top-level package outside `_THIRD_PARTY_ALLOWED` appears --
       the by-name form of "a new heavy subtree got dragged in transitively".
    3. The EXTERNAL (non-`coordinator_core`) module count stays under
       `_EXTERNAL_MODULE_COUNT_CEILING`, catching a new stdlib subtree that (2)
       would not name.
    """
    imported = _imported_module_names()
    imported_set = set(imported)

    for heavy in _HEAVY_MODULES_EXPECTED_ABSENT:
        present = any(mod == heavy or mod.startswith(heavy + ".") for mod in imported_set)
        assert not present, (
            f"coordinator_core.pickup_assemble now transitively imports {heavy!r} "
            f"(or a submodule of it), which the module docstring's deferral history "
            f"names as part of the heavy tree this module must stay clear of."
        )

    top_level = {mod.split(".")[0] for mod in imported_set}
    third_party = {
        name
        for name in top_level
        if name != "coordinator_core"
        and name not in sys.stdlib_module_names
        and name not in _THIRD_PARTY_ALLOWED
    }
    assert not third_party, (
        f"coordinator_core.pickup_assemble now transitively imports third-party "
        f"package(s) {sorted(third_party)} -- a new heavy subtree. Allowed today: "
        f"{sorted(_THIRD_PARTY_ALLOWED)}."
    )

    external = [
        mod for mod in imported
        if mod != "coordinator_core" and not mod.startswith("coordinator_core.")
    ]
    assert len(external) <= _EXTERNAL_MODULE_COUNT_CEILING, (
        f"coordinator_core.pickup_assemble pulled in {len(external)} non-"
        f"coordinator_core modules, exceeding the ceiling of "
        f"{_EXTERNAL_MODULE_COUNT_CEILING} -- likely a new subtree dragged in "
        f"transitively. Newly-imported external modules: {external}"
    )


def test_pickup_assemble_import_floor() -> None:
    """`import coordinator_core.pickup_assemble` completes within a widened sanity bound.

    This is now a SECONDARY, catastrophic-regression-only check -- the PRECISE guard
    is `test_pickup_assemble_import_modules` (see module docstring's "Primary
    guard"). Takes `_SAMPLE_COUNT` independent CPU-time samples and asserts on the
    MINIMUM (see module docstring's superseded "Estimator note" for why min-of-N
    WALL-CLOCK was tried and measured insufficient as the PRIMARY guard; and see
    `_sample_import_cost_ms` for why even a widened wall-clock bound had to be
    abandoned here in favour of CPU time).
    """
    samples_ms = [_sample_import_cost_ms() for _ in range(_SAMPLE_COUNT)]
    min_ms = min(samples_ms)

    # Widened CATASTROPHIC-blowup bound, not a precise floor (see docstring above --
    floor_ms = 350.0
    assert min_ms <= floor_ms, (
        f"coordinator_core.pickup_assemble import cost regressed: minimum of "
        f"{_SAMPLE_COUNT} samples was {min_ms:.1f}ms, exceeding the widened "
        f"catastrophic-regression bound of {floor_ms}ms. All samples (ms): "
        f"{[round(s, 1) for s in samples_ms]}. See module docstring — the "
        f"aspirational AC-6 target is <=20ms, not yet reached (residual needs the "
        f"off-limits ops/__init__.py eager-load restructure)."
    )
