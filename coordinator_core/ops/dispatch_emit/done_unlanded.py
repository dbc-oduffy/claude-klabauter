"""Find a family's DONE dispatch reports and classify each chunk no-op or land.

Reads report files and ONE scoped `git status`; writes nothing and never re-runs a chunk.
Trap: dispatch-report dirs are keyed by the minted part-spine stem
(`<family>[-tN][-pM].spine`), not by the source plan stem.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path
from typing import Callable, Dict, Iterable, Mapping, NamedTuple, Optional, Sequence

DONE_NOOP = "no-op"
DONE_LAND = "land"
#: Entry key on a `land` entry whose dirty paths were committed after the DONE report: the
#: dispatched bytes are at HEAD (a hand-land), so the route is review-only, not land_run.
HAND_LANDED = "hand_landed"

_DELIVERED_REPORT_RE = re.compile(
    r'^\s*"?[*_`]{0,2}DONE(?:_WITH_CONCERNS)?[*_`]{0,2}:|<exit-status>DONE</exit-status>'
    r'|(?i:^[ \t>#-]*"?[*_`]{0,2}status[*_`]{0,2}[ \t]*:[ \t]*[*_`]{0,2}[ \t]*DONE(?:_WITH_CONCERNS)?\b)',
    re.MULTILINE,
)
_UNDELIVERED_REPORT_RE = re.compile(
    r'^\s*"?[*_`]{0,2}(?:PARTIAL|BLOCKED)[*_`]{0,2}:|<exit-status>(?:PARTIAL|BLOCKED)</exit-status>'
    r'|(?i:^[ \t>#-]*"?[*_`]{0,2}status[*_`]{0,2}[ \t]*:[ \t]*[*_`]{0,2}[ \t]*(?:PARTIAL|BLOCKED)\b)',
    re.MULTILINE,
)


class DoneReport(NamedTuple):
    minted_id: str
    report_rel: str
    run_stem: str
    mtime: float = 0.0


def _reads_delivered(text: str) -> bool:
    return bool(_DELIVERED_REPORT_RE.search(text)) and not _UNDELIVERED_REPORT_RE.search(text)


def family_done_reports(
    repo_root: Path, family_base: str, minted_ids: Iterable[str]
) -> Dict[str, DoneReport]:
    """Map each minted id to its newest family report, only when that report reads delivered.

    Matches report dirs `<family_base>[-tN][-pM].spine`; a missing dir, an unreadable
    report and another family's dir yield nothing. Spawns nothing.
    """
    from coordinator_core.ops.dispatch_emit.emit import _DISPATCH_REPORT_DIR

    wanted = list(dict.fromkeys(minted_ids))
    if not wanted:
        return {}
    root = Path(repo_root)
    base_dir = root / _DISPATCH_REPORT_DIR
    pat = re.compile(rf"^{re.escape(family_base)}(-t\d+)?(-p\d+)?\.spine$")
    try:
        with os.scandir(base_dir) as it:
            dirs = sorted(e.name for e in it if pat.match(e.name) and e.is_dir())
    except OSError:
        return {}
    best: Dict[str, tuple] = {}
    for name in dirs:
        try:
            with os.scandir(base_dir / name) as it:
                files = {e.name: e for e in it if e.is_file()}
        except OSError:
            continue
        for mid in wanted:
            entry = files.get(f"{mid}.md")
            if entry is None:
                continue
            try:
                mtime = entry.stat().st_mtime
            except OSError:
                continue
            if mid not in best or mtime > best[mid][0]:
                best[mid] = (mtime, name, entry.path)
    out: Dict[str, DoneReport] = {}
    for mid, (_mtime, name, path) in best.items():
        try:
            text = Path(path).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if not _reads_delivered(text):
            continue
        out[mid] = DoneReport(
            mid, f"{_DISPATCH_REPORT_DIR}/{name}/{mid}.md", name[: -len(".spine")], _mtime
        )
    return out


def landing_route(entry: Mapping, inventory_dir_rel: str) -> str:
    """One-line landing instruction for a `land` entry; `""` for a no-op entry."""
    if entry.get("class") != DONE_LAND:
        return ""
    dirty = ", ".join(entry.get("dirty") or [])
    if entry.get(HAND_LANDED):
        return (
            f"already committed (hand-land): emit-dispatch-workflow --inventory <record> "
            f"--review-only --rows {entry['id']} --run-base <sha> (live diff, not this "
            f"dispatch's output: {dirty})"
        )
    return (
        f"land it: dispatch.land_run script_path={inventory_dir_rel}/{entry['run']}.spine.workflow.mjs "
        f"task_output_path=<that run's task output> incomplete_chunks=[] (uncommitted: {dirty}); "
        f"once those paths are committed, emit-dispatch-workflow --inventory <record> "
        f"--review-only --rows {entry['id']} --run-base <sha> reviews them instead"
    )


def classify_done(
    repo_root: Path,
    done: Mapping[str, DoneReport],
    declared: Mapping[str, Sequence[str]],
    inventory_dir_rel: str,
    *,
    run: Optional[Callable] = None,
) -> Dict[str, dict]:
    """Classify each DONE-unlanded chunk `no-op` or `land` with one batched dirty check.

    `no-op`: no declared path dirty and every declared path exists. `land`: a dirty path,
    an absent declared write, or a failed git check (fail closed, every entry).
    """
    if not done:
        return {}
    from coordinator_core.ops.dispatch_emit.dirty_write_set import (
        _default_ignorecase,
        _dirty_in_write_set,
        _norm,
    )

    root = Path(repo_root)
    paths_of = {mid: [p for p in (_norm(x) for x in declared.get(mid, ())) if p] for mid in done}
    union = sorted({p for ps in paths_of.values() for p in ps})
    dirty_all = _dirty_in_write_set(union, root, run=run)
    fold = (lambda s: s.lower()) if _default_ignorecase() else (lambda s: s)

    out: Dict[str, dict] = {}
    for mid, rep in done.items():
        dirty: list = []
        if dirty_all is None:
            cls, reason = DONE_LAND, "git status failed; fail closed"
        else:
            prefixes = [fold(p) for p in paths_of[mid]]
            dirty = sorted(
                d for d in dirty_all
                if any(fold(d) == p or fold(d).startswith(p + "/") for p in prefixes)
            )
            if dirty:
                cls, reason = DONE_LAND, "declared write dirty"
            elif any(not (root / p).exists() for p in paths_of[mid]):
                cls, reason = DONE_LAND, "declared write absent"
            else:
                cls, reason = DONE_NOOP, "executor report DONE, no declared write dirty"
        out[mid] = {
            "id": mid, "plan": "", "chunk": "", "class": cls, "report": rep.report_rel,
            "run": rep.run_stem, "dirty": dirty, "reason": reason, "route": "",
            "held_behind": [], HAND_LANDED: False,
        }
    candidates = {
        mid: e["dirty"] for mid, e in out.items()
        if e["reason"] == "declared write dirty" and done[mid].mtime > 0
    }
    if candidates:
        newest = _newest_commit_times(root, sorted({d for ds in candidates.values() for d in ds}),
                                      int(min(done[m].mtime for m in candidates)), run)
        for mid, dirty in candidates.items():
            floor = int(done[mid].mtime)
            if newest is not None and all(newest.get(fold(_norm(d)), -1) >= floor for d in dirty):
                out[mid][HAND_LANDED] = True
                out[mid]["reason"] = "declared write committed after the DONE report; live diff is later"
    for entry in out.values():
        entry["route"] = landing_route(entry, inventory_dir_rel)
    return out


def _newest_commit_times(
    root: Path, paths: Sequence[str], since: int, run: Optional[Callable]
) -> Optional[Dict[str, int]]:
    """`{folded path: newest committer time}` for `paths` committed since `since`, one `git log`.

    A path with no commit in the window is absent; `None` when git failed (callers fail closed).
    """
    from coordinator_core.git.literal_pathspec import git_pathspec
    from coordinator_core.git.run import run_git
    from coordinator_core.ops.dispatch_emit.dirty_write_set import _default_ignorecase, _norm

    icase = _default_ignorecase()
    fold = (lambda s: s.lower()) if icase else (lambda s: s)
    specs = [f":(icase,literal){_norm(p)}" if icase else git_pathspec(_norm(p)) for p in paths]
    argv = [
        "-C", str(root), "--no-optional-locks", "-c", "core.quotepath=off", "log",
        f"--since={max(since - 1, 0)}", "--no-renames", "--name-only", "--format=%x01%ct",
        "--", *specs,
    ]
    try:
        proc = (run or run_git)(argv, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    newest: Dict[str, int] = {}
    stamp = 0
    for line in (proc.stdout or "").splitlines():
        if line.startswith("\x01"):
            try:
                stamp = int(line[1:])
            except ValueError:
                stamp = 0
        elif line.strip():
            key = fold(_norm(line.strip()))
            newest[key] = max(newest.get(key, 0), stamp)
    return newest
