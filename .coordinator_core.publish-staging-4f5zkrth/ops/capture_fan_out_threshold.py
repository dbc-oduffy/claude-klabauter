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
Patrik F1/F7 flagged). Never clobbers an operator's manual registry value.

Port source: coordinator/bin/capture-fan-out-threshold.sh (DoE-claude)
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
    - The idempotency check (`machine-local keys` in the bash oracle) now
      reads `coordinator_core.machine_resolver.merged_flat_registry`
      in-process (2026-08-16) -- zero-spawn, same registry-layers-ONLY
      semantics (never the env layer `machine-local has` would consult).
      The persistent write (`machine-local set`) is unchanged: still an
      external CLI subprocess, no in-process write substitute exists.
"""

from __future__ import annotations

import os
import subprocess
import sys
from typing import List, Optional, Tuple

from coordinator_core.machine_resolver import merged_flat_registry as _merged_flat_registry
from coordinator_core.win_portability import no_console_creationflags

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


def _key_already_captured() -> bool:
    """Whether `_KEY` is already set in the registry.

    Zero-spawn: `merged_flat_registry` reads the same registry.local.toml
    over registry.toml chain `machine-local keys` would enumerate, in-process
    -- no `machine-local` CLI subprocess (see
    `coordinator_core.machine_resolver.merged_flat_registry`). Best-effort,
    matching the prior degrade-to-"key absent" contract: a missing/unreadable
    registry file degrades to `{}`, never raises.
    """
    return _KEY in _merged_flat_registry()


def capture(check_only: bool = False) -> Tuple[str, int]:
    """Perform (or preview) the idempotent threshold capture.

    Returns (stdout_text, rc):
      rc 0 on all normal paths (pre-existing / would-write / written).
    """
    if _key_already_captured():
        return ("fan_out_threshold: pre-existing\n", 0)

    value = _compute_value()

    if check_only:
        return (f"fan_out_threshold: would write ({value})\n", 0)

    proc = subprocess.run(
        [*_ml_argv(), "set", _KEY, str(value)],
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        **no_console_creationflags(),
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
