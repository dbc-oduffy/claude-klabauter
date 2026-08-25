"""
coordinator_core.__main__ — retired daemon entrypoint.

The coordinator_core resident daemon was retired by DR-215 (command-type execution
model). Invoking this module prints a retirement notice and exits 1.

Use coordinator_core.invoke for command-type dispatch instead.

DR-215 backlink: docs/decisions/DR-215-coordinator-core-command-type-execution-model.md
"""

# popup-safe-env-suppressed -- no subprocess calls in this file.

from __future__ import annotations

import sys


_RETIREMENT_MSG = (  # popup-safe-env-suppressed: string literal, not a subprocess call
    "coordinator_core resident daemon retired by DR-215"
    " — use: python -m coordinator_core.invoke <op> '<params>'"
)


def main() -> None:
    print(_RETIREMENT_MSG, file=sys.stderr)
    sys.exit(1)


if __name__ == "__main__":
    main()
