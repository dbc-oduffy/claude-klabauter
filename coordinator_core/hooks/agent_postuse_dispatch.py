"""
coordinator_core.hooks.agent_postuse_dispatch — PostToolUse(Agent) fan-in dispatcher op.

Purpose: fold the two ENGINE-side `PostToolUse` hooks whose matcher is exactly
`Agent` — the audit-log append and the dispatched-agent bookkeeping — into a
single op, so DoE's `hooks.json` carries one registration where it carries two.
Each registration is a fresh interpreter start (~27ms spawn floor plus its own
import), and both of these already reach this engine; the second process buys
nothing but the spawn.

WHY THIS OP EXISTS BESIDE `postuse_advisory_dispatch` RATHER THAN INSIDE IT.
That op is the obvious host — it already folds four checks and its matcher
(`Write|Edit|MultiEdit|NotebookEdit|Agent`) is a superset of this one. Folding
into it is wrong for a reason that is a registry fact, not a preference:

    op_scopes.py:  hooks.postuse_advisory_dispatch  -> "none"
                   hooks.agent_completion_log       -> "common_dir"
                   hooks.track_dispatched_agents    -> "common_dir"

A fan-in takes the UNION of its members' scope and class. Folding these two into
the advisory dispatcher widens that registration from `none` to `common_dir` on
every `Write`/`Edit`/`MultiEdit`/`NotebookEdit` fire — the highest-traffic
PostToolUse path on the box — to serve the `Agent` leg alone. The same argument
runs on matcher cost: Write/Edit fires dominate Agent fires, so folding in makes
both legs evaluate a `tool_name` gate on the common path to save a spawn on the
rare one. Keeping the classes apart lets each registration keep its own scope.

WHAT IS DELIBERATELY NOT FOLDED HERE.

  - `hooks.cater_subagent_start`. `track-dispatched-agents.py` is registered on
    TWO events; that op is its `SubagentStart` leg, a separate registration on a
    separate event. Only the `PostToolUse|Agent` leg folds.
  - `runtime-tripwire-em-check.py`, the third `Agent`-matcher registration. Its
    two engine ops (`hooks.subagent_arrival_check`,
    `hooks.subagent_zero_tool_use_surface`) return DOMAIN results — an
    `_envelope(state, agent_id, path, reason)` and a store read — not hook
    envelopes, and the stub composes the advisory prose itself across four text
    builders. That prose is doctrine-plane and has no op behind it, so folding
    it is a port across the DR-047 boundary rather than a composition. It stays
    its own registration until that port is funded.

THE FOLD IS ADDITIVE, NOT A MIGRATION. Both legs stay registered under their own
op names and keep their own direct callers; nothing moves. This module calls
their public `run` entry points and merges the results. That mirrors
`postuse_advisory_dispatch`'s own fold of `nudge_unauthorized_handoff`, which
likewise left the folded op registered for direct callers.

Merge contract:
    N of 2 emit advisory text -> post_advisory("\\n\\n".join, in
                                 completion-log / dispatch-tracking order)
    none emit                 -> no_advisory()

Both legs are write ops that return `no_advisory()` today, so the merge is
`no_advisory()` in practice. The merge is written for text anyway rather than
hardcoding an empty return: a leg that grows an advisory later must not need
this module edited to be heard, which is exactly the silent-drop shape
`_EAGER_HOOK_MODULES` exists to close off one level up.

PAYLOAD FLATTENING — engine-side, per-op, not a caller-side stub.
The HTTP hook transport forwards the harness's raw, nested `PostToolUse`
payload verbatim (`{session_id, tool_name, tool_input: {...},
tool_response: {...}}`) — it has no stub to pre-flatten it, unlike the
`command`-registration transport whose shell stub already extracts the flat
scalars each leg wants. `_flatten_hook_payload` derives those flat scalars
HERE, so both transports can reach this op with the same effect. It does not
widen either leg's accepted shape — a payload it cannot interpret raises
`CallerFacingValidationError`, preserving the `-32602` a malformed payload
already got before this adapter existed.

PER-OP, NOT A SHARED `hooks.*` BOUNDARY. `docs/decisions/DR-foreign-op-
registration-seam.md` (C3 of this op's own plan) settled the adjacent
question — a foreign op registers by declared import into `OP_MODULE_MAP`,
one entry per op key, never through a shared registration boundary that
would also be the natural home for a shared payload adapter. With no such
boundary, flattening lives beside the one op that needs it; a second hook
op with the same nested-payload shape gets its own adapter, not a shared one
inferred from this module.

Negative-spec:
    - NO handler-level `tool_name` gate. The registration's matcher is exactly
      `Agent`; re-checking it here would be a second copy of somebody else's
      config, drifting silently — the same reason `hook_http.SERVED_EVENTS` is a
      set rather than a dispatch table.
    - NO leg may fail the other. Each is awaited with its exception captured, so
      a raising audit-log write cannot suppress the bookkeeping write. A fan-in
      that lets one member take down its siblings is strictly worse than the two
      separate processes it replaces, because the failure is no longer isolated
      by the process boundary.
    - DOES NOT re-implement either leg. If a behaviour question is asked of this
      module, the answer is in the leg's own module.
    - DOES NOT widen the op to accept an arbitrary shape. `_flatten_hook_payload`
      raises `CallerFacingValidationError` (-> `-32602`) on a payload it cannot
      derive flat scalars from; it never silently degrades to an empty flat dict.
"""

from __future__ import annotations

import asyncio
import sys

from coordinator_core.ipc import CallerFacingValidationError, register_op
from coordinator_core.hooks._envelope import no_advisory, post_advisory
from coordinator_core.hooks import agent_completion_log, track_dispatched_agents


#: Generator-provenance declaration: this op writes nothing itself. Its legs
#: write only inside <git_common_dir>/coordinator-sessions/ — see their own
#: GENERATES declarations.
GENERATES: list = []


#: The folded legs, in merge order. A tuple of (label, callable) rather than a
#: bare list of callables so a leg that raises can name itself in the breadcrumb
#: below without the caller inferring it from a traceback frame.
_LEGS = (
    ("agent_completion_log", agent_completion_log.run),
    ("track_dispatched_agents", track_dispatched_agents.run),
)


def _flatten_hook_payload(params: dict) -> dict:
    """Derive the legs' flat-scalar fields from a raw, nested `PostToolUse(Agent)` payload.

    The flat-union shape both legs already read via `_payload.field()` —
    `description`, `subagent_type`, `name`, `dispatched_agent_id`,
    `dispatched_agent_id_snake`, `dispatched_model`, plus whatever top-level
    scalars a caller already sends (`session_id`) — is left ALONE when no
    nested `tool_input`/`tool_response` object is present: that is today's
    caller-side-stub shape (the `command` transport) and must keep working
    unchanged.

    When either nested key is present, this derives:
        description                -> tool_input.description
        subagent_type               -> tool_input.subagent_type
        name                        -> tool_input.name
        dispatched_agent_id         -> tool_response.agentId
        dispatched_agent_id_snake   -> tool_response.agent_id
        dispatched_model            -> tool_response.resolvedModel
                                        or tool_response.model
                                        or tool_input.model

    merged OVER a copy of the original params, so a top-level scalar the
    caller already sent flat (`session_id`) survives the merge untouched.

    Raises:
        CallerFacingValidationError: `params` is not a dict, or a present
        `tool_input`/`tool_response` key is not itself an object -- a shape
        this adapter cannot derive flat scalars from. This is the "genuinely
        malformed payload" the -32602 contract must still refuse; widening
        the handler to accept it silently is the one thing this adapter must
        not do (see module Negative-spec).
    """
    if not isinstance(params, dict):
        raise CallerFacingValidationError(
            "hooks.agent_postuse_dispatch: params must be an object, got "
            f"{type(params).__name__}"
        )

    if "tool_input" not in params and "tool_response" not in params:
        # Already the flat-union shape this op has always accepted -- pass
        # through unchanged, no caller is broken by this adapter existing.
        return params

    # `or {}` would defeat the refusal below for every FALSY non-object --
    # `[]`, `""`, `0` would each become `{}` and validate clean, so the guard
    # would fire only on truthy junk like `[1, 2]`. Default the ABSENT key
    # only, and let every present value reach the isinstance check as it came.
    tool_input = params.get("tool_input", {})
    tool_response = params.get("tool_response", {})
    if tool_input is None:
        tool_input = {}
    if tool_response is None:
        tool_response = {}
    if not isinstance(tool_input, dict) or not isinstance(tool_response, dict):
        raise CallerFacingValidationError(
            "hooks.agent_postuse_dispatch: 'tool_input'/'tool_response' must be "
            "objects when present, got "
            f"tool_input={type(tool_input).__name__} tool_response={type(tool_response).__name__}"
        )

    flat = dict(params)
    flat["description"] = tool_input.get("description")
    flat["subagent_type"] = tool_input.get("subagent_type")
    flat["name"] = tool_input.get("name")
    flat["dispatched_agent_id"] = tool_response.get("agentId")
    flat["dispatched_agent_id_snake"] = tool_response.get("agent_id")
    flat["dispatched_model"] = (
        tool_response.get("resolvedModel")
        or tool_response.get("model")
        or tool_input.get("model")
    )
    return flat


def _advisory_text(result) -> str:
    """The advisory prose carried by a leg's return envelope, or "" when it carries none.

    A leg returns one of the `_hook_envelope` shapes. `no_advisory()` is the
    empty dict, so `.get` chains to "" without a type check; any prose-carrying
    shape nests its text at `hookSpecificOutput.additionalContext`.

    Returns "" for a non-dict — a leg that returned something unexpected has a
    defect in its own module, and swallowing it HERE as "no advisory" is the
    correct disposition for a bookkeeping fan-in: the alternative is one leg's
    shape bug suppressing its sibling's write.
    """
    if not isinstance(result, dict):
        return ""
    hso = result.get("hookSpecificOutput")
    if not isinstance(hso, dict):
        return ""
    return str(hso.get("additionalContext") or "")


@register_op("hooks.agent_postuse_dispatch")
async def _handler(params: dict, repo_root=None) -> dict:
    """PostToolUse(Agent) fan-in: the audit-log append plus the dispatched-agent bookkeeping.

    Both legs run concurrently and both are write ops — the product is their
    on-disk side-effects, and the return is `no_advisory()` unless a leg grows
    prose (see the module docstring's merge contract).

    Inputs are the union of the legs' own flat-scalar fields, either sent
    flat already (the `command`-transport caller-side stub) or derived HERE
    from a raw nested `PostToolUse` payload by `_flatten_hook_payload` (the
    HTTP transport, which has no stub). Either way the legs themselves still
    read none of this module's logic — see `agent_completion_log._handler`
    and `track_dispatched_agents._handler` for the field lists and defaults.

    `_flatten_hook_payload` runs OUTSIDE the `asyncio.gather` below and is not
    caught by its `return_exceptions=True` — a payload this adapter cannot
    derive flat scalars from raises `CallerFacingValidationError` straight
    into the caller as `-32602`, the same disposition a malformed payload
    already got before this adapter existed.

    A leg that raises is logged to stderr and treated as having emitted no
    advisory; its sibling still runs and still returns. `asyncio.gather` with
    `return_exceptions=True` is what buys that — without it the first raising
    leg cancels the merge and the second leg's write is lost, which the two
    separate processes this op replaces would never have done.
    """
    flat_params = _flatten_hook_payload(params)

    results = await asyncio.gather(
        *(leg(flat_params, repo_root) for _label, leg in _LEGS),
        return_exceptions=True,
    )

    texts = []
    for (label, _leg), result in zip(_LEGS, results):
        if isinstance(result, BaseException):
            print(
                "agent_postuse_dispatch: leg=%s raised %s: %s — its sibling legs are "
                "unaffected" % (label, type(result).__name__, result),
                file=sys.stderr,
            )
            continue
        text = _advisory_text(result)
        if text:
            texts.append(text)

    if not texts:
        return no_advisory()
    return post_advisory("\n\n".join(texts))
