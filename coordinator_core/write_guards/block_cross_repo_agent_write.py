"""coordinator_core.write_guards.block_cross_repo_agent_write -- hard-deny
guard: a dispatched agent's Write/Edit into a registered fleet repo other than
its own run repo is refused unless the run's cross-repo grant ledger names the
path.

The emit-time check (``cross_repo_write_refusal.check_cross_repo_writes``)
reads declared ``writes:``; it never sees an agent's actual tool calls. This
guard is the call-time half: the run repo is the repo of the payload ``cwd``,
the target repo is the nearest ``.git`` ancestor of the target path, and the
grant is ``cross_repo_write_refusal.grant_file`` -- the ledger the same check
writes when sibling ``writes:`` pass approval.

Allow: no ``agent_id`` (EM inline write); either repo unresolvable; target in
the run repo; target repo not in the machine registry; target equal to or
under a ledger line; target repo holds a fresh run token minted by the payload's session
(``run_token.session_holds_run``).

The run-token leg assumes a subagent's hook payload carries its parent session's id, the id
``run_token.mint`` stamped (``docs/wiki/commit-path.md``: a subagent shares its EM's
``CLAUDE_PID`` and session id). It admits every subagent of session S into that repo while S holds
a run there, not only the run's own agents, because the payload carries no run identity; the
session-scoped grant ledger has the same breadth. An empty session id admits nothing.

Negative-spec: no subprocess (walks and file reads only); no override env var;
an agent type is never consulted -- every dispatched agent is bound.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, Optional

from coordinator_core.write_guards._case_fold_path import casefold_path
from coordinator_core.write_guards._repo_root import resolve_repo_root

CLASS = "hard-deny"
MATCHERS = ["Write", "Edit", "MultiEdit", "NotebookEdit"]
PRIORITY = 51

GENERATES = []

_PATH_KEYS = ("file_path", "notebook_path")

_DENY_REASON = (
    "BLOCKED {path}: outside this run's repo ({run_repo}).\n"
    "Use instead: declare the path in the row's `writes:` and re-emit with cross-repo approval."
)


def _target_path(payload: Dict[str, Any]) -> str:
    tool_input = payload.get("tool_input") or {}
    if not isinstance(tool_input, dict):
        return ""
    for key in _PATH_KEYS:
        val = tool_input.get(key)
        if isinstance(val, str) and val:
            return val
    return ""


def _norm(path: str) -> str:
    return casefold_path(os.path.normpath(path)).replace("\\", "/").rstrip("/")


def _owning_checkout(target: Path) -> Optional[Path]:
    for parent in (target, *target.parents):
        if (parent / ".git").exists():
            return parent
    return None


def _registered_roots() -> "set[str]":
    from coordinator_core.machine_resolver import merged_flat_registry

    return {
        casefold_path(os.path.realpath(v)).rstrip("/")
        for k, v in merged_flat_registry().items()
        if k.startswith("repos.") and isinstance(v, str) and v
    }


def _granted(run_root: str, session_id: str, candidate: str) -> bool:
    from coordinator_core.ops.dispatch_emit.cross_repo_write_refusal import grant_file

    try:
        lines = grant_file(Path(run_root), session_id).read_text(encoding="utf-8").splitlines()
    except OSError:
        return False
    for line in lines:
        granted = _norm(line.strip())
        if granted and (candidate == granted or candidate.startswith(granted + "/")):
            return True
    return False


def check(payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if (payload.get("tool_name") or "") not in MATCHERS or not payload.get("agent_id"):
        return None
    file_path = _target_path(payload)
    cwd = payload.get("cwd")
    if not file_path or not cwd:
        return None
    run_root = resolve_repo_root(cwd)
    if not run_root:
        return None

    target = Path(file_path) if Path(file_path).is_absolute() else Path(cwd, file_path)
    target = Path(os.path.normpath(target))
    owner = _owning_checkout(target)
    if owner is None or _norm(str(owner)) == _norm(run_root):
        return None
    owner_real = casefold_path(os.path.realpath(owner)).rstrip("/")
    if owner_real not in _registered_roots():
        return None
    session_id = payload.get("session_id") or ""
    from coordinator_core.ops.dispatch_emit.run_token import session_holds_run

    if session_holds_run(owner, session_id):
        return None
    if _granted(run_root, session_id, _norm(str(target))):
        return None

    safe = "".join(ch if ch >= " " else " " for ch in file_path)
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": _DENY_REASON.format(
                path=safe, run_repo=Path(run_root).name
            ),
        }
    }
