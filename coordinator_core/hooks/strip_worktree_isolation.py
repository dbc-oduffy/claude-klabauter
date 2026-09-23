"""coordinator_core.hooks.strip_worktree_isolation — PreToolUse(Workflow) op.

Ported from DoE-claude `coordinator/hooks/scripts/strip-worktree-isolation.py`
per docs/plans/2026-09-18-doe-holds-no-scripts.md chunk W4-C9. Mechanical
port — all computation already landed at W4-C4 as
`coordinator_core.hooks.support.worktree_isolation_strip` (`compute_strip`,
shared with `Agent`'s own strip path). This module is the thin `Workflow`-only
caller: makes `isolation: "worktree"` unreachable on a `Workflow` dispatch by
STRIPPING the key rather than denying the whole call (design-as-offers:
per-dispatch worktrees are banned outright — see shared-tree stash/worktree
discipline — so there is no legitimate value the strip destroys).

Single-emitter split (unchanged from source): `Agent`'s own strip lives inside
`enforce_agent_dispatch_mode` (this repo's future port of
`enforce-agent-dispatch-mode.py`, out of this chunk's footprint) so the two
matchers never race on `updatedInput` (last-writer-wins on same-event
parallel hooks). This hook stays scoped to `Workflow` only — its `tool_input`
carries no `prompt` key, so it cannot fold into the Agent-only merge path.

Fail-open on every detection-failure leg (non-Workflow tool_name, non-dict
tool_input, `compute_strip` returning None): `no_advisory()`. Only the
literal value "worktree" is banned; any other `isolation` value (e.g.
"remote") passes through byte-identical.

Spec backlink: docs/plans/2026-09-18-doe-holds-no-scripts.md § W4-C9
"""

from __future__ import annotations

from coordinator_core._hook_envelope import no_advisory, payload_of, rewrite_input
from coordinator_core.hooks.support.worktree_isolation_strip import compute_strip
from coordinator_core.ipc import register_op


@register_op("hooks.strip_worktree_isolation")
def _handler(params: dict, repo_root=None) -> dict:
    """PreToolUse(Workflow) op: strip a banned `isolation: "worktree"` field."""
    # Normalize the two params shapes
    # both engine doors and the cold chain send (see block_worktree_tool).
    params = payload_of(params)
    if params.get("tool_name") != "Workflow":
        return no_advisory()

    tool_input = params.get("tool_input")
    if not isinstance(tool_input, dict):
        return no_advisory()

    result = compute_strip(tool_input)
    if result is None:
        return no_advisory()

    merged, note = result
    return rewrite_input("PreToolUse", merged, context=note)
