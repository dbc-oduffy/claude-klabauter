"""group-em-memory-governor-wait — the Group EM's one-shot wake on the memory governor's log.

The governor runs as a session-long background shell appending to a log; this waiter is a second
background shell whose exit is the wake. It starts at the log's current end, so lines the Group EM
already acted on never re-wake it, prints the first new complete line, and exits 0. After acting,
relaunch the waiter only, never the governor: a governor restart would drop the trees it holds
paused.

Usage: group-em-memory-governor-wait.py <log> [--interval SECONDS]

A missing log is waited for, not an error. A log that shrinks (rotated or truncated) restarts the
read at 0. Never filter the output: the governor prints only action lines.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path
from typing import Callable, Optional


def _size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def wait_for_line(path: Path, interval: float = 2.0, sleep: Callable[[float], None] = time.sleep,
                  max_polls: Optional[int] = None) -> Optional[str]:
    offset = _size(path)
    pending = b""
    polls = 0
    while max_polls is None or polls < max_polls:
        polls += 1
        size = _size(path)
        if size < offset:
            offset, pending = 0, b""
        if size > offset:
            with path.open("rb") as fh:
                fh.seek(offset)
                chunk = fh.read(size - offset)
            offset += len(chunk)
            pending += chunk
            # Only a newline-terminated line counts: the governor may be mid-write.
            for raw in pending.split(b"\n")[:-1]:
                line = raw.decode("utf-8", "replace").rstrip("\r")
                if line.strip():
                    return line
            pending = pending.rsplit(b"\n", 1)[-1]
        sleep(interval)
    return None


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Wake once on the memory governor's next log line.")
    ap.add_argument("log", type=Path)
    ap.add_argument("--interval", type=float, default=2.0)
    args = ap.parse_args(argv)
    line = wait_for_line(args.log, args.interval)
    if line is not None:
        print(line, flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
