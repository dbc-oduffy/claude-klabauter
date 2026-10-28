"""Regrade premise rows that cite a path the refired run itself withheld, kept or landed.

A run's own edits (unlanded, kept or landed) sit in the working tree, so a premise-checker reads them as
drift against the plan's base-sha citations. The run's own work is provenance, never evidence
against its plan: such a CONTRADICTED row is regraded RESOLVES with ``own_run_edit: <run_id>``.
Stdlib plus the halt index; no subprocess, no git.
"""

from __future__ import annotations

import os
import re
from pathlib import Path, PurePosixPath
from typing import List, Optional

from coordinator_core.ops._path_guard import safe_id
from coordinator_core.ops.dispatch_emit import halted_withheld_index as _index
from coordinator_core.ops.dispatch_emit import kept_work
from coordinator_core.ops.dispatch_emit.ask_contract import RECORD_DIR_ROOT
from coordinator_core.ops.dispatch_emit.halted_run import halted_ask_run_for

_LEADING_PATH = re.compile(r"[`'\"(\s]*([^\s:`'\")]+)")


_RUN_STATUSES = frozenset({"halted", "resumed", "landed"})


def _newest_run_for(root: Path, sizing_rel: str) -> Optional[str]:
    """Newest ``ask-<stamp>`` run whose marker names ``sizing_rel`` and is halted, resumed or landed
    (the refire's predecessor, whatever its fate); None when none."""
    from coordinator_core.ops.dispatch_emit.halted_run import _norm, _read

    norm = _norm(sizing_rel)
    dated = re.match(r"(\d{4})-(\d{2})-(\d{2})", PurePosixPath(norm).stem)
    if dated is None:
        return None
    floor = "".join(dated.groups())
    warp = root / RECORD_DIR_ROOT
    try:
        names = sorted((n for n in os.listdir(warp) if n.startswith("ask-") and n[4:12] >= floor), reverse=True)
    except OSError:
        return None
    for name in names:
        if not safe_id(name):
            continue
        marker = _read(warp / name)
        if marker and marker.get("status") in _RUN_STATUSES and marker.get("sizing_rel") == norm:
            return name
    return None


def _kept_paths(record: Path) -> list:
    """Paths the run's ``kept.json`` records: every kept chunk's paths plus its post-checkpoint hunks."""
    kept = kept_work.read(record)
    out = list(kept[kept_work.POST_CHECKPOINT_KEY])
    for entry in kept["chunks"].values():
        paths = entry.get("paths") if isinstance(entry, dict) else None
        if isinstance(paths, dict):
            out += [p for p in paths if isinstance(p, str)]
    return [_index._norm(p) for p in out if p]


def own_run_paths(repo_root: Path, sizing_rel: str) -> dict:
    """``{path: run_id}`` for the paths the newest halted, resumed or landed ask run of
    ``sizing_rel`` wrote: its withheld set plus every path its ``kept.json`` records. ``{}`` when no
    such run exists. Reads that run's own records, so a path another run also holds still belongs
    to this run."""
    if not isinstance(sizing_rel, str) or not sizing_rel.strip():
        return {}
    root = Path(repo_root)
    run_id = _newest_run_for(root, sizing_rel.strip())
    if run_id is None:
        return {}
    paths: set = set()
    if halted_ask_run_for(root, sizing_rel.strip()) == run_id:
        if not (_index._dir(root) / _index.SENTINEL).exists():
            _index.rebuild(root)
        file = _index._dir(root) / f"{run_id}.json"
        if os.path.isfile(file):
            paths.update(_index._paths_of(file))
    paths.update(_kept_paths(root / RECORD_DIR_ROOT / run_id))
    return {path: run_id for path in paths}


def _cited_path(citation: str) -> str:
    match = _LEADING_PATH.match(citation)
    return _index._norm(match.group(1)) if match else ""


def regrade(rows: List[dict], own: dict) -> List[dict]:
    """Copy of ``rows`` with each CONTRADICTED row citing a path in ``own`` regraded RESOLVES and
    stamped ``own_run_edit``; every other row passes through unchanged."""
    if not own:
        return list(rows)
    out = []
    for row in rows:
        citation = row.get("citation") if isinstance(row, dict) else None
        run_id = own.get(_cited_path(citation)) if isinstance(citation, str) else None
        if run_id is not None and row.get("verdict") == "CONTRADICTED":
            row = {**row, "verdict": "RESOLVES", "own_run_edit": run_id}
        out.append(row)
    return out
