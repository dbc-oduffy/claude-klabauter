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

# `logger` is resolved lazily by the __getattr__ below rather than bound here, for the
# same reason the cache/authz re-exports are: `import logging` is not free, and this
# package sits on the PreToolUse(Bash) hot path via coordinator_core.bash_guards, which
# runs on every Bash tool call. `logging` drags traceback -> _colorize -> dataclasses ->
# inspect behind it; measured at 10.3ms, the dominant term in `import coordinator_core`.
# Nothing in either repo reads coordinator_core.logger today, so nothing pays that cost
# on purpose — but the name stays resolvable so any future caller still gets a logger.
# Deliberately NOT in __all__, unlike the cache/authz names below that share this same
# __getattr__ seam: those are cross-repo contract exports, `logger` is an internal
# diagnostic aid. The asymmetry predates the lazy change and is intentional.

# Cache primitives and auth-seam read surface — re-exported for pcore-06/10/11 consumers
# and the DoE-shim auth-seam consumer, respectively, but resolved LAZILY (PEP 562
# module-level __getattr__) rather than imported at module load. coordinator_core.cache
# and coordinator_core.authz.token are each ~3-6ms of cold-import cost that only pays off
# for callers who actually touch these names; `import coordinator_core` itself never needs
# them eagerly. Resolution is memoized into module globals on first access so repeat
# lookups (including `dir()`/tab-completion via __dir__ below) don't re-import.
#
# Write surface (write_tokens, generate_token) is deliberately NOT resolvable here — only
# the names in __all__ are — to prevent callers from bypassing write_tokens' atomic chmod
# enforcement by reaching through coordinator_core instead of coordinator_core.authz.token.
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

# start_server retired by DR-215 (command-type execution model).
# Backlink: docs/decisions/DR-215-coordinator-core-command-type-execution-model.md
