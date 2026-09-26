from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any, Optional, Sequence

from coordinator_core.roadmap_planning_assemble.scaffold_directive import (
    Flag,
    build_scaffold_directive,
)


def _slug(text: str) -> str:
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


_GOAL_FLAG_SPEC: tuple[Flag, ...] = (
    Flag("--title", "title", required=True),
)

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
    directives: list[dict[str, Any]] = []
    if goal_title:
        directives.append(_goal_directive(goal_title))
    if goal_seed_title:
        directives.append(_goal_seed_directive(goal_seed_title, goals))
    return {"directives": directives}
