"""coordinator_core.sidecar_reap — the delete-by-convention sweep over the
subagent-share sidecar homes (DR-091), shared by `coordinator/bin/reap-stale-subagent-sidecars.py`
and the `updatedocs.gates` 11j gate so the gate reaps in-process instead of spawning the CLI.

Deletion gate — session liveness AND/OR an age floor, NEVER `status:` alone:

  1. LIVENESS (per session directory). Each child of a share root is named for the
     requesting lead session; a live session's directory is preserved whole. The
     decision is `session_live()` only — never mtime/pid of the directory for liveness.
  2. AGE FLOOR (per file). A sidecar younger than the floor is preserved.
  3. STATUS can only subtract: `blocked` / `thrashing` are never reaped.

Negative spec: never mutates a sidecar's contents; only deletes whole files that clear
every gate. Tracked sidecars go through one `git rm` + one `git commit`; untracked
ones (everything under the gitignored machinery root) through plain `os.remove`.
"""

from __future__ import annotations

import os
import sys
import time
import uuid
from pathlib import Path as _Path
from typing import Callable, Optional, TextIO

from coordinator_core.git.run import run_git

DEFAULT_AGE_FLOOR_DAYS = 14

#: status: values that NEVER get reaped, regardless of liveness/age.
_STATUS_NEVER_REAP = {"blocked", "thrashing"}

#: Entries under a share root that are not session-id leaves.
_NON_SESSION_DIR_NAMES = {".git", "__pycache__"}

_PROG = "reap-stale-subagent-sidecars.py"


def fm_field(path: str, key: str) -> str:
    """Single-key frontmatter scan (first frontmatter line starting `key:`), quotes stripped."""
    prefix = key + ":"
    val = ""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            in_fm = False
            for line in fh:
                stripped_line = line.rstrip("\n").rstrip("\r")
                if not in_fm:
                    if stripped_line.strip() == "---":
                        in_fm = True
                    continue
                if stripped_line.strip() == "---":
                    break
                if stripped_line.startswith(prefix):
                    val = stripped_line[len(prefix):].strip()
                    break
    except OSError:
        return ""
    if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
        val = val[1:-1]
    return val


def write_pathspec_file(paths: list) -> str:
    """Write `paths` newline-delimited to a unique temp file for `--pathspec-from-file`.

    The argv form of a tracked-reap pathspec list exceeds the ~32KB Windows
    CreateProcess ceiling (WinError 206) at population scale; the file form also
    keeps the tracked reap one atomic commit. Caller owns cleanup.
    """
    from coordinator_core.temp_layout import managed_tempdir

    pathspec_path = os.path.join(
        managed_tempdir(), f"reap-stale-sidecars-pathspec-{os.getpid()}-{uuid.uuid4().hex}.txt"
    )
    with open(pathspec_path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("\n".join(paths) + "\n")
    return pathspec_path


def tracked_paths(repo_root: str, rel_paths: list, *, under) -> set:
    """Which of `rel_paths` are tracked, from ONE `git ls-files` spawn scoped to `under`.

    `under` is keyword-only with no default and takes the WHOLE root set: git accepts
    many pathspecs in one invocation, and a default naming one root silently classified
    every candidate under a second root as untracked. A spawn failure returns the empty
    set for all roots (an unclassifiable path is reaped by plain rm, never `git rm`).
    `git ls-files` echoes POSIX separators even on Windows; both legs compare via
    `.as_posix()` and the result keeps the caller's original strings.
    """
    if not rel_paths:
        return set()
    roots = [under] if isinstance(under, str) else list(under)
    if not roots:
        return set()
    result = run_git(["ls-files", "--", *(_Path(root).as_posix() for root in roots)], cwd=repo_root)
    if result.returncode != 0:
        return set()
    tracked_posix = {line for line in result.stdout.splitlines() if line}
    posix_by_orig = {rel: _Path(rel).as_posix() for rel in rel_paths}
    return {rel for rel, posix in posix_by_orig.items() if posix in tracked_posix}


def _md_files(session_dir: str) -> list:
    """`*.md` regular files directly under `session_dir`, sorted, as `(path, mtime)`.

    One scandir supplies the type and mtime; a file is opened only if it reaches the
    status check.
    """
    out = []
    try:
        with os.scandir(session_dir) as it:
            for entry in it:
                if not entry.name.endswith(".md"):
                    continue
                try:
                    if not entry.is_file():
                        continue
                    out.append((entry.path, entry.stat().st_mtime))
                except OSError:
                    continue
    except OSError:
        return []
    out.sort()
    return out


def classify_sidecars(share_dirs: list, session_live: Callable, *, repo_root: str, now: float, age_floor_days: int):
    """The reap decision: `(to_reap, preserved_live, preserved_status, preserved_too_young)`.

    Pure over the filesystem and the injected `session_live` predicate — no git, no
    mutation. Age is tested before status so a young file is never opened; a young
    blocked/thrashing sidecar is therefore counted as too-young, not as status-retained.
    """
    to_reap: list = []
    preserved_live_session = 0
    preserved_too_young = 0
    preserved_status = 0

    for share_dir in share_dirs:
        with os.scandir(share_dir) as it:
            session_entries = sorted((e.name, e.path, e.is_dir()) for e in it)
        for session_id, session_dir, is_dir in session_entries:
            if session_id in _NON_SESSION_DIR_NAMES or not is_dir:
                continue

            files = _md_files(session_dir)
            if session_live(session_id, cwd=repo_root):
                preserved_live_session += len(files)
                continue

            for f, mtime in files:
                if max(0.0, (now - mtime) / 86400.0) < age_floor_days:
                    preserved_too_young += 1
                    continue
                if fm_field(f, "status").lower() in _STATUS_NEVER_REAP:
                    preserved_status += 1
                    continue
                to_reap.append(f)

    return to_reap, preserved_live_session, preserved_status, preserved_too_young


def reap_stale_sidecars(
    repo_root: str,
    session_live: Callable,
    *,
    dry_run: bool = False,
    age_floor_days: int = DEFAULT_AGE_FLOOR_DAYS,
    out: Optional[TextIO] = None,
    err: Optional[TextIO] = None,
) -> int:
    """Run the sweep against `repo_root`; returns the CLI exit code (0 normal, 1 git failure).

    Stdout lines are the CLI's contract (`reaped <path>` per file, then a count line);
    `out`/`err` default to the process streams and let an in-process caller capture them.
    """
    out = sys.stdout if out is None else out
    err = sys.stderr if err is None else err

    from coordinator_core.session.machinery_paths import share_roots

    share_dirs = [d for d in share_roots(repo_root) if os.path.isdir(d)]
    if not share_dirs:
        print(f"{_PROG}: no subagent-share root exists; nothing to do", file=out)
        return 0

    to_reap, preserved_live_session, preserved_status, preserved_too_young = classify_sidecars(
        share_dirs, session_live, repo_root=repo_root, now=time.time(), age_floor_days=age_floor_days,
    )

    if not to_reap:
        print("nothing to reap", file=out)
        if preserved_live_session:
            print(f"{preserved_live_session} sidecar(s) retained (owning session still live)", file=out)
        if preserved_status:
            print(f"{preserved_status} sidecar(s) retained (status: blocked/thrashing)", file=out)
        if preserved_too_young:
            print(f"{preserved_too_young} sidecar(s) retained (younger than {age_floor_days}-day age floor)", file=out)
        return 0

    rel_by_file = {f: os.path.relpath(f, repo_root) for f in to_reap}
    tracked_rels: set = tracked_paths(
        repo_root,
        list(rel_by_file.values()),
        under=[os.path.relpath(share_dir, repo_root) for share_dir in share_dirs],
    )
    tracked_to_reap = []
    untracked_to_reap = []
    for f in to_reap:
        if rel_by_file[f] in tracked_rels:
            tracked_to_reap.append(f)
        else:
            untracked_to_reap.append(f)

    if dry_run:
        for f in tracked_to_reap:
            print(f"{_PROG}: [dry-run] would reap {f}", file=out)
        for f in untracked_to_reap:
            print(f"{_PROG}: [dry-run] would reap {f} (untracked; plain rm)", file=out)
        print(f"{len(to_reap)} stale subagent-share sidecar(s) would be reaped (dry-run)", file=out)
        if preserved_live_session:
            print(f"{preserved_live_session} sidecar(s) would be retained (owning session still live)", file=out)
        if preserved_status:
            print(f"{preserved_status} sidecar(s) would be retained (status: blocked/thrashing)", file=out)
        if preserved_too_young:
            print(f"{preserved_too_young} sidecar(s) would be retained (younger than {age_floor_days}-day age floor)", file=out)
        return 0

    from coordinator_core.review_trail.sweep_tombstone import record_swept

    def _tombstone(files: list) -> None:
        by_session: dict = {}
        for f in files:
            by_session.setdefault(os.path.basename(os.path.dirname(f)), []).append(rel_by_file[f])
        for sid, rels in by_session.items():
            record_swept(repo_root, sid, rels, "sidecar_reap", time.time())

    for f in untracked_to_reap:
        try:
            os.remove(f)
        except OSError:
            pass
        else:
            _tombstone([f])
        print(f"reaped {f} (untracked, plain rm)", file=out)

    if tracked_to_reap:
        tracked_rel = [os.path.relpath(f, repo_root) for f in tracked_to_reap]
        pathspec_file = write_pathspec_file(tracked_rel)
        try:
            rm_res = run_git(["rm", "-q", f"--pathspec-from-file={pathspec_file}"], cwd=repo_root)
            if rm_res.timed_out:
                err.write(f"{_PROG}: git rm timed out\n")
                return 1
            if rm_res.returncode != 0:
                err.write(rm_res.stderr)
                return rm_res.returncode
            _tombstone(tracked_to_reap)

            commit_msg = f"reap {len(tracked_to_reap)} stale subagent-share sidecar(s)"
            commit_res = run_git(
                ["commit", "-q", "-m", commit_msg, f"--pathspec-from-file={pathspec_file}"],
                cwd=repo_root,
            )
            if commit_res.timed_out:
                err.write(
                    f"{_PROG}: git commit timed out after git rm — "
                    f"{len(tracked_to_reap)} sidecar(s) staged for deletion, not committed:\n"
                )
                for f in tracked_to_reap:
                    err.write(f"  {f}\n")
                return 1
            if commit_res.returncode != 0:
                err.write(
                    f"{_PROG}: git commit failed after git rm — "
                    f"{len(tracked_to_reap)} sidecar(s) staged for deletion, not committed:\n"
                )
                for f in tracked_to_reap:
                    err.write(f"  {f}\n")
                return commit_res.returncode
        finally:
            try:
                os.remove(pathspec_file)
            except OSError:
                pass
        for f in tracked_to_reap:
            print(f"reaped {f}", file=out)

    print(f"{len(to_reap)} stale subagent-share sidecar(s) reaped", file=out)
    if preserved_live_session:
        print(f"{preserved_live_session} sidecar(s) retained (owning session still live)", file=out)
    if preserved_status:
        print(f"{preserved_status} sidecar(s) retained (status: blocked/thrashing)", file=out)
    if preserved_too_young:
        print(f"{preserved_too_young} sidecar(s) retained (younger than {age_floor_days}-day age floor)", file=out)
    return 0
