"""
coordinator_core.subagent_sandbox -- resolver + policy-load package + spawn-time
report-sidecar provisioner. DR-058 removed the PreToolUse Write/Edit/MultiEdit
DENY enforcement this package used to carry; see engine.py's module docstring
for what survived and who still depends on it.

Public seam: Policy / load_policy / resolve_git_root are the resolver
primitives coordinator_core.bash_guards._helpers re-exports and
coordinator_core.subagent_sandbox.provision_report consumes directly.

See engine.py for the resolver/policy-load implementation and CONTRACT.md for
the pinned provision-and-emit contract this package binds to.
"""

from __future__ import annotations

from coordinator_core.subagent_sandbox.engine import (
    Policy,
    load_policy,
    resolve_git_root,
)

__all__ = [
    "Policy",
    "load_policy",
    "resolve_git_root",
]
