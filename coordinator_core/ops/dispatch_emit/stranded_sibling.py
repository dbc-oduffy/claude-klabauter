"""Name the recorded sibling of a landing that a halted run would leave stranded.

A halted run's record dir holds `touched-blobs.json`: `{path: {"base": sha | None, "run": sha | None}}`,
the HEAD blob each touched path started from and the blob the run left (None = absent). `stranded`
answers which recorded paths a landing leaves behind; `ownerless` names dirty paths no run manifest
declares. Zero spawn: blob shas come from file bytes, HEAD blobs from the caller's one batched read.
Every reader answers "no claim" (`[]`) on an absent or unreadable record and never raises.
"""

from __future__ import annotations

import json
import os
from pathlib import Path, PurePosixPath
from typing import Iterable, Mapping, Optional

TOUCHED_FILE = "touched-blobs.json"


def _norm(path: object) -> str:
    return str(path).replace("\\", "/").removeprefix("./") if isinstance(path, str) else ""


def write(record_dir: Path, touched: Mapping[str, Mapping[str, Optional[str]]]) -> Optional[Path]:
    """Write `touched` as `<record_dir>/touched-blobs.json`; returns the path, None on OSError."""
    out = Path(record_dir) / TOUCHED_FILE
    doc = {
        _norm(p): {"base": e.get("base"), "run": e.get("run")}
        for p, e in touched.items()
        if _norm(p) and isinstance(e, Mapping)
    }
    try:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(doc, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    except OSError:
        return None
    return out


def read(record_dir: Path) -> dict:
    """The record as `{path: {"base", "run"}}`; `{}` on any error or malformed entry."""
    try:
        data = json.loads((Path(record_dir) / TOUCHED_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    out = {}
    for p, e in data.items():
        if not isinstance(e, dict):
            continue
        base, run = e.get("base"), e.get("run")
        if (base is None or isinstance(base, str)) and (run is None or isinstance(run, str)):
            out[_norm(p)] = {"base": base, "run": run}
    return out


def kind_of(path: str) -> str:
    """"test" for a path under a tests/ dir or named test_*.py, else "module"."""
    pure = PurePosixPath(_norm(path))
    if "tests" in pure.parts[:-1] or pure.name.startswith("test_"):
        return "test"
    return "module"


def _records(record_dir: Path) -> list:
    """`[(run id, record dir, record mtime, record)]`: `record_dir` itself when it holds a record,
    else each child run dir that does (the state/warp parent)."""
    base = Path(record_dir)
    dirs = []
    try:
        if (base / TOUCHED_FILE).is_file():
            dirs = [base]
        else:
            with os.scandir(base) as it:
                dirs = [Path(e.path) for e in it if e.is_dir() and (Path(e.path) / TOUCHED_FILE).is_file()]
    except OSError:
        return []
    found = []
    for d in dirs:
        rec = read(d)
        if not rec:
            continue
        try:
            mtime = (d / TOUCHED_FILE).stat().st_mtime
        except OSError:
            continue
        found.append((d.name, d, mtime, rec))
    return found


def _worktree_blob(root: Path, rel: str) -> Optional[str]:
    from coordinator_core.ops.dispatch_emit.kept_work import fingerprint

    return fingerprint(root, rel)


def stranded(
    root: Path,
    record_dir: Path,
    landing_paths: Iterable[str],
    head_blobs: Mapping[str, Optional[str]],
) -> list:
    """`[{path, run, kind, reason}]` for each recorded path a landing leaves behind.

    A record is judged only when `landing_paths` intersects its paths (shared files, case 7, are judged
    per record, so a hit names another run's sibling by `run`). A recorded path outside the landing is
    stranded when its HEAD blob (`head_blobs`, None = absent) differs from the run blob and the run
    changed it (run != base). When several records name a path, only the newest record judges it.
    `reason`: "unlanded" (worktree bytes are the run blob) or "lost" (they are not)."""
    try:
        landing = {_norm(p) for p in landing_paths}
        landing.discard("")
        records = _records(record_dir)
        newest: dict = {}
        for run_id, _d, mtime, rec in records:
            for p in rec:
                if p not in newest or mtime > newest[p][0]:
                    newest[p] = (mtime, run_id)
        hits = []
        for run_id, _d, _mtime, rec in records:
            if not landing.intersection(rec):
                continue
            for p, entry in sorted(rec.items()):
                if p in landing or newest[p][1] != run_id:
                    continue
                run_blob = entry["run"]
                if run_blob == entry["base"] or head_blobs.get(p) == run_blob:
                    continue
                reason = "unlanded" if _worktree_blob(Path(root), p) == run_blob else "lost"
                hits.append({"path": p, "run": run_id, "kind": kind_of(p), "reason": reason})
        return hits
    except Exception:
        return []


def _declared_paths(doc: Mapping) -> set:
    out = set()
    for key in ("write_baseline", "review_declared_paths", "doc_writes", "review_code_paths"):
        v = doc.get(key)
        if isinstance(v, (dict, list)):
            out.update(_norm(p) for p in v)
    for row in doc.get("rows") or ():
        if isinstance(row, dict):
            out.update(_norm(p) for p in row.get("writes") or () if isinstance(p, str))
    out.discard("")
    return out


def ownerless(root: Path, dirty_paths: Iterable[str]) -> list:
    """The `dirty_paths` that no run manifest under `<root>/state/warp/*/manifest.json` declares
    (write_baseline, row writes, doc writes), sorted; `[]` when none or on any error."""
    try:
        from coordinator_core.ops.dispatch_emit.halted_record_request import _manifest_doc

        dirty = {_norm(p) for p in dirty_paths}
        dirty.discard("")
        if not dirty:
            return []
        declared: set = set()
        with os.scandir(Path(root) / "state" / "warp") as it:
            for e in it:
                if e.is_dir():
                    declared |= _declared_paths(_manifest_doc(Path(e.path)))
        return sorted(dirty - declared)
    except Exception:
        return []
