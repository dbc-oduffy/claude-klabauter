"""Stamp-ancestry gate for the land path: is the live engine stamp's source commit in HEAD, and if
not, merge it first or refuse typed.

Invariants:
  - `check` spends one spawn (zero for a None stamp); `merge_stamp` at most two.
  - The trial (`merge-tree --write-tree`) runs in the object store, so a conflict is refused
    before the worktree or index is touched. The real merge only starts after a clean trial, and
    nothing here ever calls `merge --abort` (it would revert peers' uncommitted edits).
  - Nothing raises; an unreadable stamp or a trial that cannot run is `unknown` and the caller
    lands with an advisory.
"""

from __future__ import annotations

from typing import Any, Dict, NamedTuple, Optional

from coordinator_core.git.run import run_git

CURRENT = "current"
BEHIND = "behind"
UNKNOWN = "unknown"

REFUSED = "stamp-merge-conflict"


class Verdict(NamedTuple):
    """`state` is one of CURRENT, BEHIND, UNKNOWN; `stamp` is the sha asked about (may be None)."""

    state: str
    stamp: Optional[str]


def live_stamp_sha() -> Optional[str]:
    """The running engine's stamp source sha, read in process with zero spawns; None for an
    unstamped source tree (a dev checkout, not a stamp). `skew` is imported lazily: it pulls
    subprocess."""
    from pathlib import Path

    from coordinator_core.warm.skew import read_engine_stamp_sha

    return read_engine_stamp_sha(Path(__file__).resolve().parents[3])


def _git(root, args) -> Any:
    from coordinator_core.git_scope import scoped_git_env

    return run_git(["-C", str(root), *args], env=scoped_git_env())


def check(root, stamp: Optional[str]) -> Verdict:
    """Whether `stamp` is an ancestor of HEAD: rc 0 current, rc 1 behind, anything else unknown
    (128 is an object absent from this repo, as in a foreign-repo land)."""
    if not stamp:
        return Verdict(UNKNOWN, stamp)
    res = _git(root, ["merge-base", "--is-ancestor", stamp, "HEAD"])
    if res.timed_out:
        return Verdict(UNKNOWN, stamp)
    if res.returncode == 0:
        return Verdict(CURRENT, stamp)
    if res.returncode == 1:
        return Verdict(BEHIND, stamp)
    return Verdict(UNKNOWN, stamp)


def _refusal(stamp: str, branch: str, detail: str) -> Dict[str, Any]:
    remedy = f"git merge {stamp}"
    return {
        "refused": REFUSED,
        "stamp_sha": stamp,
        "remedy": remedy,
        "detail": detail,
        "message": (
            f"branch {branch} lacks live stamp source {stamp} and it cannot be merged "
            f"automatically ({detail}); run `{remedy}` on {branch}, resolve, re-fire"
        ),
    }


def _merge_head_present(root) -> bool:
    from coordinator_core.git.git_dir import resolve_git_dir

    try:
        return (resolve_git_dir(root) / "MERGE_HEAD").exists()
    except OSError:
        return False


def _tail(res) -> str:
    text = (res.stderr or res.stdout or "").strip()
    return text.splitlines()[0] if text else f"git exit {res.returncode}"


def merge_stamp(root, stamp: str, branch: str) -> Dict[str, Any]:
    """Merge `stamp` into HEAD after a clean object-store trial.

    Returns `{merged: True, sha, merge_commit}` on success, `{state: "unknown", reason}` when the
    trial could not run, else a `refused: "stamp-merge-conflict"` dict carrying `stamp_sha`,
    `remedy`, `detail` and `message`.
    """
    trial = _git(root, ["merge-tree", "--write-tree", "HEAD", stamp])
    if trial.timed_out:
        return {"state": UNKNOWN, "reason": "merge-tree timed out"}
    if trial.returncode == 1:
        return _refusal(stamp, branch, "trial merge conflicts")
    if trial.returncode != 0:
        return {"state": UNKNOWN, "reason": f"merge-tree rc {trial.returncode}: {_tail(trial)}"}

    res = _git(
        root,
        ["merge", "--no-edit", "--no-ff", "-m", f"land: merge live stamp source {stamp}", stamp],
    )
    if res.returncode == 0:
        from coordinator_core.git.git_state import head_sha

        return {"merged": True, "sha": stamp, "merge_commit": head_sha(root, fresh=True)}
    if _merge_head_present(root):
        out = _refusal(stamp, branch, "merge-in-progress")
        out["remedy"] = "git merge --continue"
        out["message"] = (
            f"merge of live stamp source {stamp} into {branch} started but did not commit "
            f"({_tail(res)}); run `git merge --continue`, resolve, re-fire"
        )
        return out
    return _refusal(stamp, branch, _tail(res))
