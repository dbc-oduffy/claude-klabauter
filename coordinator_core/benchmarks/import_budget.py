"""
coordinator_core.benchmarks.import_budget -- per-entrypoint import cost + module-count ceiling.

Purpose: "we keep chasing this" is the actual complaint this module answers. Nothing on disk
recorded what a hot-path entrypoint costs to import, so every regrowth investigation re-derived
it from scratch. This module records, per hot-path entrypoint, the module-count delta AND the
wall-clock import time, and gates on the module count -- see "Why module count, not wall-clock"
below.

MINIMUM ENTRYPOINT SET (per chunk C4): `coordinator_core.hooks` (package init),
`coordinator_core.write_guards.engine`, and `coordinator_core.ipc`.

Why module count, not wall-clock:
`docs/wiki/latency-benchmark-harness.md` establishes the EXISTING convention for this benchmark
package: real-subprocess wall-clock timing against `target_ms` is the gating arbiter
(`gate.py`/`budget.py`), and that doc explicitly warns spawn-count reduction does not necessarily
move wall-clock latency -- spawn/module count is named there as a REPORTED proxy metric, not a
gate. This module DELIBERATELY DEVIATES from that convention: it gates on `len(sys.modules)`
delta, not on `target_ms`. Wall-clock is machine- and load-dependent and flakes under parallel
test load (the exact failure mode `latency-benchmark-harness.md` warns about); `len(sys.modules)`
delta is deterministic for a fixed dependency graph. It is also the quantity Windows Defender's
per-file scan cost scales with (per-module-file synchronous AV scan on import, the concrete
regrowth cost this plan exists to control) -- the quantity we can actually measure from a macOS
box that structurally proxies for a cost we cannot measure directly. Wall-clock is still
RECORDED here (`baseline_elapsed_ms`, and every `ImportCost.elapsed_ms` sample) as a reported
value for trend-watching, never as an assertion.

Schema-extension vs. sibling mechanism (the engineering call this plan leaves to the executor):
this is a SIBLING manifest (`import-budget-manifest.json`), not a new field on
`budget-manifest.json`. `budget-manifest.json`'s schema
(`{schema_version, min_gating_sample_count, defaults, overrides}`) is keyed by OP NAME against
`OP_CLASSIFICATION`/`OpClass` tiers (see `budget.py`) and asserts a single `{target_ms,
tolerance}` wall-clock SLA per REGISTERED OP. The entrypoints this module measures
(`coordinator_core.hooks`, `coordinator_core.write_guards.engine`, `coordinator_core.ipc`) are
bare importable MODULE PATHS, not registered ops, and the thing asserted is a module-count
ceiling with headroom, not a wall-clock SLA -- a different key space (module path vs. op name)
and a different resolved shape (`module_count_ceiling` vs. `{target_ms, tolerance}`) with no
OpClass tier concept applicable (an entrypoint import is not classified COMPUTE_ONLY/MUTATING).
Overloading `budget.resolve_budget`'s op/OpClass-keyed resolution to also serve module-count
ceilings would blur two independently-evolving conventions (op latency SLA vs. import-cost
regrowth tripwire) behind one ambiguous schema. A sibling file with its own small resolver keeps
both conventions legible and independently versionable.

Spec backlink: docs/plans/2026-08-06-windows-hot-path-less-work-per-interpreter.md chunk C4
(AC9, AC11).
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import NamedTuple, Optional

from coordinator_core.benchmarks.timer import SUBPROCESS_CREATIONFLAGS

_MANIFEST_PATH = Path(__file__).parent / "import-budget-manifest.json"
_PROBE_PATH = Path(__file__).parent / "_import_probe.py"
_REPO_ROOT = Path(__file__).parents[2]

_PROBE_TIMEOUT_S = 30


class ImportCost(NamedTuple):
    """One fresh-interpreter measurement of an entrypoint's import cost."""

    entrypoint: str
    module_count: int
    elapsed_ms: float


def load_manifest(manifest_path: Path = _MANIFEST_PATH) -> dict:
    """Load and parse the import-budget manifest JSON document from disk."""
    import json

    with open(manifest_path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def resolve_ceiling(entrypoint: str, manifest: Optional[dict] = None) -> int:
    """Resolve the module-count ceiling for `entrypoint`.

    Raises `KeyError` if no manifest entry exists for the entrypoint -- fail loud rather than
    silently skipping the assertion.
    """
    if manifest is None:
        manifest = load_manifest()
    entrypoints = manifest.get("entrypoints", {})
    if entrypoint not in entrypoints:
        raise KeyError(f"no import-budget entry for entrypoint {entrypoint!r}")
    entry = entrypoints[entrypoint]
    if "module_count_ceiling" not in entry:
        raise ValueError(
            f"import-budget entry for entrypoint {entrypoint!r} is missing 'module_count_ceiling': {entry!r}"
        )
    return entry["module_count_ceiling"]


def measure_import_subprocess(entrypoint: str, python: Optional[str] = None) -> ImportCost:
    """Measure `entrypoint`'s import cost in a FRESH subprocess interpreter.

    Isolation is load-bearing -- see `_import_probe.py`'s module docstring for why an in-process
    measurement silently undercounts. Runs `_import_probe.py` as a script child process with the
    repo root prepended to `PYTHONPATH` (a plain script invocation puts the script's own
    directory on `sys.path[0]`, not the repo root, so the target package would otherwise fail to
    resolve) and parses its one-line `<module_count> <elapsed_ms>` stdout.
    """
    python = python or sys.executable
    env = dict(os.environ)
    # Purpose: this probe measures the EAGER import path -- the path the manifest's
    # ceilings were baselined against. `COORDINATOR_CORE_LAZY_OPS` is a real operator
    # override (coordinator_core.hooks._lazy_ops_requested) that flips
    # coordinator_core.hooks from 111 eager modules to 5 lazy ones; left ambient, a
    # developer or CI runner with it set in their shell would get a probe reporting 5
    # against a ceiling of 125, silently no longer gating the regrowth this module
    # exists to catch. Stripped rather than pinned to a specific non-"1" value so no
    # future accepted value for the var can quietly re-open the same hole.
    env.pop("COORDINATOR_CORE_LAZY_OPS", None)
    existing_pythonpath = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = (
        f"{_REPO_ROOT}{os.pathsep}{existing_pythonpath}" if existing_pythonpath else str(_REPO_ROOT)
    )
    result = subprocess.run(
        [python, str(_PROBE_PATH), entrypoint],
        capture_output=True,
        text=True,
        timeout=_PROBE_TIMEOUT_S,
        check=False,
        creationflags=SUBPROCESS_CREATIONFLAGS,
        env=env,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"import probe for entrypoint {entrypoint!r} exited {result.returncode}: {result.stderr}"
        )
    module_count_str, elapsed_ms_str = result.stdout.strip().split()
    return ImportCost(
        entrypoint=entrypoint,
        module_count=int(module_count_str),
        elapsed_ms=float(elapsed_ms_str),
    )
