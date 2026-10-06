"""Phase-1 pre-execution directive list for `/execute-plan`.

Zero spawns; the only I/O is d2's two best-effort reads (the plan's
frontmatter and its sizing object, see `_accepted_sizing_mode`):
`pre_execution_directives` returns the
ordered directive list plus the `judgment_points` gating it, in the shape
`coordinator_core.contract.apply_base.execute_directives` consumes. This
module owns none of the gate mechanism (`apply_base` already implements
`judgment_points_by_id`, `disposition_resolves_directive`,
`classify_judgment_point_ids` and `APPLY_EXIT_HALTED_AT_JUDGMENT`) — it only
populates the list the runner already reads.

ONLY d1 (pickup-assemble stamp-check) and d2 (review-exec-auth-stamp
authorize-invocation) are contiguous Phase-1 in the source skill
(`coordinator/skills/execute-plan/SKILL.md`, coordinator-content-repo). Three stop-capable
gates sit between the mint and the claim — the remaining-context gate
(Phase 1 item 3), the Executability Gate (Phase 1.4), and the roadmap
execution gate (Phase 1.5) — plus the vehicle/chunk-pair classification and
wave map, all ahead of d3/d4. Each of the three gates is one
`judgment_points[]` entry; the wave map is a fourth. d3 (session-claim-cli
claim-plan --for-execution) depends on the three gates; d4 (the workflow
emit) depends on the three gates plus the wave-map point.
"""

from __future__ import annotations

from pathlib import Path, PurePosixPath
from typing import Any

import yaml

from coordinator_core.frontmatter.primitives import read_fm_field_unquoted, split_frontmatter
from coordinator_core.ops.sizing_acceptance import acceptance_words

from coordinator_core.contract.decision_object.judgment import (
    build_disposition,
    build_untrusted_gate_judgment_point,
)

_J_REMAINING_CONTEXT = "j-remaining-context"
_J_EXECUTABILITY_GATE = "j-executability-gate"
_J_ROADMAP_EXECUTION_GATE = "j-roadmap-execution-gate"
_J_WAVE_MAP = "j-wave-map"

_GATE_IDS = (_J_REMAINING_CONTEXT, _J_EXECUTABILITY_GATE, _J_ROADMAP_EXECUTION_GATE)


def _slug_for(plan_path: str) -> str:
    """Plan basename minus `.md` — mirrors `execute-plan/SKILL.md`'s own
    `slug="$(basename "$ARGUMENTS" .md)"` and its Python port
    (`coordinator_core.ops.fleet._common.plan_claim_dir`'s `plan_path.stem`
    keying). `PurePosixPath`, not the OS-dependent `pathlib.Path`: plan
    paths are repo-relative forward-slash paths regardless of host OS.
    """
    return PurePosixPath(plan_path).stem


def _accepted_sizing_path(plan_path: str, repo_root: Path | None) -> str | None:
    """The plan's `sizing_object` path when that sizing is pm/ceo mode and carries
    an accepted exit criterion; None on any other shape, including a missing or
    unreadable plan or sizing -- the caller then keeps the typed-command arm."""
    if repo_root is None:
        return None
    try:
        split = split_frontmatter((repo_root / plan_path).read_text(encoding="utf-8"))
        rel = (read_fm_field_unquoted(split.fm_text, "sizing_object") or "").strip() if split else ""
        if not rel or rel.lower() in ("null", "~"):
            return None
        rel = rel.replace("\\", "/")
        doc = yaml.safe_load((repo_root / rel).read_text(encoding="utf-8"))
        ec = doc.get("exit_criterion") if isinstance(doc, dict) else None
        accepted = ec.get("accepted") if isinstance(ec, dict) else None
        if not acceptance_words(accepted):
            return None
        mode = str(doc.get("interaction_mode") or accepted.get("mode") or "").strip()
        return rel if mode in ("pm", "ceo") else None
    except (OSError, ValueError, yaml.YAMLError, AttributeError):
        return None


def _build_judgment_points() -> list[dict[str, Any]]:
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
    plan_path: str, *, autonomous: bool = False, repo_root: Path | None = None
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
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
        sizing_rel = _accepted_sizing_path(plan_path, repo_root)
        auth_arm = (
            ["--authorized-by-sizing", sizing_rel]
            if sizing_rel
            else ["--typed-command", "/execute-plan"]
        )
        d2 = {
            "id": "d2",
            "cli": "review-exec-auth-stamp",
            "args": [
                "authorize-invocation",
                plan_path,
                *auth_arm,
            ],
            "depends_on": None,
        }
        directives = [d1, d2, d3, d4]

    return directives, _build_judgment_points()
