"""Adapt a Claude Code hook event onto warm-engine op dispatch, and back.

The transport (`warm/http_listener.py`) moves bytes; this module decides what those bytes
MEAN. A hook event arrives as the documented JSON envelope -- `session_id`, `cwd`,
`hook_event_name`, `permission_mode`, plus per-event fields such as `tool_name` and
`tool_input` -- and Claude Code reads the result from the RESPONSE BODY rather than from an
exit code. That last part is the whole reason this module needs its own tests: on every
transport the guard chain has ever used, a deny travelled as `exit 2`. Here it travels as
`hookSpecificOutput.permissionDecision`, and a guard that cannot deny is not a guard.

THREE OBLIGATIONS, each with a test that fails loudly if it stops holding.

1. **A deny survives the round trip with its reason intact.** The reason reaches the model,
   so losing it turns an explained refusal into an unexplained one, which is the failure
   mode DR-277 exists around: an agent told "no" with no reason routes around the guard.

2. **The caller's environment travels on the event, not from this process.**
   `bash_guards/dispatch_checks.py :: _override` was re-keyed (C14c, `32d5224ed`) to PREFER
   a per-call `payload["env"]` over ambient `os.environ`, precisely so guard evaluation can
   move off a fresh child process. That re-key pinned the READER. This module is the WRITER,
   and until it exists the re-key buys nothing. Get this wrong in the obvious direction --
   forward nothing, let `_override` fall back -- and every existing test still passes while
   the per-session override boundary is deleted fleet-wide: a resident server's own environ
   becomes an invisible disarm across every session it serves, and a legitimate per-session
   override goes silently dead. `test_override_forwarded_not_ambient.py` is the pin.

3. **A listener that is down must not read as a guard that passed.** Claude Code's HTTP hook
   FAILS OPEN when nothing answers (measured by doe-claude-74 against a dead port). That is
   fine for an advisory and a safety regression for a blocking guard. This module cannot fix
   the harness's behaviour and does not try; what it owes is a truthful, machine-readable
   account of whether a guard actually ran, so the absence is detectable rather than silent.

NEGATIVE SPEC.

- **No fallback to cold dispatch from in here.** DR-347 Ruling 3 forbids a silent
  fall-through to claude-klabauter for live ops. This module reports; it does not reroute.
- **No second authorisation scheme.** The engine token is judged by `_serve_line`, the same
  code that judges a named-pipe caller. See `http_listener`'s docstring.
- **No per-fire stderr banner.** A resident server serving dozens of sessions cannot print
  per-request diagnostics; that is `_detect_hook_seam_drift`'s once-per-session precedent,
  not a per-call one.
"""

from __future__ import annotations

import json
from typing import Any, Dict, Mapping, Optional

#: `hook_event_name` values whose verdict can BLOCK the operation. A missing guard on one of
#: these is a safety regression; a missing guard on any other event is a lost advisory. The
#: distinction drives `unreachable_response` and nothing else -- this module never decides
#: whether a blocking event may ride this transport, which is a cross-repo shape question.
BLOCKING_EVENTS = frozenset({"PreToolUse"})

#: The env keys guard evaluation actually consults. Forwarding the caller's WHOLE environ
#: would put arbitrary session secrets on the wire for every hook fire; forwarding nothing
#: deletes the override boundary. Forwarding the prefixes the guards read is the narrow
#: middle, and it is a prefix match rather than a fixed list because guards add override
#: keys without telling this module.
FORWARDED_ENV_PREFIXES = ("COORDINATOR_ALLOW_", "COORDINATOR_OVERRIDE_", "COORDINATOR_PROBE_", "COORDINATOR_SCOPE_")


def forwardable_env(environ: Mapping[str, str]) -> Dict[str, str]:
    """The subset of a CALLER's environment that guard evaluation is entitled to see.

    Called against the environ carried ON THE EVENT, never against `os.environ` -- reading
    this process's own environment here is precisely the defect C14c re-keyed `_override`
    to make avoidable, and doing it inside the forwarder would reintroduce it one layer up
    where no existing test would notice.
    """
    return {
        k: v
        for k, v in environ.items()
        if any(k.startswith(p) for p in FORWARDED_ENV_PREFIXES)
    }


def payload_from_event(event: Mapping[str, Any]) -> Dict[str, Any]:
    """Build the per-call payload guard code reads, from the forwarded event alone.

    `env` is populated from `event["env"]` and NOT from `os.environ`. If the event carries
    no env, the result carries an EMPTY mapping rather than no key at all: an absent `env`
    makes `_override` fall back to ambient environment, which on a resident server is the
    server's own -- the invisible-disarm case. An empty mapping is a truthful "the caller
    set no overrides" and keeps the fallback from firing.
    """
    raw_env = event.get("env")
    env = forwardable_env(raw_env) if isinstance(raw_env, Mapping) else {}
    return {
        "env": env,
        "session_id": event.get("session_id"),
        "cwd": event.get("cwd"),
        "hook_event_name": event.get("hook_event_name"),
        "permission_mode": event.get("permission_mode"),
        "tool_name": event.get("tool_name"),
        "tool_input": event.get("tool_input"),
    }


def deny_response(event_name: str, reason: str) -> Dict[str, Any]:
    """A blocking refusal, in the shape Claude Code reads from the response body.

    `permissionDecisionReason` reaches the model, so it carries the operator-facing reason
    verbatim rather than a generic string -- see obligation 1 in the module docstring.
    """
    return {
        "hookSpecificOutput": {
            "hookEventName": event_name,
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }


def allow_response(event_name: str) -> Dict[str, Any]:
    """The no-objection verdict. Deliberately carries no `permissionDecision`.

    An explicit `"allow"` overrides the user's own permission settings on some events; a
    guard with no objection means "I do not object", not "grant this regardless of what the
    operator configured". Staying silent is the weaker and correct claim.
    """
    return {"hookSpecificOutput": {"hookEventName": event_name}}


def unreachable_response(event_name: str, detail: str) -> Dict[str, Any]:
    """What to answer when the guard could not be evaluated at all.

    NOT a deny: this module cannot distinguish "the engine is down" from "the operation is
    dangerous", and denying on infrastructure failure would take Write/Edit/Bash away from
    every session the moment the server hiccups -- the unrepairable class that disqualified
    the door from the registration slot.

    NOT a silent allow either. `systemMessage` surfaces to the operator and
    `additionalContext` reaches the model, so an unrun guard is visible in the transcript
    instead of being indistinguishable from a guard that ran and passed. That is this
    plan's anti-scope stated as code: *a guard that cannot run must never read as a guard
    that passed.*
    """
    return {
        "hookSpecificOutput": {"hookEventName": event_name},
        "systemMessage": "coordinator: guard did not run (%s)" % detail,
        "additionalContext": (
            "A coordinator guard for %s could not be evaluated (%s). "
            "It did not pass -- it did not run." % (event_name, detail)
        ),
        "suppressOutput": False,
    }


def is_blocking_event(event_name: Optional[str]) -> bool:
    """Whether an unrun guard on this event is a safety regression or a lost advisory."""
    return event_name in BLOCKING_EVENTS


def build_request(event: Mapping[str, Any], method: str, request_id: int = 1) -> bytes:
    """Frame a hook event as the JSON-RPC request `_serve_line` already understands.

    No `_engine_token` here: the transport places it from its header, so that both the
    HTTP and named-pipe paths present a token the same code judges. Adding one here would
    be a second scheme beside that one.
    """
    return json.dumps(
        {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method,
            "params": {"payload": payload_from_event(event)},
        }
    ).encode("utf-8")


def interpret_result(event_name: str, frame: bytes) -> Dict[str, Any]:
    """Turn one `_serve_line` response frame into the hook's response body.

    AN ERROR ENVELOPE IS NOT A VERDICT. `warm.client.try_warm_dispatch` counts any
    well-formed JSON-RPC response as a served hit, INCLUDING an error -- so a server that
    never registered the op returns METHOD_NOT_FOUND (-32601) and a naive caller reads it
    as a genuine "no objection". `warm/entry_seam.py :: try_warm_guard_dispatch` exists to
    make exactly this discrimination, and this function must make it too rather than
    assume a 200 means a guard ran.
    """
    try:
        obj = json.loads(frame.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return unreachable_response(event_name, "unparseable engine response")

    if not isinstance(obj, dict):
        return unreachable_response(event_name, "malformed engine response")

    if "error" in obj:
        err = obj.get("error") or {}
        code = err.get("code")
        return unreachable_response(event_name, "engine error %s" % code)

    result = obj.get("result")
    if not isinstance(result, dict):
        return unreachable_response(event_name, "engine returned no result")

    decision = result.get("permissionDecision") or result.get("decision")
    if decision == "deny":
        reason = (
            result.get("permissionDecisionReason")
            or result.get("reason")
            or "denied by coordinator guard"
        )
        return deny_response(event_name, reason)

    return allow_response(event_name)
