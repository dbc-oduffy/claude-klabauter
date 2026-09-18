"""coordinator_core.hooks.block_worktree_tool — PreToolUse
(EnterWorktree|ExitWorktree) op.

Ported from DoE-claude `coordinator/hooks/scripts/block-worktree-tool.py` per
docs/plans/2026-09-18-doe-holds-no-scripts.md chunk W4-C9. Structurally bans
the harness worktree-lifecycle tools from every session in this fleet:
worktrees degrade on Windows and do not scale to the concurrent-agent shape
this coordinator system runs (multiple sessions sharing one working tree with
scoped, disjoint file paths — see `CLAUDE.md`'s "Parallel agents share one
tree" rule and `state/lessons/`).

Block predicate: DENY `EnterWorktree` unconditionally (subject to the
sentinel override below). ALLOW `ExitWorktree` unconditionally — leaving a
worktree is cleanup, never the thing this guard exists to stop; blocking an
exit path would trap an agent already inside a worktree with no way out.

Override — repo-root sentinel file ONLY (`.coordinator-override-worktree-
guard`, shared with `strip_worktree_isolation`'s
`worktree_isolation_strip.sentinel_override_active`), deliberately no
env-var leg: a dispatched subagent can set an env var on itself before a
tool call, which is self-defeating for a guard that must bind subagents
exactly as it binds the main session.

Deny message discipline: the sentinel name is never printed — a deny
message that prints the exact bypass command reads to an eager agent as a
sanctioned next step, not a boundary. Leads with the sanctioned alternative
(design-as-offers): dispatch into the same tree with scoped, disjoint paths,
escalate to the EM (PM-approved) if branch-level isolation is genuinely
needed.

ADAPTATION: `_git_root()`'s in-process-walk-then-subprocess-fallback is
replaced by `worktree_isolation_strip.sentinel_override_active`'s own
zero-spawn `coordinator_core.git.repo_root.show_toplevel` call — the same
adaptation W4-C4 already made for the sibling `Workflow`-side strip, and the
identical override sentinel — so this module reuses that predicate rather
than re-resolving the repo root a second way.

Fail-open guards (all `no_advisory()`), in order: `tool_name` not one of
`EnterWorktree`/`ExitWorktree`; sentinel-file override active for
`EnterWorktree`; any exception resolving the override.

Spec backlink: docs/plans/2026-09-18-doe-holds-no-scripts.md § W4-C9
"""

from __future__ import annotations

from coordinator_core._hook_envelope import deny, no_advisory
from coordinator_core.hooks.support.message_envelope import compose, render
from coordinator_core.hooks.support.worktree_isolation_strip import (
    sentinel_override_active,
)
from coordinator_core.ipc import register_op

_WIKI_ANCHOR = "coordinator/docs/wiki/guard-message-concision.md#worktree-ban-rationale"

_DENY_PROSE = (
    "Worktrees banned (break Windows, don't scale to concurrent dispatch). "
    "Use scoped, disjoint paths here; escalate to the EM (PM-approved) for "
    "branch isolation."
)


def _deny_message() -> str:
    """Pure composer for the deny message — kept separate from the handler so
    it can be measured/unit-exercised without payload plumbing."""
    return render(compose(_DENY_PROSE, anchor=_WIKI_ANCHOR))


@register_op("hooks.block_worktree_tool")
async def _handler(params: dict, repo_root=None) -> dict:
    """PreToolUse(EnterWorktree|ExitWorktree) op: deny EnterWorktree unless
    the repo-root sentinel override is active; always allow ExitWorktree."""
    tool_name = params.get("tool_name") or ""

    if tool_name == "ExitWorktree":
        return no_advisory()

    if tool_name != "EnterWorktree":
        return no_advisory()

    try:
        if sentinel_override_active():
            return no_advisory()
    except Exception:
        pass

    return deny("PreToolUse", _deny_message())
