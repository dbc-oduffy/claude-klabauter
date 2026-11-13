"""Per-row fix-paths record: the repo paths a queue-grind row's fix touched, created or removed.

Stored at `<run_dir>/fix-paths/<row_id>.json` so the settle-run reconstruction can land a row's
own fix beside its lesson archive. Stdlib only.

Negative-spec:
  - A row id that could escape `fix-paths/` is refused (ValueError).
  - `read` of an absent record returns the empty record; a corrupt one raises ValueError.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Iterable

DIR_NAME = "fix-paths"
KEYS = ("touched", "created", "removed")


def _path(run_dir: Path, row_id: str) -> Path:
    if not row_id or "/" in row_id or "\\" in row_id or row_id in (".", ".."):
        raise ValueError(f"row id escapes the fix-paths directory: {row_id!r}")
    return Path(run_dir) / DIR_NAME / f"{row_id}.json"


def _clean(paths: Iterable[Any]) -> list[str]:
    out = [str(p).replace("\\", "/") for p in paths]
    return list(dict.fromkeys(p for p in out if p))


def write(run_dir: Path, row_id: str, touched: Iterable[str], created: Iterable[str], removed: Iterable[str]) -> Path:
    """Atomically write the row's record (temp file + `os.replace`); returns its path."""
    target = _path(run_dir, row_id)
    record = {"touched": _clean(touched), "created": _clean(created), "removed": _clean(removed)}
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + f".tmp{os.getpid()}")
    tmp.write_text(json.dumps(record, sort_keys=True, indent=2) + "\n", encoding="utf-8", newline="\n")
    os.replace(tmp, target)
    return target


def read(run_dir: Path, row_id: str) -> dict[str, list[str]]:
    """The row's `{touched, created, removed}` record; all-empty when none was written."""
    target = _path(run_dir, row_id)
    if not target.is_file():
        return {k: [] for k in KEYS}
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"unreadable fix-paths record {target}: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"fix-paths record {target} is not a JSON object")
    return {k: _clean(data.get(k) or []) for k in KEYS}
