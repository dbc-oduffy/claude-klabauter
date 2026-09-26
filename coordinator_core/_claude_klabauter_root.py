"""
coordinator_core._claude_klabauter_root -- shared, memoized ``_machine_local_get`` and
the ``_claude_klabauter_root()`` resolution-rung helpers duplicated across
coordinator_core's non-server ops (R4,
docs/plans/2026-09-22-spawn-budget-and-census.md).

No import-time side effects: no ``register_op``, no I/O, no subprocess spawn
at import -- a read-op module can safely import this without acquiring a
write-op's side effects. Do NOT import this module from
``coordinator_core.ops.queue_append``: that module runs ``register_op()`` at
import, a write-op side effect a read-op importer must never inherit
transitively.

Rung helpers (env override, ``engine.source_root``, machine-local registry,
mirror refusal) are kept as SEPARATE functions here, never assembled into one
``_claude_klabauter_root()`` -- each caller keeps its own raise-versus-degrade policy on
an unresolvable or mirror-resolved root. See
state/improvement-queue/2026-07-06-claude-klabauter-live-root-shared-helper-extraction.yaml.
"""

from __future__ import annotations

import os
import subprocess
import sys
from typing import Dict, Optional, Tuple

from coordinator_core._settings_home import settings_home
from coordinator_core.engine_root import (
    coordinator_engine_root_env,
    engine_source_root,
    is_published_engine_mirror,
)
from coordinator_core.telemetry import op_latency

_MACHINE_LOCAL_IMPL_ENV = "MACHINE_LOCAL_IMPL"
_CLAUDE_HOME_ENV = "CLAUDE_HOME"
_REGISTRY_KEY = "repos.claude_klabauter"

_MACHINE_LOCAL_TIMEOUT = 5

_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

_machine_local_cache: Dict[Tuple[str, str], Optional[str]] = {}


def _claude_home() -> str:
    """Return the ~/.claude root, honouring CLAUDE_HOME env var for test
    isolation."""
    override = os.environ.get(_CLAUDE_HOME_ENV)
    if override:
        return override
    home = os.environ.get("HOME") or os.environ.get("USERPROFILE") or os.path.expanduser("~")
    return os.path.join(home, ".claude")


def _machine_local_impl() -> str:
    """Return the path to ``_machine_local.py``, honouring
    ``MACHINE_LOCAL_IMPL`` for tests."""
    override = os.environ.get(_MACHINE_LOCAL_IMPL_ENV)
    if override:
        return override
    settings_home_impl = os.path.join(str(settings_home()), "bin", "_machine_local.py")
    if os.path.exists(settings_home_impl):
        return settings_home_impl
    return os.path.join(_claude_home(), "bin", "_machine_local.py")


def clear_machine_local_cache() -> None:
    """Reset the ``_machine_local_get`` memoization cache. Tests that mutate
    ``MACHINE_LOCAL_IMPL``, ``CLAUDE_HOME`` or the registry between cases MUST
    call this in setup/teardown -- a stale entry would otherwise return a
    memoized result for a since-changed steering env, and the module has no
    other reset hook."""
    _machine_local_cache.clear()


def _machine_local_get(key: str) -> Optional[str]:
    """Call ``_machine_local.py get <key>`` and return the value, or None on
    any failure (missing impl, nonzero exit, empty stdout, timeout).

    Memoized per process, keyed by ``(key, resolved impl path)`` -- the impl
    path already folds in every env var that steers *which*
    ``_machine_local.py`` gets shelled out to (``MACHINE_LOCAL_IMPL``,
    ``CLAUDE_HOME`` via ``_machine_local_impl()``/``settings_home()``), so a
    changed override env naturally produces a different cache key rather than
    a stale hit. A None (unavailable/empty) result is memoized too: it is a
    deterministic function of the impl's on-disk state for the remainder of
    this process, not a transient failure that later calls should retry.
    """
    impl = _machine_local_impl()
    cache_key = (key, impl)
    if cache_key in _machine_local_cache:
        return _machine_local_cache[cache_key]

    if not os.path.exists(impl):
        _machine_local_cache[cache_key] = None
        return None
    try:
        result = subprocess.run(
            [sys.executable, impl, "get", key],
            capture_output=True,
            text=True,
            timeout=_MACHINE_LOCAL_TIMEOUT,
            creationflags=_CREATE_NO_WINDOW,
        )
    except (OSError, subprocess.TimeoutExpired):
        _machine_local_cache[cache_key] = None
        return None
    if result.returncode != 0 or not result.stdout.strip():
        _machine_local_cache[cache_key] = None
        return None
    value = result.stdout.strip()
    _machine_local_cache[cache_key] = value
    return value


def env_override_rung(caller_module_name: str) -> Optional[str]:
    """Rung 1: the ``COORDINATOR_ENGINE_ROOT`` env var, via the accessor,
    honoured only when this process IS the one the caller ran in
    (``execution_route() == IN_PROCESS``) -- a warm-served process inherits
    its SPAWNER's environment, not the current caller's, so trusting the raw
    read under warm serving would name the spawner's root rather than the
    current caller's.

    Returns the expanded (``~``/env-var) candidate root, unchecked for
    mirror -- callers apply their own mirror-refusal policy
    (``is_published_mirror_root``) to the result.

    ``caller_module_name`` tags the reading call site for
    ``coordinator_engine_root_env``'s advisories -- pass the calling module's
    ``__name__``."""
    override = (coordinator_engine_root_env(caller_module_name) or "").strip()
    if override and op_latency.execution_route() == op_latency.IN_PROCESS:
        return os.path.expanduser(os.path.expandvars(override))
    return None


def engine_source_root_rung() -> Optional[str]:
    """Rung 1.5: the transform-proof ``engine.source_root`` registry key.
    Reached on the served route, where rung 1 (``env_override_rung``) is
    skipped. Already mirror-safe by construction -- see
    ``coordinator_core.engine_root.engine_source_root``'s own docstring."""
    return engine_source_root() or None


def registry_rung(key: str = _REGISTRY_KEY) -> Optional[str]:
    """Rung 2: ``machine-local get <key>``, defaulting to
    ``repos.claude_klabauter``."""
    return _machine_local_get(key) or None


def is_published_mirror_root(root: str) -> bool:
    """Mirror-refusal predicate: True when ``root`` resolves to the
    published engine mirror rather than a live working tree. Callers apply
    their own raise-versus-degrade policy on a True result -- some raise
    (e.g. ``queue_append._refuse_published_mirror``), some return None with a
    WARN (e.g. ``deliverable_rollup._refuse_published_mirror``)."""
    return is_published_engine_mirror(root)
