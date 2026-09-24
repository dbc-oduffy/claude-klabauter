"""coordinator_core.write_guards.nudge_unattributed_process_time_figure --
advisory guard.

New module (no `.sh` reference; see INTERFACE.md's provenance rule, which
this docstring satisfies with a spec backlink instead of a `.sh` path).

Spec backlink: docs/plans/2026-09-22-spawn-budget-and-census.md, row C7
("R5: advisory write guard -- a written process-time figure that names no
instrument gets a one-line nudge to the measure CLI"). The detector regexes
below are lifted verbatim from the plan's own census row 4 (row's `body`,
"Among state/{bug,debt}-backlog, ... how many files state a process-time
figure, and how many of those name no sanctioned primitive?") -- 1405 files
scanned, 70 stated a process-time figure, 43 of those named no sanctioned
primitive. That count sizes C8's grandfather set, not this guard's own
behavior.

Purpose: a budget/perf figure ("143ms process time", "process time of
210ms") written into the six prefixes below with no named instrument is
unattributed -- the reader cannot tell whether it came from the sanctioned
primitive, a bare `time.time()`, or hand-typing. This guard nudges the
writer toward `python -m coordinator_core.benchmarks.measure` (C6) at the
moment of write, the cheap correct path, per the row's own rejected-shape
(c): pattern matching over prose cannot classify exactly, so this is
advisory, never a hard-deny -- the corpus ratchet (C8) is the hard edge.

Fires when BOTH hold:
  (i) the write's target path (resolved against the write's own repo root)
      is under one of the six prefixes: state/bug-backlog/,
      state/debt-backlog/, state/improvement-queue/, state/baselines/,
      docs/research/, docs/problems/.
  (ii) the RESULTING text (post-write) matches the process-time-figure
       pattern while naming none of the six sanctioned-primitive names.

"Resulting text": Write's `content` is the whole post-write body outright.
Edit/MultiEdit apply their fragment(s) to the on-disk pre-image, read ONCE
(never a spawn) -- mirrors `nudge_handoff_ac_shape._resulting_body`
byte-for-byte in shape (same near-duplicate left un-extracted there for the
same PreToolUse hot-path reason; not re-extracted here either). A stale
`old_string` (no match in the pre-image) degrades to no-fire, same as that
sibling -- this guard never guesses at a body it cannot construct.

Negative-spec:
  - Does NOT hard-deny -- CLASS = "advisory" throughout; the write always
    lands (INTERFACE.md envelope: `additionalContext` only, no
    `permissionDecision`).
  - Does NOT fire on a bar citation ("the 500ms bar") -- the figure pattern
    requires "process"/"process-time" adjacency, which a bare bar number
    lacks.
  - Does NOT fire outside the six prefixes, resolved against the write's
    OWN repo root (`_repo_root.resolve_repo_root`) -- an unresolvable root
    fails open (no scope, no advisory), same discrimination shape
    `wiki_changelog_prose_advisory.py` uses for its own repo-root scoping.
  - Does NOT fire when the resulting text already names one of the six
    sanctioned primitives, however far from the figure.
  - Does NOT distinguish subagent vs. main-session payloads -- the same
    check runs either way (per the row's own test list, "a subagent
    payload behaves identically").
  - Does NOT spawn a subprocess and does NOT read stdin -- the pre-image
    read is a single bounded `Path.read_text` call, capped by
    `_MAX_WHOLE_FILE_BYTES` the same as `nudge_handoff_ac_shape.py`.
  - Never raises: any unexpected input shape or internal error is treated
    as ALLOW/no-op (fail-open on error), matching this package's advisory
    convention.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, Optional

from coordinator_core.ops._path_guard import contained_path
from coordinator_core.write_guards._repo_root import resolve_repo_root

CLASS = "advisory"
MATCHERS = ["Write", "Edit", "MultiEdit"]
PRIORITY = 223  # advisory band; next slot after wiki_changelog_prose_advisory (222)

#: Bounded pre-image read, same cap `nudge_handoff_ac_shape.py` uses.
_MAX_WHOLE_FILE_BYTES = 256 * 1024

#: The six prefixes named in the row's body, repo-root-relative,
#: forward-slash, trailing slash included for a clean startswith check.
_SCOPED_PREFIXES = (
    "state/bug-backlog/",
    "state/debt-backlog/",
    "state/improvement-queue/",
    "state/baselines/",
    "docs/research/",
    "docs/problems/",
)

#: Lifted verbatim from the plan's census-row-4 detector (docs/plans/
#: 2026-09-22-spawn-budget-and-census.md, the `fig` regex).
_FIGURE_RE = re.compile(
    r"\d+(?:\.\d+)?\s?ms\s+(?:of\s+)?process\b"
    r"|process[ -]time[^.\n]{0,40}?\d+(?:\.\d+)?\s?ms",
    re.IGNORECASE,
)

#: Lifted verbatim from the plan's census-row-4 detector (the `names`
#: regex) plus the row body's `benchmarks.measure` invocation name.
_NAMES_RE = re.compile(
    r"batched_process_time_ms|batched_process_time_quantiles"
    r"|single_invocation_tree_process_time|LiveTreeAccountant"
    r"|in_process_time_ms|benchmarks\.measure"
)

_ADVISORY_TEXT = (
    "Process-time figure with no named instrument. Measure with "
    "`python -m coordinator_core.benchmarks.measure -- <argv>` and paste "
    "its line."
)


def _resulting_body(
    tool_name: str, tool_input: Dict[str, Any], pre_image: Optional[str]
) -> Optional[str]:
    """The full post-write body text this tool call would produce, or
    ``None`` if it cannot be determined (fails open to silent). Mirrors
    `nudge_handoff_ac_shape._resulting_body` (see module docstring)."""
    if tool_name == "Write":
        content = tool_input.get("content")
        return content if isinstance(content, str) else None

    if pre_image is None:
        return None

    if tool_name == "Edit":
        old_s = tool_input.get("old_string")
        new_s = tool_input.get("new_string")
        if not isinstance(old_s, str) or not isinstance(new_s, str):
            return None
        if old_s not in pre_image:
            return None
        if tool_input.get("replace_all"):
            return pre_image.replace(old_s, new_s)
        return pre_image.replace(old_s, new_s, 1)

    if tool_name == "MultiEdit":
        edits = tool_input.get("edits")
        if not isinstance(edits, list):
            return None
        body = pre_image
        for edit in edits:
            if not isinstance(edit, dict):
                continue
            old_s = edit.get("old_string")
            new_s = edit.get("new_string")
            if not isinstance(old_s, str) or not isinstance(new_s, str):
                continue
            if old_s not in body:
                continue
            if edit.get("replace_all"):
                body = body.replace(old_s, new_s)
            else:
                body = body.replace(old_s, new_s, 1)
        return body

    return None


def check(payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    try:
        tool_name = payload.get("tool_name") or ""
        if tool_name not in ("Write", "Edit", "MultiEdit"):
            return None

        tool_input = payload.get("tool_input") or {}
        if not isinstance(tool_input, dict):
            return None

        file_path = tool_input.get("file_path")
        if not isinstance(file_path, str) or not file_path:
            return None

        cwd = payload.get("cwd") or None
        repo_root = resolve_repo_root(cwd)
        if not repo_root:
            # Fail-open: cannot establish the scope root.
            return None

        base_dir = Path(cwd) if cwd else Path.cwd()
        candidate_path = Path(file_path) if Path(file_path).is_absolute() else (base_dir / file_path)
        contained = contained_path(candidate_path, [Path(repo_root)])
        if contained is None:
            return None

        try:
            rel = contained.relative_to(Path(repo_root)).as_posix()
        except ValueError:
            return None

        if not any(rel.startswith(prefix) for prefix in _SCOPED_PREFIXES):
            return None

        pre_image: Optional[str]
        if tool_name == "Write":
            pre_image = None
        else:
            try:
                if contained.stat().st_size > _MAX_WHOLE_FILE_BYTES:
                    pre_image = None
                else:
                    pre_image = contained.read_text(encoding="utf-8", errors="replace")
            except OSError:
                pre_image = None

        body = _resulting_body(tool_name, tool_input, pre_image)
        if body is None:
            return None

        if not _FIGURE_RE.search(body):
            return None

        if _NAMES_RE.search(body):
            return None

        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "additionalContext": _ADVISORY_TEXT,
            }
        }
    except Exception:
        # Fail-open on any unexpected error -- advisory convention.
        return None
