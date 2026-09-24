"""coordinator_core.hooks.guard_host_subagent_bash_spawn_shapes — PreToolUse
(Bash|PowerShell) op: independently-invocable door onto the already-landed
host subagent-Bash-spawn-shapes decline guard.

Arrival note (W4-C8, docs/plans/2026-09-18-doe-holds-no-scripts.md): ported
from DoE-claude `coordinator/hooks/scripts/guard-host-subagent-bash-spawn-
shapes.py`, the sibling of `guard-host-subagent-bash-ban.py` — same
`guard-not-a-hook-entrypoint` header, same fold into
`preuse-bash-dispatch.py`'s in-process guard registry, same
already-landed-natively decision logic:
`coordinator_core.bash_guards.guard_host_subagent_bash_spawn_shapes`,
registered in `coordinator_core.bash_guards.dispatch`'s own
`CONFINEMENT_DENY` band and already reached on every Bash/PowerShell call
via `hooks.preuse_bash_dispatch` (this row's own sibling op). This module
plays the same standalone-entrypoint role for this guard that
`guard_host_subagent_bash_ban` (this row's other sibling op) plays for its
own — a directly-invocable `hooks.<name>` door onto that one guard's
`check()` alone.

Op contract (matches every other single-guard `hooks.*` op in this
package): `params` is the flat PreToolUse payload dict. Returns the nested
`hookSpecificOutput` deny envelope, or `no_advisory()` on allow/any failure.

Negative-spec:
    Does NOT re-implement the decline predicate — `check()` is called
    verbatim, unmodified, from `coordinator_core.bash_guards.guard_host_
    subagent_bash_spawn_shapes`, the single source of truth for this
    decision.
    Does NOT change that module's registration in the bash-guard chain —
    this op is an ADDITIONAL door onto the same check, not a replacement.

Spec backlink: docs/plans/2026-09-18-doe-holds-no-scripts.md § W4-C8
"""

from __future__ import annotations

from coordinator_core.bash_guards.guard_host_subagent_bash_spawn_shapes import check
from coordinator_core.hooks._envelope import no_advisory, payload_of
from coordinator_core.ipc import register_op


@register_op("hooks.guard_host_subagent_bash_spawn_shapes")
def _handler(params: dict, repo_root=None) -> dict:
    """PreToolUse(Bash|PowerShell) op: deny a dispatched subagent's
    in-process-answerable spawn shape on a host that declares the deny
    policy."""
    params = payload_of(params)

    try:
        result = check(params)
    except Exception:
        return no_advisory()  # any resolution failure -> fail-open ALLOW

    if result is None:
        return no_advisory()
    return result
