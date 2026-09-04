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
from typing import Any, Dict, Mapping, Optional, Tuple

from coordinator_core.warm.caller_context import resolve_caller_context

#: `hook_event_name` values whose verdict can BLOCK the operation. A missing guard on one of
#: these is a safety regression; a missing guard on any other event is a lost advisory. The
#: distinction drives `unreachable_response` and nothing else -- this module never decides
#: whether a blocking event may ride this transport, which is a cross-repo shape question.
#: Currently identical to `SERVED_EVENTS` below, coincidentally, not by construction -- the
#: two encode different facts (what's dispatch-critical vs. what this listener can serve)
#: and are expected to diverge as `SERVED_EVENTS` widens. Edit only the one you mean to.
BLOCKING_EVENTS = frozenset({"PreToolUse"})

#: The `hook_event_name` values this LISTENER can actually serve. A fact about our dispatch,
#: never about the transport: DoE's 2026-08-19 spike (`docs/research/spike-verdicts/
#: 2026-08-19-http-hook-transport.md`, verdict `viable`) established that the harness POSTs the
#: full event object -- `hook_event_name` included -- so every event below arrives complete and
#: is turned away here, not at the wire.
#:
#: WHY A SET RATHER THAN A DISPATCH TABLE. The event -> op mapping is 1:many and its source of
#: truth is DoE's `hooks.json`, where PostToolUse alone names seven ops. A table here would be a
#: second copy of somebody else's config, drifting silently. Widening this listener means
#: consuming that mapping, not transcribing it.
#: Currently identical to `BLOCKING_EVENTS` above, coincidentally -- see its docstring.
SERVED_EVENTS = frozenset({"PreToolUse"})


def route_for_event(event_name: Optional[str]) -> Optional[str]:
    """The op a posted event dispatches to, or None when this listener cannot serve it.

    Negative-spec: returning None is NOT a verdict and must never be answered as one. Before
    this existed, `supervisor.do_POST` read `hook_event_name` into a local and then framed
    every request with `DEFAULT_OP_NAME` regardless -- so a `SessionStart` POST was evaluated
    by the bash-guard chain, which examines `tool_name`/`tool_input` that a SessionStart event
    does not carry, and returned a confident no-objection about a question it was never asked.
    Silent and wrong beats loud and absent only if nobody is looking.
    """
    if event_name in SERVED_EVENTS:
        return DEFAULT_OP_NAME
    return None


def unserved_response(event_name: Optional[str]) -> Dict[str, Any]:
    """What to answer for an event this listener has no dispatch for.

    Shaped like `unreachable_response` and for the same reason -- the operator sees
    `systemMessage`, the model sees `additionalContext`, and neither reads as a guard that ran
    and passed. Distinct from it in cause: the engine is fine and reachable; this listener
    simply has no route for the event, which is a build gap rather than an outage, and the two
    must not be diagnosed as one another.
    """
    label = event_name or "an unnamed event"
    return _with_context(
        {
            **_envelope(event_name),
            "systemMessage": "coordinator: no warm route for %s -- hook did not run" % label,
            "suppressOutput": False,
        },
        "The coordinator warm listener has no dispatch for %s, so no coordinator hook ran "
        "for it. It did not pass -- it did not run." % label,
    )

#: The listener's hook endpoint, and the op a bare `/hook` POST routes to. Defined HERE
#: rather than in `supervisor` because `op_for_path` is the routing decision and the
#: transport merely applies it; `supervisor` re-exports both under its own names, which
#: are the ones `write_discovery` publishes and every existing caller already reads.
HOOK_PATH = "/hook"
DEFAULT_OP_NAME = "warm_guard.evaluate"

#: The op-name namespaces a hook registration may route to. NOT "any registered op": the hook
#: endpoint is reachable by anything that can open a loopback socket and present the engine
#: token, and a registration is a string in a config file a plugin update can rewrite. Holding
#: it to the hook namespaces means a rewritten registration can at worst reach a different
#: HOOK op, never `ceremony.*` or a mutating fleet op. Widen this only with a named reason.
ROUTABLE_OP_PREFIXES = ("hooks.", "session.", "warm_guard.")

#: WIDENED 2026-08-26 (C4), AND THE ORDER MATTERED. The comment above says
#: "reachable by anything that can open a loopback socket" -- that was true
#: when it was written and is not any more: `supervisor.parse_request` now
#: requires the boot cookie on every non-health request (`_cookie_is_valid`,
#: landed `34a0a556e`), so reaching this fence at all means holding a secret
#: only this user can read. The fence was the ONLY thing bounding blast
#: radius while the listener was unauthenticated, which is why it could not
#: move first and did not.
#:
#: What the prefixes could never express: an op CLI wants its READ ops served
#: warm, and read ops do not share a namespace -- they are spread across
#: every prefix. So the widening is by CLASS, not by more strings.
#: `authz.classification.classify` is the authority, its MUTATING default is
#: fail-closed, and an unclassified op raises `KeyError` which its own
#: docstring requires HTTP dispatch to treat as DENY. Both are honoured
#: below.
#:
#: STILL NEVER REACHABLE, AND THE SECOND ONE COST A TEST TO FIND: anything
#: MUTATING, and anything under `ceremony.*` regardless of its class. The
#: comment above bounds the blast radius of a REWRITTEN REGISTRATION -- a
#: confused-deputy threat, not a network one -- by naming `ceremony.*`
#: explicitly. Class alone does not honour that: four ceremony ops classify
#: COMPUTE_ONLY and a class-only widening quietly admitted them. The
#: credential answers "who is calling"; it does not answer "was this hook
#: client aimed somewhere it should not be", so the namespace bound survives
#: the credential landing and is kept as an explicit denial below.
#:
#: Widening past "authenticated reads, outside ceremony" needs its own named
#: reason and its own review.
DENIED_OP_PREFIXES = ("ceremony.",)

#: Keys an op result may set that Claude Code splices into the session rather than reading as
#: a verdict. `systemMessage` reaches the operator; `suppressOutput` is a display hint. Both
#: are read at the TOP LEVEL of the response body.
#:
#: `additionalContext` is NOT in this tuple, and that asymmetry is measured, not stylistic --
#: see `_with_context`.
PASSTHROUGH_RESULT_KEYS = ("systemMessage", "suppressOutput")

#: Events whose response the harness REJECTS if it carries `hookSpecificOutput` at all.
#:
#: `hookEventName` is validated against a closed enum, and not every event the harness will
#: DIAL is a member of it. `SessionEnd` is dialled, routes, and runs the op -- and then the
#: whole response fails validation on the echoed name, taking the op's `additionalContext`
#: with it. Measured by doe-claude-cd on harness 2.1.258, two-arm paired control against one
#: listener differing in exactly one field: echoing the name fails, omitting
#: `hookSpecificOutput` validates clean (DoE-claude
#: `docs/research/spike-verdicts/2026-09-02-harness-dials-posttooluse-and-sessionend-over-http.md`).
#:
#: NEGATIVE SPEC -- THIS IS A LIST OF MEASURED REJECTIONS, NOT A MODEL OF THE ENUM. Do not
#: "complete" it from the harness's published enum: an event absent from that enum today is
#: not evidence the harness rejects it, and an event present is not evidence it is dialled.
#: Both halves are measurements, taken per event, and only `SessionEnd` has been taken.
EVENTS_REJECTING_HOOK_SPECIFIC_OUTPUT = frozenset({"SessionEnd"})


def _envelope(event_name: Optional[str]) -> Dict[str, Any]:
    """The `hookSpecificOutput` wrapper, or nothing where the harness refuses one.

    `SessionEnd` is TERMINAL: no model turn follows it for context to be spliced into, at
    any nesting level. NEGATIVE SPEC -- no placement of `additionalContext` delivers on this
    event, nested or top-level; do not reintroduce one. See DoE-claude
    `docs/research/spike-verdicts/2026-09-02-harness-dials-posttooluse-and-sessionend-over-http.md`
    for the measurement.

    What this function removes is a validation error on every session close: `SessionEnd`
    is dialled and routes, and the whole response fails validation if it carries
    `hookSpecificOutput` at all -- see `EVENTS_REJECTING_HOOK_SPECIFIC_OUTPUT`.
    """
    if event_name in EVENTS_REJECTING_HOOK_SPECIFIC_OUTPUT:
        return {}
    return {"hookSpecificOutput": {"hookEventName": event_name}}


def _with_context(body: Dict[str, Any], context: Optional[str]) -> Dict[str, Any]:
    """Attach `additionalContext` where the harness actually reads it: NESTED inside
    `hookSpecificOutput`, never at the top level of the response body.

    MEASURED, not inferred. Claude-klabauter-0e probed harness 2.1.245 with each shape sent
    ALONE on the same event over the same transport, differing in nothing else:

        {"additionalContext": "<sentinel>"}                        -> model: ABSENT
        {"hookSpecificOutput": {"additionalContext": "<sentinel>"}} -> model echoed it

    NEGATIVE SPEC. Where `_envelope` returned no wrapper (`SessionEnd`), there is no
    delivering placement left -- top-level was measured to deliver nothing there too
    (`_envelope`) -- so context is dropped rather than written anywhere. Do not add a
    top-level fallback branch; that channel has no destination on any event this module
    has measured.
    """
    if context is None:
        return body
    if "hookSpecificOutput" not in body:
        return body
    hso = dict(body["hookSpecificOutput"])
    hso["additionalContext"] = context
    body["hookSpecificOutput"] = hso
    return body

#: The env keys guard evaluation actually consults. Forwarding the caller's WHOLE environ
#: would put arbitrary session secrets on the wire for every hook fire; forwarding nothing
#: deletes the override boundary. Forwarding the prefixes the guards read is the narrow
#: middle, and it is a prefix match rather than a fixed list because guards add override
#: keys without telling this module.
FORWARDED_ENV_PREFIXES = ("COORDINATOR_ALLOW_", "COORDINATOR_OVERRIDE_", "COORDINATOR_PROBE_", "COORDINATOR_SCOPE_")

#: Exact names the HEADER channel carries in addition to the prefixes above -- the env diet
#: of the ops that actually run over this transport, enumerated rather than pattern-matched.
#:
#: WHY A SECOND LIST RATHER THAN A WIDER PREFIX. The prefixes model a NAMESPACE the guards
#: own and extend without telling this module, so a prefix is the only workable allowlist for
#: them. These five are the opposite shape: they are OS/harness names this module does not
#: own, they will never grow by convention, and a prefix that admitted `HOME` would admit
#: every `HOME*` a session ever exports. Enumerating them is the narrower door, not the wider
#: one. Add a name here only when an op is measured reading it from `payload["env"]`.
#:
#: `hooks.plan_persistence_check` reads the first four; `hooks.nudge_autonomous_askuserquestion`
#: reads the fifth. Both ops' module docstrings named this list's absence as the reason their
#: env reads could not survive a `command`->`http` flip -- this closes that, and those
#: docstrings' "does not yet carry this" notes are stale as of this commit.
FORWARDED_ENV_NAMES = frozenset(
    {
        "CLAUDE_HOME",
        "HOME",
        "USERPROFILE",
        "CLAUDE_PROJECT_DIR",
        "COORDINATOR_AUTONOMOUS_ASK_OK",
    }
)


def _is_forwardable_name(name: str) -> bool:
    return name in FORWARDED_ENV_NAMES or any(
        name.startswith(p) for p in FORWARDED_ENV_PREFIXES
    )


OVERRIDE_CHANNEL_HEADER = "X-Coordinator-Env-Channel"
OVERRIDE_CANARY_HEADER = "X-Coordinator-Env-Canary"
OVERRIDE_CANARY_ENV = "COORDINATOR_PROBE_CANARY"
OVERRIDE_HEADER_PREFIX = "X-Coordinator-Env-"


def env_from_headers(
    headers: Mapping[str, str]
) -> Tuple[Dict[str, str], Optional[str]]:
    """The caller's forwardable environment, read off REGISTRATION HEADERS rather than the body.

    THE BODY HAS NO `env` KEY AND CANNOT BE MADE TO HAVE ONE. Measured n=2 on harness
    2.1.246 with two positive controls: a registration's `allowedEnvVars` enables `${...}`
    interpolation into `headers` and does nothing else, while the POST body carries
    `cwd, effort, hook_event_name, permission_mode, prompt_id, session_id, tool_input,
    tool_name, tool_use_id, transcript_path` and no `env` under any spelling -- exactly the
    key set DoE's 2026-08-19 spike recorded as the FULL verbatim event. Headers are the only
    channel that carries a caller-side value, so this is where the override boundary lives.
    See `docs/research/spike-verdicts/2026-08-25-allowedenvvars-populates-headers-not-the-post-body.md`.

    THAT KEY SET IS A CALLER SHAPE, NOT A TRANSPORT CEILING -- DO NOT CITE IT AS EVIDENCE
    A FIELD CANNOT REACH THE WIRE. The 2.1.246 measurement above was two MAIN-THREAD calls;
    it could not have distinguished "this transport drops the field" from "this caller has
    no field to send". Measured again by doe-claude-e7, 2026-08-29, harness 2.1.251, against
    a real `type: "http"` registration pointed at a recording sink in an isolated scratch
    settings session (the live registration was never touched): a dispatched SUBAGENT's
    body carries those same ten keys PLUS top-level `agent_id` and `agent_type`, both
    present and non-empty. `agent_id`/`agent_type` are documented as subagent-only fields
    (the vendored harness docs tie them to "When running as subagent" -- DoE-claude
    `state/reference/anthropic-docs/claude-code/hooks.md:201-203`, not vendored into THIS
    tree, which is why the claim is cited to the sibling rather than asserted locally);
    their absence on a main-thread call is a property of the CALLER, not the transport. A
    key set measured on one caller shape must never be read as proof that a field is absent
    on the wire for every caller shape -- re-measure against the actual caller shape in
    question before drawing that conclusion.

    SECOND-ORDER: `session_id` is IDENTICAL between the main-thread and subagent bodies.
    Subagent identity is reachable ONLY via `agent_id` -- never reconstruct it from
    `session_id`.

    CONFIRMED AGAIN BY THE SAME MEASUREMENT: `plugin_root` remains absent on http under any
    spelling, and the request carried no custom headers at all (`Accept, Accept-Encoding,
    Connection, Content-Length, Content-Type, Host, User-Agent`). This is why the forwarder
    computes `plugin_root` server-side -- it is not on the wire under any caller shape.

    Returns `(env, disarm_reason)`. A non-None `disarm_reason` means the channel is declared
    but not working, and the caller MUST refuse to report a verdict.

    NEGATIVE SPEC -- WHY A VETO MUST BE LOUD, AND WHY EMPTY ALONE CANNOT SAY SO. An
    `httpHookAllowedEnvVars` SETTING is a ceiling on the registration: when present it vetoes
    names the registration itself allowed, and a vetoed interpolation arrives as the EMPTY
    STRING rather than verbatim (finding 3,
    `docs/research/spike-verdicts/2026-08-25-http-hook-headers-expand-env-vars-but-not-path-placeholders.md`).
    An empty override header is therefore ambiguous between "the caller set nothing" and
    "a setting silently disarmed the channel" -- and the two differ in the PERMISSIVE
    direction, because the guard reads a missing override as "no override requested" and
    proceeds. Two headers disambiguate it and neither is optional:

      `X-Coordinator-Env-Channel` -- a STATIC LITERAL. Present iff the registration declares
      this channel at all. Absent means an old-style registration, which is not a fault: the
      result is `({}, None)`, today's behaviour exactly.

      `X-Coordinator-Env-Canary` -- `${COORDINATOR_PROBE_CANARY}`, a var the launcher always
      exports non-empty. Interpolated, so a setting-level veto empties it. Channel declared
      AND canary empty is a veto that has certainly eaten every other override header too,
      and returns a `disarm_reason` (one of two causes -- see THIRD DISARM CAUSE below).

    Do NOT "simplify" this to a single header. A lone literal cannot detect the veto (it is
    not interpolated, so it survives one); a lone canary cannot tell a veto from a
    registration that never declared the channel, and would fail every legacy registration
    closed. The pair is the mechanism.

    THIRD DISARM CAUSE -- A NAME THIS ENGINE DOES NOT CARRY. A header under this prefix is
    not ambient environment that happened to be present: someone wrote the header AND the
    matching `allowedEnvVars` entry, so its arrival is an explicit request. Silently dropping
    a name outside `FORWARDED_ENV_PREFIXES`/`FORWARDED_ENV_NAMES` therefore fails in the same
    PERMISSIVE direction as the canary veto -- the registration reads as correctly threaded,
    the op reads a missing override as "not requested", and nothing errors on either side.
    That is not a hypothetical: `docs/reference/warm-hook-migration.md` § Step 3 prescribed
    exactly such a registration for four names this filter dropped, which is how the gap was
    found. An unforwardable name is refused LOUDLY, like the veto, and for the same reason:
    a request this engine cannot honour must never read as one it honoured.

    Refusing rather than carrying is also why widening the filter was not the fix. The
    registration author and the engine must agree on the diet; the disagreement is the
    defect, and only one of the two can say so out loud.
    """
    lowered = {k.lower(): v for k, v in headers.items()}

    if not (lowered.get(OVERRIDE_CHANNEL_HEADER.lower()) or "").strip():
        return {}, None

    if not (lowered.get(OVERRIDE_CANARY_HEADER.lower()) or "").strip():
        return {}, (
            "override channel declared but %s interpolated empty -- an "
            "httpHookAllowedEnvVars setting is vetoing this registration's allowedEnvVars, "
            "so no caller override reached the guard" % OVERRIDE_CANARY_ENV
        )

    reserved = {OVERRIDE_CHANNEL_HEADER.lower(), OVERRIDE_CANARY_HEADER.lower()}
    prefix = OVERRIDE_HEADER_PREFIX.lower()
    candidates = {
        key[len(prefix):].upper(): value
        for key, value in lowered.items()
        if key.startswith(prefix) and key not in reserved and value != ""
    }
    refused = sorted(k for k in candidates if not _is_forwardable_name(k))
    if refused:
        return {}, (
            "override channel declared with %s, which this engine does not carry "
            "(see FORWARDED_ENV_PREFIXES / FORWARDED_ENV_NAMES) -- the registration asked "
            "for a value the op would never have seen" % ", ".join(refused)
        )
    # Review: overengineering-reviewer -- `refused` empty already proves every key in
    # `candidates` is forwardable; re-filtering the return line re-derives that proof.
    return dict(candidates), None


def forwardable_env(environ: Mapping[str, str]) -> Dict[str, str]:
    """The subset of a CALLER's environment that guard evaluation is entitled to see.

    Called against the environ carried ON THE EVENT, never against `os.environ` -- reading
    this process's own environment here is precisely the defect C14c re-keyed `_override`
    to make avoidable, and doing it inside the forwarder would reintroduce it one layer up
    where no existing test would notice.

    THIS IS THE AMBIENT PATH, AND IT FILTERS SILENTLY ON PURPOSE -- do not "make it
    consistent" with `env_from_headers`'s loud refusal. Its input is a WHOLE environment
    nobody enumerated, where the non-forwardable names are the overwhelming majority and
    their exclusion is the routine case, not a disagreement to report. The header channel is
    the opposite: every name there was written down twice by a human, so one this engine
    cannot carry is a mismatch worth stopping for. Same allowlist, different callers,
    different correct answer on a miss.

    It also does not consult `FORWARDED_ENV_NAMES`. Those five are OS/harness names present
    in essentially every ambient environment; admitting them here would forward a real
    session's `HOME` off any caller that passed its own environ, which is the invisible-
    disarm case one layer up. They are reachable only by explicit header.
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

    Every OTHER field of the event travels verbatim. The named-field subset this used to
    forward -- `tool_name`, `tool_input` and the four envelope keys -- is the PreToolUse
    guard's diet, and it silently starved every other event: `prompt` (UserPromptExpansion),
    `source` (SessionStart), `tool_response` (PostToolUse), `trigger` (PreCompact) and
    `agent_type` (SubagentStart) all reached the op as absent, so the op could not tell a
    missing field from an empty one. `env` is the ONLY key narrowed, because it is the only
    one that would put arbitrary session secrets on the wire.

    `plugin_root` RIDES THIS BODY AS A COMPUTED FIELD, never a forwarded env var. Measured
    (staff-eng finding 2, C1 dispatch brief): the harness's own posted event body carries no
    `plugin_root` under any spelling, so `payload_from_event`'s verbatim copy has nothing to
    forward for it. This function therefore COMPUTES it once, HERE, at forward time, via
    `warm.caller_context.resolve_caller_context` -- the shared accessor `bash_guards/
    dispatch.py` reads on the other end of this seam -- rather than leaving every downstream
    reader (`provision_report.assemble_contract_blocks_for_payload` today, the guard chain
    once C6/C7 wire it) to call `provision_report.resolve_plugin_root()`'s ambient probe
    independently. One computed value on the wire, not N independent re-resolutions that
    could in principle disagree.
    """
    raw_env = event.get("env")
    payload = {k: v for k, v in event.items() if k != "env"}
    payload["env"] = forwardable_env(raw_env) if isinstance(raw_env, Mapping) else {}
    for key in ("session_id", "cwd", "hook_event_name", "permission_mode", "tool_name", "tool_input"):
        payload.setdefault(key, None)
    payload["plugin_root"] = resolve_caller_context(payload).plugin_root
    return payload


def op_for_path(path: str) -> Optional[str]:
    """The op a hook registration's URL routes to, or None if the path is not routable.

    ROUTING IS PER-REGISTRATION, NOT PER-EVENT, and that is forced by the registrations
    themselves rather than chosen: `hooks.json` carries THREE SessionStart entries
    (`sessionstart-dispatch`, `sweep-boot`, `sessionstart-async-dispatch`), three
    UserPromptExpansion entries and three PostToolUse/Agent entries. `hook_event_name` cannot
    tell them apart, so a `hook_event_name -> op` map is not a smaller version of this
    function -- it is a shape that cannot express the live registration set at all.

    `/hook` with no suffix keeps routing to the guard op, so the arms already measured in
    `budget-manifest.json` § `_hook_seam_http_transport` remain the same request they were.
    """
    trimmed = (path or "").split("?", 1)[0].rstrip("/")
    if trimmed == HOOK_PATH:
        return DEFAULT_OP_NAME
    prefix = HOOK_PATH + "/"
    if not trimmed.startswith(prefix):
        return None
    op = trimmed[len(prefix):]
    if not op or "/" in op:
        return None
    if any(op.startswith(p) for p in DENIED_OP_PREFIXES):
        # Checked BEFORE the allow tests, never after: a denial that a later
        # allow can overturn is not a denial.
        return None
    if not any(op.startswith(p) for p in ROUTABLE_OP_PREFIXES):
        if not _is_compute_only(op):
            return None
    return op


def _is_compute_only(op: str) -> bool:
    """True iff `op` is classified COMPUTE_ONLY and may therefore be served
    over the authenticated HTTP transport.

    FAIL CLOSED TWICE OVER, deliberately. `classify` already defaults
    unknown-to-MUTATING at registration time, and it RAISES `KeyError` for an
    op absent from the map entirely -- its docstring requires HTTP dispatch
    to read that as DENY, never as COMPUTE_ONLY, because silently treating an
    unclassified op as a read is the actual privilege-escalation path. Both
    the MUTATING answer and the raise return False here.

    Imported inside the function, not at module scope: this module is on the
    hook hot path and `authz.classification` is a 278-entry map that a hook
    fire has no reason to pay for when the prefix check already answered.
    """
    try:
        from coordinator_core.authz.classification import OpClass, classify

        return classify(op) is OpClass.COMPUTE_ONLY
    except Exception:  # noqa: BLE001 -- see docstring; KeyError included
        # ONE CATCH, NOT TWO. `KeyError` is an `Exception`, so splitting them
        # read as two behaviours where there is one: everything that is not a
        # confirmed COMPUTE_ONLY answer denies.
        return False


def deny_response(event_name: str, reason: str) -> Dict[str, Any]:
    """A blocking refusal, in the shape Claude Code reads from the response body.

    `permissionDecisionReason` reaches the model, so it carries the operator-facing reason
    verbatim rather than a generic string -- see obligation 1 in the module docstring.

    THIS BUILDER DOES NOT GO THROUGH `_envelope`, DELIBERATELY. A deny IS the nested keys:
    drop the wrapper and the refusal becomes a 200 that permits. If a member of
    `EVENTS_REJECTING_HOOK_SPECIFIC_OUTPUT` ever becomes blocking, that event cannot be
    denied over this transport at all, and the registration must stay `command`; it must
    never be resolved by emitting a wrapper the harness rejects.

    THAT IS NOW ENFORCED IN `_decision_to_response`, NOT ASSERTED HERE. This docstring used
    to say the collision "cannot happen today" and stop there -- true, and prose-only:
    `_decision_to_response` called this builder for ANY deny verdict without checking the
    event. See the guard there for why an unrun-guard response is the right answer and an
    `assert` is not.
    """
    return {
        "hookSpecificOutput": {
            "hookEventName": event_name,
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }


def allow_response(event_name: str, result: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
    """The no-objection verdict, carrying whatever the op asked to inject.

    An explicit `"allow"` overrides the user's own permission settings on some events; a
    guard with no objection means "I do not object", not "grant this regardless of what the
    operator configured". Staying silent ON THE DECISION is the weaker and correct claim.

    Staying silent on everything else was a defect, not a matching piece of conservatism.
    Most registrations are not guards at all -- SessionStart and UserPromptExpansion earn
    their slot by injecting content the harness splices into the session -- and a response
    of nothing-but-`hookEventName` is indistinguishable from that work having happened.
    Over this transport those hooks would have returned 200 and injected nothing, at any
    listener availability, with no test and no availability sampling able to see it: the op
    ran, the round trip succeeded, and the output was dropped one layer above the op.
    """
    body: Dict[str, Any] = dict(_envelope(event_name))
    if not isinstance(result, Mapping):
        return body
    for key in PASSTHROUGH_RESULT_KEYS:
        if result.get(key) is not None:
            body[key] = result[key]
    extra = result.get("hookSpecificOutput")
    # An op that nested its own output on an event the harness refuses a wrapper for
    # (`SessionEnd`) has nowhere for it to go -- reinstating the wrapper fails the whole
    # response, and no placement of `additionalContext` delivers on a terminal event either
    # (`_envelope`, `_with_context`). Dropped, not reinstated, not lifted.
    if isinstance(extra, Mapping) and "hookSpecificOutput" in body:
        merged = dict(extra)
        merged["hookEventName"] = event_name
        merged.pop("permissionDecision", None)
        merged.pop("permissionDecisionReason", None)
        body["hookSpecificOutput"] = merged
    # An op that nests its own context has already put it where the harness reads it; one
    # that returns the plain documented key has not, and that is the shape the module
    # docstring tells op authors to use. Promote it rather than making every op know. An op
    # that set BOTH keeps its nested value: it is the more specific declaration of the two.
    if body.get("hookSpecificOutput", {}).get("additionalContext") is not None:
        return body
    return _with_context(body, result.get("additionalContext"))


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
    return _with_context(
        {
            **_envelope(event_name),
            "systemMessage": "coordinator: guard did not run (%s)" % detail,
            "suppressOutput": False,
        },
        "A coordinator guard for %s could not be evaluated (%s). "
        "It did not pass -- it did not run." % (event_name, detail),
    )


def is_blocking_event(event_name: Optional[str]) -> bool:
    """Whether an unrun guard on this event is a safety regression or a lost advisory."""
    return event_name in BLOCKING_EVENTS


def build_request(event: Mapping[str, Any], method: str, request_id: int = 1) -> bytes:
    """Frame a hook event as the JSON-RPC request `_serve_line` already understands.

    No `_engine_token` here: the transport places it from its header, so that both the
    HTTP and named-pipe paths present a token the same code judges. Adding one here would
    be a second scheme beside that one.

    The caller's identity IS stamped here, at the envelope's TOP LEVEL, as the one
    `_caller` object `warm/server.py :: _serve_line` pops (`msg.pop("_caller", None)`)
    and resolves through `caller_context.resolve_caller_context` -- the same shape and
    the same key `warm/client.py :: _try_warm_dispatch_inner` and `door.c` send on their
    own transports. Its fields ARE `warm.caller_context.CallerContext` serialised
    directly, so this leg carries the caller's identity SET (session id AND harness pid),
    not one field.

    Read off the posted event's own `session_id`, never off this process's environment:
    a resident server's `os.environ` names whoever spawned it, not the caller of this
    request. An unresolvable session id stays `None` INSIDE the object rather than
    suppressing it -- `resolve_caller_context` still resolves the caller's `pid` and
    `cwd`, which `harness_registry.self_record()` keys off, and the server-side override
    no-ops on a `None` session id exactly as it does for the other two transports. A
    fabricated value is never substituted.

    NOT a bare top-level `_session_id`: that key was retired with the alias when C1b
    widened both legs to `_caller`, and `_serve_line` no longer reads it. Stamping it
    here would drop this leg's identity on the floor while the envelope still looked
    correct to a probe that asserts on the key rather than on what dispatch pops.
    """
    request: Dict[str, Any] = {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": method,
        "params": {"payload": payload_from_event(event)},
    }
    caller_sid = event.get("session_id")
    caller = resolve_caller_context({"session_id": caller_sid} if caller_sid else None)
    request["_caller"] = {
        "plugin_root": caller.plugin_root,
        "cwd": caller.cwd,
        "session_id": caller.session_id,
        "agent_id": caller.agent_id,
        "pid": caller.pid,
    }
    return json.dumps(request).encode("utf-8")


def _decision_to_response(event_name: str, result: Mapping[str, Any]) -> Dict[str, Any]:
    """Turn a flat verdict mapping (`{}` for no-objection, or `{permissionDecision:
    "deny", permissionDecisionReason: ...}`) into the hook response body.

    Shared by both entries into this module's response shaping: `interpret_result`
    narrows a wire JSON-RPC `result` down to this same flat shape first, and
    `evaluate_cold` builds one directly from `_verdict_from_envelope` without ever
    putting it on a wire. One narrowing-to-response step, not two copies of it drifting
    apart as either caller changes.
    """
    decision = result.get("permissionDecision") or result.get("decision")
    if decision == "deny":
        reason = (
            result.get("permissionDecisionReason")
            or result.get("reason")
            or "denied by coordinator guard"
        )
        # A DENY THIS TRANSPORT CANNOT EXPRESS IS REPORTED AS UNRUN, NEVER EMITTED ANYWAY.
        # `deny_response` bypasses `_envelope` because a deny IS the nested keys; on an
        # event the harness refuses a wrapper for, emitting them fails validation and the
        # harness fails open -- so the one path whose whole job is to BLOCK would become a
        # silent no-op exactly when it fires. Unreachable today (`BLOCKING_EVENTS` is
        # `PreToolUse` alone, and no op on a wrapper-refusing event emits a deny), which is
        # precisely why it was prose-only until a reviewer asked what enforces it. An
        # `assert` would not do: `-O` strips it, and the answer to "cannot express this
        # verdict" is a loud unrun-guard response, not a crash mid-dispatch.
        if event_name in EVENTS_REJECTING_HOOK_SPECIFIC_OUTPUT:
            return unreachable_response(
                event_name,
                "a guard denied this %s operation, but the harness rejects the response "
                "shape a deny requires on this event -- the refusal could not be "
                "delivered" % event_name,
            )
        return deny_response(event_name, reason)

    return allow_response(event_name, result)


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

    return _decision_to_response(event_name, result)


def evaluate_cold(event: Mapping[str, Any]) -> Dict[str, Any]:
    """Evaluate a hook event's real guard verdict IN PROCESS, with no listener bound
    and no subprocess spawned -- the cold counterpart to the served `/hook` path above.

    PM ruling 1 (C3): this module already owns what the bytes mean for both HTTP-shaped
    transports while `http_listener.py`/`front_door.py` only move them, so the cold
    entry belongs here too. A caller holding a fired hook event and no reachable
    listener -- DoE's forwarder is the intended caller, via C7a's memo, never wired from
    this side -- gets this function instead of shelling out to the cold CLI: a
    long-lived process that already imported `coordinator_core` pays no interpreter
    start this way, where the CLI shape pays a full one on the box's worst day (see this
    chunk's dispatch brief for the measurement backing that claim).

    Runs the IDENTICAL chain the warm-side registered op (`coordinator_core.ops.
    warm_guard_evaluate._warm_guard_evaluate`) runs -- same `evaluate_payload_json` call,
    same per-call `policy_file`/`resolution_class` derivation, same envelope narrowing --
    so a payload evaluated here and a payload evaluated through a live listener produce
    the same verdict. Imports are local, matching this module's existing hot-path
    convention (see `_is_compute_only`): a caller that never goes cold never pays for
    `bash_guards.dispatch`'s or `ops.warm_guard_evaluate`'s import cost.

    Calls `warm.telemetry.record_degrade(kind="cold_run", ...)` (C2) on every invocation
    -- unconditionally, because reaching this function AT ALL already means "no
    reachable listener", which is the fact worth a durable row. A second row
    (`kind="cold_failed"`) follows it if the cold evaluation then collapses; see the
    never-raises paragraph below for why that is a distinct kind and not a re-use of
    this one. NOT a fallback: DR-347
    Ruling 3 forbids THIS module rerouting a served call to cold dispatch on failure
    (module docstring, negative-spec); this function is the opposite direction -- a
    caller that has ALREADY decided to go cold, asking for a real verdict instead of an
    unevaluated command, and telling the durable record so on its way.

    Negative-spec: an event this listener has no route for gets `unserved_response`, never
    a verdict. Going cold widens WHERE the guard chain can run, never WHAT it will answer
    about -- so this entry turns an unserveable event away exactly as `route_for_event`
    makes the served path do, and for the reason that docstring already records: the chain
    examines `tool_name`/`tool_input`, so an event that carries neither would otherwise come
    back a confident no-objection about a question it was never asked. A non-string name is
    unserveable by the same test and is reported without echoing the caller's value back.

    NEVER RAISES past the route check -- DR-402 rung 3, and the reason this function is
    written with a bare `except` where the rest of this module is not. A caller reaching
    here has ALREADY exhausted the warm listener; it has no third thing left to try, so an
    exception out of this function is not an error it can handle -- it is the caller's own
    unreachable branch re-entered from below. That is the mechanism observed on 2026-08-30
    (`state/bug-backlog/2026-08-30-the-bash-guard-forwarder-fails-closed-on-3488980e5fba.yaml`):
    an expensive evaluation surfaced as an `OSError` inside DoE's forwarder, which turned it
    into `no live engine backend reachable` and denied a command nothing had evaluated.

    On failure this answers `unreachable_response` -- the same loud, verdict-free shape
    `interpret_result` already returns for every other unverdictable outcome, which the
    harness reads as no objection while `systemMessage`/`additionalContext` keep the unrun
    guard visible in the transcript. The act proceeds; it does not proceed quietly.

    NEGATIVE-SPEC, so the breadth of that `except` is not later "tidied" into a narrow one:
    the failures worth catching here are open-ended BY CONSTRUCTION -- an import error from
    a half-published engine, a policy file that does not parse, an unstamped root, a bug in
    any guard in the chain. Enumerating them is exactly the move that leaves the
    unenumerated one denying the box. Narrowing the RETURN to a deny is forbidden for a
    different reason: a guard that could not run holds no verdict to report, and DR-402's
    premise is that these are performance and ergonomics instruments, not security
    controls. `KeyboardInterrupt`/`SystemExit` are deliberately NOT caught -- they are not
    guard failures, and a caller being shut down must not be told its guard degraded.
    """
    event_name = event.get("hook_event_name")
    if not isinstance(event_name, str):
        return unserved_response(None)
    if route_for_event(event_name) is None:
        return unserved_response(event_name)

    try:
        from coordinator_core.warm.telemetry import (
            KIND_COLD_FAILED,
            KIND_COLD_RUN,
            record_degrade,
        )

        record_degrade(kind=KIND_COLD_RUN, cause="no reachable warm listener")

        payload = payload_from_event(event)

        from coordinator_core.bash_guards.dispatch import evaluate_payload_json
        from coordinator_core.ops.warm_guard_evaluate import (
            _engine_resolution_class,
            _policy_file_for,
            _verdict_from_envelope,
        )
        from coordinator_core.session.core import session_identity_override

        # THE CALLER'S IDENTITY IS BOUND HERE OR IT IS LOST HERE. This rung is
        # the only one that evaluates the chain with no per-request scope around
        # it: the warm rung reaches the chain through `warm/server.py ::
        # _run_dispatch`, which opens `entry_seam.per_request_state(session_id=
        # caller.session_id)` from the `_caller` object `build_request` stamps.
        # This function has no such wrapper, so without this bind every guard
        # that asks `session.core.resolve_session_id()` gets the environ of
        # whichever process happens to host this call -- DoE's resident
        # forwarder, whose environ names the session that spawned it and no
        # other. That is not a degraded answer, it is a confidently wrong one:
        # `session/grant.py :: check_tier_u_grant` then reads a DIFFERENT
        # session's grant file, so a live grant reads as absent for its own
        # holder and, should the spawning session ever hold one in the caller's
        # repo, as present for everybody else. Reported cross-repo by
        # example-retrieval-repo-em, 2026-09-02; diagnosis in state/bug-backlog/
        # 2026-09-02-warm-server-environ-decides-every-callers-session-identity.yaml.
        #
        # Measured, and the reason this rung is not the rare case it reads as:
        # the box's forwarder had served 7988 of 7988 PreToolUse events here,
        # `forwarded_by_event: {}`, because the HTTP listener's discovery record
        # was absent. A fallback carrying 100% of traffic is the primary path.
        #
        # `session_identity_override` validates the value itself (UUID shape) and
        # no-ops on None, so a payload with no session id degrades to exactly
        # today's behaviour rather than fabricating an identity.
        sid = payload.get("session_id")
        with session_identity_override(sid if isinstance(sid, str) else None):
            out = evaluate_payload_json(
                json.dumps(payload),
                policy_file=_policy_file_for(payload),
                resolution_class=_engine_resolution_class(),
            )
        result = _verdict_from_envelope(out)
        return _decision_to_response(event_name, result)
    except Exception as exc:  # noqa: BLE001 -- breadth is the point; see docstring
        detail = "cold evaluation failed: %s: %s" % (type(exc).__name__, exc)
        try:
            from coordinator_core.warm.telemetry import (
                KIND_COLD_FAILED,
                record_degrade,
            )

            record_degrade(kind=KIND_COLD_FAILED, cause=detail)
        except Exception:  # noqa: BLE001 -- the instrument may not be the failure
            # Deliberately swallowed and deliberately NOT re-raised or upgraded to a
            # deny. `record_degrade` is best-effort past its own `kind` validation, but
            # the import above and the telemetry module itself are not covered by that
            # contract, and this rung is reached precisely when the engine is in a state
            # where imports fail. Losing the row costs accountability for one degrade;
            # letting it propagate costs the box its shell. The row is why rung 3 is not
            # silent, so this is the one place that trade runs the other way -- and the
            # response below is still loud in the transcript regardless.
            pass
        return unreachable_response(event_name, detail)
