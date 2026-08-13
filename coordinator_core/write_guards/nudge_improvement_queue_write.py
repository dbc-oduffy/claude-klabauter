"""coordinator_core.write_guards.nudge_improvement_queue_write — advisory guard.

Originally an engine-ification of coordinator-claude's retired
``coordinator/hooks/scripts/nudge-improvement-queue-write.sh`` PreToolUse
(Write|Edit|MultiEdit) hook (deleted 2026-07-16, coordinator-claude ``2f8b8450``). That
fidelity claim is now VOID (2026-07-30 escape-mechanism rework): coordinator-claude no
longer ships that reference hook at all, and ``COORDINATOR_QUEUE_PUNT``
appears nowhere in their tree. Claude-klabauter is the sole owner of this guard going
forward; there is no upstream parity left to preserve, and no future reader
should go looking for one. What follows describes THIS module's own,
independent design.

Motivation (unchanged from the reference hook): the improvement queue is
empirically a laundromat for reflexive deferral — a small structural issue
hit mid-flight gets typed as a queue entry instead of fixed. This guard adds
tool-boundary friction on every queue-append.

WHY THE MECHANISM CHANGED: the reference hook's only offered escape was
``COORDINATOR_QUEUE_PUNT="<non-trivial reason>"``, an env var read from
``os.environ`` in a fresh PreToolUse subprocess spawned per event (payload
via stdin, never shell-executed). No agent-invoked write can ever set an env
var that subprocess will see — pre-launch only, same unreachability
boundary every other guard in this package documents (see
``docs/reference/guard-override-keys.md`` § Security context). That made
this guard a hard wall with a painted-on door: every NEW queue entry got
denied, and the one offered exit could not be taken by the agent it was
denying. THE ESCAPE NOW LIVES IN THE ARTIFACT INSTEAD: a queue entry that
carries its own non-trivial ``justification:`` line — why this is queued
instead of fixed now — is let through. The justification permanently
travels with the entry (the next reader finds it there), rather than dying
at the process boundary the way the env var did. ``COORDINATOR_QUEUE_PUNT``
is still honored (a human operator launching the harness can still set it),
but it is no longer the advertised escape — the deny text leads with the
content-based route and only mentions the env var via
``operator_override_note`` alongside every other guard's override mention.

Preserved unchanged from the pre-rework version: the ``*improvement-queue.md``
/ ``*state/improvement-queue/*.yaml`` file-path match gate, the legacy-prose
entry-count gate (pruning/reformat passes on ``*improvement-queue.md`` are
silently allowed; YAML field-edits are always silently allowed), the
active-authoring-skill transcript-tail suppression, the non-triviality
standard (2026-07-30: consolidated into, and tightened in,
``coordinator_core.bash_guards._helpers.is_trivial_reason`` — see that
module), and the fail-OPEN-on-internal-error contract.

The five-question friction block is preserved verbatim in content but no
longer rendered inline on every firing — it now lives at
``docs/reference/queue-admission-five-questions.md`` (same cut the rest of
the guard cohort got on 2026-07-30: the deny text keeps only what a reader
needs AT DECISION TIME — the one concrete action they can take next — behind
a pointer to the full detail).

CLASS is "advisory" (2026-08-06 guard-class-census flip, DR-277): on a
positive match (queue write with no non-trivial justification anywhere in
the payload) it returns the ``additionalContext`` envelope instead of
denying; on any internal error it fails OPEN (returns None) — it never
blocks the write either way now. PRIORITY stays 120 unchanged (class flip
only, no re-slot — no lower-numbered advisory co-matches this guard's
``*improvement-queue.md`` / ``*state/improvement-queue/*.yaml`` surface).
The ``COORDINATOR_QUEUE_PUNT`` reason-shaped escape hatch described above is
PRESERVED as-is: it still allows silently when non-trivial and still falls
through to the advisory (now, not a deny) when trivial or absent. This is
the door in the "hard wall with a painted-on door" becoming real: the same
content-based ``justification:`` escape is now paired with an advisory that
can actually be acted on by the agent it is nudging, instead of a deny that
agent could never reach.

Negative-spec:
  - Does NOT replicate the retired reference hook's claude-klabauter fast-path
    re-dispatch (this module runs inside claude-klabauter's own engine; re-dispatching
    to claude-klabauter from here would be a self-call).
  - Does NOT apply the pipe-row entry-count gate to ``*.yaml`` paths — a
    field-level Edit to an existing YAML entry is legitimately lower-friction
    than a Write (no new queue entry is being created); only the legacy
    ``*improvement-queue.md`` prose form is entry-counted.
  - Does NOT read stdin, read disk, or spawn a subprocess — this module has
    no side-effects and no I/O beyond the transcript-tail suppression's
    existing best-effort read; the justification check is pure
    payload -> Optional[envelope].
  - Does NOT invent a second non-triviality standard for the justification
    field, and does NOT carry its own local copy of one — it imports
    ``coordinator_core.bash_guards._helpers.is_trivial_reason``, the same
    shared predicate ``write_guards.nudge_baton_body_bar`` imports, so the
    one standard the env-var escape and the content-based escape both use
    cannot drift into two.
  - Never raises: any unexpected input shape returns ``None`` (ALLOW/no-op).

Spec backlink: docs/wiki/coordinator-tripwires.md (improvement-queue admission rule)
Five-question detail: docs/reference/queue-admission-five-questions.md
Override-key reference: docs/reference/guard-override-keys.md
"""

from __future__ import annotations

import fnmatch
import os
import re
from collections import deque
from typing import Any, Dict, Optional

from coordinator_core.bash_guards._helpers import (
    is_trivial_reason as _is_trivial_reason,
    operator_override_note,
)

CLASS = "advisory"
MATCHERS = ["Write", "Edit", "MultiEdit"]
PRIORITY = 120  # unchanged (class flip only, no re-slot -- no lower-numbered advisory co-matches)

#: Operator-only escape hatch, kept for a human launching the harness.
#: No longer the advertised route — see module docstring.
_ESCAPE_HATCH_ENV_VAR = "COORDINATOR_QUEUE_PUNT"

#: File-path match gate.
_QUEUE_PATH_GLOBS = ("*state/improvement-queue/*.yaml", "*improvement-queue.md")

#: Legacy prose entry-line pattern.
_ENTRY_LINE_RE = re.compile(r"^- \d{4}-\d{2}-\d{2} \|", re.MULTILINE)

#: Active-authoring-skill transcript-tail suppression.
_AUTHORING_SKILL_RE = re.compile(
    r"(^|[^a-z])/(learn-lessons|workweek-complete|workday-complete|workstream-complete"
    r"|distill|update-docs|bug-blitz|mise-en-place)([^a-z]|$)"
    r"|coordinator:(learn-lessons|workweek-complete|workday-complete|workstream-complete"
    r"|distill|update-docs|bug-blitz|mise-en-place)"
    r"|<command-name>(learn-lessons|workweek-complete|workday-complete|workstream-complete"
    r"|distill|update-docs|bug-blitz|mise-en-place)</command-name>"
)
_TRANSCRIPT_TAIL_LINES = 500

_QUEUE_LABEL = "improvement queue"

#: The content-based escape: a `justification:` line anywhere in the NEW
#: content being written (a YAML top-level field for the *.yaml form, or a
#: plain "justification: ..." line for the legacy prose bullet form — this
#: guard does not require either form's schema to formally declare the key,
#: it only reads the payload text directly). Case-insensitive; tolerates an
#: optional leading bullet marker for the prose form. When more than one
#: match is present (e.g. a MultiEdit touching several lines), the LAST
#: match wins — the most recently written value is what will actually land.
_JUSTIFICATION_RE = re.compile(r"(?im)^\s*(?:[-*]\s*)?justification:\s*(.+?)\s*$")

#: Full five-question self-check + hard-forbidden-writes rule, relocated out
#: of the inline deny text (2026-07-30 cut) so the deny message itself stays
#: short. The content there is otherwise unchanged from what used to render
#: inline.
_FIVE_QUESTIONS_DOC = "docs/reference/queue-admission-five-questions.md"

_TRIVIAL_JUSTIFICATION_HINT = """
[hook] Found a `justification:` line but its value was trivial ("1", "ok",
[hook] "yes", under 12 chars, or degenerate filler like "aaaaaaaaaaaa" /
[hook] "abababababab" / an all-digit string with no letters at all) --
[hook] write an actual sentence, e.g.:
[hook]   justification: genuinely cross-cutting, fix needs separate plan
"""

_TRIVIAL_PUNT_HINT = """
[hook] The operator-only env fallback was also set but its value was
[hook] trivial -- add the `justification:` line above instead.
"""

_REASON_TEMPLATE = """No reason given for this {queue_label} entry ({file_path_norm}). Example:
`justification: <one-sentence reason>` in the entry, then rerun.
Always-forbidden cases: {five_q_doc}
{legacy_prose_note}{hint}{override_block}"""

#: Appended when the matched path is the legacy `.md` prose form (DR-115 §
#: PM direction (B)) -- the escape instruction above stays YAML-only; this
#: line names the shape and the exit instead of coaching a hand-written
#: pipe-row bullet into an artifact we are trying to leave. Not yet landed
#: as of this module's authoring -- see nudge_prose_queue_append.py for the
#: forthcoming transformer path, verified absent from disk there.
_LEGACY_PROSE_NOTE = """This file is an unmigrated prose queue -- new entries belong in
state/{queue_dir}/*.yaml. To convert: coordinator_core/ops/fleet/migrate_prose_queue.py (planned, not yet landed).
"""


def _extract_str(tool_input: Dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = tool_input.get(key)
        if value:
            return str(value)
    return ""


def _tail_lines(path: str, n: int) -> str:
    """Best-effort last-N-lines read; any failure yields "" (fail-open)."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return "".join(deque(fh, maxlen=n))
    except OSError:
        return ""


def _strip_wrapping_quotes(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
        return value[1:-1]
    return value


def _extract_justification(text: str) -> str:
    """Return the last ``justification:`` value found in ``text``, or ``""``
    if none is present. Reads the RAW WRITE PAYLOAD text — no disk I/O."""
    matches = _JUSTIFICATION_RE.findall(text or "")
    if not matches:
        return ""
    return _strip_wrapping_quotes(matches[-1].strip())


def _gather_new_content(tool_name: str, tool_input: Dict[str, Any]) -> str:
    """Return the text this write is actually ADDING, so the justification
    scan only ever sees content this very tool call is introducing:
      - Write: the ``content`` param (whole new file).
      - Edit: the ``new_string`` param (the replacement text).
      - MultiEdit: every edit's ``new_string``, joined -- a justification
        line landing in ANY of the edits in this one call counts, since a
        single MultiEdit call is one atomic write.

    ACCEPTED TRADE (Review: code-reviewer P2, 2026-07-30): because the join
    is call-scoped, not edit-scoped, a ``justification:`` line attached to
    one edit in a MultiEdit call satisfies the escape for a CO-OCCURRING,
    unrelated, unjustified entry elsewhere in the same call -- an agent
    could in principle pair a real justified change with a drive-by
    unjustified queue entry in one call and both would pass. Kept
    deliberately (the "one atomic write" rationale above), not fixed,
    because per-edit scoping would need to know which edit actually
    introduced which entry -- a heavier parse than this guard's payload-only,
    no-disk-read contract affords. Pinned by
    ``tests/test_nudge_improvement_queue_write.py::
    TestMultiEditJustificationScopeIsWholeCall`` so this is legible as a
    choice, not an unexercised gap.
    No disk read in any branch."""
    if tool_name == "MultiEdit":
        edits = tool_input.get("edits")
        if not isinstance(edits, list):
            return ""
        parts = []
        for edit in edits:
            if isinstance(edit, dict):
                new_string = edit.get("new_string")
                if isinstance(new_string, str):
                    parts.append(new_string)
        return "\n".join(parts)
    return _extract_str(tool_input, "content", "new_string")


def check(payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    try:
        tool_name = payload.get("tool_name") or ""
        if tool_name not in ("Write", "Edit", "MultiEdit"):
            return None

        # --- operator-only escape hatch (still honored, no longer advertised) ---
        punt_reason = os.environ.get(_ESCAPE_HATCH_ENV_VAR, "") or ""
        punt_trivial = False
        if punt_reason:
            if not _is_trivial_reason(punt_reason):
                return None
            punt_trivial = True

        tool_input = payload.get("tool_input") or {}
        if not isinstance(tool_input, dict):
            return None

        file_path = tool_input.get("file_path") or ""
        if not file_path:
            return None

        # MultiEdit doesn't expose a single new_string for entry-line
        # counting; treat like Write (any MultiEdit to a queue file gets the
        # nudge, subject to the justification-content check below).
        effective_tool_name = "Write" if tool_name == "MultiEdit" else tool_name

        file_path_norm = file_path.replace("\\", "/")

        if not any(fnmatch.fnmatchcase(file_path_norm, g) for g in _QUEUE_PATH_GLOBS):
            return None

        # --- Edit-only entry-count / field-edit gate ---
        if effective_tool_name == "Edit":
            if fnmatch.fnmatchcase(file_path_norm, "*improvement-queue.md"):
                new_string = _extract_str(tool_input, "new_string", "content")
                old_string = tool_input.get("old_string") or ""
                new_entries = len(_ENTRY_LINE_RE.findall(new_string))
                old_entries = len(_ENTRY_LINE_RE.findall(old_string))
                if new_entries <= old_entries:
                    return None
            elif fnmatch.fnmatchcase(file_path_norm, "*state/improvement-queue/*.yaml"):
                return None

        # --- active-authoring-skill transcript-tail suppression ---
        transcript_path = payload.get("transcript_path") or ""
        if transcript_path and os.path.isfile(transcript_path):
            recent_tail = _tail_lines(transcript_path, _TRANSCRIPT_TAIL_LINES)
            if recent_tail and _AUTHORING_SKILL_RE.search(recent_tail):
                return None

        # --- content-based escape: a non-trivial justification travels
        #     with the entry itself, permanently, instead of dying at the
        #     process boundary the way the env var did ---
        new_content = _gather_new_content(tool_name, tool_input)
        justification = _extract_justification(new_content)
        justification_trivial = bool(justification) and _is_trivial_reason(justification)
        if justification and not justification_trivial:
            return None

        hint = ""
        if justification_trivial:
            hint = _TRIVIAL_JUSTIFICATION_HINT
        elif punt_trivial:
            hint = _TRIVIAL_PUNT_HINT

        legacy_prose_note = ""
        if fnmatch.fnmatchcase(file_path_norm, "*improvement-queue.md"):
            legacy_prose_note = _LEGACY_PROSE_NOTE.format(queue_dir="improvement-queue")

        # Review: code-reviewer P1 -- COORDINATOR_QUEUE_PUNT is
        # reason-shaped, not flag-shaped; its own _is_trivial_reason
        # denylists the literal "1", so the default VAR=1 render would be
        # refused by the very guard printing it. reason_placeholder
        # renders the correct VAR="<reason>" syntax instead.
        _note = operator_override_note(
            _ESCAPE_HATCH_ENV_VAR,
            payload=payload,
            reason_placeholder="<one-sentence reason>",
        )
        reason = _REASON_TEMPLATE.format(
            queue_label=_QUEUE_LABEL,
            five_q_doc=_FIVE_QUESTIONS_DOC,
            hint=hint,
            legacy_prose_note=legacy_prose_note,
            file_path_norm=file_path_norm,
            override_block=("\n" + _note if _note else ""),
        )

        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "additionalContext": reason,
            }
        }
    except Exception:
        # Fail-OPEN on any unexpected error -- this guard denies ONLY on a
        # positive match, never on an error.
        return None
