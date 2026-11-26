"""
coordinator_core — In-process command-type coordinator engine.

Purpose: JSON-RPC 2.0 op-registry + dispatch surface for the coordinator plugin.
Replaces the per-hook/per-gate spawn swarm with direct in-process dispatch — callers
invoke coordinator_core.invoke to route ops without a resident daemon or socket.

Resident-daemon transport (UDS server, start_server) retired by DR-215.
Command-type dispatch: see coordinator_core.invoke.__main__

Spec backlink: pln-pcore-03-beachhead-coordinator-core-fecdbb
DR-215:        docs/decisions/DR-215-coordinator-core-command-type-execution-model.md

Negative-spec:
    - This package is stdlib-only on the hot path (asyncio, json, hashlib,
      subprocess for git). No third-party deps on the dispatch layer.
    - The service is a query-and-compute surface only. It MUST NOT write coordinator
      substrate (handoffs, review-trail, commits). Writers remain the EM/agent's job.
"""

from __future__ import annotations

import importlib
import os as _os


def _hide_child_consoles() -> None:
    """Windows: every child any claude-klabauter process spawns opens no console window and raises
    no AppStarting cursor, unless the call asks for its own console.

    Root of the PM-visible pointer flicker: Python's default spawn shows both, and the box
    runs thousands of bare git/python spawns a minute. Defaulting it here covers every
    engine process, CLI, hook body and test without touching each call site. Trap: a
    caller that passes its own `startupinfo` or CREATE_NEW_CONSOLE / DETACHED_PROCESS keeps it.

    Invariant: a spawner that owns a console adds neither CREATE_NO_WINDOW nor SW_HIDE. Its
    child inherits that console and opens no window anyway, and CREATE_NO_WINDOW there moves
    the child onto a hidden console of its own: an interactive TUI (agy, grok) launched by a
    coordinator python draws to nothing. Only the feedback cursor is suppressed."""
    import sys

    if sys.platform != "win32":
        return
    import subprocess

    real_init = subprocess.Popen.__init__
    if getattr(real_init, "_hides_consoles", False):
        return
    own_console = subprocess.CREATE_NEW_CONSOLE | subprocess.DETACHED_PROCESS

    def init(self, *args, **kwargs) -> None:
        flags = kwargs.get("creationflags", 0) or 0
        if not flags & own_console:
            windowless = not _spawner_has_console()
            if windowless:
                kwargs["creationflags"] = flags | subprocess.CREATE_NO_WINDOW
            if kwargs.get("startupinfo") is None:
                info = subprocess.STARTUPINFO()
                info.dwFlags |= 0x80  # STARTF_FORCEOFFFEEDBACK
                if windowless:
                    info.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                    info.wShowWindow = 0  # SW_HIDE
                kwargs["startupinfo"] = info
        real_init(self, *args, **kwargs)

    init._hides_consoles = True  # type: ignore[attr-defined]
    subprocess.Popen.__init__ = init  # type: ignore[method-assign]


_get_console_window = None


def _spawner_has_console() -> bool:
    """Is this process attached to a visible console? Read per spawn: AllocConsole/FreeConsole
    can change it. ctypes loads on the first spawn, never at import (hook import budget)."""
    global _get_console_window
    if _get_console_window is None:
        import ctypes

        _get_console_window = ctypes.windll.kernel32.GetConsoleWindow  # type: ignore[attr-defined]
    return bool(_get_console_window())


_hide_child_consoles()

# Hold this import out of a publish swap window at its own engine root. Must never raise.
try:
    from coordinator_core import _engine_landing

    _engine_landing.wait_until_clear(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
except Exception:  # noqa: BLE001 - the gate must not become a new way to fail every hook
    pass

# Op-scope parity surface — re-exported for cross-repo contract consumers (e.g. DoE shim).
# DR § AC-1b: importable as `from coordinator_core import OP_KEY_SCOPE, WORKTREE_SCOPED_OPS`.
# Sourced from the dependency-free op_scopes module (not coordinator_core.ipc) so that
# `import coordinator_core` does not transitively pull in ipc.py's top-level `import asyncio` —
# see coordinator_core/op_scopes.py module docstring for the full rationale. ipc.py still
# re-exports the same names for existing `from coordinator_core.ipc import ...` call sites.
# Kept EAGER (unlike the two lazy re-export groups below): ~0.1ms, dependency-free, and
# deliberately not worth the __getattr__ indirection.
from coordinator_core.op_scopes import OP_KEY_SCOPE, WORKTREE_SCOPED_OPS  # noqa: F401

__all__ = [
    "compute_stamp",
    "OP_KEY_SCOPE",
    "read_revalidated",
    "read_token",
    "read_token_ro",
    "WORKTREE_SCOPED_OPS",
]


_LAZY_REEXPORTS = {
    "compute_stamp": "coordinator_core.cache",
    "read_revalidated": "coordinator_core.cache",
    "read_token": "coordinator_core.authz.token",
    "read_token_ro": "coordinator_core.authz.token",
}


def __getattr__(name: str):
    if name == "logger":
        import logging

        value = logging.getLogger(__name__)
        globals()[name] = value
        return value
    module_path = _LAZY_REEXPORTS.get(name)
    if module_path is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module = importlib.import_module(module_path)
    value = getattr(module, name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))

