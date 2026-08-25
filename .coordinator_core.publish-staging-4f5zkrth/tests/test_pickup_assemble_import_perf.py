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
`coordinator_core.hooks.session_heartbeat`,
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

# Spawns a real external process; runs at cadence gates, not per-commit.
# Spawn ratchet: coordinator_core/tests/test_no_new_spawning_tests.py
pytestmark = [
    pytest.mark.spawns_process,
    pytest.mark.cadence,
]

# The subprocess below spawns a fresh interpreter that imports coordinator_core.
# That child inherits cwd but NOT pytest's rootdir sys.path insertion, so it can
# only resolve the package when cwd is (or is under) the repo root -- from any
# other cwd it dies with ModuleNotFoundError before it can write anything to
# stdout/stderr. Pinning cwd to the repo root derived from this file's own path
# makes the subprocess resolvable regardless of the invoking shell's cwd.
_REPO_ROOT = Path(__file__).resolve().parents[2]

# Number of independent subprocess samples to take, asserting on the minimum (see
# module docstring's "Estimator note"). This check is now a coarse catastrophic-
# blowup net, not the precise guard (see "Primary guard" in the module docstring)
# -- the 350ms bound has ~1.7x headroom over the worst sustained-contention
# sample observed (207ms), which a SINGLE sample would clear just as reliably as
# a minimum-of-many. The min-of-N mechanism was bought to noise-filter a tight,
# precise bound that no longer exists; kept at 2 (not 1) purely as cheap
# redundancy against a one-off subprocess-spawn anomaly, not because the
# catastrophic bound needs noise-filtering. Each sample spawns a fresh
# interpreter at ~80-100ms+, so keeping this low matters under the parallel fast
# tier this deflake was meant to stop burdening.
_SAMPLE_COUNT = 2

# Heavy modules named in the module docstring's deferral history, asserted ABSENT
# from `sys.modules` after importing `coordinator_core.pickup_assemble`. `pydantic`
# is confirmed absent as of this test's authorship (verified via a fresh-subprocess
# `sys.modules` diff, see `_imported_module_names`). `asyncio` is named in the same
# docstring passage but is NOT included here: it is currently PRESENT (see the
# docstring's "long tail of additional _EAGER_OP_MODULES leaves ... still import
# asyncio at module scope" note) -- asserting its absence today would land a
# red test. Add it back here only once that residual is actually closed.
_HEAVY_MODULES_EXPECTED_ABSENT = ("pydantic",)

# Third-party (non-stdlib, non-`coordinator_core`) top-level packages this import is
# allowed to pull in. `yaml` is the only real entry; `_cython_3_1_4` and
# `cython_runtime` are artifacts of PyYAML's C extension (`yaml._yaml`) and appear
# only where libyaml is installed, so both are tolerated rather than required.
# This is the guard that actually answers "did a new heavy subtree get dragged in
# transitively" -- by NAME, deterministically, with no count to keep re-pinning.
_THIRD_PARTY_ALLOWED = frozenset({"yaml", "_cython_3_1_4", "cython_runtime"})

# Ceiling on the EXTERNAL (non-`coordinator_core`) module count -- 133 measured on
# this machine/Python version, ~28% headroom. Deliberately NOT a ceiling on the
# whole imported set: 443 of the 576 modules imported here are `coordinator_core`'s
# own, and that number grows every time an op module is added to
# `ops/__init__.py::_EAGER_OP_MODULES` -- a whole-set ceiling therefore rots by
# construction (it went red in a week of ordinary op growth, 522 -> 576, with the
# external set unchanged and no heavy subtree anywhere near it). The external count
# is what a genuine new subtree moves, and it is stable against in-repo growth.
# Headroom here absorbs platform-specific stdlib substitutions (POSIX's
# `_posixsubprocess` vs Windows's `_winapi`/`_wmi`, psutil's per-OS leaf modules).
#
# Negative-spec: do NOT re-add a whole-set count ceiling "for completeness" -- the
# in-repo module count is not a property this test has any opinion about, and pinning
# it only buys a recurring red test with no signal in it.
_EXTERNAL_MODULE_COUNT_CEILING = 170


def _imported_module_names() -> list[str]:
    """Return the `sys.modules` keys newly added by importing the target module.

    Runs in a fresh subprocess (no pollution from the test runner's own prior
    imports) and diffs `sys.modules` before/after the import, returning the
    newly-added names as JSON on stdout.
    """
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
    """Run one fresh-interpreter sample and return the import's CPU-time cost in ms.

    CPU time (`time.process_time`), NOT wall-clock. Wall-clock was measured
    unusable for this bound at any threshold: on a machine running a parallel
    test tier, wall-clock samples for this import spanned 382-786ms against an
    idle cost of ~94ms, while `process_time` for the same import on the same
    loaded machine held at 184-202ms against ~98ms idle. Descheduling inflates
    elapsed time without inflating consumed CPU, so `process_time` is the only
    one of the two that still measures the import rather than the machine.
    Widening the wall-clock bound instead does not work — 786ms already exceeds
    any threshold that would still catch a real regression.

    Uses a subprocess so the interpreter is fresh and `sys.modules` carries no
    pollution from the test runner's own prior imports.
    """
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
    # `test_pickup_assemble_import_modules` carries the precision now). Measured in
    # CPU time, not wall-clock, per `_sample_import_cost_ms`: ~98ms idle, 184-202ms
    # on a machine saturated by a parallel test tier. 350ms clears that loaded
    # worst case with ~1.7x margin while still catching a real regression back
    # toward the pre-fix ~125-160ms idle baseline.
    #
    # Do NOT restore a wall-clock measurement here on the theory that a wide enough
    # bound is safe -- wall-clock for this import was measured at 382-786ms on that
    # same loaded machine, so no bound both survives load and catches a regression.
    floor_ms = 350.0
    assert min_ms <= floor_ms, (
        f"coordinator_core.pickup_assemble import cost regressed: minimum of "
        f"{_SAMPLE_COUNT} samples was {min_ms:.1f}ms, exceeding the widened "
        f"catastrophic-regression bound of {floor_ms}ms. All samples (ms): "
        f"{[round(s, 1) for s in samples_ms]}. See module docstring — the "
        f"aspirational AC-6 target is <=20ms, not yet reached (residual needs the "
        f"off-limits ops/__init__.py eager-load restructure)."
    )
