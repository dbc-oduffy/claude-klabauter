"""
coordinator_core.hooks._envelope — re-export shim for the hook return-envelope builders.

Purpose: preserve the historical import path. The implementation moved to
``coordinator_core._hook_envelope`` (package root) so that callers outside this
package — specifically the PreToolUse(Bash) guard chain in
``coordinator_core.bash_guards`` — can reach the five envelope builders without
importing ``coordinator_core.hooks``, whose ``__init__`` eagerly imports 13 hook
modules for their ``register_op()`` side effects. Those registrations are correct
and deliberate for the IPC server (see this package's ``__init__`` docstring), but
the bash-guard chain uses none of them and pays for them on every Bash tool call.

Modules INSIDE this package should keep importing from here — they trigger the
package ``__init__`` by their own existence, so the shim costs them nothing.
Modules OUTSIDE it that only need envelope shapes should import
``coordinator_core._hook_envelope`` directly.

Negative-spec:
    - Do NOT add logic here. This file is a re-export surface; the five builders
      have exactly one definition, at the package root.
    - Do NOT "simplify" callers in coordinator_core.bash_guards to import from
      this path. That reintroduces the hot-path cost this split exists to remove,
      and it does so silently — the guards keep working, just slower on every
      Bash call. Pinned by
      coordinator_core/tests/test_bash_guards_avoid_hooks_package.py.

Spec backlink: docs/plans/2026-07-04-pcore-04-advisory-hook-ops-claude-klabauter-engine.md § D2
"""

from __future__ import annotations

from coordinator_core._hook_envelope import (
    allow_advisory,
    context_only,
    deny,
    no_advisory,
    post_advisory,
    rewrite_input,
)

__all__ = [
    "allow_advisory",
    "context_only",
    "deny",
    "no_advisory",
    "post_advisory",
    "rewrite_input",
]
