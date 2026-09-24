"""Phase-1 pre-execution directive list for `/execute-plan`.

Pure function, zero I/O, zero spawns: `pre_execution_directives` returns the
ordered directive list plus the `judgment_points` gating it, in the shape
`coordinator_core.contract.apply_base.execute_directives` consumes. This
module owns none of the gate mechanism (`apply_base` already implements
`judgment_points_by_id`, `disposition_resolves_directive`,
`classify_judgment_point_ids` and `APPLY_EXIT_HALTED_AT_JUDGMENT`) — it only
populates the list the runner already reads.

ONLY d1 (pickup-assemble stamp-check) and d2 (review-exec-auth-stamp
authorize-invocation) are contiguous Phase-1 in the source skill
(`coordinator/skills/execute-plan/SKILL.md`, DoE-claude). Three stop-capable
gates sit between the mint and the claim — the remaining-context gate
(Phase 1 item 3), the Executability Gate (Phase 1.4), and the roadmap
execution gate (Phase 1.5) — plus the vehicle/chunk-pair classification and
wave map, all ahead of d3/d4. Each of the three gates is one
`judgment_points[]` entry; the wave map is a fourth. d3 (session-claim-cli
claim-plan --for-execution) depends on the three gates; d4 (the workflow
emit) depends on the three gates plus the wave-map point.
"""

from __future__ import annotations

from pathlib import PurePosixPath
from typing import Any

from coordinator_core.contract.decision_object.judgment import (
    build_disposition,
    build_untrusted_gate_judgment_point,
)

#: Judgment-point ids, named so ordering below stays obviously self-consistent.
_J_REMAINING_CONTEXT = "j-remaining-context"
_J_EXECUTABILITY_GATE = "j-executability-gate"
_J_ROADMAP_EXECUTION_GATE = "j-roadmap-execution-gate"
_J_WAVE_MAP = "j-wave-map"

#: The three gates d3 depends on; d4 depends on these plus the wave map.
_GATE_IDS = (_J_REMAINING_CONTEXT, _J_EXECUTABILITY_GATE, _J_ROADMAP_EXECUTION_GATE)


def _slug_for(plan_path: str) -> str:
    """Plan basename minus `.md` — mirrors `execute-plan/SKILL.md`'s own
    `slug="$(basename "$ARGUMENTS" .md)"` and its Python port
    (`coordinator_core.ops.fleet._common.plan_claim_dir`'s `plan_path.stem`
    keying). `PurePosixPath`, not the OS-dependent `pathlib.Path`: plan
    paths are repo-relative forward-slash paths regardless of host OS.
    """
    return PurePosixPath(plan_path).stem


def _build_judgment_points() -> list[dict[str, Any]]:
    """The three stop-capable gates plus the wave map, each an untrusted
    gate judgment point — none of these is mechanically decidable from disk
    state alone, matching the skill's own judgment residue at each step.
    """
    return [
        build_untrusted_gate_judgment_point(
            id=_J_REMAINING_CONTEXT,
            question=(
                "Phase 1 item 3: is there enough remaining context left in "
                "this session to execute this plan?"
            ),
            dispositions=[build_disposition("proceed", ["d3", "d4"])],
            evidence="Left to the caller SKILL.md's own remaining-context check.",
            reason="insufficient-evidence",
        ),
        build_untrusted_gate_judgment_point(
            id=_J_EXECUTABILITY_GATE,
            question=(
                "Phase 1.4 Executability Gate: does any of the six named "
                "signals bounce this plan back to /plan?"
            ),
            dispositions=[build_disposition("proceed", ["d3", "d4"])],
            evidence="Left to the caller SKILL.md's own Executability Gate.",
            reason="insufficient-evidence",
        ),
        build_untrusted_gate_judgment_point(
            id=_J_ROADMAP_EXECUTION_GATE,
            question=(
                "Phase 1.5 roadmap execution gate: check before claiming "
                "this plan for execution."
            ),
            dispositions=[build_disposition("proceed", ["d3", "d4"])],
            evidence="Left to the caller SKILL.md's own roadmap execution gate.",
            reason="insufficient-evidence",
        ),
        build_untrusted_gate_judgment_point(
            id=_J_WAVE_MAP,
            question=(
                "Vehicle/chunk-pair classification and wave map: is the "
                "wave map settled ahead of the workflow emit?"
            ),
            dispositions=[build_disposition("proceed", ["d4"])],
            evidence="Left to the caller SKILL.md's own wave-map step.",
            reason="insufficient-evidence",
        ),
    ]


def pre_execution_directives(
    plan_path: str, *, autonomous: bool = False
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Return `(directives, judgment_points)` for `/execute-plan` Phase 1,
    in the shape `apply_base.execute_directives` consumes.

    `autonomous=True` omits d1/d2 (the skill skips both under `/autonomous`)
    and returns d3+d4 only, still gated by the same `judgment_points`.
    """
    slug = _slug_for(plan_path)

    d3 = {
        "id": "d3",
        "cli": "session-claim-cli",
        "args": ["claim-plan", slug, "--for-execution"],
        "depends_on": list(_GATE_IDS),
    }
    d4 = {
        "id": "d4",
        "cli": "emit-dispatch-workflow",
        "args": ["--plan", plan_path],
        "depends_on": list(_GATE_IDS) + [_J_WAVE_MAP],
    }

    if autonomous:
        directives = [d3, d4]
    else:
        d1 = {
            "id": "d1",
            "cli": "pickup-assemble",
            "args": ["stamp-check", plan_path],
            "depends_on": None,
        }
        d2 = {
            "id": "d2",
            "cli": "review-exec-auth-stamp",
            "args": [
                "authorize-invocation",
                plan_path,
                "--typed-command",
                "/execute-plan",
            ],
            "depends_on": None,
        }
        directives = [d1, d2, d3, d4]

    return directives, _build_judgment_points()
