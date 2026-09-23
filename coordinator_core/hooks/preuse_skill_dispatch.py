"""coordinator_core.hooks.preuse_skill_dispatch — PreToolUse(Skill) fan-in
op: N registered advisory legs, concatenated, one call.

Arrival note (W4-C8, docs/plans/2026-09-18-doe-holds-no-scripts.md): ported
from DoE-claude `coordinator/hooks/scripts/preuse-skill-dispatch.py`, a
threaded subprocess fan-in over four `compute_context(stdin_text) -> str |
None` legs (each imported via `importlib.util.spec_from_file_location`,
three called directly, the fourth — the trampoline — via a stdin-swap/
stdout-capture wrapper because it has no `compute_context` entry point of
its own). None of the subprocess-avoidance or stdin/stdout-capture plumbing
applies here: every leg this op reaches is a same-repo, in-process
`hooks.<name>` op returning an envelope dict directly.

Pre-gate (unchanged from source): `read_invocation` classifies the payload
ONCE. A `Skill` call naming no leg's verb imports NOTHING beyond this
module itself — the all-miss path (almost every Skill-tool call) never
pays for a single leg's import.

REGISTRY rows — (leg id, op name, verb set). NONE of the four legs below
are landed in this engine as of this dispatch (each is a SIBLING row's
writes: per the plan's inventory table:
`nudge_workflow_authoring_trampoline` and `handoff_segment_inject`/
`mise_autofire`/`pickup_autofire` are W4-C9/W4-C12 respectively) — each is
imported LAZILY, inside its own leg call, inside its own per-leg isolation,
so this dispatcher degrades to "every matched leg absent, skip" today and
starts concatenating real context the moment each sibling row lands, with
no edit to this file required (same reuse-by-name shape
`preuse_agent_dispatch`'s own leg 1 uses for its own not-yet-landed
sibling):

    - nudge_workflow_authoring_trampoline  {"workflow-authoring"}
    - pickup_autofire        {"pickup", "mise-en-place", "warp-speed-execute"}
    - mise_autofire          {"mise-en-place", "warp-speed-execute"}
    - handoff_segment_inject {"handoff"}

`group_em_autofire` is deliberately ABSENT (DoE plan's own anti-scope) —
never add it here.

Concurrency: DELIBERATELY SEQUENTIAL, not threaded, unlike the DoE source.
That script's thread-per-leg concurrency existed to bound FOUR SEPARATE
SUBPROCESS SPAWNS (each leg shelling out to its own forwarder CLI) under one
shared sub-deadline strictly below the harness's own PreToolUse(Skill)
registration timeout — the cost this dispatcher exists to avoid entirely.
Every leg here runs in-process, in the SAME event loop this op's own
`async def` handler already runs in; each leg is `await`-ed in turn inside
its own `try`/`except BaseException`, so one leg's failure or hang is
isolated exactly as the threaded version isolated it, without introducing
thread-safety obligations (shared-dict writes, `is_alive()` polling) that
buy nothing once there is no subprocess boundary to wait across.

Isolation (unchanged from source): each leg's own exception is caught
individually and skips only that leg (fail-open for it alone), never
affecting a sibling. This op always returns a context-only envelope or
`no_advisory()` — never `permissionDecision`; every leg here is advisory
only.

A raising leg leaves a best-effort stderr breadcrumb naming the leg and
the exception, matching `preuse_agent_dispatch` and
`agent_postuse_dispatch`. Without it a leg that raised and a leg that
genuinely had nothing to say emit the identical envelope, so a silently
broken `pickup_autofire` reads as an empty baton spool — observed on this
Skill entry path, coordinator-claude#50's shape. The breadcrumb never
reaches the agent's context: a leg failure is operator diagnostics, not
advisory text.

Emission: collect each matched leg's non-empty text, in REGISTRY order,
and emit exactly ONE `context_only("PreToolUse", "\\n\\n".join(parts))` — or
`no_advisory()` when no leg produced a part.

Op contract: `params` is the flat PreToolUse payload dict (`tool_name ==
"Skill"`, `tool_input`, `session_id`, `cwd`, `agent_id`, …).

Negative-spec:
    Does not re-implement any leg's own logic — every leg's decision stays
    inside its own module, dialled by name.
    Does not host `group_em_autofire`.
    Does not change the trampoline's own once-per-session sentinel or its
    Workflow-tool leg — this op is registered on `PreToolUse(Skill)` only
    and never sees a `tool_name != "Skill"` payload.

Spec backlink: docs/plans/2026-09-18-doe-holds-no-scripts.md § W4-C8
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from typing import FrozenSet, List, Optional, Tuple

from coordinator_core.hooks._envelope import context_only, no_advisory
from coordinator_core.hooks.support.skill_invocation import read_invocation
from coordinator_core.ipc import register_op

_TRAMPOLINE_VERBS: FrozenSet[str] = frozenset({"workflow-authoring"})
_PICKUP_AUTOFIRE_VERBS: FrozenSet[str] = frozenset(
    {"pickup", "mise-en-place", "warp-speed-execute"}
)
_MISE_AUTOFIRE_VERBS: FrozenSet[str] = frozenset({"mise-en-place", "warp-speed-execute"})
_HANDOFF_SEGMENT_INJECT_VERBS: FrozenSet[str] = frozenset({"handoff"})


@dataclass(frozen=True)
class SkillLeg:
    leg_id: str
    module_path: str
    verbs: FrozenSet[str]


REGISTRY: Tuple[SkillLeg, ...] = (
    SkillLeg(
        "trampoline",
        "coordinator_core.hooks.nudge_workflow_authoring_trampoline",
        _TRAMPOLINE_VERBS,
    ),
    SkillLeg(
        "pickup-autofire",
        "coordinator_core.hooks.pickup_autofire",
        _PICKUP_AUTOFIRE_VERBS,
    ),
    SkillLeg(
        "mise-autofire",
        "coordinator_core.hooks.mise_autofire",
        _MISE_AUTOFIRE_VERBS,
    ),
    SkillLeg(
        "handoff-segment-inject",
        "coordinator_core.hooks.handoff_segment_inject",
        _HANDOFF_SEGMENT_INJECT_VERBS,
    ),
)


def _extract_context_text(envelope) -> Optional[str]:
    """Narrow a leg op's returned envelope down to its bare
    `additionalContext` text, or `None` when it has nothing to say."""
    if not isinstance(envelope, dict):
        return None
    hso = envelope.get("hookSpecificOutput")
    if not isinstance(hso, dict):
        return None
    ctx = hso.get("additionalContext")
    if isinstance(ctx, str) and ctx.strip():
        return ctx.strip()
    return None


async def _run_leg(leg: SkillLeg, params: dict) -> Optional[str]:
    """Import and run ONE matched leg, returning its extracted advisory
    text or `None`. Any exception (import failure — a not-yet-landed
    sibling module — or the leg's own crash) propagates to the caller's own
    per-leg isolation."""
    import asyncio
    import importlib

    mod = importlib.import_module(leg.module_path)
    handler = getattr(mod, "_handler")
    out = handler(params)
    if asyncio.iscoroutine(out):
        out = await out
    return _extract_context_text(out)


@register_op("hooks.preuse_skill_dispatch")
async def _handler(params: dict, repo_root=None) -> dict:
    """PreToolUse(Skill) op: run every verb-matched leg and concatenate
    their advisory text into one context-only envelope."""
    if not isinstance(params, dict):
        return no_advisory()

    inv = read_invocation(params)
    if inv is None:
        return no_advisory()

    matched = [leg for leg in REGISTRY if inv.command_name in leg.verbs]
    if not matched:
        return no_advisory()

    parts: List[str] = []
    for leg in matched:
        try:
            text = await _run_leg(leg, params)
        except BaseException as exc:  # fail-open for this leg alone
            try:
                print(
                    "preuse_skill_dispatch: leg=%s raised %s: %s — its sibling legs are "
                    "unaffected" % (leg.leg_id, type(exc).__name__, exc),
                    file=sys.stderr,
                )
            except Exception:
                pass
            continue
        if text:
            parts.append(text)

    if not parts:
        return no_advisory()

    return context_only("PreToolUse", "\n\n".join(parts))
