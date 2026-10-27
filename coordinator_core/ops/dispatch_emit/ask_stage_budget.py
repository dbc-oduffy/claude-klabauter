"""Per-stage byte attribution of the composed ask script, and the budgets that pin each stage.

`stage_bytes` splits a composed script's UTF-8 bytes into `planBlitz`, `roadmapBlitz`, `staged`
(prompt bodies read by path; 0 until they are staged) and `core` (the rest). A stage is measured
from its wrap output (`wrap_stage` / `wrap_roadmap_stage`, comment-stripped or not, whichever the
script carries), never by matching JS braces.

Ratchet rule: a measured stage over its `STAGE_BUDGETS` entry fails; each budget is set at
measured + RATCHET_SLACK so one prompt edit does not flake; a measured stage more than
2 * RATCHET_SLACK under its budget also fails, naming the number to write, so budgets only move down.
"""

from __future__ import annotations

from typing import Mapping, Optional

RATCHET_SLACK = 4096

STAGES = ("planBlitz", "roadmapBlitz", "staged", "core")

STAGE_BUDGETS: dict[str, dict[str, int]] = {
    "bare": {"planBlitz": 4096, "roadmapBlitz": 41060, "staged": 4096, "core": 222164},
    "m_plus": {"planBlitz": 194227, "roadmapBlitz": 41002, "staged": 4096, "core": 247082},
}


def _strip_variants(text: str) -> list[str]:
    from coordinator_core.ops.dispatch_emit import ask_compose, ask_plan_blitz

    strip = getattr(ask_plan_blitz, "strip_comment_lines", None) or ask_compose._strip_comment_lines
    return [text, strip(text)]


def stage_texts(plan_blitz_text: Optional[str] = None, roadmap_text: Optional[str] = None) -> dict[str, str]:
    """Wrap output per blitz stage; the plugin's real files when a text is not injected."""
    from coordinator_core.ops.dispatch_emit import ask_plan_blitz, ask_roadmap

    pb = plan_blitz_text if plan_blitz_text is not None else ask_plan_blitz.load_plan_blitz_text()
    rb = roadmap_text if roadmap_text is not None else ask_roadmap.load_roadmap_blitz_text()
    return {
        "planBlitz": ask_plan_blitz.wrap_stage(pb)[0],
        "roadmapBlitz": ask_roadmap.wrap_roadmap_stage(rb)[0],
    }


def _carried_bytes(script: str, name: str, wrapped: str) -> int:
    """Bytes of the `name` block in `script`: as many lines from its opening line as its wrap output has.

    The composer re-indents and hoists inside the block, so the wrap text is not a substring;
    its line count and closing `}` locate it. The comment-stripped or raw variant is chosen by
    how many lines still match the script's, whitespace aside.
    """
    start = script.find(f"async function {name}(")
    if start < 0:
        return 0
    lines = script[start:].split("\n")
    best_score, best_bytes = -1, 0
    for variant in _strip_variants(wrapped):
        want = variant.rstrip("\n").split("\n")
        n = len(want)
        if n > len(lines) or lines[n - 1] != "}":
            continue
        score = sum(1 for got, exp in zip(lines, want) if got.strip() == exp.strip())
        if score > best_score:
            best_score = score
            best_bytes = len("\n".join(lines[:n]).encode("utf-8")) + 1
    return best_bytes


def stage_bytes(script: str, wrapped: Optional[Mapping[str, str]] = None) -> dict[str, int]:
    """UTF-8 bytes of `script` per stage; the values sum to the script's size.

    `wrapped` maps `planBlitz` / `roadmapBlitz` to the wrap output the composer was given; when
    omitted, the plugin's real files are wrapped. A stage whose text the script does not carry (or cannot be aligned to) is 0.
    """
    texts = wrapped if wrapped is not None else stage_texts()
    out = {"planBlitz": 0, "roadmapBlitz": 0, "staged": 0}
    for name in ("planBlitz", "roadmapBlitz"):
        if f"async function {name}(" in script and name in texts:
            out[name] = _carried_bytes(script, name, texts[name])
    out["core"] = len(script.encode("utf-8")) - sum(out.values())
    return out


def budget_failures(arm: str, measured: Mapping[str, int]) -> list[str]:
    """One line per stage over budget, or more than 2 * RATCHET_SLACK under it."""
    failures = []
    for stage, budget in STAGE_BUDGETS[arm].items():
        n = measured.get(stage, 0)
        if n > budget:
            failures.append(f"{arm}.{stage} is {n} bytes; budget is {budget}")
        elif budget - n > 2 * RATCHET_SLACK:
            failures.append(
                f"{arm}.{stage} is {n} bytes, {budget - n} under its budget; lower the budget to {n + RATCHET_SLACK}"
            )
    return failures
