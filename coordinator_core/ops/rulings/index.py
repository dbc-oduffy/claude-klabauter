"""
coordinator_core.ops.rulings.index — JSON-RPC "rulings.index".

Purpose: the durable register of accepted PM rulings (``state/pm-rulings/<id>.yaml``).
``list`` reads it; ``append`` adds one ruling. A run asks the register what the PM has
already ruled before it calls anything "pending a PM ruling".

Wire params:
    verb    (str, required) — "list" or "append".
    status  (str, optional, list only) — "accepted" or "superseded"; absent lists both.
    ruling  (object, required for append) — ``{id, pm_turn, scope, text, status?,
            superseded_by?, enforcement?, recorded?}``. ``pm_turn`` cites the PM's words
            (sizing path + register row id, or run_id + anchor); ``text`` is the PM verbatim.

Reply:
    list:   ``{"rulings": [{id, pm_turn, scope, text, status, superseded_by, enforcement,
            recorded}], "unreadable": [{id, reason}]}`` sorted by id. ``unreadable`` names
            each register file that could not be parsed.
    append: ``{"applied": [id], "refused": [{id, reason}]}``.

Negative-spec:
  - Does NOT raise on a bad ruling: a malformed or path-unsafe id, a duplicate id, an empty
    pm_turn or text, an unknown status and a superseded_by naming an absent id are each
    refused by name in the reply.
  - Does NOT overwrite or edit an existing ruling; supersede by appending a new one.
  - Does NOT commit, and does NOT spawn; it declares the one file it wrote.
  - Does NOT judge a ruling's truth: the register holds what the caller cites.
"""

from __future__ import annotations

MUTATES = ["state/pm-rulings/*.yaml"]  # append writes one new file per accepted ruling
GENERATES: list = []

from pathlib import Path
from typing import Any, Optional

from coordinator_core.ipc import register_op
from coordinator_core.lifecycle import main_worktree_root
from coordinator_core.rulings import store
from coordinator_core.rulings.contract import STATUSES, Ruling
from coordinator_core.session.declared_writes import declare_write

_VERBS = ("list", "append")

_SCHEMA = {
    "type": "object",
    "properties": {
        "verb": {"type": "string", "enum": list(_VERBS), "description": "list reads the register; append adds one ruling."},
        "status": {"type": "string", "enum": list(STATUSES), "description": "list only: filter by status; absent lists both."},
        "ruling": {
            "type": "object",
            "description": "append only: {id, pm_turn, scope, text, status?, superseded_by?, enforcement?, recorded?}.",
        },
    },
    "required": ["verb"],
    "additionalProperties": False,
}


@register_op("rulings.index", params_schema=_SCHEMA)
def _handler(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC "rulings.index" handler. See module docstring."""
    if repo_root is None:
        raise ValueError("rulings.index requires a resolved repo_root")
    verb = params.get("verb")
    if verb not in _VERBS:
        raise ValueError(f"verb must be one of {list(_VERBS)}")
    root = Path(main_worktree_root(repo_root))
    if verb == "list":
        return _list(root, params.get("status"))
    return _append(root, params.get("ruling"))


def _list(root: Path, status: Any) -> dict:
    if status is not None and status not in STATUSES:
        raise ValueError(f"status must be one of {list(STATUSES)}")
    rulings, problems = store.scan(root)
    return {
        "rulings": [r.to_dict() for r in rulings if status is None or r.status == status],
        "unreadable": problems,
    }


def _append(root: Path, raw: Any) -> dict:
    rid = str(raw.get("id", "")) if isinstance(raw, dict) else ""
    try:
        ruling = Ruling.from_dict(raw)
    except ValueError as exc:
        return {"applied": [], "refused": [{"id": rid, "reason": str(exc)}]}
    reply = store.append(root, ruling)
    for applied_id in reply["applied"]:
        declare_write(store.register_dir(root) / f"{applied_id}.yaml")
    return reply
