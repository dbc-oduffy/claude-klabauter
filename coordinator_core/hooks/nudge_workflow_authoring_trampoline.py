"""coordinator_core.hooks.nudge_workflow_authoring_trampoline — PreToolUse
(Skill, Workflow) advisory op, two fire points, one op.

Ported from DoE-claude `coordinator/hooks/scripts/nudge-workflow-authoring-
trampoline.py` per docs/plans/2026-09-18-doe-holds-no-scripts.md chunk
W4-C9. Closes the gap where the native `workflow-authoring` skill teaches an
EM to hand-author a `Workflow({script: "..."})` call with no pointer back at
the emitter that would derive the same wave shape from a ratified plan
spine for roughly a quarter of the token cost. Fires at whichever of two
moments comes first: opening the `workflow-authoring` skill, or authoring
an inline `Workflow({script: "..."})` call directly.

Offer-shape (never blocks): always returns an advisory (`allow_advisory`) on
the nudge path, or `no_advisory()` otherwise — never a deny.

Fires when:
  - `tool_name == "Skill"` and `tool_input` carries a skill identifier
    (`skill`, falling back to `command`) equal to "workflow-authoring"
    (case-insensitive, bare or `coordinator:`-prefixed); OR
  - `tool_name == "Workflow"` and `tool_input` carries a non-empty `script`
    key and no `scriptPath` key — the by-construction hand-authored form; and
  - either way, the once-per-session sentinel is absent.

ONE sentinel name shared across both fire points, rooted at the git COMMON
dir (never `<git_root>/.git` — a worktree's `.git` FILE would silently
never persist this session's sentinel; see `git_common_dir`'s own
docstring).

ADAPTATION: `_git_root()`'s in-process-walk-then-subprocess-fallback is
replaced with `coordinator_core.git.repo_root.show_toplevel` — the same
zero-spawn-only adaptation already made at W4-C4 for the sibling worktree
strip module; the subprocess fallback is DROPPED, not deferred (see
`show_toplevel`'s own docstring for the 2026-08-19 measurement backing
that). Everything else (session-id shape gate, sentinel path, skill/inline
detection, message prose) is unchanged from source.

Graceful degradation — REQUIRED: any failure to resolve the git root,
common dir, or session sentinel falls through to `no_advisory()`. A
filesystem hiccup must never brick a Skill or Workflow invocation.

Spec backlink: docs/wiki/coordinator-tripwires/
a-hand-authored-workflow-costs-4x-the-plan-execution.md (DoE-claude);
docs/plans/2026-09-18-doe-holds-no-scripts.md § W4-C9
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Mapping, Optional

from coordinator_core._hook_envelope import allow_advisory, no_advisory, payload_of
from coordinator_core.git.repo_root import show_toplevel
from coordinator_core.hooks.support.git_common_dir import resolve_git_common_dir
from coordinator_core.hooks.support.message_envelope import compose, render
from coordinator_core.hooks.support.session_hub import (
    ensure_session_dir,
    session_id_is_real,
)
from coordinator_core.ipc import register_op

_WIKI_ANCHOR = (
    "coordinator/docs/wiki/coordinator-tripwires/"
    "a-hand-authored-workflow-costs-4x-the-plan-execution.md"
)

# Mirrors nudge_multiwave_workflow's own session_id format guard.
_SESSION_ID_RE = re.compile(r"^[a-zA-Z0-9_-]{4,}$")

_SENTINEL_NAME = "workflow-authoring-trampoline-nudged"

_TARGET_SKILL_NAMES = {"workflow-authoring", "coordinator:workflow-authoring"}


def _compose_skill_offer(env: object = None) -> str:
    prose = (
        "workflow-authoring trampoline: for plan dispatch, use instead: "
        "`emit-dispatch-workflow.py --plan <plan>` then fire it via "
        "`coordinator:execute-plan`. Fan-out (review/research)? Author it."
    )
    return render(compose(prose), env=env)


def _compose_inline_offer(env: object = None) -> str:
    prose = (
        "workflow-authoring trampoline: this inline `script:` is "
        "hand-authored. For plan dispatch, use instead: "
        "`emit-dispatch-workflow.py --plan <plan>` then `coordinator:"
        "execute-plan`. Fan-out (review/research)? Author it."
    )
    return render(compose(prose), env=env)


def _extract_skill_name(tool_input: object) -> Optional[str]:
    if not isinstance(tool_input, Mapping):
        return None
    skill = tool_input.get("skill")
    if not isinstance(skill, str):
        skill = tool_input.get("command")
    if not isinstance(skill, str):
        return None
    return skill


def _is_inline_script_launch(tool_input: object) -> bool:
    if not isinstance(tool_input, Mapping):
        return False
    script = tool_input.get("script")
    if not isinstance(script, str) or not script.strip():
        return False
    script_path = tool_input.get("scriptPath")
    if isinstance(script_path, str) and script_path.strip():
        return False
    return True


@register_op("hooks.nudge_workflow_authoring_trampoline")
async def _handler(params: dict, repo_root=None) -> dict:
    """PreToolUse(Skill, Workflow) op: nudge toward the emitted-and-fired
    path at either of the two hand-authoring entry points, once per session.
    """
    # Review: coordinator-code-reviewer — normalize the two params shapes
    # both engine doors and the cold chain send (see block_worktree_tool).
    params = payload_of(params)
    tool_name = params.get("tool_name")
    tool_input = params.get("tool_input")

    if tool_name == "Skill":
        skill_name = _extract_skill_name(tool_input)
        if not skill_name or skill_name.strip().lower() not in _TARGET_SKILL_NAMES:
            return no_advisory()
        compose_fn = _compose_skill_offer
    elif tool_name == "Workflow":
        if not _is_inline_script_launch(tool_input):
            return no_advisory()
        compose_fn = _compose_inline_offer
    else:
        return no_advisory()

    session_id = params.get("session_id")
    if not isinstance(session_id, str) or not session_id:
        return no_advisory()
    if not _SESSION_ID_RE.match(session_id):
        return no_advisory()
    if not session_id_is_real(session_id):
        return no_advisory()

    try:
        git_root = show_toplevel()
    except Exception:
        git_root = None
    if not git_root:
        return no_advisory()

    common_dir = resolve_git_common_dir(git_root)
    if not common_dir:
        return no_advisory()

    session_dir = Path(common_dir) / "coordinator-sessions" / session_id
    nudged_sentinel = session_dir / _SENTINEL_NAME
    try:
        if nudged_sentinel.is_file():
            return no_advisory()
    except Exception:
        return no_advisory()

    message = compose_fn(params.get("env"))

    try:
        ensure_session_dir(session_dir, session_id)
        nudged_sentinel.touch()
    except Exception:
        pass

    return allow_advisory("PreToolUse", message)
