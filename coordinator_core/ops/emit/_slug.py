
"""Derives a filesystem/id-safe machine slug from a hostname for use in emitted artifact paths."""

from __future__ import annotations

import hashlib
import re
import socket
from typing import Optional

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def machine_slug(hostname: Optional[str] = None) -> str:
    raw = hostname if hostname is not None else socket.gethostname()
    raw = (raw or "unknown").lower()
    slug = _SLUG_RE.sub("-", raw).strip("-")
    return slug or "unknown"


def tracker_machine_slug(hostname: Optional[str] = None) -> str:
    """Injective machine slug: the sanitised hostname plus a 6-hex digest of the raw hostname.

    Carries the tracker's global-uniqueness guarantee, so it refuses an empty
    hostname or a hostname with no alphanumerics instead of sharing a literal.
    Ids and shards minted under the bare ``machine_slug`` form stay readable:
    readers treat ids as opaque and discover shards by glob.
    """
    raw = hostname if hostname is not None else socket.gethostname()
    base = _SLUG_RE.sub("-", (raw or "").lower()).strip("-")
    if not base:
        raise ValueError(f"cannot derive a unique machine slug from hostname {raw!r}")
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:6]
    return f"{base}-{digest}"
