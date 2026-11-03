"""Pure box-memory decisions: pressure class, grep-storm count, remedy wording.

No process spawns, no disk writes. The file-cache trim and its rate limit live in the group-em
memory governor.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Iterable, Optional

LOW = "low"
OK = "ok"
UNKNOWN = "unknown"

KIND_LOW_MEMORY = "low-memory"
KIND_GREP_STORM = "grep-storm"

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
