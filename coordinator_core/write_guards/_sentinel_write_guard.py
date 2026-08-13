"""Shared helper for PreToolUse write-guards that protect a named sentinel
file (a file whose mere ABSENCE gates a guarded capability) from being
created or modified through Write/Edit/MultiEdit/NotebookEdit.

Ported from coordinator-claude `coordinator/hooks/scripts/_sentinel_write_guard.py`
(faithful port -- see `write_guards/INTERFACE.md`). The underscore prefix is
load-bearing: `engine.py::_discover_guards()` explicitly skips modules whose
name starts with `_`, which is exactly why this is the correct home for it --
shared plumbing consumed by multiple guards, not itself a registered guard.

Why this exists
----------------
Two guards in this tree gate a capability on a repo-root sentinel file's
absence: `guard-doctrine-surface-edits.py` (`.coordinator-doctrine-edit-
approved`) and the git-worktree ban (`.coordinator-override-worktree-
guard`, enforced on the tool-call surface by `block-worktree-tool.py` /
`strip-worktree-isolation.py`). Any guard shaped this way has the same
hole unless its sentinel is ALSO protected on the file-write surface: an
agent can `Write` the sentinel into existence directly, bypassing whatever
shell-level guard blocks `touch`. That is exactly how the doctrine guard's
own sentinel-write hole was found (see its module docstring), and it is
exactly how the worktree-ban sentinel's matching hole was found (both
`Write` and `touch` succeeded against `.coordinator-override-worktree-
guard`, after which `EnterWorktree` succeeded). This module makes closing
that second leg a two-line call instead of a hand-copied basename check,
so the next sentinel-gated guard is cheap to protect rather than repeating
the pattern -- and the bug -- by hand.

Ordering contract -- read before use
-------------------------------------
A caller that ALSO consults the sentinel's presence for an approval/allow
decision (as `guard-doctrine-surface-edits.py` does) MUST call
`sentinel_write_denial()` BEFORE that approval-state lookup. Consulting
approval state first and only then checking the target path would let a
currently-valid approval authorize an edit that EXTENDS or RENEWS itself.
A caller that has no such approval lookup at all (e.g. a guard whose only
job is protecting one sentinel, like the worktree-sentinel guard built
against this module) has no ordering hazard to worry about -- there is
nothing else in that script to order against.

Removal is intentionally out of scope here -- this module only knows how
to DENY a Write/Edit/MultiEdit/NotebookEdit call whose target is the
sentinel path. Deletion tools are not in the guarded matcher set, so
`rm`/deletion always stays available through other surfaces, and removing
a sentinel to re-lock its boundary remains the sanctioned recovery path
every sentinel-gated guard relies on.
"""

from __future__ import annotations

import os
from typing import Any, Dict, Optional

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


def reconstruct_after(tool_name: str, tool_input: dict, before: str) -> "str | None":
    """The after-state a Write/Edit/MultiEdit call would leave on disk, given
    the file's current `before` content. `None` means the shape could not be
    reconstructed confidently -- a guard that cannot compute its own input
    has no basis to deny, so callers treat `None` as fail-open.

    Consolidated home for an idiom six coordinator-claude guards (`guard-oss-
    payload-locality.py`, `guard-prompt-surface-citations.py`,
    `guard-doctrine-changelog-prose.py`, `guard-test-tree-git-fixture-
    spawn.py`, `nudge-plan-test-surface-tier.py`,
    `guard-python-syntax-on-write.py`) each hand-copied a local
    `_reconstruct_after` for, per DR-047 (claude-klabauter owns guard logic, coordinator-claude owns
    plumbing). Diffed byte-for-byte against all six before writing this: they
    are semantically identical today -- differing only in docstrings, a
    stale review comment on two of them (`guard-prompt-surface-citations.py`
    and `guard-doctrine-changelog-prose.py` carry a "Review: coordinator:
    code-reviewer -- F1" comment marking a PAST fix to their `replace_all`
    handling, already applied, not a live divergence), and one refactor
    (`guard-python-syntax-on-write.py` folds the `Edit` case into the
    `MultiEdit` loop as a one-item edit list; same result). No copy diverges
    in its actual `replace_all` output today -- the divergence risk named in
    the consolidation brief is the RISK of six independently-maintained
    copies drifting, not an already-realized bug; this module removes that
    risk by giving all six one body to import instead of six to keep in
    sync.

    Contract, matching every copy above:

    - ``Write``: ``tool_input["content"]`` taken verbatim as the after-state.
      Not a string -> `None`.
    - ``Edit``: replay ``old_string`` -> ``new_string`` against `before`,
      honouring ``replace_all`` (every occurrence when true, only the first
      when false/absent). An empty ``old_string`` is treated as "replace the
      whole file" (`new_string` becomes the entire after-state) -- this
      matches the real Edit tool's own use of an empty `old_string` to seed
      content into an empty file, and every one of the six copies encodes
      it identically.
    - ``MultiEdit``: replay each edit in ``edits`` order against the
      running text, each honouring its own ``replace_all``, same rules as
      ``Edit`` per entry.
    - **The one rule worth calling out explicitly, because it is the exact
      spot a hand-copy could regress toward the wrong default:** an
      ``old_string`` ABSENT from the text at the point it is applied is
      UNRECONSTRUCTABLE, not a no-op-and-continue. This function returns
      `None` immediately -- it does NOT skip that edit and keep replaying
      the rest, and it does NOT return `before` unchanged. A caller that
      cannot compute the after-state has no basis to reason about it, let
      alone deny or allow based on it.
    - Any tool_name other than ``Write``/``Edit``/``MultiEdit``, or a
      malformed payload shape (non-dict ``tool_input``/``edit`` entry,
      non-list ``edits``, non-string ``old_string``/``new_string``/
      ``content``) -> `None`.
    """
    if tool_name == "Write":
        content = tool_input.get("content") if isinstance(tool_input, dict) else None
        return content if isinstance(content, str) else None

    if tool_name == "Edit":
        if not isinstance(tool_input, dict):
            return None
        edits = [tool_input]
    elif tool_name == "MultiEdit":
        if not isinstance(tool_input, dict):
            return None
        edits = tool_input.get("edits")
        if not isinstance(edits, list):
            return None
    else:
        return None

    text = before
    for edit in edits:
        if not isinstance(edit, dict):
            return None
        old_s = edit.get("old_string")
        new_s = edit.get("new_string")
        if not isinstance(old_s, str) or not isinstance(new_s, str):
            return None
        if old_s == "":
            text = new_s
            continue
        if old_s not in text:
            return None
        text = (
            text.replace(old_s, new_s)
            if edit.get("replace_all")
            else text.replace(old_s, new_s, 1)
        )
    return text


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
      the override this guard exists to prevent (`block-worktree-tool.py`
      ::`_sentinel_override_active`, and the doctrine guard's own approval
      lookup) checks presence via `os.path.isfile()`, which is effectively
      case-insensitive on the fleet's primary hazard filesystem (macOS
      APFS, default case-insensitive-but-case-preserving). A case-varied
      write that a case-sensitive check here would silently allow still
      round-trips into a live override on that filesystem.

    Basename match only (never substring/prefix) -- a near-miss filename
    (e.g. a `-typo` suffix) or an unrelated file must never be caught by
    this check.

    Safe to call on a path a caller has already resolved itself (e.g.
    `guard-doctrine-surface-edits.py`'s own `_norm()`): `os.path.realpath`
    is idempotent, so resolving an already-resolved absolute path a second
    time is a harmless no-op, not a correctness concern.
    """
    if not target_path:
        return False
    try:
        resolved = os.path.realpath(os.path.abspath(target_path))
        return os.path.basename(resolved).lower() == sentinel_name.lower()
    except Exception:
        return False


def sentinel_write_denial(
    target_path: str,
    sentinel_name: str,
    reason: str,
    *,
    payload: Optional[Dict[str, Any]],
) -> "dict | None":
    """Returns a PreToolUse deny `hookSpecificOutput` dict if `target_path`
    targets `sentinel_name`, else None.

    Caller is responsible for invoking this BEFORE any approval-state
    lookup that also consults the same sentinel (see module docstring,
    "Ordering contract").

    ``payload`` (2026-08-13, AUDIENCE-GATED -- docs/plans/2026-08-13-guard-
    messages-stop-handing-agents-the-keys.md, C4c): a REQUIRED keyword, no
    default -- a caller missed by this migration must raise ``TypeError``
    at collection, never silently keep a pre-migration call shape. This
    module previously composed a deny envelope from a pre-composed
    ``reason: str`` with NO payload access at all, so it structurally could
    not resolve audience -- census B named this the "Tier-2 plumbing
    composer" that needed a signature change to close that gap, mirroring
    the same fix `bash_guards._helpers.operator_override_note` already
    received (C1).

    This function does not itself rewrite ``reason`` -- each caller is
    responsible for composing an audience-safe ``reason`` BEFORE calling
    here (see `block_disarm_marker_sentinel_write.py` and
    `block_worktree_sentinel_write.py`, both of which dropped their one
    mechanism-naming clause unconditionally, for both audiences, once this
    dispatch's callers were audited; `block_dev_repo_sentinel_write.py`'s
    advisory sibling already builds its reason via
    `bash_guards._helpers.operator_override_note(payload=payload)`, which is
    itself audience-gated). ``payload`` is threaded through and accepted
    here so this seam has payload access at all -- the structural
    precondition census B's fix depends on -- and so a future caller/lint
    (the plan's AC-5 register rule) can rely on every sentinel-write-deny
    call site actually carrying a payload, rather than a subset doing so
    and a subset not.
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


def sentinel_write_advisory(
    target_path: str,
    sentinel_name: str,
    reason: str,
    *,
    payload: Optional[Dict[str, Any]],
) -> "dict | None":
    """Advisory-shaped sibling of `sentinel_write_denial()` -- same
    same-sentinel match, `additionalContext` (never blocks the write)
    instead of a `permissionDecision: "deny"` envelope, matching every
    other advisory write-guard's envelope shape in this package (e.g.
    `block_completion_monolith_write.py`, `block_dev_side_mirror_wiki.py`).

    Added for `block_dev_repo_sentinel_write.py`'s DR-277 class flip
    (docs/plans/2026-08-06-apply-guard-class-census.md, chunk C12) without
    touching `sentinel_write_denial()` itself: several OTHER callers of
    this helper (`block_disarm_marker_sentinel_write.py`,
    `block_worktree_sentinel_write.py`, `guard_doctrine_surface_edits.py`,
    `guard_settings_json_write.py`) stay hard-deny per AC6, and they all
    call `sentinel_write_denial()` directly -- this function is additive,
    not a replacement, so none of those callers' behaviour changes.

    ``payload`` (2026-08-13, same C4c signature change as
    `sentinel_write_denial()` above, for the same reason -- REQUIRED
    keyword, no default): threaded through so this seam has payload access;
    the caller (`block_dev_repo_sentinel_write._advisory_reason`) already
    composes its `reason` audience-safely via `operator_override_note`.
    """
    if not is_sentinel_write(target_path, sentinel_name):
        return None
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "additionalContext": reason,
        }
    }
