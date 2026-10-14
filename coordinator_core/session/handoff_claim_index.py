"""
coordinator_core.session.handoff_claim_index — signature-revalidated cache of each live
handoff's frontmatter-mirror claimer.

Purpose: let ``stale_claims.list_stale_claim_handoffs`` skip reopening every handoff head. An
entry is ``name -> (mtime_ns, size, mirror_holder)``; a file whose ``(mtime_ns, size)`` is
unchanged keeps its cached holder, anything added or changed is re-read through
``claim_state._read_mirror_claim``, anything removed is dropped.

Invariants:
  * Only the MIRROR half is cached. Ledger claims are not file-signature visible, so the caller
    sends every ledger-named handoff through ``resolve_claim_state`` and lists the ledger
    directory on every call.
  * A missing, corrupt, wrong-version or other-directory index is a cold build, never an error.
  * The index lives under the git common dir (derived, per-checkout, never committed). Writes are
    a same-directory temp file plus ``os.replace``; no lock — last writer wins and every survivor
    is revalidated by its next reader.
  * Trap: an in-place edit that preserves both size and mtime_ns is invisible to the signature.
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Dict, Iterable, Optional, Tuple

from coordinator_core.claim_state import _read_mirror_claim

INDEX_SCHEMA_VERSION = 1

_CACHE_DIRNAME = "coordinator-housekeeping"
_CACHE_FILENAME = "handoff-claim-index.json"

GENERATES = []

_Entry = Tuple[int, int, Optional[str]]


def cache_path_for(common_dir: Path) -> Path:
    return Path(common_dir) / _CACHE_DIRNAME / _CACHE_FILENAME


def _load(cache_path: Optional[Path], handoffs_dir: str) -> Dict[str, _Entry]:
    if cache_path is None:
        return {}
    try:
        with open(cache_path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
        if (
            not isinstance(payload, dict)
            or payload.get("version") != INDEX_SCHEMA_VERSION
            or payload.get("handoffs_dir") != handoffs_dir
        ):
            return {}
        entries = payload["entries"]
        loaded: Dict[str, _Entry] = {}
        for name, raw in entries.items():
            mtime_ns, size, holder = raw
            if holder is not None and not isinstance(holder, str):
                return {}
            loaded[str(name)] = (int(mtime_ns), int(size), holder)
        return loaded
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        return {}


def _save(cache_path: Path, handoffs_dir: str, entries: Dict[str, _Entry]) -> bool:
    payload = {
        "version": INDEX_SCHEMA_VERSION,
        "handoffs_dir": handoffs_dir,
        "entries": {name: list(entry) for name, entry in entries.items()},
    }
    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(
            dir=str(cache_path.parent), prefix=".handoff-claim-index-", suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                json.dump(payload, handle, separators=(",", ":"))
            os.replace(tmp_name, str(cache_path))
        except BaseException:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise
        return True
    except (OSError, TypeError, ValueError):
        return False


def mirror_holders(
    handoff_paths: Iterable[Path],
    *,
    handoffs_dir: Path,
    cache_path: Optional[Path] = None,
) -> Dict[Path, Optional[str]]:
    """Return ``{path: mirror_holder}`` for ``handoff_paths``, re-reading only changed files.

    ``mirror_holder`` is what ``claim_state._read_mirror_claim`` yields (None when unclaimed).
    The persisted index is rewritten only when an entry was added, changed or removed.
    """
    dir_key = str(handoffs_dir)
    cached = _load(cache_path, dir_key)
    fresh: Dict[str, _Entry] = {}
    result: Dict[Path, Optional[str]] = {}
    for path in handoff_paths:
        name = path.name
        try:
            st = os.stat(path)
        except OSError:
            result[path] = _read_mirror_claim(path)[0]
            continue
        hit = cached.get(name)
        if hit is not None and hit[0] == st.st_mtime_ns and hit[1] == st.st_size:
            holder = hit[2]
        else:
            holder = _read_mirror_claim(path)[0]
        fresh[name] = (st.st_mtime_ns, st.st_size, holder)
        result[path] = holder
    if cache_path is not None and fresh != cached:
        _save(cache_path, dir_key, fresh)
    return result
