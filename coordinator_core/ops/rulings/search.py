"""
coordinator_core.ops.rulings.search — JSON-RPC "rulings.search".

Purpose: answers "has the PM already ruled on this?". Call it before writing any
deferral to the PM; a match is a settled question, not a pending one.

Wire params:
    scope   (list[str], optional) — scope tokens (repo, subsystem, op, path globs). A ruling
            matches when one of its scope tokens equals a given token (case-insensitive) or
            one glob-matches the other.
    query   (str, optional) — keywords; every whitespace-separated word must appear
            (case-insensitive) in the ruling text.
    status  (str, optional) — "accepted" (default) or "superseded".

Reply: ``{"matches": [{id, text, pm_turn, scope}]}`` sorted by id. With both ``scope`` and
``query`` a ruling must satisfy both; with neither, every ruling of the status matches.

Negative-spec:
  - Does NOT write, and does NOT spawn; one scan of the register files.
  - Does NOT rank or embed: keyword plus scope match only, so a paraphrase that shares no
    word with the ruling text and no scope token is a miss; widen ``scope`` before concluding
    the PM has not ruled.
  - Does NOT return a superseded ruling unless ``status`` asks for it.
"""

from __future__ import annotations

GENERATES: list = []

from pathlib import Path
from typing import Any, Optional

from coordinator_core.ipc import register_op
from coordinator_core.lifecycle import main_worktree_root
from coordinator_core.rulings import store
from coordinator_core.rulings.contract import STATUS_ACCEPTED, STATUSES, scope_token_match


_SCHEMA = {
    "type": "object",
    "properties": {
        "scope": {"anyOf": [{"type": "array", "items": {"type": "string"}}, {"type": "string"}], "description": "Scope tokens (repo, subsystem, op, path globs)."},
        "query": {"type": "string", "description": "Keywords; every word must appear in the ruling text."},
        "status": {"type": "string", "enum": list(STATUSES), "description": "accepted (default) or superseded."},
    },
    "additionalProperties": False,
}


def _tokens(scope: Any) -> list[str]:
    if scope is None:
        return []
    if isinstance(scope, str):
        scope = [scope]
    if not isinstance(scope, list) or not all(isinstance(s, str) for s in scope):
        raise ValueError("scope must be a list of strings")
    return [s.strip().lower() for s in scope if s.strip()]


@register_op("rulings.search", params_schema=_SCHEMA)
def _handler(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC "rulings.search" handler. See module docstring."""
    if repo_root is None:
        raise ValueError("rulings.search requires a resolved repo_root")
    tokens = _tokens(params.get("scope"))
    query = params.get("query")
    if query is not None and not isinstance(query, str):
        raise ValueError("query must be a string")
    words = (query or "").lower().split()
    status = params.get("status", STATUS_ACCEPTED)
    if status not in STATUSES:
        raise ValueError(f"status must be one of {list(STATUSES)}")

    matches = []
    for r in store.load(Path(main_worktree_root(repo_root))):
        if r.status != status:
            continue
        if tokens and not scope_token_match(r.scope, tokens):
            continue
        text = r.text.lower()
        if words and not all(w in text for w in words):
            continue
        matches.append(
            {"id": r.id, "text": r.text, "pm_turn": r.pm_turn, "scope": list(r.scope)}
        )
    return {"matches": matches}
