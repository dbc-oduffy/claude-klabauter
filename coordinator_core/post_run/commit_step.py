"""post_run commit step: land one run's scoped paths through the in-process `ceremony.commit_v2`.

`commit_scoped` takes plain arguments (worktree root, the run's touched paths, the transform
outputs, the evidence files, a message) and commits exactly their union, never `git add -A`: a
peer's dirty file in the shared tree is neither staged nor reverted. The gitignore verdict is
computed in process and passed as `ignored_untracked`, so commit_v2 runs no `git check-ignore`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, List, Optional

__all__ = ["CommitResult", "commit_scoped", "scoped_paths"]


@dataclass(frozen=True)
class CommitResult:
    """Outcome of `commit_scoped`.

    `ok` is true when the scoped paths are clean at HEAD afterwards: a landed commit, or nothing
    left to commit (a rerun that converged). `sha` is the landed commit, or the prior commit
    carrying the same message; None when nothing needed landing. `error` is commit_v2's refusal
    verbatim when `ok` is false. `dropped_ignored` lists scoped paths left out because they are
    gitignored and untracked. commit_v2 lands process paths (`state/`, `tasks/`) as a second commit
    beside the product one; `process_sha` is that commit, None when the scope had none.
    """

    ok: bool
    committed: bool
    sha: Optional[str]
    paths: List[str] = field(default_factory=list)
    error: Optional[str] = None
    dropped_ignored: List[str] = field(default_factory=list)
    process_sha: Optional[str] = None


def _rel(worktree: Path, raw: str) -> str:
    """Repo-relative `/`-separated form of `raw`; an absolute path must sit inside `worktree`."""
    text = str(raw).replace("\\", "/")
    p = Path(text)
    if p.is_absolute():
        try:
            text = p.resolve().relative_to(worktree.resolve()).as_posix()
        except ValueError as exc:
            raise ValueError(f"path outside the worktree: {raw}") from exc
    while text.startswith("./"):
        text = text[2:]
    if not text or text == "." or text.split("/")[0] == ".git" or ".." in text.split("/"):
        raise ValueError(f"not an explicit repo-relative path: {raw!r}")
    return text


def scoped_paths(
    worktree: Path,
    touched: Iterable[str],
    transform_outputs: Iterable[str],
    evidence: Iterable[str],
) -> List[str]:
    """Sorted, deduplicated union of the three path sets, each normalised repo-relative.

    Raises ValueError on a directory, an escaping path or a `.git` path: the commit pathspec is
    explicit files only.
    """
    out = set()
    for raw in (*touched, *transform_outputs, *evidence):
        rel = _rel(worktree, raw)
        if (worktree / rel).is_dir():
            raise ValueError(f"directory in the commit scope: {raw}")
        out.add(rel)
    return sorted(out)


def _ignored_verdict(worktree: Path, paths: List[str]) -> Optional[List[str]]:
    """The untracked, gitignored subset of `paths` read in process; None when any verdict is a
    gap (the handler then keeps its own probe)."""
    from coordinator_core.git import git_index
    from coordinator_core.git.ignore_match import IgnoreMatcher

    try:
        tracked = set(git_index.parse_index_identity(worktree, wanted=paths))
    except (git_index.IndexParseError, OSError):
        return None
    matcher = IgnoreMatcher(worktree)
    ignored: List[str] = []
    for p in paths:
        if p in tracked or not (worktree / p).exists():
            continue
        verdict = matcher.ignored(p, False)
        if verdict is None:
            return None
        if verdict:
            ignored.append(p)
    return ignored


def commit_scoped(
    worktree_root: Path,
    touched: Iterable[str],
    transform_outputs: Iterable[str],
    evidence: Iterable[str],
    message: str,
    *,
    session_id: Optional[str] = None,
    on_main_reason: Optional[str] = None,
) -> CommitResult:
    """Commit the union of `touched`, `transform_outputs` and `evidence` in one scoped commit.

    `worktree_root` is the originating worktree (never a git common dir). A path absent from disk
    and tracked at HEAD lands as a deletion; one absent from both, or gitignored and untracked,
    is dropped. Never raises on a commit refusal: the refusal is returned in
    `CommitResult.error`; a malformed path raises ValueError before any git work.
    `on_main_reason` is forwarded as commit_v2's one door past the main-branch refusal.
    """
    from coordinator_core.git.git_dir import resolve_git_common_dir
    from coordinator_core.ops.ceremony.commit_v2 import _handler

    root = Path(worktree_root)
    paths = scoped_paths(root, touched, transform_outputs, evidence)
    if not paths:
        return CommitResult(ok=True, committed=False, sha=None, paths=[])

    params: dict = {
        "paths": paths,
        "message": message if message.endswith("\n") else message + "\n",
        "gone_tracked_as_deleted": True,
        "skip_missing": True,
        "drop_ignored": True,
    }
    ignored = _ignored_verdict(root, paths)
    if ignored is not None:
        params["ignored_untracked"] = ignored
    if session_id:
        params["session_id"] = session_id
    if on_main_reason:
        params["on_main_reason"] = on_main_reason

    try:
        reply = _handler(params, repo_root=resolve_git_common_dir(root))
    except Exception as exc:  # noqa: BLE001 -- the commit outcome is unknown; surfaced, never swallowed
        return CommitResult(ok=False, committed=False, sha=None, paths=paths,
                            error=f"{type(exc).__name__}: {exc}")
    if reply.get("committed"):
        return CommitResult(ok=True, committed=True, sha=reply.get("sha"), paths=paths,
                            dropped_ignored=list(reply.get("dropped_ignored") or []),
                            process_sha=reply.get("process_sha"))
    if reply.get("already_landed_sha"):
        return CommitResult(ok=True, committed=False, sha=reply["already_landed_sha"], paths=paths)
    if reply.get("nothing_to_commit"):
        return CommitResult(ok=True, committed=False, sha=None, paths=paths)
    return CommitResult(ok=False, committed=False, sha=None, paths=paths,
                        error=str(reply.get("error") or reply))
