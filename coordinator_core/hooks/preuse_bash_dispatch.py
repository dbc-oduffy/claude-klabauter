"""coordinator_core.hooks.preuse_bash_dispatch — PreToolUse(Bash|PowerShell)
op: run the full cold-hook bash-guard chain against a native-door payload.

Arrival note (W4-C8, docs/plans/2026-09-18-doe-holds-no-scripts.md): ported
from DoE-claude `coordinator/hooks/scripts/preuse-bash-dispatch.py`. That
script's whole job was PLUMBING: resolve a SIBLING claude-klabauter checkout
(`_engine_root.resolve_claude_klabauter_root_with_provenance`), place it on
`sys.path`, arm the lazy-ops channel, then call
`coordinator_core.bash_guards.dispatch.evaluate_payload_json`. None of that
applies here: this module IS inside the engine boundary, the chain is a
same-repo sibling import, and there is nothing on the other side of a
process boundary to resolve or arm.

Op contract (matches `hooks.preuse_write_dispatch`, this row's own sibling):
the PreToolUse payload (`tool_name`, `tool_input`, `session_id`, `cwd`,
`agent_id`, …) reaches this handler in either of the two shapes callers send —
wrapped as `params["payload"]` by both engine doors, flat by the cold chain —
and `_envelope.payload_of` reads both. That payload is the SAME shape
`evaluate_payload_json`'s raw-JSON-string parameter decodes to, so this
handler's only reshaping is one `json.dumps` re-serialisation (the chain's
own documented parse-once contract; mirrors
`coordinator_core.ops.warm_guard_evaluate`'s identical re-serialise step for
its own forwarded-event caller). Returns one `hookSpecificOutput` envelope
dict (deny, or whatever advisory/rewrite shape a guard in the chain
composed) — `no_advisory()` when the chain answers `None`. Never a list —
`collect_advisories=True` is never passed, matching `warm_guard_evaluate`'s
own negative-spec for the identical reason (no consumer of a multi-envelope
wire shape exists on this door).

`policy_file`/`resolution_class` — REUSED, NOT REDERIVED. `bash_guards.
dispatch.evaluate_payload_json` genuinely reads `policy_file` (unlike
`write_guards.engine.evaluate`'s accepted-but-unused `policy_path` — see
`preuse_write_dispatch`'s own docstring for that contrast), so this handler
cannot drop it the way that sibling did. Rather than re-deriving the
plugin-root-to-policy-path computation a second time, this module calls the
already-landed `coordinator_core.ops.warm_guard_evaluate` helpers of the
same name (`_policy_file_for`, `_engine_resolution_class`) — the warm door's
own per-call caller-fact / per-process server-fact pair, unchanged: a native
command-door caller carries no `plugin_root` header the way a forwarded HTTP
event does, but `_policy_file_for`'s own `resolve_caller_context` call
already ambient-probes when the payload key is absent (`CallerContext`'s own
documented fallback rung), so this reuse degrades correctly on this door
too rather than requiring a parallel resolver.

Graceful degradation: any failure inside `evaluate_payload_json` fails OPEN
(returns `no_advisory()`) rather than raising — a Bash/PowerShell call must
never be bricked by a guard-chain defect, mirroring the DoE dispatcher's own
fail-open philosophy for engine-resolution failure (which cannot occur here)
and `preuse_write_dispatch`'s identical contract for its own chain.

Negative-spec:
    Does NOT resolve a sibling engine checkout, place anything on
    `sys.path`, or arm a lazy-ops channel — there is nothing on the other
    side of a boundary to resolve; deleting that plumbing is the point of
    this port, not an omission.
    Does NOT re-derive `_policy_file_for`/`_engine_resolution_class` — see
    above; importing the warm door's own helpers keeps the caller-fact /
    server-fact asymmetry in exactly one place.
    Does NOT accept raw JSON text — callers on this door already hand a
    parsed params dict (the JSON-RPC contract), so this re-serialises once
    rather than exposing a raw-text entry point DoE's stdin-based script
    needed.

Spec backlink: docs/plans/2026-09-18-doe-holds-no-scripts.md § W4-C8
"""

from __future__ import annotations

import json

from coordinator_core.hooks._envelope import no_advisory, payload_of
from coordinator_core.ipc import register_op


@register_op("hooks.preuse_bash_dispatch")
async def _handler(params: dict, repo_root=None) -> dict:
    """PreToolUse(Bash|PowerShell) op: run the full bash-guard chain against
    this payload and return its verdict.

    `params` is the JSON-RPC params dict; the PreToolUse payload is
    `params["payload"]`, the shape `warm/hook_http.py :: payload_from_event`
    builds and which BOTH doors send (`hook_http.build_request` and
    `coordinator/bin/hook-run.py`). `repo_root` is accepted (the handler
    signature contract) and unused — the chain derives `cwd` from the payload
    itself, exactly as every cold invocation always has.

    This body previously read `params` AS the payload and serialised the whole
    envelope. The guard chain then found no `tool_input`, matched no rule, and
    returned ALLOW for every command through both doors — a silent fail-open on
    the one surface whose job is to deny, with output byte-identical for a
    payload that must be denied and one that must be allowed. Measured by
    `doe-claude-79` against `hooks.preuse_bash_dispatch`; `git worktree add`,
    `git add -A` and unscoped `git stash` all evaluate to `deny` when the chain
    is handed the payload rather than the envelope.

    `payload_of` reads either shape -- a flat payload is what the cold DoE
    guard chain passes, and a real PreToolUse payload carries no `payload` key,
    so the two are unambiguous. Refusing the flat one would turn a caller
    mismatch into a second fail-open rather than a verdict.
    """
    params = payload_of(params)
    if not params:
        return no_advisory()

    try:
        from coordinator_core.bash_guards.dispatch import evaluate_payload_json
        from coordinator_core.ops.warm_guard_evaluate import (
            _engine_resolution_class,
            _policy_file_for,
        )
    except Exception:
        return no_advisory()

    try:
        raw = json.dumps(params)
        out = evaluate_payload_json(
            raw,
            policy_file=_policy_file_for(params),
            resolution_class=_engine_resolution_class(),
        )
    except Exception:
        return no_advisory()  # any engine failure -> fail-open ALLOW

    if out is None:
        return no_advisory()
    if not isinstance(out, dict):
        return no_advisory()  # a List (collect_advisories) is never reachable here
    return out
