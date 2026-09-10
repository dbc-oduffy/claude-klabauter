"""
coordinator_core.ops.roadmap_blitz_land — JSON-RPC "roadmap.blitz_land" operation.

Purpose: thin RPC wrapper over ``coordinator_core.roadmap.blitz_land``. This op is
what removes the operator from the plan-blitz LOOP: a wave already ran unattended
from fire to verdict, and this executes those verdicts and reports the next wave's
fire arguments, so a driver can run wave after wave with no judgment in between.

The verdicts are the EM's, made at the readiness gate. This op executes them; it
decides nothing.

Wire params:
    wave_result (dict, required) — the object the plan-blitz workflow returned:
                                    {waveIndex, ready[], pulled[], replan[],
                                     surfacedToPm[]}.
    branch      (str, optional)  — branch recorded on any minted replan baton.
    limit       (int, optional)  — max batons in the emitted next wave (default 8).
    shipped_in  (str, optional)  — SHA of the commit carrying the wave's XS work.
                                    REQUIRED whenever the wave dispatched any XS: closing a
                                    dispatched baton stamps `shipped` against it, and this op
                                    does not commit, so the caller commits first and passes
                                    the SHA. Omitted, every XS baton in the wave is refused.
                                    A wave is not a fire unit; see the skill.

Reply fields:
    {"approved": [...], "refused": [...], "minted": [...], "pulled": [...],
     "surfaced_to_pm": [...], "next_wave": {...}}

Negative-spec:
  - Does NOT commit. Landing writes records; committing them is the caller's act,
    so the diff can be read before it becomes history.
  - Does NOT fire the next wave. It reports what the next wave would be; firing
    stays one explicit call away rather than implicit in a landing, which is what
    keeps a runaway loop impossible.
  - Does NOT stamp a plan it cannot link to its baton. An unlinked approval is a
    silent no-op that reads as success — refusal is the point, not a limitation.
  - Does NOT re-queue `surfacedToPm`. Those await a PM answer, and retrying one
    would be answering on the PM's behalf.

Spec backlink: DoE-claude coordinator/skills/plan-blitz/SKILL.md § The flow, step 4.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from coordinator_core.ipc import register_op
from coordinator_core.lifecycle import main_worktree_root
from coordinator_core.roadmap.blitz_land import land_wave

# Generator-provenance: writes only the records the caller's wave result names.
GENERATES: list = []


@register_op("roadmap.blitz_land")
async def _handler(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC "roadmap.blitz_land" handler. See module docstring."""
    if repo_root is None:
        raise ValueError("roadmap.blitz_land requires a resolved repo_root")

    wave_result = params.get("wave_result")
    if not isinstance(wave_result, dict):
        raise ValueError("wave_result must be the workflow's returned object")

    branch = params.get("branch", "main")
    if not isinstance(branch, str) or not branch:
        raise ValueError("branch must be a non-empty string when supplied")

    limit = params.get("limit", 8)
    if not isinstance(limit, int) or limit < 1:
        raise ValueError("limit must be a positive integer")

    # `shipped_in` was accepted by `land_wave` and never forwarded from here, so every XS
    # baton landed through this op was refused for want of the SHA its caller had supplied.
    # That is the recycling defect the skill names: the baton stays open, returns as a
    # candidate in every later wave, and the wave still reports the work as done. Not
    # defaulted to a placeholder — `close_dispatched` validates the shape, and inventing a
    # value here would stamp `shipped` against a commit nobody made.
    shipped_in = params.get("shipped_in")
    if shipped_in is not None and (not isinstance(shipped_in, str) or not shipped_in.strip()):
        raise ValueError("shipped_in must be a non-empty string when supplied")

    return land_wave(
        Path(main_worktree_root(repo_root)),
        wave_result,
        branch=branch,
        limit=limit,
        shipped_in=shipped_in,
    )
