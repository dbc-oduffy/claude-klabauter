"""Conclude a script-fired reconcile merge left open by the start op.

Verifies the open merge (MERGE_HEAD plus a live conflict manifest), that no
listed path still holds a conflict marker, that no index entry is unmerged,
and that nothing outside the merge's own change set is staged; only then
commits the merge. Refusals print the pinned refusal string plus offending
paths on stderr, exit 3, and commit nothing. Success prints
`RECONCILED-MERGE-CONCLUDED branch=<b>`, removes the manifest, exit 0.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from coordinator_core.git import reconcile_manifest as rm
from coordinator_core.git.machine_local_paths import machine_local_paths
from coordinator_core.git.run import GitResult, run_git
from coordinator_core.git.repo_root import show_toplevel
from coordinator_core.git_lock_retry import run_with_lock_retry

_REFUSED = 3


def _git(args: list[str], cwd: Path, env: dict[str, str] | None = None) -> GitResult:
    return run_git(args, cwd=str(cwd), env=env)


def _echo(proc: GitResult) -> None:
    if proc.stdout:
        sys.stderr.write(proc.stdout)
    if proc.stderr:
        sys.stderr.write(proc.stderr)


def _refuse(code: str, paths: list[str] | tuple[str, ...] = ()) -> int:
    print(code, file=sys.stderr)
    for path in paths:
        print(path, file=sys.stderr)
    return _REFUSED


def _marker_paths(repo_root: Path, paths: tuple[str, ...]) -> list[str]:
    hits = []
    for rel in paths:
        try:
            text = (repo_root / rel).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if rm.has_marker(text):
            hits.append(rel)
    return hits


def _parse_name_status(raw: str) -> list[tuple[str, list[str]]]:
    """(status letter, paths) per entry of `--name-status -z`; renames and
    copies carry two paths."""
    tokens = [t for t in raw.split("\0") if t]
    entries = []
    i = 0
    while i < len(tokens):
        status = tokens[i]
        width = 2 if status[0] in "RC" else 1
        entries.append((status[0], tokens[i + 1 : i + 1 + width]))
        i += 1 + width
    return entries


def _commit_merge(repo_root: Path, env: dict[str, str]) -> GitResult:
    return _git(["commit", "--no-edit"], repo_root, env=env)


def main(argv: list[str]) -> int:
    repo_root = Path(show_toplevel() or Path.cwd())

    if rm.live_merge_head(repo_root) is None:
        return _refuse(rm.REFUSAL_NO_MERGE)
    manifest = rm.read(repo_root)
    if manifest is None:
        return _refuse(rm.REFUSAL_MANIFEST_STALE)

    markers = _marker_paths(repo_root, manifest.paths)
    if markers:
        return _refuse(rm.REFUSAL_MARKERS, markers)

    add = run_with_lock_retry(lambda: _git(["add", "-A", "--", *manifest.paths], repo_root))
    if add.returncode != 0:
        _echo(add)
        return add.returncode

    staged = _git(["diff", "--cached", "--name-status", "-z"], repo_root)
    if staged.returncode != 0:
        _echo(staged)
        return staged.returncode
    entries = _parse_name_status(staged.stdout)
    unmerged = sorted({p for s, ps in entries if s == "U" for p in ps})
    if unmerged:
        return _refuse(rm.REFUSAL_UNMERGED, unmerged)

    local = machine_local_paths(ps[-1] for s, ps in entries if s in "AMRC")
    if local:
        return _refuse(rm.REFUSAL_MACHINE_LOCAL, local)

    merged = _git(["diff", "--name-only", "-z", "HEAD...MERGE_HEAD"], repo_root)
    if merged.returncode != 0:
        _echo(merged)
        return merged.returncode
    allowed = {p for p in merged.stdout.split("\0") if p} | set(manifest.paths)
    foreign = sorted({p for _, ps in entries for p in ps} - allowed)
    if foreign:
        return _refuse(rm.REFUSAL_FOREIGN_STAGED, foreign)

    env = dict(os.environ)
    env["COORDINATOR_OVERRIDE_BRANCH"] = "1"
    env["COORDINATOR_OVERRIDE_BRANCH_REASON"] = (
        "reconcile-merge-conclude: commit open reconcile merge"
    )
    commit = run_with_lock_retry(lambda: _commit_merge(repo_root, env))
    if commit.returncode != 0:
        _echo(commit)
        return commit.returncode

    rm.remove(repo_root)
    print(f"{rm.OUTCOME_CONCLUDED} branch={manifest.branch}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
