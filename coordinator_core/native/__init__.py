"""Loader for the optional Rust extension ``coordinator_core.native._hot``.

``load()`` returns the module or ``None``; callers keep their pure-Python body as the fallback.
Stdlib-only: importing this package must not move any import-budget ceiling.
"""

from __future__ import annotations

import importlib
import logging
import os

_MODULE_NAME = "coordinator_core.native._hot"
_DISABLE_VALUES = frozenset({"0", "false", "off", "no"})

_log = logging.getLogger(__name__)
_UNSET = object()
_cached: object = _UNSET


def _disabled() -> bool:
    return os.environ.get("COORDINATOR_NATIVE", "").strip().lower() in _DISABLE_VALUES


def load():
    """Return the native module, or ``None`` when disabled, absent or unloadable. Never raises.

    ``COORDINATOR_NATIVE`` is read on every call; a disabled value returns ``None`` without
    touching the cache. The first enabled call imports once and caches the outcome.
    """
    global _cached
    if _disabled():
        return None
    if _cached is _UNSET:
        try:
            _cached = importlib.import_module(_MODULE_NAME)
        except BaseException as exc:  # ImportError, ABI mismatch, a broken binary
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            _log.debug("native module unavailable: %s: %s", type(exc).__name__, exc)
            _cached = None
    return _cached


def _reset_for_tests() -> None:
    """Test-only: forget the cached import outcome."""
    global _cached
    _cached = _UNSET
