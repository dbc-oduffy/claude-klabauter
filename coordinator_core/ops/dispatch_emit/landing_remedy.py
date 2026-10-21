"""Remedy text, param withhold read and review-only anchor shared by the two landing ops.

Invariant: every line printed here is one the op honours as printed. Stdlib only; spawns no process;
callers import it lazily inside their refusal and landing branches.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Sequence

WITHHOLD_PARAM = "withhold_paths"
_UNQUOTABLE = "<>&'\r\n"
_PLACEHOLDER = "TASK_OUTPUT_PATH"
_GENERIC_DRIFT = "Re-review the current bytes and pass their blob, or leave the path out."
_DRIFT_REASON = "blob-drift: bytes changed after the run's review"
REVIEW_CARRIED_REASON = "review-only carried: uncommitted at emit, outside the committed range the review read"
_RECORDED_AT = re.compile(r"^recorded_at:\s*['\"]?([0-9T:.\-+Z]+)", re.MULTILINE)


def invoke_call(op: str, task_output_path: Optional[str], extra: dict) -> str:
    """``coordinator-invoke <op> --from-task-output '<path>' '<json extra>'``; ``extra`` keys are
    top-level params, which win over the task output's ``next_action.params``. A missing or
    unquotable path prints ``TASK_OUTPUT_PATH``."""
    ok = isinstance(task_output_path, str) and task_output_path and not any(c in task_output_path for c in _UNQUOTABLE)
    target = f"'{task_output_path}'" if ok else _PLACEHOLDER
    body = json.dumps(extra, ensure_ascii=False)
    return f"coordinator-invoke {op} --from-task-output {target} '{body}'"


def release_held_call(op: str, task_output_path: Optional[str], held_ids: Sequence[str]) -> str:
    """The invoke line that lands the named held chunks."""
    return invoke_call(op, task_output_path, {"release_held": sorted(held_ids)})


def revise_resolves(root: Path, run_id: str) -> bool:
    """True when ``workflow.revise`` can resolve ``run_id``: its record dir holds a ``run.json``."""
    from coordinator_core.ops.dispatch_emit import requirements_ledger as rl

    try:
        return rl.read_run(rl.run_dir(Path(root), run_id)) is not None
    except rl.LedgerRefused:
        return False


def blob_drift_remedy(
    error: str, root: Path, run_id: str, op: str, task_output_path: Optional[str],
    drift_paths: Sequence[str],
) -> str:
    """``error`` with commit_v2's generic trailing remedy replaced by a route that resolves: the
    ``workflow.revise`` refire when the run keeps a ledger, else the review-only re-emit or a
    ``withhold_paths`` invoke line."""
    if revise_resolves(root, run_id):
        from coordinator_core.ops.dispatch_emit.peer_claim_contract import WITHHOLD_CALL

        remedy = (
            f"Real drift: call workflow.revise run_id={run_id} with a note, then make the refire call "
            "its reply's next.call returns; the refire's review re-reads the current bytes. "
            f"Or leave the path out: {WITHHOLD_CALL.format(run_id=run_id)}"
        )
    else:
        withheld = [{"path": p, "reason": _DRIFT_REASON} for p in drift_paths]
        remedy = (
            "Real drift on a run with no ledger record. Order: (1) leave the drifted paths out with the "
            "withhold call below; (2) commit them with ceremony.commit_v2; (3) re-emit review-only over the "
            "committed range to review them "
            "(emit-dispatch-workflow --plan <plan> | --inventory <inv> --review-only --rows <DONE ids> "
            "--run-base <sha>); review-only reviews committed shas only. "
            f"Withhold call: {invoke_call(op, task_output_path, {WITHHOLD_PARAM: withheld})}"
        )
    return error.replace(_GENERIC_DRIFT, remedy) if _GENERIC_DRIFT in error else f"{error} {remedy}"


def param_withholds(params: dict) -> dict:
    """``{path: reason}`` from ``params["withhold_paths"]`` via ``run_withhold.validate``; ``{}``
    when absent; ``ValueError`` on a malformed list."""
    raw = params.get(WITHHOLD_PARAM) if isinstance(params, dict) else None
    if raw is None:
        return {}
    from coordinator_core.ops.dispatch_emit import run_withhold

    return {e["path"]: e["reason"] for e in run_withhold.validate(raw)}


def review_carried_withholds(request) -> dict:
    """``{path: REVIEW_CARRIED_REASON}`` for a review-only request's ``review_carried``; ``{}``
    for any other request."""
    if not getattr(request, "review_only", False):
        return {}
    return {p: REVIEW_CARRIED_REASON for p in getattr(request, "review_carried", ())}


def _epoch(stamp: str) -> Optional[float]:
    try:
        parsed = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return None
    return (parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)).timestamp()


def review_only_anchor(root: Path, request, inline_review: object) -> tuple:
    """``(epoch the run's review read the bytes, the review rows' declared write paths)`` for a
    review-only request, else ``(None, frozenset())``. The epoch is the prep sidecar's
    ``recorded_at``, else its mtime; a sidecar that does not resolve under ``root`` yields none."""
    none: tuple = (None, frozenset())
    if not getattr(request, "review_only", False) or not isinstance(inline_review, dict):
        return none
    rel = inline_review.get("prep_sidecar")
    if not isinstance(rel, str) or not rel:
        return none
    root = Path(root)
    target = root / rel
    try:
        if root.resolve() not in target.resolve().parents:
            return none
        at = target.stat().st_mtime
        head = target.read_text(encoding="utf-8")[:4096]
    except (OSError, ValueError):
        return none
    found = _RECORDED_AT.search(head)
    stamped = _epoch(found.group(1)) if found else None
    paths = frozenset(p for c in request.chunks for p in c.paths)
    return (stamped if stamped is not None else at), paths
