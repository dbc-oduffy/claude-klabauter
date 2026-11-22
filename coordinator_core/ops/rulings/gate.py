"""JSON-RPC "rulings.gate": refuse a plan or DR body that defers or contradicts an accepted PM ruling.

Params: exactly one of `body` (text) or `path` (repo-relative plan or DR inside the worktree).
Reply: GateReply from coordinator_core.rulings.contract; `route` is "coordinator:apm" on refuse.

Negative-spec:
  - No override key; a newer PM turn clears one ruling by a `pm_turn:` line (see rulings.detect).
  - No spawn, no git, no write: one register scan and at most one file read.
  - Not a deny guard: a refusal is this op's reply, acted on by the calling stage.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from coordinator_core.ipc import CallerFacingValidationError, register_op
from coordinator_core.lifecycle import main_worktree_root
from coordinator_core.ops._path_guard import contained_path
from coordinator_core.rulings import store
from coordinator_core.rulings.detect import detect

_SCHEMA = {
    "type": "object",
    "properties": {
        "body": {"type": "string", "description": "Plan or DR text to gate."},
        "path": {"type": "string", "description": "Repo-relative plan or DR file to gate."},
    },
    "additionalProperties": False,
}


def gate_text(text: str, worktree_root: Path) -> dict:
    """Gate `text` against the register under `worktree_root`; usable in-process (prep_gate)."""
    return detect(text, store.load(worktree_root), worktree_root)


@register_op("rulings.gate", params_schema=_SCHEMA)
def _handler(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC "rulings.gate". See module docstring."""
    if repo_root is None:
        raise CallerFacingValidationError("rulings.gate: no repo root")
    body = params.get("body")
    path = params.get("path")
    if (body is None) == (path is None):
        raise CallerFacingValidationError("rulings.gate: pass exactly one of body or path")
    root = Path(main_worktree_root(repo_root))
    if body is None:
        if not isinstance(path, str) or not path.strip():
            raise CallerFacingValidationError("rulings.gate: path must be a non-empty string")
        candidate = Path(path.strip())
        if not candidate.is_absolute():
            candidate = root / candidate
        if contained_path(candidate, [root]) is None or not candidate.is_file():
            raise CallerFacingValidationError(f"rulings.gate: no such file in the worktree: {path!r}")
        body = candidate.read_text(encoding="utf-8", errors="replace")
    elif not isinstance(body, str):
        raise CallerFacingValidationError("rulings.gate: body must be a string")
    return gate_text(body, root)
