"""Pure box-memory decisions: pressure class, grep-storm count, remedy wording, trim rate limit.

No process spawns. Only the trim stamp touches disk (under .coordinator-local).
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Iterable, Optional

LOW = "low"
OK = "ok"
UNKNOWN = "unknown"

KIND_LOW_MEMORY = "low-memory"
KIND_GREP_STORM = "grep-storm"

#: Mirrors group-em-memory-governor.py LOW_TRIM_COOLDOWN_S.
TRIM_COOLDOWN_S = 120.0
DEFAULT_STORM_THRESHOLD = 8

_SEARCH_NAME = re.compile(r"(?:grep|egrep|fgrep|find|rg|xargs)(?:\.exe)?", re.IGNORECASE)
_REPO_ROOT = Path(__file__).resolve().parents[2]
_BIN_DIR = _REPO_ROOT / "coordinator" / "bin"


def classify(reading, floor_mb: float) -> str:
    """`low` when available RAM is under `floor_mb`; `unknown` when the reading is absent or
    untrusted; else `ok`. `reading` exposes `avail_mb` and `trusted` (`MemoryReading`)."""
    avail = getattr(reading, "avail_mb", None)
    if avail is None or not getattr(reading, "trusted", True):
        return UNKNOWN
    return LOW if avail < floor_mb else OK


def is_search_name(name) -> bool:
    """True for a grep/find/rg/xargs process name or path."""
    return bool(_SEARCH_NAME.fullmatch(str(name).replace("\\", "/").rsplit("/", 1)[-1]))


def storm_count(census: Iterable) -> int:
    """Rows of `census` (items with `.name`, or bare name strings) that are grep/find/rg/xargs."""
    n = 0
    for row in census:
        name = row if isinstance(row, str) else getattr(row, "name", "")
        if is_search_name(name):
            n += 1
    return n


def storm(census: Iterable, threshold: int = DEFAULT_STORM_THRESHOLD) -> bool:
    """True when the search-tool count is at or over `threshold`."""
    return storm_count(census) >= threshold


def remedy_text(kind: str, bin_dir: Optional[Path] = None) -> str:
    """The remedy a MEMORY event names. `low-memory` reuses the governor's CACHE-TRIM-HINT wording;
    `grep-storm` names the dead-parent reap."""
    if kind == KIND_GREP_STORM:
        return "reap orphaned grep/find with a dead parent (orphan_reap.reap)"
    sibling = Path(bin_dir if bin_dir is not None else _BIN_DIR) / "trim-file-cache.py"
    if not sibling.is_file():
        return "GOVERNOR CACHE-TRIM-HINT unavailable: trim-file-cache.py not beside the governor"
    return f'GOVERNOR CACHE-TRIM-HINT run elevated: python "{sibling.as_posix()}" --apply'


def default_stamp_path(root: Optional[Path] = None) -> Path:
    return Path(root if root is not None else _REPO_ROOT) / ".coordinator-local" / "box-memory" / "last-trim.json"


def _read_stamp(stamp_path: Path) -> Optional[dict]:
    try:
        data = json.loads(Path(stamp_path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def last_trim(stamp_path: Path) -> Optional[dict]:
    """The last stamped record (`ts`, `before_mb`, `after_mb`), or None."""
    return _read_stamp(stamp_path)


def trim_due(stamp_path: Path, interval_s: float = TRIM_COOLDOWN_S, now: Optional[float] = None) -> bool:
    """True when no trim is stamped, the stamp is unreadable, or `interval_s` has elapsed."""
    rec = _read_stamp(stamp_path)
    ts = rec.get("ts") if rec else None
    if not isinstance(ts, (int, float)):
        return True
    return (time.time() if now is None else now) - ts >= interval_s


def stamp_trim(stamp_path: Path, before_mb: Optional[float] = None, after_mb: Optional[float] = None,
               now: Optional[float] = None) -> None:
    """Record a trim at `now`; a write failure is swallowed (the limiter fails open to one extra trim)."""
    rec = {"ts": time.time() if now is None else now, "before_mb": before_mb, "after_mb": after_mb}
    try:
        p = Path(stamp_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(rec), encoding="utf-8")
    except OSError:
        pass
