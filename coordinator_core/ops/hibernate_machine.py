"""
coordinator_core.ops.hibernate_machine — JSON-RPC "machine.hibernate" operation.

Purpose: native cross-platform hibernate/suspend dispatch for the mise-en-place
night-shift fence (coordinator/commands/mise-en-place.md:292;
pipelines/mise-en-place/PIPELINE.md:243). The fence carried two commented
alternatives mislabeled "Linux/Mac" — `systemctl hibernate` is Linux-only
(macOS has no systemd) — so this module ships THREE native `sys.platform`
branches per DEC-6/AC12 (settlement B1,
docs/plans/2026-07-22-wave-3-design-settlements-15-design-bear.md):

    macOS (darwin)  — `pmset sleepnow`
    Windows (win32) — ctypes `PowrProf.SetSuspendState(1, 0, 0)` PRIMARY
                      (first arg TRUE → hibernate); `shutdown /h` fallback
                      when the ctypes call is unavailable or reports failure.
                      DEC-6 names both; ctypes is the more native, so it leads.
    Linux (linux)   — `systemctl hibernate`
    anything else   — structured UnsupportedPlatformError naming sys.platform.

Idempotency (AC7, DEC-7 note): VACUOUSLY satisfied, not by design — a second
invocation with identical inputs never runs, because the machine is suspended
by the first (DEC-6's own wording). This is recorded as reasoning, not
asserted as a clean pass. The double-invocation test accordingly mocks the
dispatch layer and asserts the op is *re-invocable* — two calls yield
identical results and accrue no state — rather than pretending to prove a
runtime property the op's success makes unobservable.

CC-1 note (AC12 "no shell-out"): `pmset`, `systemctl`, and `shutdown` are
platform binaries, not shell interpreters. Every spawn here is direct
list-argv `subprocess.run` — no `shell=True`, no bash/sh/cmd string anywhere
in the invocation chain — so AC12 is satisfied under the DEC-6/CC-1 reading.

Fail-loud (CC-7): a dispatch binary exiting nonzero raises a structured
HibernateDispatchError naming the argv, returncode, and stderr — the op never
reports `dispatched: true` on a failed spawn.

Scope: `none` (pure OS power-state action, touches no repo state; ratified
per-row in op-classification.tsv). Registration on the three shared surfaces
(`_EAGER_OP_MODULES`, `_OP_KEY_SCOPE`, `_registry_map.py`) lands in the
EM-serial registration pass per CC-3; this module carries only its own
`register_op`.

Contract: params {} -> {platform: str, method: str, dispatched: bool}
Spec backlink: pln-wave-3-design-settlements-15-d-76fdbd § B1
Parent plan:   docs/plans/2026-07-22-coordinator-ops-buildout-from-fence-inventory.md § DEC-6
"""

from __future__ import annotations

import subprocess
from coordinator_core.win_portability import no_console_creationflags
import sys
from typing import List, Optional

from coordinator_core.ipc import register_op


class UnsupportedPlatformError(ValueError):
    """Raised when sys.platform matches none of the three native branches."""

    def __init__(self, platform: str) -> None:
        self.platform = platform
        super().__init__(
            f"machine.hibernate: unsupported platform {platform!r} — "
            "native branches exist for darwin (pmset sleepnow), win32 "
            "(SetSuspendState / shutdown /h), and linux (systemctl hibernate) only"
        )


class HibernateDispatchError(RuntimeError):
    """Raised when a hibernate dispatch binary exits nonzero (CC-7 fail-loud)."""

    def __init__(self, argv: List[str], returncode: int, stderr: str) -> None:
        self.argv = list(argv)
        self.returncode = returncode
        self.stderr = stderr
        super().__init__(
            f"machine.hibernate: {argv!r} exited {returncode}: {stderr.strip()}"
        )


def _run_binary(argv: List[str]) -> None:
    """Direct list-argv spawn of a platform binary (CC-1: no shell interpreter).

    Raises HibernateDispatchError on a nonzero exit — the caller never reports
    a failed dispatch as success.
    """
    # Review: code-reviewer (F1, 2026-07-22) — the ctypes-primary path never
    # spawns a subprocess, but this shutdown-/h fallback does; without these
    # two flags it risks the Windows console-popup / interactive-hang classes
    # `git_native.py` and `run_pre_ci_hooks.py` this same wave already guard
    # against (cross-repo/archive/2026-05-30-windows-popup-child-process-hypothesis.md).
    proc = subprocess.run(
        argv,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        stdin=subprocess.DEVNULL,
        **no_console_creationflags(),
    )
    if proc.returncode != 0:
        raise HibernateDispatchError(argv, proc.returncode, proc.stderr or "")


def _windows_ctypes_suspend() -> bool:
    """Attempt PowrProf.SetSuspendState(1, 0, 0) — TRUE first arg → hibernate.

    Returns True when the call is available AND reports success (nonzero
    BOOLEAN per the Win32 contract); False when ctypes.windll is absent
    (non-Windows interpreter), the symbol fails to resolve, or the call
    reports failure — the caller then falls back to `shutdown /h` per B1.
    """
    import ctypes

    windll = getattr(ctypes, "windll", None)
    if windll is None:
        return False
    try:
        result = windll.PowrProf.SetSuspendState(1, 0, 0)
    except (AttributeError, OSError):
        return False
    return bool(result)


def hibernate(platform: Optional[str] = None) -> dict:
    """Dispatch a native hibernate for the current (or given) platform.

    platform: sys.platform-shaped string; None → sys.platform. Injectable so
    tests exercise every branch without monkeypatching the interpreter's own
    sys.platform (tests mock the dispatch helpers — never actually suspend).

    Returns {platform, method, dispatched: True} on successful dispatch.
    Raises UnsupportedPlatformError on an unknown platform and
    HibernateDispatchError on a failed spawn.
    """
    plat = sys.platform if platform is None else platform

    if plat == "darwin":
        _run_binary(["pmset", "sleepnow"])
        return {"platform": plat, "method": "pmset sleepnow", "dispatched": True}

    if plat == "win32":
        if _windows_ctypes_suspend():
            return {
                "platform": plat,
                "method": "ctypes PowrProf.SetSuspendState(1, 0, 0)",
                "dispatched": True,
            }
        _run_binary(["shutdown", "/h"])
        return {"platform": plat, "method": "shutdown /h", "dispatched": True}

    if plat.startswith("linux"):
        _run_binary(["systemctl", "hibernate"])
        return {"platform": plat, "method": "systemctl hibernate", "dispatched": True}

    raise UnsupportedPlatformError(plat)


@register_op("machine.hibernate")
def _machine_hibernate(params: dict, repo_root=None) -> dict:
    """JSON-RPC 'machine.hibernate' handler — dispatch a native hibernate.

    Params: none consumed (contract `params: {}`).
    repo_root: unused — scope `none` (pure OS power-state action, no repo
    state; ipc injects None for none-keyed ops).
    """
    return hibernate()
