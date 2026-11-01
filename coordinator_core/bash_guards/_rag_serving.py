"""Does a example-retrieval-repo server serve this repo? One local file read, no network.

The example-retrieval-repo hooks leave a per-box verdict file (`rag-nudge-server.json`, under
`$LOCALAPPDATA/example-retrieval-repo/state` or `~/.project-rag/state`) listing the source names
the running server serves. A repo counts as served when its root directory's name
matches a listed source (case-insensitive) and the verdict is reachable and fresh.
Every failure reads as "not served": a recommendation is only ever added, never required.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Dict, FrozenSet, Optional, Tuple

_VERDICT_FILE = "rag-nudge-server.json"
_MAX_AGE_S = 30 * 60

_parsed: Dict[Tuple[str, int], FrozenSet[str]] = {}


def _verdict_path() -> Path:
    local = os.environ.get("LOCALAPPDATA", "")
    base = Path(local) / "project-rag" / "state" if local else Path.home() / ".project-rag" / "state"
    return base / _VERDICT_FILE


def _served_sources() -> FrozenSet[str]:
    """Lower-cased served source names, or empty when the verdict is absent, unreachable or stale.
    Parsed once per (file, mtime) so a hot path pays one `stat`."""
    path = _verdict_path()
    try:
        st = path.stat()
    except OSError:
        return frozenset()
    if not 0 <= time.time() - st.st_mtime <= _MAX_AGE_S:
        return frozenset()
    key = (str(path), st.st_mtime_ns)
    hit = _parsed.get(key)
    if hit is not None:
        return hit
    names: FrozenSet[str] = frozenset()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict) and data.get("reachable") is True:
            raw = data.get("sources")
            if isinstance(raw, list):
                names = frozenset(s.lower() for s in raw if isinstance(s, str) and s)
    except (OSError, ValueError):
        pass
    _parsed.clear()
    _parsed[key] = names
    return names


def _repo_root(start: str) -> Optional[Path]:
    try:
        here = Path(start).resolve()
    except (OSError, RuntimeError):
        return None
    for cand in (here, *here.parents):
        if (cand / ".git").exists():
            return cand
    return None


def serves_repo(cwd: Optional[str] = None) -> bool:
    """True iff the cached example-retrieval-repo verdict lists the git repo containing `cwd`
    (default: the process cwd) among its served sources."""
    sources = _served_sources()
    if not sources:
        return False
    root = _repo_root(cwd or os.getcwd())
    return root is not None and root.name.lower() in sources
