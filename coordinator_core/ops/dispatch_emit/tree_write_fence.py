"""Refuse-and-record fence for engine ops that write run-held bytes into a live worktree.

`check_tree_write(rel, owned_paths=..., base_blob=..., head_blob=...)` returns a refusal reason, or
None when the write is admitted. `record_refusal(record_dir, rel, reason, run_id)` appends the
refusal to `<record_dir>/tree-write-refusals.json`. Leaf module, zero git spawns: the caller
supplies blob ids (as `live_write_guard` does through `git_state.head_blobs`).
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional

OUTSIDE_OWN_PATHS = "outside-own-paths"
OLDER_THAN_HEAD = "older-than-head"
REFUSALS_FILE = "tree-write-refusals.json"


def _norm(rel: str) -> str:
    text = str(rel).replace("\\", "/")
    return text[2:] if text.startswith("./") else text


def check_tree_write(
    rel: str,
    *,
    owned_paths: Optional[Iterable[str]],
    base_blob: Optional[str],
    head_blob: Optional[str],
) -> Optional[str]:
    """Reason to refuse writing run-held bytes to live `rel`, or None to admit.

    `owned_paths` is the writing run's own write set; None or empty owns nothing, so every path is
    refused rather than written unfenced. `base_blob` is the blob id the bytes were taken from
    (None: the path was absent), `head_blob` the blob id at current HEAD (None: absent). A write
    whose base differs from HEAD would lay older bytes over a later commit.
    """
    owned = {_norm(p) for p in (owned_paths or ())}
    if _norm(rel) not in owned:
        return OUTSIDE_OWN_PATHS
    if base_blob != head_blob:
        return OLDER_THAN_HEAD
    return None


def record_refusal(record_dir, rel: str, reason: str, run_id: Optional[str]) -> Path:
    """Append one refusal entry to `<record_dir>/tree-write-refusals.json` and claim the file.

    An unparseable existing file is moved aside to `.corrupt` rather than overwritten.
    """
    from coordinator_core.session.declared_writes import declare_write

    path = Path(record_dir) / REFUSALS_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    entries: list = []
    if path.exists():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(loaded, list):
                raise ValueError("not a list")
            entries = loaded
        except (ValueError, OSError):
            os.replace(path, path.with_name(REFUSALS_FILE + ".corrupt"))
    if any(e.get("path") == _norm(rel) and e.get("reason") == reason and e.get("run_id") == run_id
           for e in entries if isinstance(e, dict)):
        return path
    entries.append({
        "path": _norm(rel),
        "reason": reason,
        "run_id": run_id,
        "at": datetime.now(timezone.utc).isoformat(),
    })
    tmp = path.with_name(REFUSALS_FILE + ".tmp")
    tmp.write_text(json.dumps(entries, indent=2) + "\n", encoding="utf-8", newline="\n")
    os.replace(tmp, path)
    declare_write(path)
    return path
