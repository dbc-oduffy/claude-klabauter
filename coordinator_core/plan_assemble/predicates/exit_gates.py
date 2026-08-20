"""
coordinator_core.plan_assemble.predicates.exit_gates — the `gates.exit.*`
Layer 0 leaf reader for the `plan-assemble` contract's row `:189`.

Purpose: row `:189` asks whether the plan's `sizing_object` frontmatter
citation is sound. This module answers it by CONSUMING
`coordinator_core.ops.assert_plan_sizing_citation`'s own scan
(`_scan_plans`) rather than re-parsing frontmatter or re-walking
`docs/plans/*.md` a second time — one citation checker in the tree, not
two independently-drifting ones.

**What `.passed` means, and what it does not.** `assert_plan_sizing_citation`
reads the plan's `sizing_object` key from PARSED FRONTMATTER ONLY — never
the plan body — by design (see that module's own docstring, AC6/AC9): a
plan's body may legitimately cite a `state/sizings/...` path in prose to
DOCUMENT that no sizing object was ever written for an ask, and a
body-scanning implementation would misfire on that plan. Consequently the
checker's `dangling` leg catches exactly one failure mode — a frontmatter
citation present but not resolving on disk (a WRONG citation) — and never
the other — no `sizing_object` key at all (a MISSING citation, the
checker's separate `missing` leg, which this row does not surface). So:
`gates.exit.sizing_object_flag.passed == True` means "the plan's
frontmatter did not cite a dangling sizing object" (including the case
where it cited nothing at all, which passes trivially). It must NEVER be
read as "the plan cited its sizing object" — a plan with no citation, or
an explicit `sizing_object: null`, also reports `passed: True` here, and a
missing-citation finding (if the caller wants one) belongs to a different
row than `:189`.

Negative-spec:
  - Does NOT re-implement frontmatter parsing or a second `dangling`/
    `missing` scan. `_scan_plans` is imported and called as-is.
  - Does NOT surface `assert_plan_sizing_citation`'s `missing` leg under
    this row. `:189` is the dangling-citation flag only.
  - Does NOT resolve `:195-198` (the terminal-table lookup keyed off this
    flag) — that composition is Layer 2, chunk C12's exclusive write
    target.
  - Does NOT emit `False` for an absent `--plan`. A `PredicateContext`
    with `plan_path is None` emits `undetermined(...)`, per the plan's
    Executor hard constraints.

Spec backlink: pln-plan-assemble-wave-2-the-predi-fad89b, chunk C8
"""
from __future__ import annotations

import os
from typing import Any

from coordinator_core.ops import assert_plan_sizing_citation as _sizing_citation

from . import PredicateContext, undetermined


def sizing_object_flag(context: PredicateContext) -> dict[str, Any]:
    """Row `:189` -> `gates.exit.sizing_object_flag.passed` (bool).

    Emits `{"passed": bool}` when `context.plan_path` is populated and
    resolvable against `context.repo_root`; emits the `undetermined`
    sentinel otherwise. See the module docstring for what `.passed` does
    and does not mean.
    """
    if context.plan_path is None:
        return undetermined(
            "no --plan supplied; gates.exit.sizing_object_flag.passed "
            "requires a plan path"
        )

    root = str(context.repo_root)
    try:
        rel_path = os.path.relpath(str(context.plan_path), root).replace(os.sep, "/")
    except ValueError:
        return undetermined(
            f"plan_path {context.plan_path!r} could not be resolved "
            f"relative to repo_root {root!r}"
        )

    dangling, _missing, unreadable = _sizing_citation._scan_plans(root)

    if rel_path in unreadable:
        return undetermined(
            f"plan {rel_path} was unreadable by assert_plan_sizing_citation's scan"
        )

    dangling_paths = {plan for plan, _cited in dangling}
    return {"passed": rel_path not in dangling_paths}


def build_exit_gates(context: PredicateContext) -> dict[str, Any]:
    """Compose this module's rows into the `gates.exit.*` sub-tree.

    Today this is the single `:189` row; the dict shape leaves room for a
    sibling row to join this namespace without changing the composition
    seam a Layer 2 consumer reads.
    """
    return {"sizing_object_flag": sizing_object_flag(context)}


__all__ = ["sizing_object_flag", "build_exit_gates"]
