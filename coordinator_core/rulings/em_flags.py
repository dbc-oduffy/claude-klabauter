"""EM flag queue writer: one JSON line per flag in `.coordinator-local/em-flags/<session_id>.jsonl`.

Pure pathlib/os, no subprocess; a single O_APPEND write keeps concurrent appenders from interleaving.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

FLAGS_DIR = Path(".coordinator-local") / "em-flags"
UNATTRIBUTED = "_unattributed"
_UNSAFE_RE = re.compile(r"[^A-Za-z0-9._-]")


def queue_path(root: Path, session_id: str | None) -> Path:
    """Queue file for `session_id` under `root`; a missing or unusable id maps to `_unattributed`."""
    stem = _UNSAFE_RE.sub("_", str(session_id or "")).strip("._")
    return Path(root) / FLAGS_DIR / f"{stem or UNATTRIBUTED}.jsonl"


def append_flag(root: Path, session_id: str | None, record: dict) -> dict:
    """Append `record` as one JSON line; return `{path, line}` (absolute path, 1-based line number)."""
    path = queue_path(root, session_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = (json.dumps(record, ensure_ascii=False, default=str) + "\n").encode("utf-8")
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
    try:
        os.write(fd, data)
    finally:
        os.close(fd)
    return {"path": str(path), "line": path.read_bytes().count(b"\n")}
