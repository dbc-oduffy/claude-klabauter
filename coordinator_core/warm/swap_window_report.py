"""Swap-window fail-open report: how many guards the door let through unevaluated while a publish
held the swap marker.

The door appends one `ts=<UTC second> ... cause=<text>` line per fail-open to
`<settings home>/state/door-fail-open.log` (door.c `door_log_fail_open`, twin in door_posix.c).
`count_fail_opens` reads one bounded tail of that file and counts the lines inside a window;
`report` prints one advisory stderr line when the count is not zero. Nothing here refuses, denies
or raises: a missing settings home, a missing log or an unreadable one is a typed result.
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import IO, Optional

LOG_RELPATH = ("state", "door-fail-open.log")
#: One read of this many trailing bytes bounds the cost; ~200 bytes per line is ~1300 lines.
TAIL_BYTES = 256 * 1024

STATUS_COUNTED = "counted"
STATUS_NO_SETTINGS_HOME = "no_settings_home"
STATUS_NO_LOG = "no_log"
STATUS_UNREADABLE = "unreadable"

_TS_RE = re.compile(r"\bts=(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})Z")
_CAUSE_RE = re.compile(r"\bcause=(.*)$")
_TS_FORMAT = "%Y-%m-%dT%H:%M:%S"


@dataclass(frozen=True)
class FailOpenCount:
    """`count` is meaningful only for `status == "counted"`. `truncated`: the tail read did not
    reach back to the window start, so `count` is a floor."""

    status: str
    count: int = 0
    first_cause: Optional[str] = None
    truncated: bool = False
    detail: str = ""


def _line_epoch(line: str) -> Optional[int]:
    match = _TS_RE.search(line)
    if match is None:
        return None
    try:
        return int(datetime.strptime(match.group(1), _TS_FORMAT).replace(tzinfo=timezone.utc).timestamp())
    except ValueError:
        return None


def _tail_lines(log: Path) -> tuple[list[str], bool]:
    """The log's trailing lines and whether the file was cut at the front (first partial line dropped)."""
    with log.open("rb") as fh:
        size = fh.seek(0, 2)
        offset = max(0, size - TAIL_BYTES)
        fh.seek(offset)
        data = fh.read()
    lines = data.decode("utf-8", errors="replace").splitlines()
    if offset > 0 and lines:
        lines = lines[1:]
    return lines, offset > 0


def count_fail_opens(settings_home: Optional[Path], start_epoch: float, end_epoch: float) -> FailOpenCount:
    """Door fail-open lines whose UTC second falls inside `[start_epoch, end_epoch]`.

    A line stamped second S covers real time `[S, S+1)`, so it counts when `S >= floor(start)`
    and `S <= end`. Never raises."""
    if settings_home is None:
        return FailOpenCount(STATUS_NO_SETTINGS_HOME)
    log = Path(settings_home).joinpath(*LOG_RELPATH)
    try:
        if not log.is_file():
            return FailOpenCount(STATUS_NO_LOG)
        lines, cut = _tail_lines(log)
    except OSError as exc:
        return FailOpenCount(STATUS_UNREADABLE, detail=repr(exc))
    floor_start = int(start_epoch)
    count = 0
    first_cause: Optional[str] = None
    earliest: Optional[int] = None
    for line in lines:
        stamp = _line_epoch(line)
        if stamp is None:
            continue
        earliest = stamp if earliest is None else min(earliest, stamp)
        if floor_start <= stamp <= end_epoch:
            count += 1
            if first_cause is None:
                cause = _CAUSE_RE.search(line)
                first_cause = cause.group(1).strip() if cause else None
    truncated = cut and (earliest is None or earliest > floor_start)
    return FailOpenCount(STATUS_COUNTED, count, first_cause, truncated)


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%H:%M:%SZ")


def format_report(result: FailOpenCount, start_epoch: float, end_epoch: float) -> Optional[str]:
    """The one `[handover]` line for `result`, or None when there is nothing to say."""
    if result.status == STATUS_UNREADABLE:
        return (
            f"[handover] could not read the door fail-open log ({result.detail}); "
            "guards that failed open during the swap window are unknown"
        )
    if result.status != STATUS_COUNTED or result.count == 0:
        return None
    floor = " (log tail truncated; at least)" if result.truncated else ""
    cause = f"; first cause: {result.first_cause}" if result.first_cause else ""
    return (
        f"[handover] {result.count} guard fail-open(s){floor} logged by the door during the swap window "
        f"{_iso(start_epoch)}..{_iso(end_epoch)} ({max(0.0, end_epoch - start_epoch):.1f}s); "
        f"those guards were allowed unevaluated{cause}"
    )


def resolve_settings_home() -> Optional[Path]:
    """The settings home, or None when it cannot be resolved."""
    try:
        from coordinator_core import _settings_home

        return _settings_home.settings_home()
    except Exception:  # noqa: BLE001 -- an unresolvable home is a typed "none", never a raise
        return None


def report(
    start_epoch: float,
    end_epoch: float,
    err: Optional[IO[str]] = None,
    *,
    settings_home: Optional[Path] = None,
) -> Optional[str]:
    """Print the swap-window report to `err` (stderr by default) and return the line printed, if any.
    Advisory only: never raises, never changes a verdict."""
    try:
        home = settings_home if settings_home is not None else resolve_settings_home()
        line = format_report(count_fail_opens(home, start_epoch, end_epoch), start_epoch, end_epoch)
        if line is not None:
            print(line, file=err if err is not None else sys.stderr)
        return line
    except Exception:  # noqa: BLE001 -- a report never fails the publish
        return None
