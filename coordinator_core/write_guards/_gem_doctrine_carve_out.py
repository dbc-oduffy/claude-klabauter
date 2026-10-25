"""coordinator_core.write_guards._gem_doctrine_carve_out -- the doctrine-surface
path test and the live Group EM admission shared by the write guards.

Pure and spawn-free: path tests are string work over realpaths (no walk), and
the standing read is the in-process nomination read SessionStart uses. Never
raises; every failure answers "not a doctrine surface" / "not admitted".
The derived copy ``<claude_config_dir>/CLAUDE.md`` is never a surface.
"""

from __future__ import annotations

import os
from typing import Optional

from coordinator_core._settings_home import claude_config_dir
from coordinator_core.bash_guards import _gem_cross_repo_commit, _write_bump_applicability
from coordinator_core.content_root import read_content_root
from coordinator_core.write_guards._case_fold_path import casefold_path

_DOE_SUBTREES = (
    ("global-doctrine", "global-doctrine"),
    ("coordinator/skills", "skills"),
    ("coordinator/commands", "commands"),
    ("coordinator/agents", "agents"),
    ("coordinator/hooks", "hooks"),
    ("coordinator/docs/wiki", "wiki"),
    ("coordinator/docs/decisions", "decisions"),
    ("docs/decisions", "decisions"),
)

_content_root_memo: "list[Optional[str]]" = []


def _content_root() -> Optional[str]:
    """The resolved content root, read once per process."""
    if not _content_root_memo:
        try:
            resolved = read_content_root() or None
        except Exception:
            resolved = None
        _content_root_memo.append(os.path.realpath(resolved) if resolved else None)
    return _content_root_memo[0]


def _under(folded_path: str, root: str, *, resolve: bool = True) -> bool:
    """``resolve=False`` for a ``root`` built on an already-realpathed base."""
    folded_root = casefold_path(os.path.realpath(root) if resolve else root).rstrip("/")
    return folded_path.startswith(folded_root + "/")


def _classify(path: str) -> "tuple[Optional[str], Optional[str]]":
    """(label, DoE content root or None) for ``path``."""
    if not path or not isinstance(path, str):
        return None, None
    folded = casefold_path(os.path.realpath(path))
    root = _content_root()
    if root:
        for rel, label in _DOE_SUBTREES:
            if _under(folded, os.path.join(root, *rel.split("/")), resolve=False):
                return label, root
    if _under(folded, os.path.join(str(claude_config_dir()), "rules")):
        return "claude-rules", None
    return None, None


def doctrine_surface(path: str) -> Optional[str]:
    """The surface label ``path`` sits under, or None when it is not doctrine."""
    try:
        return _classify(path)[0]
    except Exception:
        return None


def doctrine_root_of(path: str) -> Optional[str]:
    """The resolved content root when ``path`` is a content-root surface, else None."""
    try:
        return _classify(path)[1]
    except Exception:
        return None


def gem_admitted(payload: dict, target: str) -> bool:
    """True iff the main session holds verified-live Group EM standing and ``target`` is a surface.

    The standing is read from the nomination record at the session's launch
    anchor, never from payload cwd, env or any session-supplied claim; a
    dispatched subagent (non-empty ``agent_id``) is never admitted. The
    standing read happens only after the path test hits.
    """
    try:
        if payload.get("agent_id"):
            return False
        if doctrine_surface(target) is None:
            return False
        session_id = payload.get("session_id") or ""
        anchor = _write_bump_applicability.resolve_launch_anchor(session_id, cwd=payload.get("cwd"))
        return _gem_cross_repo_commit.session_holds_live_gem_standing(session_id, anchor)
    except Exception:
        return False


def live_gem_holder(repo_root: Optional[str]) -> Optional[dict]:
    """``{"session_id", "name"}`` of the live Group EM holder, else None. Deny paths only."""
    if not repo_root:
        return None
    try:
        from coordinator_core.group_em import nomination

        verdict = nomination.identify(repo_root)
        if verdict.get("state") != "live":
            return None
        holder = verdict.get("holder") or {}
        return {"session_id": holder.get("session_id"), "name": holder.get("name")}
    except Exception:
        return None
