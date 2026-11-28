"""coordinator_core.ops.false_finished_sweep -- op "run.false_finished_sweep".

For each named repo root, lists the runs recorded committed whose sizing still has register rows
`requirement_register.open_register_rows` names: a run that closed green with work owed.
"Committed" is the run-outcome audit's terminal word (synthesis section 2): `halted.json` status
`landed`, else `run-stage.json` stage `landed` when no `halted.json` exists.

Negative-spec:
    - COMPUTE_ONLY: reads `state/warp/*` and sizings; writes, sends and spawns nothing.
    - Opens `run.json` and a sizing only for a committed run, and a sizing at most once per repo.
    - A sizing that does not resolve or parse is counted in `unresolved`, never listed as clean.
    - A missing `repo_roots` is a typed refusal naming the param.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Optional

from coordinator_core.ipc import CallerFacingValidationError, register_op

OP_KEY = "run.false_finished_sweep"
_WARP = ("state", "warp")
_LANDED = "landed"

_SCHEMA = {
    "type": "object",
    "properties": {
        "repo_roots": {
            "type": "array",
            "items": {"type": "string", "minLength": 1},
            "minItems": 1,
            "description": "Absolute repo roots whose state/warp runs are swept.",
        },
    },
    "required": ["repo_roots"],
    "additionalProperties": False,
}


def _json(path: str) -> Optional[dict]:
    """The JSON object at `path`; None when absent, unreadable or not an object."""
    data = _read(path)
    try:
        doc = json.loads(data) if data is not None else None
    except ValueError:
        return None
    return doc if isinstance(doc, dict) else None


def _read(path: str) -> Optional[bytes]:
    """The file's bytes via a raw descriptor (no buffered-IO wrapper: this runs once per run dir)."""
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_BINARY", 0))
    except OSError:
        return None
    try:
        chunks = []
        while chunk := os.read(fd, 1 << 16):
            chunks.append(chunk)
        return b"".join(chunks)
    except OSError:
        return None
    finally:
        os.close(fd)


def _committed(run_dir: str) -> bool:
    """True when the run's terminal record says landed (halted.json first, else run-stage.json)."""
    halted = _json(os.path.join(run_dir, "halted.json"))
    if halted is not None:
        return halted.get("status") == _LANDED
    stage = _json(os.path.join(run_dir, "run-stage.json"))
    return stage is not None and stage.get("stage") == _LANDED


def _sizing_bytes(root: Path, sizing_rel: str) -> Optional[bytes]:
    """The sizing's bytes: the literal repo-relative path first (one open), else the archive
    resolution. A path that is absolute or climbs out of the repo is never read."""
    parts = sizing_rel.replace("\\", "/").split("/")
    if os.path.isabs(sizing_rel) or ".." in parts:
        return None
    data = _read(os.path.join(root, *parts))
    if data is not None:
        return data
    from coordinator_core.ops._sizing_citation import resolve_sizing_citation

    path = resolve_sizing_citation(root, sizing_rel)
    try:
        return path.read_bytes() if path is not None else None
    except OSError:
        return None


def _owed(root: Path, sizing_rel: str, cache: dict) -> Optional[list]:
    """The register rows of `sizing_rel` that `requirement_register.open_register_rows` names,
    memoised per repo; None when the sizing does not resolve (live, then archive).

    Reads the register block alone (`read_register`) with the same `_row_block` predicate, so the
    verdict equals `open_register_rows` without its whole-document load and engine imports."""
    if sizing_rel in cache:
        return cache[sizing_rel]
    from coordinator_core.ops import requirement_register as rr

    owed: Optional[list] = None
    data = _sizing_bytes(root, sizing_rel)
    if data is not None:
        owed = []
        register = rr.read_register(data.decode("utf-8", "replace")) if rr.has_register_key(data) else None
        for row in register.rows if register else ():
            reason = rr._row_block(row)
            if reason:
                owed.append({"id": row.get(rr.ROW_ID), "status": row.get(rr.ROW_STATUS), "reason": reason})
    cache[sizing_rel] = owed
    return owed


def sweep_repo(root: Path) -> dict[str, Any]:
    """`{runs_scanned, committed, false_finished: [{run_id, sizing, open_rows}], unresolved}`."""
    warp = os.path.join(root, *_WARP)
    out: dict[str, Any] = {"runs_scanned": 0, "committed": 0, "false_finished": [], "unresolved": 0}
    try:
        entries = sorted(e.name for e in os.scandir(warp) if e.is_dir() and not e.name.startswith("."))
    except OSError:
        return {**out, "error": f"no readable {'/'.join(_WARP)} under {root}"}
    cache: dict = {}
    for run_id in entries:
        run_dir = os.path.join(warp, run_id)
        out["runs_scanned"] += 1
        if not _committed(run_dir):
            continue
        out["committed"] += 1
        run = _json(os.path.join(run_dir, "run.json"))
        sizing = run.get("sizing_rel") if run else None
        if not isinstance(sizing, str) or not sizing:
            continue
        owed = _owed(root, sizing, cache)
        if owed is None:
            out["unresolved"] += 1
        elif owed:
            out["false_finished"].append({"run_id": run_id, "sizing": sizing, "open_rows": owed})
    return out


@register_op(OP_KEY, params_schema=_SCHEMA)
def _handler(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC "run.false_finished_sweep": params `repo_roots` (required list of absolute paths).

    Returns {"repos": {<root>: {runs_scanned, committed, false_finished, unresolved}}, "total"};
    each `false_finished` entry is {run_id, sizing, open_rows}.
    """
    roots = params.get("repo_roots")
    if (
        not isinstance(roots, list)
        or not roots
        or not all(isinstance(r, str) and r.strip() and os.path.isabs(r) for r in roots)
    ):
        raise CallerFacingValidationError(
            "run.false_finished_sweep: repo_roots must be a non-empty list of absolute repo paths"
        )
    repos = {r: sweep_repo(Path(r)) for r in roots}
    return {"repos": repos, "total": sum(len(v["false_finished"]) for v in repos.values())}
