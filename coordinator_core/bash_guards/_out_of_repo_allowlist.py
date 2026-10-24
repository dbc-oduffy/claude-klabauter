"""The one closed list of out-of-repo destinations an EM's write may reach.

Under the out-of-repo write block every write outside the session repo is
refused except a destination named here. `sanctioned_destination` answers for
the Bash, interpreter and tool surfaces alike, so no surface decides alone.

CLOSED-LIST RULE: an addition to `SANCTIONED_DESTINATIONS` cites a PM or APM
ruling in its comment, and `test_out_of_repo_allowlist` pins the tuple. Engine-op
writes (memo delivery into a receiver inbox, publish, research clones) are not
entries: they run in engine code, not through an EM's tool call.

Each entry delegates to the predicate that already decides it:
claude-home, settings-home, agent-memory-store, os-temp-scratch and
declared-run-output-root come from `_write_bump_applicability`; session-repo-scratch
is plain containment under `<session repo>/scratch`; session-share-dir
from `machinery_paths.share_dir`; lessons-outbox from `is_lessons_outbox_write`.

Differences found between surfaces (the tool surface's semantics win; the Bash
legs gain allows and lose none):
- lessons-outbox: only the tool surface exempted it; the Bash foreign-repo guard
  carried a duplicate and the Bash outside-repo guard none. Path-shape only, not
  gated on which repo owns the path, because the target is the central lessons
  repo by design.
- share dir: only the current share root is sanctioned; the retired
  `state/subagent-share` root stays out of bounds.
- os-temp-scratch and settings-home keep their git-repo conditions: a checkout
  under temp, or nested in the settings home, is still a foreign repo.

`sanctioned_destination` never raises: an unresolvable input is not sanctioned.
"""

from __future__ import annotations

from pathlib import Path
from typing import Mapping, Optional

from coordinator_core.bash_guards._write_bump_applicability import (
    _is_under,
    _resolve_path,
    is_agent_memory_store_path,
    target_is_bare_temp_scratch,
    target_is_under_claude_home,
    target_is_under_declared_run_output_root,
    target_is_under_settings_home,
)
from coordinator_core.session import machinery_paths
from coordinator_core.subagent_sandbox.provision_report import _sanitize_segment
from coordinator_core.write_guards._case_fold_path import casefold_path

SANCTIONED_DESTINATIONS: tuple[str, ...] = (
    "claude-home",
    "settings-home",
    "agent-memory-store",
    "os-temp-scratch",
    "session-share-dir",
    "lessons-outbox",
    "declared-run-output-root",
    # PM exit criterion, sizing 2026-10-24-scratch-nested-clone-is-in-repo-15f195.
    "session-repo-scratch",
)

_LESSONS_OUTBOX_SEGMENTS = ("state", "lessons-outbox")


def is_lessons_outbox_write(path: str) -> bool:
    """True iff the case-folded `path` holds `state/lessons-outbox` as an adjacent
    directory pair, whichever repo owns it. Deliberately not widened to
    `cross-repo/inbox` or `outbox`: those are the memo channel and take
    `cross-repo-memo`."""
    if not path:
        return False
    parts = [p for p in casefold_path(path).split("/") if p]
    for i in range(len(parts) - 1):
        if (parts[i], parts[i + 1]) == _LESSONS_OUTBOX_SEGMENTS:
            return True
    return False


def _is_under_session_share_dir(
    path: str, session_repo: Optional[str], session_id: str
) -> bool:
    if not path or not session_repo or not session_id:
        return False
    sanitized = _sanitize_segment(session_id)
    if not sanitized:
        return False
    target_cf = _resolve_path(path)
    if target_cf is None:
        return False
    root_cf = _resolve_path(machinery_paths.share_dir(session_repo, sanitized))
    return root_cf is not None and _is_under(target_cf, root_cf)


def _is_under_session_repo_scratch(path: str, session_repo: Optional[str]) -> bool:
    """Path containment under `<session_repo>/scratch`, deciding by location and
    not by the target's git toplevel, so a nested clone there stays in-repo."""
    if not path or not session_repo:
        return False
    target_cf = _resolve_path(path)
    root_cf = _resolve_path(str(Path(session_repo) / "scratch"))
    return target_cf is not None and root_cf is not None and _is_under(target_cf, root_cf)


def sanctioned_destination(
    path: str,
    *,
    session_repo: Optional[str],
    session_id: str,
    target_gitdir: Optional[Path] = None,
    env: Optional[Mapping[str, str]] = None,
) -> Optional[str]:
    """The `SANCTIONED_DESTINATIONS` entry `path` falls under, else None. Never raises.

    `target_gitdir` is the git-dir the caller already resolved for `path`; None
    means the caller found no repo, which is what os-temp-scratch and
    settings-home require."""
    try:
        if not path:
            return None
        env_map = None if env is None else dict(env)
        if target_is_under_claude_home(path, env_map):
            return "claude-home"
        if target_is_under_settings_home(path, env_map, target_gitdir):
            return "settings-home"
        if is_agent_memory_store_path(path):
            return "agent-memory-store"
        if target_gitdir is None and target_is_bare_temp_scratch(path, env_map):
            return "os-temp-scratch"
        if _is_under_session_share_dir(path, session_repo, session_id):
            return "session-share-dir"
        if is_lessons_outbox_write(path):
            return "lessons-outbox"
        if target_is_under_declared_run_output_root(path):
            return "declared-run-output-root"
        if _is_under_session_repo_scratch(path, session_repo):
            return "session-repo-scratch"
        return None
    except Exception:
        return None
