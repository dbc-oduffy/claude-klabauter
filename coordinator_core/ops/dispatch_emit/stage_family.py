"""Per-stage model-family resolver for emitted workflow stages.

A stage names a family or names none; none means claude. A non-claude family
dispatches to the family-stage-runner agent, whose brief fields are built here.
Pure: no subprocess, no git. No default family exists on any agent or tier.
"""
from __future__ import annotations

from typing import Optional, Sequence

FAMILIES = ("claude", "codex", "grok", "agy")
STAGES = ("size", "plan", "review", "write", "scout", "research", "other")
ROLES = ("scout", "research", "review")
FALLBACK = "claude"
FALLBACK_VERDICTS = ("BLOCKED", "FAILED")
RUNNER_AGENT = "coordinator:family-stage-runner"
_RESULT_KEYS = ("family", "stage", "exit_code", "output_path", "verdict")


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

    `stage` is one of `STAGES`. `scratch_dir` must sit under
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


def _checked(label: str, result: dict) -> dict:
    if not isinstance(result, dict):
        raise ValueError(f"{label} result must be a dict")
    missing = [k for k in _RESULT_KEYS if k not in result]
    if missing:
        raise ValueError(f"{label} result lacks required keys: {', '.join(missing)}")
    return result


def stage_record(
    role: str,
    family_result: dict,
    fallback_result: Optional[dict] = None,
) -> dict:
    """Return the stage record keeping the family attempt and any claude fallback.

    Each result carries the family-stage-result required keys. A BLOCKED or
    FAILED family result must carry `blocked_reason`. The fallback result is
    appended as its own entry tagged `fallback_for: <family>`; `reported_family`
    is then claude, never the family that failed. A fallback beside an OK or
    WARN family result is refused.
    """
    if role not in ROLES:
        raise ValueError(f"unknown stage role {role!r}; expected one of {'|'.join(ROLES)}")
    fam = _checked("family", family_result)
    failed = fam["verdict"] in FALLBACK_VERDICTS
    if failed and not fam.get("blocked_reason"):
        raise ValueError(
            f"a {fam['verdict']} family result must carry blocked_reason"
        )
    if fallback_result is None:
        return {
            "stage_role": role,
            "attempts": [dict(fam)],
            "reported_family": fam["family"],
        }
    if not failed:
        raise ValueError(
            f"fallback result given beside a {fam['verdict']} family result; "
            f"fallback runs only on {'|'.join(FALLBACK_VERDICTS)}"
        )
    fb = _checked("fallback", fallback_result)
    if fb["family"] != FALLBACK:
        raise ValueError(f"fallback result family must be {FALLBACK}, got {fb['family']!r}")
    return {
        "stage_role": role,
        "attempts": [dict(fam), {**fb, "fallback_for": fam["family"]}],
        "reported_family": FALLBACK,
    }
