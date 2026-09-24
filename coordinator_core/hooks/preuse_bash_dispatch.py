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
(never raises, never denies) — a Bash/PowerShell call must never be bricked
by a guard-chain defect, mirroring the DoE dispatcher's own fail-open
philosophy for engine-resolution failure (which cannot occur here) and
`preuse_write_dispatch`'s identical contract for its own chain. Open does NOT
mean silent: a chain failure (e.g. `ipc.py`'s "Missing required routing key
... requires _origin_worktree" when a Bash call's cwd resolves outside every
registered worktree — state/bug-backlog/2026-09-23-pretooluse-bash-guard-
fails-to-evaluate-0abe3f44d9d8.yaml) is surfaced via `allow_advisory` rather
than swallowed into `no_advisory()`. The two are NOT interchangeable: a guard
that could not run and a guard that ran and had nothing to say are different
facts, and collapsing them let the guard silently stop firing on every
outside-a-worktree Bash call with no trace anywhere.

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
import sys

from coordinator_core.hooks._envelope import allow_advisory, no_advisory, payload_of
from coordinator_core.ipc import register_op


def _unevaluated(detail: str) -> dict:
    """A guard that did not run, made visibly distinct from one that ran and passed.

    `no_advisory()` and this envelope differ only in `additionalContext` — both
    ALLOW the call — but that field is the whole point: `no_advisory()` says
    nothing, which is what "ran clean" ALSO looks like, so a silenced guard read
    as a passing one. This names the failure instead, to both the model
    (`additionalContext`) and the operator (stderr), mirroring
    `coordinator_core.warm.hook_http.unreachable_response`'s wording for the
    transport-down case — this is that same fact ("did not run"), one layer
    in, for an in-process chain failure rather than an unreachable engine.
    """
    message = (
        f"A coordinator guard for PreToolUse could not be evaluated "
        f"(hooks.preuse_bash_dispatch: {detail}). It did not pass -- it did not run."
    )
    print(f"[coordinator] {message}", file=sys.stderr)
    return allow_advisory("PreToolUse", message)


@register_op("hooks.preuse_bash_dispatch")
def _handler(params: dict, repo_root=None) -> dict:
    """PreToolUse(Bash|PowerShell) op: run the full bash-guard chain against
    this payload and return its verdict.

    `params` is the JSON-RPC params dict; the PreToolUse payload is
    `params["payload"]`, the shape `warm/hook_http.py :: payload_from_event`
    builds and which BOTH doors send (`hook_http.build_request` and
    `coordinator/bin/hook-run.py`). `repo_root` is accepted (the handler
    signature contract) and unused — the chain derives `cwd` from the payload
    itself, exactly as every cold invocation always has.

    See `_envelope.payload_of` for why the two shapes must be normalised
    here rather than assumed: reading the wrong one fails open (silent
    ALLOW), never a visible error.

    NEGATIVE SPEC: never refuse the flat shape -- the cold DoE guard chain
    sends it, and must keep reaching the same verdict the doors do.
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
    except Exception as exc:
        return _unevaluated(f"{type(exc).__name__}: {exc}")

    try:
        raw = json.dumps(params)
        out = evaluate_payload_json(
            raw,
            policy_file=_policy_file_for(params),
            resolution_class=_engine_resolution_class(),
        )
    except Exception as exc:
        # Fail-open ALLOW, but visibly -- see `_unevaluated`. This is the site
        # ipc.py's "Missing required routing key ... requires _origin_worktree"
        # (a Bash call whose cwd resolves outside every registered worktree)
        # actually surfaces from; it used to be swallowed into `no_advisory()`
        # here, indistinguishable from a guard that ran clean.
        return _unevaluated(f"{type(exc).__name__}: {exc}")

    if out is None:
        return no_advisory()
    if not isinstance(out, dict):
        return no_advisory()  # a List (collect_advisories) is never reachable here
    return out
