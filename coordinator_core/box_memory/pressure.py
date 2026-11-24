"""Pure box-memory decisions: pressure class, grep-storm count, remedy wording.

No process spawns, no disk writes. The file-cache trim and its rate limit live in the group-em
memory governor.
"""

from __future__ import annotations

import re
import sys
import time
from pathlib import Path
from typing import Iterable, Optional

LOW = "low"
OK = "ok"
UNKNOWN = "unknown"

KIND_LOW_MEMORY = "low-memory"
KIND_GREP_STORM = "grep-storm"

DEFAULT_STORM_THRESHOLD = 8
MIN_ORPHAN_AGE_S = 30 * 60
MIN_ORPHAN_CPU_S = 10 * 60
_FILETIME_EPOCH_OFFSET_S = 11_644_473_600

_SEARCH_NAME = re.compile(r"(?:grep|egrep|fgrep|find|rg|xargs)(?:\.exe)?", re.IGNORECASE)
_PYTHON_NAME = re.compile(r"python(?:3(?:\.\d+)?)?(?:\.exe)?", re.IGNORECASE)
_ARGV_TOKEN = re.compile(r'"[^"]*"|\S+')
# Interpreter options that take a following argument when written bare (`-W ignore`, `-X dev`).
_ARG_OPTS = frozenset({"-W", "-X"})
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


def is_python_name(name) -> bool:
    """True for a python/python3[.N][.exe] process name or path."""
    return bool(_PYTHON_NAME.fullmatch(str(name).replace("\\", "/").rsplit("/", 1)[-1]))


def is_stdin_python(row, cmdline) -> bool:
    """True for a python image whose script slot is `-` (script read from stdin): `python -`,
    `python3 -I -`, `python.exe -u -`. `-c` and `-m` name their own program and never match; an
    absent or unparsable `cmdline` is False."""
    if not is_python_name(getattr(row, "name", "")) or not cmdline:
        return False
    argv = [t.strip('"') for t in _ARGV_TOKEN.findall(str(cmdline))][1:]
    i = 0
    while i < len(argv):
        tok = argv[i]
        if tok == "-":
            return True
        if not tok.startswith("-"):
            return False
        if tok == "--" or tok.startswith(("--", "-c", "-m")):
            return False
        i += 2 if tok in _ARG_OPTS else 1
    return False


def is_aged(row, now: Optional[int] = None) -> bool:
    """True when `row`'s age (`now` minus ctime, `ProcRow.ctime` units) is at least
    `MIN_ORPHAN_AGE_S`; an unreadable or zero ctime reads as young."""
    ctime = getattr(row, "ctime", 0)
    if not (ctime and ctime > 0):
        return False
    from coordinator_core.bash_guards._host_probe import ctime_units_per_second

    current = now_ctime() if now is None else now
    return current - ctime >= MIN_ORPHAN_AGE_S * ctime_units_per_second()


def now_ctime() -> int:
    """The current instant in `ProcRow.ctime` units for this host: FILETIME (Windows), microseconds
    since the Unix epoch (macOS), clock ticks since boot (Linux)."""
    from coordinator_core.bash_guards._host_probe import ctime_units_per_second

    per_s = ctime_units_per_second()
    if sys.platform == "win32":
        return int((time.time() + _FILETIME_EPOCH_OFFSET_S) * per_s)
    if sys.platform == "darwin":
        return int(time.time() * per_s)
    return int(time.clock_gettime(time.CLOCK_BOOTTIME) * per_s)


def parent_dead(row, by_pid: dict, primitives=None) -> bool:
    """True when `row`'s parent is verified dead: absent from the census with an unreadable
    creation time, or present with a creation time later than the child's (PID reused). A ppid of 0 or a
    zero ctime reads as alive."""
    if row.ppid <= 0:
        return False
    parent = by_pid.get(row.ppid)
    if parent is None:
        # A census can omit a live protected parent; only an unreadable creation time confirms death.
        # (`HostPrimitives.alive` also demands the ctime, which an absent parent has no row for.)
        return not (primitives is not None and primitives.creation_time(row.ppid) is not None)
    return bool(row.ctime and parent.ctime and parent.ctime > row.ctime)


def is_aged_orphan(row, by_pid: dict, primitives=None, now: Optional[int] = None) -> bool:
    """A search-named row whose parent is dead AND whose age is at least `MIN_ORPHAN_AGE_S`.

    A dead Windows parent alone is no orphan proof (Git Bash fork emulation drops the parent of a
    live pipeline). Age is `now` minus `row.ctime`; an unreadable or zero ctime reads as young.
    `now` is in `ProcRow.ctime` units and defaults to `now_ctime()`."""
    ctime = getattr(row, "ctime", 0)
    if not (is_search_name(getattr(row, "name", "")) and ctime and ctime > 0):
        return False
    from coordinator_core.bash_guards._host_probe import ctime_units_per_second

    current = now_ctime() if now is None else now
    if current - ctime < MIN_ORPHAN_AGE_S * ctime_units_per_second():
        return False
    return parent_dead(row, by_pid, primitives)


def storm_count(census: Optional[Iterable], primitives=None, now: Optional[int] = None) -> int:
    """Rows of `census` that are grep/find/rg/xargs aged orphans (`is_aged_orphan`). Young rows and
    rows under a live parent never count; bare name strings carry no ctime and read as young."""
    rows = [r for r in (census or ()) if not isinstance(r, str)]
    by_pid = {r.pid: r for r in rows}
    return sum(1 for r in rows if is_aged_orphan(r, by_pid, primitives, now))


def storm(census: Iterable, threshold: int = DEFAULT_STORM_THRESHOLD, primitives=None,
          now: Optional[int] = None) -> bool:
    """True when the aged-orphan search-tool count is at or over `threshold`."""
    return storm_count(census, primitives, now) >= threshold


def remedy_text(kind: str, bin_dir: Optional[Path] = None) -> str:
    """The remedy a MEMORY event names. `low-memory` reuses the governor's CACHE-TRIM-HINT wording;
    `grep-storm` names the dead-parent reap."""
    if kind == KIND_GREP_STORM:
        return (
            f"reap orphaned grep/find with a dead parent aged >= {MIN_ORPHAN_AGE_S // 60} min "
            "(orphan_reap.reap); younger ones are live pipelines"
        )
    sibling = Path(bin_dir if bin_dir is not None else _BIN_DIR) / "trim-file-cache.py"
    if not sibling.is_file():
        return "GOVERNOR CACHE-TRIM-HINT unavailable: trim-file-cache.py not beside the governor"
    return f'GOVERNOR CACHE-TRIM-HINT run elevated: python "{sibling.as_posix()}" --apply'
