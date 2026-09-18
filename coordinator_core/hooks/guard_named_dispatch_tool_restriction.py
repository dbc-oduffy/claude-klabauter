"""coordinator_core.hooks.guard_named_dispatch_tool_restriction — PreToolUse
(Agent) op: independently-invocable door onto the named Explore/Plan strip
decision.

Arrival note (W4-C8, docs/plans/2026-09-18-doe-holds-no-scripts.md): ported
from DoE-claude `coordinator/hooks/scripts/guard-named-dispatch-tool-
restriction.py`. That script's own header records it as DEREGISTERED from
`hooks.json`'s `Agent` matcher (2026-07-31, single-emitter fold-in) —
`enforce-agent-dispatch-mode.py` (this row's own sibling op,
`hooks.enforce_agent_dispatch_mode`) is now the sole `updatedInput` emitter
on that matcher and folds this guard's decision in as its own "Concern F",
calling the SAME shared pure computation
(`coordinator_core.hooks.support.named_dispatch_strip.
compute_named_dispatch_result`) this module also calls. DoE kept the
standalone script on disk, unwired from the live matcher, purely so its own
dedicated test suite keeps exercising the real decision logic via direct
invocation — this module plays the identical role here: a directly-
invocable `hooks.<name>` door onto the same shared computation, never
itself registered on the live `Agent` matcher's fan-in
(`hooks.preuse_agent_dispatch`, this row's own third sibling op, does NOT
call this module — it reaches the same decision only via `enforce_agent_
dispatch_mode`'s own fold-in, preserving the single-emitter invariant).

SHAPE — offer, not mistrust (unchanged from source): fires when
`subagent_type` is `Explore` or `Plan` AND `name` is present. Default action
strips `name` and lets the dispatch through via a full `tool_input`
replacement, with an `additionalContext` offer. FAIL-CLOSED for this
guard's OWN failure once it has determined the case is a named Explore/Plan
dispatch (an unrecognised `tool_input` key, or any other unhandled
exception while building the rewrite) — `compute_named_dispatch_result`'s
own documented fail-closed leg, unchanged; this wrapper adds no fail-open
catch around that specific decision the way it does around import/shape
failures, so a genuine "deny" result is never silently swallowed.

Op contract: `params` is the flat PreToolUse payload dict. Returns a
`rewrite_input` envelope (strip `name`, offer note) on the strip leg, a
`deny` envelope on the fail-closed leg, or `no_advisory()` for every ordinary
pass (not Agent, no dict `tool_input`, nothing to strip).

Negative-spec:
    Does NOT re-implement the strip/deny predicate — `compute_named_
    dispatch_result` is called verbatim, the same shared computation
    `enforce_agent_dispatch_mode`'s own Concern F calls.
    Does NOT register on `hooks.preuse_agent_dispatch`'s fan-in — see
    arrival note above; that would reintroduce the exact parallel-emitter
    race this fold-in closed.

Spec backlink: docs/plans/2026-09-18-doe-holds-no-scripts.md § W4-C8
"""

from __future__ import annotations

from coordinator_core.hooks._envelope import deny, no_advisory, rewrite_input
from coordinator_core.hooks.support.named_dispatch_strip import (
    compute_named_dispatch_result,
)
from coordinator_core.ipc import register_op


@register_op("hooks.guard_named_dispatch_tool_restriction")
async def _handler(params: dict, repo_root=None) -> dict:
    """PreToolUse(Agent) op: offer to strip `name` off a named Explore/Plan
    dispatch, denying only on this guard's own fail-closed leg."""
    if not isinstance(params, dict):
        return no_advisory()
    if params.get("tool_name") != "Agent":
        return no_advisory()

    tool_input = params.get("tool_input")
    if not isinstance(tool_input, dict):
        return no_advisory()

    try:
        result = compute_named_dispatch_result(tool_input)
    except Exception:
        return no_advisory()  # unexpected failure before any decision -> allow

    if result is None:
        return no_advisory()

    action, merged, message = result
    if action == "deny":
        return deny("PreToolUse", message)
    return rewrite_input("PreToolUse", merged, message)
