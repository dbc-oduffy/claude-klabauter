"""
coordinator_core.ops.generator_scan_cache -- an (mtime_ns, size)-keyed store
for `generator_provenance.FileWrites`, persisted at
`<machinery_root>/cache/generator-scan-cache.json` (resolved through
`session.machinery_paths.cache_dir`, the declared owner of that bucket) --
plus a SECOND, content-keyed store this module also owns: a git-committed
artifact (`generator-content-cache.json`, sibling of this source file) that
gives a cache-free checkout something to hit on its very first run.

Purpose (stat cache): re-parsing and re-walking every swept module's AST on
every sweep is the cost `discover_generators` pays for having no memory
between runs. This module is that memory -- a flat JSON file keyed on each
file's own (mtime_ns, size) pair, so a caller can skip the parse/scan
entirely for any file whose stat hasn't moved since it was last recorded.
The store itself never decides whether an entry is still valid; it hands
back whatever it has and lets the caller compare stats.

Purpose (content cache): a fresh clone or install has NO stat cache -- git
stores no mtime, so every file's `(mtime_ns, size)` is fresh the moment it
lands on the installing box, and the stat cache misses on every entry
regardless of content
(`docs/research/spike-verdicts/2026-08-31-generator-discovery-cache-rebuild.md`,
carried forward by
`state/bug-backlog/2026-08-22-generator-discovery-ast-parses-71mb-per-94a6779e1ad8.yaml`'s
2026-09-07 and 2026-09-09 notes). A stat miss on a file whose BYTES are
unchanged from what shipped in this repo is still avoidable: hash the bytes
(blake2b, ~609ms/79.4MB measured -- refuted as a PER-RUN identity check
against the 500ms brightline, but paid once on a cold run against a ~48-60s
alternative it is not a close call) and look the digest up in this second,
git-tracked store. The content cache is a pure function of a file's bytes --
never its path, its mtime, or which repo it sits in -- so a hit is exactly as
trustworthy as a fresh scan regardless of where or when the file landed. It
carries no resolution against a tracked-path set: only `generates`/`mutates`/
`write_sites`, the same fields the stat cache holds, resolved against the
LIVE tracked set on every run exactly as a stat-cache hit or a cold scan
would be -- caching a resolved verdict would go stale the moment the tracked
set moved, behind a valid-looking key (spike verdict, Q1).

Both stores are gated by the SAME `_SCHEMA_VERSION`: both hold nothing but
`FileWrites`, produced by the same scanner, so a scanner-semantics change
that invalidates one invalidates the other identically -- there is one
version to bump, not two to keep in lockstep by hand.

Fail-open is the whole contract: `load` and `load_content_cache` NEVER
raise. A cache is an optimisation layered over a sweep that already works
without it -- a missing file, a corrupt one, a concurrent writer's
half-written bytes, or a stale schema version must all degrade to "no
cache, sweep/hash from scratch", never to an exception surfacing out of an
op that was only trying to go faster. `save`/`save_content_cache` write
atomically (temp sibling + `os.replace`) so a torn read is never possible
even under this repo's normal 50+-concurrent-session load, and swallow their
own write failures for the same reason the loaders swallow read failures.

Negative-spec:
  - This module does not scan any file's AST -- it stores and retrieves
    whatever `FileWrites` a caller already produced (`generator_provenance.
    _scan_file_writes`), and imports nothing from that module but the
    `FileWrites` dataclass itself.
  - This module does not resolve a `FileWrites` against a tracked-path set
    or decide staleness/freshness -- it is a dumb keyed store, not a
    verdict engine. This holds for BOTH stores.
  - This module does not know about the tracked set, `git ls-files`, or
    which files the caller intends to sweep. It has no opinion on staleness
    beyond handing back a keyed entry for the caller to compare against its
    own fresh `stat()` (stat cache) or content hash (content cache).
  - This module does not decide when either store is consulted --
    `discover_generators` is the only production consumer of both, calls
    `load`/`save` on every sweep run, and consults `load_content_cache`
    only on a stat-miss (never on the warm path, so a warm run's cost is
    unchanged by this store's existence).
  - This module does not regenerate the content cache -- that is
    `coordinator/bin/regenerate-generator-content-cache.py`'s job. This
    module only loads and saves the bytes; it has no opinion on when they
    are stale beyond the schema-version gate every load already applies.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

from coordinator_core.ops.generator_provenance import FileWrites

_CACHE_FILENAME = "generator-scan-cache.json"
_CONTENT_CACHE_FILENAME = "generator-content-cache.json"
_CONTENT_HASH_DIGEST_SIZE = 16

#: SEMANTICS, not its shape -- it now resolves f-strings and names over the
#: modules that previously scanned as `__MALFORMED__` -> UNDECLARED now report
_SCHEMA_VERSION = 5


def _cache_path(repo_root: Path) -> Path:
    from coordinator_core.session.machinery_paths import cache_dir

    return Path(cache_dir(str(repo_root))) / _CACHE_FILENAME


def _write_site_to_json(target_literal: str | None) -> str | None:
    return target_literal


def _write_site_from_json(data: object) -> str | None:
    if data is not None and not isinstance(data, str):
        raise ValueError("write site entry must be a string or null")
    return data


def file_writes_to_json(writes: FileWrites) -> dict:
    return {
        "generates": writes.generates,
        "mutates": writes.mutates,
        "write_sites": [_write_site_to_json(site) for site in writes.write_sites],
        "syntax_error": writes.syntax_error,
        "write_surface_paths": list(writes.write_surface_paths),
    }


def file_writes_from_json(data: object) -> FileWrites:
    if not isinstance(data, dict):
        raise ValueError("FileWrites entry is not a mapping")
    write_sites_raw = data["write_sites"]
    if not isinstance(write_sites_raw, list):
        raise ValueError("write_sites must be a list")
    syntax_error = data["syntax_error"]
    if not isinstance(syntax_error, bool):
        raise ValueError("syntax_error must be a boolean")
    surface_paths_raw = data["write_surface_paths"]
    if not isinstance(surface_paths_raw, list) or not all(
        isinstance(entry, str) for entry in surface_paths_raw
    ):
        raise ValueError("write_surface_paths must be a list of strings")
    return FileWrites(
        generates=data["generates"],
        mutates=data["mutates"],
        write_sites=[_write_site_from_json(site) for site in write_sites_raw],
        syntax_error=syntax_error,
        write_surface_paths=tuple(surface_paths_raw),
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
        tmp_path.write_text(json.dumps(payload), encoding="utf-8", newline="\n")
        os.replace(tmp_path, path)
    except OSError:
        try:
            if tmp_path.exists():
                tmp_path.unlink()
        except OSError:
            pass


def content_hash(data: bytes) -> str:
    return hashlib.blake2b(data, digest_size=_CONTENT_HASH_DIGEST_SIZE).hexdigest()


def _content_cache_path() -> Path:
    return Path(__file__).resolve().parent / _CONTENT_CACHE_FILENAME


def load_content_cache() -> dict[str, FileWrites]:
    path = _content_cache_path()
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

    entries: dict[str, FileWrites] = {}
    for digest, writes_data in entries_raw.items():
        if not isinstance(digest, str):
            continue
        try:
            writes = file_writes_from_json(writes_data)
        except (ValueError, KeyError, TypeError):
            continue
        entries[digest] = writes
    return entries


def save_content_cache(entries: dict[str, FileWrites]) -> None:
    path = _content_cache_path()
    payload = {
        "schema": _SCHEMA_VERSION,
        "entries": {
            digest: file_writes_to_json(writes)
            for digest, writes in sorted(entries.items())
        },
    }

    tmp_path = path.parent / f"{path.name}.tmp.{os.getpid()}"
    try:
        tmp_path.write_text(
            json.dumps(payload, sort_keys=True), encoding="utf-8", newline="\n"
        )
        os.replace(tmp_path, path)
    except OSError:
        try:
            if tmp_path.exists():
                tmp_path.unlink()
        except OSError:
            pass
