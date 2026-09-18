"""Shared helper for PreToolUse write-guards that protect a named sentinel
file (a file whose mere ABSENCE gates a guarded capability) from being
created or modified through Write/Edit/MultiEdit/NotebookEdit.

Ported from DoE-claude `coordinator/hooks/scripts/_sentinel_write_guard.py`
per docs/plans/2026-09-18-doe-holds-no-scripts.md chunk W4-C3.

Why this exists
----------------
More than one guard gates a capability on a repo-root sentinel file's
absence (a doctrine-surface-edit approval marker, the git-worktree ban
marker). Any guard shaped this way has the same hole unless its sentinel is
ALSO protected on the file-write surface: an agent can `Write` the sentinel
into existence directly, bypassing whatever shell-level guard blocks
`touch`. This module makes closing that leg a two-line call instead of a
hand-copied basename check, so the next sentinel-gated guard is cheap to
protect rather than repeating the pattern -- and the bug -- by hand.

Ordering contract -- read before use
-------------------------------------
A caller that ALSO consults the sentinel's presence for an approval/allow
decision MUST call `sentinel_write_denial()` BEFORE that approval-state
lookup. Consulting approval state first and only then checking the target
path would let a currently-valid approval authorize an edit that EXTENDS or
RENEWS itself. A caller that has no such approval lookup at all (e.g. a
guard whose only job is protecting one sentinel) has no ordering hazard to
worry about -- there is nothing else in that script to order against.

Removal is intentionally out of scope here -- this module only knows how
to DENY a Write/Edit/MultiEdit/NotebookEdit call whose target is the
sentinel path. Deletion tools are not in the guarded matcher set, so
`rm`/deletion always stays available through other surfaces, and removing
a sentinel to re-lock its boundary remains the sanctioned recovery path
every sentinel-gated guard relies on.

`reconstruct_after` re-export
-------------------------------
ADAPTATION FROM THE PORTED SOURCE: DoE's copy resolved `reconstruct_after`
via a sibling-directory `sys.path` insert plus an `_engine_root`-resolved
cross-repo import into `coordinator_core.write_guards._sentinel_write_guard`
-- necessary there because the doctrine plane and the engine were separate
repos. This module now lives INSIDE the engine
(`coordinator_core/hooks/support/`), so that boundary crossing no longer
exists: `reconstruct_after` is imported directly from its one real
implementation, `coordinator_core.write_guards._sentinel_write_guard`, with
no path manipulation and no import-failure fallback stub -- an import that
cannot resolve inside this engine's own package is a packaging defect to
surface loudly, not a partial-deploy shape to degrade past.
"""

from __future__ import annotations

import os

from coordinator_core.write_guards._sentinel_write_guard import reconstruct_after

__all__ = [
    "extract_target_path",
    "is_sentinel_write",
    "sentinel_write_denial",
    "reconstruct_after",
]

_PATH_KEYS = ("file_path", "notebook_path", "path")


def extract_target_path(tool_input: dict) -> str:
    """Best-effort target path from a Write/Edit/MultiEdit/NotebookEdit
    `tool_input` payload, checking `_PATH_KEYS` in order. Returns "" if
    none of the recognized keys carry a non-empty string value.

    Deliberately permissive, not an oversight: a non-string value under a
    `_PATH_KEYS` entry (e.g. a list/dict, which no real Write/Edit/
    MultiEdit/NotebookEdit tool schema produces today) is treated as "no
    path" and falls through to the next key / the empty-string return,
    i.e. fail-open. That is the same fail-open posture every other
    unresolvable shape in this module takes -- there is nothing to protect
    against a payload shape the guarded tools cannot actually emit.
    """
    if not isinstance(tool_input, dict):
        return ""
    for key in _PATH_KEYS:
        val = tool_input.get(key, "")
        if isinstance(val, str) and val.strip():
            return val.strip()
    return ""


def is_sentinel_write(target_path: str, sentinel_name: str) -> bool:
    """True if `target_path` resolves to `sentinel_name`, case-folded.

    Two hardenings beyond a plain basename `==`:

    - Resolved before comparison via `os.path.realpath(os.path.abspath(...))`,
      so a write through a symlink whose own name is NOT the sentinel but
      which POINTS AT the sentinel path still matches. `os.path.realpath`
      on a not-yet-existing leaf component is safe -- it normalizes the
      path without raising, which matters here because the sentinel
      usually does not exist yet (that is the whole point of a guard that
      denies its creation).
    - Compared case-folded (`.lower()`), because the read side that grants
      the override this guard exists to prevent checks presence via
      `os.path.isfile()`, which is effectively case-insensitive on the
      fleet's primary hazard filesystem (macOS APFS, default
      case-insensitive-but-case-preserving). A case-varied write that a
      case-sensitive check here would silently allow still round-trips
      into a live override on that filesystem.

    Basename match only (never substring/prefix) -- a near-miss filename
    (e.g. a `-typo` suffix) or an unrelated file must never be caught by
    this check.

    Safe to call on a path a caller has already resolved itself:
    `os.path.realpath` is idempotent, so resolving an already-resolved
    absolute path a second time is a harmless no-op, not a correctness
    concern.
    """
    if not target_path:
        return False
    try:
        resolved = os.path.realpath(os.path.abspath(target_path))
        return os.path.basename(resolved).lower() == sentinel_name.lower()
    except Exception:
        return False


def sentinel_write_denial(
    target_path: str, sentinel_name: str, reason: str
) -> "dict | None":
    """Returns a PreToolUse deny `hookSpecificOutput` dict if `target_path`
    targets `sentinel_name`, else None.

    Caller is responsible for invoking this BEFORE any approval-state
    lookup that also consults the same sentinel (see module docstring,
    "Ordering contract").
    """
    if not is_sentinel_write(target_path, sentinel_name):
        return None
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }
