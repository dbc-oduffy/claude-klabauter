"""coordinator_core.hooks.enforce_agent_model_pin -- PreToolUse(Agent)
guard enforcing a dispatched agent definition's `model:`/`effort:`
frontmatter pin against the Agent tool's own `model:`/`effort:` params.

THE HOLE THIS CLOSES. The harness Agent tool's `model:`/`effort:` params
take precedence over an agent definition's frontmatter pin, SILENTLY --
there is no counter, no warning, nothing surfaced at dispatch time.
Coordinator treats those pins as COST AND ROLE INVARIANTS ("executors are
always Sonnet -- not up for debate"), not defaults: 21 agents pin
`model: sonnet`, 3 pin `model: haiku`, 27 pin `effort: low`. Measured live:
a `coordinator:executor` dispatch ran as Opus this way. See
cross-repo/inbox/2026-08-11-example-retrieval-repo-em-agent-model-pin-silently-overridable.md.

OWN CONCERN, OWN FILE. Enumeration (is `subagent_type` a known type at
all?) and pin-enforcement (does a KNOWN type's dispatch respect its OWN
pin?) are different questions with different failure shapes --
`block_unenumerated_agent_type`'s docstring is already at its limit. See
that module's own docstring, "COMPOSITION WITH `enforce_agent_model_pin`",
for how the two compose into the one DoE-registered `PreToolUse(Agent)`
seam.

INPUT TRUST. `subagent_type`, `model`, `effort` are read from `tool_input`
ONLY -- never from `dispatched-agents.txt` or any agent-writable file,
matching the sibling module's own "INPUT TRUST" note.

COMPARISON RULE -- DENY-BY-DEFAULT ON ANY NON-EQUAL, EXCEPT A
STRICTLY-CHEAPER VALUE. Cost risk is one-directional: a dispatcher nudging
a worker UP a tier costs real money with no counter anywhere, so only a
downward move is safe to let through, and even then only as a visible
advisory, never a silent pass:
    - equal to the pin            -> PASS silently.
    - strictly cheaper than the pin -> PASS, but return a non-blocking
      advisory naming the pin and the passed value (see "ADVISORY" below).
    - anything else (more expensive, OR unrecognized/unorderable) -> DENY.

MODEL ORDER: `haiku < sonnet < opus`. `fable` is DELIBERATELY NOT placed in
that ordering -- NEGATIVE SPEC: a future reader must not "fix" this by
guessing a rank. There is no defensible cost position for `fable` relative
to the other three (it is not a cheaper-or-more-expensive point on the same
haiku/sonnet/opus cost line, it is a different axis entirely), so a
`fable` override against ANY pin lands in the unorderable leg below and
DENIES unconditionally. That is the conservative direction, and it is
exactly what makes "executors are always Sonnet" mechanically true rather
than aspirational -- an unrecognized value never gets the benefit of the
doubt.

EFFORT ORDER: `low < medium < high < xhigh < max`, taken verbatim from the
Agent tool's own `effort` enum ordering, which is itself a cost ordering.

DENY MESSAGE QUOTES THE PIN BACK. The memo's core diagnosis is that the pin
is invisible exactly where the override is visible -- the harness surfaces
`model:`/`effort:` on the dispatching call, never the frontmatter pin it
is silently outranking. The deny message is the fix for that asymmetry,
not decoration: it names the pin, the value that violated it, and the
resolved `coordinator/agents/<name>.md` path the pin was read from
(`resolve_model_pins()`'s `_source_path`, ACTUALLY RESOLVED AT RUNTIME via
`read_doe_root_pointer()` -- never a hardcoded drive-lettered path in this
module's source; see that function's own docstring).

ADVISORY. A strictly-cheaper override is not silent: it returns
`coordinator_core._hook_envelope.allow_advisory("PreToolUse", ...)` --
permissionDecision:"allow" + additionalContext, the existing PreToolUse
pass-with-context shape this repo's other advisory hooks already use
(e.g. `bash_guards/block_illegal_filename.py`). No new envelope shape is
invented for this.

UNPINNED-MODEL ADVISORY. A dispatch that passes no `model` at all, against a
`subagent_type` `resolve_model_pins()` has NO ENTRY for (an unenumerated or
genuinely unpinned type -- `general-purpose`, a host-native type, or a
custom agent whose frontmatter carries no `model:` pin), gets a
non-blocking advisory suggesting `model: sonnet` -- the house cost default,
never a silent opus-by-harness-default. This is deliberately WEAKER than
the DENY/advisory legs above: those compare a PASSED value against a PIN;
this leg fires on the ABSENCE of both, so there is nothing to compare and
nothing to deny -- see "R14 warns; it does not refuse" in the plan's
Design decisions. Fires at most once per `(session_id, subagent_type)` --
a session-scoped sentinel file under
`<git common dir>/coordinator-sessions/<session_id>/`, the same hub every
other per-session dedup marker in this package uses (see
`hooks.support.session_hub`). Dedup is advisory-only plumbing, not a
correctness gate: an unresolvable session id or git root just means the
advisory fires every time rather than once -- it never blocks, so failing
to dedupe costs a repeated notice, not a wrong verdict.

ESCAPE HATCH -- `COORDINATOR_OVERRIDE_AGENT_MODEL_PIN`, non-empty to
bypass, read INLINE inside `check()` (never hoisted to module scope --
F2 discipline, matching `block_illegal_filename.py`'s own inline-read
convention). ENV, DELIBERATELY NOT A PROMPT MARKER: an env var is
pre-launch, human-operator-only -- reachable only by someone who can set
process environment BEFORE the harness/hook process launches. A prompt
marker (the pattern `block_unenumerated_agent_type` uses for ITS OWN
override) is reachable by the very dispatching agent this guard exists to
bound -- the agent that just got denied for passing `model: opus` could
just as easily append an override line to its own next prompt. This guard
protects against exactly that actor, so its hatch cannot be one more thing
that actor controls. Same key-naming pattern as every other
`COORDINATOR_OVERRIDE_*` in this repo -- no new pattern invented; see
`docs/reference/guard-override-keys.md`.

FAIL-CLOSED ON PIN-RESOLUTION FAILURE. `resolve_model_pins()` inherits
`resolve_roster()`'s fail-closed contract (same `coordinator/agents/*.md`
walk, see that function's own docstring) -- an unreadable/missing
`coordinator/agents/` directory denies here too, rather than silently
treating every dispatch as unpinned. In the composed, DoE-registered path
(`block_unenumerated_agent_type.check()` delegating to this module) this
leg is normally unreachable in practice: the caller only delegates after
ITS OWN roster resolution already succeeded against the identical
`doe_root`, so a resolution failure here would already have denied one
level up. It is reachable when this module's `check()` is called directly
(as this file's own tests do), which is why the fail-closed leg is kept
rather than assumed away.

Two entrypoints, matching the sibling module's own convention:
    check(payload) -- pure function, `Dict[str, Any] -> Optional[Dict]`.
    main()          -- stdin-JSON / stdout-JSON / exit-0 standalone script,
        for direct `hooks.json` registration (DoE-side; out of scope here,
        and unused in practice since `block_unenumerated_agent_type`'s own
        seam already delegates in-process).

Spec backlink: cross-repo/inbox/2026-08-11-example-retrieval-repo-em-agent-model-pin-silently-overridable.md
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Dict, Optional

from coordinator_core._hook_envelope import allow_advisory, deny
from coordinator_core.hooks.block_unenumerated_agent_type import (
    resolve_model_pins,
    resolve_subagent_type,
)
from coordinator_core.hooks.support.git_common_dir import resolve_git_common_dir
from coordinator_core.hooks.support.session_hub import (
    ensure_session_dir,
    session_id_is_real,
)

CLASS = "hard-deny"
MATCHERS = ("Agent",)

_OVERRIDE_ENV = "COORDINATOR_OVERRIDE_AGENT_MODEL_PIN"

#: `block_unenumerated_agent_type._HARNESS_BUILTIN_TYPES`'s own `fork` note.
_FORK_TYPE = "fork"

#: deliberately absent -- see "NEGATIVE SPEC" above. Do not add it here by
_MODEL_ORDER: Dict[str, int] = {"haiku": 0, "sonnet": 1, "opus": 2}

_EFFORT_ORDER: Dict[str, int] = {"low": 0, "medium": 1, "high": 2, "xhigh": 3, "max": 4}

#: sentinel filename prefix for the unpinned-model advisory dedup marker --
#: see "UNPINNED-MODEL ADVISORY" in the module docstring.
_UNPINNED_SENTINEL_PREFIX = "unpinned-model-advisory."

_UNSAFE_CHARS_RE = re.compile(r"[^A-Za-z0-9_.-]")

_UNPINNED_MODEL_MESSAGE = (
    "ADVISORY: {subagent_type} was dispatched with no `model` and has no "
    "model pin on record -- consider `model: sonnet` (the house cost "
    "default). Advisory only -- an unpinned type is never blocked."
)


def _repo_root_from_cwd(cwd: Optional[str]) -> Optional[str]:
    probe = os.path.abspath(cwd if isinstance(cwd, str) and cwd else os.getcwd())
    while True:
        if os.path.exists(os.path.join(probe, ".git")):
            return probe
        parent = os.path.dirname(probe)
        if parent == probe:
            return None
        probe = parent


def _claim_unpinned_advisory_once(payload: Dict[str, Any], subagent_type: str) -> bool:
    """Return True if this `(session_id, subagent_type)` has not yet fired.

    Best-effort: any resolution failure (no real session id, no git root, an
    OSError writing the sentinel) returns True -- see "Dedup is
    advisory-only plumbing" in the module docstring.
    """
    session_id = payload.get("session_id")
    if not session_id_is_real(session_id):
        return True
    repo_root = _repo_root_from_cwd(payload.get("cwd"))
    if not repo_root:
        return True
    common_dir = resolve_git_common_dir(repo_root)
    if not common_dir:
        return True
    session_dir = Path(common_dir) / "coordinator-sessions" / str(session_id)
    safe_type = _UNSAFE_CHARS_RE.sub("_", subagent_type) or "unknown"
    sentinel = session_dir / (_UNPINNED_SENTINEL_PREFIX + safe_type)
    try:
        if sentinel.exists():
            return False
        if not ensure_session_dir(session_dir, session_id):
            return True
        sentinel.touch()
    except OSError:
        return True
    return True


def _unpinned_model_advisory(payload: Dict[str, Any], subagent_type: str) -> Optional[Dict[str, Any]]:
    if not _claim_unpinned_advisory_once(payload, subagent_type):
        return None
    return allow_advisory("PreToolUse", _UNPINNED_MODEL_MESSAGE.format(subagent_type=subagent_type))


def _clean_str(value: Any) -> Optional[str]:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _axis_verdict(pin_value: str, passed_value: str, order: Dict[str, int]) -> str:
    """Classify one axis comparison -- `"equal"`, `"cheaper"`, or `"deny"`.

    `"deny"` covers both "strictly more expensive" and "unorderable" (either
    value absent from `order`) -- see module docstring "COMPARISON RULE"
    and "MODEL ORDER" for why the unorderable leg denies rather than
    passing.
    """
    if passed_value == pin_value:
        return "equal"
    if pin_value not in order or passed_value not in order:
        return "deny"
    return "cheaper" if order[passed_value] < order[pin_value] else "deny"


def _deny_reason(subagent_type: str, source_path: str, violations: "list[tuple[str, str, str]]") -> str:
    lines = [
        f"{subagent_type} pins {axis}={pin_value}, passed {passed_value}."
        for axis, pin_value, passed_value in violations
    ]
    return (
        "DENY: " + " ".join(lines) + " Each pin is a "
        "cost-and-role invariant, not a default: drop the parameter(s) and "
        "re-dispatch.\n"
        f"<resolved from: {source_path}>"
    )


def _advisory_context(subagent_type: str, source_path: str, advisories: "list[tuple[str, str, str]]") -> str:
    lines = [
        f"{subagent_type} pins {axis}: {pin_value} -- this dispatch passed the strictly cheaper {axis}: {passed_value}."
        for axis, pin_value, passed_value in advisories
    ]
    return (
        " ".join(lines)
        + f" (pin resolved from: {source_path}) Advisory only -- a downward "
        "override is never blocked."
    )


def check(payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if (payload.get("tool_name") or "") not in MATCHERS:
        return None

    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        tool_input = {}

    subagent_type = resolve_subagent_type(tool_input)

    passed_model = _clean_str(tool_input.get("model"))
    passed_effort = _clean_str(tool_input.get("effort"))

    if subagent_type == _FORK_TYPE:
        return None

    if os.environ.get(_OVERRIDE_ENV):
        return None

    if passed_model is None and passed_effort is None:
        pins, _error_reason = resolve_model_pins()
        if pins is not None and not pins.get(subagent_type):
            return _unpinned_model_advisory(payload, subagent_type)
        return None

    pins, error_reason = resolve_model_pins()
    if pins is None:
        return deny("PreToolUse", error_reason or "model-pin roster unresolved")

    entry = pins.get(subagent_type)
    if not entry:
        if passed_model is None:
            return _unpinned_model_advisory(payload, subagent_type)
        return None

    pin_model = entry.get("model")
    pin_effort = entry.get("effort")
    source_path = entry.get("_source_path", "<unresolved>")

    violations: "list[tuple[str, str, str]]" = []
    advisories: "list[tuple[str, str, str]]" = []

    if pin_model and passed_model is not None:
        verdict = _axis_verdict(pin_model, passed_model, _MODEL_ORDER)
        if verdict == "deny":
            violations.append(("model", pin_model, passed_model))
        elif verdict == "cheaper":
            advisories.append(("model", pin_model, passed_model))

    if pin_effort and passed_effort is not None:
        verdict = _axis_verdict(pin_effort, passed_effort, _EFFORT_ORDER)
        if verdict == "deny":
            violations.append(("effort", pin_effort, passed_effort))
        elif verdict == "cheaper":
            advisories.append(("effort", pin_effort, passed_effort))

    if violations:
        return deny("PreToolUse", _deny_reason(subagent_type, source_path, violations))

    if advisories:
        return allow_advisory("PreToolUse", _advisory_context(subagent_type, source_path, advisories))

    return None


def main() -> int:
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
