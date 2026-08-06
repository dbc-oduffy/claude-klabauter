"""coordinator_core.write_guards.guard_doctrine_surface_edits — deny edits to
always-loaded doctrine surfaces unless a PM-created approval sentinel is
present and unexpired.

Ported from example-doctrine-repo
`coordinator/hooks/scripts/guard-doctrine-surface-edits.py` (faithful port —
see `write_guards/INTERFACE.md`). CLASS is hard-deny and fail-closed; this
is the one guard in the example-doctrine-repo source tree that deliberately differs from every
sibling guard's fail-open posture — see "Fail-closed is DELIBERATE" below,
carried over verbatim from the source docstring.

Why this exists
----------------
A handful of files are always-loaded into every session and every dispatched
agent's context (the global CLAUDE.md, the EM-only operating-doctrine
channel, the repo-root project CLAUDE.md, coordinator.local.md). An EM
editing one of these unilaterally is self-approval of a change that reaches
every downstream session and agent in the fleet. This guard makes that
structurally impossible: the EM cannot self-approve, only a PM-created
sentinel (created out-of-band, never by this hook or by the agent it
blocks) unlocks the edit, and the unlock is time-boxed so it is never a
standing bypass.

Protected surfaces (resolved, symlink-free, absolute real paths; matched
exactly — never by substring or basename, so a nested CLAUDE.md deeper in
the tree, e.g. coordinator/tests/CLAUDE.md, is NOT protected):
  - <repo-root>/global-doctrine/CLAUDE.md
  - $HOME/.claude/CLAUDE.md              (the derived live copy)
  - <repo-root>/coordinator/snippets/em-operating-doctrine.md
  - <repo-root>/CLAUDE.md                (repo-root project instructions)
  - <repo-root>/coordinator.local.md

Approval mechanism
-------------------
Repo-root sentinel file `.coordinator-doctrine-edit-approved` (name
deliberately NOT printed in the deny message — see Deny message below):
  - absent -> DENY
  - present, mtime older than APPROVAL_WINDOW_SECONDS (30 minutes) -> DENY
    (treated as expired)
  - present, mtime within the window -> ALLOW (silent, no output). One
    approval covers a multi-edit change, which is the normal shape of a
    doctrine edit (several Edit calls against the same file in one pass).

Fail-closed is DELIBERATE and is the one place this guard differs from
every sibling guard in this tree. Every other guard fails OPEN on its own
resolution failures (git root unresolvable, home unresolvable, subprocess
timeout) because each of those guards protects against a mistake, and
refusing to protect against a mistake you can't currently detect is the
safer failure mode. This guard protects a PM approval BOUNDARY, not a
mistake — a boundary that fails open under an unreadable sentinel or an
unresolvable repo root is not a boundary at all, it is a suggestion. So: if
the git root cannot be resolved, this guard falls back to checking ONLY the
$HOME/.claude/CLAUDE.md protection (the one protected path that does not
require a repo root), and treats the approval sentinel as ABSENT (deny)
rather than skipping the check.

The one place this guard still fails OPEN, unconditionally, is an
unparseable or non-matching payload: if the guard cannot tell what file is
being edited (missing tool_input, no path key, tool not in the guarded set,
or the resolved path is not one of the five protected surfaces), it has no
basis to block anything and allows silently. The distinction that matters:
unparseable payload -> allow; parseable payload that DOES target a
protected file but whose approval state cannot be resolved -> deny.

**HARD CONSTRAINT carried from the porting brief — do NOT wrap `check()` in
a blanket try/except.** Every risky call below (`_git_root`, `_norm`,
`_protected_paths`, `_sentinel_state`) is individually try/excepted to a
definite value exactly as in the source, so the fail-closed posture holds
without needing a function-level catch-all; adding one would convert this
boundary to fail-open, which is the one identified way to put a hole in it.

Deny message
-------------
Design-as-offers: leads with why the file matters (always-loaded, reaches
every session and every dispatched agent) and what to do about it (describe
the proposed change to the PM, ask for approval; consider whether the
content belongs in a wiki/skill surface instead, which needs no approval).
It deliberately never names the sentinel file or prints a command that
would create one — an eager agent reading its own bypass in a deny message
treats it as sanctioned, exactly the failure mode that bit
block-home-dir-memo-delivery.py's precedent (that guard's own docstring
cites this) and the block-destructive-rm incident where a printed override
string led to a peer's in-flight work being destroyed. Regression-pinned by
test_guard_doctrine_surface_edits.py, which asserts the sentinel filename
never appears in the deny reason.

Fail-open paths (all return None, in order):
  - tool_name not in the guarded set.
  - no target path in tool_input.
  - resolved target path is not one of the five protected surfaces.

Spec backlink: example-doctrine-repo
  coordinator/hooks/scripts/guard-doctrine-surface-edits.py
"""

from __future__ import annotations

import os
import time
from typing import Any, Dict, Optional

from coordinator_core.write_guards._sentinel_write_guard import (
    extract_target_path,
    sentinel_write_denial,
)

CLASS = "hard-deny"
MATCHERS = ["Write", "Edit", "MultiEdit", "NotebookEdit"]
PRIORITY = 127

_GUARDED_TOOLS = ("Write", "Edit", "MultiEdit", "NotebookEdit")

_SENTINEL_NAME = ".coordinator-doctrine-edit-approved"
_APPROVAL_WINDOW_SECONDS = 30 * 60


def _norm(path: str) -> str:
    """Resolve to an absolute, symlink-free real path."""
    return os.path.realpath(os.path.abspath(path))


def _git_root() -> "str | None":
    """`git rev-parse --show-toplevel`, 1s timeout.

    Any failure (not a git repo, git missing, timeout) returns None; the
    caller falls back to the HOME-only protected-path check and treats the
    approval sentinel as absent (deny), per this guard's fail-closed
    posture.
    """
    import subprocess
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            timeout=1,
        )
    except Exception:
        return None
    if result.returncode != 0:
        return None
    root = result.stdout.strip()
    return root or None


def _home_claude_md() -> "str | None":
    try:
        home = os.path.expanduser("~")
        if not home or home == "~":
            return None
        return _norm(os.path.join(home, ".claude", "CLAUDE.md"))
    except Exception:
        return None


def _protected_paths(repo_root: "str | None") -> "list[str]":
    paths: "list[str]" = []
    home_claude_md = _home_claude_md()
    if home_claude_md:
        paths.append(home_claude_md)
    if repo_root:
        try:
            paths.append(_norm(os.path.join(repo_root, "global-doctrine", "CLAUDE.md")))
            paths.append(
                _norm(
                    os.path.join(
                        repo_root, "coordinator", "snippets", "em-operating-doctrine.md"
                    )
                )
            )
            paths.append(_norm(os.path.join(repo_root, "CLAUDE.md")))
            paths.append(_norm(os.path.join(repo_root, "coordinator.local.md")))
        except Exception:
            pass
    return paths


def _sentinel_state(repo_root: "str | None") -> str:
    """Returns "allow", "deny-absent", or "deny-expired".

    No repo root resolvable -> "deny-absent" (fail closed on the guard's
    own resolution failure, per this guard's deliberate fail-closed
    posture -- see module docstring).

    A DIRECTORY at the sentinel path is treated identically to an ABSENT
    sentinel (2026-07-30 forge-closure fix, ported from the example-doctrine-repo-side
    source). `os.path.getmtime` succeeds on a directory exactly as it does
    on a regular file, so `mkdir <sentinel>` used to read as a real,
    honoured approval -- this guard's read side never actually checked
    that the path was a *file*. `os.path.isfile()` follows symlinks (a
    symlink to a regular file the PM created still approves,
    deliberately) but returns False for a directory, fifo, socket, or
    device, all of which now fall into the same "deny-absent" bucket a
    missing sentinel does.
    """
    if not repo_root:
        return "deny-absent"
    sentinel_path = os.path.join(repo_root, _SENTINEL_NAME)
    try:
        if not os.path.isfile(sentinel_path):
            return "deny-absent"
        mtime = os.path.getmtime(sentinel_path)
    except OSError:
        return "deny-absent"
    except Exception:
        return "deny-absent"
    age = time.time() - mtime
    if age > _APPROVAL_WINDOW_SECONDS:
        return "deny-expired"
    return "allow"


def _deny_reason(target: str) -> str:
    return (
        "[doctrine-surface guard] BLOCKED: "
        f"{target} is always-loaded doctrine — it reaches every session and "
        "every dispatched agent in the fleet, so a change here is not a "
        "unilateral edit the EM can make on its own. Describe the proposed "
        "change to the PM and ask them to approve it before editing this "
        "file.\n"
        "What approval looks like, so you do not have to re-derive it: the PM "
        f"creates `{_SENTINEL_NAME}` at the repo root, themselves, in-harness "
        f"(`!touch {_SENTINEL_NAME}`). One approval opens a "
        f"{int(_APPROVAL_WINDOW_SECONDS // 60)}-minute window covering a "
        "multi-edit change. Relay that command to the PM — do NOT run it. You "
        "cannot: creation is blocked on BOTH surfaces (Write/Edit here, Bash "
        "in the engine's approval-sentinel creation guard), because an agent "
        "creating its own approval is self-approval, not approval.\n"
        "In the meantime, consider whether the content actually belongs "
        "in a wiki page or a skill surface instead — those need no "
        "approval and are the right home for most doctrine additions."
    )


def _sentinel_write_deny_reason() -> str:
    return (
        "[doctrine-surface guard] BLOCKED: this file is the PM's approval to "
        "edit doctrine; you creating it would be self-approval. Ask the PM to "
        "approve and create it. Deleting it (re-locking) is still allowed."
    )


def check(payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Evaluate the doctrine-surface guard against a PreToolUse payload.

    Returns ``None`` (allow) or the nested hard-deny envelope. Mirrors the
    source `main()` control flow exactly, minus the stdin/stdout plumbing
    the engine already handles.
    """
    if payload.get("tool_name", "") not in _GUARDED_TOOLS:
        return None

    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        return None

    target_raw = extract_target_path(tool_input)
    if not target_raw:
        return None

    try:
        target = _norm(target_raw)
    except Exception:
        return None

    repo_root = _git_root()

    # The sentinel itself is unwritable through the file-write tools, and this
    # check runs BEFORE the approval lookup below -- deliberately, because
    # consulting approval here would let a valid approval authorize extending
    # itself. Removal stays available via `rm`; only creation is the
    # boundary. Delegated to the shared _sentinel_write_guard helper -- see
    # that module's docstring for the ordering contract this call site
    # relies on.
    denial = sentinel_write_denial(target, _SENTINEL_NAME, _sentinel_write_deny_reason())
    if denial is not None:
        return denial

    protected = _protected_paths(repo_root)
    if target not in protected:
        return None

    state = _sentinel_state(repo_root)
    if state == "allow":
        return None

    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": _deny_reason(target_raw),
        }
    }
