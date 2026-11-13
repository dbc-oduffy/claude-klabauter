"""coordinator_core.install.authoring_checkout_freshness_apply — per-checkout lag probe, overlap,
merge-tree advisory, optional fetch and the ff-only act for authoring checkouts.

Builds on the pure names in `authoring_checkout_freshness`. The only tree write is
`merge --ff-only` of the checked-out branch; every unsafe state is a row and an advisory,
never an exception. Git runs through `klabauter_freshness_apply._git`.
"""

from __future__ import annotations

import socket
from pathlib import Path
from typing import Mapping, Optional

from coordinator_core.install import authoring_checkout_freshness as acf
from coordinator_core.install.klabauter_freshness_apply import _git

MAIN_REF = "refs/remotes/origin/main"
ACTION_UNREADABLE = "unreadable"

#: Stands in for a run id when the live-run registry cannot be read, so the tree defers.
UNREADABLE_RUNS_SENTINEL = "unreadable-run-registry"

MERGE_REMEDIATION = "git merge origin/main"


def _line(text: str, limit: int = 200) -> str:
    return " ".join(text.split())[:limit]


def _status(root: Path) -> Optional[tuple[str, str, tuple[str, ...]]]:
    """`(branch, head_sha, dirty tracked paths)` from one `status`; `None` when unreadable.

    `branch` is "" for a detached HEAD. Untracked files are not dirt (`-uno`).
    """
    try:
        out = _git(root, "status", "--porcelain=v2", "--branch", "-uno", "-z")
    except OSError:
        return None
    if out.returncode != 0:
        return None
    sha = branch = ""
    dirty: list[str] = []
    entries = out.stdout.split("\0")
    skip_orig = False
    for entry in entries:
        if skip_orig:
            skip_orig = False
            if entry:
                dirty.append(entry)
            continue
        if entry.startswith("# branch.oid "):
            sha = entry.split(" ", 2)[2]
        elif entry.startswith("# branch.head "):
            head = entry.split(" ", 2)[2]
            branch = "" if head == "(detached)" else head
        elif entry.startswith("1 "):
            dirty.append(entry.split(" ", 8)[8])
        elif entry.startswith("2 "):
            dirty.append(entry.split(" ", 9)[9])
            skip_orig = True
        elif entry.startswith("u "):
            dirty.append(entry.split(" ", 10)[10])
    if not sha or sha == "(initial)":
        return None
    return branch, sha, tuple(dirty)


def _lag(root: Path) -> Optional[tuple[int, int]]:
    """`(ahead, behind)` of HEAD against `MAIN_REF`; `None` when the ref is absent or unreadable."""
    try:
        out = _git(root, "rev-list", "--left-right", "--count", f"HEAD...{MAIN_REF}")
    except OSError:
        return None
    if out.returncode != 0:
        return None
    parts = out.stdout.split()
    if len(parts) != 2:
        return None
    return int(parts[0]), int(parts[1])


def _overlap(root: Path, dirty: tuple[str, ...]) -> tuple[str, ...]:
    """Dirty tracked paths that the `HEAD..MAIN_REF` update also changes.

    An unreadable diff reads as every dirty path overlapping: the safe side.
    """
    try:
        out = _git(root, "diff", "--name-only", "-z", "HEAD", MAIN_REF)
    except OSError:
        return dirty
    if out.returncode != 0:
        return dirty
    touched = {p for p in out.stdout.split("\0") if p}
    return tuple(sorted(set(dirty) & touched))


def _live_run_ids(key: str, root: Path) -> tuple[str, ...]:
    from coordinator_core.warm import live_runs

    try:
        return tuple(run.run_id for run in live_runs.find_live_runs(repos={key: str(root)}))
    except Exception:
        return (UNREADABLE_RUNS_SENTINEL,)


def _merge_probe(root: Path) -> tuple[Optional[bool], list[str], str]:
    """`(merge_clean, conflict paths, stderr line)` from `merge-tree --write-tree` (git >= 2.38).

    Exit 0 is clean, exit 1 a conflict; any other exit (older git, unrelated histories)
    is `None` with the stderr line. Writes objects only, never the work tree.
    """
    try:
        out = _git(root, "merge-tree", "--write-tree", "--name-only", "HEAD", MAIN_REF)
    except OSError as exc:
        return None, [], _line(str(exc))
    if out.returncode == 0:
        return True, [], ""
    if out.returncode != 1:
        return None, [], _line(out.stderr or out.stdout)
    paths: list[str] = []
    for line in out.stdout.splitlines()[1:]:
        if not line:
            break
        paths.append(line)
    return False, paths, ""


def fetch(root: Path) -> str:
    """`git fetch origin main` over the remote's own URL; `""` on success, an error line otherwise.

    A remote round trip: bounded by `run_git`'s remote lane, never retried, and run only
    when the caller asks (`do_fetch`).
    """
    try:
        out = _git(root, "fetch", "--quiet", "origin", "main", remote=True)
    except OSError as exc:
        return f"fetch failed: {_line(str(exc))}"
    return "" if out.returncode == 0 else f"fetch failed: {_line(out.stderr)}"


def apply_checkout(key: str, root: str, *, do_fetch: bool = False) -> dict:
    """Probe one checkout, decide, and fast-forward when safe. Never raises for an unsafe tree."""
    path = Path(root)
    row: dict = {
        "key": key,
        "root": str(root),
        "branch": "",
        "head_sha": "",
        "behind": 0,
        "ahead": 0,
        "action": ACTION_UNREADABLE,
        "reason": "not a readable git checkout",
        "moved": False,
    }
    if do_fetch:
        error = fetch(path)
        if error:
            row["fetch_error"] = error
    status = _status(path)
    if status is None:
        return row
    branch, head_sha, dirty = status
    row.update(branch=branch, head_sha=head_sha)
    counts = _lag(path) if branch else None
    ahead, behind = counts if counts is not None else (0, 0)
    lag = acf.CheckoutLag(
        key, str(root), branch, head_sha, MAIN_REF if counts is not None else "", ahead, behind
    )
    row.update(ahead=ahead, behind=behind)

    candidate = bool(branch) and counts is not None and behind > 0 and ahead == 0
    live = _live_run_ids(key, path) if candidate else ()
    overlap = _overlap(path, dirty) if candidate and dirty and not live else ()
    verdict = acf.decide(lag, overlap, live)
    row.update(action=verdict.action, reason=verdict.reason)

    if verdict.action == acf.ACTION_DIVERGED:
        clean, conflicts, error = _merge_probe(path)
        row.update(merge_clean=clean, conflict_paths=conflicts, remediation=MERGE_REMEDIATION)
        if clean is False:
            row["reason"] += f"; merge would conflict in: {', '.join(conflicts[: acf.MAX_NAMED_PATHS])}"
        elif clean is None:
            row["reason"] += f"; merge-tree unavailable: {error}"
    elif verdict.action == acf.ACTION_FAST_FORWARD:
        try:
            merge = _git(path, "merge", "--ff-only", MAIN_REF)
        except OSError as exc:
            row["reason"] = f"ff-only refused: {_line(str(exc))}"
        else:
            if merge.returncode == 0:
                row["moved"] = True
            else:
                row["reason"] = f"ff-only refused: {_line(merge.stderr or merge.stdout)}"
    return row


def apply_all(registry: Mapping[str, str], *, do_fetch: bool = False) -> dict:
    """Freshness rows and advisories for every authoring checkout in `registry`.

    The klabauter mirror is excluded by `authoring_roster`. One probe per checkout is
    irreducible: each is a distinct repository.
    """
    hostname = socket.gethostname()
    rows = [apply_checkout(key, root, do_fetch=do_fetch) for key, root in acf.authoring_roster(registry)]
    advisories: list[str] = []
    for row in rows:
        line = acf.advisory_line(hostname, row)
        if line is None:
            continue
        if row.get("remediation"):
            line += f"; run: {row['remediation']}"
        advisories.append(line)
    return {"hostname": hostname, "rows": rows, "advisories": advisories}
