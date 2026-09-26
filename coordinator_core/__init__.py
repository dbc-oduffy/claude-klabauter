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

# DR § AC-1b: importable as `from coordinator_core import OP_KEY_SCOPE, WORKTREE_SCOPED_OPS`.
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


def __dir__():
    return sorted(set(globals()) | set(__all__))

