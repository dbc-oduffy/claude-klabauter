
from __future__ import annotations

import os
import re
import sys
from typing import List, Optional

from coordinator_core.frontmatter.primitives import unquote_yaml_scalar

_PROG = "extract-scope-paths.sh"


def _extract_scope_paths(text: str, key: str = "scope") -> List[str]:
    paths: List[str] = []
    found = False
    prefix = f"{key}:"
    for line in text.splitlines():
        if not found:
            if line.startswith(prefix):
                found = True
            continue
        if line.startswith("  - "):
            paths.append(unquote_yaml_scalar(line[4:].strip()))
            continue
        if line.startswith("---"):
            break
        if re.match(r"^[a-z]", line):
            break
    return paths


def main(argv: Optional[List[str]] = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)

    if len(args) < 1:
        print(f"usage: {_PROG} <handoff-file>", file=sys.stderr)
        return 2

    handoff = args[0]

    if not os.path.isfile(handoff):
        print(f"{_PROG.replace('.sh', '')}: file not found: {handoff}", file=sys.stderr)
        return 2

    try:
        with open(handoff, "r", encoding="utf-8", errors="replace") as fh:
            text = fh.read()
    except OSError as exc:
        print(f"{_PROG.replace('.sh', '')}: file not found: {handoff} ({exc})", file=sys.stderr)
        return 2

    scope = _extract_scope_paths(text)

    if not scope:
        print(
            f"{_PROG.replace('.sh', '')}: scope: block missing or empty in {handoff}",
            file=sys.stderr,
        )
        return 1

    for path in scope:
        print(path)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
