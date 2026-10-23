"""coordinator_core.bash_guards.check_raw_pid_liveness -- PreToolUse(Bash)
hard-deny guard closing the RAW-PID-LIVENESS tripwire's own long-standing
"forthcoming" mechanical-enforcement tier (DoE
``coordinator-content-repo coordinator/docs/wiki/coordinator-tripwires.md`` § RAW-PID-LIVENESS: "a C5 PreToolUse
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
  - Does NOT deny the idiom text inside a quoted argument of a command that
    does not execute it (``sizing-assemble --intent "... ps -p 1 ..."``).
    A quoted span run as shell or interpreter text (``bash -c``, ``sh -c``,
    ``eval``, ``python -c``, ``timeout``/``sudo``/``env``/``nohup``/``xargs``/
    ``ssh`` wrappers) or holding ``$(``/a backtick is still scanned, and
    unbalanced quotes leave the command unmasked.
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

Spec backlink: DoE ``coordinator-content-repo:pln-claude-md-altitude-triage-earn-31f32e``
§ C14/RAW-PID-LIVENESS-GUARD.
Tripwire entry: DoE ``coordinator/docs/wiki/coordinator-tripwires.md``
§ RAW-PID-LIVENESS.
"""

from __future__ import annotations

import os
import re
from typing import Any, Dict, Optional

from coordinator_core._hook_envelope import _stamp
from coordinator_core.bash_guards._dialect import Dialect, dialect_from_tool_name
from coordinator_core.bash_guards._helpers import operator_override_note
from coordinator_core.bash_guards._tool_names import COMMAND_TOOL_NAMES
from coordinator_core.bash_guards._verdict import record_silent
from coordinator_core.bash_guards.block_subagent_destructive_action import (
    _strip_heredoc_bodies,
)

CLASS = "hard-deny"
#: Fires under both Bash and PowerShell tool names; a PowerShell command is
#: recorded SILENT (its three idioms are POSIX-only). Denies via the dispatch
#: policy point, which downgrades to an advisory on a consumer box.
MATCHERS = COMMAND_TOOL_NAMES
PRIORITY = 46

_OVERRIDE_ENV = "COORDINATOR_OVERRIDE_RAW_PID_LIVENESS"

_SEGMENT_SPLIT_RE = re.compile(r"&&|\|\||[;\n|]")

_PS_P_RE = re.compile(r"\bps\s+-p\s+(\S+)")

_KILL_0_RE = re.compile(r"\bkill\s+-0\s+(\S+)")

_OS_KILL_RE = re.compile(r"\bos\.kill\(\s*([^,()]+?)\s*,\s*0\s*\)")

_PID_VAR_HINT_RE = re.compile(r"\$\$|\$\{?\w+\}?|(?i:pid)|\b\d+\b")

_ASSIGNMENT_RE = re.compile(r"^[A-Za-z_]\w*=")

#: Commands whose quoted argument is itself shell or interpreter text, so it is
#: executed rather than merely carried as data.
_EXECUTING_COMMAND_RE = re.compile(
    r"^(?:bash|sh|zsh|dash|eval|python[\d.]*|py|timeout|sudo|env|nohup|xargs|ssh)$"
)


def _command_word(prefix: str) -> str:
    """First word of a segment prefix after leading ``VAR=val`` assignments."""
    for token in prefix.split():
        if not _ASSIGNMENT_RE.match(token):
            return token.rsplit("/", 1)[-1]
    return ""


def _mask_quoted_arguments(cmd: str) -> str:
    """Length-preserving copy of ``cmd`` with the interior of each quoted span
    that is plain data (not run by a shell/interpreter/wrapper, no ``$(`` or
    backtick) replaced by ``x``. Unbalanced quotes return ``cmd`` unmasked."""
    out: list = []
    seg_start = 0
    i = 0
    n = len(cmd)
    while i < n:
        ch = cmd[i]
        if ch == "\\":
            out.append(cmd[i : i + 2])
            i += 2
            continue
        if ch in "'\"":
            j = i + 1
            while j < n and cmd[j] != ch:
                j += 2 if (ch == '"' and cmd[j] == "\\") else 1
            if j >= n:
                return cmd
            interior = cmd[i + 1 : j]
            word = _command_word("".join(out)[seg_start:])
            if (
                word
                and not _EXECUTING_COMMAND_RE.match(word)
                and "$(" not in interior
                and "`" not in interior
            ):
                interior = "x" * len(interior)
            out.append(cmd[i] + interior + cmd[j])
            i = j + 1
            continue
        m = _SEGMENT_SPLIT_RE.match(cmd, i)
        if m:
            out.append(m.group())
            i = m.end()
            seg_start = sum(len(piece) for piece in out)
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def _segment_has_raw_pid_liveness_idiom(
    segment: str, raw_segment: Optional[str] = None
) -> Optional[str]:
    """Idiom regexes run on ``segment`` (masked); the pid-shape hint runs on
    ``raw_segment`` so a quoted ``"$PID"`` still reads as pid-shaped."""
    hint_text = segment if raw_segment is None else raw_segment
    for regex, label in (
        (_PS_P_RE, "ps -p"),
        (_KILL_0_RE, "kill -0"),
        (_OS_KILL_RE, "os.kill(pid, 0)"),
    ):
        m = regex.search(segment)
        if not m:
            continue
        if _PID_VAR_HINT_RE.search(hint_text):
            return label
    return None


def _deny_reason(idiom: str, payload: Optional[Dict[str, Any]] = None) -> str:
    return (
        "%s: dead pid, not a live session. Use instead: "
        "`session-liveness-cli session-live SID` or "
        "`session-liveness-cli claim-holder-live CLAIM_DIR`.\n\n"
        + operator_override_note(_OVERRIDE_ENV, payload=payload)
    ) % (idiom,)


def _deny_record(idiom: str, payload: Dict[str, Any]) -> Any:
    """Deny record; ``em_text`` is the stamped legacy reason, byte for byte.
    The EscalationRoute exists because this guard is not identity-gated, so a
    subagent render must have one."""
    from coordinator_core.guard_messages.alternative_record import (
        AdaptableOffer,
        AlternativeRecord,
        EscalationRoute,
        Override,
    )

    reason = _deny_reason(idiom, payload=payload)
    return AlternativeRecord(
        verdict_kind="deny",
        guard_name="check-raw-pid-liveness",
        summary=reason,
        members=(
            AdaptableOffer(
                template="session-liveness-cli session-live SID", what_to_adapt="SID"
            ),
            AdaptableOffer(
                template="session-liveness-cli claim-holder-live CLAIM_DIR",
                what_to_adapt="CLAIM_DIR",
            ),
            EscalationRoute(
                report_what="the process you need to probe",
                authorize_what="a raw pid probe",
            ),
            Override(env_var=_OVERRIDE_ENV),
        ),
        em_text=_stamp(reason),
    )


def check(payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    tool_name = payload.get("tool_name") or ""
    dialect = dialect_from_tool_name(tool_name)
    if dialect is Dialect.POWERSHELL:
        # C5 (row 19, `docs/reference/guard-dialect-coverage.md`): this
        # guard's own segment splitter (`_SEGMENT_SPLIT_RE`) and all three
        # detection forms (`ps -p`, `kill -0`, `os.kill(pid, 0)`) are
        # POSIX-only -- no PowerShell liveness idiom (`Get-Process -Id`) is
        # recognized at all. A PowerShell command is therefore never a
        # confirmed clean verdict here; record SILENT rather than let an
        # unscanned command read as cleared. Does NOT scan `cmd` -- the
        # splitter itself is the closed hole the tokenizer docstring warns
        # about (module docstring "Detection shape"), so re-using it against
        # PowerShell input would be exactly the guess this plan forbids.
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
    # Heredoc bodies are stdin DATA, not shell command text -- strip them
    # before scanning, same as every sibling guard in this package (2026-
    # 07-29 incident fix; see e.g. `check_test_suite_invocation.py`'s own
    # `_strip_heredoc_bodies` call). Without this, a heredoc body merely
    # CONTAINING the string "ps -p 1234" as data (a probe script, a
    # findings write-up) was denied as if it were a live liveness probe.
    cmd = _strip_heredoc_bodies(cmd)

    if os.environ.get(_OVERRIDE_ENV, "0") == "1":
        return None

    masked = _mask_quoted_arguments(cmd)
    start = 0
    bounds = [(m.start(), m.end()) for m in _SEGMENT_SPLIT_RE.finditer(masked)]
    bounds.append((len(masked), len(masked)))
    for seg_end, next_start in bounds:
        segment, raw_segment = masked[start:seg_end], cmd[start:seg_end]
        start = next_start
        if not segment.strip():
            continue
        idiom = _segment_has_raw_pid_liveness_idiom(segment, raw_segment)
        if idiom is not None:
            return _deny_record(idiom, payload)

    return None
