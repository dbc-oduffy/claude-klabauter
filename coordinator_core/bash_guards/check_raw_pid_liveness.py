"""coordinator_core.bash_guards.check_raw_pid_liveness -- PreToolUse(Bash)
hard-deny guard closing the RAW-PID-LIVENESS tripwire's own long-standing
"forthcoming" mechanical-enforcement tier (example-doctrine-repo
``docs/wiki/coordinator-tripwires.md`` § RAW-PID-LIVENESS: "a C5 PreToolUse
offer-hook (mechanical enforcement tier)").

Doctrine this mechanizes: session/claim liveness is ``cs_live_session_ids`` /
``cs_claim_holder_live`` ONLY. A raw ``ps -p``/``kill -0`` (or the Python
one-liner shape ``os.kill(pid, 0)``) probing a *stored* pid as a liveness
gate is structurally wrong, not merely discouraged -- a pid persisted to
disk (a claim-dir file, a hook subshell's own ``$$``) is near-certain to be
a dead hook subshell by the time it is re-read, so the raw-pid test reads
"dead" regardless of whether the SESSION it was meant to represent is still
live. This guard denies the raw-pid idiom and offers the correct primitive
in the same envelope (design-as-offers, ``CLAUDE.md`` § Implementation
Standards -- Extensions) instead of a bare block.

Detection shape -- three literal forms, each scoped to a single shell
SEGMENT (split on ``;``/``&&``/``||``/``|``/newline) so a match in one
command of a longer pipeline never over-attributes context from an
unrelated neighboring command:

  1. ``ps -p <arg>`` -- ``-p`` is ``ps``'s own "list these pids" flag; any
     invocation of this shape is BY DEFINITION a pid-liveness probe, never
     anything else.
  2. ``kill -0 <arg>`` -- signal 0 sent to a pid is POSIX's canonical
     "does this process exist" probe; there is no other reason to send it.
  3. ``os.kill(<arg>, 0)`` -- the Python stdlib mirror of (2), as it
     appears in an inline ``python3 -c '...'`` one-liner.

"Near a stored pid-var pattern" (the scoping condition the C14 stub names)
is implemented as: the flag match is a hard requirement (forms 1-2) or is
itself the Python spelling (form 3), AND the SAME shell segment additionally
shows the argument is pid-shaped -- a ``$``-prefixed variable/command
substitution reference, a bare digit-literal pid, the current-shell pid
token ``$$``, or the literal substring ``pid`` (case-insensitive) anywhere
in the segment (covers ``$(cat foo.pid)``, ``PID=...``, ``the_pid``, etc).
Because ``-p``/``-0``/``os.kill(...,0)`` are unambiguous liveness idioms by
construction (see 1-3 above), this second condition is a low-value-but-
free narrowing pass, not the guard's real selectivity -- it exists so a
segment merely quoting the string ``"ps -p"`` inside an unrelated echo
without ANY pid-shaped token nearby does not fire.

Negative-spec:
  - Does NOT deny ``ps -p`` / ``kill -0`` variants with no pid-shaped
    argument recognizable in the same segment (e.g. a bare ``ps -p`` with
    nothing following, which would itself error at the shell before
    reaching a liveness question).
  - Does NOT deny ``kill`` invocations that are not signal-0 (``kill -9
    $pid``, plain ``kill $pid``) -- those terminate a process, they do not
    probe its liveness, and are out of this guard's remit.
  - Does NOT deny ``ps`` invocations that do not use ``-p`` (``ps aux``,
    ``ps -ef | grep foo``) -- those are general process listing, not a
    liveness gate on a specific stored pid.
  - Does NOT resolve or read the caller's identity (``agent_id`` etc) --
    unlike the subagent-confinement cohort in this package, the raw-pid
    anti-pattern is wrong for EVERY caller, EM included; this guard is
    NOT identity-gated by design.
  - Does NOT attempt cross-segment correlation (an assignment ``pid=$!`` in
    one segment and a bare ``ps -p $pid`` in the next COULD legitimately be
    an intentional-but-still-wrong raw-pid liveness check the "same
    segment" scoping above would miss) -- accepted false-negative, not an
    oversight: cross-segment dataflow tracking is unbounded-cost static
    analysis this guard does not attempt, matching every sibling guard in
    this package's segment/regex-scoped-not-dataflow-scoped posture.

Escape hatch: ``COORDINATOR_OVERRIDE_RAW_PID_LIVENESS=1`` -- read inline at
``check()`` call time (F2 discipline, never hoisted to module scope), for
the rare legitimate raw-pid use this guard cannot distinguish (e.g.
interactive process-management tooling entirely unrelated to session/claim
liveness that merely happens to share the same two flag spellings).

Spec backlink: example-doctrine-repo ``docs/plans/2026-07-27-claude-md-altitude-triage.md``
§ C14/RAW-PID-LIVENESS-GUARD.
Tripwire entry: example-doctrine-repo ``coordinator/docs/wiki/coordinator-tripwires.md``
§ RAW-PID-LIVENESS.
"""

from __future__ import annotations

import os
import re
from typing import Any, Dict, Optional

from coordinator_core.bash_guards._helpers import operator_override_note
from coordinator_core.bash_guards.block_subagent_destructive_action import (
    _strip_heredoc_bodies,
)

CLASS = "hard-deny"
MATCHERS = ["Bash"]
PRIORITY = 46

#: Escape hatch -- inline env read only, never hoisted (F2 discipline).
_OVERRIDE_ENV = "COORDINATOR_OVERRIDE_RAW_PID_LIVENESS"

#: Segment splitter -- mirrors the sibling guards' shell-segment scoping
#: (e.g. ``block_subagent_commit``'s heredoc/segment handling): a raw-pid
#: idiom in one pipeline stage must not borrow "pid-shaped" context from an
#: unrelated neighboring stage.
_SEGMENT_SPLIT_RE = re.compile(r"&&|\|\||[;\n|]")

#: Form 1 -- ``ps -p <arg>``. ``-p`` is ps's own pid-list flag; the
#: match itself is the whole selectivity, the trailing capture is only used
#: to confirm an argument is actually present (a bare flag with nothing
#: after it cannot be a liveness probe of anything).
_PS_P_RE = re.compile(r"\bps\s+-p\s+(\S+)")

#: Form 2 -- ``kill -0 <arg>``. Signal 0 is POSIX's canonical existence probe.
_KILL_0_RE = re.compile(r"\bkill\s+-0\s+(\S+)")

#: Form 3 -- Python stdlib mirror, as it appears inline in a
#: ``python3 -c '...'`` one-liner: ``os.kill(<arg>, 0)``.
_OS_KILL_RE = re.compile(r"\bos\.kill\(\s*([^,()]+?)\s*,\s*0\s*\)")

#: "Near a stored pid-var pattern" narrowing signal -- any ONE of these
#: appearing in the SAME segment as a form-1/2/3 match confirms the argument
#: is pid-shaped: a shell variable/command-substitution reference, the
#: current-shell-pid token, a bare digit-literal pid, or the literal
#: substring "pid" (case-insensitive) anywhere in the segment.
_PID_VAR_HINT_RE = re.compile(r"\$\$|\$\{?\w+\}?|(?i:pid)|\b\d+\b")


def _segment_has_raw_pid_liveness_idiom(segment: str) -> Optional[str]:
    """Return the matched idiom's human-facing label if ``segment`` contains
    a raw-pid liveness probe with a pid-shaped argument nearby, else
    ``None``. Checked in ``ps -p`` -> ``kill -0`` -> ``os.kill(...,0)``
    order; first match wins (mirrors every sibling guard's first-match-wins
    convention in this package)."""
    for regex, label in (
        (_PS_P_RE, "ps -p"),
        (_KILL_0_RE, "kill -0"),
        (_OS_KILL_RE, "os.kill(pid, 0)"),
    ):
        m = regex.search(segment)
        if not m:
            continue
        if _PID_VAR_HINT_RE.search(segment):
            return label
    return None


def _advisory_reason(idiom: str) -> str:
    return (
        "%s: dead pid, not a live session. Use instead: "
        "`session-liveness-cli session-live SID` or "
        "`session-liveness-cli claim-holder-live CLAIM_DIR`.\n\n"
        + operator_override_note(_OVERRIDE_ENV)
    ) % (idiom,)


def check(payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Evaluate the raw-pid-liveness gate against a PreToolUse payload.

    Returns ``None`` (allow) or the nested advisory-allow envelope. Never
    identity-gated -- see module docstring negative-spec.
    """
    # Deliberately no try/except here: fail-CLOSED-on-exception is the
    # dispatcher's job for hard-deny guards (see dispatch.py's guard_chain
    # fail_closed=True entries, which route an uncaught exception from this
    # function through its crash-deny wrapper) -- catching and swallowing an
    # unexpected error into a silent allow here would defeat that contract.
    if (payload.get("tool_name") or "") != "Bash":
        return None

    tool_input = payload.get("tool_input") or {}
    cmd = (tool_input.get("command") if isinstance(tool_input, dict) else None) or ""
    if not cmd:
        return None
    cmd = cmd.replace("\r", "")
    # Heredoc bodies are stdin DATA, not shell command text -- strip them
    # before scanning, same as every sibling guard in this package (2026-
    # 07-29 incident fix; see e.g. `check_test_suite_invocation.py`'s own
    # `_strip_heredoc_bodies` call). Without this, a heredoc body merely
    # CONTAINING the string "ps -p 1234" as data (a probe script, a
    # findings write-up) was denied as if it were a live liveness probe.
    cmd = _strip_heredoc_bodies(cmd)

    if os.environ.get(_OVERRIDE_ENV, "0") == "1":
        return None

    for segment in _SEGMENT_SPLIT_RE.split(cmd):
        if not segment.strip():
            continue
        idiom = _segment_has_raw_pid_liveness_idiom(segment)
        if idiom is not None:
            return {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "allow",
                    "additionalContext": _advisory_reason(idiom),
                }
            }

    return None
