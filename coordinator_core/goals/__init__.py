"""coordinator_core.goals — per-repo goal-artifact analysis ops.

Purpose: namespace for ops that read/derive state from state/goals/*.yaml
whole-document goal artifacts (schema: goal, C1 2026-07-13 shape — no '---'
frontmatter fence, the entire file IS the record).

C6 (docs/plans/2026-09-11-document-scaffolding-is-emitted-not-remembered.md)
adds this module's own `brief()` — this host's scaffold-emission compute
for the two rows `coordinator_core/ops/doctype_hosts.py` marks `emitted`
against `module="coordinator_core.goals"`: `goal` and `goal-seed` (both
keyed `ceremony="goals"`). It computes each type's `coordinator-doc-new`
directive through the shared constructor
(`coordinator_core.roadmap_planning_assemble.scaffold_directive.
build_scaffold_directive`, C1) from a caller's own already-resolved
ceremony state — a ratified goal title for `goal`, a deferred vision-slice
title (plus an optional ratified-goal FK list) for `goal-seed` — never a
caller-supplied free-text argument threaded straight through. Additive and
gated, same shape as `roadmap_planning_assemble.brief`'s C3 precedent: a
caller supplying neither `goal_title` nor `goal_seed_title` gets no
directive, so every pre-C6 caller of this package is unaffected.

Spec backlink: DoE-claude:pln-per-repo-okr-goal-setting-syst-80bced § C6
Spec backlink: docs/plans/2026-09-11-document-scaffolding-is-emitted-not-
remembered.md, chunk C6.

Negative spec (C6's slice): does NOT decide whether `goal`/`goal-seed` are
emitted at all — that is `coordinator_core.ops.doctype_hosts`'s (C0) table,
read by C8's coverage pin, never re-derived here. Does NOT call
`coordinator-doc-new` or any other CLI — a compute-half constructor only
(§ Which discriminator this plan uses). Does NOT read or write any
`state/goals/*.yaml` file itself — `reassess_krs.py`/`wire_read.py` own
that surface; this module's own writes are scoped to `__init__.py` alone.
"""
from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any, Optional, Sequence

from coordinator_core.roadmap_planning_assemble.scaffold_directive import (
    Flag,
    build_scaffold_directive,
)


def _slug(text: str) -> str:
    """Lowercase-dash slug, mirroring `coordinator-doc-new`'s own
    `_slug_from_title` closely enough for a computed (never free-text)
    `--out` default — collapses any run of non-alphanumeric characters to
    a single dash and strips leading/trailing dashes."""
    out = []
    prev_dash = False
    for ch in text.lower():
        if ch.isalnum():
            out.append(ch)
            prev_dash = False
        elif not prev_dash:
            out.append("-")
            prev_dash = True
    return "".join(out).strip("-") or "untitled"


# C6: the shared constructor's (C1) per-type required-flag computation for
# this host's two emitted rows (coordinator_core/ops/doctype_hosts.py --
# both keyed (ceremony="goals"), module=this package).
_GOAL_FLAG_SPEC: tuple[Flag, ...] = (
    Flag("--title", "title", required=True),
)

# `--goals` is comma-joined, not repeated: `coordinator-doc-new`'s
# `--goals` is a single `GOAL_ID[,GOAL_ID...]` string argument (not an
# `action="append"` flag) — same reason `roadmap_planning_assemble` joins
# its own `--goals` rather than letting the shared constructor's per-item
# repeat shape run over a list value. Optional here (unlike
# `roadmap-seed`'s own `--goals`): a `goal-seed` may capture a deferred
# vision-slice with no ratified-goal FK yet.
_GOAL_SEED_FLAG_SPEC: tuple[Flag, ...] = (
    Flag("--title", "title", required=True),
)


def _goal_directive(title: str) -> dict[str, Any]:
    """Computes the `goal` scaffold directive through the shared
    constructor (C1) from a caller's own already-ratified goal title.
    `--out` mirrors `coordinator-doc-new`'s own `goal` default path
    (`state/goals/YYYY-MM-DD-<slug>.yaml`) — computed here, never left to
    the CLI's own placeholder-title default, so a replayed directive's
    `already_satisfied` predicate is meaningful."""
    root = Path.cwd()
    today = date.today().isoformat()
    out_path = f"state/goals/{today}-{_slug(title)}.yaml"
    resolved = {"title": title, "out": out_path}
    return build_scaffold_directive(
        "d-scaffold-goal",
        "goal",
        resolved,
        _GOAL_FLAG_SPEC,
        root=root,
    )


def _goal_seed_directive(title: str, goals: Optional[Sequence[str]]) -> dict[str, Any]:
    """Computes the `goal-seed` scaffold directive through the shared
    constructor (C1) from a caller's own already-resolved deferred
    vision-slice title, plus an OPTIONAL ratified-goal FK list (`origin_
    goal_id` in the emitted frontmatter — schema field name, comma-joined
    per `--goals`'s real argument shape). `--out` mirrors
    `coordinator-doc-new`'s own `goal-seed` default path (`state/handoffs/
    YYYY-MM-DD-<slug>.md`)."""
    root = Path.cwd()
    today = date.today().isoformat()
    out_path = f"state/handoffs/{today}-{_slug(title)}.md"
    resolved: dict[str, Any] = {"title": title, "out": out_path}
    if goals:
        resolved["goals"] = ",".join(goals)
    flag_spec = _GOAL_SEED_FLAG_SPEC
    if goals:
        flag_spec = flag_spec + (Flag("--goals", "goals", required=False),)
    return build_scaffold_directive(
        "d-scaffold-goal-seed",
        "goal-seed",
        resolved,
        flag_spec,
        root=root,
    )


def brief(
    *,
    goal_title: Optional[str] = None,
    goal_seed_title: Optional[str] = None,
    goals: Optional[Sequence[str]] = None,
) -> dict[str, Any]:
    """C6: this module's own scaffold-emission compute. Returns
    `{"directives": [...]}`: a `goal` directive when the caller has
    resolved `goal_title` (a ratified goal's own title), and/or a
    `goal-seed` directive when the caller has resolved `goal_seed_title`
    (a deferred vision-slice's own title, `goals` its optional
    ratified-goal FK list) — additive and gated, same shape as
    `roadmap_planning_assemble.brief`'s C3 precedent, so a caller
    supplying neither is unaffected.
    """
    directives: list[dict[str, Any]] = []
    if goal_title:
        directives.append(_goal_directive(goal_title))
    if goal_seed_title:
        directives.append(_goal_seed_directive(goal_seed_title, goals))
    return {"directives": directives}
