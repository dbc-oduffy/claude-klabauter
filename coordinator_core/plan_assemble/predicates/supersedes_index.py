from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from coordinator_core.plan_assemble.predicates import PredicateContext, undetermined

_PLANS_DIR = "docs/plans"


def _plan_identity(fpath: Path, frontmatter: dict[str, Any]) -> str:
    plan_id_val = frontmatter.get("plan_id")
    if isinstance(plan_id_val, str) and plan_id_val:
        return plan_id_val
    return fpath.stem


def _build_reverse_index(plans_dir: Path) -> dict[str, list[str]]:
    reverse_index: dict[str, list[str]] = {}

    if not plans_dir.is_dir():
        return reverse_index

    for fpath in sorted(plans_dir.glob("*.md")):
        try:
            raw = fpath.read_text(encoding="utf-8").replace("\r\n", "\n")
            if not raw.startswith("---\n"):
                continue
            fm_text = raw.split("---\n", 2)[1]
            frontmatter = yaml.safe_load(fm_text)
        except Exception:
            continue

        if not isinstance(frontmatter, dict):
            continue

        superseded_by = frontmatter.get("superseded_by")
        if not isinstance(superseded_by, str) or not superseded_by.strip():
            continue

        successor_identity = _plan_identity(fpath, frontmatter)
        reverse_index.setdefault(superseded_by.strip(), []).append(successor_identity)

    return reverse_index


def supersedes_plan(ctx: PredicateContext) -> dict[str, Any]:
    if ctx.plan_path is None:
        return undetermined("no --plan supplied: cannot resolve this plan's own identity")

    plans_dir = ctx.repo_root / _PLANS_DIR
    reverse_index = _build_reverse_index(plans_dir)

    candidate_identities = [ctx.plan_path.stem, ctx.plan_path.name]
    if ctx.plan_frontmatter is not None:
        plan_id_val = ctx.plan_frontmatter.get("plan_id")
        if isinstance(plan_id_val, str) and plan_id_val:
            candidate_identities.insert(0, plan_id_val)

    for identity in candidate_identities:
        successors = reverse_index.get(identity)
        if successors:
            return {"present": True, "target": successors[0]}

    return {"present": False, "target": None}


__all__ = ["supersedes_plan"]
