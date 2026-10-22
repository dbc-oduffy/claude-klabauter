"""
coordinator_core.ops.group_em_admission_post -- JSON-RPC "groupem.admission_post".

Purpose: a run agent posts a START or END admission line, with its live PIDs, to the box Group
EM. Appends one JSONL line to `<settings-home>/state/group-em/box/admission.jsonl` via
`group_em.admission_post`; the Group EM tails it with `workflow_watch/mailbox_line.py`.

Registered in `coordinator_core/ops/__init__.py`'s registration list,
`_registry_map.py`'s `OP_MODULE_MAP`, `op_scopes.py` (scope "none") and
`authz/classification.py` (MUTATING). Enrolled in `_BUDGETED_ENTRYPOINTS` of the spawn audit
with an empty reachable spawn set.

Negative-spec:
    - Never raises: an invalid param or I/O failure is `{"error": ...}`.
    - Spawns nothing; with no box Group EM holder it writes nothing.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from coordinator_core.group_em import admission_post
from coordinator_core.ipc import register_op


@register_op("groupem.admission_post")
def _groupem_admission_post(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC "groupem.admission_post" handler.

    Params:
        phase (str) -- "START" or "END" (case-insensitive).
        session_id (str) -- the posting run agent's session id.
        pids (list[int], optional) -- PIDs of the heavy processes; only live ones are reported live.
        run (str, optional) -- run or task label.
        note (str, optional) -- free text.
        declared_peak_mb (number, optional) -- expected peak tree RSS in MB; START lines only.

    Returns:
        {"posted": True, "path", "phase", "pids", "dead_pids", "holder_live"}
        {"posted": False, "reason": "bad_phase" | "no_box_holder"}
        or {"error": str} on an invalid param.
    """
    p = params if isinstance(params, dict) else {}
    sid = p.get("session_id")
    if not isinstance(sid, str) or not sid:
        return {"error": "session_id (non-empty str) is required"}
    pids = p.get("pids", [])
    if not isinstance(pids, (list, tuple)):
        return {"error": "pids must be a list of ints"}
    run = p.get("run", "")
    note = p.get("note", "")
    if not isinstance(run, str) or not isinstance(note, str):
        return {"error": "run and note must be str"}
    peak = p.get("declared_peak_mb")
    if peak is not None and admission_post.valid_peak_mb(peak) is None:
        return {"error": "declared_peak_mb must be a number in [0, MAX_PEAK_MB]"}
    try:
        return admission_post.post(
            str(p.get("phase", "")), sid, pids, run=run, note=note, declared_peak_mb=peak
        )
    except Exception as exc:  # noqa: BLE001 -- degrade-never-raise
        return {"error": f"{type(exc).__name__}: {exc}"}
