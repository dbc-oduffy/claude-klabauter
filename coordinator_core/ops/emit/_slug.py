
from __future__ import annotations

import re
import socket
from typing import Optional

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def machine_slug(hostname: Optional[str] = None) -> str:
    raw = hostname if hostname is not None else socket.gethostname()
    raw = (raw or "unknown").lower()
    slug = _SLUG_RE.sub("-", raw).strip("-")
    return slug or "unknown"
