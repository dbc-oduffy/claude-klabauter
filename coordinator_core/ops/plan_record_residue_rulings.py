"""
coordinator_core.ops.plan_record_residue_rulings — JSON-RPC "plan.record_residue_rulings".

Purpose: the writer of a plan's ``pm_ruling:`` list. The mise-prep residue
workflow's APM step only rules; this op persists each ruling so the next round
does not raise the same item again (``mise_residue.ruled_items``).

Wire params:
    run_id   (str, required) — the residue run the rulings came from.
    on       (str, required) — the date stamped on each entry.
    rulings  (list, required) — ``{id, item_kind, plan, ruling, pmOnly,
             subjectSha?, cycle_plans?}``. ``id`` is the item id
             (``census[i]`` / ``cycle:<path>``).

Reply: ``{"applied": [{plan, id}], "refused": [{plan, id, reason}]}``. A ruling
that cannot be written is a refusal in the reply, never an exception.

Negative-spec:
  - Does NOT raise on a bad ruling: an uncontained or missing plan, a cycle
    ruling whose plan is absent from its ``cycle_plans``, an empty ruling and a
    malformed row are each refused by name.
  - Does NOT touch any frontmatter outside the ``pm_ruling`` block.
  - Does NOT commit, and does NOT spawn.
  - Merges by ``item``: a ruling for an id already in the list replaces it; other
    entries stay as they were.
"""

from __future__ import annotations

MUTATES = ["docs/plans/*.md"]  # rewrites the pm_ruling block of each caller-named plan only
GENERATES: list = []

from pathlib import Path
from typing import Any, Dict, List, Optional

from coordinator_core.frontmatter.primitives import (
    rebuild,
    split_frontmatter,
    write_fm_nested_field,
)
from coordinator_core.ipc import register_op
from coordinator_core.lifecycle import main_worktree_root
from coordinator_core.locked_write import LockTimeout, MutateAbort, locked_rmw
from coordinator_core.ops._path_guard import contained_path
from coordinator_core.roadmap.mise_residue import RULINGS_OP, render_pm_ruling


def _existing_entries(text: str) -> List[dict]:
    from coordinator_core.frontmatter.schema_validate import parse_frontmatter

    fm = parse_frontmatter(text).get("frontmatter")
    block = fm.get("pm_ruling") if isinstance(fm, dict) else None
    if not isinstance(block, list):
        return []
    return [e for e in block if isinstance(e, dict) and isinstance(e.get("item"), str)]


def _entry(r: dict, run_id: str, on: str) -> dict:
    return {
        "item": r["id"],
        "ruling": r["ruling"].strip(),
        "run_id": run_id,
        "on": on,
        "pm_only": bool(r.get("pmOnly")),
        "subject_sha": r.get("subjectSha") or "",
    }


def _refusal_for(r: Any) -> Optional[str]:
    if not isinstance(r, dict):
        return "ruling row is not an object"
    if not isinstance(r.get("id"), str) or not r["id"].strip():
        return "ruling has no item id"
    if not isinstance(r.get("plan"), str) or not r["plan"].strip():
        return "ruling names no plan"
    if not isinstance(r.get("ruling"), str) or not r["ruling"].strip():
        return "empty ruling"
    if r.get("item_kind") == "seam-fix-cycle" or r["id"].startswith("cycle:"):
        cycle_plans = r.get("cycle_plans")
        if not isinstance(cycle_plans, list) or r["plan"] not in cycle_plans:
            return "plan is not in the cycle's plans"
    return None


@register_op(RULINGS_OP)
def _handler(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC "plan.record_residue_rulings" handler. See module docstring."""
    if repo_root is None:
        raise ValueError("plan.record_residue_rulings requires a resolved repo_root")
    run_id, on, rulings = params.get("run_id"), params.get("on"), params.get("rulings")
    if not isinstance(run_id, str) or not run_id.strip():
        raise ValueError("run_id must be a non-empty string")
    if not isinstance(on, str) or not on.strip():
        raise ValueError("on must be a non-empty string")
    if not isinstance(rulings, list):
        raise ValueError("rulings must be a list")

    worktree_root = Path(main_worktree_root(repo_root))
    applied: List[Dict[str, str]] = []
    refused: List[Dict[str, str]] = []
    by_plan: Dict[str, List[dict]] = {}

    for r in rulings:
        reason = _refusal_for(r)
        if reason is not None:
            refused.append(
                {
                    "plan": str(r.get("plan", "")) if isinstance(r, dict) else "",
                    "id": str(r.get("id", "")) if isinstance(r, dict) else "",
                    "reason": reason,
                }
            )
            continue
        by_plan.setdefault(r["plan"].strip(), []).append(r)

    for raw_plan, group in by_plan.items():
        candidate = Path(raw_plan)
        if not candidate.is_absolute():
            candidate = worktree_root / candidate
        if contained_path(candidate, [worktree_root]) is None:
            refused += [
                {"plan": raw_plan, "id": r["id"], "reason": "plan escapes the resolved worktree"}
                for r in group
            ]
            continue

        def mutate(old_text: str, group: List[dict] = group) -> str:
            split = split_frontmatter(old_text)
            if split is None:
                raise MutateAbort("no parseable YAML frontmatter")
            merged = {e["item"]: e for e in _existing_entries(old_text)}
            for r in group:
                merged[r["id"]] = _entry(r, run_id, on)
            fm_text = write_fm_nested_field(
                split.fm_text, "pm_ruling", render_pm_ruling(merged.values()).split("\n", 1)[1]
            )
            return rebuild(split, fm_text)

        try:
            locked_rmw(candidate, mutate, repo_root=repo_root)
        except FileNotFoundError:
            reason = "no such plan"
        except LockTimeout as exc:
            reason = f"timed out waiting for the file lock: {exc}"
        except MutateAbort as exc:
            reason = str(exc.args[0]) if exc.args else "mutation aborted"
        else:
            applied += [{"plan": raw_plan, "id": r["id"]} for r in group]
            continue
        refused += [{"plan": raw_plan, "id": r["id"], "reason": reason} for r in group]

    return {"applied": applied, "refused": refused}
