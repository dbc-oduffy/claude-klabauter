"""Probe: which objective-reacher legs a composed ask script carries, and the guard in front of each.

The judge is the `judge` return key of the one verify agent (ASK_VERIFY_LABEL), not a phase of its
own; the overengineering reviewer is a labelled agent call in the review-integration stage; the
remnant leg is `_remnantComplete`. `compose_cell` composes one arm against a live-shaped roster.
"""

from __future__ import annotations

import json
import re

from coordinator_core.ops.dispatch_emit import ask_compose
from coordinator_core.ops.dispatch_emit.ask_compose import compose_ask_script

KIRA_LABEL = "review:coordinator:overengineering-reviewer"
ARMS = ("xs", "s", "m_plus")
SIZES = ("L", "XL", "XXL")

_OBJ = {"type": "object"}
LIVE_SHAPED_SCHEMAS = {
    n: _OBJ
    for n in (
        "review-prep-result",
        "slice-review-result",
        "whole-diff-review-result",
        "terminal-judge-result",
    )
}


def live_shaped_fragment(*, judge: bool = True, kira: bool = True, min_tshirt: str | None = "L") -> dict:
    """The DoE roster's stage shape: prep, a per-slice wave, Kira in the integration stage, the judge."""
    stages: list[dict] = [
        {"kind": "prep", "agents": [{"agentType": "coordinator:test-runner", "model": "haiku",
                                     "effort": "low", "schema": "review-prep-result"}]},
        {"kind": "review-wave", "agents": [{
            "agentType": "coordinator:code-reviewer", "model": "sonnet", "effort": "low", "per": "slice",
            "executor_rows_per_reviewer": {"min": 3, "max": 6}, "applies": "own-slice",
            "schema": "slice-review-result"}]},
    ]
    if kira:
        agent = {"agentType": "coordinator:overengineering-reviewer", "model": "opus", "effort": "low",
                 "per": "whole-diff", "applies": "whole-diff", "schema": "whole-diff-review-result"}
        if min_tshirt is not None:
            agent["min_tshirt"] = min_tshirt
        stages.append({"kind": "integration", "agents": [agent]})
    if judge:
        stages.append({"kind": "judge", "agents": [{
            "agentType": "coordinator:exit-criterion-judge", "model": "sonnet", "effort": "low",
            "schema": "terminal-judge-result"}]})
    return {"schema": "review-roster-fragment", "schema_version": 6, "execute_review": {"stages": stages}}


_BLITZ_FN = "  async function planBlitz(args) {\n    return { ready: [] };\n  }"


def compose_cell(monkeypatch, arm: str, *, fragment: dict | None = None, **extra) -> str:
    """One ask script for `arm` over `fragment` (default: the live-shaped roster)."""
    monkeypatch.setattr(ask_compose, "_known_arm", lambda *_: arm)
    monkeypatch.setattr(ask_compose, "head_sha", lambda _root: "h" * 40)
    return compose_ask_script(
        repo_root="REPO",
        prompt=None,
        sizing_rel="state/sizings/x.yaml",
        run_id="run-1",
        session_id=None,
        wrap_stage=lambda _t: (_BLITZ_FN, ["Size", "Plan"]),
        plan_blitz_text="stub",
        script_path="x.mjs",
        in_run_commit=True,
        review_roster_fragment=fragment if fragment is not None else live_shaped_fragment(),
        review_stage_schemas=LIVE_SHAPED_SCHEMAS,
        **extra,
    )


_VERIFY_GUARD = re.compile(r"^(?P<g>\s*if \([^\n]*\) \{)\n\s*phase\('Verify'\);", re.M)
_KIRA_SIZE_GATE = re.compile(r"_integrationNotRun = !\((\[[^\]]*\])\.includes\(([^)]*)\)\);")
_INTEGRATION_SKIP = re.compile(r"^\s*(if \([^\n]*\) \{ _integrationNotRun = true; \})", re.M)
_REMNANT_GUARD = re.compile(r"const _remnantComplete = async \(\) => \{\n\s*(if \([^\n]*\) return;)")
_REMNANT_CALL = re.compile(r"^\s*(if \([^\n]*\) await _remnantComplete\(\);)", re.M)


def reacher_legs(script: str) -> dict:
    """Presence of the judge, Kira and remnant legs in `script`, with the guard expression before each.

    `judge`: the verify agent's prompt asks for a `judge` key and the script reads `_verify.judge`.
    `kira`: the overengineering reviewer is called; `kira_admits` is the sizes its fire-time gate
    admits (None when ungated) and `kira_skips` every statement that sets `_integrationNotRun`.
    `remnant`: `_remnantComplete` is defined and called.
    """
    guard = _VERIFY_GUARD.search(script)
    size_gate = _KIRA_SIZE_GATE.search(script)
    remnant_guard = _REMNANT_GUARD.search(script)
    remnant_call = _REMNANT_CALL.search(script)
    return {
        "verify_agents": script.count("label: 'verify'"),
        "judge": "_verify.judge" in script and "`judge` (the criterion verdict fields" in script,
        "judge_gate": guard.group("g").strip() if guard else None,
        "kira": f"label: '{KIRA_LABEL}'" in script,
        "kira_admits": json.loads(size_gate.group(1)) if size_gate else None,
        "kira_size_expr": size_gate.group(2) if size_gate else None,
        "kira_skips": [m.group(1) for m in _INTEGRATION_SKIP.finditer(script)],
        "remnant": "const _remnantComplete = async" in script,
        "remnant_gate": remnant_guard.group(1) if remnant_guard else None,
        "remnant_call_gate": remnant_call.group(1) if remnant_call else None,
    }
