"""coordinator_core.hooks.guard_host_subagent_bash_ban — PreToolUse(Bash) op:
independently-invocable door onto the already-landed host subagent-Bash-ban
guard.

Arrival note (W4-C8, docs/plans/2026-09-18-doe-holds-no-scripts.md): ported
from DoE-claude `coordinator/hooks/scripts/guard-host-subagent-bash-ban.py`.
That script's own header records it as `guard-not-a-hook-entrypoint` —
folded into `preuse-bash-dispatch.py`'s in-process guard registry, kept on
disk only so its own dedicated test suite keeps exercising the real
decision logic directly. The decision logic itself already lives here,
natively, as `coordinator_core.bash_guards.guard_host_subagent_bash_ban` —
registered in `coordinator_core.bash_guards.dispatch`'s own
`CONFINEMENT_DENY` band, so `hooks.preuse_bash_dispatch` (this row's own
sibling op) already reaches it on every Bash/PowerShell call via that
chain. This module is the SAME standalone-entrypoint role DoE's copy plays
for its own guard: a directly-invocable `hooks.<name>` door onto that one
guard's `check()`, for a caller (or a test) that wants this guard alone,
without running the whole chain.

Op contract (matches every other single-guard `hooks.*` op in this
package): `params` is the flat PreToolUse payload dict. Returns the nested
`hookSpecificOutput` deny envelope, or `no_advisory()` on allow/any failure.

Negative-spec:
    Does NOT re-implement the ban predicate — `check()` is called verbatim,
    unmodified, from `coordinator_core.bash_guards.guard_host_subagent_bash_
    ban`, the single source of truth for this decision (see that module's
    own docstring for the full IDENTITY RESOLUTION / CWD RESIDENCY history).
    Does NOT change that module's registration in the bash-guard chain —
    this op is an ADDITIONAL door onto the same check, not a replacement for
    the chain's own call to it.

Spec backlink: docs/plans/2026-09-18-doe-holds-no-scripts.md § W4-C8
"""

from __future__ import annotations

from coordinator_core.bash_guards.guard_host_subagent_bash_ban import check
from coordinator_core.hooks._envelope import no_advisory, payload_of
from coordinator_core.ipc import register_op


@register_op("hooks.guard_host_subagent_bash_ban")
def _handler(params: dict, repo_root=None) -> dict:
    """PreToolUse(Bash) op: deny a dispatched subagent's Bash call on a
    host that declares `subagent_bash_policy: deny`."""
    params = payload_of(params)

    try:
        result = check(params)
    except Exception:
        return no_advisory()  # any resolution failure -> fail-open ALLOW

    if result is None:
        return no_advisory()
    return result
