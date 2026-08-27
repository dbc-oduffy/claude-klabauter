"""coordinator_core.ops.warm_guard_evaluate — the registered warm-side counterpart of
``bash_guards.dispatch.evaluate_payload_json``.

Purpose: ``warm/entry_seam.py :: try_warm_guard_dispatch`` and ``warm/supervisor.py``'s
``/hook`` route both dispatch a JSON-RPC method named ``GUARD_OP_NAME`` (currently
``"warm_guard.evaluate"``) expecting a real guard verdict; before this module existed
that name resolved nowhere, so every call took the ``METHOD_NOT_FOUND`` fail-open path
and the warm shortcut bought nothing (state/handoffs/2026-08-23-the-warm-guard-op-gets-
registered.md). This module is the missing counterpart: it wraps the same guard chain
every cold hook process already runs, so a resident engine can answer the identical
question without spawning one.

THE ONE HAZARD THIS MODULE EXISTS TO AVOID (spec's own framing, restated in code): the
warm server is a single long-lived process shared by every session on the box. If this
handler ever consulted ITS OWN ``os.environ`` for a guard's ``COORDINATOR_OVERRIDE_*``/
``COORDINATOR_ALLOW_*`` check, two failures land at once — a legitimate per-session
override goes silently dead (not present in the server's environ), and whatever the
server happened to start under becomes a fleet-wide, invisible disarm. This module
therefore never reads ``os.environ`` itself, and does not need to: ``params["payload"]``
already carries a per-call ``env`` mapping (``warm/hook_http.py :: payload_from_event``
populates it from the FORWARDED event), and passing that payload straight through to
``evaluate_payload_json`` is what lets ``bash_guards/dispatch_checks.py :: _override``
prefer it over ambient environ, exactly as C14c re-keyed that function to do.

RESPONSE SHAPE — the contract ``warm/hook_http.py :: interpret_result`` reads:
    result.get("permissionDecision") or result.get("decision")
That reads TOP-LEVEL keys of the JSON-RPC ``result``, not a nested ``hookSpecificOutput``
envelope — but every guard in the chain composes ITS verdict nested exactly that way
(``{"hookSpecificOutput": {"permissionDecision": ..., ...}}``, the shape Claude Code's
own hook contract uses and ``evaluate_payload_json`` returns unmodified). This handler's
whole job, beyond invoking the chain, is that one unwrap-and-narrow step —
``_verdict_from_envelope`` below.

FAIL-OPEN, non-negotiable, and asymmetric on purpose (see module's own AC discussion):
    - A genuine deny survives with its reason intact.
    - Anything that is NOT a genuine deny (a no-objection, an advisory allow+context
      envelope, an "allow" a guard composed for its own reasons) collapses to the SAME
      no-objection shape: a dict carrying no decision key at all. Never a fabricated
      explicit "allow" — `hook_http.allow_response`'s own docstring is the reason: an
      explicit allow can override the operator's own permission settings on some events,
      while staying silent means only "I do not object".
    - A genuine internal failure (a malformed payload, an unexpected return shape from
      `evaluate_payload_json`) is raised, not swallowed — `ipc`'s dispatch core turns an
      uncaught handler exception into a JSON-RPC INTERNAL_ERROR envelope, which
      `interpret_result` already treats as "did not run", never as an allow. Fabricating
      a no-objection on an internal failure would be indistinguishable, on the wire, from
      a guard that ran and found nothing wrong — the exact hazard the spec calls out.

KNOWN ASYMMETRY BETWEEN THE TWO TRANSPORTS (recorded here, not fixed here — outside this
op's writes:, and DoE's problem to solve on their side):
    - Over HTTP, `hook_http.interpret_result` makes the same "error is not a verdict"
      discrimination this module relies on, so a caller on that transport is safe.
    - Over the DOOR transport, `warm/entry_seam.py :: try_warm_guard_dispatch` treats any
      well-formed JSON-RPC response as `hit=True`, INCLUDING an error response — only
      `METHOD_NOT_FOUND` is special-cased as a cold fall-through (see that function's own
      docstring, "THE TRAP THAT BLOCKED C14"). A door-side caller that receives a hit
      carrying `response["error"]` from THIS op must itself treat that as "the guard did
      not run" and fall back to the cold in-process guard — this module cannot enforce
      that from the server side; it can only ensure it never sends an error that is
      secretly a passing verdict.

Negative-spec (RAG-bait):
    This module registers no fallback to cold dispatch on any failure (DR-347 Ruling 3
    forbids it here — see this repo's copy of that ruling and `hook_http`'s own negative-
    spec section). It does not read `os.environ`, at any layer, for any guard decision.
    It does not translate a `List[Dict]` envelope (`evaluate_payload_json`'s
    ``collect_advisories=True`` shape) — this handler never passes that flag, so a `List`
    return is unreachable by construction; if one is ever observed, that is itself an
    internal failure this module refuses to guess an answer for (see
    `_verdict_from_envelope`).
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from coordinator_core.bash_guards.dispatch import evaluate_payload_json
from coordinator_core.ipc import register_op

#: The no-objection verdict this op ever answers with — an empty result dict. Mirrors
#: `hook_http.allow_response`'s own "carries no permissionDecision" contract one layer
#: down: this op speaks JSON-RPC `result`, not the hook response body, so it has no
#: `hookSpecificOutput` wrapper of its own to omit a key from — the absence of
#: `permissionDecision` IS the no-objection signal `interpret_result` reads.
NO_OBJECTION: Dict[str, Any] = {}


def _verdict_from_envelope(
    out: Union[Dict[str, Any], List[Dict[str, Any]], None],
) -> Dict[str, Any]:
    """Narrow one `evaluate_payload_json` result down to this op's flat response shape.

    `None` (every guard allowed silently) and any non-deny `Dict` (an advisory
    allow+context envelope, or a guard-composed explicit "allow") both collapse to
    `NO_OBJECTION` — see module docstring for why collapsing rather than forwarding
    advisory content is the documented choice, not a silent drop: `hook_http.
    interpret_result` does not read `additionalContext`/`systemMessage` off a `result`
    object today regardless (only `unreachable_response` — a DIFFERENT code path this
    function never builds — carries those keys), so forwarding them here would be dead
    weight on the wire, not a capability this op is withholding.

    A `List` (only reachable had this module passed `collect_advisories=True`, which it
    deliberately never does) or any other non-dict/non-None shape raises `TypeError`
    rather than guessing — see module docstring's negative-spec.
    """
    if out is None:
        return dict(NO_OBJECTION)
    if isinstance(out, list):
        raise TypeError(
            "warm_guard.evaluate: evaluate_payload_json returned a List envelope, "
            "which only occurs with collect_advisories=True; this op never passes "
            "that flag (see module docstring). Refusing to guess which entry, if "
            "any, is the real verdict rather than silently picking one."
        )
    if not isinstance(out, dict):
        raise TypeError(
            "warm_guard.evaluate: evaluate_payload_json returned %r (%s), expected "
            "a dict, a list, or None." % (out, type(out).__name__)
        )
    hso = out.get("hookSpecificOutput")
    if not isinstance(hso, dict):
        return dict(NO_OBJECTION)
    if hso.get("permissionDecision") != "deny":
        return dict(NO_OBJECTION)
    reason = hso.get("permissionDecisionReason") or "denied by coordinator guard"
    return {"permissionDecision": "deny", "permissionDecisionReason": reason}


@register_op("warm_guard.evaluate")
async def _warm_guard_evaluate(params: dict, repo_root: Optional[Path] = None) -> dict:
    """Run the full cold-hook guard chain against a forwarded event, warm.

    `params["payload"]` must be the dict `warm/hook_http.py :: payload_from_event`
    builds from the FIRED event — never this process's own ambient state. It is
    re-serialised to JSON exactly once (`evaluate_payload_json`'s own documented
    parse-once contract) and handed to the same function every cold `preuse-bash-
    dispatch.py` invocation calls, unblocking the event loop via `asyncio.to_thread`
    per this op's registration docstring contract (`evaluate_payload_json` is
    synchronous and can do real work — git probes, disk reads for advisory dedupe).

    `repo_root` is accepted (the handler signature contract) and deliberately unused:
    this op's `_OP_KEY_SCOPE` entry is `"none"` — it derives `cwd` from the payload
    itself, exactly as every cold invocation of this guard chain always has, and
    never needs the JSON-RPC envelope's `_origin_worktree`-derived key. Neither hook
    transport (`warm/hook_http.py :: build_request`, nor the yet-to-land door caller)
    sends that field for this op; requiring it would make the op unreachable rather
    than merely unused.

    A malformed or absent `payload` raises rather than silently falling back to an
    empty payload — see module docstring: an internal failure must surface as a JSON-
    RPC error, never as a guessed no-objection.
    """
    payload = params.get("payload")
    if not isinstance(payload, dict):
        raise TypeError(
            "warm_guard.evaluate: params[\"payload\"] must be a dict built by "
            "warm.hook_http.payload_from_event; got %r (%s)."
            % (payload, type(payload).__name__)
        )
    raw = json.dumps(payload)
    out = await asyncio.to_thread(evaluate_payload_json, raw)
    return _verdict_from_envelope(out)
