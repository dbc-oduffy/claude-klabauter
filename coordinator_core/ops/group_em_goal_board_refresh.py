"""
coordinator_core.ops.group_em_goal_board_refresh -- JSON-RPC "groupem.goal_board_refresh".

Purpose: the sole writer of the G-EM fleet goal board, the board file under the group-em home.
Validates the board against the vendored schema, recomputes its drift flags and rewrites it
atomically, via `group_em.goal_board.refresh`.

Registered in `coordinator_core/ops/__init__.py`'s registration list,
`_registry_map.py`'s `OP_MODULE_MAP`, `op_scopes.py` (scope "none") and
`authz/classification.py` (MUTATING). Enrolled in `_BUDGETED_ENTRYPOINTS` of the spawn audit
with an empty reachable spawn set.

Negative-spec:
    - Never raises: a refusal is `{"ok": False, "errors": [...]}`.
    - Never writes when the board is absent, not JSON or schema-invalid.
    - Takes no params; the board path is always the one resolved by `goal_board`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from coordinator_core.group_em import goal_board
from coordinator_core.ipc import register_op


@register_op("groupem.goal_board_refresh")
def _groupem_goal_board_refresh(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC "groupem.goal_board_refresh" handler.

    Params: none.

    Returns:
        {"ok": bool, "path": str | None, "flags": [{"flag", "id"}], "errors": [{"field", "error", "hint"}]}
        `flags` names `stale`, `no-live-owner` and `unmapped-task` rows; `ok: False` leaves the file untouched.
    """
    try:
        return goal_board.refresh()
    except Exception as exc:  # noqa: BLE001 -- degrade-never-raise
        return {
            "ok": False,
            "path": None,
            "flags": [],
            "errors": [{"field": "board", "error": f"{type(exc).__name__}: {exc}", "hint": "see the error"}],
        }
