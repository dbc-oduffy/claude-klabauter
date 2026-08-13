"""coordinator_core.write_guards.block_worktree_sentinel_write — hard-deny guard.

Python engine-ification of coordinator-claude's
``coordinator/hooks/scripts/guard-worktree-sentinel-write.py`` PreToolUse
(Write|Edit|MultiEdit|NotebookEdit) hook, per the write-guard fold
(write_guards/INTERFACE.md).

Purpose (ported verbatim from the reference hook): denies creating or
modifying the git-worktree-ban override sentinel
``.coordinator-override-worktree-guard`` through any file-write tool.
``block-worktree-tool.py`` and ``strip-worktree-isolation.py`` gate the
worktree ban's override on this sentinel's PRESENCE, and both deliberately
omit an env-var override leg -- a subagent can set an env var on itself,
which would defeat a guard that must bind subagents. But the sentinel FILE
itself was unprotected: a plain ``Write`` to
``.coordinator-override-worktree-guard`` succeeded, and a ``touch`` from Bash
also succeeded -- both reintroduce exactly the agent-self-grant the missing
env-var leg was designed to prevent, just on a different surface.

This hook closes the file-write leg. The Bash/shell leg is closed separately
in this same tree by
``coordinator_core.bash_guards.block_worktree_sentinel_creation`` (already
ported; reused here only for cross-reference, not for logic -- the two
guards operate on disjoint tool surfaces and neither imports the other).

Mechanism: delegates entirely to the shared ``_sentinel_write_guard`` helper
(``extract_target_path``, ``sentinel_write_denial``) -- the same helper
``guard_doctrine_surface_edits`` is built on for its own sentinel. This
guard has no approval-lookup concern to order against (unlike the doctrine
guard, which ALSO decides allow/deny for other paths based on its own
sentinel class of file) -- it exists solely to protect one sentinel path.

Deny message discipline -- the sentinel name is NEVER printed. The deny
reason does not name the sentinel file and does not print any override
incantation, for the same reason ``block-worktree-tool.py`` withholds it: a
deny message that prints its own bypass reads to an eager agent as a
sanctioned next step, not a boundary. The message leads with the sanctioned
alternative (design-as-offers) -- scoped-parallel dispatch into the same
tree -- then names PM permission as the path to genuine branch-level
isolation.

Removal stays allowed -- this guard only matches
Write/Edit/MultiEdit/NotebookEdit -- there is no file-deletion tool in that
set, so ``rm``-based removal of an existing sentinel is untouched and
remains the sanctioned way to re-lock the boundary.

Symlink and case-folding are handled by the shared helper (see
``_sentinel_write_guard.is_sentinel_write`` docstring): the target path is
resolved (``os.path.realpath(os.path.abspath(...))``) and the basename
comparison is case-folded before matching against the sentinel name.

Fail-CLOSED on its own resolution failures: an unexpected exception inside
this module's own ``check()`` propagates rather than being swallowed into an
allow, matching the reference hook's hard-deny/fail-closed posture --
mirrors the fidelity rule against wrapping a ported hard-deny ``check()`` in
a blanket ``try/except: return None``.

Negative-spec:
  - Does NOT block reads, or removal (``rm``) of the sentinel -- no
    deletion-shaped tool is in ``MATCHERS``.
  - Does NOT name the sentinel basename or any override incantation in its
    deny reason, by design (see "Deny message discipline" above).
  - Does NOT gate any other approval/allow decision -- unlike
    ``guard_doctrine_surface_edits``, this module has no ordering hazard
    against an approval-state lookup because it has no such lookup.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from coordinator_core.write_guards._sentinel_write_guard import (
    extract_target_path,
    sentinel_write_denial,
)

CLASS = "hard-deny"
MATCHERS = ["Write", "Edit", "MultiEdit", "NotebookEdit"]
PRIORITY = 129

#: The exact basename this guard protects (reference hook `_SENTINEL_NAME`).
_SENTINEL_NAME = ".coordinator-override-worktree-guard"

#: Deny reason. 2026-08-13 (docs/plans/2026-08-13-guard-messages-stop-
#: handing-agents-the-keys.md, C4c): dropped the trailing "Removing an
#: override re-locks it" clause, unconditionally, for every audience -- it
#: is a statement that an unlock/override exists (AC-1 bans this for a
#: subagent render), and this guard has no doc pointer to substitute for an
#: EM-audience render (AC-2 caps that render at a wiki pointer only; no
#: override env var backs this sentinel).
_DENY_REASON = (
    "[worktree guard] BLOCKED: overrides can't be self-granted.\n"
    "Instead: dispatch scoped, disjoint-path edits in this tree.\n"
    "Need branch isolation? Ask the EM for PM permission."
)


def check(payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Evaluate the worktree-override-sentinel-write-ban gate against a
    PreToolUse payload.

    Returns `None` (allow) or the nested hard-deny envelope.
    """
    if (payload.get("tool_name") or "") not in MATCHERS:
        return None

    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        return None

    target = extract_target_path(tool_input)
    if not target:
        return None

    return sentinel_write_denial(target, _SENTINEL_NAME, _DENY_REASON, payload=payload)
