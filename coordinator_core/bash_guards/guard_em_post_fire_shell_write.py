"""Records a Bash write to a tracked product path once the EM session has fired a Workflow run.

The Bash twin of the Edit/Write post-fire record: both decide through `_em_post_fire`, whose
`GUARD_NAME` is unregistered in `deny_registry`, so the hit is a log-only advisory record and
the command proceeds. One tmp read answers "has this session fired" before any command parsing;
an unfired session, a subagent payload and an empty session id return at once. No subprocess.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

_PREFILTER_TOKENS = (">", "tee", "<<", "-c", "sed", "cp", "mv", "git", "rm", "perl", "patch", "truncate", "dd", "rsync", "install")


def check(cmd: str, session_id: str, cwd: str, payload: Optional[Dict[str, Any]] = None) -> Any:
    """Always None: the first product write target of `cmd` in a fired EM session is recorded to the
    advisory counter, nothing reaches the context. Fail-open on a crash."""
    payload = payload or {}
    if not cmd or "agent_id" in payload or not session_id:
        return None
    if not any(tok in cmd for tok in _PREFILTER_TOKENS):
        return None
    try:
        return _check(cmd, session_id, cwd, payload)
    except Exception:  # noqa: BLE001 -- fail-open
        return None


_COMPOUND_MARKERS = ("&", ";", "|", "\n", "`", "$(", ">", "<")


def _is_plain_rm(cmd: str) -> bool:
    """True for one simple `rm` segment: no chaining, redirection or substitution, no recursive or
    directory flag."""
    import shlex

    if any(m in cmd for m in _COMPOUND_MARKERS):
        return False
    try:
        tokens = shlex.split(cmd)
    except ValueError:
        return False
    if len(tokens) < 2 or tokens[0] != "rm":
        return False
    for tok in tokens[1:]:
        if tok == "--":
            break
        if tok in ("--recursive", "--dir"):
            return False
        if tok.startswith("-") and not tok.startswith("--") and any(c in tok for c in "rRd"):
            return False
    return True


def _is_git_stage_commit_only(cmd: str) -> bool:
    """True when every `&&`/`;`/newline segment of `cmd` is a `git add` or `git commit`: a commit of
    files already on disk, never a product write."""
    import re

    segments = [s.strip() for s in re.split(r"&&|;|\n", cmd) if s.strip()]
    return bool(segments) and all(re.match(r"git\s+(add|commit)\b", s) for s in segments) and not any(
        m in cmd for m in ("|", ">", "<", "`", "$(")
    )


def _plain_pathspec_revert_paths(cmd: str) -> list:
    """The pathspec tokens of one simple `git checkout -- <paths>` / `git restore <paths>` segment
    (backslashes read as separators), or [] when `cmd` is anything else: chaining, redirection,
    substitution, a flag other than `--`, a tree-ish or `--source`."""
    import shlex

    if any(m in cmd for m in _COMPOUND_MARKERS):
        return []
    try:
        tokens = shlex.split(cmd.replace("\\", "/"))
    except ValueError:
        return []
    if len(tokens) < 3 or tokens[0] != "git" or tokens[1] not in ("checkout", "restore"):
        return []
    rest = tokens[2:]
    if tokens[1] == "checkout":
        if rest[0] != "--":
            return []
        rest = rest[1:]
    elif rest[0] == "--":
        rest = rest[1:]
    return [] if any(t.startswith("-") for t in rest) else rest


def _session_written_rels(session_id: str, root: str) -> set:
    """Repo-relative paths the session's Edit/Write touch record holds a live TOUCH for; empty on
    any read failure."""
    from pathlib import Path

    try:
        from coordinator_core.lifecycle import git_common_dir
        from coordinator_core.session import touch_record

        sink = touch_record.sink_path(Path(git_common_dir(Path(root))) / "coordinator-sessions" / session_id)
        claims, _degraded, _reasons = touch_record._read_stream_claims(sink)
        return {p for p, ev in claims.items() if ev.verb == touch_record.VERB_TOUCH}
    except Exception:  # noqa: BLE001 -- unreadable record: the revert stays denied
        return set()


def _reverts_only_own_writes(cmd: str, cwd: str, root: str, session_id: str) -> bool:
    """True when `cmd` is a plain pathspec revert and every path is one this session wrote: the
    effect restores the session's own bytes, not a product write."""
    import os

    specs = _plain_pathspec_revert_paths(cmd)
    if not specs:
        return False
    written = _session_written_rels(session_id, root)
    if not written:
        return False
    base = os.path.abspath(root)
    where = cwd or base
    return all(
        os.path.relpath(os.path.normpath(s if os.path.isabs(s) else os.path.join(where, s)), base).replace(os.sep, "/")
        in written
        for s in specs
    )


def _check(cmd: str, session_id: str, cwd: str, payload: Dict[str, Any]) -> Any:
    from coordinator_core.bash_guards._shell_write_projection import shell_writes
    from coordinator_core.hooks import _em_post_fire
    from coordinator_core.hooks.nudge_unrouted_sizing import _repo_root

    if cwd and not payload.get("cwd"):
        payload = dict(payload, cwd=cwd)
    root = _repo_root(payload)
    if not root or not _em_post_fire.fired(session_id, root):
        return None
    targets = shell_writes(cmd, cwd or payload.get("cwd") or "", root, with_git=True)
    if targets and _is_plain_rm(cmd) and all(
        _em_post_fire.is_empty_untracked(t.abs_path, root) for t in targets
    ):
        return None
    if _reverts_only_own_writes(cmd, cwd or payload.get("cwd") or "", root, session_id):
        return None
    if _is_git_stage_commit_only(cmd):
        return None
    for target in targets:
        if _em_post_fire.is_product_path(target.abs_path, root, session_id):
            _em_post_fire.deny_envelope(session_id, cwd or payload.get("cwd"), (target.abs_path,))
            return None
    return None
