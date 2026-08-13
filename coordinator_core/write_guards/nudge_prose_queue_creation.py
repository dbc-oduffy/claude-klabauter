"""coordinator_core.write_guards.nudge_prose_queue_creation — advisory guard.

Spec: example-doctrine-repo docs/decisions/DR-115-queue-shape-is-a-scope-collision-not-a-staleness.md
part 3 ("Row 4's guard: creation-deny, append-silent") — the deny half of
that ruling. Its sibling module, ``nudge_prose_queue_append.py``, carries the
append half, which DR-115's later § PM direction (A) amended from silent to
advisory; this module (the creation half) is unaffected by that amendment
and stands exactly as part 3 specified.

Background (DR-115 § Descriptive verdict): the structured-queue directories
(``state/{bug,debt,improvement}-.../*.yaml``) are the only shape
``records_query`` and every ceremony downstream of it (depth counts, triage,
Cockpit) can see. A legacy single-markdown-file form
(``improvement-queue.md``, ``bug-backlog.md``, ``debt-backlog.md``) survives
in eight sibling repos as an unmigrated shape with live producers — not a
harmless remnant. The writer CLI, ``coordinator-queue-append``, cannot
produce that legacy shape at all; every new instance of it is a hand-rolled
regression against tooling that moved on release generations ago. DR-115
draws the line at CREATION specifically: a repo already maintaining its own
prose queue keeps working (see the append guard for what changed there
under PM direction); a genuinely NEW prose queue file has no legitimate
justification anywhere in the fleet, which is what makes this the one
half of the pair that stays a hard deny rather than an advisory.

CLASS is "advisory" per DR-277 (2026-08-06 guard-class census;
``docs/decisions/DR-277-guards-are-advisory-by-default-two-named.md``) — was
"hard-deny" at PRIORITY 119. This is a class flip, not a rename: DR-115's
detection reasoning below (existence check makes the false-positive rate
structurally zero) is unchanged and still sound, but DR-277 supersedes
DR-115 part 3's "Deny, not advisory" call as the fleet-wide default. PRIORITY
stays 119 — the slot survives unchanged because 119/120 already described
themselves as advisory-band positions, not hard-deny-specific
(``docs/wiki/write-guard-priority-bands.md``). On a positive match this now
returns the ``additionalContext`` advisory envelope, never
``permissionDecision: deny`` — the write proceeds, with the redirect-to-CLI
offer surfaced alongside it. On any internal error (a failed existence
check, an unresolvable path, an unexpected payload shape) it fails OPEN, as
before.

DETECTION — three gates, all required:
  1. ``MATCHERS = ["Write"]`` only, never ``Edit``/``MultiEdit`` — DR-115:
     "Creating a prose queue is a ``Write``; appending to one is an
     ``Edit``. That single choice does most of the scoping work."
  2. Path gate: the ``/``-normalized, case-folded target basename matches
     the three named legacy queues (``improvement-queue.md``,
     ``bug-backlog.md``, ``debt-backlog.md``) or the sibling
     ``<bug|debt|improvement>-queue.md`` / ``<bug|debt|improvement>-backlog.md``
     shape (the other suffix for the same three known families), under a
     ``state/`` path segment, and NEVER under an ``archive/`` segment (a
     queue file that has already been closed out via ``git mv`` to
     ``archive/<queue>/<YYYY-MM>/`` is not a live creation target). This is
     deliberately scoped to the three named families, NOT an open wildcard
     match on any ``*-queue.md``/``*-backlog.md`` basename — an unrelated
     file like ``state/release-backlog.md`` must pass silently.
  3. Existence gate: the target path does not already exist on disk. This
     is what makes the deny defensible — creating a brand-new legacy queue
     is unambiguous; denying writes to one of the sixteen that already
     exist fleet-wide would fight repos maintaining their own queues and
     earn a bypass within a day (DR-115 part 3).
  4. Content gate: the payload's ``content`` contains at least one line
     matching the dated pipe-row shape (``_ENTRY_LINE_RE``, reused verbatim
     from ``nudge_improvement_queue_write`` rather than re-derived, so the
     one line-shape definition cannot drift into two) — a markdown file at
     a queue-ish path that is a README or a tombstone note is not a queue
     and must pass silently.

PRIORITY = 119, one slot BELOW ``nudge_improvement_queue_write`` (120) and
now within the advisory phase (``docs/wiki/write-guard-priority-bands.md``).
This is a deliberate ordering choice, not an arbitrary adjacent slot: both
guards can match the SAME event — a ``Write`` creating a brand-new
``improvement-queue.md`` with dated rows and no ``justification:`` line
matches this guard's creation gate AND ``nudge_improvement_queue_write``'s
path glob. That guard offers a content-based escape (a ``justification:``
line lets the write through); this guard offers no such escape, because
DR-115 is unconditional about creation — there is no legitimate reason to
create a new line-per-row queue anywhere in the fleet now, escape hatch or
not. The advisory phase shows at most one message and the first non-None
return wins, so this guard MUST still run first, or the creation-specific
offer would never surface ahead of the append guard's own message on the
same event. Running this guard first preserves that; the two guards
otherwise target disjoint events (this one only fires on
Write-of-nonexistent-target, the sibling only fires past that point) so
there is no other ordering interaction between them. Both numbers carry
across the phase boundary unchanged because 119 and 120 already sorted
ahead of every other existing advisory that could plausibly co-match a
legacy-queue write (checked per ``docs/wiki/write-guard-priority-bands.md``'s
same-surface collision rule; the nearest lower advisory,
``guard_concrete_path_citations`` at 111, does not match these paths).

Negative-spec:
  - Does NOT fire on ``Edit``/``MultiEdit`` under any condition — appending
    to an existing prose queue is the sibling advisory's job, never this
    module's.
  - Does NOT fire when the target already exists on disk, regardless of
    content — an existing legacy queue being appended-to via a whole-file
    ``Write`` (some tools rewrite the whole file) is not a NEW queue.
  - Does NOT fire under ``archive/`` — a closed-out queue is not a creation
    target.
  - Does NOT fire on a queue-path-shaped file with no dated pipe-row in the
    new content — a README or a tombstone note at that path passes.
  - Does NOT read stdin or spawn a subprocess; the one disk read is a
    single ``os.path.exists`` check, Windows-safe via ``pathlib``.
  - Never raises: any unexpected input shape, or a failed existence check,
    returns ``None`` (ALLOW/no-op).
  - Does NOT return ``permissionDecision: "deny"`` — ``CLASS = "advisory"``
    per DR-277; the write always proceeds, even on a positive match.

Spec backlink: example-doctrine-repo docs/decisions/DR-115-queue-shape-is-a-scope-collision-not-a-staleness.md
"""

from __future__ import annotations

import os
import re
from pathlib import PurePosixPath
from typing import Any, Dict, Optional

from coordinator_core.bash_guards._helpers import operator_override_note
from coordinator_core.write_guards.nudge_improvement_queue_write import _ENTRY_LINE_RE

CLASS = "advisory"  # DR-277 -- was "hard-deny" at PRIORITY 119; slot unchanged, not re-slotted.
MATCHERS = ["Write"]
PRIORITY = 119  # one slot below nudge_improvement_queue_write (120) -- see module docstring

#: Operator-only escape hatch, following the block_* flag-shaped convention
#: (this guard has no content-based escape by design -- see module
#: docstring; the only override is a human launching the harness).
_OVERRIDE_ENV_VAR = "COORDINATOR_OVERRIDE_PROSE_QUEUE_CREATION"

#: Named legacy queue basenames, case-folded.
_NAMED_QUEUE_BASENAMES = frozenset(
    {"improvement-queue.md", "bug-backlog.md", "debt-backlog.md"}
)

#: Generic sibling shape: catches the OTHER queue/backlog suffix for each of
#: the three known families (e.g. `bug-queue.md`, `improvement-backlog.md`)
#: that the three named basenames above don't already cover. Deliberately
#: scoped to the three family stems, NOT an open `[\w.-]*` wildcard --
#: review found the open form hard-denies unrelated files like
#: `state/release-backlog.md` that have nothing to do with DR-115's legacy
#: bug/debt/improvement shape (code-reviewer finding, 2026-07-31).
_GENERIC_QUEUE_RE = re.compile(
    r"^(?:bug|debt|improvement)-(?:queue|backlog)\.md$", re.IGNORECASE
)


def _is_queue_shaped_basename(basename: str) -> bool:
    lowered = basename.lower()
    if lowered in _NAMED_QUEUE_BASENAMES:
        return True
    return bool(_GENERIC_QUEUE_RE.match(basename))


def _family_from_basename(basename: str) -> str:
    """Derive the ``--schema`` name ``coordinator-queue-append`` expects
    from a legacy queue basename (e.g. ``bug-backlog.md`` -> ``bug-backlog``,
    ``improvement-queue.md`` -> ``improvement-queue``)."""
    stem = basename[:-3] if basename.lower().endswith(".md") else basename
    return stem


def _extract_file_path(payload: Dict[str, Any]) -> str:
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        return ""
    file_path = tool_input.get("file_path")
    return file_path if isinstance(file_path, str) else ""


_REASON_TEMPLATE = """OFFER: New legacy queue files are invisible to records_query and downstream ceremonies (depth, triage, cockpit). Use instead: `coordinator-queue-append --schema {family}`, which writes `state/{family}/<date>-<slug>.yaml`.{override_block}"""


def check(payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    try:
        tool_name = payload.get("tool_name") or ""
        if tool_name != "Write":
            return None

        if os.environ.get(_OVERRIDE_ENV_VAR, "0") == "1":
            return None

        file_path = _extract_file_path(payload)
        if not file_path:
            return None

        normalized = file_path.replace("\\", "/")
        while "//" in normalized:
            normalized = normalized.replace("//", "/")

        segments = [seg for seg in normalized.split("/") if seg]
        if not segments:
            return None
        if any(seg.lower() == "archive" for seg in segments):
            return None
        if not any(seg.lower() == "state" for seg in segments):
            return None

        basename = PurePosixPath(normalized).name
        if not _is_queue_shaped_basename(basename):
            return None

        # --- existence gate: target absent -> creation; target present ->
        #     an existing legacy repo maintaining its own queue, silent ---
        resolved = file_path
        if not os.path.isabs(resolved) and not re.match(r"^[A-Za-z]:[\\/]", resolved):
            cwd = payload.get("cwd")
            if isinstance(cwd, str) and cwd:
                resolved = os.path.join(cwd, file_path)
        try:
            if os.path.exists(resolved):
                return None
        except Exception:
            return None

        tool_input = payload.get("tool_input") or {}
        if not isinstance(tool_input, dict):
            return None
        content = tool_input.get("content")
        if not isinstance(content, str):
            return None

        if not _ENTRY_LINE_RE.search(content):
            return None

        family = _family_from_basename(basename)
        _note = operator_override_note(_OVERRIDE_ENV_VAR, payload=payload)
        reason = _REASON_TEMPLATE.format(
            family=family,
            override_block=("\n\n" + _note if _note else ""),
        )

        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "additionalContext": reason,
            }
        }
    except Exception:
        # Fail-OPEN on any unexpected error -- this guard advises ONLY on a
        # positive creation match, never on an error.
        return None
