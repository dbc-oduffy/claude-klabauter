"""Scanner-agnostic cache store for the amplification gate: content keys, a
racy-window stat memo, an analyzer digest, and a versioned two-blob marshal store.

Knows nothing about AmpSite. The head blob is small (digest, memo, body id); the
body blob is content-addressed by sha1 so a head always names a body that was
written whole. Every load returns None on absence, corruption, or a version or
digest mismatch, and never raises. save never raises.
"""

from __future__ import annotations

import hashlib
import marshal
import os
import sys
import time
from pathlib import Path

from coordinator_core.atomic_replace import atomic_write_bytes

FORMAT_VERSION = 1
RACY_WINDOW_NS = 2_000_000_000
_HEAD_NAME = "head.bin"
_BODY_PREFIX = "body-"
_BODY_SUFFIX = ".bin"
_PRUNE_AGE_S = 60.0


def content_key(data: bytes) -> str:
    return hashlib.sha1(data).hexdigest()


def analyzer_digest(paths, extra: bytes = b"") -> str:
    """Digest of the analyzer's own source plus ``extra``; a changed analyzer
    invalidates every cached summary."""
    h = hashlib.sha1()
    for p in sorted(str(x) for x in paths):
        h.update(p.encode("utf-8", "replace") + b"\0")
        try:
            h.update(Path(p).read_bytes())
        except OSError:
            h.update(b"<missing>")
        h.update(b"\0")
    h.update(extra)
    return h.hexdigest()


class StatMemo:
    """relpath -> content key, trusted only when size and mtime match and the
    mtime was already older than RACY_WINDOW_NS when the entry was recorded
    (a same-size edit inside the window can share an mtime tick)."""

    def __init__(self, entries: dict | None = None):
        self.entries: dict = dict(entries or {})

    def record(self, relpath, size, mtime_ns, key, now_ns: int | None = None) -> None:
        now = time.time_ns() if now_ns is None else now_ns
        self.entries[relpath] = (size, mtime_ns, key, now)

    def trust(self, relpath, size, mtime_ns):
        e = self.entries.get(relpath)
        if e is None:
            return None
        e_size, e_mtime, key, recorded = e
        if e_size != size or e_mtime != mtime_ns:
            return None
        if recorded - e_mtime < RACY_WINDOW_NS:
            return None
        return key

    def to_obj(self) -> dict:
        return dict(self.entries)

    @classmethod
    def from_obj(cls, obj) -> "StatMemo":
        return cls(obj if isinstance(obj, dict) else {})


def _body_path(cache_dir: Path, body_id: str) -> Path:
    return cache_dir / f"{_BODY_PREFIX}{body_id}{_BODY_SUFFIX}"


def load_head(cache_dir, digest: str):
    try:
        env = marshal.loads((Path(cache_dir) / _HEAD_NAME).read_bytes())
        if (
            not isinstance(env, dict)
            or env.get("version") != FORMAT_VERSION
            or env.get("digest") != digest
            or not isinstance(env.get("head"), dict)
            or not isinstance(env.get("body"), str)
        ):
            return None
        head = dict(env["head"])
        head["_body"] = env["body"]
        return head
    except Exception:
        return None


def load_body(cache_dir, head):
    try:
        body_id = head["_body"]
        raw = _body_path(Path(cache_dir), body_id).read_bytes()
        if content_key(raw) != body_id:
            return None
        env = marshal.loads(raw)
        if not isinstance(env, tuple) or len(env) != 2 or env[0] != FORMAT_VERSION:
            return None
        return env[1]
    except Exception:
        return None


def save(cache_dir, head: dict, body, digest: str) -> None:
    """Body first, then head. An unwritable directory prints one stderr line."""
    cache_dir = Path(cache_dir)
    try:
        raw = marshal.dumps((FORMAT_VERSION, body))
        body_id = content_key(raw)
        atomic_write_bytes(_body_path(cache_dir, body_id), raw)
        clean = {k: v for k, v in head.items() if k != "_body"}
        env = {
            "version": FORMAT_VERSION,
            "digest": digest,
            "body": body_id,
            "head": clean,
        }
        atomic_write_bytes(cache_dir / _HEAD_NAME, marshal.dumps(env))
    except Exception:
        print(f"amp-scan cache: cannot write {cache_dir}", file=sys.stderr)
        return
    _prune(cache_dir, body_id)


def _prune(cache_dir: Path, keep: str) -> None:
    try:
        now = time.time()
        for p in cache_dir.glob(f"{_BODY_PREFIX}*{_BODY_SUFFIX}"):
            if p.name == f"{_BODY_PREFIX}{keep}{_BODY_SUFFIX}":
                continue
            try:
                if now - p.stat().st_mtime > _PRUNE_AGE_S:
                    p.unlink()
            except OSError:
                pass
    except OSError:
        pass
