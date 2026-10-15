"""Block until the EM mailbox gains a line, print it, exit 0.

Run as a background command (`run_in_background=true`): the process exits on the event, so the
harness re-invokes the EM. No Monitor wrapper, hence no Monitor timeout. Default start is the
file's current end; `--from-start` delivers lines already present.
"""

from __future__ import annotations

import argparse
import os
import sys
import time

from coordinator_core.workflow_watch.tail import TailReader

POLL_SECONDS = 1.0


def wait_for_line(path: str, *, from_start: bool = False, poll_seconds: float = POLL_SECONDS) -> str:
    """Return the complete lines appended at or after the start point, newline-joined.

    All lines that landed in the same poll are returned: the process exits after one delivery,
    so a line dropped here is never seen by the EM.
    """
    reader = TailReader(path)
    if not from_start:
        try:
            reader._line_offset = os.stat(path).st_size
        except OSError:
            pass
    while True:
        lines = reader.poll_lines()
        if lines:
            return "\n".join(lines)
        time.sleep(poll_seconds)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="mailbox_line")
    parser.add_argument("path")
    parser.add_argument("--from-start", action="store_true")
    args = parser.parse_args(argv)
    sys.stdout.write(wait_for_line(args.path, from_start=args.from_start) + "\n")
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
