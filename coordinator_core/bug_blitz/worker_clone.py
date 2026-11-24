"""worker_clone: one `clone --shared` of HEAD per bug-blitz run, where every worker stage edits and tests.

Bulk-transfer class (a throwaway clone, like `foreign_edits.head_tree`); never call it inside an engine op hot
path. A real `git clone`, never `git worktree`. Reads of carried blobs are in-process file reads, no spawn.
"""
from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Callable, Mapping, Optional, Sequence

from coordinator_core import temp_layout
from coordinator_core.git.run import run_git
from coordinator_core.ops._path_guard import safe_id

CLONES_DIRNAME = "bug-blitz-clones"
BASE_FILE = "blitz-base"
CONE_FILE = "blitz-cone.txt"
# A clone provisioned this recently is kept by the sibling sweep whatever the liveness predicate says: the
# run's stage record may not exist yet.
FRESH_S = 900.0
_TAIL = 400


class WorkerCloneError(RuntimeError):
    """The clone could not be provisioned or read."""


class InvalidRunId(WorkerCloneError, ValueError):
    """`run_id` failed `_path_guard.safe_id`; nothing was created."""


def _checked(run_id: str) -> str:
    if not isinstance(run_id, str) or not safe_id(run_id):
        raise InvalidRunId(f"run_id {run_id!r} is not a safe path component")
    return run_id


def run_dir(repo_root: Path | str, run_id: str) -> Path:
    """The run's scratch directory under `repo_root`, where the submit hook writes `CONE_FILE`."""
    from coordinator_core.bug_blitz.contract import SCRATCH_ROOT

    return Path(repo_root) / SCRATCH_ROOT / run_id


def read_cone(run_dir: Path | str) -> tuple[str, ...]:
    """The run's sparse-cone directories (one repo-relative directory per line of `<run_dir>/CONE_FILE`), in file
    order without duplicates; empty when the file is absent or blank (full checkout). An absolute, climbing or
    option-shaped entry raises `WorkerCloneError`."""
    try:
        text = (Path(run_dir) / CONE_FILE).read_text(encoding="utf-8")
    except OSError:
        return ()
    out: list = []
    for line in text.splitlines():
        entry = line.strip().replace("\\", "/").strip("/")
        if not entry:
            continue
        rel = Path(entry)
        if rel.is_absolute() or ".." in rel.parts or entry.startswith("-") or ":" in entry:
            raise WorkerCloneError(f"cone entry {line.strip()!r} is not a repo-relative directory")
        if entry not in out:
            out.append(entry)
    return tuple(out)


def _clones_dir(repo_root: Path | str) -> Path:
    return temp_layout.coordinator_temp_root(repo_root) / CLONES_DIRNAME


def clone_root(repo_root: Path | str, run_id: str) -> Optional[Path]:
    """The run's provisioned clone, or None when it has none (or `run_id` is unsafe). Pure file stat."""
    if not isinstance(run_id, str) or not safe_id(run_id):
        return None
    dest = _clones_dir(repo_root) / run_id
    return dest if (dest / ".git" / BASE_FILE).is_file() else None


def _drop(path: Path) -> None:
    try:
        temp_layout._rmtree_strict(path)
    except FileNotFoundError:
        pass
    except OSError:
        import shutil

        shutil.rmtree(path, ignore_errors=True)


def _default_live(repo_root: Path, run_id: str) -> bool:
    """Keep the clone unless the run's stage record is readable and says it ended or its owner session is dead.
    An absent or unreadable record keeps it: a bug-blitz mint writes no `run-stage.json`."""
    from coordinator_core.session import record_homes
    from coordinator_core.warm import live_runs

    data = live_runs._read_stage(Path(record_homes.home_dir(str(Path(repo_root)), "warp")) / run_id / "run-stage.json")
    if data is None:
        return True
    stage = data.get("stage")
    if not isinstance(stage, str) or not stage or stage in live_runs._TERMINAL_STAGES:
        return False
    return live_runs._owner_live(data.get("session_id"))


def _reap_dead_siblings(
    repo_root: Path, keep: str, is_live: Callable[[Path, str], bool], fresh_s: float
) -> list:
    """Remove every `bug-blitz-clones/<id>` other than `keep` whose run is not live and whose clone is older than
    `fresh_s`: one directory listing, no git spawn. Returns the reaped ids."""
    parent = _clones_dir(repo_root)
    try:
        names = os.listdir(parent)
    except OSError:
        return []
    now = time.time()
    reaped = []
    for name in names:
        if name == keep or not safe_id(name):
            continue
        path = parent / name
        try:
            if not path.is_dir() or now - path.stat().st_mtime < fresh_s:
                continue
            if is_live(repo_root, name):
                continue
        except OSError:
            continue
        _drop(path)
        reaped.append(name)
    return reaped


def provision(
    repo_root: Path | str,
    run_id: str,
    *,
    is_live: Optional[Callable[[Path, str], bool]] = None,
    fresh_s: float = FRESH_S,
) -> Path:
    """The run's clone: `clone --shared --no-checkout` of `repo_root`'s HEAD, checked out detached at that sha,
    its sha recorded in `<clone>/.git/blitz-base`. The run's cone list (`read_cone`) narrows the checkout to those
    top-level directories plus root files; an empty list checks out the whole tree. Idempotent per `run_id`.
    Also reaps sibling clones whose run
    is no longer live (`is_live(repo_root, id)`, default: the run's owner record). Raises `InvalidRunId` for an
    unsafe `run_id` before touching disk, `WorkerCloneError` when git fails."""
    _checked(run_id)
    root = Path(repo_root)
    dirs = read_cone(run_dir(root, run_id))
    existing = clone_root(root, run_id)
    _reap_dead_siblings(root, run_id, is_live or _default_live, fresh_s)
    if existing is not None:
        return existing
    dest = _clones_dir(root) / run_id
    _drop(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    head = run_git(["rev-parse", "--verify", "-q", "HEAD^{commit}"], cwd=str(root))
    sha = head.stdout.strip()
    if head.returncode or not sha:
        raise WorkerCloneError(f"run {run_id}: cannot resolve HEAD of {root.name}: {head.stderr.strip()[-_TAIL:]}")
    try:
        clone = run_git(["clone", "--shared", "--no-checkout", "-q", str(root), str(dest)], cwd=str(root))
        if clone.returncode:
            why = "timed out" if clone.timed_out else clone.stderr.strip()[-_TAIL:]
            raise WorkerCloneError(f"run {run_id}: clone failed: {why}")
        if dirs:
            cone = run_git(["sparse-checkout", "set", "--cone", *dirs], cwd=str(dest))
            if cone.returncode:
                why = "timed out" if cone.timed_out else cone.stderr.strip()[-_TAIL:]
                raise WorkerCloneError(f"run {run_id}: sparse-checkout of {', '.join(dirs)} failed: {why}")
        out = run_git(["checkout", "-q", "--detach", sha], cwd=str(dest))
        if out.returncode:
            why = "timed out" if out.timed_out else out.stderr.strip()[-_TAIL:]
            raise WorkerCloneError(f"run {run_id}: checkout of {sha[:12]} failed: {why}")
        (dest / ".git" / BASE_FILE).write_text(sha + "\n", encoding="utf-8")
    except BaseException:
        _drop(dest)
        raise
    return dest


def base_sha(clone: Path | str) -> str:
    """The sha the clone was provisioned at; '' when it carries no record."""
    try:
        return (Path(clone) / ".git" / BASE_FILE).read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def carried_blobs(clone: Path | str, paths: Sequence[str]) -> Mapping[str, Optional[bytes]]:
    """`{path: bytes}` of each repo-relative `paths` entry as it stands in the clone's worktree; None for a path
    absent there (a deletion). In-process reads. A path that is absolute or climbs out of the clone raises
    `WorkerCloneError`."""
    base = Path(clone).resolve()
    out: dict = {}
    for p in paths:
        rel = Path(p)
        if rel.is_absolute() or ".." in rel.parts:
            raise WorkerCloneError(f"carried path {p!r} is not repo-relative inside the clone")
        target = base / rel
        try:
            out[p] = target.read_bytes() if target.is_file() else None
        except OSError:
            out[p] = None
    return out


def reap(repo_root: Path | str, run_id: str) -> bool:
    """Remove the run's clone; True when a directory was removed. Idempotent; an unsafe `run_id` raises
    `InvalidRunId`."""
    _checked(run_id)
    dest = _clones_dir(repo_root) / run_id
    if not dest.exists():
        return False
    _drop(dest)
    return not dest.exists()
