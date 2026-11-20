"""
coordinator_core.ops.runs_withheld_overlap -- JSON-RPC "runs.withheld_overlap".

Purpose: before a fire, an EM asks which of a path set another run already holds. Names, per path,
the other live or halted run that withholds it or keeps a blob of it, and whether the path is dirty
in the working tree with no run attributed to it. Another run's dirt riding into this run's commit is
the cross-run sweep (defect 33); this op lets the caller see the overlap first.

Registered in `coordinator_core/ops/__init__.py`'s registration list, `_registry_map.py`'s
`OP_MODULE_MAP`, `op_scopes.py` (scope "show_top": it reads the worktree's `state/warp` and status) and `authz/classification.py` (COMPUTE_ONLY).
Enrolled in `_BUDGETED_ENTRYPOINTS` of the spawn audit; its one spawn is the pathspec-limited
`git status --porcelain=v1 -z` (spawn budget: 1, none when no path is given).

Negative-spec:
    - Never raises: an invalid param or I/O failure is `{"error": ...}`.
    - Spawns at most ONE git process; never scans `state/warp/*` (the halted index and live run-token
      records name the candidate runs; each candidate's `kept.json` is one cached read).
    - Writes nothing and judges nothing: a `kept` row is a recorded blob, not proof the file is
      still dirty; the caller decides.
    - The named `run_id` is never its own holder.
"""

from __future__ import annotations

import json
from pathlib import Path, PurePosixPath
from typing import Iterable, Optional

from coordinator_core.ipc import register_op

STATE_WITHHELD = "withheld"
STATE_KEPT = "kept"
STATE_DIRTY = "dirty"
#: A halted run's declared footprint that its halt record never withheld (the halt-commit failed or was malformed).
STATE_DECLARED = "declared"
UNATTRIBUTED = "unattributed"


def _norm(p: object) -> Optional[str]:
    if not isinstance(p, str) or not p.strip():
        return None
    posix = PurePosixPath(p.replace("\\", "/")).as_posix()
    return posix[2:] if posix.startswith("./") else posix


def _declared_of_run(root: Path, run_id: str) -> list:
    """The run's declared writes: its manifest rows' `writes` and `review_declared_paths`."""
    from coordinator_core.ops.dispatch_emit import ask_contract

    try:
        manifest = json.loads((ask_contract.record_dir(root, run_id) / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if not isinstance(manifest, dict):
        return []
    out: list = []
    for row in manifest.get("rows") or ():
        if isinstance(row, dict) and isinstance(row.get("writes"), list):
            out.extend(row["writes"])
    if isinstance(manifest.get("review_declared_paths"), list):
        out.extend(manifest["review_declared_paths"])
    return out


def _holders(root: Path, wanted: set, own: Optional[str]) -> list:
    """`[(path, run_id, state)]` from the halted index and the live run-token records. No spawn."""
    from coordinator_core.ops.dispatch_emit import (
        ask_contract,
        halted_hold_release,
        halted_withheld_index,
        kept_work,
        run_token,
    )

    held: dict = {}
    declared: dict = {}
    try:
        for run_id, entry in halted_withheld_index.entries(root).items():
            # kept paths are a subset of the declared footprint: a run naming no wanted path holds none
            if not wanted.isdisjoint(entry.paths) or not wanted.isdisjoint(entry.declared):  # index paths are normalised
                held[run_id] = set(entry.paths)
                declared[run_id] = set(entry.declared)
    except OSError:
        pass
    try:
        for rec in run_token.held_records(root):
            held.setdefault(rec.get("run_id"), set())
    except OSError:
        pass
    out = []
    with halted_hold_release.release_scope():
        for run_id, withheld in held.items():
            if not isinstance(run_id, str) or run_id == own:
                continue
            try:  # a superseded or stale-released halted run holds nothing
                if (
                    halted_hold_release.supersession_reason(root, run_id) is not None
                    or halted_hold_release.stale_release(root, run_id) is not None
                ):
                    continue
            except Exception:  # noqa: BLE001 -- release unknown: the run still holds
                pass
            for path in sorted(wanted.intersection(withheld)):
                out.append((path, run_id, STATE_WITHHELD))
            # A halted run keeps its declared paths even when its halt-commit failed and withheld none of them:
            # without the row the owner is hidden and the dirty path reads unattributed.
            for path in sorted((wanted & declared.get(run_id, set())) - withheld):
                out.append((path, run_id, STATE_DECLARED))
            try:
                kept = kept_work.kept_fingerprints(ask_contract.record_dir(root, run_id))
            except (OSError, ValueError):
                continue
            for path in sorted(wanted & set(kept)):
                out.append((path, run_id, STATE_KEPT))
    return out


def _dirty(root: Path, paths: Iterable[str]) -> set:
    """Paths among `paths` that `git status` lists, from ONE pathspec-limited call. Empty on failure."""
    from coordinator_core.git.run import run_git

    specs = sorted(paths)
    if not specs:
        return set()
    res = run_git(
        ["-C", str(root), "--no-optional-locks", "status", "--porcelain=v1", "-z", "--untracked-files=all", "--"]
        + [f":(literal){p}" for p in specs],
        binary=True,
    )
    if getattr(res, "returncode", 1) != 0:
        return set()
    stdout = res.stdout or b""
    parts = (stdout if isinstance(stdout, bytes) else stdout.encode()).split(b"\0")
    found: set = set()
    i = 0
    while i < len(parts):
        rec = parts[i].decode("utf-8", "replace")
        i += 1
        if len(rec) < 4:
            continue
        found.add(rec[3:])
        if rec[0] in "RC":  # a rename/copy record carries its source as the next field
            i += 1
    return found


@register_op("runs.withheld_overlap")
def _runs_withheld_overlap(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC "runs.withheld_overlap" handler.

    Params:
        repo (str) -- absolute repo root; defaults to the dispatch repo_root.
        paths (list[str]) -- repo-relative paths to check; or
        run_id (str) -- a run whose declared writes (manifest rows) are the paths; that run is
            never reported as its own holder.

    Returns:
        {"overlaps": [{"path", "holder", "state"}], "clean": bool}; `holder` is a run id or
        "unattributed" (dirty, no other run holds the path); `state` is "withheld", "declared" (a halted
        run's declared path its halt never withheld), "kept" or "dirty". Or {"error": str} on an invalid param.
    """
    p = params if isinstance(params, dict) else {}
    repo = p.get("repo") or (str(repo_root) if repo_root is not None else None)
    if not isinstance(repo, str) or not repo or not Path(repo).is_absolute():
        return {"error": "repo (absolute path str) is required"}
    run_id = p.get("run_id")
    raw = p.get("paths")
    if raw is None and not isinstance(run_id, str):
        return {"error": "paths (list) or run_id (str) is required"}
    root = Path(repo)
    try:
        if raw is None and isinstance(run_id, str):
            raw = _declared_of_run(root, run_id)
        elif not isinstance(raw, list):
            return {"error": "paths must be a list of str"}
        wanted = {k for k in (_norm(x) for x in raw) if k}
        own = run_id if isinstance(run_id, str) else None
        rows = [
            {"path": path, "holder": holder, "state": state} for path, holder, state in _holders(root, wanted, own)
        ]
        held_paths = {r["path"] for r in rows}
        for path in sorted(_dirty(root, wanted) - held_paths):
            rows.append({"path": path, "holder": UNATTRIBUTED, "state": STATE_DIRTY})
    except Exception as exc:  # noqa: BLE001 -- degrade-never-raise
        return {"error": f"{type(exc).__name__}: {exc}"}
    rows.sort(key=lambda r: (r["path"], r["holder"], r["state"]))
    return {"overlaps": rows, "clean": not rows}
