"""plan_chain plan stage: bind the plan Workflow script, read the plan digest.

The plan digest is read by field and never schema-validated: DoE's plan digest emits a
``next_action`` that its own schema pins to null.
"""
from __future__ import annotations

from coordinator_core.session.declared_writes import declare_write
import asyncio
import re
from pathlib import Path

from coordinator_core.ops.plan_chain.contract import ChainManifest, Halt, WorkflowResult, halt

_SAFE_ID = re.compile(r"[^A-Za-z0-9._-]")


def bind_plan_script(
    manifest: ChainManifest, *, child_session_id: str, chain_id: str | None = None
) -> Path:
    """Bind ``manifest.script_source`` with ``manifest.wave_args`` and write the plan script.

    Output is ``<trail>/chain-<chain_id>.plan.mjs`` with its emission receipt naming
    ``child_session_id``. A receipt that cannot be written raises and removes the script: a
    receipt-less script, or one naming a session that never ran, is false provenance.
    """
    if not child_session_id:
        raise ValueError("bind_plan_script requires a non-empty child_session_id")
    from coordinator_core.ipc import dispatch_message
    from coordinator_core.ops.dispatch_emit.op import _write_emission_receipt

    repo_root = Path(manifest.repo_root)
    msg = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "workflow.bind_args",
        "params": {"script_path": manifest.script_source, "args": manifest.wave_args},
        "_origin_worktree": str(repo_root),
    }
    reply = asyncio.run(dispatch_message(msg, caller="plan-chain-run"))
    if "error" in reply:
        err = reply["error"]
        raise ValueError(f"workflow.bind_args refused: {(err or {}).get('message') or err}")
    script = (reply.get("result") or {}).get("script")
    if not isinstance(script, str) or not script:
        raise ValueError("workflow.bind_args returned no script text")

    trail = Path(manifest.trail_dir)
    if not trail.is_absolute():
        trail = repo_root / trail
    trail.mkdir(parents=True, exist_ok=True)
    cid = _SAFE_ID.sub("_", chain_id or child_session_id)
    out = trail / f"chain-{cid}.plan.mjs"
    out.write_text(script, encoding="utf-8", newline="\n")
    declare_write(out)
    receipt = _write_emission_receipt(out, None, {"session_id": child_session_id})
    if receipt is None:
        out.unlink(missing_ok=True)
        raise OSError(f"could not write the emission receipt for {out}")
    return out


def plan_digest(result: WorkflowResult) -> dict | None:
    """The ``kind: plan`` digest: plan-blitz returns its wave result with the digest under ``digest``."""
    top = result.digest
    if not isinstance(top, dict):
        return None
    if top.get("kind") == "plan":
        return top
    nested = top.get("digest")
    return nested if isinstance(nested, dict) and nested.get("kind") == "plan" else None


def _pull_reasons(result: WorkflowResult) -> str:
    """The wave's own ``pulled`` reasons (a prep gate NOT-PREPPED included), or ""."""
    top = result.digest
    pulled = top.get("pulled") if isinstance(top, dict) else None
    reasons = [
        f"{p.get('batonId') or p.get('id') or '?'}: {p.get('reason') or '(no reason)'}"
        + (f" (gate report: {p['prepGateReport']})" if p.get("prepGateReport") else "")
        for p in pulled or ()
        if isinstance(p, dict)
    ]
    return f"; pulled — {'; '.join(reasons)}" if reasons else ""


def read_plan_result(result: WorkflowResult, *, repo_root: str | Path) -> str | Halt:
    """The ready plan's path (as the digest names it), or the Halt that ends the chain."""
    digest = plan_digest(result)
    if digest is None:
        return halt(
            "plan-no-digest", f"plan Workflow returned no kind:plan digest{_pull_reasons(result)}"
        )
    outcome = digest.get("outcome")
    if outcome != "ready":
        return halt(
            "ready-gate-not-ready",
            f"plan outcome is {outcome!r}, not 'ready'{_pull_reasons(result)}",
        )
    action = digest.get("next_action")
    params = action.get("params") if isinstance(action, dict) else None
    plan_path = params.get("plan_path") if isinstance(params, dict) else None
    if not isinstance(plan_path, str) or not plan_path:
        return halt("plan-no-digest", "ready plan digest carries no next_action.params.plan_path")
    candidate = Path(plan_path)
    if not candidate.is_absolute():
        candidate = Path(repo_root) / candidate
    if not candidate.is_file():
        return halt("plan-no-digest", f"ready plan file does not exist: {plan_path}")
    return plan_path
