"""Structured per-run withhold list: product paths an EM ruled out of a run's terminal commit.

Invariant: a withhold lives only in a run's record dir (beside ``notes.jsonl``) and expires when
that dir lands with the commit that honoured it. Stdlib only; spawns no process.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Iterable

from coordinator_core.session.declared_writes import declare_write

WITHHOLD_FILE = "withhold.jsonl"
MAX_ENTRIES = 64
REASON_CAP = 500
_RECORD_PREFIX = "state/warp/"


def validate(raw: object) -> list[dict]:
    """Normalise ``[{path, reason}]``; raise ``ValueError`` naming the first bad entry."""
    from coordinator_core.ops.dispatch_emit.run_artifacts import _norm_rel

    if not isinstance(raw, (list, tuple)):
        raise ValueError("withhold_paths must be a list of {path, reason}")
    if len(raw) > MAX_ENTRIES:
        raise ValueError(f"withhold_paths holds {len(raw)} entries; the cap is {MAX_ENTRIES}")
    out: list[dict] = []
    for i, entry in enumerate(raw):
        if not isinstance(entry, dict):
            raise ValueError(f"withhold_paths[{i}] is not an object with path and reason")
        path = _norm_rel(entry.get("path"))
        if path is None:
            raise ValueError(f"withhold_paths[{i}].path {entry.get('path')!r} is not a clean repo-relative path")
        if path.startswith(_RECORD_PREFIX):
            raise ValueError(f"withhold_paths[{i}].path {path!r} is under {_RECORD_PREFIX}; the record dir lands with the run")
        reason = entry.get("reason")
        reason = reason.strip() if isinstance(reason, str) else ""
        if not reason:
            raise ValueError(f"withhold_paths[{i}].reason is empty")
        out.append({"path": path, "reason": reason[:REASON_CAP]})
    return out


def record(dir_: Path, entries: list[dict], at: str) -> Path:
    """Append one ``{path, reason, at}`` JSON line per entry to ``dir_/withhold.jsonl``."""
    from coordinator_core.atomic_append import append_line

    sink = Path(dir_) / WITHHOLD_FILE
    sink.parent.mkdir(parents=True, exist_ok=True)
    for e in entries:
        line = json.dumps({"path": e["path"], "reason": e["reason"], "at": at}, ensure_ascii=False)
        append_line(sink, (line + "\n").encode("utf-8"))
    declare_write(sink)
    return sink


def read(record_dirs: Iterable[Path]) -> dict[str, str]:
    """``{path: reason}`` across the dirs; later line wins per path; never raises."""
    merged: dict[str, str] = {}
    for d in record_dirs:
        try:
            text = (Path(d) / WITHHOLD_FILE).read_text(encoding="utf-8")
        except (OSError, ValueError):
            continue
        for line in text.splitlines():
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            if not isinstance(obj, dict):
                continue
            path, reason = obj.get("path"), obj.get("reason")
            if isinstance(path, str) and path and isinstance(reason, str) and reason:
                merged[path] = reason
    return merged
