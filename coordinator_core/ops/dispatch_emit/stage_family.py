"""Per-stage model-family resolver for emitted workflow stages.

A stage names a family or names none; none means claude. A non-claude family
dispatches to the family-stage-runner agent, whose brief fields are built here.
Pure: no subprocess, no git. No default family exists on any agent or tier.
"""
from __future__ import annotations

from typing import Optional, Sequence

FAMILIES = ("claude", "codex", "grok", "agy")
STAGES = ("size", "plan", "review", "write", "other")
RUNNER_AGENT = "coordinator:family-stage-runner"


def resolve(value: Optional[str]) -> str:
    """Return the family for a stage's `family` value; None or empty is claude.

    Raises ValueError naming the closed set for any other value.
    """
    if value is None or value == "":
        return "claude"
    if not isinstance(value, str) or value not in FAMILIES:
        raise ValueError(
            f"unknown stage family {value!r}; expected one of {'|'.join(FAMILIES)}"
        )
    return value


def runner_agent(family: Optional[str]) -> Optional[str]:
    """Return the agent type a stage dispatches to, or None when it stays claude."""
    return None if resolve(family) == "claude" else RUNNER_AGENT


def runner_brief(
    family: str,
    stage: str,
    prompt_path: str,
    scratch_dir: str,
    writes: Optional[Sequence[str]] = None,
) -> str:
    """Return the stage brief naming the fields family-stage-runner reads.

    `stage` is one of size|plan|review|write|other. `scratch_dir` must sit under
    `scratch/`. A write stage must declare at least one write path.
    """
    fam = resolve(family)
    if stage not in STAGES:
        raise ValueError(
            f"unknown stage {stage!r}; expected one of {'|'.join(STAGES)}"
        )
    scratch = scratch_dir.replace("\\", "/").strip()
    if not (scratch == "scratch" or scratch.startswith("scratch/")):
        raise ValueError(f"scratch_dir {scratch_dir!r} must be under scratch/")
    paths = [p for p in (writes or ()) if p]
    if stage == "write" and not paths:
        raise ValueError("a write stage must declare its write paths")
    lines = [
        f"family: {fam}",
        f"stage: {stage}",
        f"prompt: {prompt_path}",
        f"scratch: {scratch}",
    ]
    if stage == "write":
        lines.append("writes:")
        lines.extend(f"  - {p}" for p in paths)
    return "\n".join(lines) + "\n"
