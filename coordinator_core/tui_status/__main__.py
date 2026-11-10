"""`python -m coordinator_core.tui_status {agy,grok}`: one status line from a stdin JSON payload; always exits 0."""
from __future__ import annotations

import json
import sys

from coordinator_core.tui_status import agy, grok
from coordinator_core.tui_status.fields import format_line

RENDERERS = {"agy": agy.line, "grok": grok.line}


def main(argv: list[str]) -> int:
    sys.stdin.reconfigure(encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8")
    renderer = RENDERERS.get(argv[0]) if argv else None
    try:
        payload = json.loads(sys.stdin.read())
        text = renderer(payload) if renderer and isinstance(payload, dict) else format_line(None, None, None)
    except (ValueError, OSError):
        text = format_line(None, None, None)
    sys.stdout.write(text + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
