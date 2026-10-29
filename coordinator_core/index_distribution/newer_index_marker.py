"""Box-local newer-index marker: a small JSON file a refresh writes and SessionStart reads.

The marker is never committed and a read never touches the network or spawns. Shape:
`{"kind": "structural"|"semantic", "checked_at": <epoch seconds>, "newer_available": bool,
"detail": <str, optional>, "local_rev"/"remote_rev": <ISO str, optional>}`. Any unreadable or malformed file reads as `None`.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Mapping

MARKER_RELPATH = ".coordinator-local/newer-index-marker.json"
TTL_SECONDS = 24 * 60 * 60
KINDS = ("structural", "semantic")


def _path(repo_root: Path | str) -> Path:
    return Path(repo_root) / MARKER_RELPATH


def read_marker(repo_root: Path | str) -> dict[str, Any] | None:
    """Return the marker dict, or None when absent, unreadable, or not a valid marker."""
    try:
        data = json.loads(_path(repo_root).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("kind") not in KINDS:
        return None
    checked = data.get("checked_at")
    if isinstance(checked, bool) or not isinstance(checked, (int, float)):
        return None
    return data


def write_marker(repo_root: Path | str, marker: Mapping[str, Any]) -> Path:
    """Atomically write `marker` (kind and checked_at required); returns the marker path."""
    if marker.get("kind") not in KINDS:
        raise ValueError(f"marker kind must be one of {KINDS}")
    checked = marker.get("checked_at")
    if isinstance(checked, bool) or not isinstance(checked, (int, float)):
        raise ValueError("marker checked_at must be epoch seconds")
    path = _path(repo_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(dict(marker), sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)
    return path


def is_fresh(marker: Mapping[str, Any] | None, now: float) -> bool:
    """True when the marker was checked within TTL_SECONDS of `now` (a future stamp counts fresh)."""
    if not marker:
        return False
    checked = marker.get("checked_at")
    if isinstance(checked, bool) or not isinstance(checked, (int, float)):
        return False
    return now - checked < TTL_SECONDS


def newer_available(marker: Mapping[str, Any] | None) -> bool:
    """True only when the marker explicitly records a newer index."""
    return bool(marker) and marker.get("newer_available") is True
