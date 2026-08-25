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

Spec backlink: pln-windows-hot-path-cost-less-wor-0ec8ea chunk C4
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
    """One fresh-interpreter measurement of an entrypoint's import cost.

    `own_module_count` is the `coordinator_core.*` share of `module_count`
    (`module_count - own_module_count` is therefore the stdlib/third-party share).
    `None` only when parsing a two-field line from an older probe.
    """

    entrypoint: str
    module_count: int
    elapsed_ms: float
    own_module_count: Optional[int] = None


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
    fields = result.stdout.strip().split()
    module_count_str, elapsed_ms_str = fields[0], fields[1]
    own_module_count = int(fields[2]) if len(fields) > 2 else None
    return ImportCost(
        entrypoint=entrypoint,
        module_count=int(module_count_str),
        elapsed_ms=float(elapsed_ms_str),
        own_module_count=own_module_count,
    )


def resolve_baseline_python(entrypoint: str, manifest: Optional[dict] = None) -> Optional[str]:
    """Resolve the interpreter version an entrypoint's baseline was measured under.

    Returns `None` for an entry that predates the field (2026-08-17). Missing is a
    diagnostic gap, not a gate failure -- `resolve_ceiling` stays the sole gating
    resolver, so a `None` here can never turn a green ceiling red.
    """
    if manifest is None:
        manifest = load_manifest()
    entry = manifest.get("entrypoints", {}).get(entrypoint)
    if entry is None:
        raise KeyError(f"no import-budget entry for entrypoint {entrypoint!r}")
    return entry.get("measured_under_python")


def running_python_version() -> str:
    """This interpreter's `<major>.<minor>.<micro>`, the shape recorded in the manifest."""
    return ".".join(str(part) for part in sys.version_info[:3])


def interpreter_drift_note(entrypoint: str, manifest: Optional[dict] = None) -> str:
    """Render the interpreter-drift clause for a ceiling-breach message, or ''.

    Why this exists: the breach message used to assert "a failure here means a real
    import-cost regrowth, not a tight-pin false positive". On 2026-08-17 that was
    false and cost an investigation -- `coordinator_core.ipc` breached 56 with no
    code change, because Python 3.14 routed `bz2`/`lzma` onto the new `compression`
    package that `shutil` -> `tempfile` drags in, growing the stdlib graph under a
    baseline frozen on an earlier interpreter. A module-count ceiling is
    interpreter-version-sensitive by construction; the gate stays a hard block
    (a stdlib module costs the same Windows per-file AV scan as one of ours), but it
    must not name only one of the two possible causes.
    """
    baseline_python = resolve_baseline_python(entrypoint, manifest=manifest)
    running = running_python_version()
    if baseline_python is None:
        return (
            f" This entry records no `measured_under_python`, so interpreter drift cannot be "
            f"ruled out from disk; you are on {running}."
        )
    if baseline_python == running:
        return (
            f" Baseline and this run are both on Python {running}, so interpreter stdlib "
            f"drift is ruled out -- this is real regrowth."
        )
    return (
        f" Baseline was measured under Python {baseline_python}; you are on {running}. "
        f"A stdlib refactor between those versions can breach a frozen ceiling with no "
        f"code change (precedent: 3.14 moved bz2/lzma onto `compression`, reached via "
        f"shutil <- tempfile). Compare `own_module_count` against the baseline's before "
        f"concluding regrowth: if the `coordinator_core.*` share is flat, the growth is "
        f"the interpreter's, and the fix is deferring the stdlib import that reaches it, "
        f"not widening the ceiling."
    )
