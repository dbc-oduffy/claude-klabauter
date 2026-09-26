from __future__ import annotations

import sys
from typing import List

from coordinator_core.text.sentinel_blocks import extract_block


def _usage() -> None:
    print(
        "Usage: sentinel-blocks-cli.js extract <file> <begin-marker> <end-marker>",
        file=sys.stderr,
    )


def main(argv: List[str]) -> int:
    if not argv:
        _usage()
        return 1

    command = argv[0]
    rest = argv[1:]

    if command == "extract":
        if len(rest) < 3:
            _usage()
            return 1
        file, begin_marker, end_marker = rest[0], rest[1], rest[2]

        try:
            with open(file, "r", encoding="utf-8") as f:
                content = f.read()
        except OSError as exc:
            print(
                f"sentinel-blocks-cli: cannot read file: {file}: {exc}",
                file=sys.stderr,
            )
            return 1

        result = extract_block(content, begin_marker, end_marker)
        if result is None:
            print(
                f"sentinel-blocks-cli: markers not found in {file}\n"
                f"  begin: {begin_marker}\n"
                f"  end:   {end_marker}",
                file=sys.stderr,
            )
            return 1

        sys.stdout.write(result["block"])
        return 0

    print(f"sentinel-blocks-cli: unknown command: {command}", file=sys.stderr)
    _usage()
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
