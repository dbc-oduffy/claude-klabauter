"""
coordinator_core.plan_assemble.predicates.supersedes_index — Layer 0 leaf
reader for contract row `:164`.

Purpose: answer "does anything declare THIS plan as its predecessor" by
building a reverse index over every `docs/plans/*.md` plan's `superseded_by`
frontmatter field (`coordinator_core/frontmatter/schemas/plan.schema.json:43`)
and looking up the current plan's own path/slug in the inverted mapping.

RULING, inherited from chunk C7's contract row: the contract's row asks for a
plan frontmatter `supersedes:` key. That key does not exist on `plan.schema.
json` and must not be added — `frontmatter/schema_validate.py`'s
`_cf_supersedes_spinoff_only` already claims the bare `supersedes` name for
an unrelated, spinoff-only baton field, and layering a THIRD meaning onto one
key name is the defect the ruling forecloses. What exists, and what this
module builds its answer from, is `superseded_by` — declared BY the
superseding plan, pointing AT the plan it replaces. This module walks every
plan's `superseded_by` value, inverts it into predecessor -> [successors],
and answers the row by membership in that inverted index — a derived view
over a shipped field, not a new key.

Read path mirrors `coordinator_core.ops.plan_match._collect_plans`: split on
the first `---\n` fence, `yaml.safe_load` the frontmatter block only, skip
(quarantine) anything that fails to parse or isn't a dict — never raise.

Negative-spec:
  - Does NOT add, read, or reference a `supersedes:` key on any schema or any
    plan document. `plan.schema.json` is untouched by this chunk; the ONLY
    field this module reads off any plan is `superseded_by`.
  - Does NOT touch `_cf_supersedes_spinoff_only` or any other
    `schema_validate.py` rule — this module is read-only over
    `docs/plans/*.md` and never validates or mutates frontmatter.
  - Does NOT re-implement a second `docs/plans/*.md` walk from scratch. The
    directory-glob-and-frontmatter-split shape mirrors `ops.plan_match.
    _collect_plans` exactly (same fence-split, same quarantine-on-parse-
    error posture) rather than inventing a third variant.
  - Does NOT decide truthiness beyond "is this plan's identity present as a
    value in the reverse index" — no fuzzy match, no title comparison, no
    git history. A `superseded_by` value that doesn't textually match any
    known plan's identity simply never appears as a key; the row for that
    plan reports `present=False`, not an error.
  - Does NOT resolve a missing `PredicateContext.plan_path` by scanning
    `docs/plans/` for a "best guess" match — an absent `plan_path` is an
    `undetermined` row per the shared seam's contract, not a inference
    opportunity.
  - Does NOT cache the built index across calls — one `_build_reverse_index`
    per `supersedes_plan(...)` invocation, matching `PredicateContext`'s own
    no-memoization negative-spec.

Spec backlink: pln-plan-assemble-wave-2-the-predi-fad89b, chunk C7
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from coordinator_core.plan_assemble.predicates import PredicateContext, undetermined

#: Relative to `PredicateContext.repo_root` — mirrors `ops.plan_match`'s
#: `docs/plans/*.md` resolution.
_PLANS_DIR = "docs/plans"


def _plan_identity(fpath: Path, frontmatter: dict[str, Any]) -> str:
    """The identity a plan is addressed by elsewhere in the corpus: its
    `plan_id` frontmatter value when present, else the filename stem —
    the same fallback `ops.plan_match._collect_plans` uses."""
    plan_id_val = frontmatter.get("plan_id")
    if isinstance(plan_id_val, str) and plan_id_val:
        return plan_id_val
    return fpath.stem


def _build_reverse_index(plans_dir: Path) -> dict[str, list[str]]:
    """Walk `plans_dir/*.md`, invert each plan's `superseded_by` into a
    predecessor-identity -> [successor-identity, ...] mapping.

    Graceful-absent (`plans_dir` missing) returns `{}`, matching
    `ops.plan_match._collect_plans`'s own graceful-absent posture. Any
    single plan that fails to parse or carries non-dict frontmatter is
    quarantined (skipped), never raised.
    """
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
    """Contract row `:164` -> `gates.composition.supersedes_plan.present`
    (bool), `.target` (path/slug of the FIRST declared successor, if any).

    Answers "does anything declare THIS plan as its predecessor" by
    building the `superseded_by` reverse index over `<repo_root>/docs/plans`
    and looking up this plan's own identity (its frontmatter `plan_id`, its
    `plan_path` stem, and the bare `plan_path.name`, each tried in turn) as
    a key in that index.

    `undetermined(...)` (never `False`) if `ctx.plan_path` is `None` (no
    plan supplied — there is no "this plan" to look up).
    """
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
