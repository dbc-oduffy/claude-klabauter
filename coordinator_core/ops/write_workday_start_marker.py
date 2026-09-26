
from __future__ import annotations

GENERATES = []

import sys
from pathlib import Path
from typing import List

from coordinator_core.daily_day import local_day
from coordinator_core.ops.check_weekly_staleness import _resolve_state_root
from coordinator_core.session.declared_writes import declare_write

_MARKER_FILENAME = ".workday-start-marker"


def write_marker() -> "tuple[str, int]":
    state_root_str = _resolve_state_root()
    if not state_root_str:
        sys.stderr.write(
            "write-workday-start-marker: could not resolve state root — skipping\n"
        )
        return ("", 0)

    today = local_day()
    marker_path = Path(state_root_str) / _MARKER_FILENAME

    existing = ""
    if marker_path.is_file():
        existing = marker_path.read_text(encoding="utf-8", errors="replace").strip()
    if existing == today:
        return (f"workday-start-marker: already fresh ({today})\n", 0)

    marker_path.parent.mkdir(parents=True, exist_ok=True)
    marker_path.write_text(today, encoding="utf-8", newline="\n")
    declare_write(marker_path)
    return (f"workday-start-marker: written ({today})\n", 0)


def main(argv: List[str]) -> int:
    if argv:
        sys.stderr.write("usage: write-workday-start-marker.py (no arguments)\n")
        return 2
    text, rc = write_marker()
    if text:
        sys.stdout.write(text)
    return rc


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
