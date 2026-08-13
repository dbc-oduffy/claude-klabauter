"""coordinator_core.write_guards.nudge_baton_body_bar — advisory guard.

Spec: docs/plans/2026-07-23-queue-triage-terminus-ops.md § C9

Motivation: a baton (handoff) whose body is nothing but a bare row-list — a
markdown table or a flat bullet/numbered list with no authored prose around
it — hands the next session a manifest with no "why." The next reader gets
rows, not a baton: no stated goal, no decisions made, no next-step framing.
This guard offers the shape a GOOD baton body has instead of merely refusing
the bad one, per the design-as-offers convention (a nudge leads with the
better alternative, not the violation).

This is a NEW guard, not a port of a coordinator-claude reference `.sh` hook — there is no
`coordinator/hooks/scripts/nudge-baton-body-bar.sh` predecessor. It follows
the module-shape convention set by
`coordinator_core/write_guards/nudge_improvement_queue_write.py` (constants
naming, escape-hatch trivial-reason gate, fail-open contract) and the
envelope shape of `coordinator_core/write_guards/nudge_windows_subprocess_popup.py`
(CLASS is "advisory" — this guard OFFERS via `additionalContext`, it never
denies; a guard that only blocks is the mistrust shape this deliberately is
not).

DETECTION is deliberately narrow: it fires only when EVERY non-blank,
non-heading line of the baton's body is a table row or a list item — i.e.
zero authored prose anywhere in the body, not merely "mostly rows." A false
positive on a legitimate body (rows with a paragraph of context above/below/
between them) is worse than a miss, so any single prose line anywhere in the
body suppresses the advisory. `Write` sees the whole resulting file (frontmatter
delegated to `coordinator_core.frontmatter.primitives.split_frontmatter` — no
new frontmatter parser is authored here); `Edit`/`MultiEdit` reconstruct the
post-edit whole file by reading the on-disk content and re-applying the same
old_string -> new_string substitution(s), falling back to skipping the
advisory (never to firing it) on any read/reconstruction failure.

Negative-spec:
  - Does NOT deny/block anything — CLASS is "advisory"; the envelope carries
    only `additionalContext`, never `permissionDecision`.
  - Does NOT fire on `archive/handoffs/**` (archived batons are historical
    record, not an authoring target) — path gate is `state/handoffs/*.md`
    only.
  - Does NOT fire when the body is empty (nothing to nudge about) or when
    frontmatter cannot be split out (fail-open — this guard is not a
    frontmatter validator).
  - Does NOT count a markdown table's `|---|---|` separator row or a lone
    heading line as either "row" or "prose" — both are neutral and skipped.
  - Does NOT flag a body containing fewer than `_MIN_ROW_LINES` row-like
    lines — a two-line list is not the "bare row-bar" shape this targets.
  - Never raises: any unexpected input shape, read failure, or reconstruction
    failure returns ``None`` (ALLOW/no-op), never an advisory.

Escape hatch: ``COORDINATOR_BATON_BODY_PUNT`` — spelled the same way its
``COORDINATOR_QUEUE_PUNT`` precedent in nudge_improvement_queue_write.py
spelled its own. UNLIKE that precedent, this guard is advisory only: the
write always proceeds regardless of what this var holds, so there is
nothing to "re-run" the way the sibling's now-superseded escape text used to
suggest (that phrasing was wrong on this module twice over — the var is
pre-launch-only and unreachable from inside a session, AND the write it
would have been "re-run" for already landed unblocked the first time). What
the var actually does here: set before launch, with a non-trivial reason (at
least 12 chars), it suppresses this advisory on FUTURE writes to the same
kind of body. A human operator, not this agent, is the only one who can set
it — see ``docs/reference/guard-override-keys.md``. This guard does NOT gain
a content-based escape like its hard-deny sibling: there is no write to
unblock, so there is nothing for a content check to unblock.
"""

from __future__ import annotations

import fnmatch
import os
import re
from typing import Any, Dict, Optional

from coordinator_core.bash_guards._helpers import (
    is_trivial_reason as _is_trivial_reason,
    operator_override_note,
)
from coordinator_core.frontmatter.primitives import split_frontmatter

CLASS = "advisory"
MATCHERS = ["Write", "Edit", "MultiEdit"]
PRIORITY = 130  # deny-offer: runs after the structural block_* guards (≤90)

#: Escape-hatch env var, same shape as nudge_improvement_queue_write's
#: COORDINATOR_QUEUE_PUNT — a typed, non-trivial reason suppresses the offer.
_ESCAPE_HATCH_ENV_VAR = "COORDINATOR_BATON_BODY_PUNT"

#: Path gate — live batons only, not archived ones. Anchored to a `state/`
#: path-segment boundary (not a bare substring match) so a path like
#: `vendor/upstate/handoffs/notes.md` — which contains the literal substring
#: "state/handoffs/" but is not actually under a `state/handoffs/` directory —
#: does not false-positive. See `_matches_baton_path`.
_BATON_PATH_GLOB = "*/state/handoffs/*.md"
_BATON_PATH_PREFIX = "state/handoffs/"

#: Read cap for Edit/MultiEdit whole-file reconstruction — batons are small
#: markdown files; anything larger falls back to skipping the advisory.
_MAX_WHOLE_FILE_BYTES = 256 * 1024

#: Row-like lines: markdown table rows, bullets, or numbered list items.
_TABLE_ROW_RE = re.compile(r"^\s*\|.*\|\s*$")
_TABLE_SEP_RE = re.compile(r"^\s*\|?[\s:|-]+\|?\s*$")
_BULLET_RE = re.compile(r"^\s*[-*+]\s+\S")
_NUMBERED_RE = re.compile(r"^\s*\d+[.)]\s+\S")
_HEADING_RE = re.compile(r"^\s*#{1,6}\s")

_MIN_ROW_LINES = 3

# Review: code-reviewer — Finding 3 (P2): a bullet/numbered line matching
# _BULLET_RE/_NUMBERED_RE syntactically could be either a terse data row
# ("2026-07-23 | did thing one") or a full narrative bullet with real
# reasoning ("Decided to defer X because Y would break Z..."). Only the
# former is the bare-row-bar shape this guard targets. A coarse length +
# terminal-punctuation heuristic (no NLP) tells them apart — either signal is
# enough to call it prose, per false-positive-is-worse-than-a-miss.
_PROSE_LEN_THRESHOLD = 60
_TERMINAL_PUNCT_RE = re.compile(r"[.!?]\s*$")


def _bullet_or_numbered_is_prose(line: str) -> bool:
    """True if a bullet/numbered line reads as narrative prose, not a data row."""
    stripped = line.strip()
    if _TERMINAL_PUNCT_RE.search(stripped):
        return True
    return len(stripped) > _PROSE_LEN_THRESHOLD

_REASON = """OFFER: baton body is bare rows, no prose -- next reader gets a
manifest, not a baton. Add why: goal, decisions made, next step. Advisory
only, write already landed.{override_sentence}
"""

_TRIVIAL_HINT = """
[hook] The operator-only env fallback was set but its value was trivial
[hook] ("1", "ok", "yes", under 12 chars, or degenerate filler like
[hook] "aaaaaaaaaaaa" / "abababababab" / an all-digit string with no
[hook] letters at all) -- add authored prose to the body instead: goal,
[hook] decisions made, next step.
"""


def _matches_baton_path(file_path_norm: str) -> bool:
    """True iff `file_path_norm` is actually under a `state/handoffs/` dir.

    Anchored to a path-segment boundary — either an absolute/nested path with
    `/state/handoffs/` preceded by a separator, or a bare repo-relative path
    starting with `state/handoffs/` — so a substring coincidence like
    `vendor/upstate/handoffs/notes.md` does not match.
    """
    return fnmatch.fnmatchcase(file_path_norm, _BATON_PATH_GLOB) or file_path_norm.startswith(
        _BATON_PATH_PREFIX
    )


def _extract_str(tool_input: Dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = tool_input.get(key)
        if value:
            return str(value)
    return ""


def _read_file_safely(file_path: str) -> Optional[str]:
    try:
        if os.path.getsize(file_path) > _MAX_WHOLE_FILE_BYTES:
            return None
        with open(file_path, "r", encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except OSError:
        return None


def _apply_one_edit(
    content: str, old_string: Any, new_string: Any, replace_all: Any
) -> Optional[str]:
    if not isinstance(old_string, str) or not old_string:
        return None
    if old_string not in content:
        return None
    new_string = new_string if isinstance(new_string, str) else ""
    if replace_all:
        return content.replace(old_string, new_string)
    return content.replace(old_string, new_string, 1)


def _reconstruct_whole_file(
    tool_name: str, tool_input: Dict[str, Any], file_path: str
) -> Optional[str]:
    """Reconstruct the POST-EDIT whole file for Edit/MultiEdit; Write already
    carries the whole file. Returns None (skip the advisory) on any failure —
    never falls back to fragment-scoped detection, since a fragment cannot
    tell whether the REST of the body already has authored prose."""
    if tool_name == "Write":
        return _extract_str(tool_input, "content")

    current = _read_file_safely(file_path)
    if current is None:
        return None

    if tool_name == "Edit":
        return _apply_one_edit(
            current,
            tool_input.get("old_string"),
            tool_input.get("new_string"),
            tool_input.get("replace_all"),
        )

    if tool_name == "MultiEdit":
        edits = tool_input.get("edits")
        if not isinstance(edits, list) or not edits:
            return None
        for edit in edits:
            if not isinstance(edit, dict):
                return None
            result = _apply_one_edit(
                current,
                edit.get("old_string"),
                edit.get("new_string"),
                edit.get("replace_all"),
            )
            if result is None:
                return None
            current = result
        return current

    return None


def _classify_line(line: str) -> str:
    """Classify one body line as 'blank', 'neutral' (heading/table-separator),
    'row' (table row/bullet/numbered item), or 'prose' (anything else)."""
    if not line.strip():
        return "blank"
    if _HEADING_RE.match(line):
        return "neutral"
    if _TABLE_ROW_RE.match(line) and _TABLE_SEP_RE.match(line):
        return "neutral"
    if _TABLE_ROW_RE.match(line):
        return "row"
    if _BULLET_RE.match(line) or _NUMBERED_RE.match(line):
        return "prose" if _bullet_or_numbered_is_prose(line) else "row"
    return "prose"


def _is_bare_row_list(body: str) -> bool:
    """True iff the body has >= _MIN_ROW_LINES row-like lines and ZERO prose
    lines anywhere. A single prose line suppresses the advisory entirely —
    narrow-gate-first per the false-positive-is-worse-than-a-miss brief."""
    row_count = 0
    for raw_line in body.split("\n"):
        kind = _classify_line(raw_line)
        if kind == "prose":
            return False
        if kind == "row":
            row_count += 1
    return row_count >= _MIN_ROW_LINES


def check(payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    try:
        tool_name = payload.get("tool_name") or ""
        if tool_name not in ("Write", "Edit", "MultiEdit"):
            return None

        tool_input = payload.get("tool_input") or {}
        if not isinstance(tool_input, dict):
            return None

        file_path = tool_input.get("file_path") or ""
        if not file_path:
            return None
        file_path_norm = file_path.replace("\\", "/")

        if not _matches_baton_path(file_path_norm):
            return None

        whole_file = _reconstruct_whole_file(tool_name, tool_input, file_path)
        if not whole_file:
            return None

        split = split_frontmatter(whole_file)
        body = split.body_with_leading_newline if split is not None else whole_file
        if not body.strip():
            return None

        if not _is_bare_row_list(body):
            return None

        # --- escape hatch (same shape as COORDINATOR_QUEUE_PUNT) ---
        punt_reason = os.environ.get(_ESCAPE_HATCH_ENV_VAR, "") or ""
        trivial_reason = False
        if punt_reason:
            if not _is_trivial_reason(punt_reason):
                return None
            trivial_reason = True

        # Review: code-reviewer P1 -- COORDINATOR_BATON_BODY_PUNT is
        # reason-shaped, not flag-shaped; render VAR="<reason>", not the
        # default VAR=1 (which this guard's own _is_trivial_reason would
        # reject).
        _note = operator_override_note(
            _ESCAPE_HATCH_ENV_VAR,
            payload=payload,
            reason_placeholder="<one-sentence reason>",
        )
        _override_sentence = f" Silence future notes: {_note}" if _note else ""
        reason = _REASON.format(
            override_sentence=_override_sentence
        ) + (_TRIVIAL_HINT if trivial_reason else "")

        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "additionalContext": reason,
            }
        }
    except Exception:
        # Fail-OPEN on any unexpected error — this guard offers only on a
        # positive bare-row-list match, never on an error.
        return None
