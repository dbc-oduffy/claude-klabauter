"""
coordinator_core.ops.generator_scan_cache -- an (mtime_ns, size)-keyed store
for `generator_provenance.FileWrites`, persisted at
`<repo_root>/state/cache/generator-scan-cache.json`.

Purpose: re-parsing and re-walking every swept module's AST on every sweep is
the cost `discover_generators` pays for having no memory between runs. This
module is that memory -- a flat JSON file keyed on each file's own
(mtime_ns, size) pair, so a caller can skip the parse/scan entirely for any
file whose stat hasn't moved since it was last recorded. The store itself
never decides whether an entry is still valid; it hands back whatever it has
and lets the caller compare stats.

Fail-open is the whole contract: `load` NEVER raises. A cache is an
optimisation layered over a sweep that already works without it -- a missing
file, a corrupt one, a concurrent writer's half-written bytes, or a stale
schema version must all degrade to "no cache, sweep from scratch", never to
an exception surfacing out of an op that was only trying to go faster.
`save` writes atomically (temp sibling + `os.replace`) so a torn read is
never possible even under this repo's normal 50+-concurrent-session load,
and swallows its own write failures for the same reason `load` swallows read
failures.

Negative-spec:
  - This module does not scan any file's AST -- it stores and retrieves
    whatever `FileWrites` a caller already produced (`generator_provenance.
    _scan_file_writes`), and imports nothing from that module.
  - This module does not resolve a `FileWrites` against a tracked-path set
    or decide staleness/freshness -- it is a dumb keyed store, not a
    verdict engine.
  - This module does not know about the tracked set, `git ls-files`, or
    which files the caller intends to sweep. It has no opinion on staleness
    beyond handing back a `(mtime_ns, size)`-keyed entry for the caller to
    compare against its own fresh `stat()`.
  - This module does not decide when it is called -- `discover_generators`
    is its only production consumer, wired in via C4, and calls `load`/
    `save` on every sweep run.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from coordinator_core.ops.generator_provenance import FileWrites

_CACHE_RELATIVE_PATH = ("state", "cache", "generator-scan-cache.json")
_SCHEMA_VERSION = 2


def _cache_path(repo_root: Path) -> Path:
    """Resolve the cache file path from *repo_root* alone.

    No home directory, no environment variable, no hardcoded drive letter --
    the path is always `<repo_root>/state/cache/generator-scan-cache.json`.
    """
    path = repo_root
    for part in _CACHE_RELATIVE_PATH:
        path = path / part
    return path


def _write_site_to_json(target_literal: str | None) -> str | None:
    return target_literal


def _write_site_from_json(data: object) -> str | None:
    if data is not None and not isinstance(data, str):
        raise ValueError("write site entry must be a string or null")
    return data


def file_writes_to_json(writes: FileWrites) -> dict:
    """Serialize *writes* to a JSON-able mapping.

    `generates`/`mutates` are already `json`-safe Python values (the raw
    `ast.literal_eval` output a caller produced upstream -- a list, a
    string sentinel, a dict, or None) and are stored as-is. `write_sites`
    entries are already JSON-able (`str | None`) -- a site R1/R2/R5/R7
    excludes never reaches `FileWrites.write_sites` in the first place, so
    there is nothing left to filter here.
    """
    return {
        "generates": writes.generates,
        "mutates": writes.mutates,
        "write_sites": [_write_site_to_json(site) for site in writes.write_sites],
        "syntax_error": writes.syntax_error,
    }


def file_writes_from_json(data: object) -> FileWrites:
    """Inverse of `file_writes_to_json`. Raises on any malformed shape --
    callers on the read path are expected to catch broadly, per this
    module's fail-open contract at the store level."""
    if not isinstance(data, dict):
        raise ValueError("FileWrites entry is not a mapping")
    write_sites_raw = data["write_sites"]
    if not isinstance(write_sites_raw, list):
        raise ValueError("write_sites must be a list")
    syntax_error = data["syntax_error"]
    if not isinstance(syntax_error, bool):
        raise ValueError("syntax_error must be a boolean")
    return FileWrites(
        generates=data["generates"],
        mutates=data["mutates"],
        write_sites=[_write_site_from_json(site) for site in write_sites_raw],
        syntax_error=syntax_error,
    )


def _entry_from_json(rel_path: str, data: object) -> tuple[str, dict] | None:
    if not isinstance(data, dict):
        return None
    mtime_ns = data.get("mtime_ns")
    size = data.get("size")
    if not isinstance(mtime_ns, int) or not isinstance(size, int):
        return None
    try:
        writes = file_writes_from_json(data.get("writes"))
    except (ValueError, KeyError, TypeError):
        return None
    return rel_path, {"mtime_ns": mtime_ns, "size": size, "writes": writes}


def load(repo_root: Path) -> dict:
    """Load the scan cache for *repo_root*.

    Returns a mapping of `<posix rel path>` -> `{"mtime_ns": int, "size":
    int, "writes": FileWrites}`. NEVER raises: a missing file, an unreadable
    file, invalid or truncated JSON, a wrong schema value, or any malformed
    entry each degrade to an empty mapping -- either for the whole file (a
    top-level shape failure) or per-entry (a single bad entry is dropped,
    the rest of a well-formed file is still usable).
    """
    path = _cache_path(repo_root)
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return {}

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {}

    if not isinstance(data, dict):
        return {}
    if data.get("schema") != _SCHEMA_VERSION:
        return {}
    entries_raw = data.get("entries")
    if not isinstance(entries_raw, dict):
        return {}

    entries: dict = {}
    for rel_path, entry_data in entries_raw.items():
        if not isinstance(rel_path, str):
            continue
        parsed = _entry_from_json(rel_path, entry_data)
        if parsed is None:
            continue
        key, value = parsed
        entries[key] = value
    return entries


def save(repo_root: Path, entries: dict) -> None:
    """Persist *entries* atomically. Swallows every write failure -- a
    cache that cannot be written must not break the op that was only trying
    to go faster. `entries` has the same shape `load` returns.

    Written via a temp sibling in the same directory plus `os.replace`, so
    a reader never observes a torn file even with concurrent writers.
    """
    path = _cache_path(repo_root)
    payload = {
        "schema": _SCHEMA_VERSION,
        "entries": {
            rel_path: {
                "mtime_ns": entry["mtime_ns"],
                "size": entry["size"],
                "writes": file_writes_to_json(entry["writes"]),
            }
            for rel_path, entry in entries.items()
        },
    }

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        return

    tmp_path = path.parent / f"{path.name}.tmp.{os.getpid()}"
    try:
        tmp_path.write_text(json.dumps(payload), encoding="utf-8")
        os.replace(tmp_path, path)
    except OSError:
        try:
            if tmp_path.exists():
                tmp_path.unlink()
        except OSError:
            pass
