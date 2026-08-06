"""
coordinator_core.ops.capture_fan_out_threshold — idempotent capture of the
cores-scaled fan-out large-wave reminder threshold into the machine-local
registry.

Purpose: single source of the setup-time threshold capture so the guard is
behaviorally testable (not an untestable markdown snippet). Called by
coordinator:install Step 8; the value is read back at fan-out dispatch time
(env LARGE_WAVE_THRESHOLD -> machine-local get fan_out.large_wave_threshold
-> 16).

Value = 3 x logical CPU count (floored at 1) -- a speed-taper advisory
threshold, NOT a cap: a CPU time-slices far more than (cores) tasks, so this
marks where parallel returns may start tapering, not a ceiling. The
genuinely load-bearing resource is memory commit (RAM/VRAM); the core-count
proxy this module writes is complemented at dispatch time by a live
RAM/VRAM headroom probe (fires a "headroom tight" NOTE on a loaded machine
regardless of wave size). The idempotency guard uses `machine-local keys`
(registry layers ONLY) -- NOT `machine-local has`, which consults the env
layer and would skip the persistent write if the operator already has
MACHINE_LOCAL_FAN_OUT_LARGE_WAVE_THRESHOLD exported (the clean-install hole
The Staff Engineer F1/F7 flagged). Never clobbers an operator's manual registry value.

Port source: coordinator/bin/capture-fan-out-threshold.sh (example-doctrine-repo)
Spec backlink: docs/plans/2026-05-30-organic-ramp-concurrency-doctrine.md §C6

Negative-spec (deliberate divergence from the bash oracle):
    - The bash script resolved a Python interpreter (via
      coordinator_core.pyresolve --print-bin, degrading to a bare python3/
      python PATH probe) purely to shell out and compute `os.cpu_count()` in
      a subprocess. This module already runs IN a Python process (the
      polyglot trampoline that imports it), so it calls `os.cpu_count()`
      in-process directly -- no interpreter-resolution dance, no subprocess.
      This is not a behavior change (the computed VALUE is identical); it
      only drops a redundant subprocess hop that only existed because the
      bash caller had no Python of its own.
    - `machine-local` itself remains an external CLI (still bash-resident,
      not yet ported) -- this module shells out to it via subprocess exactly
      as the bash script did, preserving the registry-only idempotency
      contract (`machine-local keys`, never `machine-local has`).
"""

from __future__ import annotations

import os
import subprocess
import sys
from typing import List, Optional, Tuple

_KEY = "fan_out.large_wave_threshold"


def _compute_value() -> int:
    cores = max(1, os.cpu_count() or 1)
    return 3 * cores


def _ml_argv() -> List[str]:
    """Concrete argv for the machine-local CLI.

    A bare "machine-local" is unrunnable on Windows: the delivered wrapper is
    extension-less (CreateProcess -> WinError 193) and the `.cmd` beside it is
    invisible to CreateProcess, which does not consult PATHEXT (-> WinError 2).
    The canonical resolver prefers [sys.executable, _machine_local.py], avoiding
    shebang exec entirely. Falls back to the bare name so POSIX behavior — and
    the documented degrade-to-empty path below — is unchanged.
    """
    from coordinator_core.install._shared import resolve_machine_local_cli

    return resolve_machine_local_cli(os.environ.get("CLAUDE_PLUGIN_ROOT", "")) or [
        "machine-local"
    ]


def _machine_local_keys() -> Tuple[str, int]:
    """Run `machine-local keys`, degrading to empty output on any failure.

    Mirrors the bash script's `machine-local keys 2>/dev/null || true` --
    a missing/erroring `machine-local` (OSS user partway through setup, or a
    CI runner without it on PATH) degrades to "key absent -> write" instead
    of aborting.
    """
    try:
        proc = subprocess.run(
            [*_ml_argv(), "keys"],
            capture_output=True,
            encoding="utf-8",
            errors="replace",
        )
    except (OSError, FileNotFoundError):
        print(f"skip: _machine_local_keys: proc = subprocess.run( failed: {sys.exc_info()[1]}", file=sys.stderr)
        return ("", 0)
    return (proc.stdout or "", proc.returncode)


def capture(check_only: bool = False) -> Tuple[str, int]:
    """Perform (or preview) the idempotent threshold capture.

    Returns (stdout_text, rc):
      rc 0 on all normal paths (pre-existing / would-write / written).
    """
    keys_out, _rc = _machine_local_keys()
    existing_keys = keys_out.splitlines()
    if _KEY in existing_keys:
        return ("fan_out_threshold: pre-existing\n", 0)

    value = _compute_value()

    if check_only:
        return (f"fan_out_threshold: would write ({value})\n", 0)

    proc = subprocess.run(
        [*_ml_argv(), "set", _KEY, str(value)],
        capture_output=True,
        encoding="utf-8",
        errors="replace",
    )
    if proc.returncode != 0:
        sys.stderr.write(proc.stderr or "")
        return ("", proc.returncode)

    return (f"fan_out_threshold: written ({value})\n", 0)


def main(argv: List[str]) -> int:
    """CLI entry — `[--check-only]`, matching the bash script's usage."""
    check_only = False
    if argv:
        if argv[0] == "--check-only":
            check_only = True
        else:
            sys.stderr.write("usage: capture-fan-out-threshold.sh [--check-only]\n")
            return 2

    text, rc = capture(check_only=check_only)
    if text:
        sys.stdout.write(text)
    return rc


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
