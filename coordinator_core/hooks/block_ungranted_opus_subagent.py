"""coordinator_core.hooks.block_ungranted_opus_subagent -- PreToolUse(Agent)
hard-deny guard enforcing the PM rule "no Opus-tier subagent fires unless it
is a named persona (frontmatter `model: opus`) or the PM has granted Opus
tier for this session."

THE HOLE THIS CLOSES. state/audits/2026-09-18-why-the-opus-subagent-guard-
did-not-fire.md: `enforce_agent_model_pin.check()` only defends a KNOWN
type's OWN declared pin against an override -- it returns `None` (out of
scope) the moment no `model`/`effort` param is passed at all, and it also
returns `None` for an unpinned type (`general-purpose`, `Explore`, `Plan`,
`claude`, ...) even when `model: "opus"` IS passed, because there is no pin
for that type to compare against. Measured live: five `general-purpose`
dispatches inherited an Opus EM's tier with zero `model` param and nothing
in the chain objected. This module is the missing THIRD question -- not
"is a param present" (out of scope for the sibling), not "does a KNOWN
type's OWN pin get respected" (the sibling's job) -- but "is the model this
dispatch will actually run under Opus-or-costlier, and if so, is that
JUSTIFIED (a named persona) or GRANTED (PM)?".

OWN CONCERN, OWN FILE -- same discipline `enforce_agent_model_pin`'s own
docstring states for its split from `block_unenumerated_agent_type`. A pin
COMPARISON (equal / cheaper / more-expensive against a declared value) and
an ABSOLUTE TIER GATE (is the resolved tier Opus-or-Fable at all, full
stop, regardless of any pin) are different questions with different
failure shapes and different owners of the "yes, this is fine" answer (a
human-authored persona pin vs. a PM-set grant). Folding this into the
sibling module would make ONE module answer two independently-changing
policies.

GATED TIERS -- OPUS AND FABLE, TREATED IDENTICALLY (PM spec amendment,
2026-09-18, superseding this module's own first-draft brief). `fable` is
NOT "unorderable and therefore denied on override" here the way
`enforce_agent_model_pin._axis_verdict` treats it (that is a RANK
question: fable has no defensible cost position relative to
haiku/sonnet/opus). This module asks a DIFFERENT, binary question -- "is
this tier gated at all" -- and the PM ruling is that Fable-tier is exactly
as gated as Opus-tier: an explicit `model: "fable"` on a type with no
`model: opus` pin denies, and an INHERITED Fable-tier parent dispatching a
generic (unpinned) type denies too. Do not "fix" `_is_gated_tier` by
removing `fable` on the theory that the sibling module's unorderable
stance should apply here -- it is a deliberately different axis.

EFFECTIVE MODEL RESOLUTION (three rungs, first match wins):
    1. `tool_input.model`, if present -- an explicit override always wins,
       `subagent_type == "fork"` never reaches resolution: a fork is exempt
       outright (PM ruling 2026-09-18 -- a fork is a new EM session with
       inherited backstory, and EMs run Opus, so it is a named exception).
    2. The type's own frontmatter `model:` pin (`resolve_model_pins()`),
       when rung 1 does not apply.
    3. INHERITED -- the parent session's own model. Harness builtins
       (`general-purpose`, `Explore`, `Plan`, `claude`, ...) have no
       frontmatter to pin, so they land here whenever no `model` param is
       passed.

PARENT-MODEL RESOLUTION -- NO PROCESS SPAWN, BOUNDED READ. The raw
PreToolUse(Agent) hook event carries no `model` field for the DISPATCHING
session (verified against this repo's own hook-payload fixtures,
`warm/hook_http.py :: payload_from_event`'s own docstring enumerating what
travels, and `docs/reference/DR-344...`'s brightline: no shell-out is
worth spawning to answer this). `payload.get("model")` is checked first as
a defensive no-op (costs nothing, covers a future payload shape this
module does not currently expect), then `transcript_tail.resolve_last_assistant_model` tail-reads
`payload["transcript_path"]` for the most recent `type == "assistant"`
record's `message.model` field -- same bounded chunked-read shape as
`hooks/subagent_arrival_check.py :: _read_last_nonempty_line` (8KiB
chunks, capped at 32 chunks / ~256KiB from EOF), generalized to scan up to
`transcript_tail`'s bounded window of trailing lines newest-first rather than only the
literal last line, because the last on-disk record is not reliably an
assistant turn (it may be a tool_result/user record).

FAIL-CLOSED WHEN THE PARENT MODEL CANNOT BE DETERMINED. No transcript, an
unreadable transcript, or a tail with no assistant record carrying a
`model` field resolves the INHERITED case to "gated" (Opus-tier) rather
than "ungated" -- this is a COST invariant (the whole point of the PM
rule), and treating "I don't know" as "assume it's cheap" is exactly the
silent-inherit hole this module exists to close. Mirrors
`enforce_agent_model_pin`'s own fail-closed stance on pin-resolution
failure, applied to the parent-model question instead of the pin question.

INHERITED IS REWRITTEN, NOT DENIED (PM ask, 2026-09-22). When the tier
resolved at rung 3 is gated (or unresolved, per the fail-closed rule
above), the dispatch is rewritten to `model: "sonnet"` via `updatedInput`
and proceeds, with one advisory line. No one chose that tier, so there is no
decision to gate. A deny here was always answered by an identical re-send
with `model: "sonnet"` added. Rungs 1 and 2 still DENY: an explicit gated
`model` param, or a gated non-`opus` frontmatter pin, is a choice the PM
gates, and it must never be quietly downgraded.

PERSONA EXEMPTION -- UNCONDITIONAL ON THE TYPE'S OWN PIN, NOT ON WHAT WAS
PASSED. If `resolve_model_pins()` shows this `subagent_type`'s own
frontmatter pins `model: opus`, this module returns `None` (out of scope)
immediately, before computing an effective model at all -- an Opus-pinned
persona (e.g. an eng-director/reviewer type) is TRUSTED for whatever it is
dispatched as; the composed sibling `enforce_agent_model_pin` already
governs any attempt to run it under a passed value that isn't equal-to or
cheaper-than its own pin (a passed `model: "fable"` against an `opus` pin
is `enforce_agent_model_pin`'s own "unorderable -> deny" leg, upstream of
this module in the composed seam -- see `block_unenumerated_agent_type`'s
own "COMPOSITION" docstring for the order). This module never re-derives
that comparison; it only asks "does this type's OWN declaration already
carry the Opus invariant", which is a fact independent of the current
call's passed value.

GRANT -- `COORDINATOR_OVERRIDE_AGENT_MODEL_GUARD`, non-empty in the hook
process's OWN environment to bypass, read INLINE inside `check()` (never
hoisted to module scope, matching `enforce_agent_model_pin`'s own
F2-discipline convention). Name and convention mirror DoE-claude's shipped
`BLOCK-WORKFLOW-UNMODELED-AGENT` guard
(`hooks/scripts/block-workflow-unmodeled-agent.py`, override
`COORDINATOR_OVERRIDE_WORKFLOW_MODEL_GUARD=1`) rather than inventing a new
key shape -- PM ruling, 2026-09-18: reuse the sibling domain's own
"`COORDINATOR_OVERRIDE_<DOMAIN>_MODEL_GUARD`" convention rather than a
bespoke name. ENV, DELIBERATELY NOT A PROMPT MARKER, for the identical
reason `enforce_agent_model_pin`'s own "ESCAPE HATCH" section states: an
env var is pre-launch, human-operator-only, reachable only by whoever can
set process environment BEFORE the harness/hook process launches -- never
by the dispatching agent this guard exists to bound, which could otherwise
just append an override line to its own next prompt.

DENY MESSAGE -- register per docs/wiki/guard-messaging.md § Register: one
fact (which model this dispatch resolves to, and why no exemption
applies), one terse alternative (pass `model: "sonnet"` or `"haiku"`, or
dispatch `coordinator:executor` for implementation work). Never spells out
the grant's env-var name, path, or any other artifact of the bypass
mechanism (B6) -- it states only that a PM grant is the thing that would
change the outcome, which is the fact the reader needs to know a human
operator (not the reader) can act on, not the mechanism itself.

INPUT TRUST. `subagent_type`/`model` read from `tool_input` ONLY; the
parent model is read from `payload["transcript_path"]`, a harness-supplied
fact, never from any agent-writable file. Matches the sibling modules'
own "INPUT TRUST" notes.

Two entrypoints, matching the sibling modules' own convention:
    check(payload) -- pure function, `Dict[str, Any] -> Optional[Dict]`.
    main()          -- stdin-JSON / stdout-JSON / exit-0 standalone script,
        unused by the composed path (see `block_unenumerated_agent_type`'s
        own "COMPOSITION" docstring for why registration stays DoE-side
        and no new hooks.json entry is needed).

Spec backlink: state/audits/2026-09-18-why-the-opus-subagent-guard-did-not-fire.md
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any, Dict, Optional

from coordinator_core._hook_envelope import deny, rewrite_input
from coordinator_core.hooks.block_unenumerated_agent_type import resolve_model_pins
from coordinator_core.hooks.enforce_agent_model_pin import _clean_str
from coordinator_core.transcript_tail import resolve_last_assistant_model

CLASS = "hard-deny"
MATCHERS = ("Agent",)

#: Grant -- see module docstring "GRANT". Read inline at `check()` call time
#: only, never hoisted to module scope.
_OVERRIDE_ENV = "COORDINATOR_OVERRIDE_AGENT_MODEL_GUARD"

#: `fork` is exempt -- see module docstring "EFFECTIVE MODEL RESOLUTION"
#: rung 1. Mirrors `enforce_agent_model_pin._FORK_TYPE`.
_FORK_TYPE = "fork"

#: Gated-tier substrings, case-insensitive -- see module docstring "GATED
#: TIERS". Deliberately includes `fable` alongside `opus`; do not remove
#: it by analogy with the sibling module's RANK-ordering stance, which
#: answers a different question (see docstring).
_GATED_TIER_TOKENS = ("opus", "fable")

#: What a gated INHERITED dispatch is rewritten to -- see module docstring
#: "INHERITED IS REWRITTEN, NOT DENIED".
_INHERITED_REWRITE_MODEL = "sonnet"

def _is_gated_tier(model_id: Optional[str]) -> bool:
    """True iff `model_id` names an Opus- or Fable-tier model (see module
    docstring "GATED TIERS"). Never raises; a non-string/empty input is
    "not gated" by definition (nothing to classify), which is safe here
    because every call site that reaches this function on a genuinely
    unresolved value already substitutes a fail-closed literal before
    calling it -- this function itself never fails open on unresolved
    input, it simply is never asked to classify one.
    """
    if not isinstance(model_id, str) or not model_id.strip():
        return False
    lowered = model_id.lower()
    return any(token in lowered for token in _GATED_TIER_TOKENS)


def _deny_reason(subagent_type: str, resolved_model: str, note: str) -> str:
    return (
        f"AGENT DISPATCH BLOCKED: subagent_type={subagent_type!r} resolves to "
        f"{resolved_model!r} ({note}, no persona pin, no PM grant). "
        "Pass model: \"sonnet\"/\"haiku\", or dispatch coordinator:executor."
    )


def check(payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Evaluate the Opus/Fable-tier persona-or-grant gate against a
    `PreToolUse(Agent)` payload. See module docstring for the full rule.
    """
    if (payload.get("tool_name") or "") not in MATCHERS:
        return None

    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        tool_input = {}

    subagent_type = _clean_str(tool_input.get("subagent_type"))
    if subagent_type is None:
        return None

    if os.environ.get(_OVERRIDE_ENV):
        return None

    if subagent_type == _FORK_TYPE:
        # PM ruling 2026-09-18: a fork is a new EM session carrying inherited
        # backstory, and EMs run Opus -- a named exception, never gated.
        return None

    pins, _error_reason = resolve_model_pins()
    if pins is None:
        # An unreadable roster is not grounds to deny a Sonnet dispatch; the
        # enumeration leg owns that defect. Proceed as if nothing is pinned,
        # which can only ever gate MORE, never exempt a type.
        pins = {}

    entry = pins.get(subagent_type) or {}
    pin_model = entry.get("model")

    if pin_model == "opus":
        # Named persona -- see module docstring "PERSONA EXEMPTION".
        return None

    passed_model = _clean_str(tool_input.get("model"))

    effective_model: Optional[str]
    if passed_model is not None:
        effective_model = passed_model
        note = "explicit model param"
    elif pin_model is not None:
        effective_model = pin_model
        note = "type's own frontmatter pin"
    else:
        effective_model = None
        note = "inherited -- no model param, no frontmatter pin"

    if effective_model is None:
        parent_model = _clean_str(payload.get("model")) or resolve_last_assistant_model(
            payload.get("transcript_path")
        )
        if parent_model is None:
            resolved_model = "<unresolved>"
            gated = True  # fail-closed -- see module docstring.
            note = note + "; parent model unresolved, fail-closed to gated tier"
        else:
            resolved_model = parent_model
            gated = _is_gated_tier(parent_model)
    else:
        resolved_model = effective_model
        gated = _is_gated_tier(effective_model)

    if not gated:
        return None

    if effective_model is None:
        # Nobody chose the tier -- it leaked in from the parent (or could not
        # be read). A rewrite has no decision to override, and unlike a deny it
        # is not answered by a verbatim re-send, so it closes the hole firmer.
        return rewrite_input(
            "PreToolUse",
            {**tool_input, "model": _INHERITED_REWRITE_MODEL},
            f"MODEL SWITCHED: {subagent_type!r} runs as {_INHERITED_REWRITE_MODEL}, not "
            f"the inherited {resolved_model!r} -- {_INHERITED_REWRITE_MODEL} fits most "
            f"agent work. For Opus-grade judgment, dispatch a named persona.",
        )

    return deny("PreToolUse", _deny_reason(subagent_type, resolved_model, note))


def main() -> int:
    """Standalone stdin-JSON / stdout-JSON / exit-0 entrypoint -- unused by
    the composed path (see module docstring "Two entrypoints"), kept for
    parity with the sibling modules' own calling convention.
    """
    try:
        raw = sys.stdin.read()
    except Exception:
        raw = ""
    if not raw:
        return 0
    try:
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            payload = {}
    except Exception:
        payload = {}

    envelope = check(payload)
    if envelope:
        sys.stdout.write(json.dumps(envelope, ensure_ascii=False, separators=(",", ":")))
        sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
