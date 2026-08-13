"""coordinator_core.bash_guards.block_disarm_marker_sentinel_creation --
PreToolUse (Bash) hard-deny guard that makes the blanket-disarm marker
un-creatable by any agent -- Bash-level, EM included.

WHY THIS EXISTS. `_blanket_disarm.py` reads a single machine-scoped marker
file (`<settings-home>/.coordinator-bash-guards-disarmed`, basename
re-exported as `_blanket_disarm.MARKER_BASENAME`) to suppress whole bands
of this guard suite (see that module's own docstring "THE SCOPE AXIS" /
"BAND-SCOPED SUPPRESSION"). That module's own docstring names this exact
gap as "DESIGNED FOR, NOT WIRED, IN THIS TASK" and explicitly defers it to
a follow-up -- this guard is that follow-up. The marker's own module
docstring states the threat directly: "if a subagent could `touch` or
redirect into this marker, the entire guard suite becomes self-disarmable
by exactly the caller class it exists to constrain (and, worse, a subagent
forging a `Scope: machine-total` marker would defeat the EM-only audience
narrowing entirely by writing its own way around it)." This is the
identical discovery shape that produced `block_worktree_sentinel_
creation.py`: a sentinel file's mere ABSENCE gates a capability (there,
the worktree ban; here, the entire suppressible-band chain), so its
CREATION must be un-forgeable, or the boundary is theatre.

CONFINEMENT_DENY, UNCONDITIONALLY -- this is the one property that closes
the loop rather than reopening it. Any WEAKER band would be circular: the
disarm marker itself could then be used to suppress the very guard that
protects it (an operator writes `Bands: advisory-rewrite` intending to
quiet noisy rewrite advisories, and that same marker happens to also
suppress this guard if this guard were registered in a suppressible band
-- an agent could then forge a SECOND marker naming `machine-total` scope
while this guard looked the other way). `GuardBand.CONFINEMENT_DENY` is
unconditionally non-suppressible by any marker of any scope (see
`_blanket_disarm.py`'s own "BAND-SCOPED SUPPRESSION" -- a marker naming
`confinement-deny` in `Bands:` is rejected in its entirety, never
half-applied), which is exactly what breaks the loop: no marker, however
crafted, can ever suppress the guard that stops a new marker from being
forged.

NOT IDENTITY-GATED -- fires for every caller, EM included, same posture as
`block_worktree_sentinel_creation.py` and `block_approval_sentinel_
creation.py`: the anti-pattern (self-granting a guard-suite disarm) is
wrong regardless of who types it. An EM-only gate would defeat the guard's
own purpose, since `Scope: machine-total` is itself EM-audience-narrowed
(per `_blanket_disarm.py`'s own "AUDIENCE NARROWING") precisely because an
EM has a human PM watching in real time -- that narrowing means nothing if
the EM can forge the marker exactly as freely as any subagent could.

NO OVERRIDE -- DELIBERATE, by design, no exceptions. Same reasoning as the
two sibling sentinel guards' own "NO OVERRIDE" sections: any
`COORDINATOR_OVERRIDE_*` escape hatch here would be reachable by exactly
the caller class this guard exists to constrain (a subagent, or an EM,
setting its own process env), making this a bypass of a bypass-prevention
guard.

REGISTRATION ORDERING -- MUST run BEFORE `offer-git-c` in
`coordinator_core.bash_guards.dispatch`, for the identical short-circuit
reason documented on the two sibling sentinel guards' own "REGISTRATION
ORDERING" sections: `offer-git-c` rewrites `cd <dir> && git <sub>`-shaped
commands into `git -C <dir> <sub>` and returns allow-with-updatedInput,
which SHORT-CIRCUITS every later guard in the chain -- so a guard
registered after it never sees `cd <settings-home> && touch
.coordinator-bash-guards-disarmed`. Registered immediately adjacent to
`block_worktree_sentinel_creation` and `block_approval_sentinel_creation`
in `dispatch.py`, ahead of `offer-git-c`, for the same reason.

DETECTION SURFACE. Delegates entirely to the shared `SentinelCreationDetector`
in `_sentinel_creation_guard.py` -- this is the THIRD guard built on it,
after `block_approval_sentinel_creation.py` and `block_worktree_sentinel_
creation.py` (see that module's docstring for the full rule set:
redirection, `touch`/`cp`/`mv`/`install`/`ln`/`tee`, `sed -i`, `python -c`,
`dd of=`, and the indirection-wrapper hardening). `SentinelCreationDetector`
matches on BASENAME only (`_basename(token) == target_basename`), never
the full path -- which is exactly right here: this marker is
settings-home-scoped, not repo-root-scoped like its two siblings, so its
absolute path varies by machine (`COORDINATOR_SETTINGS_HOME` override, else
`CLAUDE_HOME`, else the platform home + `.coordinator-claude-settings` --
see `_settings_home.settings_home()`). A caller writing `touch
~/.coordinator-claude-settings/.coordinator-bash-guards-disarmed`, `touch
$COORDINATOR_SETTINGS_HOME/.coordinator-bash-guards-disarmed`, or a bare
`touch .coordinator-bash-guards-disarmed` from inside that directory all
present the identical final path component, so basename matching denies
all three without this guard needing to resolve `settings_home()` itself
(which would require importing and calling the settings-home resolver on
every Bash command, for no additional coverage the basename match doesn't
already provide -- the two existing sentinel guards establish the same
basename-only precedent for their own, differently-located targets).
Target basename imported from `_blanket_disarm.MARKER_BASENAME` rather
than hand-copied, so the two modules can never drift apart on what string
they mean by "the disarm marker." DEFAULT POSTURE ON AMBIGUITY IS DENY,
same asymmetric posture as the two sibling sentinel guards.

ALLOWED, UNCONDITIONALLY: reads (`cat`, `ls`, `stat`) and removal (`rm`,
`test -f ... && rm ...`) of the marker. Removing a disarm marker always
re-arms the guard suite rather than disarming it further.

Deny message deliberately never names the marker's basename or path and
prints no workaround -- same discipline as both sibling sentinel guards'
own deny-message sections ("an eager agent reading its own bypass in a
deny message treats it as sanctioned"). Names only the legitimate path:
escalating to the EM/PM.

Write-surface note (Leg 2 of this guard's own dispatch brief, "a Bash
guard is not a file-creation guard"): this module closes ONLY the Bash
leg. The `Write`/`Edit`/`NotebookEdit` leg is a SEPARATE PreToolUse hook
family this repo does not own -- see this guard's own test file's "Write
surface" note for what was checked and what was found. Do not treat this
module's presence as proof the marker is un-creatable via every tool; it
proves only that the Bash leg is closed.

Spec: blanket-disarm-marker sentinel un-creatable-by-agent guard
(dispatch M18-leg2, 2026-07-30) -- companion to `_blanket_disarm.py`'s own
"AGENT-CREATION MUST BE BLOCKABLE" module-docstring section, which this
guard is the follow-up for.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from coordinator_core.bash_guards._blanket_disarm import MARKER_BASENAME
from coordinator_core.bash_guards._sentinel_creation_guard import (
    REASON_INDIRECTION,
    SentinelCreationDetector,
)
from coordinator_core.bash_guards._dialect import Dialect, dialect_from_tool_name
from coordinator_core.bash_guards._tool_names import COMMAND_TOOL_NAMES

CLASS = "hard-deny"
#: Widened 2026-08-07 (C4e) from `["Bash"]` -- see `block_approval_sentinel_
#: creation.py`'s identical note. A direct reference to the shared universe
#: (C2 declaration-form conversion) -- never a copy or re-wrap.
MATCHERS = COMMAND_TOOL_NAMES
PRIORITY = 41

#: The exact basename this guard protects -- imported from `_blanket_
#: disarm.py`, the module that defines what "the disarm marker" means, so
#: the two can never independently drift on the string. Never relaxed to a
#: substring/prefix match (see `_sentinel_creation_guard.SentinelCreation
#: Detector._is_target`): an unrelated file merely CONTAINING this string
#: in a longer name is a different file, not the marker `_blanket_disarm`
#: reads.
_TARGET_BASENAME = MARKER_BASENAME

#: Shared detection engine -- see `_sentinel_creation_guard.py` module
#: docstring. This module is the third guard built on it, after
#: `block_approval_sentinel_creation.py` and `block_worktree_sentinel_
#: creation.py`.
_detector = SentinelCreationDetector(_TARGET_BASENAME)


def _evaluate(cmd: str, dialect: Optional[Dialect] = None):
    """See `block_approval_sentinel_creation._evaluate`'s identical note --
    `dialect=None`/`Dialect.BASH` preserves the exact pre-C4e call shape."""
    if dialect is None or dialect is Dialect.BASH:
        return _detector.evaluate(cmd)
    return _detector.evaluate_for_dialect(
        cmd, dialect, guard_name="block_disarm_marker_sentinel_creation"
    )


def _deny_reason(cmd: str, reason_kind: str, reason_class: str) -> str:
    # Deliberately does NOT echo `cmd` back into the message and does NOT
    # name the target basename in either branch below -- both would print
    # the exact bypass an eager agent could copy-paste, which reads as
    # sanctioning it rather than blocking it (same discipline as both
    # sibling sentinel guards' own deny messages). `cmd` stays accepted for
    # call-site symmetry with the sibling guards, but is intentionally
    # unused here.
    del cmd
    if reason_class == REASON_INDIRECTION:
        # `reason_kind` names a shell SHAPE, not a bypass -- but a
        # recursive indirection verdict can still bottom out one level
        # down in the direct branch's target-naming string (e.g.
        # `bash -c "touch <marker>"`), so redact the basename out
        # regardless, rather than trusting the branch alone.
        safe_shape = reason_kind.replace(_TARGET_BASENAME, "<the marker>")
        return (
            "[disarm-marker guard] BLOCKED: this command was denied because "
            "its payload is delivered through an interpreter, stdin, or "
            "command-assembly indirection this guard cannot examine -- NOT "
            "because the payload was found to touch the blanket-disarm "
            "marker.\n\n"
            "Detected shape: %s\n\n"
            "If this command genuinely does not touch the disarm marker: "
            "run its underlying steps directly (not through an "
            "interpreter/stdin/xargs wrapper) so this guard can see them, "
            "or ask the EM/PM to run it.\n\n"
            "Reading or removing an existing marker remains available -- "
            "removal only re-arms the guard suite." % safe_shape
        )
    del reason_kind  # REASON_DIRECT: message below is fixed, not shape-derived.
    return (
        "[disarm-marker guard] BLOCKED: this command would create or "
        "modify the file that suppresses this guard suite's own bands, "
        "and an agent cannot grant itself that.\n\n"
        "If you believe a disarm is genuinely warranted, say so in your "
        "report and let the EM/PM decide -- do not try to create it from "
        "inside this session.\n\n"
        "Reading or removing an existing marker remains available -- "
        "removal only re-arms the guard suite."
    )


def check(payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Evaluate the disarm-marker-creation-ban gate against a PreToolUse
    payload.

    Returns `None` (allow) or the nested hard-deny envelope. Never
    identity-gated -- fires for every caller including the main-loop EM
    (see module docstring "NOT IDENTITY-GATED").
    """
    # Deliberately no try/except here -- fail-CLOSED-on-exception is the
    # dispatcher's job for hard-deny guards; catching and swallowing an
    # unexpected error into a silent allow here would defeat that contract.
    tool_name = payload.get("tool_name") or ""
    if tool_name not in MATCHERS:
        return None
    dialect = dialect_from_tool_name(tool_name)

    tool_input = payload.get("tool_input") or {}
    cmd = (tool_input.get("command") if isinstance(tool_input, dict) else None) or ""
    if not cmd:
        return None
    cmd = cmd.replace("\r", "")

    deny, reason_kind, reason_class = _evaluate(cmd, dialect)
    if not deny:
        return None

    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": _deny_reason(cmd, reason_kind, reason_class),
        }
    }
