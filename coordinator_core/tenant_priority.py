"""Prime-tenant background priority: lower this process and its children on a box that serves example-retrieval-repo first.

Prime tenancy is the machine-local key `example_retrieval_repo_prime_tenant` (truthy string); absent or
unreadable means not prime. Off-prime boxes, win32 and serve-lineage argv are never touched.
Module-top imports stay stdlib-only; cold CLI callers import this.
"""

from __future__ import annotations

import ctypes
import os
import sys

PRIME_TENANT_KEY = "example_retrieval_repo_prime_tenant"
PRIME_TENANT_NICE = 15
SERVE_LINEAGE_MARKERS = ("run_daemon_supervisor.py", "example_retrieval_repo_server.py")

_PRIO_DARWIN_PROCESS = 4
_PRIO_DARWIN_BG = 0x1000
_FALSY = frozenset({"", "0", "false", "no", "off"})

_UNSET = object()
_PRIME = _UNSET


def _resolve_darwin_setpriority():
    if sys.platform != "darwin":
        return None
    try:
        fn = ctypes.CDLL(None, use_errno=True).setpriority
        fn.argtypes = [ctypes.c_int, ctypes.c_uint, ctypes.c_int]
        fn.restype = ctypes.c_int
        return fn
    except Exception:
        return None


# Resolved at import so the fork-to-exec hook never loads a library.
_DARWIN_SETPRIORITY = _resolve_darwin_setpriority()


def is_prime_tenant() -> bool:
    """True when this box's machine-local config marks example-retrieval-repo as the prime tenant; memoised per process, any failure is False."""
    global _PRIME
    if _PRIME is _UNSET:
        try:
            from coordinator_core import machine_local_spawn

            value, ok = machine_local_spawn.cached_get(machine_local_spawn.kernel_path(), PRIME_TENANT_KEY)
            _PRIME = bool(ok and isinstance(value, str) and value.strip().lower() not in _FALSY)
        except Exception:
            _PRIME = False
    return _PRIME


def _names_serve(argv) -> bool:
    try:
        for arg in argv or ():
            text = str(arg).replace("\\", "/")
            if text.endswith(SERVE_LINEAGE_MARKERS):
                return True
    except Exception:
        pass
    return False


def apply_to_self() -> dict:
    """For a batch/example-game-repo process lowering itself to PRIME_TENANT_NICE (darwin: plus background QoS); never an engine entry point. Never raises.

    No-ops, returning {"prime": False}, off-prime, on win32, or when sys.argv names a serve marker.
    """
    try:
        if sys.platform == "win32" or _names_serve(sys.argv) or not is_prime_tenant():
            return {"prime": False}
        nice = os.getpriority(os.PRIO_PROCESS, 0)
        if nice < PRIME_TENANT_NICE:
            try:
                os.setpriority(os.PRIO_PROCESS, 0, PRIME_TENANT_NICE)
                nice = PRIME_TENANT_NICE
            except OSError:
                pass
        bg = False
        if _DARWIN_SETPRIORITY is not None:
            bg = _DARWIN_SETPRIORITY(_PRIO_DARWIN_PROCESS, 0, _PRIO_DARWIN_BG) == 0
        return {"prime": True, "nice": nice, "bg": bg}
    except Exception:
        return {"prime": False}


def _lower_child() -> None:
    # Runs between fork and exec in a possibly threaded parent: no imports, no config reads, no allocation beyond ints.
    try:
        os.setpriority(os.PRIO_PROCESS, 0, PRIME_TENANT_NICE)
    except Exception:
        pass
    try:
        if _DARWIN_SETPRIORITY is not None:
            _DARWIN_SETPRIORITY(_PRIO_DARWIN_PROCESS, 0, _PRIO_DARWIN_BG)
    except Exception:
        pass


def popen_kwargs(argv=None) -> dict:
    """Popen kwargs that lower a batch leg to nice 15 on a prime box, whatever the parent's nice; {} off-prime, on win32, or for a serve-lineage argv.

    A caller with its own preexec_fn chains it: call the returned one first, then its own.
    """
    try:
        if sys.platform == "win32" or _names_serve(argv) or not is_prime_tenant():
            return {}
        return {"preexec_fn": _lower_child}
    except Exception:
        return {}


def is_serve_lineage(cmdline) -> bool:
    """True when a process cmdline (string or argv sequence) names a example-retrieval-repo serve marker; the bounce selector excludes these and their descendants."""
    if isinstance(cmdline, str):
        text = cmdline.replace("\\", "/")
        return any(marker in text for marker in SERVE_LINEAGE_MARKERS)
    return _names_serve(cmdline)


class SettingsHomeKeyMissing(RuntimeError):
    """The settings-home root key is absent or unresolvable."""


def settings_home_prefix() -> str:
    """Settings-home root with a trailing separator, '/'-normalised, for cmdline prefix matching; raises SettingsHomeKeyMissing naming COORDINATOR_SETTINGS_HOME when unresolvable."""
    try:
        from coordinator_core._settings_home import settings_home

        root = str(settings_home()).replace("\\", "/").rstrip("/")
    except Exception as exc:
        raise SettingsHomeKeyMissing(f"COORDINATOR_SETTINGS_HOME unresolvable: {exc}") from exc
    if not root:
        raise SettingsHomeKeyMissing("COORDINATOR_SETTINGS_HOME resolved empty")
    return root + "/"


def _reset_for_tests() -> None:
    global _PRIME
    _PRIME = _UNSET
