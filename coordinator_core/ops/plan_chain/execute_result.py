"""plan_chain execute stage: classify the execute Workflow's wake digest and the commit reply.

Field names are those of ``contract/wake-digest.schema.json``. That schema has no
``halted_by``: a halt is ``outcome: halted`` plus the free-text ``halted`` string.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from coordinator_core.ops.plan_chain.contract import Halt, WorkflowResult, halt

# ask_compose.HALT_USAGE_LIMIT: the execute script's _haltOnUsageLimit writes this token.
USAGE_LIMIT_MARKER = "usage_limit"
_TERMINAL_COMMIT_OP = "dispatch.terminal_commit"


def read_execute_result(result: WorkflowResult) -> dict[str, Any] | Halt:
    """``next_action.params`` for ``dispatch.terminal_commit``, unmodified, or the Halt."""
    d = result.digest
    if not isinstance(d, dict):
        return halt("child-no-digest", "execute Workflow returned no digest", running_stage="execute")

    if d.get("outcome") == "halted":
        text = str(d.get("halted") or "")
        if USAGE_LIMIT_MARKER in text:
            return halt("usage-limit", text or USAGE_LIMIT_MARKER, running_stage="execute")
        return halt("executor-block", f"execute halted: {text}" if text else "execute halted")

    for dev in d.get("deviations") or []:
        if isinstance(dev, dict) and dev.get("kind") == "blocked":
            why = str(dev.get("anchor") or "").strip() or "the executor gave no reason; read its report in the task output"
            return halt("executor-block", f"chunk {dev.get('chunk')} blocked: {why}")

    verdict = ((d.get("review") or {}).get("delivery") or {}).get("verdict")
    if verdict in ("FAIL", "unstructured"):
        return halt("review-fail", f"review delivery verdict {verdict}")

    status = (d.get("criterion") or {}).get("status")
    if status == "not_met":
        observation = (d.get("criterion") or {}).get("observation") or ""
        return halt("falsifier-not-met", f"terminal judge: criterion not_met. {observation}")

    action = d.get("next_action")
    if isinstance(action, dict) and action.get("op") == _TERMINAL_COMMIT_OP:
        params = action.get("params")
        if isinstance(params, dict):
            return params
    return halt(
        "child-no-digest",
        "execute digest carries no dispatch.terminal_commit next_action",
        running_stage="execute",
    )


def resume_params(result: WorkflowResult, script_path: Path) -> dict[str, Any]:
    """What the EM passes to ``dispatch.terminal_commit`` after an execute-stage halt.

    The digest's own terminal_commit params when it carried them, always with ``script_path``
    and ``task_output_path`` set, so no one hunts the child's task file by session id.
    """
    action = (result.digest or {}).get("next_action")
    params = action.get("params") if isinstance(action, dict) and action.get("op") == _TERMINAL_COMMIT_OP else None
    out = dict(params) if isinstance(params, dict) else {}
    out["script_path"] = str(script_path)
    out["task_output_path"] = result.task_output_path
    return out


def read_commit_reply(reply: Mapping[str, Any] | None) -> dict[str, str] | Halt:
    """``{sha, receipt_path}`` from a ``dispatch.terminal_commit`` reply, or the refusal Halt.

    The op's reply is ``{committed, sha, error, receipts: [...]}``; ``receipt_path`` is the
    first receipt, ``""`` when the commit wrote none.
    """
    if not isinstance(reply, Mapping):
        return halt("terminal-commit-refused", "terminal_commit returned no reply")
    err = reply.get("error")
    if err:
        text = err.get("message") if isinstance(err, Mapping) else err
        return halt("terminal-commit-refused", str(text))
    body = reply.get("result", reply)
    if not isinstance(body, Mapping):
        return halt("terminal-commit-refused", "terminal_commit reply carries no result")
    sha = body.get("sha")
    if not body.get("committed") or not sha:
        refusal = body.get("error") or body.get("refusal") or body.get("reason")
        return halt(
            "terminal-commit-refused",
            str(refusal) if refusal else "terminal_commit made no commit",
        )
    if body.get("review_stamp") == "refused":
        return halt(
            "terminal-commit-refused",
            f"committed {sha} but review_stamp refused: {body.get('review_stamp_refusal')}",
        )
    receipts = body.get("receipts")
    receipt = receipts[0] if isinstance(receipts, list) and receipts else ""
    return {"sha": str(sha), "receipt_path": str(receipt)}


def stages_seen(digest: Mapping[str, Any] | None) -> list[str]:
    """Stages the execute child ran: ``execute``, plus ``review`` when review ran."""
    if not isinstance(digest, Mapping):
        return []
    seen = ["execute"]
    if (digest.get("review") or {}).get("status") in ("integrated", "unstructured"):
        seen.append("review")
    return seen
