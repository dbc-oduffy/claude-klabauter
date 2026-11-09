"""Infer the files an undeclared-writes ask will touch from its sizing's plan spine."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping


def inferred_writes(root: Path, sizing_doc: Mapping[str, Any]) -> tuple[str, ...]:
    """Sorted `writes:` union of the plan spine the sizing's `plan:` names; () when it names no plan
    under `docs/plans/` or the plan declares no writes. Reads one file; no git, no spawn."""
    plan_rel = sizing_doc.get("plan") if isinstance(sizing_doc, Mapping) else None
    if not isinstance(plan_rel, str) or not plan_rel.strip():
        return ()
    from coordinator_core.ops.dispatch_emit.cross_plan_write_overlap import _declared_write_paths

    try:
        plans = (Path(root) / "docs" / "plans").resolve()
        plan = (Path(root) / plan_rel.strip()).resolve()
        if plans not in plan.parents or not plan.is_file():
            return ()
        return tuple(sorted(_declared_write_paths(plan)))
    except OSError:
        return ()
