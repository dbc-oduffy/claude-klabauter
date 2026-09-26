

from __future__ import annotations

import sys


_RETIREMENT_MSG = (
    "coordinator_core resident daemon retired by DR-215"
    " — use: python -m coordinator_core.invoke <op> '<params>'"
)


def main() -> None:
    print(_RETIREMENT_MSG, file=sys.stderr)
    sys.exit(1)


if __name__ == "__main__":
    main()
