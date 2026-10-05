"""Blob-sha keyed derived cache for the declared-pair census.

Maps ``{index blob sha -> Declarations | None}`` in one JSON file under the machinery
cache dir. Never executes code on read (JSON, not pickle). ``load`` never raises: absence,
corruption, or any header mismatch reads as an empty cache. ``save`` is atomic, writes
exactly the entries it is given (prune), and never raises.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Any, Mapping

from coordinator_core.atomic_replace import atomic_write_bytes

CACHE_FILENAME = "generator-census.json"

_PACKAGE_DIR = Path(__file__).resolve().parent


def extractor_digest() -> str:
    """sha1 over the package's own ``.py`` sources, newline-normalised.

    Covers the store's own format too, so no separate format version exists.
    """
    h = hashlib.sha1()
    for path in sorted(_PACKAGE_DIR.glob("*.py")):
        h.update(path.name.encode("utf-8") + b"\0")
        h.update(path.read_bytes().replace(b"\r\n", b"\n"))
    return h.hexdigest()


def _declarations_module():
    from coordinator_core.ops.generator_census import declarations

    return declarations


def _encode(value: Any, decl: Any) -> Any:
    if value is None:
        return None
    if value is getattr(decl, "MALFORMED", object()):
        return {"m": 1}
    if isinstance(value, dict):
        return {"o": {k: _encode(v, decl) for k, v in value.items()}}
    if isinstance(value, decl.Declarations):
        return {
            "d": {name: _encode(getattr(value, name), decl) for name in value.__dataclass_fields__}
        }
    if isinstance(value, tuple):
        return {"t": [_encode(v, decl) for v in value]}
    if isinstance(value, list):
        return [_encode(v, decl) for v in value]
    return value


def _decode(value: Any, decl: Any) -> Any:
    if isinstance(value, dict):
        if "m" in value:
            return decl.MALFORMED
        if "o" in value:
            return {k: _decode(v, decl) for k, v in value["o"].items()}
        if "t" in value:
            return tuple(_decode(v, decl) for v in value["t"])
        return decl.Declarations(**{k: _decode(v, decl) for k, v in value["d"].items()})
    if isinstance(value, list):
        return [_decode(v, decl) for v in value]
    return value


def _normalise_header(header: Mapping[str, Any]) -> Any:
    return json.loads(json.dumps(dict(header), sort_keys=True))


def _cache_path(cache_dir: str | os.PathLike) -> str:
    return os.path.join(os.fspath(cache_dir), CACHE_FILENAME)


def load(cache_dir: str | os.PathLike, header: Mapping[str, Any]) -> dict[str, Any]:
    """Return the cached entries, or ``{}`` on absence, corruption, or header mismatch."""
    try:
        with open(_cache_path(cache_dir), "rb") as f:
            blob = json.loads(f.read())
        if blob["header"] != _normalise_header(header):
            return {}
        decl = _declarations_module()
        return {sha: _decode(v, decl) for sha, v in blob["entries"].items()}
    except Exception:
        return {}


def save(
    cache_dir: str | os.PathLike, header: Mapping[str, Any], entries: Mapping[str, Any]
) -> None:
    """Atomically write exactly ``entries``; an unwritable dir logs one stderr line."""
    try:
        decl = _declarations_module()
        data = json.dumps(
            {
                "header": _normalise_header(header),
                "entries": {sha: _encode(v, decl) for sha, v in entries.items()},
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        path = _cache_path(cache_dir)
        try:
            with open(path, "rb") as f:
                if f.read() == data:
                    return
        except OSError:
            pass
        atomic_write_bytes(path, data)
    except Exception as exc:
        print(f"generator_census.store: cache not written: {exc}", file=sys.stderr)
