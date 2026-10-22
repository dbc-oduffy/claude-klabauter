"""Find a family's DONE dispatch reports and classify each chunk no-op or land.

Reads report files and ONE scoped `git status`; writes nothing and never re-runs a chunk.
Trap: dispatch-report dirs are keyed by the minted part-spine stem
(`<family>[-tN][-pM].spine`), not by the source plan stem.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Callable, Dict, Iterable, Mapping, NamedTuple, Optional, Sequence

DONE_NOOP = "no-op"
DONE_LAND = "land"

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
            mid, f"{_DISPATCH_REPORT_DIR}/{name}/{mid}.md", name[: -len(".spine")]
        )
    return out


def landing_route(entry: Mapping, inventory_dir_rel: str) -> str:
    """One-line landing instruction for a `land` entry; `""` for a no-op entry."""
    if entry.get("class") != DONE_LAND:
        return ""
    dirty = ", ".join(entry.get("dirty") or [])
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
        entry = {
            "id": mid, "plan": "", "chunk": "", "class": cls, "report": rep.report_rel,
            "run": rep.run_stem, "dirty": dirty, "reason": reason, "route": "",
            "held_behind": [],
        }
        entry["route"] = landing_route(entry, inventory_dir_rel)
        out[mid] = entry
    return out
