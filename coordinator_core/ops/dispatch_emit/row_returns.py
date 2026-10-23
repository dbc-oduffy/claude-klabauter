"""Row returns: the replies of finished rows another row waits on, persisted beside ``kept.json``.

Invariant: ``returns.json`` lives in the run record dir and is committed with it, so a resume on any
box reads the figures from the record, never from the box-local dispatch report. Stdlib only;
spawns no process.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Mapping, Optional

from coordinator_core.session.declared_writes import declare_write

RETURNS_FILE = "returns.json"
CONSUMED_RETURNS_PARAM = "consumed_returns"

# Bounds the committed record per consumed row while holding a verify-only row's figures whole.
RETURN_CAP_CHARS = 32000


def digest(text: str) -> str:
    """sha256 hex of ``text`` encoded UTF-8: the fingerprint ``kept.json`` records for a return."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def is_done(text: object) -> bool:
    """True when a reply's first line opens with ``DONE``."""
    return isinstance(text, str) and text.lstrip().upper().startswith("DONE")


def _capped(text: str, report_path: Optional[str]) -> str:
    if len(text) <= RETURN_CAP_CHARS:
        return text
    where = report_path or "unrecorded"
    return text[:RETURN_CAP_CHARS] + (
        f"\n[reply cut at {RETURN_CAP_CHARS} chars; full reply in box-local report {where}]"
    )


def write(
    record_dir: Path,
    returns: Mapping[str, str],
    report_paths: Optional[Mapping[str, str]] = None,
) -> Optional[Path]:
    """Write ``returns.json`` (``{row: {"text", "sha256"}}``, UTF-8, LF, sorted keys); None and no file when empty."""
    entries = {}
    for row, text in returns.items():
        if isinstance(row, str) and isinstance(text, str):
            cut = _capped(text, (report_paths or {}).get(row))
            entries[row] = {"text": cut, "sha256": digest(cut)}
    if not entries:
        return None
    path = Path(record_dir) / RETURNS_FILE
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(json.dumps(entries, sort_keys=True, indent=2) + "\n")
    except OSError:
        return None
    declare_write(path)
    return path


def read(record_dir: Path) -> dict[str, dict]:
    """``{row: {"text", "sha256"}}``; malformed or sha-mismatched entries dropped. Never raises."""
    try:
        data = json.loads((Path(record_dir) / RETURNS_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    out: dict[str, dict] = {}
    for row, entry in data.items():
        if not isinstance(entry, dict):
            continue
        text, sha = entry.get("text"), entry.get("sha256")
        if isinstance(text, str) and isinstance(sha, str) and digest(text) == sha:
            out[row] = {"text": text, "sha256": sha}
    return out


def persisted_path(record_dir_rel: str) -> str:
    """Repo-relative POSIX path to ``returns.json`` under the record dir."""
    return f"{str(record_dir_rel).replace(chr(92), '/').rstrip('/')}/{RETURNS_FILE}"
