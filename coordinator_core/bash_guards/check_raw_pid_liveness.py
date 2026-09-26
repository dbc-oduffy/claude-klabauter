"""coordinator_core.bash_guards.check_raw_pid_liveness -- PreToolUse(Bash)
hard-deny guard closing the RAW-PID-LIVENESS tripwire's own long-standing
"forthcoming" mechanical-enforcement tier (DoE
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

Spec backlink: DoE ``DoE-claude:pln-claude-md-altitude-triage-earn-31f32e``
§ C14/RAW-PID-LIVENESS-GUARD.
Tripwire entry: DoE ``coordinator/docs/wiki/coordinator-tripwires.md``
§ RAW-PID-LIVENESS.
"""

from __future__ import annotations

import os
import re
from typing import Any, Dict, Optional

from coordinator_core.bash_guards._dialect import Dialect, dialect_from_tool_name
from coordinator_core.bash_guards._helpers import operator_override_note
from coordinator_core.bash_guards._tool_names import COMMAND_TOOL_NAMES
from coordinator_core.bash_guards._verdict import record_silent
from coordinator_core.bash_guards.block_subagent_destructive_action import (
    _strip_heredoc_bodies,
)

CLASS = "hard-deny"
#: "check-raw-pid-liveness" entry) declares `GuardBand.ADVISORY_REWRITE`
#: It also already dialect-branches to SILENT for `Dialect.POWERSHELL`
#: text. `MATCHERS` therefore references the shared tool-name universe
MATCHERS = COMMAND_TOOL_NAMES
PRIORITY = 46

_OVERRIDE_ENV = "COORDINATOR_OVERRIDE_RAW_PID_LIVENESS"

_SEGMENT_SPLIT_RE = re.compile(r"&&|\|\||[;\n|]")

_PS_P_RE = re.compile(r"\bps\s+-p\s+(\S+)")

_KILL_0_RE = re.compile(r"\bkill\s+-0\s+(\S+)")

_OS_KILL_RE = re.compile(r"\bos\.kill\(\s*([^,()]+?)\s*,\s*0\s*\)")

_PID_VAR_HINT_RE = re.compile(r"\$\$|\$\{?\w+\}?|(?i:pid)|\b\d+\b")


def _segment_has_raw_pid_liveness_idiom(segment: str) -> Optional[str]:
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


def _advisory_reason(idiom: str, payload: Optional[Dict[str, Any]] = None) -> str:
    return (
        "%s: dead pid, not a live session. Use instead: "
        "`session-liveness-cli session-live SID` or "
        "`session-liveness-cli claim-holder-live CLAIM_DIR`.\n\n"
        + operator_override_note(_OVERRIDE_ENV, payload=payload)
    ) % (idiom,)


def check(payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    tool_name = payload.get("tool_name") or ""
    dialect = dialect_from_tool_name(tool_name)
    if dialect is Dialect.POWERSHELL:
        # guard's own segment splitter (`_SEGMENT_SPLIT_RE`) and all three
        record_silent(
            "check_raw_pid_liveness",
            "PowerShell dialect: no recognized liveness idiom "
            "(ps -p / kill -0 / os.kill are POSIX-only)",
        )
        return None
    if dialect is not Dialect.BASH:
        return None

    tool_input = payload.get("tool_input") or {}
    cmd = (tool_input.get("command") if isinstance(tool_input, dict) else None) or ""
    if not cmd:
        return None
    cmd = cmd.replace("\r", "")
    # CONTAINING the string "ps -p 1234" as data (a probe script, a
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
                    "additionalContext": _advisory_reason(idiom, payload=payload),
                }
            }

    return None
