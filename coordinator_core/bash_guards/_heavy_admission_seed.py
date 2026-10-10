"""Seeds the machine-local heavy_admission.* registry keys from host total RAM at setup.

The sizing formula lives here and nowhere in the guard: an absent key is untrusted there and
denies heavy launches. Spawn-free; an operator's existing value is never overwritten.
"""

from __future__ import annotations

from typing import Callable, Optional

from coordinator_core.bash_guards._heavy_admission_contract import (
    KEY_FREE_RAM_FLOOR_MB,
    KEY_LEASE_RESERVE_MB,
    KEY_SESSION_BACKGROUND_CAP,
    KEY_SESSION_HEAVY_CAP,
    KEY_VITEST_MAX_WORKERS,
    KEY_WORKER_RSS_CEILING_MB,
)

_FLOOR_MIN_MB = 1536
_FLOOR_MAX_MB = 8192
# example-stats-repo caps each vitest worker heap at 4096 MB (3a6aa0a); one over that is a runaway.
_WORKER_RSS_CEILING_MB = 4096


def derive_defaults(total_mb: int) -> dict:
    """Map host total RAM (MB) to the heavy_admission values, keyed by contract key name."""
    floor = min(_FLOOR_MAX_MB, max(_FLOOR_MIN_MB, total_mb // 10))
    if total_mb < 24 * 1024:
        heavy_cap = 1
    elif total_mb < 64 * 1024:
        heavy_cap = 2
    else:
        heavy_cap = 3
    return {
        KEY_FREE_RAM_FLOOR_MB: floor,
        KEY_SESSION_HEAVY_CAP: heavy_cap,
        KEY_SESSION_BACKGROUND_CAP: heavy_cap + 2,
        KEY_LEASE_RESERVE_MB: 1024 if total_mb < 24 * 1024 else 2048,
        KEY_VITEST_MAX_WORKERS: heavy_cap * 2,
        KEY_WORKER_RSS_CEILING_MB: _WORKER_RSS_CEILING_MB,
    }


def _host_total_mb() -> Optional[int]:
    import sys

    from coordinator_core.telemetry import host_sampler

    if sys.platform == "win32":
        reader = host_sampler._windows_memory_mb
    elif sys.platform == "darwin":
        reader = host_sampler._darwin_memory_mb
    else:
        reader = host_sampler._posix_memory_mb
    total = reader()[2]
    return int(total) if total and total > 0 else None


def seed_if_absent(
    total_mb: Optional[int] = None,
    *,
    get: Optional[Callable[[], dict]] = None,
    put: Optional[Callable[[str, str], None]] = None,
) -> dict:
    """Write each absent heavy_admission key; return {key: value} for the keys written.

    Returns {} without writing when total RAM is unreadable, leaving the guard unconfigured and
    fail-closed. A failed write for one key is skipped, not raised.
    """
    if get is None or put is None:
        from coordinator_core import machine_resolver

        get = get or machine_resolver.merged_flat_registry
        put = put or machine_resolver.registry_set
    if total_mb is None:
        total_mb = _host_total_mb()
    if total_mb is None:
        return {}
    existing = get()
    written: dict = {}
    for key, value in derive_defaults(total_mb).items():
        if key in existing:
            continue
        try:
            put(key, str(value))
        except (OSError, ValueError):
            continue
        written[key] = value
    return written
