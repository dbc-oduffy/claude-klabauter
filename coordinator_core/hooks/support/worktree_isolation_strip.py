"""worktree_isolation_strip -- shared library, NOT a registered hook.

Ported from DoE-claude `coordinator/hooks/scripts/_worktree_isolation_strip.py`
per docs/plans/2026-09-18-doe-holds-no-scripts.md chunk W4-C4. ADAPTATION (the
class-1 site named in this package's own `__init__.py` docstring): DoE's
`_git_root()` combined a sibling-import zero-spawn walk (`_git_root_walk.
git_root_walk`, itself a duplicate of a walk this engine already implements)
with a `git rev-parse --show-toplevel` subprocess FALLBACK (1s timeout). That
sibling module is not part of this chunk's footprint and cannot be imported
across the doctrine/engine boundary, so `_git_root()` now calls
`coordinator_core.git.repo_root.show_toplevel` directly -- the engine's own
zero-spawn-only walk primitive, byte-for-byte the same contract
(`Path`-resolved root or `None`, never raises). The subprocess fallback is
DROPPED, not merely deferred: `show_toplevel`'s own docstring documents a
2026-08-19 measurement that the fallback never won a case the walk had not
already produced, and its negative-spec forbids reintroducing one -- carrying
DoE's fallback forward would reopen the exact spawn that primitive's own
docstring closes. Everything else (the override-sentinel-only check, the
worktree-literal strip, the fail-open contract) is unchanged from the source.

Single-emitter fix (2026-07-31): `strip-worktree-isolation.py` and
`enforce-agent-dispatch-mode.py` used to independently build their own
`hookSpecificOutput.updatedInput` from a full copy of the same `tool_input`
on overlapping PreToolUse matchers (`Agent|Workflow` and `Agent`
respectively). Claude Code runs same-event hooks in parallel with undefined
completion order, and `updatedInput` is last-writer-wins -- so on an `Agent`
dispatch carrying BOTH `isolation: "worktree"` AND a mode-elevation/sidecar/
role-framing trigger, exactly one hook's rewrite silently clobbered the
other's (confirmed live on harness 2.1.220; see
`docs/plans/2026-07-31-agent-updated-input-single-emitter.md` -- or the
dispatch brief that fixed this, if that plan was never written).

Fix shape: the worktree-isolation-strip COMPUTATION (this module) is now
shared, pure, and side-effect-free (no stdout, no sys.exit). The only two
callers that may EMIT `updatedInput` for a tool_name are:
  - `enforce-agent-dispatch-mode.py` for `Agent` -- folds this module's
    result into its own single merged `updatedInput` (mode elevation +
    sidecar + contract-blocks + role-framing + worktree-strip, all layered
    onto ONE `merged` dict, ONE emission site).
  - `strip-worktree-isolation.py` for `Workflow` -- `Workflow`'s tool_input
    has no `prompt` key (it carries `script`/`scriptPath`), so it cannot
    share `enforce-agent-dispatch-mode.py`'s Agent-only emit path; it stays
    its own hook, scoped to `Workflow` only, importing this module instead
    of re-implementing the strip/override logic.

Neither caller re-implements the override-sentinel check or the key-removal
logic -- both import `compute_strip` from here. This is the "one function,
one module" shape: two matchers still need the computation, but the
merge-and-emit path per matcher exists exactly once.

Only the literal value "worktree" is banned. `isolation: "remote"` (and any
other value) is a legitimate, unrelated isolation mode and passes through
byte-identical -- `compute_strip` returns `None` for it, same as when no
`isolation` key is present at all.

Override: a repo-root sentinel file only, `.coordinator-override-worktree-
guard` -- deliberately NOT an env-var leg. An env var is process-inheritable
by a dispatched subagent (it can set its own env before its own tool calls
run under this same hook), which would let a subagent silently defeat a
guard that exists specifically to bind subagents.

Fail-open on every detection-failure leg: `compute_strip` returns `None`
(nothing to strip) on any git-root-resolution failure -- it never raises.
Both callers remain responsible for their own stdin/JSON/tool_name/
tool_input validation; this module only computes the strip once the caller
has already established `tool_input` is a dict.
"""

from __future__ import annotations

import os
from typing import Optional

from coordinator_core.git.repo_root import show_toplevel

_OVERRIDE_SENTINEL_NAME = ".coordinator-override-worktree-guard"

_STRIP_NOTE = (
    "[worktree guard] isolation: \"worktree\" stripped -- per-dispatch "
    "worktrees are banned (shared-tree discipline); dispatch proceeds "
    "without it."
)


def _git_root() -> "str | None":
    """Repo root as `git rev-parse --show-toplevel` would report it, without
    spawning a subprocess -- see this module's own ADAPTATION note for why
    the doctrine-plane source's subprocess fallback is dropped here rather
    than ported. Any failure (not a git repo, unresolvable) returns None and
    the sentinel check below is skipped -- fails toward "no override", never
    toward a crash.
    """
    return show_toplevel()


def sentinel_override_active() -> bool:
    root = _git_root()
    if not root:
        return False
    try:
        return os.path.isfile(os.path.join(root, _OVERRIDE_SENTINEL_NAME))
    except Exception:
        return False


def compute_strip(tool_input: dict) -> Optional[tuple[dict, str]]:
    if tool_input.get("isolation") != "worktree":
        return None
    if sentinel_override_active():
        return None
    merged = dict(tool_input)
    del merged["isolation"]
    return merged, _STRIP_NOTE
