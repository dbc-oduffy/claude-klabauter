"""Currency of an existing plan a refired ask would resume: one in-process prep-gate pass, no subprocess."""
from __future__ import annotations

from pathlib import Path
from typing import Any

#: The prep-gate verdict that means the plan needs no upgrade before its open rows stage.
_DECENT_VERDICT = "PREPPED"


def assess(repo_root: str | Path, plan_rel: str) -> dict[str, Any]:
    """`{decent, findings}` for the plan at `plan_rel`.

    `decent` is True only when `plan.prep_gate` reads `PREPPED`; `findings` are the gate's failing classes
    and message (empty when decent). A plan the gate cannot read is not decent, with the cause as the
    finding: the caller upgrades it in-run and never halts on it.
    """
    from coordinator_core.ops.plan_prep_gate import prep_gate_with_text

    try:
        reply, _text = prep_gate_with_text(plan_rel, Path(repo_root))
    except (OSError, ValueError) as exc:
        return {"decent": False, "findings": [f"plan unreadable: {exc}"]}
    if reply.get("verdict") == _DECENT_VERDICT:
        return {"decent": True, "findings": []}
    classes = reply.get("classes") or {}
    findings = [
        f"{name}: {v.get('detail') or v.get('kind') or v.get('status')}"
        for name, v in classes.items()
        if isinstance(v, dict) and v.get("status") != "OK"
    ]
    message = str(reply.get("message") or "").strip()
    if message:
        findings.append(message)
    return {"decent": False, "findings": findings or [f"prep gate verdict {reply.get('verdict')!r}"]}
