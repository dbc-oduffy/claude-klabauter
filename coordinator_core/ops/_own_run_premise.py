"""Regrade premise rows that cite a path the refired run itself withholds.

A halted run's unlanded edits still sit in the working tree, so a premise-checker reads them as
drift against the plan's base-sha citations. The run's own work is provenance, never evidence
against its plan: such a CONTRADICTED row is regraded RESOLVES with ``own_run_edit: <run_id>``.
Stdlib plus the halt index; no subprocess, no git.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import List

from coordinator_core.ops.dispatch_emit import halted_withheld_index as _index
from coordinator_core.ops.dispatch_emit.halted_run import halted_ask_run_for

_LEADING_PATH = re.compile(r"[`'\"(\s]*([^\s:`'\")]+)")


def own_run_paths(repo_root: Path, sizing_rel: str) -> dict:
    """``{path: run_id}`` for the paths the halted ask run of ``sizing_rel`` withholds; ``{}`` when
    no run is halted for it. Reads that run's own index file, so a path another halted run also
    withholds still belongs to this run."""
    if not isinstance(sizing_rel, str) or not sizing_rel.strip():
        return {}
    root = Path(repo_root)
    run_id = halted_ask_run_for(root, sizing_rel.strip())
    if run_id is None:
        return {}
    if not (_index._dir(root) / _index.SENTINEL).exists():
        _index.rebuild(root)
    file = _index._dir(root) / f"{run_id}.json"
    if not os.path.isfile(file):
        return {}
    return {path: run_id for path in _index._paths_of(file)}


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
