"""
coordinator_core._claude_klabauter_root -- shared, memoized ``_machine_local_get`` plus
the ``_claude_home`` / ``_machine_local_impl`` path helpers it depends on,
consumed by coordinator_core's non-server ops (R4,
docs/plans/2026-09-22-spawn-budget-and-census.md).

No import-time side effects: no ``register_op``, no I/O, no subprocess spawn
at import -- a read-op module can safely import this without acquiring a
write-op's side effects. Do NOT import this module from
``coordinator_core.ops.queue_append``: that module runs ``register_op()`` at
import, a write-op side effect a read-op importer must never inherit
transitively.

Mechanism only: each caller keeps its own ``_claude_klabauter_root()`` rung order and
its own raise-versus-degrade policy on an unresolvable or mirror-resolved
root (``queue_append`` raises, ``deliverable_rollup`` degrades). See
state/improvement-queue/2026-07-06-claude-klabauter-live-root-shared-helper-extraction.yaml.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from typing import Dict, Optional, Tuple

from coordinator_core._settings_home import settings_home
from coordinator_core.win_portability import console_python, no_console_creationflags

_MACHINE_LOCAL_IMPL_ENV = "MACHINE_LOCAL_IMPL"
_CLAUDE_HOME_ENV = "CLAUDE_HOME"

#: JSON ``{key: value}`` a parent exports so its pool workers answer a lookup
#: without a spawn; see ``seed_machine_local``.
MACHINE_LOCAL_SEED_ENV = "COORDINATOR_MACHINE_LOCAL_SEED"

_MACHINE_LOCAL_TIMEOUT = 2.0

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


def _seeded_value(key: str) -> Optional[str]:
    """The parent-seeded value for ``key``, or None when the seed is absent,
    malformed or lacks the key."""
    raw = os.environ.get(MACHINE_LOCAL_SEED_ENV)
    if not raw:
        return None
    try:
        seed = json.loads(raw)
    except ValueError:
        return None
    value = seed.get(key) if isinstance(seed, dict) else None
    return value if isinstance(value, str) and value else None


def seed_machine_local(*keys: str) -> None:
    """Resolve each ``key`` once via ``_machine_local_get`` and export the
    resolved ones into ``MACHINE_LOCAL_SEED_ENV`` so children inheriting the
    environment (pool workers) skip the per-process spawn. Keys that resolve to
    None are left out, so their lookups fall through unchanged. Existing seed
    entries are kept."""
    try:
        current = json.loads(os.environ.get(MACHINE_LOCAL_SEED_ENV) or "{}")
    except ValueError:
        current = {}
    if not isinstance(current, dict):
        current = {}
    for key in keys:
        value = _machine_local_get(key)
        if value:
            current[key] = value
    if current:
        os.environ[MACHINE_LOCAL_SEED_ENV] = json.dumps(current)


def _machine_local_get(key: str) -> Optional[str]:
    """Call ``_machine_local.py get <key>`` and return the value, or None on
    any failure (missing impl, nonzero exit, empty stdout, timeout).

    A value seeded by the parent (``MACHINE_LOCAL_SEED_ENV``) answers with no
    spawn; a missing seed or key falls through to the spawn below.

    Memoized per process, keyed by ``(key, resolved impl path)`` -- the impl
    path already folds in every env var that steers *which*
    ``_machine_local.py`` gets shelled out to (``MACHINE_LOCAL_IMPL``,
    ``CLAUDE_HOME`` via ``_machine_local_impl()``/``settings_home()``), so a
    changed override env naturally produces a different cache key rather than
    a stale hit. A None (unavailable/empty) result is memoized too: it is a
    deterministic function of the impl's on-disk state for the remainder of
    this process, not a transient failure that later calls should retry.
    """
    seeded = _seeded_value(key)
    if seeded is not None:
        return seeded
    impl = _machine_local_impl()
    cache_key = (key, impl)
    if cache_key in _machine_local_cache:
        return _machine_local_cache[cache_key]

    if not os.path.exists(impl):
        _machine_local_cache[cache_key] = None
        return None
    try:
        result = subprocess.run(
            [console_python(sys.executable), impl, "get", key],
            capture_output=True,
            text=True,
            timeout=_MACHINE_LOCAL_TIMEOUT,
            **no_console_creationflags(),
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
