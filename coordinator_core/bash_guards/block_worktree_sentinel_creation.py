"""coordinator_core.bash_guards.block_worktree_sentinel_creation --
PreToolUse (Bash) hard-deny guard that makes the git-worktree ban's override
sentinel un-creatable by any agent -- Bash-level, EM included.

WHY THIS EXISTS. The fleet-wide git-worktree ban (example-doctrine-repo-side
`block-worktree-tool.py` / `strip-worktree-isolation.py`) is gated open by a
single repo-root sentinel file, `.coordinator-override-worktree-guard`.
Both of those example-doctrine-repo hooks deliberately omit an env-var override leg -- a
subagent can set an env var on itself before its own tool call runs, which
would defeat a guard that must bind subagents exactly as it binds the main
session (see either hook's own module docstring). But the sentinel FILE
itself was reachable: `Write`-ing it directly, or `touch`-ing it from a
shell, both succeeded, and `EnterWorktree` succeeded immediately afterward.
That is the identical self-grant the missing env-var leg exists to prevent,
reintroduced on a different surface. Found live by attempting the bypass,
after both worktree-ban guards were otherwise green -- the same discovery
shape that led to `block_approval_sentinel_creation.py` for the doctrine-
approval sentinel.

This guard closes the Bash-level leg for the worktree sentinel. The
file-write leg (Write/Edit/MultiEdit/NotebookEdit) is closed separately in
Example-doctrine-repo's `coordinator/hooks/scripts/guard-worktree-sentinel-write.py`.

NOT IDENTITY-GATED -- fires for every caller, EM included, same posture as
`block_approval_sentinel_creation.py` and `block_worktree_creation.py`: the
anti-pattern (self-granting a worktree-ban override) is wrong regardless of
who types it.

NO OVERRIDE -- DELIBERATE, by design, no exceptions. Same reasoning as
`block_approval_sentinel_creation.py`'s own "NO OVERRIDE" section: any
`COORDINATOR_OVERRIDE_*` escape hatch here would be reachable by exactly
the caller class this guard exists to constrain (a subagent, or an EM,
setting its own process env), making this a bypass of a bypass-prevention
guard.

REGISTRATION ORDERING -- MUST run BEFORE `offer-git-c` in
`coordinator_core.bash_guards.dispatch`, for the identical short-circuit
reason documented on `block_approval_sentinel_creation.py` ("REGISTRATION
ORDERING") and `block_worktree_creation.py`: `offer-git-c` rewrites
`cd <dir> && git <sub>`-shaped commands into `git -C <dir> <sub>` and
returns allow-with-updatedInput, which SHORT-CIRCUITS every later guard in
the chain. Registered immediately adjacent to `block_approval_sentinel_
creation` in `dispatch.py`, ahead of `offer-git-c`, for the same reason.

DETECTION SURFACE. Delegates entirely to the shared
`SentinelCreationDetector` in `_sentinel_creation_guard.py` (see that
module's docstring for the full rule set: redirection, `touch`/`cp`/`mv`/
`install`/`ln`/`tee`, `sed -i`, `python -c`, and the `cd <dir> &&`-prefixed
/ `git -C <dir>`-prefixed forms this guard's dispatch-level position
already covers by running ahead of the rewriter). DEFAULT POSTURE ON
AMBIGUITY IS DENY, same asymmetric posture as the doctrine-sentinel guard.

ALLOWED, UNCONDITIONALLY: reads (`cat`, `ls`, `stat`) and removal (`rm`,
`test -f ... && rm ...`) of the sentinel. Removing an override always
re-locks the boundary rather than unlocking it.

Deny message deliberately never names the sentinel file or prints a
workaround -- an eager agent reading its own bypass in a deny message
treats it as sanctioned (precedent: `block-worktree-tool.py`'s own deny
message discipline). Leads with the sanctioned alternative (scoped-parallel
dispatch into the same tree), then names PM permission as the path to
genuine branch-level isolation.

Spec: git-worktree-ban sentinel un-creatable-by-agent guard (example-doctrine-repo
dispatch, 2026-07-28) -- companion to the sibling example-doctrine-repo-side hooks that read
this sentinel to gate the worktree ban's override.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from coordinator_core.bash_guards._sentinel_creation_guard import (
    REASON_INDIRECTION,
    SentinelCreationDetector,
)
from coordinator_core.bash_guards._dialect import Dialect, dialect_from_tool_name
from coordinator_core.bash_guards._tool_names import COMMAND_TOOL_NAMES

CLASS = "hard-deny"
#: Widened 2026-08-07 (C4e) from `["Bash"]` -- see `block_approval_sentinel_
#: creation.py`'s identical note. A direct reference to the shared universe
#: (C2 declaration-form conversion) -- never a copy or re-wrap.
MATCHERS = COMMAND_TOOL_NAMES
PRIORITY = 41

#: The exact basename this guard protects. Never relaxed to a substring/
#: prefix match -- an unrelated file that merely CONTAINS this string in a
#: longer name is a DIFFERENT file and is not the worktree-ban override
#: sentinel the sibling example-doctrine-repo hooks read.
_TARGET_BASENAME = ".coordinator-override-worktree-guard"

#: Shared detection engine -- see `_sentinel_creation_guard.py` module
#: docstring. This module is the second guard built on it, after
#: `block_approval_sentinel_creation.py`.
_detector = SentinelCreationDetector(_TARGET_BASENAME)


def _evaluate(cmd: str, dialect: Optional[Dialect] = None):
    """See `block_approval_sentinel_creation._evaluate`'s identical note --
    `dialect=None`/`Dialect.BASH` preserves the exact pre-C4e call shape."""
    if dialect is None or dialect is Dialect.BASH:
        return _detector.evaluate(cmd)
    return _detector.evaluate_for_dialect(
        cmd, dialect, guard_name="block_worktree_sentinel_creation"
    )


def _deny_reason(cmd: str, reason_kind: str, reason_class: str) -> str:
    # Deliberately does NOT echo `cmd` back into the message and does NOT
    # name the target basename in either branch below -- both would print
    # the exact bypass an eager agent could copy-paste, which reads as
    # sanctioning it rather than blocking it (same discipline as
    # block-worktree-tool.py's and guard-doctrine-surface-edits.py's own
    # deny messages). `cmd` stays accepted for call-site symmetry with the
    # sibling guard, but is intentionally unused here.
    #
    # `reason_class` (2026-07-28 diagnosability fix, mirrors
    # `block_approval_sentinel_creation._deny_reason` -- see
    # `_sentinel_creation_guard.py` module docstring "REASON CLASS") splits
    # the single fixed message this function used to return into two
    # truthful ones: REASON_DIRECT means a rule positively matched the
    # override sentinel, so the "this command would create/modify it"
    # assertion is correct. REASON_INDIRECTION means the payload sits
    # behind an interpreter/env/xargs/heredoc wrapper this guard cannot
    # examine, so it denies BY CONSTRUCTION -- not because anything was
    # found. Keep both guards mirrored.
    del cmd
    if reason_class == REASON_INDIRECTION:
        # `reason_kind` names a shell SHAPE, not a bypass -- but a
        # recursive indirection verdict can still bottom out one level
        # down in the direct branch's target-naming string (e.g.
        # `bash -c "touch <sentinel>"`), so redact the basename out
        # regardless, rather than trusting the branch alone.
        safe_shape = reason_kind.replace(_TARGET_BASENAME, "<the sentinel>")
        return (
            "[worktree guard] BLOCKED: interpreter/stdin/xargs indirection "
            "this guard cannot examine -- NOT because the payload was "
            "found to touch the sentinel. Shape: %s -- run its underlying "
            "steps directly (no wrapper), or ask the EM/PM." % safe_shape
        )
    del reason_kind  # REASON_DIRECT: message below is fixed, not shape-derived.
    return (
        "[worktree guard] BLOCKED: this command would create or modify a "
        "worktree-ban override file; agents cannot self-grant that. "
        "Instead: dispatch scoped-parallel edits in this tree; branch "
        "isolation needs EM+PM approval."
    )


def check(payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Evaluate the worktree-override-sentinel-creation-ban gate against a
    PreToolUse payload.

    Returns `None` (allow) or the nested hard-deny envelope. Never
    identity-gated -- fires for every caller including the main-loop EM
    (see module docstring "NOT IDENTITY-GATED").
    """
    # Deliberately no try/except here -- fail-CLOSED-on-exception is the
    # dispatcher's job for hard-deny guards; catching and swallowing an
    # unexpected error into a silent allow here would defeat that contract.
    tool_name = payload.get("tool_name") or ""
    if tool_name not in MATCHERS:
        return None
    dialect = dialect_from_tool_name(tool_name)

    tool_input = payload.get("tool_input") or {}
    cmd = (tool_input.get("command") if isinstance(tool_input, dict) else None) or ""
    if not cmd:
        return None
    cmd = cmd.replace("\r", "")

    deny, reason_kind, reason_class = _evaluate(cmd, dialect)
    if not deny:
        return None

    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": _deny_reason(cmd, reason_kind, reason_class),
        }
    }
