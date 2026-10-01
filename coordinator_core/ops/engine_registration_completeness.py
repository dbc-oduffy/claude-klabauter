"""
coordinator_core.ops.engine_registration_completeness — JSON-RPC
"engine.registration_completeness" operation.

Purpose: ask the RUNNING engine whether its own op surface is registration-complete
(every registered op also carries its classification, scope, module-map and eager-list
entries) and which published generation answered. Suspended ops missing a surface are
reported under `suspended_by_design`, never under `violations`.

Zero spawns: one idempotent eager import, one pure check over the injected live registry
(never the pkgutil walk), two small file reads.
"""

from __future__ import annotations

from pathlib import Path

from coordinator_core.authz.registration_quad import (
    QuadViolation,
    check_registration_quad,
    filter_known_violations,
    partition_suspended,
)
from coordinator_core.ipc import _REGISTRY, register_op


def _engine_root() -> Path:
    """Root of the engine checkout/publish that is executing."""
    import coordinator_core

    return Path(coordinator_core.__file__).resolve().parent.parent


def _row(v: QuadViolation) -> dict:
    return {
        "op_key": v.op_key,
        "surfaces_missing": list(v.surfaces_missing),
        "missing_surface_files": [list(pair) for pair in v.missing_surface_files],
    }


def build_report(raw, *, suspended, generation: dict, registered_op_count: int) -> dict:
    """Pure: prune known debt once, then partition suspended ops into their own field."""
    pruned = filter_known_violations(list(raw), suspended=frozenset())
    live, by_design = partition_suspended(pruned, suspended)
    return {
        "complete": not live,
        "violations": [_row(v) for v in live],
        "suspended_by_design": [_row(v) for v in by_design],
        "registered_op_count": registered_op_count,
        "generation": generation,
    }


def _generation() -> dict:
    from coordinator_core.engine_version import engine_build
    from coordinator_core.warm.skew import read_engine_published_at, read_engine_stamp_sha

    root = _engine_root()
    published_at = read_engine_published_at(root)
    return {
        "engine_root": root.as_posix(),
        "stamp_sha": read_engine_stamp_sha(root),
        "published_at": published_at.isoformat() if published_at is not None else None,
        "engine_sha": engine_build()["engine_sha"],
    }


@register_op("engine.registration_completeness")
def _engine_registration_completeness(params: dict, repo_root=None) -> dict:
    """JSON-RPC 'engine.registration_completeness' handler.

    Params: none consumed. repo_root: unused — reports on the engine that is running.
    """
    from coordinator_core.op_budget_suspension import SUSPENDED_OPS
    from coordinator_core.ops import _eager_import_all

    _eager_import_all()
    registry = dict(_REGISTRY)
    return build_report(
        check_registration_quad(registry=registry),
        suspended=frozenset(SUSPENDED_OPS),
        generation=_generation(),
        registered_op_count=len(registry),
    )
